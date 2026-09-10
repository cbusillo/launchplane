from __future__ import annotations

from datetime import datetime, timezone
import unittest
from unittest.mock import Mock, patch
from typing import cast

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeEffect,
    CandidateRefPrepareEffect,
    MergeTrainEffectLineage,
)
from control_plane.contracts.merge_train_batch import (
    build_merge_train_batch_landing_plan,
    build_ordinary_merge_train_candidate_ref,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerStateRecord,
)
from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentCommitIdentity,
    OrdinaryAgentMergeTrainSnapshotResult,
    OrdinaryAgentProtectionEvidence,
    OrdinaryAgentProviderRequestCounts,
    OrdinaryAgentPullRequestHeadIdentity,
    OrdinaryAgentRequiredCheck,
)
from control_plane.merge_admission import MergeAdmissionDeniedError
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.merge_train_controller_run_once import (
    MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
    MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
    MergeTrainControllerRunOnceResult,
)
from control_plane.ordinary_agent_github_transport import OrdinaryAgentProviderDeferred
from control_plane.ordinary_agent_controller_store import OrdinaryAgentControllerAdapter
from control_plane.ordinary_agent_merge_train_job import (
    _EvidenceBoundOrdinaryAdmissionEvaluator,
    _expected_exception_disposition,
    _result_disposition,
    advance_ordinary_agent_merge_train_job,
)
from control_plane.storage.postgres import (
    LaunchplaneMergeTrainControllerStateRow,
    LaunchplaneOrdinaryAgentJobClaimRow,
)
from tests import test_ordinary_agent_effect_storage as effect_support
from tests import test_ordinary_agent_landing_execution as landing_execution_support
from tests import test_ordinary_agent_landing_storage as landing_support
from tests import test_ordinary_agent_session_storage as session_support
from tests.test_merge_admission_records import _guard_records


