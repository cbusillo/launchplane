"""Claim-bound recovery reads use one database snapshot without granting work."""

from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from threading import Event, current_thread
import unittest

from sqlalchemy import delete, event, func, select

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.contracts.merge_train_effect import (
    MergeTrainEffectLineage,
    PullRequestHeadRefreshEffect,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentLeaseRecord
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.storage.postgres import (
    LaunchplaneOrdinaryAgentEffectRow,
    LaunchplaneOrdinaryAgentFiniteRequestRow,
    LaunchplaneOrdinaryAgentJobClaimRow,
    LaunchplaneOrdinaryAgentLandingPreparationRow,
    LaunchplaneOrdinaryAgentLeaseRow,
    LaunchplaneOrdinaryAgentReadAttemptRow,
)
from tests import test_ordinary_agent_effect_storage as effect_support
from tests import test_ordinary_agent_landing_storage as landing_support
from tests import test_ordinary_agent_session_storage as session_support


class OrdinaryAgentJobRecoverySnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = session_support.OrdinaryAgentSessionStorageTests()
        self.session.setUp(installation_id=77)
        self.addCleanup(self.session.doCleanups)
        self.fixture = effect_support.OrdinaryAgentEffectStorageTests()
        self.fixture.prepare_effect_fixture(self.session)
        self.store = self.fixture.store
        self.fence, self.command = self.fixture.prepare_controller()
        self.claim = self.fixture.claim

    def snapshot(self) -> effects.OrdinaryAgentJobRecoverySnapshot:
        return self.store.read_ordinary_agent_job_recovery_snapshot(
            claim_fence=self.claim.claim_fence
        )

    def test_reserved_effect_readiness_deadlines_and_custody_are_explicit(self) -> None:
        effect = self.store.reserve_ordinary_agent_effect(
            request_id=self.fixture.request.request_id,
            expected_binding_revision=1,
            controller_fence=self.fence,
            command=self.command,
            semantic_ordinal=1,
        )
        read = self.store.reserve_ordinary_agent_snapshot_attempt(
            request_id=self.fixture.request.request_id,
            expected_binding_revision=1,
            controller_fence=self.fence,
        )
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneOrdinaryAgentReadAttemptRow, read.attempt_id)
            assert row is not None
            first_due = self.session.now + 60
            first = read.model_copy(
                update={
                    "state": "incomplete",
                    "revision": read.revision + 1,
                    "next_due_at": first_due,
                }
            )
            row.state, row.revision, row.payload = (
                first.state,
                first.revision,
                first.model_dump(mode="json", exclude_none=True),
            )
            later_due = self.session.now + 120
            second = first.model_copy(
                update={
                    "attempt_id": "candidate-check-two",
                    "purpose": "candidate_check",
                    "candidate_sha": "f" * 40,
                    "attempt_ordinal": 1,
                    "next_due_at": later_due,
                }
            )
            session.add(
                LaunchplaneOrdinaryAgentReadAttemptRow(
                    attempt_id=second.attempt_id,
                    request_id=second.request_id,
                    binding_revision=second.binding_revision,
                    purpose=second.purpose,
                    candidate_sha=second.candidate_sha,
                    attempt_ordinal=second.attempt_ordinal,
                    state=second.state,
                    revision=second.revision,
                    payload=second.model_dump(mode="json", exclude_none=True),
                )
            )
            session.commit()

        view = self.store.read_ordinary_agent_job(
            proof=self.session.proof, request_id=self.fixture.request.request_id
        )
        initial = self.snapshot()
        assert initial.unresolved_effect is not None
        self.assertEqual(view.unresolved_effects, 0)
        self.assertEqual(initial.unresolved_effect.effect, effect)
        self.assertFalse(initial.custody_uncertain)
        self.assertEqual(initial.pending_read_retry_not_before, later_due)
        self.assertEqual(
            (initial.inspected_app_id, initial.inspected_installation_id),
            (self.session.envelope.custody.github_app_id, 77),
        )
        self.assertEqual((initial.completed_effects, initial.total_effects), (0, 1))

        permit = self.store.reserve_ordinary_custody_attempt(
            effect_id=effect.effect_id, expected_effect_revision=effect.revision
        )
        self.fixture.issue(permit)
        self.store.mark_ordinary_agent_custody_cleanup_unknown(attempt_id=permit.attempt_id)
        app_due = self.session.now + 180
        installation_due = self.session.now + 240
        for quota_key, retry_not_before in (
            (
                effects.OrdinaryAgentProviderQuotaKey(
                    authority_kind="app", authority_id=42, resource_class="core"
                ),
                app_due,
            ),
            (
                effects.OrdinaryAgentProviderQuotaKey(
                    authority_kind="installation",
                    authority_id=77,
                    resource_class="secondary",
                ),
                installation_due,
            ),
        ):
            self.store.record_provider_wait(
                quota_key=quota_key,
                observation=effects.OrdinaryAgentProviderWaitObservation(
                    retry_not_before=retry_not_before,
                    classification="secondary_rate_limit"
                    if quota_key.resource_class == "secondary"
                    else "primary_rate_limit",
                ),
            )

        uncertain = self.snapshot()
        self.assertTrue(uncertain.custody_uncertain)
        self.assertEqual(uncertain.provider_retry_not_before, installation_due)
        public = self.store.read_ordinary_agent_job(
            proof=self.session.proof, request_id=self.fixture.request.request_id
        )
        self.assertEqual(public.unresolved_effects, 1)
        self.assertEqual(uncertain.total_effects, 1)

    def test_unknown_history_and_db_current_binding_are_read_together(self) -> None:
        effect = self.store.reserve_ordinary_agent_effect(
            request_id=self.fixture.request.request_id,
            expected_binding_revision=1,
            controller_fence=self.fence,
            command=self.command,
            semantic_ordinal=1,
        )
        permit = self.store.reserve_ordinary_custody_attempt(
            effect_id=effect.effect_id, expected_effect_revision=effect.revision
        )
        self.fixture.issue(permit)
        child = self.store.checkpoint_ordinary_semantic_dispatch(
            effect_id=effect.effect_id,
            controller_fence=self.fence,
            custody_attempt_id=permit.attempt_id,
            fixed_token_expires_at=self.session.now + 300,
        )
        self.store.record_ordinary_semantic_outcome(
            child_id=child.child_id,
            typed_outcome=effects.OrdinaryAgentUnknownOutcome(reason="transport_ambiguous"),
        )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=permit.attempt_id, reason="confirmed_revoked"
        )

        first = self.snapshot()
        assert first.unresolved_effect is not None
        assert first.unresolved_effect.outcome is not None
        self.assertEqual(first.unresolved_effect.child, child)
        self.assertEqual(first.unresolved_effect.outcome.kind, "unknown")

        with self.store._session_factory() as session:
            request_row = session.get(
                LaunchplaneOrdinaryAgentFiniteRequestRow, self.fixture.request.request_id
            )
            effect_row = session.get(LaunchplaneOrdinaryAgentEffectRow, effect.effect_id)
            assert request_row is not None and effect_row is not None
            request = self.fixture.request.model_copy(update={"binding_revision": 2})
            request_row.payload = request.model_dump(mode="json")
            current = effect.model_copy(
                update={
                    "effect_id": "effect-current-binding",
                    "binding_revision": 2,
                    "semantic_ordinal": 2,
                    "action_ordinal": 2,
                }
            )
            session.add(
                LaunchplaneOrdinaryAgentEffectRow(
                    effect_id=current.effect_id,
                    lease_id=current.lease_id,
                    request_id=current.request_id,
                    scope_sha256=current.scope_sha256,
                    binding_revision=current.binding_revision,
                    action_ordinal=current.action_ordinal,
                    semantic_key=effect_row.semantic_key,
                    command_sha256=current.command_sha256,
                    revision=current.revision,
                    payload=current.model_dump(mode="json", exclude_none=True),
                )
            )
            session.commit()

        rebound = self.snapshot()
        self.assertEqual(rebound.binding_revision, 2)
        self.assertEqual(rebound.scope_sha256, self.fixture.request.scope_sha256)
        assert rebound.unresolved_effect is not None
        self.assertEqual(rebound.unresolved_effect.effect.effect_id, current.effect_id)
        self.assertEqual(rebound.total_effects, 2)

    def test_open_preparation_denies_unrelated_effect_before_charge(self) -> None:
        landing = landing_support.OrdinaryAgentLandingStorageTests()
        landing.setUp()
        self.addCleanup(landing.doCleanups)
        preparation = landing.reserve().preparation

        snapshot = landing.store.read_ordinary_agent_job_recovery_snapshot(
            claim_fence=landing.claimed.claim_fence
        )
        self.assertEqual(snapshot.open_landing_preparation, preparation)
        self.assertIsNone(snapshot.unresolved_effect)
        self.assertEqual(landing.request.execution_record_ids, ())

        command = effects.PullRequestHeadRefreshCommand(
            effect=PullRequestHeadRefreshEffect(
                lineage=MergeTrainEffectLineage(
                    repository=landing.request.target.repository,
                    base_branch=landing.request.target.base_branch,
                ),
                pull_request_number=landing.pull_request,
                expected_head_sha=landing.request.pull_requests[0].head_sha,
                expected_base_sha=landing.request.base_sha,
            )
        )
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "prior_effect_unresolved"):
            landing.store.reserve_ordinary_agent_effect(
                request_id=landing.request.request_id,
                expected_binding_revision=1,
                controller_fence=landing.fence,
                command=command,
                semantic_ordinal=2,
            )
        with landing.store._session_factory() as session:
            lease_row = session.get(LaunchplaneOrdinaryAgentLeaseRow, landing.request.lease_id)
            assert lease_row is not None
            lease = OrdinaryAgentLeaseRecord.model_validate(lease_row.payload)
            effect_count = session.scalar(
                select(func.count(LaunchplaneOrdinaryAgentEffectRow.effect_id)).where(
                    LaunchplaneOrdinaryAgentEffectRow.request_id == landing.request.request_id
                )
            )
        self.assertEqual(lease.budget.actions_used, preparation.action_ordinal)
        self.assertEqual(effect_count, 0)
        self.assertEqual(
            landing.store.read_ordinary_agent_job_recovery_snapshot(
                claim_fence=landing.claimed.claim_fence
            ).open_landing_preparation,
            preparation,
        )

    def test_corrupt_open_preparation_effect_overlap_is_rejected(self) -> None:
        landing = landing_support.OrdinaryAgentLandingStorageTests()
        landing.setUp()
        self.addCleanup(landing.doCleanups)
        preparation = landing.reserve().preparation
        command = effects.PullRequestHeadRefreshCommand(
            effect=PullRequestHeadRefreshEffect(
                lineage=MergeTrainEffectLineage(
                    repository=landing.request.target.repository,
                    base_branch=landing.request.target.base_branch,
                ),
                pull_request_number=landing.pull_request,
                expected_head_sha=landing.request.pull_requests[0].head_sha,
                expected_base_sha=landing.request.base_sha,
            )
        )
        with landing.store._session_factory() as session:
            row = session.get(
                LaunchplaneOrdinaryAgentLandingPreparationRow,
                preparation.preparation_id,
            )
            assert row is not None
            row.payload = preparation.model_copy(update={"state": "consumed"}).model_dump(
                mode="json", exclude_none=True
            )
            session.commit()
        landing.store.reserve_ordinary_agent_effect(
            request_id=landing.request.request_id,
            expected_binding_revision=1,
            controller_fence=landing.fence,
            command=command,
            semantic_ordinal=2,
        )
        with landing.store._session_factory() as session:
            row = session.get(
                LaunchplaneOrdinaryAgentLandingPreparationRow,
                preparation.preparation_id,
            )
            assert row is not None
            row.payload = preparation.model_dump(mode="json", exclude_none=True)
            session.commit()

        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "landing_preparation_conflict"
        ):
            landing.store.read_ordinary_agent_job_recovery_snapshot(
                claim_fence=landing.claimed.claim_fence
            )

    def test_completed_pending_read_deadline_is_a_retry_backoff(self) -> None:
        attempt = self.store.reserve_ordinary_agent_snapshot_attempt(
            request_id=self.fixture.request.request_id,
            expected_binding_revision=1,
            controller_fence=self.fence,
        )
        custody = self.store.reserve_ordinary_agent_read_custody_attempt(
            attempt_id=attempt.attempt_id,
            expected_attempt_revision=attempt.revision,
        )
        self.fixture.issue(custody)
        result = self.fixture.snapshot_result()
        pending_snapshot = result.snapshot.model_copy(
            update={
                "pull_requests": tuple(
                    item.model_copy(update={"required_checks_status": "pending"})
                    for item in result.snapshot.pull_requests
                )
            }
        )
        completed = self.store.record_ordinary_agent_snapshot_success(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=custody.custody_attempt_id,
            result=result.model_copy(update={"snapshot": pending_snapshot}),
        )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=custody.custody_attempt_id,
            reason="confirmed_revoked",
        )
        self.assertEqual(completed.state, "completed")
        assert completed.next_due_at is not None

        with self.assertRaises(OrdinaryAgentSessionAdmissionDenied) as raised:
            self.store.reserve_ordinary_agent_snapshot_attempt(
                request_id=self.fixture.request.request_id,
                expected_binding_revision=1,
                controller_fence=self.fence,
            )
        self.assertEqual(raised.exception.reason_code, "source_check_wait")
        self.assertEqual(raised.exception.retry_not_before, completed.next_due_at)
        self.assertEqual(
            self.snapshot().pending_read_retry_not_before,
            completed.next_due_at,
        )

    def test_cancelled_expired_job_still_reads_but_stale_or_missing_claim_data_does_not(
        self,
    ) -> None:
        with self.store._session_factory() as session:
            request_row = session.get(
                LaunchplaneOrdinaryAgentFiniteRequestRow, self.fixture.request.request_id
            )
            claim_row = session.get(
                LaunchplaneOrdinaryAgentJobClaimRow, self.fixture.request.request_id
            )
            assert request_row is not None and claim_row is not None
            request = self.fixture.request.model_copy(
                update={
                    "status": "cancelled",
                    "cancellation_requested_at": self.session.now,
                    "expires_at": self.session.now + 1,
                    "continuation_expires_at": None,
                }
            )
            request_row.payload = request.model_dump(mode="json")
            claim_row.claim_expires_at = self.session.now + 100
            session.commit()
        self.session.clock.return_value = datetime.fromtimestamp(
            self.session.now + 2, timezone.utc
        ).isoformat()

        snapshot = self.snapshot()
        self.assertEqual(snapshot.request_id, self.fixture.request.request_id)
        self.assertEqual(snapshot.observed_at, self.session.now + 2)

        stale = self.claim.claim_fence.model_copy(update={"worker_id": "stale-worker"})
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "job_claim_lost"):
            self.store.read_ordinary_agent_job_recovery_snapshot(claim_fence=stale)
        with self.store._session_factory() as session:
            session.execute(
                delete(LaunchplaneOrdinaryAgentFiniteRequestRow).where(
                    LaunchplaneOrdinaryAgentFiniteRequestRow.request_id
                    == self.fixture.request.request_id
                )
            )
            session.commit()
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "job_unavailable"):
            self.snapshot()

    def test_sqlite_deferred_transaction_holds_one_prewrite_snapshot(self) -> None:
        effect = self.store.reserve_ordinary_agent_effect(
            request_id=self.fixture.request.request_id,
            expected_binding_revision=1,
            controller_fence=self.fence,
            command=self.command,
            semantic_ordinal=1,
        )
        with self.store._engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
        paused, resume = Event(), Event()

        def pause_before_effect_read(
            _connection: object,
            _cursor: object,
            statement: str,
            _parameters: object,
            _context: object,
            _executemany: bool,
        ) -> None:
            if (
                current_thread().name.startswith("snapshot-reader")
                and statement.lstrip().upper().startswith("SELECT")
                and "launchplane_ordinary_agent_effects" in statement
            ):
                paused.set()
                self.assertTrue(resume.wait(timeout=5))

        event.listen(self.store._engine, "before_cursor_execute", pause_before_effect_read)
        try:
            with ThreadPoolExecutor(
                max_workers=1, thread_name_prefix="snapshot-reader"
            ) as executor:
                future = executor.submit(self.snapshot)
                self.assertTrue(paused.wait(timeout=5))
                with self.store._session_factory() as session:
                    row = session.get(LaunchplaneOrdinaryAgentEffectRow, effect.effect_id)
                    assert row is not None
                    completed = effect.model_copy(
                        update={"state": "completed", "revision": effect.revision + 1}
                    )
                    row.revision = completed.revision
                    row.payload = completed.model_dump(mode="json", exclude_none=True)
                    session.commit()
                resume.set()
                before_write = future.result(timeout=5)
        finally:
            resume.set()
            event.remove(self.store._engine, "before_cursor_execute", pause_before_effect_read)

        assert before_write.unresolved_effect is not None
        self.assertEqual(before_write.unresolved_effect.effect, effect)
        after_write = self.snapshot()
        self.assertIsNone(after_write.unresolved_effect)
        self.assertEqual(after_write.completed_effects, 1)


if __name__ == "__main__":
    unittest.main()
