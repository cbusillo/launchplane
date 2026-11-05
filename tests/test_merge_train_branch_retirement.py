import unittest
from copy import deepcopy
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from unittest.mock import Mock
from tempfile import TemporaryDirectory
from pathlib import Path

from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.contracts.merge_train_batch import build_merge_train_batch_landing_plan
from control_plane.merge_train_batch_landing import _cleanup_merge_train_batch_candidate_ref

from control_plane.merge_train_controller_run_once import (
    _close_service_batch_pull_request,
    _supersede_active_merge_train_batch_candidate_records,
    _resume_merge_train_controller_state,
    MergeTrainControllerRunOnceEnvelope,
)

from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubError,
    merge_train_construction_ref,
)
from tests.test_merge_train_structural_provenance import _entry, _records


class _RetirementProvider:
    def __init__(self, refs: tuple[str, ...]) -> None:
        self.refs = {ref: "a" * 40 for ref in refs}
        self.pulls: list[dict[str, Any]] = []
        self.deleted: list[str] = []
        self.move_before_delete = False
        self.fail_delete = False
        self.incomplete_pulls = False

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        if method == "GET" and "/git/ref/" in path:
            ref = "refs/" + unquote(path.split("/git/ref/", 1)[1])
            if ref not in self.refs:
                raise MergeTrainGitHubError("missing", status_code=404)
            return {"ref": ref, "object": {"sha": self.refs[ref]}}
        if method == "GET" and "/pulls?" in path:
            if self.incomplete_pulls:
                return [{}] * 100
            query = parse_qs(urlsplit(path).query)
            field = "head" if "head" in query else "base"
            branch = query[field][0].removeprefix("acme:")
            return [
                deepcopy(pull)
                for pull in self.pulls
                if pull["state"] == "open" and pull[field]["ref"] == branch
            ]
        if method == "GET" and path == "/repos/acme/app":
            return {"node_id": "repository-node"}
        if method == "POST" and path == "/graphql":
            assert body is not None
            variables: Any = body["variables"]
            update = variables["input"]["refUpdates"][0]
            reference = update["name"]
            if self.move_before_delete:
                self.refs[reference] = "b" * 40
            if self.fail_delete:
                raise MergeTrainGitHubError("unavailable", status_code=502)
            if self.refs.get(reference) != update["beforeOid"]:
                return {"errors": [{"message": "reference changed"}]}
            assert update["afterOid"] == "0" * 40
            assert not update.get("force")
            del self.refs[reference]
            self.deleted.append(reference)
            return {"data": {"updateRefs": {"clientMutationId": None}}}
        raise AssertionError((method, path, body))


