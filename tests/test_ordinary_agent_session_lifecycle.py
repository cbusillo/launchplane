from __future__ import annotations

import unittest

from control_plane.contracts.ordinary_agent import OrdinaryAgentPullRequest
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentFiniteRequestRecord,
    OrdinaryAgentSessionDelegation,
)
from control_plane.ordinary_agent_lifecycle import build_ordinary_agent_lifecycle_write_set
from control_plane.ordinary_agent_session_lifecycle import (
    OrdinaryAgentSessionAdmissionDenied,
    build_ordinary_agent_request_admission_write_set,
    build_ordinary_agent_session_write_set,
    require_ordinary_agent_finite_job_authority,
    require_ordinary_agent_current_job_authority,
    rebind_ordinary_agent_finite_request,
    cancel_ordinary_agent_finite_request,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.ordinary_agent_lifecycle import (
    enrollment_envelope,
    setup_ordinary_agent_authority,
)


class OrdinaryAgentSessionLifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        store = PostgresRecordStore(database_url="sqlite+pysqlite:///:memory:")
        self.addCleanup(store.close)
        store.ensure_schema()
        self.policy, inventory = setup_ordinary_agent_authority(store)
        lifecycle = build_ordinary_agent_lifecycle_write_set(
            envelope=enrollment_envelope(policy_record=self.policy, inventory=inventory),
            previous_principal=None,
            previous_credential=None,
            execution_profile="guarded_executor",
            recorded_at="2026-09-08T00:00:00Z",
        )
        self.principal = lifecycle.principal
        assert lifecycle.credential is not None
        self.credential = lifecycle.credential
        self.now = 1_800_000_000
        self.delegation = OrdinaryAgentSessionDelegation(
            operation_id="approved-session-one",
            approval_sha256="a" * 64,
            receiver_sha256="b" * 64,
            actions=("guarded_merge",),
            session_expires_at=self.now + 100,
            lease_expires_at=self.now + 100,
            action_limit=3,
            pull_request_limit=1,
            refresh_allowance=2,
            continuation_expires_at=self.now + 200,
        )
        issued = build_ordinary_agent_session_write_set(
            policy=self.policy,
            principal=self.principal,
            credential=self.credential,
            delegation=self.delegation,
            now=self.now,
        )
        self.session, self.lease = issued.session, issued.leases[0]
        self.request = OrdinaryAgentFiniteRequestRecord(
            request_id="request-one",
            idempotency_key="request-one",
            principal_id=self.principal.principal_id,
            session_id=self.session.session_id,
            lease_id=self.lease.lease_id,
            target=self.lease.target,
            base_sha="a" * 40,
            pull_requests=(OrdinaryAgentPullRequest(number=12, head_sha="b" * 40),),
            permitted_stack_edit_pull_requests=(),
            refresh_allowance_total=2,
            admitted_at=self.now,
            expires_at=self.now + 100,
            continuation_expires_at=self.now + 200,
        )

    def test_last_charged_action_keeps_current_authority_but_cannot_spend_again(self) -> None:
        lease = self.lease.model_copy(
            update={"budget": self.lease.budget.model_copy(update={"actions_used": 3})}
        )
        require_ordinary_agent_current_job_authority(
            policy=self.policy,
            principal=self.principal,
            credential=self.credential,
            session=self.session,
            lease=lease,
            request=self.request,
            now=self.now + 1,
        )
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "budget_exhausted"):
            require_ordinary_agent_finite_job_authority(
                policy=self.policy,
                principal=self.principal,
                credential=self.credential,
                session=self.session,
                lease=lease,
                request=self.request,
                now=self.now + 1,
            )
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "session_revoked"):
            require_ordinary_agent_current_job_authority(
                policy=self.policy,
                principal=self.principal,
                credential=self.credential,
                session=self.session.model_copy(update={"revoked_at": self.now}),
                lease=lease,
                request=self.request,
                now=self.now + 1,
            )

    def test_admission_spends_once_and_existing_job_retains_original_grant(self) -> None:
        admitted = build_ordinary_agent_request_admission_write_set(
            policy=self.policy,
            principal=self.principal,
            credential=self.credential,
            session=self.session,
            lease=self.lease,
            request=self.request,
            now=self.now,
        )
        self.assertEqual(admitted.lease.budget.pull_requests_used, 1)
        self.assertEqual(admitted.lease.budget.actions_used, 0)
        for when in (self.now + 1, self.now + 150):
            require_ordinary_agent_finite_job_authority(
                policy=self.policy,
                principal=self.principal,
                credential=self.credential,
                session=self.session,
                lease=admitted.lease,
                request=self.request,
                now=when,
            )
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "budget_exhausted"):
            build_ordinary_agent_request_admission_write_set(
                policy=self.policy,
                principal=self.principal,
                credential=self.credential,
                session=self.session,
                lease=admitted.lease,
                request=self.request.model_copy(update={"request_id": "request-two"}),
                now=self.now,
            )
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "session_expired"):
            build_ordinary_agent_request_admission_write_set(
                policy=self.policy,
                principal=self.principal,
                credential=self.credential,
                session=self.session,
                lease=self.lease,
                request=self.request.model_copy(update={"admitted_at": self.now + 100}),
                now=self.now + 100,
            )

    def test_continuation_expires_and_cannot_survive_revocation_or_rotation(self) -> None:
        for session, credential, principal, request, now, expected in (
            (
                self.session,
                self.credential,
                self.principal,
                self.request,
                self.now + 200,
                "finite_job_expired",
            ),
            (
                self.session.model_copy(update={"revoked_at": self.now + 1}),
                self.credential,
                self.principal,
                self.request,
                self.now + 150,
                "session_revoked",
            ),
            (
                self.session,
                self.credential.model_copy(update={"credential_version": 2}),
                self.principal,
                self.request,
                self.now + 150,
                "credential_binding_mismatch",
            ),
            (
                self.session,
                self.credential,
                self.principal.model_copy(update={"status": "revoked"}),
                self.request,
                self.now + 150,
                "principal_revoked",
            ),
            (
                self.session,
                self.credential,
                self.principal,
                self.request.model_copy(update={"continuation_expires_at": None}),
                self.now + 150,
                "finite_job_expired",
            ),
        ):
            with (
                self.subTest(expected=expected),
                self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, expected),
            ):
                require_ordinary_agent_finite_job_authority(
                    policy=self.policy,
                    principal=principal,
                    credential=credential,
                    session=session,
                    lease=self.lease,
                    request=request,
                    now=now,
                )

    def test_distinct_approved_operations_allow_concurrent_sessions(self) -> None:
        other = build_ordinary_agent_session_write_set(
            policy=self.policy,
            principal=self.principal,
            credential=self.credential,
            delegation=self.delegation.model_copy(update={"operation_id": "approved-session-two"}),
            now=self.now,
        )
        self.assertNotEqual(other.session.session_id, self.session.session_id)

    def test_rebind_is_bounded_and_cannot_expand_pr_scope(self) -> None:
        first = rebind_ordinary_agent_finite_request(
            request=self.request,
            base_sha="c" * 40,
            pull_requests=self.request.pull_requests,
            expected_binding_revision=1,
        )
        self.assertEqual(first.scope_sha256, self.request.scope_sha256)
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "binding_revision_conflict"
        ):
            rebind_ordinary_agent_finite_request(
                request=first,
                base_sha="d" * 40,
                pull_requests=first.pull_requests,
                expected_binding_revision=1,
            )
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "refresh_scope_changed"):
            rebind_ordinary_agent_finite_request(
                request=first,
                base_sha="d" * 40,
                pull_requests=(OrdinaryAgentPullRequest(number=99, head_sha="b" * 40),),
                expected_binding_revision=2,
            )
        second = rebind_ordinary_agent_finite_request(
            request=first,
            base_sha="d" * 40,
            pull_requests=first.pull_requests,
            expected_binding_revision=2,
        )
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "refresh_allowance_exhausted"
        ):
            rebind_ordinary_agent_finite_request(
                request=second,
                base_sha="e" * 40,
                pull_requests=second.pull_requests,
                expected_binding_revision=3,
            )

    def test_cancellation_retains_unknown_effect_fence(self) -> None:
        unknown = self.request.model_copy(
            update={
                "status": "reconciliation_required",
                "execution_record_ids": ("landing-attempt-one",),
            }
        )
        cancelled = cancel_ordinary_agent_finite_request(request=unknown, now=self.now + 1)
        self.assertEqual(cancelled.status, "reconciliation_required")
        self.assertEqual(cancelled.execution_record_ids, unknown.execution_record_ids)
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "job_not_dispatchable"):
            require_ordinary_agent_finite_job_authority(
                policy=self.policy,
                principal=self.principal,
                credential=self.credential,
                session=self.session,
                lease=self.lease,
                request=cancelled,
                now=self.now + 2,
            )

    def test_current_rule_change_invalidates_admitted_job(self) -> None:
        policy = self.policy.model_copy(
            update={"policy": self.policy.policy.model_copy(update={"ordinary_agents": ()})}
        )
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "bound_rule_missing"):
            require_ordinary_agent_finite_job_authority(
                policy=policy,
                principal=self.principal,
                credential=self.credential,
                session=self.session,
                lease=self.lease,
                request=self.request,
                now=self.now + 150,
            )
