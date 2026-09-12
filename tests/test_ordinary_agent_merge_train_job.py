from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch
from typing import cast

from control_plane.contracts import ordinary_agent_effect as effects
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_train_effect import (
    CandidateHeadMergeEffect,
    CandidateRefPrepareEffect,
    MergeTrainEffectLineage,
    PullRequestHeadRefreshEffect,
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
    OrdinaryAgentReadmissionObservation,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentGuardedFiniteRequest,
)
from control_plane.merge_admission import MergeAdmissionDeniedError
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from control_plane.merge_train_github import RecordingMergeTrainGitHubTransport
from control_plane.merge_train_controller_run_once import (
    MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
    MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
    MergeTrainControllerRunOnceResult,
)
from control_plane.ordinary_agent_github_transport import OrdinaryAgentProviderDeferred
from control_plane.ordinary_agent_custody import (
    OrdinaryAgentPreDispatchAdmissionCleanupUnknown,
)
from control_plane.ordinary_agent_merge_train_snapshot import (
    OrdinaryAgentReadmissionRequired,
    acquire_ordinary_agent_merge_train_snapshot,
)
from control_plane.ordinary_agent_controller_store import OrdinaryAgentControllerAdapter
from control_plane.ordinary_agent_merge_train_job import (
    _EvidenceBoundOrdinaryAdmissionEvaluator,
    _expected_exception_disposition,
    _result_disposition,
    advance_ordinary_agent_merge_train_job,
)
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
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
        self,
        claimed: effects.OrdinaryAgentClaimedJob,
        *,
        ensure_provider_readiness: Callable[..., object] | None = None,
    ) -> effects.OrdinaryAgentJobAttemptDisposition:
        return advance_ordinary_agent_merge_train_job(
            claimed=claimed,
            store=self.store,
            api_request=self.provider,
            effect_transport_factory=lambda _: self.provider,
            monotonic=lambda: 0,
            utc_now=lambda: datetime.fromtimestamp(self.session.now, timezone.utc),
            ensure_provider_readiness=(ensure_provider_readiness or (lambda **_: object())),
        )

    def test_provider_readiness_deferral_precedes_controller_and_provider_work(self) -> None:
        claimed = self.claim()
        ensure = Mock(
            side_effect=OrdinaryAgentSessionAdmissionDenied(
                "provider_readiness_in_progress",
                retry_not_before=self.session.now + 20,
                server_observed_at=self.session.now,
            )
        )

        with patch.object(
            self.store, "acquire_ordinary_merge_train_controller_state_record"
        ) as acquire:
            disposition = self.advance(claimed, ensure_provider_readiness=ensure)

        self.assertEqual(
            (disposition.status, disposition.reason_code),
            ("waiting", "provider_readiness_in_progress"),
        )
        self.assertEqual(disposition.next_due_at, self.session.now + 20)
        ensure.assert_called_once_with(
            store=self.store,
            request_id=self.request.request_id,
        )
        self.provider.assert_not_called()
        acquire.assert_not_called()

    def completed_refresh(self) -> tuple[effects.OrdinaryAgentClaimedJob, str]:
        claimed = self.claim()
        fence = self.acquire(claimed)
        effect = self.store.reserve_ordinary_agent_effect(
            request_id=self.request.request_id,
            expected_binding_revision=self.request.binding_revision,
            controller_fence=fence,
            command=effects.PullRequestHeadRefreshCommand(
                effect=PullRequestHeadRefreshEffect(
                    lineage=MergeTrainEffectLineage(
                        repository=self.request.target.repository,
                        base_branch=self.request.target.base_branch,
                    ),
                    pull_request_number=self.request.pull_requests[0].number,
                    expected_head_sha=self.request.pull_requests[0].head_sha,
                    expected_base_sha=self.request.base_sha,
                )
            ),
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
            typed_outcome=effects.OrdinaryAgentCompletedOutcome(
                result_sha="d" * 40,
                proof=effects.OrdinaryAgentPullRequestObservation(
                    repository=self.request.target.repository,
                    number=self.request.pull_requests[0].number,
                    head_sha="d" * 40,
                    base_ref=self.request.target.base_branch,
                    base_sha=self.request.base_sha,
                    state="open",
                    merged=False,
                    head_parents=(self.request.pull_requests[0].head_sha, self.request.base_sha),
                ),
            ),
        )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=custody.attempt_id, reason="confirmed_revoked"
        )
        # Restart after the original worker and controller leases expire.
        self.session.now += 31
        self.session.clock.return_value = datetime.fromtimestamp(
            self.session.now, timezone.utc
        ).isoformat()
        return self.claim("restarted-worker"), effect.effect_id

    def test_completed_refresh_rebinds_and_next_claim_reads_new_binding(self) -> None:
        claimed, effect_id = self.completed_refresh()
        result = self.advance(claimed)
        self.assertEqual((result.status, result.reason_code), ("waiting", None))
        history = self.store.read_ordinary_agent_effect_history(effect_id=effect_id)
        self.assertEqual((history.effect.state, history.effect.rebound_revision), ("completed", 2))
        self.assertEqual(history.effect.dispatch_count, 1)
        self.store.finish_ordinary_agent_job_attempt(
            claim_fence=claimed.claim_fence, disposition=result
        )
        assert result.next_due_at is not None
        self.session.now = result.next_due_at
        self.session.clock.return_value = datetime.fromtimestamp(
            self.session.now, timezone.utc
        ).isoformat()
        resumed = self.claim("next-worker")
        resumed_request = cast(OrdinaryAgentGuardedFiniteRequest, resumed.request)
        self.assertEqual(resumed_request.binding_revision, 2)
        self.assertEqual(resumed_request.pull_requests[0].head_sha, "d" * 40)
        self.assertEqual(resumed_request.refresh_used, 1)
        self.assertIsNone(resumed.controller_fence)
        self.provider.assert_not_called()

    def test_rebind_failure_yields_new_acquisition_before_mapping_or_raising(self) -> None:
        claimed, _ = self.completed_refresh()
        for reason, expected in (
            ("refresh_allowance_exhausted", "ordinary_readmission_required"),
            ("record_predecessor_conflict", "prior_effect_unresolved"),
            ("binding_revision_conflict", None),
            ("policy_unavailable", "ordinary_readmission_required"),
        ):
            with (
                self.subTest(reason=reason),
                patch.object(
                    self.store,
                    "rebind_ordinary_agent_after_head_refresh",
                    side_effect=OrdinaryAgentSessionAdmissionDenied(reason),
                ),
                patch.object(
                    self.store,
                    "yield_ordinary_merge_train_controller_state_record",
                    wraps=self.store.yield_ordinary_merge_train_controller_state_record,
                ) as yielded,
                patch.object(
                    OrdinaryAgentControllerAdapter,
                    "release_terminal_history",
                    side_effect=AssertionError(
                        "must not release historical fence after acquisition"
                    ),
                ),
            ):
                if expected is None:
                    with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, reason):
                        self.advance(claimed)
                else:
                    self.assertEqual(self.advance(claimed).reason_code, expected)
                yielded.assert_called_once()
                controllers = self.store.list_merge_train_controller_state_records(
                    repository=self.request.target.repository,
                    base_branch=self.request.target.base_branch,
                )
                self.assertEqual(controllers[0].status, "idle")
        self.provider.assert_not_called()

    def test_rebind_acquire_denial_releases_historical_controller_and_finishes_attempt(
        self,
    ) -> None:
        claimed, _ = self.completed_refresh()
        assert claimed.controller_fence is not None
        with self.store._session_factory() as session:
            row = session.get(
                LaunchplaneMergeTrainControllerStateRow, claimed.controller_fence.controller_key
            )
            assert row is not None
            record = MergeTrainControllerStateRecord.model_validate(row.payload)
            row.payload = record.model_copy(update={"policy_sha256": "1" * 64}).model_dump(
                mode="json"
            )
            session.commit()
        with patch.object(
            self.store,
            "yield_ordinary_merge_train_controller_state_record",
            wraps=self.store.yield_ordinary_merge_train_controller_state_record,
        ) as yielded:
            result = self.advance(claimed)
        self.assertEqual(
            (result.status, result.reason_code), ("blocked", "ordinary_readmission_required")
        )
        self.assertEqual(yielded.call_args.kwargs["controller_fence"], claimed.controller_fence)
        yielded.assert_called_once()
        finished = self.store.finish_ordinary_agent_job_attempt(
            claim_fence=claimed.claim_fence, disposition=result
        )
        self.assertEqual(finished.status, "blocked")
        self.provider.assert_not_called()

    def test_rebind_failed_yield_remains_loud(self) -> None:
        claimed, _ = self.completed_refresh()
        with (
            patch.object(
                self.store,
                "rebind_ordinary_agent_after_head_refresh",
                side_effect=OrdinaryAgentSessionAdmissionDenied("refresh_allowance_exhausted"),
            ),
            patch.object(
                self.store,
                "yield_ordinary_merge_train_controller_state_record",
                side_effect=RuntimeError("yield failed"),
            ),
            patch.object(
                OrdinaryAgentControllerAdapter,
                "release_terminal_history",
                side_effect=AssertionError("must not retry a historical yield"),
            ),
        ):
            with self.assertRaises(ExceptionGroup) as caught:
                self.advance(claimed)
        self.assertEqual(len(caught.exception.exceptions), 2)
        self.assertIsInstance(caught.exception.exceptions[0], OrdinaryAgentSessionAdmissionDenied)
        self.assertEqual(str(caught.exception.exceptions[1]), "yield failed")
        self.provider.assert_not_called()

    def readmission_observation(self) -> OrdinaryAgentReadmissionObservation:
        request = self.request
        payload = {
            "schema_version": 1,
            "observed_at": self.session.now,
            "target": request.target.model_dump(mode="json"),
            "repository_owner_id": 202,
            "captured_base_sha": request.base_sha,
            "captured_pull_requests": [
                item.model_dump(mode="json") for item in request.pull_requests
            ],
            "base_identity": {"sha": "e" * 40, "tree_sha": "f" * 40, "parent_shas": []},
            "pull_requests": [
                {
                    "number": item.number,
                    "head_sha": item.head_sha,
                    "base_ref": request.target.base_branch,
                    "lifecycle": "open",
                    "head_repository_id": request.target.repository_id,
                    "head_repository": request.target.repository,
                    "base_repository_id": request.target.repository_id,
                    "base_repository": request.target.repository,
                }
                for item in request.pull_requests
            ],
            "drift": "base",
            "counts": {"rest_core_requests": 0, "graphql_requests": 0, "graphql_points": 0},
        }
        return OrdinaryAgentReadmissionObservation.model_validate(
            {**payload, "observation_sha256": canonical_json_sha256(payload)}
        )

    def test_readmission_replay_raises_stored_signal_without_provider_or_custody(self) -> None:
        claimed = self.claim()
        fence = self.acquire(claimed)
        attempt = self.store.reserve_ordinary_agent_snapshot_attempt(
            request_id=self.request.request_id, expected_binding_revision=1, controller_fence=fence
        )
        custody = self.store.reserve_ordinary_agent_read_custody_attempt(
            attempt_id=attempt.attempt_id, expected_attempt_revision=attempt.revision
        )
        self.fixture.issue(custody)
        observation = self.readmission_observation()
        self.store.record_ordinary_agent_snapshot_success(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=custody.custody_attempt_id,
            result=observation,
        )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=custody.custody_attempt_id, reason="confirmed_revoked"
        )
        custody_store = Mock()
        with self.assertRaises(OrdinaryAgentReadmissionRequired) as caught:
            acquire_ordinary_agent_merge_train_snapshot(
                store=self.store,
                custody_store=custody_store,
                secret_store=Mock(),
                request_id=self.request.request_id,
                expected_binding_revision=1,
                controller_fence=fence,
                reader=self.provider,
                api_request=self.provider,
            )
        self.assertEqual(caught.exception.attempt_id, attempt.attempt_id)
        self.assertEqual(caught.exception.observation_sha256, observation.observation_sha256)
        self.assertEqual(custody_store.mock_calls, [])
        self.provider.assert_not_called()

    def test_readmission_signal_cannot_finalize_after_failed_core_yield(self) -> None:
        claimed = self.claim()
        signal = OrdinaryAgentReadmissionRequired(
            attempt_id="read-test", observation_sha256="a" * 64
        )
        with (
            patch(
                "control_plane.ordinary_agent_merge_train_job.acquire_ordinary_agent_merge_train_snapshot",
                side_effect=signal,
            ),
            patch.object(
                self.store,
                "yield_ordinary_merge_train_controller_state_record",
                side_effect=RuntimeError("yield failed"),
            ),
            patch.object(self.store, "finalize_ordinary_agent_readmission") as finalize,
            self.assertLogs("control_plane.merge_train_controller_run_once", level="WARNING"),
        ):
            with self.assertRaises(OrdinaryAgentReadmissionRequired):
                self.advance(claimed)
        finalize.assert_not_called()
        self.provider.assert_not_called()

    def test_pristine_drift_commits_after_custody_cleanup_and_core_yield(self) -> None:
        # Reader tests cover GraphQL parsing and counts; inject its immutable
        # result here to exercise custody, the real core yield, and joined storage.
        claimed = self.claim()
        observation = self.readmission_observation()
        provider = Mock(return_value=None)
        mint = Mock(
            return_value=GitHubAppInstallationToken(
                token="test-readmission-token",
                app_id=self.session.envelope.custody.github_app_id,
                installation_id=77,
                repository_id=self.request.target.repository_id,
                repository=self.request.target.repository,
                expires_at=datetime.fromtimestamp(self.session.now + 300, timezone.utc).isoformat(),
            )
        )
        with (
            patch(
                "control_plane.ordinary_agent_custody.resolve_ordinary_agent_github_app_identity",
                side_effect=lambda **kw: SimpleNamespace(
                    identity=SimpleNamespace(app_id=kw["candidate"].expected_app_id)
                ),
            ),
            patch(
                "control_plane.ordinary_agent_custody.mint_ordinary_agent_installation_token",
                mint,
            ),
            patch(
                "control_plane.ordinary_agent_merge_train_job.read_ordinary_controller_snapshot",
                return_value=observation,
            ),
        ):
            result = advance_ordinary_agent_merge_train_job(
                claimed=claimed,
                store=self.store,
                api_request=provider,
                monotonic=lambda: 0,
                utc_now=lambda: datetime.fromtimestamp(self.session.now, timezone.utc),
                ensure_provider_readiness=lambda **_: object(),
            )
        self.assertEqual((result.status, result.reason_code), ("waiting", None))
        refreshed = self.store.read_ordinary_agent_job_recovery_snapshot(
            claim_fence=claimed.claim_fence
        )
        self.assertEqual(refreshed.binding_revision, 2)
        self.assertEqual(refreshed.total_effects, 0)
        self.assertFalse(refreshed.custody_uncertain)
        self.store.finish_ordinary_agent_job_attempt(
            claim_fence=claimed.claim_fence, disposition=result
        )
        assert result.next_due_at is not None
        self.session.now = result.next_due_at
        self.session.clock.return_value = datetime.fromtimestamp(
            self.session.now, timezone.utc
        ).isoformat()
        resumed = self.claim("readmitted-worker")
        resumed_request = cast(OrdinaryAgentGuardedFiniteRequest, resumed.request)
        self.assertEqual(resumed_request.base_sha, observation.base_identity.sha)
        self.assertEqual(resumed_request.refresh_used, 1)
        mint.assert_called_once()
        self.assertEqual(
            [(call.kwargs["method"], call.kwargs["path"]) for call in provider.call_args_list],
            [("DELETE", "/installation/token")],
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
            ensure_provider_readiness=lambda **_: object(),
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

    def test_non_landing_deadline_classification_cannot_enable_landing_retry(
        self,
    ) -> None:
        landing = self.landing_fixture()
        preparation, proposal = landing.observed_proposal()
        finalization = landing.finalize(preparation, proposal)
        landing.store.record_ordinary_semantic_outcome(
            child_id=finalization.child.child_id,
            typed_outcome=effects.OrdinaryAgentKnownNotDispatchedOutcome(
                reason="transport_not_sent"
            ),
        )
        landing.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=preparation.custody_attempt_id, reason="confirmed_revoked"
        )
        reclaimed = self.reclaim_landing(landing, worker="undispatched-landing-recovery")
        with patch.object(
            landing.store,
            "acquire_ordinary_merge_train_controller_state_record",
            side_effect=AssertionError("non-landing deferral cannot authorize fresh work"),
        ):
            result = advance_ordinary_agent_merge_train_job(
                claimed=reclaimed,
                store=landing.store,
                api_request=self.provider,
                effect_transport_factory=lambda _: self.provider,
                monotonic=lambda: 0,
                utc_now=lambda: datetime.fromtimestamp(landing.fixture.fixture.now, timezone.utc),
                ensure_provider_readiness=lambda **_: object(),
            )
        self.assertEqual(
            (result.status, result.reason_code), ("blocked", "ordinary_readmission_required")
        )
        history = landing.store.read_ordinary_agent_effect_history(
            effect_id=finalization.effect.effect_id
        )
        self.assertEqual(history.effect.dispatch_count, 1)
        self.assertEqual(history.child, finalization.child)
        landing.store.finish_ordinary_agent_job_attempt(
            claim_fence=reclaimed.claim_fence, disposition=result
        )
        self.provider.assert_not_called()

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
                ensure_provider_readiness=lambda **_: object(),
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

    def test_pre_dispatch_readiness_cleanup_failure_preserves_both_retry_fences(self) -> None:
        claimed = self.claim()
        provider_retry = self.session.now + 90
        custody_retry = self.session.now + 45
        snapshot = self.store.read_ordinary_agent_job_recovery_snapshot(
            claim_fence=claimed.claim_fence
        ).model_copy(
            update={
                "custody_uncertain": True,
                "provider_retry_not_before": custody_retry,
            }
        )
        error = OrdinaryAgentPreDispatchAdmissionCleanupUnknown(
            OrdinaryAgentSessionAdmissionDenied(
                "provider_readiness_refresh_required",
                retry_not_before=provider_retry,
            )
        )

        disposition = _expected_exception_disposition(error=error, snapshot=snapshot)

        assert disposition is not None
        self.assertEqual(
            (disposition.status, disposition.reason_code, disposition.next_due_at),
            ("waiting", "provider_readiness_cleanup_required", provider_retry),
        )

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
