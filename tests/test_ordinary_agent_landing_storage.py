"""Preparation history remains charged even when no merge is dispatched."""

import unittest
from types import SimpleNamespace
from datetime import datetime, timezone
import hashlib
from unittest.mock import patch

from sqlalchemy.orm import Session

from control_plane.contracts.ordinary_agent_snapshot import (
    OrdinaryAgentLandingEvidence,
    OrdinaryAgentCommitIdentity,
    OrdinaryAgentProtectionEvidence,
    OrdinaryAgentProviderRequestCounts,
    OrdinaryAgentRequiredCheck,
)
from control_plane.contracts.owner_acceptance import OwnerAcceptanceDecision
from control_plane.contracts.change_impact import (
    ChangeImpactRepositoryEvidence,
    ChangeImpactTarget,
    ChangeImpactBaseEvidence,
    ChangeImpactChangedFileEvidence,
)
from control_plane.tenant_admission_controller import (
    TenantAdmissionTechnicalChecks,
    TenantAdmissionRequiredTechnicalCheck,
    TenantAdmissionTechnicalCheckSignal,
)
from control_plane.merge_admission import GuardedMergeAdmission, MergeAdmissionEvaluation
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from tests import test_merge_readiness as readiness_support
from tests.test_merge_admission_records import _StaticEvaluator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_admission_record import MergeAdmissionProposal
from control_plane.contracts.merge_train_controller_state import MergeTrainControllerStateRecord
from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentControllerFence,
    OrdinaryAgentLandingReservation,
    OrdinaryAgentLandingPreparation,
    OrdinaryAgentLandingFinalization,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentLeaseRecord
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.github_app_identity import (
    ordinary_agent_effect_permissions,
    GitHubAppInstallationToken,
)
from control_plane.ordinary_agent_custody import ordinary_agent_provider_token_lease
from control_plane.storage.postgres import (
    LaunchplaneMergeTrainBatchCandidateRow,
    LaunchplaneMergeTrainBatchLandingPlanRow,
    LaunchplaneMergeTrainControllerStateRow,
    LaunchplaneOrdinaryAgentLeaseRow,
    LaunchplaneMergeAdmissionRow,
    LaunchplaneOrdinaryAgentEffectRow,
    LaunchplaneOrdinaryAgentLandingPreparationRow,
)
from tests import test_ordinary_agent_effect_storage as effect_support
from tests import test_ordinary_agent_session_storage as session_support
from tests import test_postgres_integration as postgres_support
from tests.test_merge_admission_records import _guard_records


class OrdinaryAgentLandingStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        session_fixture = session_support.OrdinaryAgentSessionStorageTests()
        session_fixture.setUp()
        self.addCleanup(session_fixture.doCleanups)
        self.prepare_landing_fixture(session_fixture)

    def prepare_landing_fixture(
        self, session_fixture: session_support.OrdinaryAgentSessionStorageTests
    ) -> None:
        self.fixture = effect_support.OrdinaryAgentEffectStorageTests()
        self.fixture.prepare_effect_fixture(session_fixture)
        self.store = self.fixture.store
        self.request = self.fixture.request
        claim = self.store.claim_due_ordinary_agent_job(
            worker_id="landing-worker", lease_seconds=300
        )
        assert claim is not None
        self.claimed = claim
        controller = self.store.acquire_ordinary_merge_train_controller_state_record(
            claim_fence=claim.claim_fence,
            expected_binding_revision=1,
            policy_key=self.fixture.merge_policy.policy.policies[0].policy_key,
            policy_sha256=self.fixture.merge_policy.policy_sha256,
            lease_seconds=300,
            initial_active_action="land_batch",
            initial_active_phase="merge_batch_entries",
            adoptable_active_actions=("land_batch",),
        )
        self.fence = OrdinaryAgentControllerFence(
            controller_key=controller.controller_key,
            lease_owner=controller.lease_owner,
            lease_acquired_at=controller.lease_acquired_at,
        )
        self.pull_request = self.request.pull_requests[0].number
        candidate, plan, _, self.structural = _guard_records(
            repository=self.request.target.repository,
            pull_request_number=self.pull_request,
            base_sha=self.request.base_sha,
            head_sha=self.request.pull_requests[0].head_sha,
            policy_sha256=self.fixture.merge_policy.policy_sha256,
        )
        self.candidate = candidate.model_copy(
            update={"ordinary_job_binding": controller.ordinary_job_binding}
        )
        self.plan = plan.model_copy(
            update={"ordinary_job_binding": controller.ordinary_job_binding}
        )
        # Seed already-built historical artifacts. These tests exercise landing
        # preparation, not the independently tested candidate-builder lifecycle.
        with self.store._session_factory() as session:
            session.add(
                LaunchplaneMergeTrainBatchCandidateRow(
                    record_id=self.candidate.record_id,
                    status=self.candidate.status,
                    source=self.candidate.source,
                    updated_at=self.candidate.updated_at,
                    repository=self.request.target.repository,
                    base_branch=self.request.target.base_branch,
                    batch_id=self.candidate.candidate.batch_id,
                    candidate_status=self.candidate.candidate.status,
                    payload=self.candidate.model_dump(mode="json"),
                )
            )
            session.add(
                LaunchplaneMergeTrainBatchLandingPlanRow(
                    record_id=self.plan.record_id,
                    status=self.plan.status,
                    source=self.plan.source,
                    updated_at=self.plan.updated_at,
                    repository=self.request.target.repository,
                    base_branch=self.request.target.base_branch,
                    batch_id=self.plan.landing_plan.batch_id,
                    plan_id=self.plan.landing_plan.plan_id,
                    payload=self.plan.model_dump(mode="json"),
                )
            )
            row = session.get(LaunchplaneMergeTrainControllerStateRow, controller.controller_key)
            assert row is not None
            current = MergeTrainControllerStateRecord.model_validate(row.payload)
            row.payload = current.model_copy(
                update={
                    "active_record_id": self.plan.record_id,
                    "active_pull_request_number": self.pull_request,
                    "step_payload": {
                        "landing_plan_id": self.plan.landing_plan.plan_id,
                        "expected_effect_sha": self.plan.landing_plan.candidate_sha,
                    },
                }
            ).model_dump(mode="json")
            session.commit()

    def reserve(self) -> OrdinaryAgentLandingReservation:
        return self.store.reserve_ordinary_landing_preparation(
            request_id=self.request.request_id,
            expected_binding_revision=1,
            controller_fence=self.fence,
            pull_request_number=self.pull_request,
            semantic_ordinal=1,
        )

    def observed_proposal(
        self, *, check_age_seconds: int = 0
    ) -> tuple[OrdinaryAgentLandingPreparation, MergeAdmissionProposal]:
        preparation = self.reserve().preparation
        candidate = preparation.candidate
        now = self.fixture.fixture.now
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
        preparation = self.reserve().preparation
        evidence = self.evidence(preparation, check_age_seconds=check_age_seconds)
        observed = self.store.record_ordinary_landing_evidence(
            preparation_id=preparation.preparation_id,
            expected_revision=preparation.revision,
            controller_fence=self.fence,
            evidence=evidence,
        )
        guard = self.guard(preparation)
        proposal = guard.build_proposal(
            entry=preparation.entry,
            observed_base_sha=preparation.expected_base_sha,
            observed_base_tree_sha=preparation.expected_base_tree_sha,
            observed_head_sha=preparation.entry.expected_head_sha,
            observed_head_tree_sha=preparation.entry.expected_head_tree_sha,
        )
        return observed, proposal

    def evidence(self, preparation, *, check_age_seconds=0):
        now = self.fixture.fixture.now
        timestamp = datetime.fromtimestamp(now, timezone.utc).isoformat()
        checks = TenantAdmissionTechnicalChecks(
            head_sha=self.candidate.candidate.candidate_sha,
            base_sha=preparation.expected_base_sha,
            strict=False,
            status="pass",
            required_checks=(TenantAdmissionRequiredTechnicalCheck(name="ci-gate"),),
            signals=(
                TenantAdmissionTechnicalCheckSignal(
                    source="check_run", name="ci-gate", state="pass"
                ),
            ),
            evaluated_at=datetime.fromtimestamp(now - check_age_seconds, timezone.utc).isoformat(),
        )
        repository = ChangeImpactRepositoryEvidence(
            target=ChangeImpactTarget(
                repository_id=str(preparation.target.repository_id),
                repository_owner_id="202",
                repository=preparation.target.repository,
                pull_request_number=preparation.entry.pull_request_number,
                head_sha=preparation.entry.expected_head_sha,
                tree_sha=preparation.entry.expected_head_tree_sha,
            ),
            base=ChangeImpactBaseEvidence(base_ref="main", base_sha=preparation.expected_base_sha),
            changed_files=(ChangeImpactChangedFileEvidence(path="control_plane/example.py"),),
        )
        evidence = OrdinaryAgentLandingEvidence(
            repository_id=preparation.target.repository_id,
            repository_owner_id=202,
            repository=preparation.target.repository,
            base_ref="main",
            base_identity=OrdinaryAgentCommitIdentity(
                sha=preparation.expected_base_sha, tree_sha=preparation.expected_base_tree_sha
            ),
            repository_evidence=repository,
            candidate_entry_evidence=(repository,),
            snapshot=MergeTrainDryRunSnapshot(
                repository=preparation.target.repository,
                base_branch=preparation.target.base_branch,
                base_sha=preparation.expected_base_sha,
                pull_requests=(
                    MergeTrainPullRequestSnapshot(
                        number=preparation.entry.pull_request_number,
                        head_sha=preparation.entry.expected_head_sha,
                        created_at=timestamp,
                    ),
                ),
            ),
            candidate_sha=self.candidate.candidate.candidate_sha,
            technical_checks=checks,
            protection=OrdinaryAgentProtectionEvidence(
                source="evaluated_rules",
                evaluated_rules_sha256="a" * 64,
                required_checks=tuple(
                    OrdinaryAgentRequiredCheck(context=item.name, integration_id=item.app_id)
                    for item in checks.required_checks
                ),
            ),
            expected_merge_tree_sha=preparation.expected_merge_tree_sha,
            observed_at=now,
            counts=OrdinaryAgentProviderRequestCounts(
                rest_core_requests=0, graphql_requests=0, graphql_points=0
            ),
            evidence_sha256="b" * 64,
        )
        return evidence

    def guard(self, preparation):
        timestamp = datetime.fromtimestamp(self.fixture.fixture.now, timezone.utc).isoformat()
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneMergeTrainControllerStateRow, self.fence.controller_key)
            assert row is not None
            controller = MergeTrainControllerStateRecord.model_validate(row.payload)
        target = readiness_support._target(
            repository=preparation.target.repository,
            pull_request_number=self.pull_request,
            base_sha=preparation.expected_base_sha,
            pull_request_head_sha=preparation.entry.expected_head_sha,
            pull_request_tree_sha=preparation.entry.expected_head_tree_sha,
            queue_position=1,
        )
        ready = readiness_support._evaluate(
            target=target,
            policy_fingerprints=readiness_support._policy_fingerprints().model_copy(
                update={
                    "merge_train": readiness_support._policy_fingerprints().merge_train.model_copy(
                        update={
                            "expected_sha256": controller.policy_sha256,
                            "current_sha256": controller.policy_sha256,
                        }
                    ),
                }
            ),
            owner_decision=OwnerAcceptanceDecision(
                status="not_required", reason_code="engineering_only", evaluated_at=timestamp
            ),
            engineering_decision=None,
            engineering_evidence=(),
            engineering_review_authority="advisory",
            candidate_evidence=readiness_support._candidate(
                repository=target.repository,
                base_sha=target.base_sha,
                pull_request_number=self.pull_request,
                queue_position=1,
                pull_request_head_sha=target.pull_request_head_sha,
            ),
            fence_evidence=readiness_support._fence(
                controller_key=controller.controller_key,
                controller_repository=target.repository,
                expected_lease_owner=controller.lease_owner,
                observed_lease_owner=controller.lease_owner,
                lease_expires_at=controller.lease_expires_at,
            ),
            evaluated_at=timestamp,
        )
        guard = GuardedMergeAdmission(
            record_store=self.store,
            evaluator=_StaticEvaluator(MergeAdmissionEvaluation(ready, self.structural)),
            candidate_record=self.candidate,
            landing_plan_record=self.plan,
            controller_state=controller,
            trace_id="test-joined-landing",
            admission_time_provider=lambda: timestamp,
        )
        return guard

    def finalize(
        self, preparation: OrdinaryAgentLandingPreparation, proposal: MergeAdmissionProposal
    ) -> OrdinaryAgentLandingFinalization:
        return self.store.finalize_ordinary_landing_preparation(
            preparation_id=preparation.preparation_id,
            expected_revision=preparation.revision,
            controller_fence=self.fence,
            proposal=proposal,
            custody_attempt_id=preparation.custody_attempt_id,
        )

    def test_supported_custody_lease_stamps_and_closes_the_reserved_preparation(self) -> None:
        preparation = self.reserve().preparation
        now = self.fixture.fixture.now
        token = GitHubAppInstallationToken(
            token="test-provider-token",
            app_id=preparation.candidate.expected_app_id,
            installation_id=77,
            repository_id=preparation.target.repository_id,
            repository=preparation.target.repository,
            expires_at=datetime.fromtimestamp(now + 300, timezone.utc).isoformat(),
        )
        with (
            patch(
                "control_plane.ordinary_agent_custody.resolve_ordinary_agent_github_app_identity",
                return_value=SimpleNamespace(identity=None),
            ),
            patch(
                "control_plane.ordinary_agent_custody.mint_ordinary_agent_installation_token",
                return_value=token,
            ),
            ordinary_agent_provider_token_lease(
                record_store=self.store,
                secret_store=self.store,
                candidate=preparation.candidate,
                idempotency_key=preparation.idempotency_key,
                request_payload=preparation.request_payload,
                api_request=lambda **kwargs: None,
                monotonic=lambda: 0.0,
                utc_now=lambda: datetime.fromtimestamp(now, timezone.utc),
            ) as lease,
        ):
            stamped = self.reserve().preparation
            self.assertIsNotNone(stamped.work_expires_at)
            self.assertGreater(stamped.revision, preparation.revision)
            self.assertEqual(stamped.custody_attempt_id, lease.attempt_id)
        closed = self.store.read_ordinary_agent_custody_issue_attempt(lease.attempt_id)
        self.assertEqual((closed.state, closed.close_reason), ("closed", "confirmed_revoked"))

    def test_joined_finalization_replay_never_issues_a_second_dispatch(self) -> None:
        preparation, proposal = self.observed_proposal()
        result = self.finalize(preparation, proposal)
        self.assertEqual(result.disposition, "created")
        replay = self.finalize(preparation, proposal)
        self.assertEqual(replay.disposition, "replay")
        self.assertEqual(result.child, replay.child)
        self.assertEqual(result.preparation.state, "consumed")
        self.assertEqual(result.effect.action_ordinal, preparation.action_ordinal)
        self.assertEqual(
            self.store.read_merge_admission_record(proposal.record.admission_id), proposal.record
        )

    def test_failure_after_flush_leaves_no_orphan_admission_or_effect(self) -> None:
        preparation, proposal = self.observed_proposal()

        def fail_after_flush(session: Session) -> None:
            session.flush()
            raise ValueError("injected commit failure")

        with patch.object(Session, "commit", fail_after_flush), self.assertRaises(ValueError):
            self.finalize(preparation, proposal)
        with self.store._session_factory() as session:
            self.assertIsNone(
                session.get(LaunchplaneMergeAdmissionRow, proposal.record.admission_id)
            )
            self.assertEqual(len(list(session.query(LaunchplaneOrdinaryAgentEffectRow))), 0)
            row = session.get(
                LaunchplaneOrdinaryAgentLandingPreparationRow, preparation.preparation_id
            )
            assert row is not None
            self.assertEqual(row.payload["state"], "observed")
        self.assertIsNone(
            self.store.read_ordinary_landing_finalization(preparation_id=preparation.preparation_id)
        )

    def test_delayed_finalization_uses_current_time_and_expired_replay_is_history(self) -> None:
        preparation, proposal = self.observed_proposal()
        clock = self.fixture.fixture
        clock.clock.return_value = datetime.fromtimestamp(clock.now + 2, timezone.utc).isoformat()
        result = self.finalize(preparation, proposal)
        self.assertEqual(result.child.dispatch_checkpoint_at, clock.now + 2)
        clock.clock.return_value = datetime.fromtimestamp(clock.now + 150, timezone.utc).isoformat()
        self.assertEqual(self.finalize(preparation, proposal).disposition, "replay")

    def test_expired_evidence_cannot_create_an_admission(self) -> None:
        preparation, proposal = self.observed_proposal()
        clock = self.fixture.fixture
        clock.clock.return_value = datetime.fromtimestamp(clock.now + 46, timezone.utc).isoformat()
        with self.assertRaises(OrdinaryAgentSessionAdmissionDenied):
            self.finalize(preparation, proposal)
        with self.store._session_factory() as session:
            self.assertIsNone(
                session.get(LaunchplaneMergeAdmissionRow, proposal.record.admission_id)
            )

    def test_new_owner_decision_invalidates_observed_authority(self) -> None:
        preparation, proposal = self.observed_proposal()
        original = postgres_support._owner_acceptance_event()
        decision = original.model_copy(
            update={
                "binding": original.binding.model_copy(
                    update={
                        "repository_id": str(preparation.target.repository_id),
                        "repository": preparation.target.repository,
                        "pull_request_number": preparation.entry.pull_request_number,
                    }
                )
            }
        )
        self.store.write_owner_acceptance_event_record(decision)
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "landing_authority_changed"
        ):
            self.finalize(preparation, proposal)
        with self.store._session_factory() as session:
            self.assertIsNone(
                session.get(LaunchplaneMergeAdmissionRow, proposal.record.admission_id)
            )
            self.assertEqual(len(list(session.query(LaunchplaneOrdinaryAgentEffectRow))), 0)

    def test_fresh_envelope_cannot_reuse_checks_from_before_preparation(self) -> None:
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "landing_evidence_conflict"
        ):
            self.observed_proposal(check_age_seconds=60)
        with self.store._session_factory() as session:
            self.assertEqual(len(list(session.query(LaunchplaneMergeAdmissionRow))), 0)
            self.assertEqual(len(list(session.query(LaunchplaneOrdinaryAgentEffectRow))), 0)

    def test_close_keeps_charge_and_never_creates_dispatch_permission(self) -> None:
        first = self.reserve()
        replay = self.reserve()
        self.assertEqual(first.disposition, "created")
        self.assertEqual(replay.disposition, "replay")
        self.assertEqual(first.preparation, replay.preparation)
        closed = self.store.close_ordinary_landing_preparation(
            preparation_id=first.preparation.preparation_id,
            expected_revision=first.preparation.revision,
            reason_code="evidence_denied",
        )
        self.assertEqual(closed.state, "terminal")
        self.assertIsNone(
            self.store.read_ordinary_landing_finalization(preparation_id=closed.preparation_id)
        )
        self.assertEqual(self.reserve().preparation, closed)
        with self.store._session_factory() as session:
            lease_row = session.get(LaunchplaneOrdinaryAgentLeaseRow, self.request.lease_id)
            assert lease_row is not None
            lease = OrdinaryAgentLeaseRecord.model_validate(lease_row.payload)
            self.assertEqual(lease.budget.actions_used, 1)

    def test_stale_close_cannot_overwrite_terminal_reason(self) -> None:
        first = self.reserve().preparation
        closed = self.store.close_ordinary_landing_preparation(
            preparation_id=first.preparation_id,
            expected_revision=first.revision,
            reason_code="process_interrupted",
        )
        with self.assertRaises(OrdinaryAgentSessionAdmissionDenied):
            self.store.close_ordinary_landing_preparation(
                preparation_id=first.preparation_id,
                expected_revision=first.revision,
                reason_code="evidence_denied",
            )
        self.assertEqual(self.reserve().preparation, closed)

    def test_late_mint_is_recorded_without_reopening_closed_preparation(self) -> None:
        preparation = self.reserve().preparation
        candidate = preparation.candidate
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
        closed = self.store.close_ordinary_landing_preparation(
            preparation_id=preparation.preparation_id,
            expected_revision=preparation.revision,
            reason_code="process_interrupted",
        )
        with self.store._session_factory() as session:
            self.assertTrue(
                self.store._ordinary_agent_job_custody_uncertainty(session, self.request.request_id)
            )
        self.store.mark_ordinary_agent_custody_issued(
            attempt_id=preparation.custody_attempt_id,
            app_id=candidate.expected_app_id,
            installation_id=77,
            token_expires_at=datetime.fromtimestamp(
                self.fixture.fixture.now + 300, timezone.utc
            ).isoformat(),
            residual_expires_at=datetime.fromtimestamp(
                self.fixture.fixture.now + 360, timezone.utc
            ).isoformat(),
        )
        self.assertEqual(
            self.store.read_ordinary_agent_custody_issue_attempt(
                preparation.custody_attempt_id
            ).state,
            "issued",
        )
        self.assertEqual(self.reserve().preparation, closed)
        self.assertIsNone(closed.work_expires_at)
