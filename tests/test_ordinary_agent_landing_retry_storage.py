"""Landing retries preserve one action while renewing dispatch authority."""

from datetime import datetime, timezone
import hashlib
import unittest

from sqlalchemy import select

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_admission_record import (
    MergeAdmissionProposal,
    MergeLandingOutcomeRecord,
)
from control_plane.contracts.ordinary_agent_effect import (
    MAX_ORDINARY_LANDING_ATTEMPTS_PER_ENTRY,
    OrdinaryAgentControllerFence,
    OrdinaryAgentKnownNotDispatchedOutcome,
    OrdinaryAgentIncompleteReadObservation,
    OrdinaryAgentLandingFinalization,
    OrdinaryAgentLandingPreparation,
    OrdinaryAgentLandingReservation,
    OrdinaryAgentReconciliationObservation,
    OrdinaryAgentUnknownOutcome,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentLeaseRecord,
)
from control_plane.github_app_identity import ordinary_agent_effect_permissions
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.storage.postgres import (
    LaunchplaneMergeTrainBatchCandidateRow,
    LaunchplaneMergeTrainBatchLandingPlanRow,
    LaunchplaneOrdinaryAgentEffectReconciliationRow,
    LaunchplaneOrdinaryAgentFiniteRequestRow,
    LaunchplaneOrdinaryAgentLandingBindingRow,
    LaunchplaneOrdinaryAgentLandingPreparationRow,
    LaunchplaneOrdinaryAgentLeaseRow,
    LaunchplaneOrdinaryAgentSemanticDispatchRow,
)
from tests import test_ordinary_agent_landing_storage as landing_support
from tests import test_ordinary_agent_session_storage as session_support


class OrdinaryAgentLandingRetryStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        session_fixture = session_support.OrdinaryAgentSessionStorageTests()
        session_fixture.setUp()
        self.addCleanup(session_fixture.doCleanups)
        self.prepare_retry_fixture(session_fixture)

    def prepare_retry_fixture(
        self, session_fixture: session_support.OrdinaryAgentSessionStorageTests
    ) -> None:
        self.session_fixture = session_fixture
        self.landing = landing_support.OrdinaryAgentLandingStorageTests()
        self.landing.prepare_landing_fixture(session_fixture)
        self.store = self.landing.store
        self.landing.candidate = self.landing.candidate.model_copy(update={"status": "superseded"})
        with self.store._session_factory() as session:
            row = session.get(
                LaunchplaneMergeTrainBatchCandidateRow,
                self.landing.candidate.record_id,
            )
            assert row is not None
            row.status = "superseded"
            row.payload = self.landing.candidate.model_dump(mode="json")
            session.commit()

    def _issue_and_observe(
        self, preparation: OrdinaryAgentLandingPreparation
    ) -> tuple[OrdinaryAgentLandingPreparation, MergeAdmissionProposal]:
        candidate = preparation.candidate
        now = self.session_fixture.now
        self.store.acquire_ordinary_agent_custody_issue_attempt(
            attempt_id=preparation.custody_attempt_id,
            idempotency_key_sha256=hashlib.sha256(preparation.idempotency_key.encode()).hexdigest(),
            request_sha256=canonical_json_sha256(
                {
                    "candidate": candidate.model_dump(mode="json"),
                    "request": preparation.request_payload,
                }
            ),
            candidate=candidate,
            requested_permissions=ordinary_agent_effect_permissions(candidate.effect_profile),
            dispatch_window_seconds=30,
        )
        self.store.mark_ordinary_agent_custody_issued(
            attempt_id=preparation.custody_attempt_id,
            app_id=candidate.expected_app_id,
            installation_id=77,
            token_expires_at=datetime.fromtimestamp(now + 300, timezone.utc).isoformat(),
            residual_expires_at=datetime.fromtimestamp(now + 360, timezone.utc).isoformat(),
        )
        stamped = self.store.read_ordinary_landing_preparation(
            preparation_id=preparation.preparation_id
        )
        observed = self.store.record_ordinary_landing_evidence(
            preparation_id=stamped.preparation_id,
            expected_revision=stamped.revision,
            controller_fence=self.landing.fence,
            evidence=self.landing.evidence(stamped),
        )
        proposal = self.landing.guard(observed).build_proposal(
            entry=observed.entry,
            observed_base_sha=observed.expected_base_sha,
            observed_base_tree_sha=observed.expected_base_tree_sha,
            observed_head_sha=observed.entry.expected_head_sha,
            observed_head_tree_sha=observed.entry.expected_head_tree_sha,
        )
        return observed, proposal

    def _finalize(
        self, preparation: OrdinaryAgentLandingPreparation
    ) -> OrdinaryAgentLandingFinalization:
        observed, proposal = self._issue_and_observe(preparation)
        return self.store.finalize_ordinary_landing_preparation(
            preparation_id=observed.preparation_id,
            expected_revision=observed.revision,
            controller_fence=self.landing.fence,
            proposal=proposal,
            custody_attempt_id=observed.custody_attempt_id,
        )

    def _record_deadline_and_reacquire(
        self, finalization: OrdinaryAgentLandingFinalization
    ) -> OrdinaryAgentControllerFence:
        self.store.record_ordinary_semantic_outcome(
            child_id=finalization.child.child_id,
            typed_outcome=OrdinaryAgentKnownNotDispatchedOutcome(
                reason="provider_attempt_deadline"
            ),
        )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=finalization.preparation.custody_attempt_id,
            reason="confirmed_revoked",
        )
        return self._yield_and_reacquire()

    def _yield_and_reacquire(self) -> OrdinaryAgentControllerFence:
        old_fence = self.landing.fence
        self.store.yield_ordinary_merge_train_controller_state_record(
            request_id=self.landing.request.request_id,
            expected_binding_revision=1,
            controller_fence=old_fence,
        )
        self.session_fixture.now += 1
        self.session_fixture.clock.return_value = datetime.fromtimestamp(
            self.session_fixture.now, timezone.utc
        ).isoformat()
        controller = self.store.acquire_ordinary_merge_train_controller_state_record(
            claim_fence=self.landing.claimed.claim_fence,
            expected_binding_revision=1,
            policy_key=self.landing.fixture.merge_policy.policy.policies[0].policy_key,
            policy_sha256=self.landing.fixture.merge_policy.policy_sha256,
            lease_seconds=300,
            initial_active_action="land_batch",
            initial_active_phase="merge_batch_entries",
            adoptable_active_actions=("land_batch",),
        )
        self.landing.fence = OrdinaryAgentControllerFence(
            controller_key=controller.controller_key,
            lease_owner=controller.lease_owner,
            lease_acquired_at=controller.lease_acquired_at,
        )
        return old_fence

    def _reserve_retry(self, predecessor_id: str) -> OrdinaryAgentLandingReservation:
        return self.store.reserve_ordinary_landing_retry_preparation(
            request_id=self.landing.request.request_id,
            expected_binding_revision=1,
            controller_fence=self.landing.fence,
            predecessor_preparation_id=predecessor_id,
        )

    def _actions_used(self) -> int:
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneOrdinaryAgentLeaseRow, self.landing.request.lease_id)
            assert row is not None
            return OrdinaryAgentLeaseRecord.model_validate(row.payload).budget.actions_used

    def test_consumed_retry_renews_dispatch_binding_without_recharging(self) -> None:
        first = self._finalize(self.landing.reserve().preparation)
        charged = self._actions_used()
        old_fence = self._record_deadline_and_reacquire(first)
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "controller_binding_conflict"
        ):
            self.store.reserve_ordinary_landing_retry_preparation(
                request_id=self.landing.request.request_id,
                expected_binding_revision=1,
                controller_fence=old_fence,
                predecessor_preparation_id=first.preparation.preparation_id,
            )
        second_reservation = self._reserve_retry(first.preparation.preparation_id)
        self.assertEqual(second_reservation.disposition, "created")
        second_preparation = second_reservation.preparation
        self.assertEqual(
            self._reserve_retry(first.preparation.preparation_id).disposition, "replay"
        )
        self.assertEqual(
            self.store.resolve_latest_ordinary_landing_preparation(
                root_preparation_id=first.preparation.preparation_id
            ),
            second_preparation,
        )
        second = self._finalize(second_preparation)
        self.assertEqual(second.effect.effect_id, first.effect.effect_id)
        self.assertEqual(second.effect.command_sha256, first.effect.command_sha256)
        self.assertEqual((second.effect.dispatch_count, second.child.semantic_ordinal), (2, 2))
        self.assertEqual(second.child.admission_id, second.admission.admission_id)
        self.assertEqual(
            second.child.admission_binding_sha256,
            second.admission.admission_binding_sha256,
        )
        self.assertEqual(self._actions_used(), charged)
        with self.store._session_factory() as session:
            request_row = session.get(
                LaunchplaneOrdinaryAgentFiniteRequestRow,
                self.landing.request.request_id,
            )
            assert request_row is not None
            request = OrdinaryAgentFiniteRequestRecord.model_validate(request_row.payload)
        self.assertEqual(request.execution_record_ids.count(first.effect.effect_id), 1)
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied,
            "landing_dispatch_requires_joined_finalization",
        ):
            self.store.reserve_ordinary_custody_attempt(
                effect_id=first.effect.effect_id,
                expected_effect_revision=second.effect.revision,
            )
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied,
            "landing_dispatch_requires_joined_finalization",
        ):
            self.store.checkpoint_ordinary_semantic_dispatch(
                effect_id=first.effect.effect_id,
                controller_fence=self.landing.fence,
                custody_attempt_id="custody_forbidden",
                fixed_token_expires_at=self.session_fixture.now + 300,
            )

    def test_legacy_first_child_can_retry_but_fabricated_projection_cannot(self) -> None:
        first = self._finalize(self.landing.reserve().preparation)
        with self.store._session_factory() as session:
            child_row = session.get(
                LaunchplaneOrdinaryAgentSemanticDispatchRow, first.child.child_id
            )
            binding_row = session.get(
                LaunchplaneOrdinaryAgentLandingBindingRow,
                first.preparation.preparation_id,
            )
            assert child_row is not None and binding_row is not None
            child_payload = dict(child_row.payload)
            child_payload.pop("admission_id")
            child_payload.pop("admission_binding_sha256")
            child_row.payload = child_payload
            binding_payload = dict(binding_row.payload)
            binding_payload["child"] = child_payload
            binding_row.payload = binding_payload
            session.commit()
        self._record_deadline_and_reacquire(first)
        projected = self.store.list_merge_landing_outcome_records(
            admission_id=first.admission.admission_id
        )[0]
        self.assertEqual(projected.reason, "dispatch_not_attempted")
        fabricated = MergeLandingOutcomeRecord.model_validate(
            {
                **projected.model_dump(mode="json"),
                "outcome_id": "",
                "outcome_binding_sha256": "",
                "source": "fabricated-replay",
            }
        )
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied,
            "dispatch_not_attempted_requires_joined_history",
        ):
            self.store.create_merge_landing_outcome_record_if_absent(fabricated)
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied,
            "dispatch_not_attempted_requires_joined_history",
        ):
            self.store.create_ordinary_merge_landing_outcome_record_if_absent(
                request_id=self.landing.request.request_id,
                expected_binding_revision=1,
                record=fabricated,
            )
        retry = self._reserve_retry(first.preparation.preparation_id).preparation
        second = self._finalize(retry)
        self.assertEqual(second.child.semantic_ordinal, 2)
        self.assertEqual(second.child.admission_id, second.admission.admission_id)
        replayed, created = self.store.create_ordinary_merge_landing_outcome_record_if_absent(
            request_id=self.landing.request.request_id,
            expected_binding_revision=1,
            record=projected,
        )
        self.assertFalse(created)
        self.assertEqual(replayed, projected)

    def test_retry_attempt_cap_is_finite_and_preserves_one_action(self) -> None:
        current = self._finalize(self.landing.reserve().preparation)
        root_id = current.preparation.preparation_id
        charged = self._actions_used()
        while current.preparation.attempt_ordinal < MAX_ORDINARY_LANDING_ATTEMPTS_PER_ENTRY:
            self._record_deadline_and_reacquire(current)
            current = self._finalize(
                self._reserve_retry(current.preparation.preparation_id).preparation
            )
        self._record_deadline_and_reacquire(current)
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "landing_retry_attempts_exhausted"
        ):
            self._reserve_retry(current.preparation.preparation_id)
        latest = self.store.resolve_latest_ordinary_landing_preparation(root_preparation_id=root_id)
        self.assertEqual(latest.attempt_ordinal, MAX_ORDINARY_LANDING_ATTEMPTS_PER_ENTRY)
        self.assertEqual(self._actions_used(), charged)

    def test_terminal_attempt_after_consumed_deadline_reuses_lineage_effect(self) -> None:
        first = self._finalize(self.landing.reserve().preparation)
        charged = self._actions_used()
        self._record_deadline_and_reacquire(first)
        second = self._reserve_retry(first.preparation.preparation_id).preparation
        observed_second, _ = self._issue_and_observe(second)
        terminal_second = self.store.close_ordinary_landing_preparation(
            preparation_id=observed_second.preparation_id,
            expected_revision=observed_second.revision,
            reason_code="provider_wait",
        )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=terminal_second.custody_attempt_id,
            reason="confirmed_revoked",
        )
        self._yield_and_reacquire()

        third = self._reserve_retry(terminal_second.preparation_id).preparation
        finalized = self._finalize(third)
        self.assertEqual(third.attempt_ordinal, 3)
        self.assertEqual(finalized.child.semantic_ordinal, 2)
        self.assertEqual(finalized.effect.effect_id, first.effect.effect_id)
        self.assertEqual(finalized.effect.command_sha256, first.effect.command_sha256)
        self.assertEqual(finalized.effect.action_ordinal, first.effect.action_ordinal)
        self.assertEqual(self._actions_used(), charged)
        self.assertEqual(
            self.store.read_ordinary_landing_preparation(
                preparation_id=terminal_second.preparation_id
            ).state,
            "superseded",
        )

    def test_ambiguous_or_reconciled_history_cannot_reserve_retry(self) -> None:
        for history_kind in ("unknown", "reconciled"):
            with self.subTest(history_kind=history_kind):
                if history_kind != "unknown":
                    session_fixture = session_support.OrdinaryAgentSessionStorageTests()
                    session_fixture.setUp()
                    self.addCleanup(session_fixture.doCleanups)
                    self.prepare_retry_fixture(session_fixture)
                first = self._finalize(self.landing.reserve().preparation)
                if history_kind == "unknown":
                    self.store.record_ordinary_semantic_outcome(
                        child_id=first.child.child_id,
                        typed_outcome=OrdinaryAgentUnknownOutcome(reason="response_ambiguous"),
                    )
                else:
                    self.store.record_ordinary_semantic_outcome(
                        child_id=first.child.child_id,
                        typed_outcome=OrdinaryAgentKnownNotDispatchedOutcome(
                            reason="provider_attempt_deadline"
                        ),
                    )
                    observation = OrdinaryAgentReconciliationObservation(
                        observation_id="landing-reconciliation-no-effect",
                        custody_attempt_id=first.child.custody_attempt_id,
                        observed_at=self.session_fixture.now,
                        observation=OrdinaryAgentIncompleteReadObservation(
                            repository=self.landing.request.target.repository,
                            reason="provider_incomplete",
                        ),
                    )
                    with self.store._session_factory() as session:
                        session.add(
                            LaunchplaneOrdinaryAgentEffectReconciliationRow(
                                observation_id=observation.observation_id,
                                child_id=first.child.child_id,
                                payload=observation.model_dump(mode="json"),
                            )
                        )
                        session.commit()
                self.store.close_ordinary_agent_custody_issue_attempt(
                    attempt_id=first.preparation.custody_attempt_id,
                    reason="confirmed_revoked",
                )
                self.store.yield_ordinary_merge_train_controller_state_record(
                    request_id=self.landing.request.request_id,
                    expected_binding_revision=1,
                    controller_fence=self.landing.fence,
                )
                self.session_fixture.now += 1
                self.session_fixture.clock.return_value = datetime.fromtimestamp(
                    self.session_fixture.now, timezone.utc
                ).isoformat()
                controller = self.store.acquire_ordinary_merge_train_controller_state_record(
                    claim_fence=self.landing.claimed.claim_fence,
                    expected_binding_revision=1,
                    policy_key=self.landing.fixture.merge_policy.policy.policies[0].policy_key,
                    policy_sha256=self.landing.fixture.merge_policy.policy_sha256,
                    lease_seconds=300,
                    initial_active_action="land_batch",
                    initial_active_phase="merge_batch_entries",
                    adoptable_active_actions=("land_batch",),
                )
                self.landing.fence = OrdinaryAgentControllerFence(
                    controller_key=controller.controller_key,
                    lease_owner=controller.lease_owner,
                    lease_acquired_at=controller.lease_acquired_at,
                )
                with self.assertRaisesRegex(
                    OrdinaryAgentSessionAdmissionDenied, "landing_retry_not_proven"
                ):
                    self._reserve_retry(first.preparation.preparation_id)

    def test_current_plan_change_blocks_retry_without_creating_a_successor(self) -> None:
        first = self._finalize(self.landing.reserve().preparation)
        self._record_deadline_and_reacquire(first)
        with self.store._session_factory() as session:
            plan_row = session.get(
                LaunchplaneMergeTrainBatchLandingPlanRow,
                self.landing.plan.record_id,
            )
            assert plan_row is not None
            plan_row.status = "superseded"
            plan_row.payload = self.landing.plan.model_copy(
                update={"status": "superseded"}
            ).model_dump(mode="json")
            session.commit()
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "landing_plan_not_dispatchable"
        ):
            self._reserve_retry(first.preparation.preparation_id)
        with self.store._session_factory() as session:
            preparation_ids = tuple(
                session.scalars(
                    select(LaunchplaneOrdinaryAgentLandingPreparationRow.preparation_id)
                ).all()
            )
        self.assertEqual(preparation_ids, (first.preparation.preparation_id,))

    def test_superseded_terminal_ancestor_cannot_be_reopened(self) -> None:
        observed, _ = self._issue_and_observe(self.landing.reserve().preparation)
        terminal = self.store.close_ordinary_landing_preparation(
            preparation_id=observed.preparation_id,
            expected_revision=observed.revision,
            reason_code="evidence_denied",
        )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=terminal.custody_attempt_id,
            reason="confirmed_revoked",
        )
        self._yield_and_reacquire()
        self._reserve_retry(terminal.preparation_id)
        superseded = self.store.read_ordinary_landing_preparation(
            preparation_id=terminal.preparation_id
        )
        self.assertEqual(superseded.state, "superseded")
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "landing_already_finalized"
        ):
            self.store.close_ordinary_landing_preparation(
                preparation_id=superseded.preparation_id,
                expected_revision=superseded.revision,
                reason_code="provider_wait",
            )


if __name__ == "__main__":
    unittest.main()
