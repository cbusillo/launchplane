import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import Mock

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    build_merge_train_batch_candidate,
    build_merge_train_batch_candidate_record,
)
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train_batch_pull_request import (
    batch_pull_request_body,
    changed_closed_batch_body,
)
from control_plane.merge_train_controller_run_once import try_reflow_failed_merge_train_candidate
from control_plane.merge_train_github import GitHubMergeTrainClient
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.support.merge_train import _FakeExpandedMergeTrainSnapshotReader
from tests.test_merge_train_batch_pull_request import _BatchProvider


class FailedBatchRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.snapshot = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(repository="cbusillo/sellyouroutboard", base_branch="main")
        self.policy = build_test_merge_train_policy()
        self.candidate = build_merge_train_batch_candidate(
            dry_run_result=build_merge_train_dry_run_result(
                policy=self.policy, snapshot=self.snapshot
            ),
            base_sha=self.snapshot.base_sha,
            policy_sha256="policy-digest",
            created_at="2026-10-03T12:00:00Z",
        ).model_copy(
            update={
                "candidate_sha": "failed-sha",
                "status": "failed",
                "required_checks_status": "fail",
            }
        )
        self.provider = _BatchProvider(self.candidate)
        self.client = GitHubMergeTrainClient(transport=self.provider)
        self.provider.created = True
        self.provider.closed = True
        self.provider.body = batch_pull_request_body(client=self.client, candidate=self.candidate)

    def _reflow(
        self,
        store: FilesystemRecordStore,
        candidate: MergeTrainBatchCandidate,
        *,
        mutate: bool = True,
    ) -> dict[str, Any]:
        record = build_merge_train_batch_candidate_record(
            candidate=candidate, source="test", updated_at="2026-10-03T12:00:00Z"
        )
        store.write_merge_train_batch_candidate_record(record)
        client = Mock(wraps=self.client)
        client.read_merge_train_snapshot.return_value = self.snapshot
        client.probe_batch_entry_conflicts.return_value = ()
        result = try_reflow_failed_merge_train_candidate(
            candidate_store=store,
            active_candidate_record=record,
            policy=self.policy,
            policy_sha256="policy-digest",
            transport=self.provider,
            github_client=cast(GitHubMergeTrainClient, client),
            repository=self.snapshot.repository,
            base_branch=self.snapshot.base_branch,
            merge_method="merge",
            recorded_at="2026-10-03T12:01:00Z",
            trace_id="recovery",
            mutate=mutate,
            lease=Mock(),
        )
        assert result is not None
        return result

    def test_changed_closed_body_rebuilds_once_and_creates_a_new_batch_pr(self) -> None:
        self.provider.body = self.provider.body.split("## Owner test notes")[0]
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            dry = self._reflow(store, self.candidate, mutate=False)
            self.assertEqual(dry["controller_action"], "plan_candidate")
            self.assertFalse(
                any(
                    record.candidate.batch_body_retry_of
                    for record in store.list_merge_train_batch_candidate_records()
                )
            )
            result = self._reflow(store, self.candidate)
            replacement = MergeTrainBatchCandidate.model_validate(result["candidate"])
            self.assertEqual(result["controller_action"], "plan_candidate")
            self.assertNotEqual(replacement.candidate_ref, self.candidate.candidate_ref)
            self.assertTrue(replacement.batch_body_retry_of)
            self.assertEqual(replacement.entries, self.candidate.entries)
            self.assertEqual(replacement.base_sha, self.candidate.base_sha)
            # The retry ref has no PR yet; it goes through ordinary batch creation.
            built = replacement.model_copy(
                update={"candidate_sha": "rebuilt-sha", "status": "ready_for_checks"}
            )
            provider = _BatchProvider(built)
            provider.number = self.provider.number + 1
            new_client = GitHubMergeTrainClient(transport=provider)
            new_number = new_client.ensure_batch_pull_request(candidate=built)
            self.assertNotEqual(new_number, self.provider.number)
            self.assertIn("## Owner test notes", provider.body)
            posted_body = next(body for method, _, body in provider.requests if method == "POST")
            assert posted_body is not None
            self.assertEqual(posted_body["head"], built.candidate_ref.removeprefix("refs/heads/"))
            # A restarted process reads the persisted budget, even if generation changes again.
            restarted = FilesystemRecordStore(state_dir=Path(directory))
            failed_retry = built.model_copy(
                update={"status": "failed", "required_checks_status": "fail"}
            )
            stopped = self._reflow(restarted, failed_retry)
            self.assertEqual(stopped["controller_action"], "candidate_failed")
            self.assertEqual(stopped["recovery_reason"], "batch_body_retry_already_used")
            # Older failed evidence cannot reset the budget either.
            self.assertEqual(
                self._reflow(restarted, self.candidate)["recovery_reason"],
                "batch_body_retry_already_used",
            )

    def test_unchanged_body_keeps_the_failure_stopped(self) -> None:
        with TemporaryDirectory() as directory:
            result = self._reflow(FilesystemRecordStore(state_dir=Path(directory)), self.candidate)
        self.assertEqual(result["controller_action"], "candidate_failed")
        self.assertEqual(result["recovery_reason"], "closed_batch_body_unchanged_or_unavailable")

    def test_only_bound_closed_unmerged_batch_is_recovery_evidence(self) -> None:
        for state in ("open", "merged", "missing", "wrong_binding"):
            with self.subTest(state=state):
                self.provider.closed = state != "open"
                self.provider.merged = state == "merged"
                self.provider.created = state != "missing"
                self.provider.body = (
                    "unbound"
                    if state == "wrong_binding"
                    else batch_pull_request_body(
                        client=self.client, candidate=self.candidate
                    ).split("## Owner test notes")[0]
                )
                self.assertFalse(
                    changed_closed_batch_body(client=self.client, candidate=self.candidate)
                )

    def test_failed_batch_reports_current_queue_head_wait_on_both_passes(self) -> None:
        self.snapshot = self.snapshot.model_copy(
            update={
                "pull_requests": tuple(
                    pr.model_copy(update={"required_checks_status": "pending"})
                    if index == 0
                    else pr
                    for index, pr in enumerate(self.snapshot.pull_requests)
                )
            }
        )
        for mutate in (False, True):
            with self.subTest(mutate=mutate), TemporaryDirectory() as directory:
                store = FilesystemRecordStore(state_dir=Path(directory))
                result = self._reflow(store, self.candidate, mutate=mutate)
                self.assertEqual(result["controller_action"], "wait_for_checks")
                self.assertEqual(
                    result["dry_run_result"]["selected_pr"]["number"],
                    self.snapshot.pull_requests[0].number,
                )
                self.assertEqual(
                    result["dry_run_result"]["intended_next_action"], "wait_for_checks"
                )
                self.assertFalse(
                    any(
                        record.candidate.batch_body_retry_of
                        for record in store.list_merge_train_batch_candidate_records()
                    )
                )
