from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import patch

from control_plane.client_release import (
    ClientReleaseFailureView,
    ClientReleaseRunView,
    ClientReleaseStepView,
    ClientReleaseStepStatus,
)
from control_plane.release_invitation import advance_release_invitations, ReleaseInvitationBackoff
from control_plane.release_review_record import release_decision_marker
from control_plane.release_event_waits import settle_product_release_waits
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
        self.settle_waits = self.enterContext(
            patch("control_plane.release_event_waits.settle_product_release_waits")
        )
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

    def test_release_waits_require_successful_forward_release_and_current_production(self) -> None:
        assert self.release_run is not None
        for state, mode in (("stopped", "promote"), ("running", "promote"), ("passed", "rollback")):
            with (
                self.subTest(state=state, mode=mode),
                patch(
                    "control_plane.release_event_waits.release_version",
                    side_effect=AssertionError("Must not read production"),
                ),
            ):
                settle_product_release_waits(
                    store=self.store,
                    control_plane_root=self.root,
                    profile=self.profile,
                    decision=self.decision.model_copy(update={"release_start": mode}),
                    run=self.release_run.model_copy(update={"state": state}),
                )
        with (
            patch(
                "control_plane.release_event_waits.release_version",
                return_value=self.decision.checklist.production,
            ),
            patch("control_plane.release_event_waits.resolve_repository_release_waits") as resolve,
        ):
            settle_product_release_waits(
                store=self.store,
                control_plane_root=self.root,
                profile=self.profile,
                decision=self.decision,
                run=self.release_run,
            )
            resolve.assert_not_called()
        repositories = (
            self.profile.repository,
            self.profile.repository.split("/")[0] + "/planning",
        )
        source = SimpleNamespace(service_authz=None)
        policy = SimpleNamespace(
            policies=tuple(
                SimpleNamespace(repository=repo, service_authz=None) for repo in repositories
            ),
            find_repository_policy=lambda **kwargs: source,
        )
        with (
            patch(
                "control_plane.release_event_waits.release_version",
                return_value=self.decision.checklist.candidate,
            ),
            patch(
                "control_plane.release_event_waits.resolve_merge_train_policy_record",
                return_value=SimpleNamespace(policy=policy),
            ),
            patch(
                "control_plane.release_event_waits.planning_scope_resolver",
                return_value=lambda repo: SimpleNamespace(repository=repo, trusted_author_ids=()),
            ),
            patch("control_plane.release_event_waits.resolve_repository_release_waits") as resolve,
        ):
            settle_product_release_waits(
                store=self.store,
                control_plane_root=self.root,
                profile=self.profile,
                decision=self.decision,
                run=self.release_run,
            )
            self.assertEqual(
                {call.kwargs["scope"].repository for call in resolve.call_args_list},
                set(repositories),
            )
            self.assertEqual(resolve.call_args.kwargs["product"], self.profile.product)
            self.assertTrue(callable(resolve.call_args.kwargs["contains_commit"]))

    def test_only_current_release_waits_recheck_after_record_settlement(self) -> None:
        from control_plane.release_event_waits import RELEASE_WAIT_RECHECK_SECONDS

        backoff = ReleaseInvitationBackoff()
        with (
            patch(
                "control_plane.release_event_waits.release_version",
                return_value=self.decision.checklist.candidate,
            ),
            patch("control_plane.release_event_waits.monotonic", return_value=1000) as clock,
        ):
            self.advance(backoff)
            self.advance(backoff)
            self.assertEqual(self.settle_waits.call_count, 1)
            self.assertEqual(self.issue["state"], "closed")
            clock.return_value += RELEASE_WAIT_RECHECK_SECONDS + 1
            self.advance(backoff)
            self.assertEqual(self.settle_waits.call_count, 2)
            clock.return_value += RELEASE_WAIT_RECHECK_SECONDS + 1
            with patch(
                "control_plane.release_event_waits.release_version",
                return_value=self.decision.checklist.production,
            ):
                self.advance(backoff)
            self.assertEqual(self.settle_waits.call_count, 2)

    def test_wait_resolution_failure_does_not_skip_invitation_completion(self) -> None:
        with (
            patch(
                "control_plane.release_event_waits.advance_product_release_waits",
                side_effect=ValueError("Wait incomplete"),
            ),
            patch(
                "control_plane.release_invitation._client_release_requests_enabled",
                return_value=True,
            ),
        ):
            self.advance()
            self.assertEqual(self.issue["state"], "closed")

    def test_worker_closes_release_record_without_invitation_or_equal_lanes(self) -> None:
        self.advance()
        self.assertEqual(self.issue["state"], "closed")
        self.assertTrue("\n".join(self.comments[0]["body"].splitlines()[1:]).strip())
        self.assertNotIn("@", self.comments[0]["body"])
        self.advance()
        self.assertEqual(len(self.comments), 1)
        self.assertEqual(sum(method == "PATCH" for method, _ in self.calls), 1)

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

    def test_stopped_outcome_explains_failure_and_reads_current_production(self) -> None:
        failure = ClientReleaseFailureView(
            code="snapshot_time_limit",
            reason="The safety check could not start within its time limit.",
            record_id="failed-operation",
            recorded_at="2026-10-24T16:00:00Z",
        )
        self.release_run = ClientReleaseRunView(
            decision_record_id=self.decision.record_id,
            rollback_drill=True,
            state="stopped",
            steps=(
                ClientReleaseStepView(
                    step="promote-1", kind="promote", status="pass", operation_id="promoted"
                ),
                ClientReleaseStepView(
                    step="rollback-1",
                    kind="rollback",
                    status="fail",
                    operation_id=failure.record_id,
                    failure=failure,
                ),
            ),
        )
        for label, version in (
            ("reviewed", self.decision.checklist.candidate),
            ("previous", self.decision.checklist.production),
            (
                "another",
                self.decision.checklist.production.model_copy(update={"source_commit": "c" * 40}),
            ),
        ):
            with self.subTest(version=label):
                self.issue["state"] = "open"
                self.comments.clear()
                with patch(
                    "control_plane.release_invitation.release_version", return_value=version
                ):
                    self.advance()
                body = self.comments[0]["body"]
                self.assertIn(failure.reason, body)
                self.assertIn("rollback", body.lower())
                self.assertIn(label, body.split("\n\n")[-1])
                self.assertIn(version.source_commit[:7], body)
                self.assertEqual(self.issue["state"], "closed")

    def test_missing_failure_or_production_evidence_is_explicit_without_blocking_settlement(
        self,
    ) -> None:
        self.release_run = ClientReleaseRunView(
            decision_record_id=self.decision.record_id,
            rollback_drill=False,
            state="stopped",
            steps=(
                ClientReleaseStepView(
                    step="backup-1", kind="backup", status="cancelled", operation_id="cancelled"
                ),
            ),
        )
        with patch(
            "control_plane.release_invitation.release_version",
            side_effect=ValueError("Unavailable"),
        ) as read_production:
            self.advance()
        read_production.assert_called()
        body = self.comments[0]["body"]
        self.assertIn("backup", body.lower())
        self.assertNotIn(self.decision.checklist.candidate.source_commit[:7], body)
        self.assertNotIn(self.decision.checklist.production.source_commit[:7], body)
        self.assertEqual(self.issue["state"], "closed")

    def test_failed_rejection_rollback_identifies_rejected_version(self) -> None:
        self.store.write_release_review_decision_record(
            self.decision.model_copy(
                update={
                    "release_start": "rollback",
                    "decision": "changes_requested",
                    "reason": "Client rejected the hotfix",
                }
            )
        )
        self.release_run = ClientReleaseRunView(
            decision_record_id=self.decision.record_id,
            rollback_drill=False,
            state="stopped",
            steps=(
                ClientReleaseStepView(
                    step="rollback-1", kind="rollback", status="fail", operation_id="rollback"
                ),
            ),
        )
        with patch(
            "control_plane.release_invitation.release_version",
            return_value=self.decision.checklist.candidate,
        ):
            self.advance()
        self.assertIn("rejected", self.comments[0]["body"].split("\n\n")[-1])

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
        completed = cast(ClientReleaseRunView, cast(object, self.release_run))
        with patch(
            "control_plane.client_release.read_client_release_run",
            side_effect=lambda **kwargs: completed.model_copy(
                update={"decision_record_id": kwargs["decision"].record_id}
            ),
        ):
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

    def test_finished_earlier_attempt_cannot_close_the_same_candidates_active_retry(self) -> None:
        newer = self.decision.model_copy(
            update={"record_id": "retry", "decided_at": "2026-09-24T01:00:00Z"}
        )
        self.store.create_release_review_decision_record_if_absent(newer)
        self.issue["body"] += "\n" + release_decision_marker(newer.record_id)
        assert self.release_run is not None
        original = self.release_run
        retry = original.model_copy(
            update={"decision_record_id": newer.record_id, "state": "waiting", "steps": ()}
        )
        with patch(
            "control_plane.client_release.read_client_release_run",
            side_effect=lambda **kwargs: (
                retry if kwargs["decision"].record_id == newer.record_id else original
            ),
        ):
            self.advance()
        self.assertEqual(self.issue["state"], "open")
        self.assertEqual(len(self.comments), 1)
        finished_retry = retry.model_copy(update={"state": "passed"})
        with patch(
            "control_plane.client_release.read_client_release_run",
            side_effect=lambda **kwargs: (
                finished_retry if kwargs["decision"].record_id == newer.record_id else original
            ),
        ):
            self.advance()
        self.assertEqual(self.issue["state"], "closed")
        self.assertEqual(len(self.comments), 2)

    def test_record_only_decision_does_not_hide_running_release_outcome(self) -> None:
        newer = self.decision.model_copy(
            update={
                "record_id": "record-only",
                "decided_at": "2026-09-24T01:00:00Z",
                "release_start": "",
                "decision": "overridden",
                "reason": "Recorded override",
            }
        )
        self.store.create_release_review_decision_record_if_absent(newer)
        self.advance()
        self.assertEqual(self.issue["state"], "closed")
        self.assertEqual(len(self.comments), 1)

    def test_superseded_unstarted_intent_does_not_hold_a_completed_candidate_open(self) -> None:
        newer = self.decision.model_copy(
            update={"record_id": "completed-retry", "decided_at": "2026-09-24T01:00:00Z"}
        )
        self.store.create_release_review_decision_record_if_absent(newer)
        self.issue["body"] += "\n" + release_decision_marker(newer.record_id)
        assert self.release_run is not None
        completed = self.release_run.model_copy(update={"decision_record_id": newer.record_id})
        statuses: tuple[ClientReleaseStepStatus, ...] = (
            "not_started",
            "pending",
            "running",
            "reconciliation_required",
        )
        for status in statuses:
            with self.subTest(status=status):
                self.issue["state"] = "open"
                earlier = ClientReleaseRunView(
                    decision_record_id=self.decision.record_id,
                    rollback_drill=False,
                    state="waiting",
                    steps=(
                        ClientReleaseStepView(
                            step="backup-1",
                            kind="backup",
                            status=status,
                            operation_id="" if status == "not_started" else "backup",
                        ),
                    ),
                )
                with patch(
                    "control_plane.client_release.read_client_release_run",
                    side_effect=lambda **kwargs: (
                        completed if kwargs["decision"].record_id == newer.record_id else earlier
                    ),
                ):
                    self.advance()
                self.assertEqual(
                    self.issue["state"], "closed" if status == "not_started" else "open"
                )

    def test_superseded_unpublished_retry_does_not_keep_polling_a_closed_issue(self) -> None:
        pending = self.decision.model_copy(
            update={
                "record_id": "superseded-unpublished",
                "decided_at": "2026-09-24T01:00:00Z",
                "release_issue_url": "",
            }
        )
        rejected = pending.model_copy(
            update={
                "record_id": "latest-rejection",
                "decided_at": "2026-09-25T01:00:00Z",
                "decision": "changes_requested",
                "reason": "Revise it",
                "release_start": "",
                "release_issue_url": self.decision.release_issue_url,
            }
        )
        for item in (pending, rejected):
            self.store.create_release_review_decision_record_if_absent(item)
        self.issue["state"] = "closed"
        backoff = ReleaseInvitationBackoff()
        assert self.release_run is not None
        original = self.release_run
        unstarted = ClientReleaseRunView(
            decision_record_id=pending.record_id,
            rollback_drill=False,
            state="waiting",
            steps=(
                ClientReleaseStepView(
                    step="backup-1", kind="backup", status="not_started", operation_id=""
                ),
            ),
        )
        with patch(
            "control_plane.client_release.read_client_release_run",
            side_effect=lambda **kwargs: (
                unstarted if kwargs["decision"].record_id == pending.record_id else original
            ),
        ):
            self.advance(backoff)
            calls = self.calls.copy()
            with patch("control_plane.release_invitation.monotonic", return_value=1e20):
                self.advance(backoff)
        self.assertEqual(self.calls, calls)
        self.assertEqual(len(self.comments), 1)

    def test_finished_earlier_attempt_keeps_the_same_candidates_hotfix_review_open(self) -> None:
        hotfix = self.decision.model_copy(
            update={
                "record_id": "hotfix-retry",
                "decided_at": "2026-09-24T01:00:00Z",
                "decision": "temporary_hotfix",
                "reason": "Repair live breakage",
                "actor_github_id": "7001",
            }
        )
        self.store.create_release_review_decision_record_if_absent(hotfix)
        assert self.release_run is not None
        original = self.release_run
        with patch(
            "control_plane.client_release.read_client_release_run",
            side_effect=lambda **kwargs: original.model_copy(
                update={"decision_record_id": kwargs["decision"].record_id}
            ),
        ):
            self.advance()
        self.assertEqual(self.issue["state"], "open")
        self.assertEqual(len(self.comments), 1)

    def test_closed_issue_and_unpublished_retry_preserve_outcomes_and_reconcile_closure(
        self,
    ) -> None:
        self.issue["state"] = "closed"
        self.advance()
        self.assertEqual(len(self.comments), 1)
        pending = self.decision.model_copy(
            update={
                "record_id": "unpublished-retry",
                "decided_at": "2026-09-24T01:00:00Z",
                "release_issue_url": "",
            }
        )
        self.store.create_release_review_decision_record_if_absent(pending)
        self.issue["state"] = "open"
        assert self.release_run is not None
        original = self.release_run
        with patch(
            "control_plane.client_release.read_client_release_run",
            side_effect=lambda **kwargs: (
                None if kwargs["decision"].record_id == pending.record_id else original
            ),
        ):
            self.advance()
            self.advance()
        self.assertEqual(self.issue["state"], "open")
        self.assertEqual(len(self.comments), 1)
        self.store.record_release_review_decision_publication(
            record_id=pending.record_id, release_issue_url=self.decision.release_issue_url
        )
        self.comments.append(
            {
                "id": 90,
                "body": release_decision_marker(pending.record_id),
                "performed_via_github_app": {"id": 77},
            }
        )
        with patch(
            "control_plane.client_release.read_client_release_run",
            side_effect=lambda **kwargs: original.model_copy(
                update={"decision_record_id": kwargs["decision"].record_id}
            ),
        ):
            self.advance()
            self.advance()
        self.assertEqual(self.issue["state"], "closed")
        self.assertEqual(len(self.comments), 3)

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
        ):
            self.store.write_product_profile_record(
                self.profile.model_copy(update={"release_on_acceptance": "promote"})
            )
            self.advance()
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
