from __future__ import annotations

from datetime import datetime, timezone, timedelta
from dataclasses import replace
from pathlib import Path
from typing import Any
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from sqlalchemy import select

from control_plane.contracts.ordinary_agent import OrdinaryAgentPullRequest
from control_plane.contracts.ordinary_agent_lifecycle import (
    OrdinaryAgentEnrollApplyEnvelope,
    OrdinaryAgentEnrollmentIntent,
)
from control_plane.ordinary_agent_session_approval import (
    approve_existing_ordinary_agent_session,
    cancel_pending_ordinary_agent_operation,
    revoke_ordinary_agent_session,
    disconnect_ordinary_agent_principal,
    approve_ordinary_agent_enrollment,
    read_human_ordinary_agent_session_operation,
)
from control_plane.service_human_auth import HumanSessionManager, GitHubOAuthConfig
from control_plane.service_auth import GitHubHumanIdentity, TerminalAgentIdentity
from control_plane.contracts.ordinary_agent_session_lifecycle import (
    OrdinaryAgentSessionAttenuation,
    OrdinaryAgentSessionDelegation,
    OrdinaryAgentFiniteRequestRecord,
)
from control_plane.ordinary_agent_authentication import parse_ordinary_agent_token
from control_plane.ordinary_agent_session_lifecycle import OrdinaryAgentSessionAdmissionDenied
from control_plane.storage.postgres import (
    PostgresRecordStore,
    LaunchplaneOrdinaryAgentFiniteRequestRow,
    LaunchplaneOrdinaryAgentSessionRow,
)
from tests.support.ordinary_agent_lifecycle import (
    ADMIN_GITHUB_ID,
    setup_ordinary_agent_authority,
    replace_policy_without_ordinary_agent_rule,
    enrollment_envelope,
    enrollment_mutation,
    prepare_test_issuance,
    prepare_approved_test_issuance,
    revocation_envelope,
    rotation_envelope,
    apply_test_enrollment,
)


