import unittest
from copy import deepcopy
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit
from unittest.mock import Mock

from control_plane.merge_train_controller_run_once import _close_service_batch_pull_request

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
                self.client.cleanup_retired_batch_refs(candidate=candidate)
                self.client.cleanup_retired_batch_refs(candidate=candidate)
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
                self.client.cleanup_retired_batch_refs(
                    candidate=self.candidate,
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
                    self.client.cleanup_retired_batch_refs(candidate=self.candidate)
                self.assertEqual(set(self.provider.refs), set(self.refs))
                setattr(self.provider, failure, False)

    def test_changed_tip_cannot_be_deleted_after_qualification(self) -> None:
        self.provider.move_before_delete = True
        with self.assertRaises(MergeTrainGitHubError):
            self.client.cleanup_retired_batch_refs(candidate=self.candidate)
        self.assertEqual(self.provider.deleted, [])
        self.assertEqual(set(self.provider.refs), set(self.refs))

    def test_non_owned_ref_is_refused_before_provider_calls(self) -> None:
        for reference in ("refs/heads/main", "refs/heads/work/source"):
            with self.subTest(reference=reference):
                candidate = self.candidate.model_copy(update={"candidate_ref": reference})
                with self.assertRaises(MergeTrainGitHubError):
                    self.client.cleanup_retired_batch_refs(candidate=candidate)
                self.assertEqual(self.provider.deleted, [])

    def test_controller_retires_failed_construction_without_a_candidate_sha(self) -> None:
        record, _ = _records((_entry(1, 1), _entry(2, 2)), repository="acme/app")
        record = record.model_copy(
            update={
                "candidate": record.candidate.model_copy(
                    update={"candidate_sha": "", "status": "failed"}
                )
            }
        )
        store = Mock()
        store.list_merge_train_batch_candidate_records.return_value = (record,)
        _close_service_batch_pull_request(
            github_client=self.client,
            candidate_store=store,
            candidate_record=record,
            lease=Mock(),
            reason="construction failed",
        )
        self.assertEqual(self.provider.refs, {})

    def test_controller_preserves_refs_used_by_another_active_batch(self) -> None:
        record, _ = _records((_entry(1, 1), _entry(2, 2)), repository="acme/app")
        record = record.model_copy(
            update={
                "candidate": record.candidate.model_copy(
                    update={"candidate_sha": "", "status": "failed"}
                )
            }
        )
        other = record.model_copy(
            update={"candidate": record.candidate.model_copy(update={"batch_id": "other-batch"})}
        )
        store = Mock()
        store.list_merge_train_batch_candidate_records.return_value = (record, other)
        _close_service_batch_pull_request(
            github_client=self.client,
            candidate_store=store,
            candidate_record=record,
            lease=Mock(),
            reason="construction failed",
        )
        self.assertIn(self.refs[0], self.provider.refs)
