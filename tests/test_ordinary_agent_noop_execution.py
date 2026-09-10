"""The ordinary no-op uses the existing core checkpoint and one joined write."""

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any
from unittest.mock import Mock, patch
import unittest

from sqlalchemy import func, select

from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingEntry
from control_plane.contracts.merge_train_effect import MergeTrainSemanticEffectExecutor
from control_plane.contracts.ordinary_agent_effect import OrdinaryAgentLandingPreparation
from control_plane.contracts.ordinary_agent_snapshot import OrdinaryAgentLandingEvidence
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.merge_admission import GuardedMergeAdmission
from control_plane.merge_train_controller_run_once import (
    MergeTrainControllerLeaseContext,
    MergeTrainControllerRunOnceEnvelope,
    _advance_active_landing_record,
)
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.ordinary_agent_admission_store import OrdinaryAgentAdmissionAdapter
from control_plane.ordinary_agent_controller_store import (
    OrdinaryAgentControllerAdapter,
    OrdinaryAgentProgressAdapter,
)
from control_plane.ordinary_agent_landing_execution import execute_fresh_ordinary_landing
from control_plane.ordinary_agent_landing_recovery import (
    OrdinaryLandingProgressReloadRequired,
    recover_ordinary_landing_entry,
)
from control_plane.ordinary_agent_merge_train_client import OrdinaryAgentMergeTrainClient
from control_plane.ordinary_agent_noop_route import OrdinaryNoOpLandingRoute
from control_plane.ordinary_agent_merge_train_job import _expected_exception_disposition
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.storage.postgres import (
    LaunchplaneOrdinaryAgentSemanticDispatchRow,
    LaunchplaneOrdinaryAgentNoOpLandingFinalizationRow,
)
from tests.test_ordinary_agent_noop_storage import NoOpLandingStorageFixture
from tests import test_ordinary_agent_session_storage as session_support


