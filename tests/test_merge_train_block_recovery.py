"""Regression for author-fixed candidate blocks, including unchanged-head refusal."""

from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.parse import quote
from typing import Any
from unittest.mock import Mock

from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate_record
from control_plane.contracts.merge_train_pr_feedback_record import MergeTrainPrFeedbackRecord
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerRunOnceEnvelope,
    _advance_from_live_snapshot,
)
from control_plane.merge_train_github import GitHubMergeTrainClient
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.merge_train_policy_fixtures import build_test_merge_train_policy
from tests.support.merge_train import _FakeExpandedMergeTrainSnapshotReader
from control_plane.contracts.merge_train_batch import build_merge_train_batch_candidate


class CandidateBlockRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = FilesystemRecordStore(Path(self.directory.name))
        self.policy = build_test_merge_train_policy(trusted_automation_github_user_ids=(42,))
        self.target = self.policy.policies[0]
        template = _FakeExpandedMergeTrainSnapshotReader(
            transport=object()
        ).read_merge_train_snapshot(
            repository=self.target.repository, base_branch=self.target.base_branch
        )
        self.old = template.pull_requests[1].model_copy(update={"head_sha": "a" * 40})
        initial = template.model_copy(update={"pull_requests": (self.old,)})
        candidate = build_merge_train_batch_candidate(
            dry_run_result=build_merge_train_dry_run_result(policy=self.policy, snapshot=initial),
            base_sha=initial.base_sha,
            policy_sha256="digest",
            created_at="2026-10-18T00:00:00Z",
        ).model_copy(update={"status": "failed", "required_checks_status": "fail"})
        self.record = build_merge_train_batch_candidate_record(
            candidate=candidate,
            source="service:controller:batch-candidate-failed:test",
            updated_at="2026-10-18T00:01:00Z",
        ).model_copy(update={"status": "superseded"})
        self.store.write_merge_train_batch_candidate_record(self.record)
        self.feedback = MergeTrainPrFeedbackRecord(
            feedback_id="blocked-feedback",
            repository=self.target.repository,
            base_branch=self.target.base_branch,
            pull_request_number=self.old.number,
            pull_request_url="https://github.com/example/repo/pull/2",
            event="blocked",
            marker="marker",
            comment_markdown="Recorded block",
            source="launchplane:merge-train-scheduler",
            recorded_at="2026-10-18T00:02:00Z",
            policy_key=self.target.policy_key,
            policy_sha256="digest",
            controller_action="block",
            controller_record_id=self.record.record_id,
            delivery_status="delivered",
            comment_id=100,
        )
        self.store.write_merge_train_pr_feedback_record(self.feedback)
        self.current = self.old.model_copy(
            update={"head_sha": "b" * 40, "labels": (self.target.blocked_label,)}
        )
        self.snapshot = template.model_copy(update={"pull_requests": (self.current,)})
        self.comments: list[dict[str, Any]] = [
            {
                "id": 101,
                "body": f"Review result: weighed\nHead: {self.current.head_sha}\n\nFindings addressed.",
                "user": {"id": 42, "login": "train[bot]"},
                "created_at": "2026-10-18T00:03:00Z",
                "updated_at": "2026-10-18T00:03:00Z",
            }
        ]
        self.label_event: dict[str, Any] = {
            "id": 50,
            "event": "labeled",
            "label": {"name": self.target.blocked_label},
            "actor": {"id": 42, "login": "train[bot]"},
            "created_at": "2026-10-18T00:01:30Z",
        }
        self.block_comment = {
            "id": 100,
            "body": self.feedback.comment_markdown,
            "user": {"id": 42, "login": "train[bot]"},
            "created_at": "2026-10-18T00:02:00Z",
            "updated_at": "2026-10-18T00:02:00Z",
        }
        self.events: list[dict[str, Any]] | None = None
        self.mutations: list[str] = []
        self.transport = Mock()
        self.transport.request.side_effect = self.request
        self.client = GitHubMergeTrainClient(transport=self.transport)
        self.client.read_pull_request_snapshot = Mock(side_effect=lambda **_: self.current)  # type: ignore[method-assign]
        self.lease = Mock()
        self.lease.record.ordinary_job_binding = None

    def request(self, *, method: str, path: str, body: object = None) -> object:
        del body
        if method == "DELETE":
            self.mutations.append(path)
            self.current = self.current.model_copy(update={"labels": ()})
            return None
        if "/events?" in path:
            return self.events if self.events is not None else [self.label_event]
        if path.endswith("/issues/comments/100"):
            return self.block_comment
        if "/comments?" in path:
            return self.comments
        if "/pulls/" in path:
            return {"labels": [{"name": label} for label in self.current.labels]}
        raise AssertionError((method, path))

    def advance(self, *, mutate: bool = False) -> dict[str, Any]:
        return _advance_from_live_snapshot(
            request=MergeTrainControllerRunOnceEnvelope(
                repository=self.target.repository, mutate=mutate
            ),
            policy=self.policy,
            policy_sha256="digest",
            transport=self.transport,
            github_client=self.client,
            candidate_store=self.store,
            stack_collapse_store=self.store,
            trace_id="recovery",
            recorded_at="2026-10-18T00:04:00Z",
            lease=self.lease,
            snapshot=self.snapshot,
        )

    def test_dry_run_finds_reviewed_green_new_head_without_requiring_ready(self) -> None:
        result = self.advance()
        self.assertEqual(result["controller_action"], "recover_candidate_block")
        self.assertEqual(self.mutations, [])
        self.assertEqual(result["details"]["blocked_head_sha"], self.old.head_sha)
        self.assertEqual(result["details"]["head_sha"], self.current.head_sha)

    def test_mutation_clears_only_proven_old_block_then_requires_normal_ready(self) -> None:
        result = self.advance(mutate=True)
        self.assertEqual(result["controller_action"], "recover_candidate_block")
        self.assertEqual(len(self.mutations), 1)
        self.assertTrue(
            self.mutations[0].endswith(f"/labels/{quote(self.target.blocked_label, safe='')}")
        )
        self.assertNotIn(self.target.enqueue_label, self.current.labels)
        self.assertEqual(self.store.list_merge_train_batch_candidate_records(status="active"), ())
        self.lease.checkpoint.assert_called()

    def test_same_head_and_unready_heads_keep_block(self) -> None:
        for changes in (
            {"head_sha": self.old.head_sha},
            {"required_checks_status": "pending"},
            {"required_checks_status": "fail"},
            {"required_checks_status": "unknown"},
            {"mergeable": "conflicting"},
            {"is_draft": True},
        ):
            with self.subTest(changes=changes):
                current = self.current
                self.current = current.model_copy(update=changes)
                self.snapshot = self.snapshot.model_copy(update={"pull_requests": (self.current,)})
                self.assertNotEqual(
                    self.advance(mutate=True)["controller_action"], "recover_candidate_block"
                )
                self.assertEqual(self.mutations, [])
                self.current = current

    def test_missing_stale_unweighed_or_renewed_review_keeps_block(self) -> None:
        receipt = self.comments[0]
        for comments in (
            [],
            [{**receipt, "body": f"Review result: weighed\nHead: {self.old.head_sha}\n\nDone."}],
            [{**receipt, "body": "Review of record: Anthropic, no findings."}],
            [
                receipt,
                {
                    **receipt,
                    "id": 102,
                    "body": "Supervisor runs the review",
                    "created_at": "2026-10-18T00:03:30Z",
                },
            ],
            [{**receipt, "updated_at": "2026-10-18T00:03:30Z"}],
            [{**receipt, "user": {"id": 999, "login": "outsider"}}],
        ):
            with self.subTest(comments=comments):
                self.comments = comments
                self.assertNotEqual(
                    self.advance(mutate=True)["controller_action"], "recover_candidate_block"
                )
                self.assertEqual(self.mutations, [])

    def test_manual_or_reapplied_label_is_preserved(self) -> None:
        original = dict(self.label_event)
        for event in (
            {**original, "actor": {"id": 999, "login": "owner"}},
            {**original, "created_at": "2026-10-18T00:03:30Z"},
            {
                **original,
                "performed_via_github_app": {"id": 1},
                "actor": {"id": 42, "type": "User"},
            },
        ):
            with self.subTest(event=event):
                self.label_event = event
                self.assertNotEqual(
                    self.advance(mutate=True)["controller_action"], "recover_candidate_block"
                )
                self.assertEqual(self.mutations, [])

    def test_absent_durable_block_receipt_does_not_clear_generic_label(self) -> None:
        self.store.write_merge_train_pr_feedback_record(
            self.feedback.model_copy(update={"event": "waiting"})
        )
        self.assertNotEqual(
            self.advance(mutate=True)["controller_action"], "recover_candidate_block"
        )
        self.assertEqual(self.mutations, [])

    def test_head_or_label_change_during_final_recheck_refuses_before_delete(self) -> None:
        for change in ("head", "label"):
            with self.subTest(change=change):
                current = self.current
                event = dict(self.label_event)

                def checkpoint(**_: object) -> None:
                    if change == "head":
                        self.current = current.model_copy(update={"head_sha": "c" * 40})
                    else:
                        self.label_event = {**event, "id": 51}

                self.lease.checkpoint.side_effect = checkpoint
                self.assertNotEqual(
                    self.advance(mutate=True)["controller_action"], "recover_candidate_block"
                )
                self.assertEqual(self.mutations, [])
                self.current, self.label_event = current, event

    def test_same_timestamp_remove_and_reapply_is_a_new_hold(self) -> None:
        self.events = [
            self.label_event,
            {**self.label_event, "id": 51, "event": "unlabeled"},
            {**self.label_event, "id": 52},
        ]
        self.advance(mutate=True)
        self.assertEqual(self.mutations, [])

    def test_provider_denial_is_not_retried(self) -> None:
        from control_plane.merge_train_github import MergeTrainGitHubError

        requests = self.request
        attempts = 0

        def denied(**kwargs: Any) -> object:
            nonlocal attempts
            if kwargs["method"] == "DELETE":
                attempts += 1
                raise MergeTrainGitHubError("denied", status_code=403)
            return requests(**kwargs)

        self.transport.request.side_effect = denied
        with self.assertRaises(MergeTrainGitHubError):
            self.advance(mutate=True)
        self.assertEqual(attempts, 1)

    def test_lost_delete_reply_is_observed_without_replaying_removal(self) -> None:
        requests = self.request

        def lost_reply(**kwargs: Any) -> object:
            result = requests(**kwargs)
            if kwargs["method"] == "DELETE":
                raise TimeoutError("reply lost")
            return result

        self.transport.request.side_effect = lost_reply
        with self.assertRaises(TimeoutError):
            self.advance(mutate=True)
        self.snapshot = self.snapshot.model_copy(update={"pull_requests": (self.current,)})
        self.transport.request.side_effect = requests
        self.assertNotEqual(
            self.advance(mutate=True)["controller_action"], "recover_candidate_block"
        )
        self.assertEqual(len(self.mutations), 1)

    def test_mismatched_or_changed_feedback_cannot_clear_a_hold(self) -> None:
        for update in (
            {"policy_sha256": "other"},
            {"controller_record_id": "unrelated"},
            {"source": "manual"},
            {"delivery_status": "failed"},
            {"comment_markdown": "different body"},
        ):
            with self.subTest(update=update):
                self.store.write_merge_train_pr_feedback_record(
                    self.feedback.model_copy(update=update)
                )
                self.assertNotEqual(
                    self.advance(mutate=True)["controller_action"], "recover_candidate_block"
                )
                self.assertEqual(self.mutations, [])

    def test_success_feedback_replaces_the_stale_block_instructions(self) -> None:
        from control_plane.merge_train_controller_feedback import build_feedback_payloads

        result = self.advance(mutate=True)
        payloads = build_feedback_payloads(response={"result": result, "records": {}})
        self.assertEqual(len(payloads), 1)
        self.assertEqual(payloads[0]["pull_request_number"], self.current.number)
        self.assertEqual(payloads[0]["event"], "waiting")
        self.assertEqual(
            build_feedback_payloads(response={"result": self.advance(), "records": {}}), []
        )


if __name__ == "__main__":
    unittest.main()