class OrdinaryAgentMergeTrainJobTests(unittest.TestCase):
    def setUp(self) -> None:
        self.session = session_support.OrdinaryAgentSessionStorageTests()
        self.session.setUp(installation_id=77)
        self.addCleanup(self.session.doCleanups)
        self.fixture = effect_support.OrdinaryAgentEffectStorageTests()
        self.fixture.prepare_effect_fixture(self.session)
        self.store = self.fixture.store
        self.request = self.fixture.request
        self.policy = self.fixture.merge_policy
        self.provider = Mock(side_effect=AssertionError("provider I/O was not expected"))

    def claim(self, worker: str = "worker") -> effects.OrdinaryAgentClaimedJob:
        claimed = self.store.claim_due_ordinary_agent_job(worker_id=worker, lease_seconds=30)
        self.assertIsNotNone(claimed)
        assert claimed is not None
        return claimed

    def acquire(
        self, claimed: effects.OrdinaryAgentClaimedJob
    ) -> effects.OrdinaryAgentControllerFence:
        record = self.store.acquire_ordinary_merge_train_controller_state_record(
            claim_fence=claimed.claim_fence,
            expected_binding_revision=claimed.request.binding_revision,
            policy_key=self.policy.policy.policies[0].policy_key,
            policy_sha256=self.policy.policy_sha256,
            lease_seconds=30,
            initial_active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
            initial_active_phase="select_next_action",
            adoptable_active_actions=MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
        )
        return effects.OrdinaryAgentControllerFence(
            controller_key=record.controller_key,
            lease_owner=record.lease_owner,
            lease_acquired_at=record.lease_acquired_at,
        )

    def preload_snapshot(self, claimed: effects.OrdinaryAgentClaimedJob) -> None:
        fence = self.acquire(claimed)
        attempt = self.store.reserve_ordinary_agent_snapshot_attempt(
            request_id=self.request.request_id,
            expected_binding_revision=self.request.binding_revision,
            controller_fence=fence,
        )
        custody = self.store.reserve_ordinary_agent_read_custody_attempt(
            attempt_id=attempt.attempt_id,
            expected_attempt_revision=attempt.revision,
        )
        self.fixture.issue(custody)
        self.store.record_ordinary_agent_snapshot_success(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=custody.custody_attempt_id,
            result=self.snapshot_result(),
        )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=custody.custody_attempt_id,
            reason="confirmed_revoked",
        )
        self.store.yield_ordinary_merge_train_controller_state_record(
            request_id=self.request.request_id,
            expected_binding_revision=self.request.binding_revision,
            controller_fence=fence,
        )

    def snapshot_result(self) -> OrdinaryAgentMergeTrainSnapshotResult:
        pull_request = self.request.pull_requests[0]
        snapshot = MergeTrainDryRunSnapshot(
            repository=self.request.target.repository,
            base_branch=self.request.target.base_branch,
            base_sha=self.request.base_sha,
            pull_requests=(
                MergeTrainPullRequestSnapshot(
                    number=pull_request.number,
                    created_at="2026-09-09T10:00:00Z",
                    labels=("queue",),
                    actor_role="repo_admin",
                    head_sha=pull_request.head_sha,
                    mergeable="mergeable",
                    required_checks_status="pass",
                ),
            ),
        )
        return OrdinaryAgentMergeTrainSnapshotResult(
            snapshot=snapshot,
            base_identity=OrdinaryAgentCommitIdentity(sha=self.request.base_sha, tree_sha="c" * 40),
            head_identities=(
                OrdinaryAgentPullRequestHeadIdentity(
                    pull_request_number=pull_request.number,
                    identity=OrdinaryAgentCommitIdentity(
                        sha=pull_request.head_sha, tree_sha="d" * 40
                    ),
                ),
            ),
            protection=OrdinaryAgentProtectionEvidence(
                source="evaluated_rules",
                evaluated_rules_sha256="e" * 64,
                required_checks=(OrdinaryAgentRequiredCheck(context="required"),),
            ),
            counts=OrdinaryAgentProviderRequestCounts(
                rest_core_requests=1,
                graphql_requests=1,
                graphql_points=1,
            ),
            snapshot_sha256="f" * 64,
        )

    def advance(
        self, claimed: effects.OrdinaryAgentClaimedJob
    ) -> effects.OrdinaryAgentJobAttemptDisposition:
        return advance_ordinary_agent_merge_train_job(
            claimed=claimed,
            store=self.store,
            api_request=self.provider,
            effect_transport_factory=lambda _: self.provider,
            monotonic=lambda: 0,
            utc_now=lambda: datetime.fromtimestamp(self.session.now, timezone.utc),
        )

    def landing_fixture(self) -> landing_support.OrdinaryAgentLandingStorageTests:
        session = session_support.OrdinaryAgentSessionStorageTests()
        session.setUp(installation_id=77)
        self.addCleanup(session.doCleanups)
        landing = landing_support.OrdinaryAgentLandingStorageTests()
        landing.prepare_landing_fixture(session)
        return landing

    def real_landing_fixture(self) -> landing_support.OrdinaryAgentLandingStorageTests:
        session = session_support.OrdinaryAgentSessionStorageTests()
        session.setUp(installation_id=77)
        self.addCleanup(session.doCleanups)
        effect = effect_support.OrdinaryAgentEffectStorageTests()
        effect.prepare_effect_fixture(session)
        store = effect.store
        request = effect.request
        claimed = store.claim_due_ordinary_agent_job(worker_id="landing-worker", lease_seconds=300)
        assert claimed is not None
        controller = store.acquire_ordinary_merge_train_controller_state_record(
            claim_fence=claimed.claim_fence,
            expected_binding_revision=request.binding_revision,
            policy_key=effect.merge_policy.policy.policies[0].policy_key,
            policy_sha256=effect.merge_policy.policy_sha256,
            lease_seconds=300,
            initial_active_action="land_batch",
            initial_active_phase="merge_batch_entries",
            adoptable_active_actions=("land_batch",),
        )
        fence = effects.OrdinaryAgentControllerFence(
            controller_key=controller.controller_key,
            lease_owner=controller.lease_owner,
            lease_acquired_at=controller.lease_acquired_at,
        )
        candidate, plan, _, structural = _guard_records(
            repository=request.target.repository,
            pull_request_number=request.pull_requests[0].number,
            base_sha=request.base_sha,
            head_sha=request.pull_requests[0].head_sha,
            policy_sha256=effect.merge_policy.policy_sha256,
        )
        binding = controller.ordinary_job_binding
        assert binding is not None
        candidate_value = candidate.candidate.model_copy(
            update={
                "candidate_ref": build_ordinary_merge_train_candidate_ref(
                    binding=binding,
                    batch_id=candidate.candidate.batch_id,
                )
            }
        )
        candidate = candidate.model_copy(
            update={"ordinary_job_binding": binding, "candidate": candidate_value}
        )
        plan = plan.model_copy(
            update={
                "ordinary_job_binding": binding,
                "landing_plan": build_merge_train_batch_landing_plan(
                    candidate=candidate_value,
                    merge_method="merge",
                    created_at=plan.landing_plan.created_at,
                ),
            }
        )
        provenance = candidate_value.structural_provenance
        assert provenance is not None
        structural = structural.model_copy(
            update={
                "candidate_sha256": candidate_value.candidate_sha256,
                "landing_plan_sha256": plan.landing_plan.landing_plan_sha256,
                "provenance_sha256": provenance.provenance_sha256,
            }
        )
        building_candidate = candidate.model_copy(
            update={
                "record_id": "candidate-building",
                "candidate": candidate_value.model_copy(
                    update={
                        "status": "building",
                        "candidate_sha": "",
                        "candidate_tree_sha": "",
                        "candidate_sha256": "",
                        "structural_provenance": None,
                        "required_checks_status": "unknown",
                    }
                ),
            }
        )
        store.write_ordinary_merge_train_record(
            request_id=request.request_id,
            expected_binding_revision=request.binding_revision,
            controller_fence=fence,
            record=building_candidate,
        )
        candidate_effect = store.reserve_ordinary_agent_effect(
            request_id=request.request_id,
            expected_binding_revision=request.binding_revision,
            controller_fence=fence,
            command=effects.CandidateHeadMergeCommand(
                effect=CandidateHeadMergeEffect(
                    lineage=MergeTrainEffectLineage(
                        repository=request.target.repository,
                        base_branch=request.target.base_branch,
                        batch_id=candidate_value.batch_id,
                    ),
                    candidate_ref=candidate_value.candidate_ref,
                    rolling_parent_sha=request.base_sha,
                    pull_request_number=request.pull_requests[0].number,
                    head_sha=request.pull_requests[0].head_sha,
                )
            ),
            semantic_ordinal=1,
        )
        custody = store.reserve_ordinary_custody_attempt(
            effect_id=candidate_effect.effect_id,
            expected_effect_revision=candidate_effect.revision,
        )
        effect.issue(custody)
        child = store.checkpoint_ordinary_semantic_dispatch(
            effect_id=candidate_effect.effect_id,
            controller_fence=fence,
            custody_attempt_id=custody.attempt_id,
            fixed_token_expires_at=session.now + 300,
        )
        store.record_ordinary_semantic_outcome(
            child_id=child.child_id,
            typed_outcome=effects.OrdinaryAgentCompletedOutcome(
                result_sha=candidate_value.candidate_sha,
                proof=effects.OrdinaryAgentRefObservation(
                    repository=request.target.repository,
                    ref=candidate_value.candidate_ref,
                    sha=candidate_value.candidate_sha,
                    tree_sha=candidate_value.candidate_tree_sha,
                    parents=(request.base_sha, request.pull_requests[0].head_sha),
                ),
            ),
        )
        store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=custody.attempt_id,
            reason="confirmed_revoked",
        )
        persisted_candidate = store.write_ordinary_merge_train_record(
            request_id=request.request_id,
            expected_binding_revision=request.binding_revision,
            controller_fence=fence,
            record=candidate,
            expected_predecessor_record_id=building_candidate.record_id,
        )
        self.assertEqual(persisted_candidate, candidate)
        persisted_plan = store.write_ordinary_merge_train_record(
            request_id=request.request_id,
            expected_binding_revision=request.binding_revision,
            controller_fence=fence,
            record=plan,
            expected_predecessor_record_id=candidate.record_id,
        )
        self.assertEqual(persisted_plan, plan)
        current = store.list_merge_train_controller_state_records(
            repository=request.target.repository,
            base_branch=request.target.base_branch,
            limit=1,
        )[0]
        store.compare_and_set_ordinary_merge_train_controller_state_record(
            request_id=request.request_id,
            expected_binding_revision=request.binding_revision,
            controller_fence=fence,
            record=current.model_copy(
                update={
                    "active_action": "land_batch",
                    "active_phase": "merge_batch_entries",
                    "active_pull_request_number": request.pull_requests[0].number,
                    "step_payload": {
                        "landing_plan_id": plan.landing_plan.plan_id,
                        "expected_effect_sha": plan.landing_plan.candidate_sha,
                    },
                }
            ),
            lease_seconds=300,
        )
        landing = landing_support.OrdinaryAgentLandingStorageTests()
        landing.fixture = effect
        landing.store = store
        landing.request = request
        landing.claimed = claimed
        landing.fence = fence
        landing.pull_request = request.pull_requests[0].number
        landing.candidate = candidate
        landing.plan = plan
        landing.structural = structural
        return landing

    def reclaim_landing(
        self,
        landing: landing_support.OrdinaryAgentLandingStorageTests,
        *,
        worker: str,
    ) -> effects.OrdinaryAgentClaimedJob:
        now = landing.fixture.fixture.now
        expired_at = datetime.fromtimestamp(now - 1, timezone.utc).isoformat()
        with landing.store._session_factory() as session:
            claim = session.get(LaunchplaneOrdinaryAgentJobClaimRow, landing.request.request_id)
            controller = session.get(
                LaunchplaneMergeTrainControllerStateRow, landing.fence.controller_key
            )
            assert claim is not None and controller is not None
            record = MergeTrainControllerStateRecord.model_validate(controller.payload)
            expired = record.model_copy(update={"lease_expires_at": expired_at})
            controller.lease_expires_at = expired_at
            controller.payload = expired.model_dump(mode="json")
            claim.claim_expires_at = now
            claim.next_due_at = now
            session.commit()
        reclaimed = landing.store.claim_due_ordinary_agent_job(worker_id=worker, lease_seconds=30)
        self.assertIsNotNone(reclaimed)
        assert reclaimed is not None
        self.assertEqual(reclaimed.controller_fence, landing.fence)
        return reclaimed

    def test_actual_store_normal_step_replays_completed_effect_without_provider_resend(
        self,
    ) -> None:
        claimed = self.claim()
        self.preload_snapshot(claimed)

        planned = self.advance(claimed)

        self.assertEqual(planned.status, "waiting")
        candidates = self.store.list_merge_train_batch_candidate_records(
            repository=self.request.target.repository,
            base_branch=self.request.target.base_branch,
            status="active",
        )
        self.assertEqual(len(candidates), 1)
        candidate = candidates[0].candidate
        self.store.finish_ordinary_agent_job_attempt(
            claim_fence=claimed.claim_fence, disposition=planned
        )
        assert planned.next_due_at is not None
        self.session.now = planned.next_due_at
        self.session.clock.return_value = datetime.fromtimestamp(
            self.session.now, timezone.utc
        ).isoformat()
        resumed = self.claim("worker-resumed")
        fence = self.acquire(resumed)
        command = effects.CandidateRefPrepareCommand(
            effect=CandidateRefPrepareEffect(
                lineage=MergeTrainEffectLineage(
                    repository=candidate.repository,
                    base_branch=candidate.base_branch,
                    batch_id=candidate.batch_id,
                ),
                candidate_ref=candidate.candidate_ref,
                base_sha=candidate.base_sha,
            )
        )
        effect = self.store.reserve_ordinary_agent_effect(
            request_id=self.request.request_id,
            expected_binding_revision=self.request.binding_revision,
            controller_fence=fence,
            command=command,
            semantic_ordinal=1,
        )
        custody = self.store.reserve_ordinary_custody_attempt(
            effect_id=effect.effect_id,
            expected_effect_revision=effect.revision,
        )
        self.fixture.issue(custody)
        child = self.store.checkpoint_ordinary_semantic_dispatch(
            effect_id=effect.effect_id,
            controller_fence=fence,
            custody_attempt_id=custody.attempt_id,
            fixed_token_expires_at=self.session.now + 300,
        )
        self.store.record_ordinary_semantic_outcome(
            child_id=child.child_id,
            typed_outcome=effects.OrdinaryAgentCompletedOutcome(
                proof=effects.OrdinaryAgentRefObservation(
                    repository=candidate.repository,
                    ref=candidate.candidate_ref,
                    sha=candidate.base_sha,
                )
            ),
        )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=custody.attempt_id,
            reason="confirmed_revoked",
        )
        self.store.yield_ordinary_merge_train_controller_state_record(
            request_id=self.request.request_id,
            expected_binding_revision=self.request.binding_revision,
            controller_fence=fence,
        )

        built = self.advance(resumed)

        self.assertEqual(built.status, "waiting")
        self.assertEqual(
            self.store.read_ordinary_agent_effect(effect_id=effect.effect_id).dispatch_count,
            1,
        )
        active = self.store.list_merge_train_batch_candidate_records(
            repository=self.request.target.repository,
            base_branch=self.request.target.base_branch,
            status="active",
        )
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].candidate.candidate_sha, candidate.base_sha)
        self.provider.assert_not_called()

    def test_unknown_history_observes_once_without_controller_acquisition(self) -> None:
        fence, command = self.fixture.prepare_controller()
        claimed = self.fixture.claim
        effect = self.store.reserve_ordinary_agent_effect(
            request_id=self.request.request_id,
            expected_binding_revision=1,
            controller_fence=fence,
            command=command,
            semantic_ordinal=1,
        )
        custody = self.store.reserve_ordinary_custody_attempt(
            effect_id=effect.effect_id, expected_effect_revision=effect.revision
        )
        self.fixture.issue(custody)
        child = self.store.checkpoint_ordinary_semantic_dispatch(
            effect_id=effect.effect_id,
            controller_fence=fence,
            custody_attempt_id=custody.attempt_id,
            fixed_token_expires_at=self.session.now + 300,
        )
        effect = self.store.record_ordinary_semantic_outcome(
            child_id=child.child_id,
            typed_outcome=effects.OrdinaryAgentUnknownOutcome(reason="transport_ambiguous"),
        )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=custody.attempt_id, reason="confirmed_revoked"
        )
        self.store.yield_ordinary_merge_train_controller_state_record(
            request_id=self.request.request_id,
            expected_binding_revision=1,
            controller_fence=fence,
        )
        assert effect.next_observation_at is not None
        self.session.now = effect.next_observation_at
        self.session.clock.return_value = datetime.fromtimestamp(
            self.session.now, timezone.utc
        ).isoformat()
        history = self.store.read_ordinary_agent_effect_history(effect_id=effect.effect_id)
        with (
            patch(
                "control_plane.ordinary_agent_merge_train_job.reconcile_ordinary_effect_once",
                return_value=history,
            ) as reconcile,
            patch.object(
                self.store,
                "acquire_ordinary_merge_train_controller_state_record",
                wraps=self.store.acquire_ordinary_merge_train_controller_state_record,
            ) as acquire,
        ):
            disposition = self.advance(claimed)

        reconcile.assert_called_once()
        acquire.assert_not_called()
        self.assertEqual(disposition.status, "waiting")
        self.assertEqual(disposition.reason_code, "prior_effect_unresolved")
        self.assertGreater(disposition.next_due_at or 0, self.session.now)
        self.provider.assert_not_called()

    def test_open_pre_dispatch_landing_requires_readmission_after_historical_yield(
        self,
    ) -> None:
        landing = self.landing_fixture()
        preparation = landing.reserve().preparation
        reclaimed = self.reclaim_landing(landing, worker="open-preparation-recovery")
        provider = Mock(side_effect=AssertionError("provider I/O was not expected"))

        disposition = advance_ordinary_agent_merge_train_job(
            claimed=reclaimed,
            store=landing.store,
            api_request=provider,
            effect_transport_factory=lambda _: provider,
            monotonic=lambda: 0,
            utc_now=lambda: datetime.fromtimestamp(landing.fixture.fixture.now, timezone.utc),
        )

        self.assertEqual(disposition.status, "blocked")
        self.assertEqual(disposition.reason_code, "ordinary_readmission_required")
        self.assertGreater(disposition.next_due_at or 0, landing.fixture.fixture.now)
        self.assertEqual(
            landing.store.read_ordinary_landing_preparation(
                preparation_id=preparation.preparation_id
            ),
            preparation,
        )
        controller = landing.store.list_merge_train_controller_state_records(
            repository=landing.request.target.repository,
            base_branch=landing.request.target.base_branch,
        )
        self.assertEqual(len(controller), 1)
        self.assertEqual(controller[0].status, "idle")
        provider.assert_not_called()

    def test_completed_landing_restarts_from_superseded_candidate_without_provider_resend(
        self,
    ) -> None:
        landing = self.real_landing_fixture()
        execution = landing_execution_support.OrdinaryAgentLandingExecutionTests()
        execution.fixture = landing
        execution.store = landing.store
        execution.now = landing.fixture.fixture.now
        execution.inner = RecordingMergeTrainGitHubTransport()
        execution.preparation = None
        execution.read_error = None
        execution.merge_error = None
        execution.checkpoint_error = RuntimeError("checkpoint interrupted")
        execution.guard_error = None
        execution.checkpoints = []
        execution.mints = 0
        execution.elapsed = 0.0
        execution.result_sha = "9" * 40
        execution.revoke_error = None

        with self.assertRaisesRegex(RuntimeError, "checkpoint interrupted"):
            execution.run_landing()
        self.assertEqual(execution.mints, 1)
        request_count = len(execution.inner.requests)
        reclaimed = self.reclaim_landing(landing, worker="completed-landing-recovery")
        fail_provider = Mock(side_effect=AssertionError("provider I/O was not expected"))
        with patch(
            "control_plane.ordinary_agent_custody.mint_ordinary_agent_installation_token",
            side_effect=AssertionError("token mint was not expected"),
        ) as mint:
            disposition = advance_ordinary_agent_merge_train_job(
                claimed=reclaimed,
                store=landing.store,
                api_request=fail_provider,
                effect_transport_factory=lambda _: fail_provider,
                monotonic=lambda: 0,
                utc_now=lambda: datetime.fromtimestamp(landing.fixture.fixture.now, timezone.utc),
            )

        self.assertEqual(disposition.status, "waiting")
        self.assertIsNone(disposition.reason_code)
        self.assertGreater(disposition.next_due_at or 0, landing.fixture.fixture.now)
        mint.assert_not_called()
        fail_provider.assert_not_called()
        self.assertEqual(execution.mints, 1)
        self.assertEqual(len(execution.inner.requests), request_count)
        assert execution.preparation is not None
        finalization = landing.store.read_ordinary_landing_finalization(
            preparation_id=execution.preparation.preparation_id
        )
        assert finalization is not None
        self.assertEqual(finalization.effect.dispatch_count, 1)
        active = landing.store.list_merge_train_batch_landing_plan_records(
            repository=landing.request.target.repository,
            base_branch=landing.request.target.base_branch,
            status="active",
        )
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0].landing_plan.entries[0].status, "merged")
        candidates = landing.store.list_ordinary_merge_train_batch_candidate_dependencies(
            landing_plan_record=active[0],
        )
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0].status, "superseded")

    def test_idle_result_requires_readmission_and_joined_yield(self) -> None:
        claimed = self.claim()

        def idle_step(**kwargs: object) -> MergeTrainControllerRunOnceResult:
            controller = cast(OrdinaryAgentControllerAdapter, kwargs["controller_state_store"])
            record = controller.acquire_merge_train_controller_state_record(
                repository=self.request.target.repository,
                base_branch=self.request.target.base_branch,
                policy_key=self.policy.policy.policies[0].policy_key,
                policy_sha256=self.policy.policy_sha256,
                lease_owner="ignored",
                lease_seconds=30,
                initial_active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
                initial_active_phase="select_next_action",
                adoptable_active_actions=MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
            )
            controller.compare_and_set_merge_train_controller_state_record(
                record=record.model_copy(update={"lease_owner": "", "lease_acquired_at": ""}),
                expected_lease_owner=record.lease_owner,
                expected_lease_acquired_at=record.lease_acquired_at,
                lease_seconds=30,
            )
            return MergeTrainControllerRunOnceResult(
                accepted_result={
                    "repository": self.request.target.repository,
                    "base_branch": self.request.target.base_branch,
                    "mode": "dry-run",
                    "controller_action": "idle",
                },
                records={},
            )

        with patch(
            "control_plane.ordinary_agent_merge_train_job."
            "execute_merge_train_controller_with_client",
            side_effect=idle_step,
        ):
            disposition = self.advance(claimed)

        self.assertEqual(disposition.status, "blocked")
        self.assertEqual(disposition.reason_code, "ordinary_readmission_required")
        self.assertGreater(disposition.next_due_at or 0, self.session.now)

    def test_expected_error_is_not_mapped_when_joined_yield_failed(self) -> None:
        claimed = self.claim()
        original = OrdinaryAgentProviderDeferred(
            "provider_wait", retry_not_before=self.session.now + 20
        )

        def failed_release(**kwargs: object) -> MergeTrainControllerRunOnceResult:
            controller = cast(OrdinaryAgentControllerAdapter, kwargs["controller_state_store"])
            record = controller.acquire_merge_train_controller_state_record(
                repository=self.request.target.repository,
                base_branch=self.request.target.base_branch,
                policy_key=self.policy.policy.policies[0].policy_key,
                policy_sha256=self.policy.policy_sha256,
                lease_owner="ignored",
                lease_seconds=30,
                initial_active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
                initial_active_phase="select_next_action",
                adoptable_active_actions=MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
            )
            try:
                with patch.object(
                    self.store,
                    "yield_ordinary_merge_train_controller_state_record",
                    side_effect=RuntimeError("yield failed"),
                ):
                    controller.compare_and_set_merge_train_controller_state_record(
                        record=record.model_copy(
                            update={"lease_owner": "", "lease_acquired_at": ""}
                        ),
                        expected_lease_owner=record.lease_owner,
                        expected_lease_acquired_at=record.lease_acquired_at,
                        lease_seconds=30,
                    )
            except RuntimeError:
                raise original
            raise AssertionError("yield failure was not injected")

        with (
            patch(
                "control_plane.ordinary_agent_merge_train_job."
                "execute_merge_train_controller_with_client",
                side_effect=failed_release,
            ),
            self.assertRaises(OrdinaryAgentProviderDeferred) as raised,
        ):
            self.advance(claimed)
        self.assertIs(raised.exception, original)

    def test_unexpected_error_is_not_hidden_by_recovery_state(self) -> None:
        claimed = self.claim()
        snapshot = self.store.read_ordinary_agent_job_recovery_snapshot(
            claim_fence=claimed.claim_fence
        ).model_copy(update={"custody_uncertain": True})

        disposition = _expected_exception_disposition(
            error=RuntimeError("unexpected"), snapshot=snapshot
        )

        self.assertIsNone(disposition)

    def test_complete_result_waits_for_retryable_unadvanced_effect(self) -> None:
        fence, command = self.fixture.prepare_controller()
        claimed = self.fixture.claim
        effect = self.store.reserve_ordinary_agent_effect(
            request_id=self.request.request_id,
            expected_binding_revision=1,
            controller_fence=fence,
            command=command,
            semantic_ordinal=1,
        )
        custody = self.store.reserve_ordinary_custody_attempt(
            effect_id=effect.effect_id, expected_effect_revision=effect.revision
        )
        self.fixture.issue(custody)
        child = self.store.checkpoint_ordinary_semantic_dispatch(
            effect_id=effect.effect_id,
            controller_fence=fence,
            custody_attempt_id=custody.attempt_id,
            fixed_token_expires_at=self.session.now + 300,
        )
        self.store.record_ordinary_semantic_outcome(
            child_id=child.child_id,
            typed_outcome=effects.OrdinaryAgentKnownNotDispatchedOutcome(
                reason="transport_not_sent"
            ),
        )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=custody.attempt_id, reason="confirmed_revoked"
        )
        self.store.yield_ordinary_merge_train_controller_state_record(
            request_id=self.request.request_id,
            expected_binding_revision=1,
            controller_fence=fence,
        )

        disposition = _result_disposition(
            result=MergeTrainControllerRunOnceResult(
                accepted_result={"landing_progress": "complete"}, records={}
            ),
            claimed=claimed,
            store=self.store,
        )

        self.assertEqual(disposition.status, "waiting")
        self.assertEqual(disposition.reason_code, "prior_effect_unresolved")
        self.assertGreater(disposition.next_due_at or 0, self.session.now)

    def test_evidence_bound_evaluator_fails_closed_before_bind(self) -> None:
        evaluator = _EvidenceBoundOrdinaryAdmissionEvaluator(
            store=self.store,
            policy_record=self.policy,
        )
        with self.assertRaisesRegex(MergeAdmissionDeniedError, "has not been bound"):
            evaluator.evaluate(
                candidate_record=Mock(),
                landing_plan_record=Mock(),
                entry=Mock(),
                observed_base_sha="a",
                observed_base_tree_sha="b",
                observed_head_sha="c",
                observed_head_tree_sha="d",
                controller_state=Mock(),
                expected_lease_owner="worker",
                stack_collapse_record=None,
                evaluated_at="2026-09-09T10:00:00Z",
            )


if __name__ == "__main__":
    unittest.main()
