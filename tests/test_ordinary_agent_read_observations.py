"""Finite read observations survive pending CI and cleanup recovery."""

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from sqlalchemy import select

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.contracts import ordinary_agent_snapshot as snapshots
from control_plane.contracts.merge_train_controller_state import MergeTrainControllerStateRecord
from control_plane.merge_train import MergeTrainCheckStatus
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.storage.postgres import (
    LaunchplaneMergeTrainControllerStateRow,
    LaunchplaneOrdinaryAgentReadOutcomeRow,
    LaunchplaneOrdinaryAgentReadAttemptRow,
)
from tests import test_ordinary_agent_landing_storage as landing_support


class OrdinaryAgentReadObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = landing_support.OrdinaryAgentLandingStorageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.store = self.fixture.store
        self.request = self.fixture.request
        self.clock = self.fixture.fixture.fixture.clock
        self.now = self.fixture.fixture.fixture.now
        self.enterContext(patch.object(effects, "CANDIDATE_CHECK_DELAYS_SECONDS", (1,)))
        # The fixture seeds built artifacts; exercise the actual candidate-check
        # stage with that candidate as the durable controller pointer.
        with self.store._session_factory() as session:
            row = session.get(
                LaunchplaneMergeTrainControllerStateRow, self.fixture.fence.controller_key
            )
            assert row is not None
            current = MergeTrainControllerStateRecord.model_validate(row.payload)
            row.payload = current.model_copy(
                update={
                    "active_record_id": self.fixture.candidate.record_id,
                }
            ).model_dump(mode="json")
            session.commit()

    def advance(self) -> None:
        self.now += 1
        self.clock.return_value = datetime.fromtimestamp(self.now, timezone.utc).isoformat()

    def read_attempt_count(self, purpose: str) -> int:
        with self.store._session_factory() as session:
            return len(
                tuple(
                    session.scalars(
                        select(LaunchplaneOrdinaryAgentReadAttemptRow.attempt_id).where(
                            LaunchplaneOrdinaryAgentReadAttemptRow.request_id
                            == self.request.request_id,
                            LaunchplaneOrdinaryAgentReadAttemptRow.purpose == purpose,
                        )
                    )
                )
            )

    def observe(
        self,
        result: snapshots.OrdinaryAgentMergeTrainSnapshotResult
        | snapshots.OrdinaryAgentCandidateCheckResult,
        *,
        cleanup_unknown: bool = False,
    ) -> effects.OrdinaryAgentSnapshotAttemptRecord:
        if isinstance(result, snapshots.OrdinaryAgentMergeTrainSnapshotResult):
            attempt = self.store.reserve_ordinary_agent_snapshot_attempt(
                request_id=self.request.request_id,
                expected_binding_revision=1,
                controller_fence=self.fixture.fence,
            )
        else:
            attempt = self.store.reserve_ordinary_agent_candidate_check_attempt(
                request_id=self.request.request_id,
                expected_binding_revision=1,
                controller_fence=self.fixture.fence,
                candidate_sha=result.candidate_identity.sha,
            )
        permit = self.store.reserve_ordinary_agent_read_custody_attempt(
            attempt_id=attempt.attempt_id,
            expected_attempt_revision=attempt.revision,
        )
        self.fixture.fixture.issue(permit)
        if isinstance(result, snapshots.OrdinaryAgentMergeTrainSnapshotResult):
            completed = self.store.record_ordinary_agent_snapshot_success(
                attempt_id=attempt.attempt_id,
                custody_attempt_id=permit.custody_attempt_id,
                result=result,
            )
        else:
            completed = self.store.record_ordinary_agent_candidate_check_success(
                attempt_id=attempt.attempt_id,
                custody_attempt_id=permit.custody_attempt_id,
                result=result,
            )
        if cleanup_unknown:
            self.store.mark_ordinary_agent_custody_cleanup_unknown(
                attempt_id=permit.custody_attempt_id
            )
            self.store.record_ordinary_agent_read_failure(
                attempt_id=attempt.attempt_id,
                custody_attempt_id=permit.custody_attempt_id,
                reason_code="cleanup_unknown",
                counts=result.counts,
            )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=permit.custody_attempt_id,
            reason="confirmed_revoked",
        )
        return completed

    def candidate_result(
        self, status: MergeTrainCheckStatus
    ) -> snapshots.OrdinaryAgentCandidateCheckResult:
        candidate = self.fixture.candidate.candidate
        return snapshots.OrdinaryAgentCandidateCheckResult(
            candidate_identity=snapshots.OrdinaryAgentCommitIdentity(
                sha=candidate.candidate_sha,
                tree_sha=candidate.candidate_tree_sha,
            ),
            protection=self.fixture.fixture.snapshot_result().protection,
            status=status,
            counts=snapshots.OrdinaryAgentProviderRequestCounts(
                rest_core_requests=1, graphql_requests=1, graphql_points=1
            ),
            observation_sha256="d" * 64,
        )

    def test_candidate_unknown_then_pending_then_pass_uses_accepted_source_protection(self) -> None:
        ready = self.fixture.fixture.snapshot_result()
        pending = ready.model_copy(
            update={
                "snapshot": ready.snapshot.model_copy(
                    update={
                        "pull_requests": tuple(
                            item.model_copy(update={"required_checks_status": "unknown"})
                            for item in ready.snapshot.pull_requests
                        ),
                    }
                )
            }
        )
        self.observe(pending)
        self.advance()
        self.observe(ready)
        for status in ("unknown", "pending", "pass"):
            result = self.candidate_result(status)
            completed = self.observe(result, cleanup_unknown=status == "unknown")
            if status != "pass":
                expected_due = completed.next_due_at
                self.assertIsNotNone(expected_due)
                attempts_before_wait = self.read_attempt_count("candidate_check")
                with self.assertRaisesRegex(
                    OrdinaryAgentSessionAdmissionDenied, "candidate_check_wait"
                ) as raised:
                    self.store.reserve_ordinary_agent_candidate_check_attempt(
                        request_id=self.request.request_id,
                        expected_binding_revision=1,
                        controller_fence=self.fixture.fence,
                        candidate_sha=result.candidate_identity.sha,
                    )
                self.assertEqual(raised.exception.retry_not_before, expected_due)
                self.assertEqual(self.read_attempt_count("candidate_check"), attempts_before_wait)
                self.advance()
            else:
                replay = self.store.reserve_ordinary_agent_candidate_check_attempt(
                    request_id=self.request.request_id,
                    expected_binding_revision=1,
                    controller_fence=self.fixture.fence,
                    candidate_sha=result.candidate_identity.sha,
                )
                self.assertEqual(replay, completed)

    def test_changed_protection_cannot_be_recovered_as_success(self) -> None:
        self.observe(self.fixture.fixture.snapshot_result())
        result = self.candidate_result("pass")
        result = result.model_copy(
            update={
                "protection": result.protection.model_copy(
                    update={"evaluated_rules_sha256": "e" * 64}
                )
            }
        )
        completed = self.observe(result, cleanup_unknown=True)
        self.assertEqual(completed.reason_code, "protection_changed")
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "protection_changed"):
            self.store.reserve_ordinary_agent_candidate_check_attempt(
                request_id=self.request.request_id,
                expected_binding_revision=1,
                controller_fence=self.fixture.fence,
                candidate_sha=result.candidate_identity.sha,
            )

    def test_candidate_unknown_stops_after_observation_budget(self) -> None:
        self.observe(self.fixture.fixture.snapshot_result())
        result = self.candidate_result("unknown")
        with patch.object(effects, "MAX_CANDIDATE_CHECK_OBSERVATIONS", 2):
            self.observe(result)
            self.advance()
            terminal = self.observe(result, cleanup_unknown=True)
            self.assertEqual(terminal.reason_code, "candidate_check_observation_budget_exhausted")
            with self.assertRaisesRegex(
                OrdinaryAgentSessionAdmissionDenied, "candidate_check_observation_budget_exhausted"
            ):
                self.store.reserve_ordinary_agent_candidate_check_attempt(
                    request_id=self.request.request_id,
                    expected_binding_revision=1,
                    controller_fence=self.fixture.fence,
                    candidate_sha=result.candidate_identity.sha,
                )

    def test_missing_historical_completion_becomes_durable_terminal_reason(self) -> None:
        completed = self.observe(self.fixture.fixture.snapshot_result(), cleanup_unknown=True)
        # Simulate pre-metadata stored history. No provider call can restore a
        # lost local decision, so recovery must terminate without guessing.
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneOrdinaryAgentReadOutcomeRow, completed.attempt_id)
            assert row is not None
            row.payload = {key: value for key, value in row.payload.items() if key != "completion"}
            session.commit()
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "read_recovery_evidence_unavailable"
        ):
            self.store.reserve_ordinary_agent_snapshot_attempt(
                request_id=self.request.request_id,
                expected_binding_revision=1,
                controller_fence=self.fixture.fence,
            )
        with self.store._session_factory() as session:
            attempt = session.get(LaunchplaneOrdinaryAgentReadAttemptRow, completed.attempt_id)
            assert attempt is not None
            self.assertEqual(attempt.state, "exhausted")
            self.assertEqual(attempt.payload["reason_code"], "read_recovery_evidence_unavailable")

    def test_source_observations_stop_at_their_finite_budget(self) -> None:
        ready = self.fixture.fixture.snapshot_result()
        pending = ready.model_copy(
            update={
                "snapshot": ready.snapshot.model_copy(
                    update={
                        "pull_requests": tuple(
                            item.model_copy(update={"mergeable": "unknown"})
                            for item in ready.snapshot.pull_requests
                        ),
                    }
                )
            }
        )
        for index in range(effects.MAX_SNAPSHOT_SOURCE_OBSERVATIONS):
            terminal = self.observe(pending, cleanup_unknown=True)
            if index + 1 < effects.MAX_SNAPSHOT_SOURCE_OBSERVATIONS:
                with self.assertRaisesRegex(
                    OrdinaryAgentSessionAdmissionDenied, "source_check_wait"
                ):
                    self.store.reserve_ordinary_agent_snapshot_attempt(
                        request_id=self.request.request_id,
                        expected_binding_revision=1,
                        controller_fence=self.fixture.fence,
                    )
                self.advance()
        self.assertEqual(terminal.state, "exhausted")
        self.assertIsNone(terminal.next_due_at)
        self.assertEqual(terminal.reason_code, "source_check_observation_budget_exhausted")
        for _ in range(2):
            with self.assertRaisesRegex(
                OrdinaryAgentSessionAdmissionDenied, "source_check_observation_budget_exhausted"
            ):
                self.store.reserve_ordinary_agent_snapshot_attempt(
                    request_id=self.request.request_id,
                    expected_binding_revision=1,
                    controller_fence=self.fixture.fence,
                )

    def test_unlisted_pending_pull_request_does_not_block_requested_ready_set(self) -> None:
        ready = self.fixture.fixture.snapshot_result()
        requested_numbers = tuple(item.number for item in self.request.pull_requests)
        requested_pending = ready.model_copy(
            update={
                "snapshot": ready.snapshot.model_copy(
                    update={
                        "pull_requests": tuple(
                            item.model_copy(update={"required_checks_status": "pending"})
                            for item in ready.snapshot.pull_requests
                        )
                    }
                )
            }
        )
        pending = self.observe(requested_pending)
        self.assertEqual(pending.reason_code, "source_checks_undecided")
        expected_due = pending.next_due_at
        self.assertIsNotNone(expected_due)
        attempts_before_wait = self.read_attempt_count("snapshot")
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "source_check_wait"
        ) as raised:
            self.store.reserve_ordinary_agent_snapshot_attempt(
                request_id=self.request.request_id,
                expected_binding_revision=1,
                controller_fence=self.fixture.fence,
            )
        self.assertEqual(raised.exception.retry_not_before, expected_due)
        self.assertEqual(self.read_attempt_count("snapshot"), attempts_before_wait)
        self.advance()

        extra_number = max(requested_numbers) + 1
        extra_head_sha = "e" * 40
        extra_pull_request = ready.snapshot.pull_requests[0].model_copy(
            update={
                "number": extra_number,
                "head_sha": extra_head_sha,
                "required_checks_status": "pending",
            }
        )
        scoped_ready = snapshots.OrdinaryAgentMergeTrainSnapshotResult.model_validate(
            ready.model_copy(
                update={
                    "snapshot": ready.snapshot.model_copy(
                        update={
                            "pull_requests": (*ready.snapshot.pull_requests, extra_pull_request),
                        }
                    ),
                    "head_identities": (
                        *ready.head_identities,
                        snapshots.OrdinaryAgentPullRequestHeadIdentity(
                            pull_request_number=extra_number,
                            identity=snapshots.OrdinaryAgentCommitIdentity(
                                sha=extra_head_sha,
                                tree_sha="f" * 40,
                            ),
                        ),
                    ),
                }
            ).model_dump(mode="json")
        )
        self.assertTrue(scoped_ready.awaits_source_observation)
        self.assertFalse(scoped_ready.awaits_source_observation_for(requested_numbers))
        self.assertTrue(
            scoped_ready.awaits_source_observation_for((*requested_numbers, extra_number + 1))
        )

        completed = self.observe(scoped_ready)
        self.assertEqual(completed.state, "completed")
        self.assertIsNone(completed.reason_code)
        self.assertEqual(completed.result, scoped_ready)
        replay = self.store.reserve_ordinary_agent_snapshot_attempt(
            request_id=self.request.request_id,
            expected_binding_revision=1,
            controller_fence=self.fixture.fence,
        )
        self.assertEqual(replay, completed)
        assert isinstance(replay.result, snapshots.OrdinaryAgentMergeTrainSnapshotResult)
        self.assertEqual(
            tuple(item.pull_request_number for item in replay.result.head_identities),
            (*requested_numbers, extra_number),
        )
