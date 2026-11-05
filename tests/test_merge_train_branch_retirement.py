import unittest
from copy import deepcopy
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from unittest.mock import Mock
from tempfile import TemporaryDirectory
from pathlib import Path

from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.contracts.merge_train_batch import (
    build_merge_train_batch_landing_plan,
    build_merge_train_batch_candidate_ref,
    build_merge_train_batch_candidate_record,
)
from control_plane.merge_train_batch_landing import _cleanup_merge_train_batch_candidate_ref

from control_plane.merge_train_controller_run_once import (
    MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
    MergeTrainControllerLeaseContext,
    _close_service_batch_pull_request,
    _supersede_active_merge_train_batch_candidate_records,
    _resume_merge_train_controller_state,
    _finish_pending_retired_candidate_refs,
    _cleanup_service_batch_refs,
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

    def _lease(self, store: FilesystemRecordStore) -> MergeTrainControllerLeaseContext:
        state = store.acquire_merge_train_controller_state_record(
            repository=self.candidate.repository,
            base_branch=self.candidate.base_branch,
            policy_key=self.candidate.policy_key,
            policy_sha256=self.candidate.policy_sha256,
            lease_owner="retirement-test",
            lease_seconds=300,
            initial_active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
            initial_active_phase="persist_replacement_candidate",
            adoptable_active_actions=(MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,),
        )
        return MergeTrainControllerLeaseContext(record=state, record_store=store)

    def test_supersession_leaves_cleanup_until_the_caller_has_finished(self) -> None:
        record, _ = _records((_entry(1, 1), _entry(2, 2)), repository="acme/app")
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_batch_candidate_record(record)
            lease = self._lease(store)
            self.provider.fail_delete = True
            _supersede_active_merge_train_batch_candidate_records(
                lease=lease,
                record_store=store,
                repository=record.candidate.repository,
                base_branch=record.candidate.base_branch,
                batch_id=record.candidate.batch_id,
                replacement_record_id=None,
            )
            self.assertEqual(store.list_merge_train_batch_candidate_records(status="active"), ())
            self.assertEqual(self.provider.deleted, [])
            saved = store.list_merge_train_controller_state_records()[0]
            pending = saved.step_payload["retired_candidate_record_ids"]
            assert isinstance(pending, list)
            self.assertIn(record.record_id, pending)
            self.assertEqual(saved.active_phase, "persist_replacement_candidate")

    def test_cleanup_intent_is_persisted_before_terminal_record_write(self) -> None:
        record, _ = _records((_entry(1, 1), _entry(2, 2)), repository="acme/app")

        class InterruptedStore(FilesystemRecordStore):
            def write_merge_train_batch_candidate_record(self, item: Any) -> Any:
                if item.status == "superseded":
                    raise RuntimeError("interrupted terminal write")
                return super().write_merge_train_batch_candidate_record(item)

        with TemporaryDirectory() as directory:
            store = InterruptedStore(Path(directory))
            store.write_merge_train_batch_candidate_record(record)
            lease = self._lease(store)
            with self.assertRaisesRegex(RuntimeError, "interrupted terminal write"):
                _supersede_active_merge_train_batch_candidate_records(
                    lease=lease,
                    record_store=store,
                    repository=record.candidate.repository,
                    base_branch=record.candidate.base_branch,
                    batch_id=record.candidate.batch_id,
                    replacement_record_id=None,
                )
            saved = store.list_merge_train_controller_state_records()[0]
            pending = saved.step_payload["retired_candidate_record_ids"]
            assert isinstance(pending, list)
            self.assertIn(record.record_id, pending)
            self.assertEqual(self.provider.deleted, [])

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

    def test_open_pr_heads_and_bases_keep_their_refs(self) -> None:
        for field in ("head", "base"):
            with self.subTest(field=field):
                self.provider.refs = {ref: "a" * 40 for ref in self.refs}
                branch = self.refs[0].removeprefix("refs/heads/")
                self.provider.pulls = []
                self.provider.pulls = [
                    {
                        "state": "open",
                        "head": {"ref": branch if field == "head" else "other"},
                        "base": {"ref": branch if field == "base" else "other"},
                    }
                ]
                self.client.cleanup_batch_candidate_ref(
                    landing_plan=self.candidate,
                )
                self.assertIn(self.refs[0], self.provider.refs)

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
            candidate_store=Mock(list_merge_train_batch_candidate_records=Mock(return_value=())),
            github_client=self.client,
            landing_plan=plan,
            request_trace_id="retained-fixture",
        )
        self.assertEqual(result["candidate_ref_cleanup_status"], "retained")
        self.assertIn(plan.candidate_ref, self.provider.refs)

    def test_active_batch_on_another_base_protects_its_shared_ref(self) -> None:
        target = self.candidate.model_copy(
            update={
                "base_branch": "release/a",
                "structural_provenance": None,
                "status": "planned",
                "candidate_sha": "",
                "candidate_tree_sha": "",
                "candidate_sha256": "",
                "candidate_ref": build_merge_train_batch_candidate_ref(
                    repository=self.candidate.repository,
                    base_branch="release/a",
                    batch_id=self.candidate.batch_id,
                ),
            }
        )
        active = target.model_copy(update={"base_branch": "release-a"})
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_batch_candidate_record(
                build_merge_train_batch_candidate_record(
                    candidate=active,
                    source="test:running-other-base",
                    updated_at=active.updated_at,
                )
            )
            self.assertEqual(
                _cleanup_service_batch_refs(
                    github_client=self.client,
                    candidate_store=store,
                    candidate=target,
                    lease=self._lease(store),
                ),
                "retained",
            )
            self.assertEqual(self.provider.deleted, [])

    def test_construction_ref_kept_by_open_pr_is_reported_when_canonical_is_missing(self) -> None:
        del self.provider.refs[self.refs[0]]
        self.provider.pulls = [
            {
                "state": "open",
                "head": {"ref": self.refs[1].removeprefix("refs/heads/")},
                "base": {"ref": "other"},
            }
        ]
        plan = build_merge_train_batch_landing_plan(
            candidate=self.candidate,
            merge_method="merge",
            created_at=self.candidate.updated_at,
        )
        result = _cleanup_merge_train_batch_candidate_ref(
            candidate_store=Mock(list_merge_train_batch_candidate_records=Mock(return_value=())),
            github_client=self.client,
            landing_plan=plan,
            request_trace_id="retained-construction",
        )
        self.assertEqual(result["candidate_ref_cleanup_status"], "retained")
        self.assertEqual(self.provider.deleted, [])

    def test_non_owned_ref_is_refused_before_provider_calls(self) -> None:
        for reference in ("refs/heads/main", "refs/heads/work/source"):
            with self.subTest(reference=reference):
                candidate = self.candidate.model_copy(update={"candidate_ref": reference})
                with self.assertRaises(MergeTrainGitHubError):
                    self.client.cleanup_batch_candidate_ref(landing_plan=candidate)
                self.assertEqual(self.provider.deleted, [])

    def test_terminal_cleanup_resumes_from_the_persisted_lease_after_provider_failure(self) -> None:
        record, _ = _records((_entry(1, 1), _entry(2, 2)), repository="acme/app")
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            store.write_merge_train_batch_candidate_record(record)
            lease = self._lease(store)
            _supersede_active_merge_train_batch_candidate_records(
                record_store=store,
                repository=record.candidate.repository,
                base_branch=record.candidate.base_branch,
                batch_id=record.candidate.batch_id,
                replacement_record_id=None,
                lease=lease,
            )
            lease.checkpoint(step_payload={"caller_completed": True})
            self.provider.fail_delete = True
            result = _finish_pending_retired_candidate_refs(
                github_client=self.client,
                candidate_store=store,
                lease=lease,
            )
            self.assertEqual(result["candidate_ref_cleanup_status"], "failed")
            lease.release(reconciliation_status="required", clear_active_state=False)
            state = store.acquire_merge_train_controller_state_record(
                repository=self.candidate.repository,
                base_branch=self.candidate.base_branch,
                policy_key=self.candidate.policy_key,
                policy_sha256=self.candidate.policy_sha256,
                lease_owner="resumed-retirement-test",
                lease_seconds=300,
                initial_active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
                initial_active_phase="select_next_action",
                adoptable_active_actions=(MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,),
            )
            resumed = MergeTrainControllerLeaseContext(record=state, record_store=store)
            self.assertTrue(state.step_payload["caller_completed"])
            self.provider.fail_delete = False
            resumed_result = _resume_merge_train_controller_state(
                request=MergeTrainControllerRunOnceEnvelope(
                    repository=record.candidate.repository, mutate=True
                ),
                policy_sha256=record.candidate.policy_sha256,
                repository_policy=Mock(),
                trace_id="resumed-retirement",
                recorded_at=state.updated_at,
                github_client=self.client,
                candidate_store=store,
                landing_store=store,
                stack_collapse_store=store,
                admission_store=store,
                lease=resumed,
            )
            assert resumed_result is not None
            self.assertEqual(resumed_result["candidate_ref_cleanup_status"], "deleted")
            self.assertEqual(self.provider.refs, {})
            saved = store.list_merge_train_controller_state_records()[0]
            self.assertEqual(saved.step_payload["retired_candidate_record_ids"], [])

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
            lease = self._lease(store)
            for _ in range(2):
                _supersede_active_merge_train_batch_candidate_records(
                    record_store=store,
                    repository=record.candidate.repository,
                    base_branch=record.candidate.base_branch,
                    batch_id=record.candidate.batch_id,
                    replacement_record_id=replacement.record_id,
                    lease=lease,
                )
                self.assertEqual(set(self.provider.refs), set(self.refs))
