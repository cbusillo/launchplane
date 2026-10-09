from __future__ import annotations

from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch
from urllib.error import HTTPError, URLError

import click

from control_plane.contracts.merge_train_pr_feedback_record import (
    MergeTrainPrFeedbackEvent,
    MergeTrainPrFeedbackRecord,
)
from control_plane import merge_train_scheduler
from control_plane.merge_train_scheduler import MergeTrainScheduledTargetResult
from control_plane.merge_train_controller_run_once import MergeTrainControllerRunOnceResult
from control_plane.merge_train_pr_feedback import (
    MergeTrainPrFeedbackEnvelope,
    build_merge_train_pr_feedback_record,
    feedback_retry_is_due,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.launchplane import upsert_github_issue_comment
from control_plane.contracts.merge_train_policy import MergeTrainSchedulerPolicy
from tests.test_merge_train_scheduler import _policy_record


_TIME = "2026-10-01T06:00:00Z"


def _failure(cause: Exception) -> click.ClickException:
    error = click.ClickException("comment transport failed")
    error.__cause__ = cause
    return error


def _record(
    *, number: int = 7, event: MergeTrainPrFeedbackEvent = "completed", recorded_at: str = _TIME
) -> MergeTrainPrFeedbackRecord:
    return build_merge_train_pr_feedback_record(
        request=MergeTrainPrFeedbackEnvelope(
            repository="cbusillo/alpha",
            pull_request_number=number,
            event=event,
            controller_action="batch_landed",
            message="Saved terminal landing evidence.",
        ),
        policy_key="test",
        policy_sha256="digest",
        token="test-token",
        recorded_at=recorded_at,
        response_trace_id=f"record-{number}-{event}",
    )


class MergeTrainFeedbackRetryTests(TestCase):
    def test_scheduled_terminal_failure_recovers_after_idle_without_duplicate_delivery(
        self,
    ) -> None:
        policy = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True, mutate=True))
        )
        terminal = MergeTrainControllerRunOnceResult(
            accepted_result={
                "repository": "cbusillo/alpha",
                "base_branch": "main",
                "controller_action": "batch_landed",
                "landing_plan": {"entries": [{"pull_request_number": 7, "status": "merged"}]},
            },
            records={"merge_train_batch_landing_plan_record_id": "landed-7"},
        )
        idle = MergeTrainControllerRunOnceResult(
            accepted_result={
                "repository": "cbusillo/alpha",
                "base_branch": "main",
                "controller_action": "idle",
            },
            records={},
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            with (
                patch.object(
                    merge_train_scheduler,
                    "execute_merge_train_controller_run_once",
                    side_effect=[terminal, idle, idle, idle],
                ) as controller,
                patch(
                    "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
                    side_effect=[
                        _failure(URLError("temporary outage")),
                        {
                            "action": "updated_comment",
                            "comment_id": 123,
                            "comment_url": "https://example.test/comment",
                        },
                    ],
                ) as comment,
            ):

                def run(now: str) -> MergeTrainScheduledTargetResult:
                    return merge_train_scheduler._run_controller(
                        record_store=store,
                        control_plane_root=Path(directory),
                        policy_record=policy,
                        repository_policy=policy.policy.policies[0],
                        token="test-token",
                        trace_id=now,
                        now=lambda: now,
                    )

                self.assertEqual(run(_TIME).feedback_failed, 1)
                original = store.list_merge_train_pr_feedback_records()[0]
                self.assertEqual(original.event, "completed")
                self.assertEqual(run("2026-10-01T06:00:30Z").feedback_delivered, 0)
                self.assertEqual(comment.call_count, 1)
                self.assertEqual(run("2026-10-01T06:01:01Z").feedback_delivered, 1)
                self.assertEqual(run("2026-10-01T06:02:01Z").feedback_delivered, 0)
                self.assertEqual(comment.call_count, 2)
                self.assertEqual(controller.call_count, 4)  # only the normal scheduled pass
                recovered = store.list_merge_train_pr_feedback_records()[0]
                self.assertEqual(recovered.feedback_id, original.feedback_id)
                self.assertEqual(recovered.recorded_at, original.recorded_at)
                self.assertEqual(recovered.comment_markdown, original.comment_markdown)
                self.assertEqual(recovered.delivery_status, "delivered")
                self.assertEqual(recovered.delivery_attempts, 2)
                self.assertEqual(store.list_merge_admission_records(), ())

    def test_retry_honors_quota_and_does_not_retry_permission_refusal(self) -> None:
        for code, headers, due, retryable in (
            (429, {"Retry-After": "120"}, "2026-10-01T06:02:00Z", True),
            (
                403,
                {
                    "Retry-After": "120",
                    "X-RateLimit-Remaining": "42",
                    "X-RateLimit-Reset": "1790820000",
                },
                "2026-10-01T06:02:00Z",
                True,
            ),
            (
                403,
                {
                    "X-RateLimit-Remaining": "0",
                    "X-RateLimit-Reset": str(
                        int(datetime(2026, 10, 1, 6, 5, tzinfo=timezone.utc).timestamp())
                    ),
                },
                "2026-10-01T06:05:00Z",
                True,
            ),
            (503, {}, "2026-10-01T06:01:00Z", True),
            (403, {}, "2026-10-01T06:01:00Z", False),
        ):
            with self.subTest(code=code, headers=headers):
                message = Message()
                for name, value in headers.items():
                    message[name] = value
                cause = HTTPError("https://example.test/comment", code, "refused", message, None)
                with patch(
                    "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
                    side_effect=_failure(cause),
                ):
                    record = _record()
                self.assertEqual(record.retryable, retryable)
                self.assertFalse(feedback_retry_is_due(record, now="2026-10-01T06:00:59Z"))
                self.assertEqual(feedback_retry_is_due(record, now=due), retryable)

    def test_provider_backoff_defers_other_pr_comments_and_retains_newer_status(self) -> None:
        policy = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True, mutate=True))
        )
        headers = Message()
        headers["Retry-After"] = "120"
        quota_error = HTTPError("https://example.test/comment", 429, "quota", headers, None)
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            with patch(
                "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
                side_effect=_failure(quota_error),
            ):
                quota = _record()
            with patch(
                "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
                side_effect=_failure(URLError("temporary")),
            ):
                other = _record(number=8)
            store.write_merge_train_pr_feedback_record(quota)
            store.write_merge_train_pr_feedback_record(other)
            response: dict[str, object] = {
                "result": {
                    "repository": "cbusillo/alpha",
                    "base_branch": "main",
                    "controller_action": "wait_for_checks",
                    "candidate": {"entries": [{"pull_request_number": 7}]},
                },
                "records": {},
            }
            with patch(
                "control_plane.merge_train_pr_feedback.upsert_github_issue_comment"
            ) as comment:
                counts = merge_train_scheduler._deliver_controller_feedback(
                    record_store=store,
                    policy_record=policy,
                    repository_policy=policy.policy.policies[0],
                    token="test-token",
                    trace_id="new-waiting",
                    response=response,
                    now=lambda: "2026-10-01T06:01:01Z",
                )
            comment.assert_not_called()
            self.assertEqual(counts, (0, 1))
            latest = store.list_merge_train_pr_feedback_records(pr_number=7, limit=1)[0]
            self.assertEqual(latest.event, "waiting")
            self.assertEqual(latest.provider_retry_at, quota.provider_retry_at)
            with patch(
                "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
                return_value={"action": "updated_comment", "comment_id": 123, "comment_url": ""},
            ) as comment:
                counts = merge_train_scheduler._deliver_controller_feedback(
                    record_store=store,
                    policy_record=policy,
                    repository_policy=policy.policy.policies[0],
                    token="test-token",
                    trace_id="idle-after-deadline",
                    response={
                        "result": {
                            "repository": "cbusillo/alpha",
                            "base_branch": "main",
                            "controller_action": "idle",
                        },
                        "records": {},
                    },
                    now=lambda: "2026-10-01T06:02:01Z",
                )
            self.assertEqual(counts, (1, 0))
            self.assertEqual(comment.call_args.kwargs["issue_number"], 8)
            self.assertIn(
                "completed", store.list_merge_train_pr_feedback_records(pr_number=8)[0].event
            )

    def test_legacy_same_second_conflict_is_not_replayed_without_order_evidence(self) -> None:
        policy = _policy_record(
            ("cbusillo/alpha", MergeTrainSchedulerPolicy(enabled=True, mutate=True))
        )
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(Path(directory))
            with patch(
                "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
                side_effect=_failure(URLError("temporary")),
            ):
                old = _record().model_copy(update={"feedback_id": "legacy-z", "created_at_ns": 0})
            with patch(
                "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
                return_value={"action": "updated_comment", "comment_id": 123, "comment_url": ""},
            ):
                newer = _record(event="waiting").model_copy(
                    update={"feedback_id": "legacy-a", "created_at_ns": 0}
                )
            store.write_merge_train_pr_feedback_record(old)
            store.write_merge_train_pr_feedback_record(newer)
            with patch(
                "control_plane.merge_train_pr_feedback.upsert_github_issue_comment"
            ) as comment:
                result = merge_train_scheduler._deliver_controller_feedback(
                    record_store=store,
                    policy_record=policy,
                    repository_policy=policy.policy.policies[0],
                    token="test-token",
                    trace_id="legacy-idle",
                    now=lambda: "2026-10-01T06:02:01Z",
                    response={
                        "result": {
                            "repository": "cbusillo/alpha",
                            "base_branch": "main",
                            "controller_action": "idle",
                        },
                        "records": {},
                    },
                )
            self.assertEqual(result, (0, 0))
            comment.assert_not_called()

    def test_identical_managed_comment_does_not_patch(self) -> None:
        with (
            patch(
                "control_plane.workflows.launchplane.find_github_issue_comment_by_marker",
                return_value={
                    "id": 123,
                    "body": "saved body",
                    "html_url": "https://example.test/comment",
                },
            ),
            patch("control_plane.workflows.launchplane.update_github_issue_comment") as update,
        ):
            result = upsert_github_issue_comment(
                owner="test",
                repo="repo",
                issue_number=7,
                token="test-token",
                marker="marker",
                body="saved body",
                skip_unchanged=True,
            )
        self.assertEqual(result["action"], "unchanged_comment")
        update.assert_not_called()

    def test_bounded_recovery_query_qualifies_latest_status_before_limit(self) -> None:
        with TemporaryDirectory() as directory:
            stores = (
                FilesystemRecordStore(Path(directory) / "files"),
                PostgresRecordStore(
                    database_url=f"sqlite+pysqlite:///{Path(directory) / 'records.sqlite'}"
                ),
            )
            stores[1].ensure_schema()
            self.addCleanup(stores[1].close)
            with patch(
                "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
                side_effect=_failure(URLError("temporary")),
            ):
                failed = _record()
                superseded = _record(number=8)
                waiting = _record(number=9, event="waiting")
            with patch(
                "control_plane.merge_train_pr_feedback.upsert_github_issue_comment",
                return_value={"action": "created_comment", "comment_id": 123, "comment_url": ""},
            ):
                successes = [
                    _record(number=number, recorded_at="2026-10-01T06:03:00Z")
                    for number in range(10, 50)
                ]
                newer = _record(number=8, event="waiting")
            for store in stores:
                with self.subTest(store=type(store).__name__):
                    for record in (failed, superseded, waiting, newer, *successes):
                        store.write_merge_train_pr_feedback_record(record)
                    retries = store.list_merge_train_pr_feedback_records(
                        repository="cbusillo/alpha",
                        base_branch="main",
                        latest_per_pr=True,
                        delivery_status="failed",
                        terminal_retry_candidates=True,
                        limit=1,
                    )
                    self.assertEqual(
                        [record.feedback_id for record in retries], [failed.feedback_id]
                    )