class MergeTrainBranchRetirementTests(unittest.TestCase):
    def setUp(self) -> None:
        record, _ = _records((_entry(1, 1), _entry(2, 2)), repository="acme/app")
        self.candidate = record.candidate
        self.refs = (
            self.candidate.candidate_ref,
            merge_train_construction_ref(self.candidate.candidate_ref),
        )
        self.provider = _RetirementProvider(self.refs)
        self.client = GitHubMergeTrainClient(transport=self.provider)

    def test_retirement_removes_published_and_unpublished_batch_refs_idempotently(self) -> None:
        for status, sha in (("passed", self.candidate.candidate_sha), ("failed", "")):
            with self.subTest(status=status):
                self.provider.refs = {ref: "a" * 40 for ref in self.refs}
                candidate = self.candidate.model_copy(
                    update={"status": status, "candidate_sha": sha}
                )
                self.assertTrue(self.client.cleanup_batch_candidate_ref(landing_plan=candidate))
                self.assertFalse(self.client.cleanup_batch_candidate_ref(landing_plan=candidate))
                self.assertEqual(self.provider.refs, {})

    def test_open_pr_heads_and_bases_and_running_batches_keep_their_refs(self) -> None:
        for field in ("head", "base", "running"):
            with self.subTest(field=field):
                self.provider.refs = {ref: "a" * 40 for ref in self.refs}
                branch = self.refs[0].removeprefix("refs/heads/")
                self.provider.pulls = []
                if field != "running":
                    self.provider.pulls = [
                        {
                            "state": "open",
                            "head": {"ref": branch if field == "head" else "other"},
                            "base": {"ref": branch if field == "base" else "other"},
                        }
                    ]
                self.client.cleanup_batch_candidate_ref(
                    landing_plan=self.candidate,
                    protected_refs=frozenset(self.refs) if field == "running" else frozenset(),
                )
                self.assertIn(self.refs[0], self.provider.refs)
                if field == "running":
                    self.assertEqual(set(self.provider.refs), set(self.refs))

    def test_incomplete_pr_reads_and_provider_failures_keep_refs(self) -> None:
        for failure in ("incomplete_pulls", "fail_delete"):
            with self.subTest(failure=failure):
                setattr(self.provider, failure, True)
                with self.assertRaises(MergeTrainGitHubError):
                    self.client.cleanup_batch_candidate_ref(landing_plan=self.candidate)
                self.assertEqual(set(self.provider.refs), set(self.refs))
                setattr(self.provider, failure, False)

    def test_changed_tip_cannot_be_deleted_after_qualification(self) -> None:
        self.provider.move_before_delete = True
        with self.assertRaises(MergeTrainGitHubError):
            self.client.cleanup_batch_candidate_ref(landing_plan=self.candidate)
        self.assertEqual(self.provider.deleted, [])
        self.assertEqual(set(self.provider.refs), set(self.refs))

    def test_landing_cleanup_uses_the_scoped_executor_for_both_refs(self) -> None:
        plan = build_merge_train_batch_landing_plan(
            candidate=self.candidate, merge_method="merge", created_at=self.candidate.updated_at
        )
        executor = Mock()
        executor.delete_candidate_ref.return_value = True
        self.client.cleanup_batch_candidate_ref(landing_plan=plan, effect_executor=executor)
        effects = [call.args[0] for call in executor.delete_candidate_ref.call_args_list]
        self.assertEqual({effect.candidate_ref for effect in effects}, set(self.refs))
        self.assertTrue(all(effect.lineage.landing_plan_id == plan.plan_id for effect in effects))
        self.assertTrue(
            all(
                effect.expected_ref_sha == self.provider.refs[effect.candidate_ref]
                for effect in effects
            )
        )

    def test_direct_landing_reports_retained_when_an_open_pr_keeps_its_ref(self) -> None:
        plan = build_merge_train_batch_landing_plan(
            candidate=self.candidate, merge_method="merge", created_at=self.candidate.updated_at
        )
        branch = plan.candidate_ref.removeprefix("refs/heads/")
        self.provider.pulls = [{"state": "open", "head": {"ref": branch}, "base": {"ref": "other"}}]
        result = _cleanup_merge_train_batch_candidate_ref(
            github_client=self.client, landing_plan=plan, request_trace_id="retained-fixture"
        )
        self.assertEqual(result["candidate_ref_cleanup_status"], "retained")
        self.assertIn(plan.candidate_ref, self.provider.refs)

    def test_non_owned_ref_is_refused_before_provider_calls(self) -> None:
        for reference in ("refs/heads/main", "refs/heads/work/source"):
            with self.subTest(reference=reference):
                candidate = self.candidate.model_copy(update={"candidate_ref": reference})
                with self.assertRaises(MergeTrainGitHubError):
                    self.client.cleanup_batch_candidate_ref(landing_plan=candidate)
                self.assertEqual(self.provider.deleted, [])

    def test_terminal_construction_retires_both_refs_after_persisting_supersession(self) -> None:
        for fail_delete in (False, True):
            with self.subTest(fail_delete=fail_delete), TemporaryDirectory() as directory:
                record, _ = _records((_entry(1, 1), _entry(2, 2)), repository="acme/app")
                record = record.model_copy(
                    update={
                        "candidate": record.candidate.model_copy(
                            update={
                                "candidate_sha": "",
                                "candidate_tree_sha": "",
                                "structural_provenance": None,
                                "status": "failed",
                            }
                        )
                    }
                )
                store = FilesystemRecordStore(Path(directory))
                store.write_merge_train_batch_candidate_record(record)
                self.provider.refs = {ref: "a" * 40 for ref in self.refs}
                self.provider.fail_delete = fail_delete
                kwargs: dict[str, Any] = dict(
                    github_client=self.client,
                    record_store=store,
                    repository=record.candidate.repository,
                    base_branch=record.candidate.base_branch,
                    batch_id=record.candidate.batch_id,
                    replacement_record_id=None,
                    lease=Mock(),
                )
                if fail_delete:
                    with self.assertRaises(MergeTrainGitHubError):
                        _supersede_active_merge_train_batch_candidate_records(**kwargs)
                else:
                    _supersede_active_merge_train_batch_candidate_records(**kwargs)
                    self.assertEqual(self.provider.refs, {})
                self.assertEqual(
                    store.list_merge_train_batch_candidate_records(status="active"), ()
                )
                if fail_delete:
                    self.provider.fail_delete = False
                    lease = Mock(
                        record=Mock(
                            ordinary_job_binding=None,
                            active_action="reflow_candidate",
                            active_phase="cleanup_retired_candidate_refs",
                            reconciliation_detail="",
                            reconciliation_status="adopted",
                            active_record_id=record.record_id,
                        )
                    )
                    _resume_merge_train_controller_state(
                        request=MergeTrainControllerRunOnceEnvelope(
                            repository=record.candidate.repository, mutate=True
                        ),
                        policy_sha256=record.candidate.policy_sha256,
                        repository_policy=Mock(),
                        trace_id="resume",
                        recorded_at=record.updated_at,
                        github_client=self.client,
                        candidate_store=store,
                        landing_store=store,
                        stack_collapse_store=store,
                        admission_store=store,
                        lease=lease,
                    )
                    self.assertEqual(self.provider.refs, {})

    def test_failed_batch_and_same_batch_replacement_keep_refs_across_passes(self) -> None:
        record, _ = _records((_entry(1, 1), _entry(2, 2)), repository="acme/app")
        record = record.model_copy(
            update={
                "candidate": record.candidate.model_copy(
                    update={"status": "failed", "check_rerun_attempts": {101: 1}}
                )
            }
        )
        replacement = record.model_copy(update={"record_id": record.record_id + "-retry"})
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_batch_candidate_record(record)
            for _ in range(2):
                _close_service_batch_pull_request(
                    github_client=self.client,
                    candidate_record=record,
                    lease=Mock(),
                    reason="checks failed",
                )
                self.assertEqual(set(self.provider.refs), set(self.refs))
            store.write_merge_train_batch_candidate_record(replacement)
            for _ in range(2):
                _supersede_active_merge_train_batch_candidate_records(
                    github_client=self.client,
                    record_store=store,
                    repository=record.candidate.repository,
                    base_branch=record.candidate.base_branch,
                    batch_id=record.candidate.batch_id,
                    replacement_record_id=replacement.record_id,
                    lease=Mock(),
                )
                self.assertEqual(set(self.provider.refs), set(self.refs))