class OrdinaryAgentSessionStorageTests(unittest.TestCase):
    def setUp(self, *, pull_request_limit: int = 1) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(directory.name) / 'db.sqlite3'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.prepare_store(self.store, pull_request_limit=pull_request_limit)

    def prepare_store(self, store: PostgresRecordStore, *, pull_request_limit: int = 1) -> None:
        self.store = store
        self.now = int(datetime.now(timezone.utc).timestamp())
        self.clock = self.enterContext(
            patch.object(
                self.store,
                "_database_mutation_timestamp",
                return_value=datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
            )
        )
        self.policy, inventory = setup_ordinary_agent_authority(self.store)
        envelope = enrollment_envelope(policy_record=self.policy, inventory=inventory)
        self.manager = HumanSessionManager(
            config=GitHubOAuthConfig(
                client_id="test",
                client_secret="test",
                public_url="https://example.test",
                session_secret="test-session-secret",
            ),
            session_store=self.store,
            now=lambda: datetime.fromtimestamp(self.now, timezone.utc),
        )
        self.human = self.manager.issue(
            GitHubHumanIdentity(
                login="test-admin",
                github_id=ADMIN_GITHUB_ID,
                name="Test",
                email="test@example.test",
                organizations=frozenset(),
                teams=frozenset(),
                role="admin",
            )
        )
        self.delegation = OrdinaryAgentSessionDelegation(
            operation_id=envelope.operation_id,
            approval_sha256=envelope.approval_sha256,
            receiver_sha256=envelope.delivery.receiver_claim_sha256,
            actions=("guarded_merge",),
            session_expires_at=self.now + 100,
            lease_expires_at=self.now + 100,
            action_limit=4,
            pull_request_limit=pull_request_limit,
            refresh_allowance=1,
            continuation_expires_at=self.now + 200,
        )
        self.attenuation = OrdinaryAgentSessionAttenuation.model_validate(
            self.delegation.model_dump(
                exclude={"operation_id", "approval_sha256", "receiver_sha256"}
            )
        )
        intent = OrdinaryAgentEnrollmentIntent.from_envelope(
            envelope.model_copy(update={"session_attenuation": self.attenuation})
        )
        self.store.propose_ordinary_agent_enrollment(
            intent=intent, requester=TerminalAgentIdentity(subject="test-cli", token_label="test")
        )
        approved = approve_ordinary_agent_enrollment(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(self.human),
            csrf_token=self.manager.csrf_token(self.human),
            principal_id=intent.principal_id,
            operation_id=intent.operation_id,
        )
        self.envelope, self.bundle = prepare_approved_test_issuance(approved)
        assert isinstance(self.envelope, OrdinaryAgentEnrollApplyEnvelope)
        self.proof = parse_ordinary_agent_token(self.bundle.token.value)

    def enroll(self) -> None:
        result = self.store.compare_and_apply_ordinary_agent_enrollment(
            envelope=self.envelope,
            mutation=enrollment_mutation(self.envelope),
            issuance=self.bundle,
        )
        self.assertEqual(result.status, "written")
        self.issued = self.store.reconnect_ordinary_agent_session(
            proof=self.proof, operation_id=self.envelope.operation_id
        )
        self.request = OrdinaryAgentFiniteRequestRecord(
            request_id="finite-request-one",
            idempotency_key="request-one",
            principal_id=self.issued.session.principal_id,
            session_id=self.issued.session.session_id,
            lease_id=self.issued.leases[0].lease_id,
            target=self.issued.leases[0].target,
            base_sha="a" * 40,
            pull_requests=(OrdinaryAgentPullRequest(number=12, head_sha="b" * 40),),
            permitted_stack_edit_pull_requests=(),
            refresh_allowance_total=1,
            admitted_at=self.now,
            expires_at=self.now + 100,
            continuation_expires_at=self.now + 200,
        )

    def test_atomic_enrollment_replay_and_budgeted_request_replay(self) -> None:
        self.enroll()
        retry = self.store.compare_and_apply_ordinary_agent_enrollment(
            envelope=self.envelope, mutation=enrollment_mutation(self.envelope)
        )
        self.assertEqual(retry.status, "replayed")
        original = self.store.admit_ordinary_agent_finite_request(
            proof=self.proof, request=self.request
        )
        replay = self.store.admit_ordinary_agent_finite_request(
            proof=self.proof, request=self.request
        )
        self.assertEqual(replay, original)
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "idempotency_conflict"):
            self.store.admit_ordinary_agent_finite_request(
                proof=self.proof, request=self.request.model_copy(update={"base_sha": "c" * 40})
            )
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "budget_exhausted"):
            self.store.admit_ordinary_agent_finite_request(
                proof=self.proof,
                request=self.request.model_copy(
                    update={"request_id": "finite-request-two", "idempotency_key": "request-two"}
                ),
            )
        self.clock.return_value = datetime.fromtimestamp(self.now + 150, timezone.utc).isoformat()
        self.assertEqual(
            self.store.reauthorize_ordinary_agent_finite_job(request_id=original.request_id),
            original,
        )
        self.assertEqual(
            self.store.reconnect_ordinary_agent_session(
                proof=self.proof, operation_id=self.envelope.operation_id
            ).session,
            self.issued.session,
        )
        self.clock.return_value = datetime.fromtimestamp(self.now + 200, timezone.utc).isoformat()
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "finite_job_expired"):
            self.store.reauthorize_ordinary_agent_finite_job(request_id=original.request_id)

    def test_failure_after_session_write_rolls_back_enrollment_and_retry_succeeds(self) -> None:
        with patch.object(
            self.store,
            "_after_ordinary_agent_enrollment_write_step",
            side_effect=lambda step: (
                (_ for _ in ()).throw(RuntimeError("injected"))
                if step == "insert_session"
                else None
            ),
        ):
            with self.assertRaisesRegex(RuntimeError, "injected"):
                self.enroll()
        self.assertIsNone(
            self.store.read_current_ordinary_agent_principal(principal_id="agent_one")
        )
        with self.store._session_factory() as session:
            self.assertIsNone(session.scalar(select(LaunchplaneOrdinaryAgentSessionRow)))
        self.enroll()

    def test_revocation_cancels_jobs_and_invalidates_current_authentication(self) -> None:
        self.enroll()
        self.store.admit_ordinary_agent_finite_request(proof=self.proof, request=self.request)
        principal = self.store.read_current_ordinary_agent_principal(principal_id="agent_one")
        assert principal is not None
        assert isinstance(self.envelope, OrdinaryAgentEnrollApplyEnvelope)
        revoke = revocation_envelope(
            enrolled=self.envelope,
            principal_record_id=principal.record_id,
            principal_revision=principal.principal_revision,
            principal_sha256=principal.record_sha256,
        )
        result = apply_test_enrollment(
            self.store, envelope=revoke, mutation=enrollment_mutation(revoke)
        )
        self.assertEqual(result.status, "written")
        self.assertIsNone(self.store.verify_ordinary_agent_token(self.proof))
        with self.store._session_factory() as session:
            job = session.get(LaunchplaneOrdinaryAgentFiniteRequestRow, self.request.request_id)
            assert job is not None
            self.assertEqual(job.payload["status"], "cancelled")
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "credential_unavailable"):
            self.store.reauthorize_ordinary_agent_finite_job(request_id=self.request.request_id)

    def test_fresh_session_uses_stored_proposal_without_bearer_in_browser(self) -> None:
        self.enroll()
        operation_id = (
            self.store.propose_ordinary_agent_session(
                proof=self.proof, operation_id="fresh-session-one", attenuation=self.attenuation
            )
        ).operation_id
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "administrator_authentication_failed"
        ):
            approve_existing_ordinary_agent_session(
                store=self.store,
                manager=self.manager,
                cookie_header="forged",
                csrf_token=self.manager.csrf_token(self.human),
                principal_id="agent_one",
                operation_id=operation_id,
            )
        fresh = approve_existing_ordinary_agent_session(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(self.human),
            csrf_token=self.manager.csrf_token(self.human),
            principal_id="agent_one",
            operation_id=operation_id,
        )
        self.assertNotEqual(fresh.session.session_id, self.issued.session.session_id)
        self.assertEqual(fresh.session.credential_version, self.issued.session.credential_version)
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "idempotency_conflict"):
            self.store.propose_ordinary_agent_session(
                proof=self.proof,
                operation_id=operation_id,
                attenuation=self.attenuation.model_copy(update={"pull_request_limit": 2}),
            )
        self.store.cancel_ordinary_agent_session(
            proof=self.proof, session_id=fresh.session.session_id
        )
        replay = approve_existing_ordinary_agent_session(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(self.human),
            csrf_token=self.manager.csrf_token(self.human),
            principal_id="agent_one",
            operation_id=operation_id,
        )
        self.assertIsNotNone(replay.session.revoked_at)
        self.assertEqual(replay.session.expires_at, fresh.session.expires_at)
        self.assertIsNone(
            self.store.reconnect_ordinary_agent_session(
                proof=self.proof, operation_id=self.envelope.operation_id
            ).session.revoked_at
        )

    def test_initial_attenuation_cannot_be_attached_or_changed_after_approval(self) -> None:
        changed = self.envelope.model_copy(
            update={
                "session_attenuation": self.attenuation.model_copy(update={"pull_request_limit": 2})
            }
        )
        prepared, bundle = prepare_test_issuance(changed)
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "delegation_approval_binding_mismatch"
        ):
            self.store.compare_and_apply_ordinary_agent_enrollment(
                envelope=prepared, mutation=enrollment_mutation(prepared), issuance=bundle
            )
        self.assertIsNone(
            self.store.read_current_ordinary_agent_principal(principal_id="agent_one")
        )
        self.enroll()

    def test_logged_out_administrator_cannot_approve_saved_proposal(self) -> None:
        self.enroll()
        operation = (
            self.store.propose_ordinary_agent_session(
                proof=self.proof, operation_id="fresh-session-one", attenuation=self.attenuation
            )
        ).operation_id
        cookie, csrf = (
            self.manager.session_cookie_header(self.human),
            self.manager.csrf_token(self.human),
        )
        self.store.delete_session(self.human.session_id)
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "administrator_authentication_failed"
        ):
            approve_existing_ordinary_agent_session(
                store=self.store,
                manager=self.manager,
                cookie_header=cookie,
                csrf_token=csrf,
                principal_id="agent_one",
                operation_id=operation,
            )

    def test_rotation_cancels_old_session_but_preserves_unknown_effect_fence(self) -> None:
        self.enroll()
        self.store.admit_ordinary_agent_finite_request(proof=self.proof, request=self.request)
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneOrdinaryAgentFiniteRequestRow, self.request.request_id)
            assert row is not None
            row.payload = {
                **row.payload,
                "status": "reconciliation_required",
                "execution_record_ids": ["landing-one"],
            }
            session.commit()
        principal = self.store.read_current_ordinary_agent_principal(principal_id="agent_one")
        assert principal is not None and isinstance(self.envelope, OrdinaryAgentEnrollApplyEnvelope)
        rotated = rotation_envelope(
            enrolled=self.envelope,
            principal_record_id=principal.record_id,
            principal_revision=principal.principal_revision,
            principal_sha256=principal.record_sha256,
            custody_record_id=principal.custody_record_id or "",
            custody_sha256=principal.custody_sha256 or "",
        )
        rotated = rotated.model_copy(update={"session_attenuation": None})
        result = apply_test_enrollment(
            self.store, envelope=rotated, mutation=enrollment_mutation(rotated)
        )
        self.assertEqual(result.status, "written")
        self.assertIsNone(self.store.verify_ordinary_agent_token(self.proof))
        with self.store._session_factory() as session:
            row = session.get(LaunchplaneOrdinaryAgentFiniteRequestRow, self.request.request_id)
            assert row is not None
            self.assertEqual(row.payload["status"], "reconciliation_required")
            self.assertEqual(row.payload["execution_record_ids"], ["landing-one"])
            self.assertIsNotNone(row.payload["cancellation_requested_at"])

    def test_failed_request_write_rolls_back_budget_before_retry(self) -> None:
        self.enroll()

        def fail(step: str) -> None:
            if step == "admit_finite_request":
                raise RuntimeError("injected admission failure")

        with patch.object(
            self.store, "_after_ordinary_agent_enrollment_write_step", side_effect=fail
        ):
            with self.assertRaisesRegex(RuntimeError, "injected admission failure"):
                self.store.admit_ordinary_agent_finite_request(
                    proof=self.proof, request=self.request
                )
        self.assertEqual(
            self.store.admit_ordinary_agent_finite_request(
                proof=self.proof, request=self.request
            ).request_id,
            self.request.request_id,
        )

    def test_no_session_enrollment_uses_stored_approval_without_creating_lease(self) -> None:
        plain = self.envelope.model_copy(
            update={"operation_id": "plain-enrollment", "session_attenuation": None}
        )
        self.store.propose_ordinary_agent_enrollment(
            requester=TerminalAgentIdentity(subject="test-cli", token_label="test"),
            intent=OrdinaryAgentEnrollmentIntent.from_envelope(plain),
        )
        approved = approve_ordinary_agent_enrollment(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(self.human),
            csrf_token=self.manager.csrf_token(self.human),
            principal_id=plain.principal_id,
            operation_id=plain.operation_id,
        )
        worker_input = self.store.read_approved_ordinary_agent_enrollment(
            principal_id=plain.principal_id, operation_id=plain.operation_id
        )
        self.assertEqual(worker_input, approved)
        prepared, bundle = prepare_approved_test_issuance(worker_input)
        result = self.store.apply_approved_ordinary_agent_enrollment(
            envelope=prepared, mutation=enrollment_mutation(prepared), issuance=bundle
        )
        self.assertEqual(result.status, "written")
        self.assertIsNotNone(
            self.store.verify_ordinary_agent_token(parse_ordinary_agent_token(bundle.token.value))
        )
        with self.store._session_factory() as session:
            self.assertIsNone(session.scalar(select(LaunchplaneOrdinaryAgentSessionRow)))
        view = read_human_ordinary_agent_session_operation(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(self.human),
            principal_id=plain.principal_id,
            operation_id=plain.operation_id,
        )
        self.assertTrue(view.applied)
        self.assertIsNone(view.attenuation)
        self.assertIsNone(view.session_id)
        self.assertFalse(view.can_approve)
        disconnect_ordinary_agent_principal(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(self.human),
            csrf_token=self.manager.csrf_token(self.human),
            principal_id=plain.principal_id,
            source_event_id="disconnect-no-session",
        )
        self.assertIsNone(
            self.store.verify_ordinary_agent_token(parse_ordinary_agent_token(bundle.token.value))
        )

    def test_operation_projection_never_exposes_private_proof_or_revives_session(self) -> None:
        self.enroll()
        operation = (
            self.store.propose_ordinary_agent_session(
                proof=self.proof, operation_id="fresh-session-one", attenuation=self.attenuation
            )
        ).operation_id
        view = read_human_ordinary_agent_session_operation(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(self.human),
            principal_id="agent_one",
            operation_id=operation,
        )
        self.assertTrue(view.can_approve)
        public = view.model_dump_json()
        self.assertNotIn("credential_digest", public)
        self.assertNotIn("receiver_sha256", public)
        self.assertNotIn("approval_sha256", public)
        self.assertNotIn(self.bundle.token.value, public)
        fresh = approve_existing_ordinary_agent_session(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(self.human),
            csrf_token=self.manager.csrf_token(self.human),
            principal_id="agent_one",
            operation_id=operation,
        )
        self.store.cancel_ordinary_agent_session(
            proof=self.proof, session_id=fresh.session.session_id
        )
        historical = self.store.read_ordinary_agent_session_operation(
            proof=self.proof, operation_id=operation
        )
        self.assertEqual(historical.status, "revoked")
        self.assertFalse(historical.can_approve)
        self.assertEqual(historical.session_id, fresh.session.session_id)

    def test_cancel_approved_unapplied_intent_fences_worker_and_apply(self) -> None:
        view = cancel_pending_ordinary_agent_operation(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(self.human),
            csrf_token=self.manager.csrf_token(self.human),
            principal_id=self.envelope.principal_id,
            operation_id=self.envelope.operation_id,
        )
        self.assertEqual(view.status, "cancelled")
        self.assertFalse(view.can_approve)
        self.assertEqual(self.store.list_pending_approved_ordinary_agent_enrollments(), ())
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "operation_cancelled"):
            self.store.read_approved_ordinary_agent_enrollment(
                principal_id=self.envelope.principal_id, operation_id=self.envelope.operation_id
            )
        with self.assertRaisesRegex(OrdinaryAgentSessionAdmissionDenied, "delegation_approval"):
            self.enroll()
        self.assertIsNone(
            self.store.read_current_ordinary_agent_principal(principal_id="agent_one")
        )

    def test_admin_revoke_and_disconnect_work_after_ordinary_rule_removed(self) -> None:
        self.enroll()
        self.store.admit_ordinary_agent_finite_request(proof=self.proof, request=self.request)
        replace_policy_without_ordinary_agent_rule(self.store, current=self.policy)
        view = revoke_ordinary_agent_session(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(self.human),
            csrf_token=self.manager.csrf_token(self.human),
            principal_id="agent_one",
            session_id=self.request.session_id,
        )
        self.assertEqual(view.status, "revoked")
        self.assertIsNotNone(self.store.verify_ordinary_agent_token(self.proof))
        with self.store._session_factory() as session:
            job = session.get(LaunchplaneOrdinaryAgentFiniteRequestRow, self.request.request_id)
            assert job is not None
            self.assertEqual(job.payload["status"], "cancelled")
        disconnected_records = []
        for _ in range(2):
            result = disconnect_ordinary_agent_principal(
                store=self.store,
                manager=self.manager,
                cookie_header=self.manager.session_cookie_header(self.human),
                csrf_token=self.manager.csrf_token(self.human),
                principal_id="agent_one",
                source_event_id="disconnect-test",
            )
            self.assertEqual(result.status, "revoked")
            disconnected_records.append(
                self.store.read_current_ordinary_agent_principal(principal_id="agent_one")
            )
        self.assertEqual(disconnected_records[0], disconnected_records[1])
        self.assertIsNone(self.store.verify_ordinary_agent_token(self.proof))
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "operation_already_applied"
        ):
            cancel_pending_ordinary_agent_operation(
                store=self.store,
                manager=self.manager,
                cookie_header=self.manager.session_cookie_header(self.human),
                csrf_token=self.manager.csrf_token(self.human),
                principal_id="agent_one",
                operation_id=self.envelope.operation_id,
            )

    def test_human_precision_and_expiry_after_principal_wait(self) -> None:
        human = replace(
            self.human, created_at=self.human.created_at + timedelta(microseconds=500000)
        )
        self.store.write_session(human)
        precise_now = datetime.fromtimestamp(self.now, timezone.utc) + timedelta(
            microseconds=750000
        )
        self.enterContext(patch.object(self.manager, "_now", return_value=precise_now))
        self.clock.return_value = precise_now.isoformat()
        view = read_human_ordinary_agent_session_operation(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(human),
            principal_id=self.envelope.principal_id,
            operation_id=self.envelope.operation_id,
        )
        self.assertEqual(view.requester_subject, "test-cli")
        self.assertEqual(view.requester_kind, "terminal_agent")
        self.assertEqual(
            view.credential_expires_at, self.envelope.authentication_credential.expires_at
        )
        lock = self.store._lock_ordinary_agent_principal

        def wait(session: object, *, principal_id: str) -> None:
            lock(session, principal_id=principal_id)
            self.clock.return_value = datetime.fromtimestamp(
                self.now + 86400, timezone.utc
            ).isoformat()

        with patch.object(self.store, "_lock_ordinary_agent_principal", side_effect=wait):
            with self.assertRaisesRegex(
                OrdinaryAgentSessionAdmissionDenied, "administrator_session_changed"
            ):
                cancel_pending_ordinary_agent_operation(
                    store=self.store,
                    manager=self.manager,
                    cookie_header=self.manager.session_cookie_header(human),
                    csrf_token=self.manager.csrf_token(human),
                    principal_id=self.envelope.principal_id,
                    operation_id=self.envelope.operation_id,
                )

    def test_other_principal_audit_does_not_mark_proposal_applied(self) -> None:
        self.enroll()
        intent = OrdinaryAgentEnrollmentIntent.from_envelope(self.envelope)
        other = intent.model_copy(
            update={
                "principal_id": "agent_two",
                "custody": intent.custody.model_copy(update={"principal_id": "agent_two"}),
            }
        )
        self.store.propose_ordinary_agent_enrollment(
            intent=other,
            requester=TerminalAgentIdentity(subject="second-cli", token_label="second"),
        )
        view = read_human_ordinary_agent_session_operation(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(self.human),
            principal_id="agent_two",
            operation_id=other.operation_id,
        )
        self.assertFalse(view.applied)
        self.assertEqual(view.status, "blocked")
        self.assertEqual(view.requester_subject, "second-cli")

    def test_approved_enrollment_wrapper_rejects_revoke(self) -> None:
        self.enroll()
        principal = self.store.read_current_ordinary_agent_principal(principal_id="agent_one")
        assert principal is not None
        assert isinstance(self.envelope, OrdinaryAgentEnrollApplyEnvelope)
        envelope = revocation_envelope(
            enrolled=self.envelope,
            principal_record_id=principal.record_id,
            principal_revision=principal.principal_revision,
            principal_sha256=principal.record_sha256,
        )
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "enrollment_operation_required"
        ):
            self.store.apply_approved_ordinary_agent_enrollment(
                envelope=envelope, mutation=enrollment_mutation(envelope)
            )
        self.assertIsNotNone(self.store.verify_ordinary_agent_token(self.proof))

    def test_recovery_discovery_and_terminal_status_preserve_requester_scope(self) -> None:
        references = self.store.list_pending_approved_ordinary_agent_enrollments()
        self.assertEqual(
            [(row.principal_id, row.operation_id) for row in references],
            [(self.envelope.principal_id, self.envelope.operation_id)],
        )
        view = self.store.read_proposed_ordinary_agent_enrollment(
            requester=TerminalAgentIdentity(subject="test-cli", token_label="test"),
            principal_id=self.envelope.principal_id,
            operation_id=self.envelope.operation_id,
        )
        self.assertFalse(view.can_approve)
        self.assertEqual(view.status, "approved")
        with self.assertRaisesRegex(
            OrdinaryAgentSessionAdmissionDenied, "session_proposal_unavailable"
        ):
            self.store.read_proposed_ordinary_agent_enrollment(
                requester=TerminalAgentIdentity(subject="different-cli", token_label="test"),
                principal_id=self.envelope.principal_id,
                operation_id=self.envelope.operation_id,
            )
        self.enroll()
        self.assertEqual(self.store.list_pending_approved_ordinary_agent_enrollments(), ())

    def test_disconnect_rechecks_actual_human_after_preparation(self) -> None:
        self.enroll()
        apply = self.store.compare_and_apply_ordinary_agent_enrollment

        def logout(**kwargs: Any) -> object:
            self.store.delete_session(self.human.session_id)
            return apply(**kwargs)

        with patch.object(
            self.store, "compare_and_apply_ordinary_agent_enrollment", side_effect=logout
        ):
            with self.assertRaisesRegex(
                OrdinaryAgentSessionAdmissionDenied, "administrator_session_unavailable"
            ):
                disconnect_ordinary_agent_principal(
                    store=self.store,
                    manager=self.manager,
                    cookie_header=self.manager.session_cookie_header(self.human),
                    csrf_token=self.manager.csrf_token(self.human),
                    principal_id="agent_one",
                    source_event_id="disconnect-logout-test",
                )
        self.assertIsNotNone(self.store.verify_ordinary_agent_token(self.proof))

    def test_recovery_keyset_advances_past_unapplied_operations(self) -> None:
        intent = OrdinaryAgentEnrollmentIntent.from_envelope(self.envelope).model_copy(
            update={"operation_id": "z-recovery-second"}
        )
        self.store.propose_ordinary_agent_enrollment(
            intent=intent, requester=TerminalAgentIdentity(subject="test-cli", token_label="test")
        )
        approve_ordinary_agent_enrollment(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(self.human),
            csrf_token=self.manager.csrf_token(self.human),
            principal_id=intent.principal_id,
            operation_id=intent.operation_id,
        )
        first = self.store.list_pending_approved_ordinary_agent_enrollments(limit=1)
        second = self.store.list_pending_approved_ordinary_agent_enrollments(
            limit=1, after=first[0]
        )
        self.assertEqual(second[0].operation_id, intent.operation_id)
        self.assertEqual(
            self.store.list_pending_approved_ordinary_agent_enrollments(limit=1, after=second[0]),
            (),
        )
        self.assertNotEqual(first, second)

    def test_disconnect_event_cannot_impersonate_pending_enrollment(self) -> None:
        self.enroll()
        pending = OrdinaryAgentEnrollmentIntent.from_envelope(self.envelope).model_copy(
            update={"operation_id": "pending-enrollment-event"}
        )
        self.store.propose_ordinary_agent_enrollment(
            intent=pending, requester=TerminalAgentIdentity(subject="test-cli", token_label="test")
        )
        disconnect_ordinary_agent_principal(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(self.human),
            csrf_token=self.manager.csrf_token(self.human),
            principal_id=pending.principal_id,
            source_event_id=pending.operation_id,
        )
        view = cancel_pending_ordinary_agent_operation(
            store=self.store,
            manager=self.manager,
            cookie_header=self.manager.session_cookie_header(self.human),
            csrf_token=self.manager.csrf_token(self.human),
            principal_id=pending.principal_id,
            operation_id=pending.operation_id,
        )
        self.assertFalse(view.applied)
        self.assertEqual(view.status, "cancelled")
