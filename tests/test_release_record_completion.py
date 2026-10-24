from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.client_release import (
    ClientReleaseRunView,
    ClientReleaseStepView,
)
from control_plane.release_invitation import advance_release_invitations, ReleaseInvitationBackoff
from control_plane.release_review_record import release_decision_marker
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_release_review import decision, profile, seed


class ReleaseRecordCompletionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'state.db'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        seed(self.store)
        self.profile = profile().model_copy(update={"release_on_acceptance": "director_standing"})
        self.store.write_product_profile_record(self.profile)
        self.decision = decision(self.store).model_copy(update={"release_start": "promote"})
        self.store.create_release_review_decision_record_if_absent(self.decision)
        self.issue: dict[str, Any] = {
            "number": 99,
            "state": "open",
            "body": release_decision_marker(self.decision.record_id),
        }
        self.comments: list[dict[str, Any]] = []
        self.calls: list[tuple[str, str]] = []
        self.lose_response = ""
        self.release_run: ClientReleaseRunView | None = ClientReleaseRunView(
            decision_record_id=self.decision.record_id,
            rollback_drill=False,
            state="passed",
            steps=(),
        )
        for target, replacement in (
            ("control_plane.release_invitation.github_api_request", self.github),
            (
                "control_plane.release_invitation.resolve_launchplane_github_token",
                lambda **kwargs: "test-token",
            ),
            ("control_plane.release_invitation.publish_release_invitation", lambda **kwargs: False),
            (
                "control_plane.release_invitation.resolve_delivery_github_app_id",
                lambda **kwargs: 77,
            ),
            (
                "control_plane.client_release.read_client_release_run",
                lambda **kwargs: self.release_run,
            ),
        ):
            self.enterContext(patch(target, replacement))

    def github(
        self, *, path: str, token: str, method: str = "GET", body: dict[str, Any] | None = None
    ) -> object:
        self.calls.append((method, path))
        if method == "GET":
            return self.comments.copy() if "/comments?" in path else self.issue.copy()
        assert body is not None
        if method == "POST":
            result = {"id": len(self.comments) + 1, "performed_via_github_app": {"id": 77}, **body}
            self.comments.append(result)
        else:
            self.issue.update(body)
            result = self.issue.copy()
        if method == self.lose_response:
            self.lose_response = ""
            raise ValueError("Response lost after write")
        return result

    def advance(self, backoff: ReleaseInvitationBackoff | None = None) -> None:
        advance_release_invitations(store=self.store, control_plane_root=self.root, backoff=backoff)

    def test_worker_closes_release_record_without_invitation_or_equal_lanes(self) -> None:
        self.advance()
        self.assertEqual(self.issue["state"], "closed")
        self.assertTrue("\n".join(self.comments[0]["body"].splitlines()[1:]).strip())
        self.assertNotIn("@", self.comments[0]["body"])
        calls = self.calls.copy()
        self.advance()
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(sum(method == "PATCH" for method, _ in self.calls), 1)
        self.assertGreater(len(self.calls), len(calls))

    def test_failed_and_cancelled_runs_close_but_active_or_unknown_runs_stay_open(self) -> None:
        for status in (
            "not_started",
            "pending",
            "running",
            "reconciliation_required",
            "fail",
            "cancelled",
        ):
            with self.subTest(status=status):
                self.issue["state"] = "open"
                self.comments.clear()
                self.release_run = ClientReleaseRunView(
                    decision_record_id=self.decision.record_id,
                    rollback_drill=False,
                    state="stopped"
                    if status in {"reconciliation_required", "fail", "cancelled"}
                    else "running",
                    steps=(
                        ClientReleaseStepView(
                            step="promote-1",
                            kind="promote",
                            status=status,
                            operation_id="operation",
                        ),
                    ),
                )
                self.advance()
                self.assertEqual(
                    self.issue["state"], "closed" if status in {"fail", "cancelled"} else "open"
                )
        self.release_run = None
        self.issue["state"] = "open"
        self.advance()
        self.assertEqual(self.issue["state"], "open")

    def test_failed_promotion_waits_for_its_recovery(self) -> None:
        failed = ClientReleaseStepView(
            step="promote-1", kind="promote", status="fail", operation_id="promotion"
        )
        for status in ("running", "reconciliation_required", "pass"):
            self.release_run = ClientReleaseRunView(
                decision_record_id=self.decision.record_id,
                rollback_drill=False,
                state="stopped",
                steps=(
                    failed,
                    ClientReleaseStepView(
                        step="recovery-1", kind="recovery", status=status, operation_id="recovery"
                    ),
                ),
            )
            self.advance()
            self.assertEqual(self.issue["state"], "closed" if status == "pass" else "open")

    def test_lost_comment_or_close_response_and_concurrent_ticks_do_not_duplicate_outcomes(
        self,
    ) -> None:
        for method in ("POST", "PATCH"):
            with self.subTest(method=method):
                self.comments.clear()
                self.issue["state"] = "open"
                self.lose_response = method
                self.advance()
                with ThreadPoolExecutor(max_workers=2) as workers:
                    tuple(workers.map(lambda _: self.advance(), range(2)))
                self.assertEqual(self.issue["state"], "closed")
                self.assertEqual(len(self.comments), 1)

    def test_wrong_issue_or_run_identity_never_closes(self) -> None:
        for field, value in (("body", "unrelated"), ("number", 100), ("pull_request", {})):
            with self.subTest(field=field):
                original = self.issue.copy()
                self.issue[field] = value
                self.advance()
                self.assertEqual(self.issue["state"], "open")
                self.issue = original
        assert self.release_run is not None
        self.release_run = self.release_run.model_copy(update={"decision_record_id": "other"})
        self.advance()
        self.assertEqual(self.issue["state"], "open")
        self.assertEqual(self.comments, [])

    def test_successful_rejection_rollback_closes_with_rollback_outcome(self) -> None:
        self.advance()
        forward_outcome = self.comments[0]["body"].splitlines()[1:]
        self.comments.clear()
        self.issue["state"] = "open"
        rollback = self.decision.model_copy(
            update={
                "record_id": "rollback",
                "decision": "changes_requested",
                "reason": "Client rejected hotfix",
                "release_start": "rollback",
            }
        )
        self.store.create_release_review_decision_record_if_absent(rollback)
        self.issue["body"] = release_decision_marker(rollback.record_id)
        assert self.release_run is not None
        self.release_run = self.release_run.model_copy(
            update={"decision_record_id": rollback.record_id}
        )
        self.advance()
        self.assertEqual(self.issue["state"], "closed")
        self.assertNotEqual(forward_outcome, self.comments[0]["body"].splitlines()[1:])

    def test_newer_decision_does_not_hide_completed_release(self) -> None:
        newer = self.decision.model_copy(
            update={
                "record_id": "newer",
                "decided_at": "2026-09-24T01:00:00Z",
                "release_start": "",
                "release_issue_url": "",
            }
        )
        self.store.create_release_review_decision_record_if_absent(newer)
        self.advance()
        self.assertEqual(self.issue["state"], "closed")

    def test_changed_run_after_issue_lookup_stays_open(self) -> None:
        github = self.github

        def changed_run(**kwargs: Any) -> object:
            result = github(**kwargs)
            if kwargs.get("method", "GET") == "GET":
                self.release_run = None
            return result

        with patch("control_plane.release_invitation.github_api_request", changed_run):
            self.advance()
        self.assertEqual(self.issue["state"], "open")
        self.assertEqual(self.comments, [])

    def test_bad_record_does_not_interrupt_other_records_or_invitations(self) -> None:
        bad = self.decision.model_copy(
            update={
                "record_id": "bad",
                "decided_at": "2026-09-25T01:00:00Z",
                "release_issue_url": "https://github.com/other/repo/issues/99",
            }
        )
        self.store.create_release_review_decision_record_if_absent(bad)
        assert self.release_run is not None
        run = self.release_run
        with (
            patch(
                "control_plane.client_release.read_client_release_run",
                side_effect=lambda **kwargs: run.model_copy(
                    update={"decision_record_id": kwargs["decision"].record_id}
                ),
            ),
            patch("control_plane.release_invitation.publish_release_invitation") as publish,
        ):
            self.advance()
            publish.assert_called_once()
        self.assertEqual(self.issue["state"], "closed")

    def test_temporary_hotfix_keeps_its_pending_client_review_open(self) -> None:
        hotfix = self.decision.model_copy(
            update={
                "record_id": "hotfix",
                "decision": "temporary_hotfix",
                "reason": "Repair live breakage",
                "actor_github_id": "7001",
            }
        )
        self.store.create_release_review_decision_record_if_absent(hotfix)
        self.issue["body"] = release_decision_marker(hotfix.record_id)
        assert self.release_run is not None
        self.release_run = self.release_run.model_copy(
            update={"decision_record_id": hotfix.record_id}
        )
        self.advance()
        self.assertEqual(self.issue["state"], "open")
        self.assertEqual(self.calls, [])

    def test_foreign_receipt_cannot_suppress_outcome(self) -> None:
        self.advance()
        foreign = self.comments[0].copy()
        foreign["performed_via_github_app"] = {"id": 88}
        self.comments[:] = [foreign]
        self.issue["state"] = "open"
        self.advance()
        self.assertEqual(len(self.comments), 2)
        self.assertEqual(self.issue["state"], "closed")

    def test_stop_between_records_leaves_remaining_records_untouched(self) -> None:
        stop = Event()
        original = self.store.list_release_review_decision_records

        def stop_after_listing(**kwargs: Any) -> Any:
            records = original(**kwargs)
            stop.set()
            return records

        with patch.object(self.store, "list_release_review_decision_records", stop_after_listing):
            advance_release_invitations(
                store=self.store, control_plane_root=self.root, stop_event=stop
            )
        self.assertEqual(self.calls, [])

    def test_completion_backoff_avoids_repeated_github_reads(self) -> None:
        backoff = ReleaseInvitationBackoff()
        self.advance(backoff)
        self.assertEqual(self.issue["state"], "closed")
        calls = self.calls.copy()
        self.advance(backoff)
        self.assertEqual(self.calls, calls)
