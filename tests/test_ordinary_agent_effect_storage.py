from __future__ import annotations

from datetime import datetime, timezone
import unittest
import hashlib

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.github_app_identity import ordinary_agent_effect_permissions

from control_plane.contracts.ordinary_agent_effect import (
    OrdinaryAgentControllerFence,
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
from tests import test_ordinary_agent_session_storage as session_support


class OrdinaryAgentEffectStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = session_support.OrdinaryAgentSessionStorageTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.fixture.enroll()
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

    def test_effect_reservation_charges_once_and_last_ordinal_remains_current(self) -> None:
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneOrdinaryAgentLeaseRow, self.request.lease_id)
            assert row is not None
            lease = OrdinaryAgentLeaseRecord.model_validate(row.payload)
            row.payload = lease.model_copy(
                update={"budget": lease.budget.model_copy(update={"action_limit": 1})}
            ).model_dump(mode="json")
            session.commit()
        claim = self.store.claim_due_ordinary_agent_job(worker_id="worker", lease_seconds=30)
        assert claim is not None
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
        effect = self.store.reserve_ordinary_agent_effect(
            request_id=self.request.request_id,
            expected_binding_revision=1,
            controller_fence=fence,
            command=command,
            semantic_ordinal=1,
        )
        replay = self.store.reserve_ordinary_agent_effect(
            request_id=self.request.request_id,
            expected_binding_revision=1,
            controller_fence=fence,
            command=command,
            semantic_ordinal=1,
        )
        self.assertEqual(replay, effect)
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "budget_exhausted"):
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
        candidate = reservation.candidate
        self.store.acquire_ordinary_agent_custody_issue_attempt(
            attempt_id=reservation.attempt_id,
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
        self.store.mark_ordinary_agent_custody_issued(
            attempt_id=reservation.attempt_id,
            app_id=candidate.expected_app_id,
            installation_id=77,
            token_expires_at=datetime.fromtimestamp(
                self.fixture.now + 300, timezone.utc
            ).isoformat(),
            residual_expires_at=datetime.fromtimestamp(
                self.fixture.now + 360, timezone.utc
            ).isoformat(),
        )
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