class OrdinaryAgentNoOpExecutionTests(unittest.TestCase):
    def test_core_checkpoint_commits_no_op_once_after_lease_renewal(self) -> None:
        self._exercise_no_op(checkpoint_delay=2)

    def test_expired_provider_window_waits_and_closes_preparation_without_a_merge(self) -> None:
        self._exercise_no_op(checkpoint_delay=46)

    def _exercise_no_op(self, *, checkpoint_delay: int) -> None:
        session_fixture = session_support.OrdinaryAgentSessionStorageTests()
        session_fixture.setUp(installation_id=77)
        self.addCleanup(session_fixture.doCleanups)
        fixture = NoOpLandingStorageFixture(self, session_fixture)
        landing, store = fixture.landing, fixture.store
        controller = OrdinaryAgentControllerAdapter(
            claimed=landing.claimed, store=store, reader=store
        )
        # The fixture has already acquired this exact fence through the joined
        # store. This test starts at the active-landing phase, not acquisition.
        controller._acquired_fence = landing.fence
        route = OrdinaryNoOpLandingRoute(store=store)
        progress = OrdinaryAgentProgressAdapter(controller, store, no_op_route=route)
        admissions = OrdinaryAgentAdmissionAdapter(controller, progress, store)
        current = controller.list_merge_train_controller_state_records(
            repository=landing.request.target.repository,
            base_branch=landing.request.target.base_branch,
            limit=1,
        )[0]
        lease = MergeTrainControllerLeaseContext(
            record=current, record_store=controller, lease_seconds=90
        )
        policy_record = landing.fixture.merge_policy
        policy = policy_record.policy.find_repository_policy(
            repository=landing.request.target.repository,
            base_branch=landing.request.target.base_branch,
        )
        transport = RecordingMergeTrainGitHubTransport()
        provider = Mock(return_value=None)
        mint = Mock(
            return_value=GitHubAppInstallationToken(
                token="test-no-op-token",
                app_id=session_fixture.envelope.custody.github_app_id,
                installation_id=77,
                repository_id=landing.request.target.repository_id,
                repository=landing.request.target.repository,
                expires_at=datetime.fromtimestamp(
                    session_fixture.now + 300, timezone.utc
                ).isoformat(),
            )
        )
        prepared: list[OrdinaryAgentLandingPreparation] = []
        evaluation_phases: list[str] = []
        before_checkpoint_expiry: list[str] = []

        def guard_factory(
            preparation: OrdinaryAgentLandingPreparation, evidence: OrdinaryAgentLandingEvidence
        ) -> GuardedMergeAdmission:
            prepared.append(preparation)
            guard = fixture._guard(preparation)
            guard.record_store = admissions
            guard.controller_state_provider = lease.read_current
            guard.admission_time_provider = lambda: datetime.fromtimestamp(
                session_fixture.now, timezone.utc
            ).isoformat()
            before_checkpoint_expiry.append(lease.read_current().lease_expires_at)
            session_fixture.now += checkpoint_delay
            session_fixture.clock.return_value = datetime.fromtimestamp(
                session_fixture.now, timezone.utc
            ).isoformat()

            def evaluate(**kwargs: Any) -> Any:
                evaluation_phases.append(lease.read_current().active_phase)
                # Recompute fixture readiness at the current persisted fence,
                # just as the live evaluator does after checkpoint renewal.
                return fixture._guard(preparation).evaluator.evaluate(**kwargs)

            guard.evaluator = Mock(evaluate=evaluate)
            return guard

        def step(**kwargs: Any) -> MergeTrainBatchLandingEntry:
            return execute_fresh_ordinary_landing(
                store=store,
                request_id=landing.request.request_id,
                binding_revision=landing.request.binding_revision,
                controller_fence=controller.acquired_fence,
                pull_request_number=kwargs["entry"].pull_request_number,
                semantic_ordinal=kwargs["semantic_ordinal"],
                candidate_record=kwargs["candidate_record"],
                landing_plan_record=kwargs["landing_plan_record"],
                repository_owner_id=202,
                repository_policy=policy,
                guard_factory=guard_factory,
                checkpoint=kwargs["checkpoint"],
                no_op_route=route,
                api_request=provider,
                transport_factory=lambda token: transport,
                utc_now=lambda: datetime.fromtimestamp(session_fixture.now, timezone.utc),
            )

        client = OrdinaryAgentMergeTrainClient(
            request=landing.request,
            effect_executor=Mock(spec=MergeTrainSemanticEffectExecutor),
            snapshot=Mock(),
            candidate_check=Mock(),
            advance_landing_entry=step,
            advance_no_op_landing_entry=step,
        )
        with store._session_factory() as session:
            dispatches_before = session.scalar(
                select(func.count()).select_from(LaunchplaneOrdinaryAgentSemanticDispatchRow)
            )

        def advance() -> dict[str, object]:
            return _advance_active_landing_record(
                request=MergeTrainControllerRunOnceEnvelope(
                    repository=landing.request.target.repository,
                    base_branch=landing.request.target.base_branch,
                    mutate=True,
                ),
                policy_sha256=policy_record.policy_sha256,
                repository_policy=policy,
                trace_id="no-op-checkpoint",
                recorded_at=datetime.fromtimestamp(session_fixture.now, timezone.utc).isoformat(),
                github_client=client,
                candidate_store=progress,
                landing_store=progress,
                stack_collapse_store=progress,
                admission_store=admissions,
                admission_evaluator=Mock(),
                active_landing_record=landing.plan,
                lease=lease,
            )

        with (
            patch(
                "control_plane.ordinary_agent_custody.resolve_ordinary_agent_github_app_identity",
                side_effect=lambda **kw: SimpleNamespace(
                    identity=SimpleNamespace(app_id=kw["candidate"].expected_app_id)
                ),
            ),
            patch(
                "control_plane.ordinary_agent_custody.mint_ordinary_agent_installation_token", mint
            ),
            patch(
                "control_plane.ordinary_agent_landing_execution.read_ordinary_agent_landing_evidence",
                side_effect=lambda **kw: landing.evidence(kw["preparation"]),
            ),
        ):
            result: dict[str, object] = {}
            if checkpoint_delay == 46:
                with self.assertRaises(OrdinaryAgentSessionAdmissionDenied) as denied:
                    advance()
            else:
                result = advance()
        self.assertEqual(evaluation_phases, ["landing_entry_merged"])
        mint.assert_called_once()
        self.assertEqual(transport.requests, [])
        self.assertFalse(route.armed)
        finalization = store.read_ordinary_no_op_landing_finalization(
            preparation_id=prepared[0].preparation_id
        )
        if checkpoint_delay == 46:
            self.assertEqual(denied.exception.reason_code, "provider_attempt_deadline")
            snapshot = store.read_ordinary_agent_job_recovery_snapshot(
                claim_fence=landing.claimed.claim_fence
            )
            disposition = _expected_exception_disposition(error=denied.exception, snapshot=snapshot)
            assert disposition is not None
            self.assertEqual(disposition.status, "waiting")
            assert disposition.next_due_at is not None
            self.assertGreater(disposition.next_due_at, session_fixture.now)
            self.assertIsNone(finalization)
            terminal = store.read_ordinary_landing_preparation(
                preparation_id=prepared[0].preparation_id
            )
            self.assertEqual(
                (terminal.state, terminal.reason_code), ("terminal", "provider_attempt_deadline")
            )
            with store._session_factory() as session:
                self.assertEqual(
                    session.scalar(
                        select(func.count()).select_from(
                            LaunchplaneOrdinaryAgentSemanticDispatchRow
                        )
                    ),
                    dispatches_before,
                )
            return
        self.assertEqual(result["landing_progress"], "cleanup_pending")
        assert finalization is not None
        self.assertEqual(
            result["merge_train_batch_landing_plan_record_id"], finalization.successor.record_id
        )
        self.assertEqual(finalization.successor.landing_plan.entries[0].status, "skipped")
        self.assertGreater(lease.read_current().lease_expires_at, before_checkpoint_expiry[0])
        with store._session_factory() as session:
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(LaunchplaneOrdinaryAgentSemanticDispatchRow)
                ),
                dispatches_before,
            )
            self.assertEqual(
                session.scalar(
                    select(func.count()).select_from(
                        LaunchplaneOrdinaryAgentNoOpLandingFinalizationRow
                    )
                ),
                1,
            )
        checkpoint = Mock(
            side_effect=AssertionError("already committed progress must not be rewritten")
        )
        with self.assertRaises(OrdinaryLandingProgressReloadRequired):
            recover_ordinary_landing_entry(
                store=store,
                request=landing.request,
                preparation_id=prepared[0].preparation_id,
                candidate_record=landing.candidate,
                landing_plan_record=landing.plan,
                guard_factory=guard_factory,
                checkpoint=checkpoint,
            )
        checkpoint.assert_not_called()
        mint.assert_called_once()
