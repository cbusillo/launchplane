"""A proven pre-send deferral gets fresh admission without duplicating the action."""

from datetime import datetime, timezone
import unittest
from unittest.mock import patch

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentLeaseRecord,
    OrdinaryAgentFiniteRequestRecord,
)
from control_plane.ordinary_agent_github_transport import OrdinaryAgentProviderDeferred
from control_plane.ordinary_agent_landing_recovery import (
    OrdinaryLandingRetryRequired,
    recover_ordinary_landing_entry,
)
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.ordinary_agent_merge_train_job import _expected_exception_disposition
from control_plane.storage.postgres import (
    LaunchplaneOrdinaryAgentLeaseRow,
    LaunchplaneOrdinaryAgentFiniteRequestRow,
    LaunchplaneMergeTrainBatchCandidateRow,
)
from tests import test_ordinary_agent_landing_execution as support


class OrdinaryLandingRetryExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = support.OrdinaryAgentLandingExecutionTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        # The focused landing fixture seeds both artifacts. Real plan creation
        # supersedes its candidate; reproduce that historical state before
        # exercising release/reacquisition of the single active plan.
        landing = self.fixture.fixture
        landing.candidate = landing.candidate.model_copy(update={"status": "superseded"})
        with self.fixture.store._session_factory() as session:
            row = session.get(LaunchplaneMergeTrainBatchCandidateRow, landing.candidate.record_id)
            assert row is not None
            row.status = "superseded"
            row.payload = landing.candidate.model_dump(mode="json")
            session.commit()

    def recover(self, preparation_id: str) -> object:
        fixture = self.fixture
        return recover_ordinary_landing_entry(
            store=fixture.store,
            request=fixture.fixture.request,
            preparation_id=preparation_id,
            candidate_record=fixture.fixture.candidate,
            landing_plan_record=fixture.fixture.plan,
            guard_factory=fixture.guard,
            checkpoint=fixture.checkpoints.append,
        )

    def reacquire(self) -> None:
        fixture = self.fixture
        landing = fixture.fixture
        fixture.store.yield_ordinary_merge_train_controller_state_record(
            request_id=landing.request.request_id,
            expected_binding_revision=1,
            controller_fence=landing.fence,
        )
        fixture.elapsed += 1
        landing.fixture.fixture.now = int(fixture.now + fixture.elapsed)
        landing.fixture.fixture.clock.return_value = datetime.fromtimestamp(
            fixture.now + fixture.elapsed, timezone.utc
        ).isoformat()
        controller = fixture.store.acquire_ordinary_merge_train_controller_state_record(
            claim_fence=landing.claimed.claim_fence,
            expected_binding_revision=1,
            policy_key=landing.fixture.merge_policy.policy.policies[0].policy_key,
            policy_sha256=landing.fixture.merge_policy.policy_sha256,
            lease_seconds=300,
            initial_active_action="land_batch",
            initial_active_phase="merge_batch_entries",
            adoptable_active_actions=("land_batch",),
        )
        landing.fence = effects.OrdinaryAgentControllerFence(
            controller_key=controller.controller_key,
            lease_owner=controller.lease_owner,
            lease_acquired_at=controller.lease_acquired_at,
        )

    def actions_used(self) -> int:
        with self.fixture.store._session_factory() as session:
            row = session.get(
                LaunchplaneOrdinaryAgentLeaseRow, self.fixture.fixture.request.lease_id
            )
            assert row is not None
            return OrdinaryAgentLeaseRecord.model_validate(row.payload).budget.actions_used

    def expire_landing(self, predecessor_preparation_id: str | None = None) -> None:
        fixture = self.fixture
        finalize = fixture.store.finalize_ordinary_landing_preparation

        def expire_after_finalization(**kwargs: object) -> effects.OrdinaryAgentLandingFinalization:
            result = finalize(**kwargs)  # type: ignore[arg-type]
            fixture.elapsed += 51
            fixture.fixture.fixture.fixture.clock.return_value = datetime.fromtimestamp(
                fixture.now + fixture.elapsed, timezone.utc
            ).isoformat()
            return result

        with patch.object(
            fixture.store,
            "finalize_ordinary_landing_preparation",
            side_effect=expire_after_finalization,
        ):
            with self.assertRaises(OrdinaryAgentProviderDeferred):
                fixture.run_landing(predecessor_preparation_id)

    def test_pre_send_deadline_retries_once_with_new_admission_and_replays_latest_history(
        self,
    ) -> None:
        fixture = self.fixture
        self.expire_landing()
        assert fixture.preparation is not None
        first = fixture.store.read_ordinary_landing_finalization(
            preparation_id=fixture.preparation.preparation_id
        )
        assert first is not None
        self.assertEqual(fixture.inner.requests, [])
        self.assertEqual(
            fixture.store.list_merge_landing_outcome_records(
                admission_id=first.admission.admission_id
            )[0].reason,
            "dispatch_not_attempted",
        )
        charged = self.actions_used()
        self.reacquire()
        with self.assertRaises(OrdinaryLandingRetryRequired) as retry:
            self.recover(first.preparation.preparation_id)
        result = fixture.run_landing(retry.exception.predecessor_preparation_id)
        assert fixture.preparation is not None
        second = fixture.store.read_ordinary_landing_finalization(
            preparation_id=fixture.preparation.preparation_id
        )
        assert second is not None
        self.assertNotEqual(second.admission.admission_id, first.admission.admission_id)
        self.assertNotEqual(second.child.custody_attempt_id, first.child.custody_attempt_id)
        self.assertNotEqual(second.child.controller_fence, first.child.controller_fence)
        self.assertEqual(second.effect.effect_id, first.effect.effect_id)
        self.assertEqual(second.effect.command_sha256, first.effect.command_sha256)
        self.assertEqual(second.child.admission_id, second.admission.admission_id)
        self.assertEqual(
            (second.effect.dispatch_count, second.effect.dispatch_custody_count), (2, 2)
        )
        self.assertEqual((second.effect.state, second.effect.reason_code), ("dispatching", None))
        self.assertEqual(self.actions_used(), charged)
        self.assertEqual([item.method for item in fixture.inner.requests], ["PUT", "POST"])
        self.assertEqual(result.status, "merged")
        before = (fixture.mints, len(fixture.inner.requests))
        self.recover(first.preparation.preparation_id)
        self.recover(first.preparation.preparation_id)
        self.assertEqual((fixture.mints, len(fixture.inner.requests)), before)
        with fixture.store._session_factory() as session:
            row = session.get(
                LaunchplaneOrdinaryAgentFiniteRequestRow, fixture.fixture.request.request_id
            )
            assert row is not None
            request = OrdinaryAgentFiniteRequestRecord.model_validate(row.payload)
        self.assertEqual(request.execution_record_ids.count(first.effect.effect_id), 1)
        self.assertEqual(
            fixture.store.list_merge_landing_outcome_records(
                admission_id=second.admission.admission_id
            )[0].status,
            "landed",
        )

    def test_terminal_preparation_retries_with_original_action_and_no_terminal_ancestor(
        self,
    ) -> None:
        fixture = self.fixture
        fixture.read_error = OrdinaryAgentProviderDeferred()
        with self.assertRaises(OrdinaryAgentProviderDeferred):
            fixture.run_landing()
        assert fixture.preparation is not None
        first_id = fixture.preparation.preparation_id
        charged = self.actions_used()
        fixture.read_error = None
        self.reacquire()
        with self.assertRaises(OrdinaryLandingRetryRequired):
            self.recover(first_id)
        fixture.run_landing(first_id)
        self.assertEqual(self.actions_used(), charged)
        self.assertEqual(
            fixture.store.read_ordinary_landing_preparation(preparation_id=first_id).state,
            "superseded",
        )
        self.assertEqual([item.method for item in fixture.inner.requests], ["PUT", "POST"])
        snapshot = fixture.store.read_ordinary_agent_job_recovery_snapshot(
            claim_fence=fixture.fixture.claimed.claim_fence
        )
        self.assertIsNone(snapshot.open_landing_preparation)

    def test_consumed_terminal_consumed_lineage_exhausts_preparations_without_recharging(
        self,
    ) -> None:
        fixture = self.fixture
        self.expire_landing()
        assert fixture.preparation is not None
        root_id = fixture.preparation.preparation_id
        first = fixture.store.read_ordinary_landing_finalization(preparation_id=root_id)
        assert first is not None
        charged = self.actions_used()

        self.reacquire()
        fixture.read_error = OrdinaryAgentProviderDeferred()
        with self.assertRaises(OrdinaryAgentProviderDeferred):
            fixture.run_landing(root_id)
        second = fixture.store.resolve_latest_ordinary_landing_preparation(
            root_preparation_id=root_id
        )
        self.assertEqual((second.state, second.attempt_ordinal), ("terminal", 2))
        self.assertIsNone(second.effect_id)
        fixture.read_error = None

        self.reacquire()
        with self.assertRaises(OrdinaryLandingRetryRequired) as retry:
            self.recover(root_id)
        self.expire_landing(retry.exception.predecessor_preparation_id)
        latest = fixture.store.resolve_latest_ordinary_landing_preparation(
            root_preparation_id=root_id
        )
        third = fixture.store.read_ordinary_landing_finalization(
            preparation_id=latest.preparation_id
        )
        assert third is not None
        self.assertEqual((latest.state, latest.attempt_ordinal), ("consumed", 3))
        self.assertEqual(third.effect.effect_id, first.effect.effect_id)
        self.assertEqual(third.effect.command_sha256, first.effect.command_sha256)
        self.assertEqual(third.child.semantic_ordinal, 2)
        self.assertEqual(self.actions_used(), charged)
        self.assertEqual(fixture.inner.requests, [])

        self.reacquire()
        before = (fixture.mints, len(fixture.inner.requests))
        with self.assertRaises(OrdinaryAgentSessionAdmissionDenied) as denied:
            self.recover(root_id)
        self.assertEqual(denied.exception.reason_code, "effect_attempts_exhausted")
        snapshot = fixture.store.read_ordinary_agent_job_recovery_snapshot(
            claim_fence=fixture.fixture.claimed.claim_fence
        )
        disposition = _expected_exception_disposition(error=denied.exception, snapshot=snapshot)
        assert disposition is not None
        self.assertEqual(
            (disposition.status, disposition.reason_code), ("blocked", "effect_budget_exhausted")
        )
        self.assertEqual((fixture.mints, len(fixture.inner.requests)), before)
