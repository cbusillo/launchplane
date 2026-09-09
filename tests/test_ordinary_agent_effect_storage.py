from __future__ import annotations

from datetime import datetime, timezone
from dataclasses import replace
import unittest
import hashlib
from unittest.mock import patch

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent_custody import OrdinaryAgentCustodyCandidate
from control_plane.contracts.ordinary_agent import OrdinaryAgentPullRequest
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts import ordinary_agent_snapshot as snapshots
from control_plane.merge_train import MergeTrainDryRunSnapshot, MergeTrainPullRequestSnapshot
from control_plane.github_app_identity import ordinary_agent_effect_permissions

from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentControllerFence,
    OrdinaryAgentEffectRecord,
    OrdinaryAgentCustodyAttemptReservation,
    OrdinaryAgentReadCustodyReservation,
    OrdinaryAgentJobClaimFence,
    OrdinaryAgentPullRequestObservation,
    OrdinaryAgentReconciliationObservation,
    OrdinaryAgentUnknownOutcome,
    OrdinaryAgentCompletedOutcome,
    PullRequestHeadRefreshCommand,
    OrdinaryAgentJobAttemptDisposition,
    OrdinaryAgentProviderQuotaKey,
    OrdinaryAgentProviderWaitObservation,
)
from control_plane.contracts.merge_train_effect import (
    MergeTrainEffectLineage,
    PullRequestHeadRefreshEffect,
)
from control_plane.contracts.ordinary_agent_session_lifecycle import OrdinaryAgentLeaseRecord
from control_plane.storage.postgres import LaunchplaneOrdinaryAgentLeaseRow
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.ordinary_agent_effect_recovery import recover_ordinary_effect
from tests import test_ordinary_agent_session_storage as session_support


class OrdinaryAgentEffectStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = session_support.OrdinaryAgentSessionStorageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.prepare_effect_fixture(self.fixture)

    def prepare_effect_fixture(
        self,
        fixture: session_support.OrdinaryAgentSessionStorageTests,
        *,
        stack_child_number: int | None = None,
    ) -> None:
        self.fixture = fixture
        self.fixture.enroll()
        if stack_child_number is not None:
            self.fixture.request = self.fixture.request.model_copy(
                update={
                    "pull_requests": self.fixture.request.pull_requests
                    + (OrdinaryAgentPullRequest(number=stack_child_number, head_sha="c" * 40),),
                    "permitted_stack_edit_pull_requests": (stack_child_number,),
                }
            )
        self.store = self.fixture.store
        self.merge_policy = MergeTrainPolicyRecord.model_validate(
            {
                "record_id": "effect-test-policy",
                "source": "test",
                "updated_at": "2026-01-01T00:00:00Z",
                "policy": {
                    "policies": [
                        {
                            "repository": self.fixture.request.target.repository,
                            "base_branch": self.fixture.request.target.base_branch,
                            "enqueue_label": "queue",
                            "blocked_label": "blocked",
                            "merge_method": "merge",
                            "failure_policy": "pause_train",
                            "enqueue": {},
                            "merge_identity": {"kind": "github_app", "name": "test"},
                        }
                    ]
                },
            }
        )
        self.store.write_merge_train_policy_record(self.merge_policy)
        self.request = self.store.admit_ordinary_agent_finite_request(
            proof=self.fixture.proof, request=self.fixture.request
        )

    def prepare_controller(
        self,
    ) -> tuple[OrdinaryAgentControllerFence, PullRequestHeadRefreshCommand]:
        claim = self.store.claim_due_ordinary_agent_job(worker_id="worker", lease_seconds=30)
        assert claim is not None
        self.claim = claim
        controller = self.store.acquire_ordinary_merge_train_controller_state_record(
            claim_fence=claim.claim_fence,
            expected_binding_revision=1,
            policy_key=self.merge_policy.policy.policies[0].policy_key,
            policy_sha256=self.merge_policy.policy_sha256,
            lease_seconds=30,
            initial_active_action="head_refresh",
            initial_active_phase="prepare",
            adoptable_active_actions=("head_refresh",),
        )
        fence = OrdinaryAgentControllerFence(
            controller_key=controller.controller_key,
            lease_owner=controller.lease_owner,
            lease_acquired_at=controller.lease_acquired_at,
        )
        command = PullRequestHeadRefreshCommand(
            effect=PullRequestHeadRefreshEffect(
                lineage=MergeTrainEffectLineage(
                    repository=self.request.target.repository,
                    base_branch=self.request.target.base_branch,
                ),
                pull_request_number=self.request.pull_requests[0].number,
                expected_head_sha=self.request.pull_requests[0].head_sha,
                expected_base_sha=self.request.base_sha,
            )
        )
        return fence, command

    def prepare_refresh(
        self,
    ) -> tuple[
        OrdinaryAgentEffectRecord, OrdinaryAgentControllerFence, PullRequestHeadRefreshCommand
    ]:
        fence, command = self.prepare_controller()
        effect = self.store.reserve_ordinary_agent_effect(
            request_id=self.request.request_id,
            expected_binding_revision=1,
            controller_fence=fence,
            command=command,
            semantic_ordinal=1,
        )
        return effect, fence, command

    def issue(
        self,
        reservation: OrdinaryAgentCustodyAttemptReservation | OrdinaryAgentReadCustodyReservation,
    ) -> None:
        attempt_id = (
            reservation.attempt_id
            if isinstance(reservation, OrdinaryAgentCustodyAttemptReservation)
            else reservation.custody_attempt_id
        )
        candidate = reservation.candidate
        acquired, _ = self.store.acquire_ordinary_agent_custody_issue_attempt(
            attempt_id=attempt_id,
            idempotency_key_sha256=hashlib.sha256(reservation.idempotency_key.encode()).hexdigest(),
            request_sha256=canonical_json_sha256(
                {
                    "candidate": candidate.model_dump(mode="json"),
                    "request": reservation.request_payload,
                }
            ),
            candidate=candidate,
            requested_permissions=ordinary_agent_effect_permissions(candidate.effect_profile),
            dispatch_window_seconds=30,
        )
        self.assertEqual(acquired, "acquired")
        self.store.mark_ordinary_agent_custody_issued(
            attempt_id=attempt_id,
            app_id=candidate.expected_app_id,
            installation_id=77,
            token_expires_at=datetime.fromtimestamp(
                self.fixture.now + 300, timezone.utc
            ).isoformat(),
            residual_expires_at=datetime.fromtimestamp(
                self.fixture.now + 360, timezone.utc
            ).isoformat(),
        )

    def test_effect_reservation_charges_once_and_last_ordinal_remains_current(self) -> None:
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneOrdinaryAgentLeaseRow, self.request.lease_id)
            assert row is not None
            lease = OrdinaryAgentLeaseRecord.model_validate(row.payload)
            row.payload = lease.model_copy(
                update={"budget": lease.budget.model_copy(update={"action_limit": 1})}
            ).model_dump(mode="json")
            session.commit()
        effect, fence, command = self.prepare_refresh()
        replay = self.store.reserve_ordinary_agent_effect(
            request_id=self.request.request_id,
            expected_binding_revision=1,
            controller_fence=fence,
            command=command,
            semantic_ordinal=1,
        )
        self.assertEqual(replay, effect)
        with self.store._session_factory() as session:
            with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "budget_exhausted"):
                self.store._ordinary_agent_new_effect_context(
                    session, request_id=self.request.request_id
                )
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "semantic_command_already_reserved"
        ):
            self.store.reserve_ordinary_agent_effect(
                request_id=self.request.request_id,
                expected_binding_revision=1,
                controller_fence=fence,
                command=command,
                semantic_ordinal=2,
            )
        with self.store._session_factory() as session:
            self.store._begin_serialized_write(session)
            context, _, _ = self.store._ordinary_agent_reserved_effect_context(
                session, effect_id=effect.effect_id
            )
            self.assertEqual(context.lease.budget.actions_used, 1)
        reservation = self.store.reserve_ordinary_custody_attempt(
            effect_id=effect.effect_id, expected_effect_revision=effect.revision
        )
        self.issue(reservation)
        child = self.store.checkpoint_ordinary_semantic_dispatch(
            effect_id=effect.effect_id,
            controller_fence=fence,
            custody_attempt_id=reservation.attempt_id,
            fixed_token_expires_at=self.fixture.now + 300,
        )
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "dispatch_already_checkpointed"
        ):
            self.store.checkpoint_ordinary_semantic_dispatch(
                effect_id=effect.effect_id,
                controller_fence=fence,
                custody_attempt_id=reservation.attempt_id,
                fixed_token_expires_at=self.fixture.now + 300,
            )
        self.store.cancel_ordinary_agent_session(
            proof=self.fixture.proof, session_id=self.request.session_id
        )
        with self.store._session_factory() as session:
            with self.assertRaises(OrdinaryAgentSessionAdmissionDenied):
                self.store._ordinary_agent_reserved_effect_context(
                    session, effect_id=effect.effect_id
                )

        unknown = self.store.record_ordinary_semantic_outcome(
            child_id=child.child_id,
            typed_outcome=OrdinaryAgentUnknownOutcome(reason="transport_ambiguous"),
        )
        self.assertEqual(unknown.state, "reconciliation_required")
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "immutable_outcome_conflict"
        ):
            self.store.record_ordinary_semantic_outcome(
                child_id=child.child_id, typed_outcome=OrdinaryAgentCompletedOutcome()
            )

    def test_unknown_dispatch_cannot_be_bypassed_with_a_new_semantic_parent(self) -> None:
        effect, fence, command = self.prepare_refresh()
        permit = self.store.reserve_ordinary_custody_attempt(
            effect_id=effect.effect_id, expected_effect_revision=effect.revision
        )
        self.issue(permit)
        child = self.store.checkpoint_ordinary_semantic_dispatch(
            effect_id=effect.effect_id,
            controller_fence=fence,
            custody_attempt_id=permit.attempt_id,
            fixed_token_expires_at=self.fixture.now + 300,
        )
        self.store.record_ordinary_semantic_outcome(
            child_id=child.child_id,
            typed_outcome=OrdinaryAgentUnknownOutcome(reason="transport_ambiguous"),
        )
        another = PullRequestHeadRefreshCommand(
            effect=replace(
                command.effect, lineage=replace(command.effect.lineage, batch_id="another-plan")
            )
        )
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "prior_effect_unresolved"):
            self.store.reserve_ordinary_agent_effect(
                request_id=self.request.request_id,
                expected_binding_revision=1,
                controller_fence=fence,
                command=another,
                semantic_ordinal=2,
            )
        self.assertEqual(
            self.store.read_ordinary_agent_job(
                proof=self.fixture.proof, request_id=self.request.request_id
            ).unresolved_effects,
            1,
        )

    def test_completed_refresh_rebinds_only_exact_proof_without_new_charge(self) -> None:
        effect, fence, _ = self.prepare_refresh()
        permit = self.store.reserve_ordinary_custody_attempt(
            effect_id=effect.effect_id, expected_effect_revision=effect.revision
        )
        self.issue(permit)
        child = self.store.checkpoint_ordinary_semantic_dispatch(
            effect_id=effect.effect_id,
            controller_fence=fence,
            custody_attempt_id=permit.attempt_id,
            fixed_token_expires_at=self.fixture.now + 300,
        )
        proof = OrdinaryAgentPullRequestObservation(
            repository=self.request.target.repository,
            number=self.request.pull_requests[0].number,
            head_sha="d" * 40,
            base_ref=self.request.target.base_branch,
            base_sha=self.request.base_sha,
            state="open",
            merged=False,
            head_parents=(self.request.pull_requests[0].head_sha, self.request.base_sha),
        )
        completed = self.store.record_ordinary_semantic_outcome(
            child_id=child.child_id,
            typed_outcome=OrdinaryAgentCompletedOutcome(result_sha="d" * 40, proof=proof),
        )
        self.assertEqual(completed.state, "rebind_pending")
        self.assertEqual(
            recover_ordinary_effect(
                self.store.read_ordinary_agent_effect_history(effect_id=effect.effect_id)
            ).disposition,
            "rebind",
        )
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "effect_linked_refresh_required"
        ):
            self.store.refresh_ordinary_agent_finite_job(
                request_id=self.request.request_id,
                expected_binding_revision=1,
                base_sha="invented-base",
                pull_requests=self.request.pull_requests,
            )
        rebound = self.store.rebind_ordinary_agent_after_head_refresh(
            effect_id=effect.effect_id,
            expected_effect_revision=completed.revision,
            controller_fence=fence,
        )
        self.assertEqual(rebound.pull_requests[0].head_sha, "d" * 40)
        self.assertEqual(rebound.expires_at, self.request.expires_at)
        self.assertEqual(rebound.refresh_used, 1)
        self.assertEqual(
            recover_ordinary_effect(
                self.store.read_ordinary_agent_effect_history(effect_id=effect.effect_id)
            ).disposition,
            "replay",
        )
        self.assertEqual(
            self.store.rebind_ordinary_agent_after_head_refresh(
                effect_id=effect.effect_id,
                expected_effect_revision=completed.revision,
                controller_fence=fence,
            ),
            rebound,
        )
        with self.store._session_factory() as session:
            lease = session.get(LaunchplaneOrdinaryAgentLeaseRow, rebound.lease_id)
            assert lease is not None
            self.assertEqual(
                OrdinaryAgentLeaseRecord.model_validate(lease.payload).budget.actions_used, 1
            )

    def test_reconciliation_survives_session_cancel_but_cannot_authorize_rebind(self) -> None:
        effect, fence, _ = self.prepare_refresh()
        permit = self.store.reserve_ordinary_custody_attempt(
            effect_id=effect.effect_id, expected_effect_revision=effect.revision
        )
        self.issue(permit)
        child = self.store.checkpoint_ordinary_semantic_dispatch(
            effect_id=effect.effect_id,
            controller_fence=fence,
            custody_attempt_id=permit.attempt_id,
            fixed_token_expires_at=self.fixture.now + 300,
        )
        unknown = self.store.record_ordinary_semantic_outcome(
            child_id=child.child_id,
            typed_outcome=OrdinaryAgentUnknownOutcome(reason="transport_ambiguous"),
        )
        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=permit.attempt_id, reason="confirmed_revoked"
        )
        self.store.cancel_ordinary_agent_session(
            proof=self.fixture.proof, session_id=self.request.session_id
        )
        self.fixture.clock.return_value = datetime.fromtimestamp(
            self.fixture.now + 16, timezone.utc
        ).isoformat()
        observation_permit = self.store.reserve_ordinary_reconciliation_custody_attempt(
            effect_id=effect.effect_id, expected_effect_revision=unknown.revision
        )
        self.issue(observation_permit)
        observation = OrdinaryAgentReconciliationObservation(
            observation_id="refresh-observation",
            custody_attempt_id=observation_permit.attempt_id,
            observed_at=self.fixture.now + 16,
            observation=OrdinaryAgentPullRequestObservation(
                repository=self.request.target.repository,
                number=self.request.pull_requests[0].number,
                head_sha="e" * 40,
                base_ref=self.request.target.base_branch,
                base_sha=self.request.base_sha,
                state="open",
                merged=False,
                head_parents=(self.request.pull_requests[0].head_sha, self.request.base_sha),
            ),
        )
        result = self.store.append_ordinary_effect_reconciliation(
            child_id=child.child_id, typed_observation=observation
        )
        self.assertEqual(result.state, "rebind_pending")
        self.assertEqual(
            self.store.append_ordinary_effect_reconciliation(
                child_id=child.child_id, typed_observation=observation
            ),
            result,
        )
        with self.assertRaises(OrdinaryAgentSessionAdmissionDenied):
            self.store.rebind_ordinary_agent_after_head_refresh(
                effect_id=effect.effect_id,
                expected_effect_revision=result.revision,
                controller_fence=fence,
            )

    def snapshot_result(self) -> snapshots.OrdinaryAgentMergeTrainSnapshotResult:
        return snapshots.OrdinaryAgentMergeTrainSnapshotResult(
            snapshot=MergeTrainDryRunSnapshot(
                repository=self.request.target.repository,
                base_branch=self.request.target.base_branch,
                base_sha=self.request.base_sha,
                pull_requests=tuple(
                    MergeTrainPullRequestSnapshot(
                        number=item.number,
                        head_sha=item.head_sha,
                        created_at="2026-01-01T00:00:00Z",
                        mergeable="mergeable",
                        required_checks_status="pass",
                    )
                    for item in self.request.pull_requests
                ),
            ),
            base_identity=snapshots.OrdinaryAgentCommitIdentity(
                sha=self.request.base_sha, tree_sha="base-tree"
            ),
            head_identities=tuple(
                snapshots.OrdinaryAgentPullRequestHeadIdentity(
                    pull_request_number=item.number,
                    identity=snapshots.OrdinaryAgentCommitIdentity(
                        sha=item.head_sha, tree_sha="head-tree"
                    ),
                )
                for item in self.request.pull_requests
            ),
            protection=snapshots.OrdinaryAgentProtectionEvidence(
                source="both",
                classic_sha256="a" * 64,
                evaluated_rules_sha256="b" * 64,
                required_checks=(snapshots.OrdinaryAgentRequiredCheck(context="ci"),),
            ),
            counts=snapshots.OrdinaryAgentProviderRequestCounts(
                rest_core_requests=1, graphql_requests=1, graphql_points=1
            ),
            snapshot_sha256="c" * 64,
        )

    def test_snapshot_replay_and_cleanup_uncertainty_preserve_normalized_result(self) -> None:
        _, fence, _ = self.prepare_refresh()
        attempt = self.store.reserve_ordinary_agent_snapshot_attempt(
            request_id=self.request.request_id, expected_binding_revision=1, controller_fence=fence
        )
        replay = self.store.reserve_ordinary_agent_snapshot_attempt(
            request_id=self.request.request_id, expected_binding_revision=1, controller_fence=fence
        )
        self.assertEqual(attempt, replay)
        permit = self.store.reserve_ordinary_agent_read_custody_attempt(
            attempt_id=attempt.attempt_id, expected_attempt_revision=attempt.revision
        )
        self.issue(permit)
        result = self.snapshot_result()
        observed = self.store.record_ordinary_agent_snapshot_success(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=permit.custody_attempt_id,
            result=result,
        )
        self.assertEqual(
            self.store.reserve_ordinary_agent_snapshot_attempt(
                request_id=self.request.request_id,
                expected_binding_revision=1,
                controller_fence=fence,
            ).result,
            result,
        )
        self.store.mark_ordinary_agent_custody_cleanup_unknown(attempt_id=permit.custody_attempt_id)
        fenced = self.store.record_ordinary_agent_read_failure(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=permit.custody_attempt_id,
            reason_code="cleanup_unknown",
            counts=result.counts,
        )
        self.assertEqual(fenced.result, observed.result)
        self.assertEqual(fenced.state, "fenced")
        view = self.store.read_ordinary_agent_job(
            proof=self.fixture.proof, request_id=self.request.request_id
        )
        self.assertEqual(view.status, "reconciliation_required")
        with self.assertRaises(OrdinaryAgentSessionAdmissionDenied):
            self.store.reserve_ordinary_agent_read_custody_attempt(
                attempt_id=attempt.attempt_id, expected_attempt_revision=fenced.revision
            )

        self.store.close_ordinary_agent_custody_issue_attempt(
            attempt_id=permit.custody_attempt_id, reason="confirmed_revoked"
        )
        recovered = self.store.reserve_ordinary_agent_snapshot_attempt(
            request_id=self.request.request_id, expected_binding_revision=1, controller_fence=fence
        )
        self.assertEqual(recovered.result, result)
        self.assertEqual(recovered.state, "completed")

    def test_pending_source_observations_wait_then_accept_without_using_failure_budget(
        self,
    ) -> None:
        _, fence, _ = self.prepare_refresh()
        ready = self.snapshot_result()
        initial_time = self.fixture.now
        with patch(
            "control_plane.contracts.ordinary_agent_effect.CANDIDATE_CHECK_DELAYS_SECONDS", (1,)
        ):
            for index in range(5):
                attempt = self.store.reserve_ordinary_agent_snapshot_attempt(
                    request_id=self.request.request_id,
                    expected_binding_revision=1,
                    controller_fence=fence,
                )
                permit = self.store.reserve_ordinary_agent_read_custody_attempt(
                    attempt_id=attempt.attempt_id,
                    expected_attempt_revision=attempt.revision,
                )
                self.issue(permit)
                # Alternate pending checks, unknown checks and unknown mergeability.
                values = (
                    {"required_checks_status": "pending"},
                    {"required_checks_status": "unknown"},
                    {"mergeable": "unknown"},
                    {"required_checks_status": "pending"},
                    {},
                )[index]
                result = ready.model_copy(
                    update={
                        "snapshot": ready.snapshot.model_copy(
                            update={
                                "pull_requests": tuple(
                                    item.model_copy(update=values)
                                    for item in ready.snapshot.pull_requests
                                )
                            }
                        )
                    }
                )
                observed = self.store.record_ordinary_agent_snapshot_success(
                    attempt_id=attempt.attempt_id,
                    custody_attempt_id=permit.custody_attempt_id,
                    result=result,
                )
                if index == 0:
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
                if index < 4:
                    self.assertEqual(observed.state, "completed")
                    with self.assertRaisesRegex(
                        OrdinaryAgentSessionAdmissionDenied, "source_check_wait"
                    ):
                        self.store.reserve_ordinary_agent_snapshot_attempt(
                            request_id=self.request.request_id,
                            expected_binding_revision=1,
                            controller_fence=fence,
                        )
                    self.fixture.clock.return_value = datetime.fromtimestamp(
                        initial_time + index + 1, timezone.utc
                    ).isoformat()
                else:
                    replay = self.store.reserve_ordinary_agent_snapshot_attempt(
                        request_id=self.request.request_id,
                        expected_binding_revision=1,
                        controller_fence=fence,
                    )
                    self.assertEqual(replay.result, ready)
                    self.assertEqual(replay.attempt_id, attempt.attempt_id)

    def test_unconfigured_checks_remain_exhausted_after_cleanup_recovery(self) -> None:
        _, fence, _ = self.prepare_refresh()
        ready = self.snapshot_result()
        result = ready.model_copy(
            update={"protection": ready.protection.model_copy(update={"required_checks": ()})}
        )
        attempt = self.store.reserve_ordinary_agent_snapshot_attempt(
            request_id=self.request.request_id,
            expected_binding_revision=1,
            controller_fence=fence,
        )
        permit = self.store.reserve_ordinary_agent_read_custody_attempt(
            attempt_id=attempt.attempt_id,
            expected_attempt_revision=attempt.revision,
        )
        self.issue(permit)
        observed = self.store.record_ordinary_agent_snapshot_success(
            attempt_id=attempt.attempt_id,
            custody_attempt_id=permit.custody_attempt_id,
            result=result,
        )
        self.assertEqual(observed.reason_code, "required_checks_unconfigured")
        self.assertEqual(observed.state, "exhausted")
        self.store.mark_ordinary_agent_custody_cleanup_unknown(attempt_id=permit.custody_attempt_id)
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
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "required_checks_unconfigured"
        ):
            self.store.reserve_ordinary_agent_snapshot_attempt(
                request_id=self.request.request_id,
                expected_binding_revision=1,
                controller_fence=fence,
            )

    def test_expiry_during_dispatch_validation_does_not_checkpoint(self) -> None:
        effect, fence, _ = self.prepare_refresh()
        reservation = self.store.reserve_ordinary_custody_attempt(
            effect_id=effect.effect_id, expected_effect_revision=effect.revision
        )
        self.issue(reservation)

        def advance_clock(*args: object, **kwargs: object) -> OrdinaryAgentCustodyCandidate:
            self.fixture.clock.return_value = datetime.fromtimestamp(
                self.fixture.now + 31, timezone.utc
            ).isoformat()
            return reservation.candidate

        with patch.object(
            self.store, "_ordinary_agent_effect_custody_candidate", side_effect=advance_clock
        ):
            with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "job_claim_lost"):
                self.store.checkpoint_ordinary_semantic_dispatch(
                    effect_id=effect.effect_id,
                    controller_fence=fence,
                    custody_attempt_id=reservation.attempt_id,
                    fixed_token_expires_at=self.fixture.now + 300,
                )
        stored = self.store.read_ordinary_agent_effect(effect_id=effect.effect_id)
        assert stored is not None
        self.assertEqual(stored.dispatch_count, 0)
        self.assertEqual(stored.state, "reserved")

    def test_completion_requires_current_claim_and_persists_terminal_request(self) -> None:
        fence, _ = self.prepare_controller()
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "controller_not_yielded"):
            self.store.finish_ordinary_agent_job_attempt(
                claim_fence=self.claim.claim_fence,
                disposition=OrdinaryAgentJobAttemptDisposition(status="completed"),
            )
        self.store.yield_ordinary_merge_train_controller_state_record(
            request_id=self.request.request_id, expected_binding_revision=1, controller_fence=fence
        )
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "job_claim_lost"):
            self.store.finish_ordinary_agent_job_attempt(
                claim_fence=OrdinaryAgentJobClaimFence(
                    request_id=self.request.request_id,
                    worker_id="foreign",
                    generation=self.claim.claim_fence.generation,
                ),
                disposition=OrdinaryAgentJobAttemptDisposition(status="completed"),
            )
        finished = self.store.finish_ordinary_agent_job_attempt(
            claim_fence=self.claim.claim_fence,
            disposition=OrdinaryAgentJobAttemptDisposition(status="completed"),
        )
        self.assertEqual(finished.status, "completed")
        replay = self.store.admit_ordinary_agent_finite_request(
            proof=self.fixture.proof, request=self.fixture.request
        )
        self.assertEqual(replay.status, "completed")
        with self.assertRaises(OrdinaryAgentSessionAdmissionDenied):
            self.store.reauthorize_ordinary_agent_finite_job(request_id=self.request.request_id)

    def test_claim_takeover_fences_old_worker_and_preserves_cancellation(self) -> None:
        first = self.store.claim_due_ordinary_agent_job(worker_id="first", lease_seconds=10)
        assert first is not None
        self.assertIsNone(
            self.store.claim_due_ordinary_agent_job(worker_id="second", lease_seconds=10)
        )
        self.fixture.clock.return_value = datetime.fromtimestamp(
            self.fixture.now + 11, timezone.utc
        ).isoformat()
        second = self.store.claim_due_ordinary_agent_job(worker_id="second", lease_seconds=10)
        assert second is not None
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "job_claim_lost"):
            self.store.finish_ordinary_agent_job_attempt(
                claim_fence=first.claim_fence,
                disposition=OrdinaryAgentJobAttemptDisposition(status="completed"),
            )
        self.store.cancel_ordinary_agent_session(
            proof=self.fixture.proof, session_id=self.request.session_id
        )
        view = self.store.finish_ordinary_agent_job_attempt(
            claim_fence=second.claim_fence,
            disposition=OrdinaryAgentJobAttemptDisposition(
                status="blocked", reason_code="raw provider text must never escape"
            ),
        )
        self.assertEqual(view.status, "cancelled")
        self.assertTrue(view.cancellation_requested)
        self.assertIsNone(view.reason_code)
        self.assertIsNone(
            self.store.claim_due_ordinary_agent_job(worker_id="third", lease_seconds=10)
        )

    def test_custody_wait_reports_latest_deadline_without_consuming_an_attempt(self) -> None:
        effect, _, _ = self.prepare_refresh()
        for resource, delay in (("core", 30), ("secondary", 600)):
            self.store.record_provider_wait(
                quota_key=OrdinaryAgentProviderQuotaKey.model_validate(
                    {
                        "authority_kind": "app",
                        "authority_id": 123456,
                        "resource_class": resource,
                    }
                ),
                observation=OrdinaryAgentProviderWaitObservation(
                    retry_not_before=self.fixture.now + delay, classification="primary_rate_limit"
                ),
            )
        with self.assertRaises(OrdinaryAgentSessionAdmissionDenied) as raised:
            self.store.reserve_ordinary_custody_attempt(
                effect_id=effect.effect_id,
                expected_effect_revision=effect.revision,
            )
        self.assertEqual(raised.exception.reason_code, "provider_wait")
        self.assertEqual(raised.exception.retry_not_before, self.fixture.now + 600)
        # Remove fixture waits to verify that the denied call consumed no custody attempt.
        from control_plane.storage.postgres import LaunchplaneOrdinaryAgentProviderWaitRow

        with self.store._session_factory() as session:
            session.query(LaunchplaneOrdinaryAgentProviderWaitRow).delete()
            session.commit()
        permit = self.store.reserve_ordinary_custody_attempt(
            effect_id=effect.effect_id,
            expected_effect_revision=effect.revision,
        )
        self.assertEqual(permit.custody_ordinal, 1)

    def test_provider_wait_keeps_later_shared_deadline_and_isolates_quota_identity(self) -> None:
        key = OrdinaryAgentProviderQuotaKey(
            provider="github",
            authority_kind="installation",
            authority_id=17,
            resource_class="core",
        )
        later = self.store.record_provider_wait(
            quota_key=key,
            observation=OrdinaryAgentProviderWaitObservation(
                retry_not_before=self.fixture.now + 600, classification="primary_rate_limit"
            ),
        )
        replay = self.store.record_provider_wait(
            quota_key=key,
            observation=OrdinaryAgentProviderWaitObservation(
                retry_not_before=self.fixture.now + 30, classification="secondary_rate_limit"
            ),
        )
        self.assertEqual(replay, later)
        self.assertEqual(self.store.read_provider_wait(quota_key=key), later)
        self.assertIsNone(
            self.store.read_provider_wait(quota_key=key.model_copy(update={"authority_id": 18}))
        )
