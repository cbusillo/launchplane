from __future__ import annotations

import base64
from datetime import datetime
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from typing import Literal
import unittest
from unittest.mock import patch

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from pydantic import ValidationError
from sqlalchemy import text

from control_plane.authz_grant_service import AuthzManagedPolicyDiff
from control_plane.contracts.owner_control import (
    ApprovalRequest,
    ChannelBindingRecord,
    ChallengeResponse,
    OwnerControlConfirmationEnvelope,
    ReviewItem,
    ServerReviewPayload,
    owner_control_approval_request_digest,
    owner_control_channel_binding_sha256,
    owner_control_signature_payload_bytes,
)
from control_plane.contracts.owner_control_shadow_verifier import (
    OwnerControlChallengeIssueRequest,
    evaluate_owner_control_shadow_verification,
    owner_control_challenge_lifecycle_event_id,
    terminalize_expired_owner_control_challenge_record,
)
from control_plane.contracts.owner_control_enrollment_provenance import (
    OwnerControlEnrollmentProvenanceConflictError,
    OwnerControlHostPrincipalClaim,
)
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.privileged_operation import (
    ManagedAuthzPolicySetHumanEvidence,
    ManagedAuthzPolicySetProposalInput,
    ManagedSecretReencryptionHumanEvidence,
    ManagedSecretReencryptionPlanInput,
    AUTHZ_POLICY_OPERATION_APPROVE_ACTION,
    PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,
    PrivilegedOperationActor,
    PrivilegedOperationEventRecord,
    PrivilegedOperationRecord,
    privileged_operation_evidence_digest,
    privileged_operation_record_digest,
    privileged_operation_request_digest,
)
from control_plane.owner_control_challenge import (
    OwnerControlChallengeProvenanceError,
    derive_owner_control_approval_request,
)
from control_plane.service_auth import GitHubHumanPolicyRule, LaunchplaneAuthzPolicy
from control_plane.privileged_operation_service import cancel_privileged_operation
from control_plane.storage.postgres import PostgresRecordStore


REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
OWNER_CONTROL_BASE_REVISION = "8c34cb5849edafd8db05f936afe994ac82372087"


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _binding(
    private_key: Ed25519PrivateKey, *, session_id: str = "owner-control-session"
) -> ChannelBindingRecord:
    return ChannelBindingRecord(
        channel_session_id=session_id,
        owner_github_id=100001,
        signature_algorithm="ed25519",
        owner_public_key=_base64url(
            private_key.public_key().public_bytes_raw(),
        ),
        session_issued_at="2025-01-01T00:00:00+00:00",
        session_expires_at="2035-01-01T00:00:00+00:00",
    )


def _host_principal_claim() -> OwnerControlHostPrincipalClaim:
    return OwnerControlHostPrincipalClaim(
        host_instance_id="owner-control-test-host",
        principal_id="owner-control-test-principal",
        principal_separation="not_claimed",
        key_custody="not_claimed",
        gesture_source="not_claimed",
    )


def _request(*, nonce: str = "owner-control-nonce-0000000000000001") -> ApprovalRequest:
    return ApprovalRequest(
        operation_id="privileged-operation-0123456789abcdef0123456789abcdef",
        descriptor_id="managed-secret-reencryption",
        descriptor_version=1,
        request_digest="1" * 64,
        plan_digest="2" * 64,
        evidence_digest="3" * 64,
        pre_state_digest="4" * 64,
        policy_record_id="owner-policy",
        policy_revision=1,
        policy_sha256="5" * 64,
        owner_github_id=100001,
        server_review=ServerReviewPayload(
            review_id="owner-control-review",
            title="Review operation",
            summary="Review the exact server-authored request.",
            items=(ReviewItem(key="operation", label="Operation", value="Re-encrypt"),),
        ),
        nonce=nonce,
        issued_at="2026-01-01T00:00:00+00:00",
        expires_at="2026-01-01T00:01:00+00:00",
    )


def _envelope(
    private_key: Ed25519PrivateKey,
    *,
    binding: ChannelBindingRecord,
    request: ApprovalRequest,
) -> OwnerControlConfirmationEnvelope:
    response = ChallengeResponse(
        approval_request=request,
        approval_request_digest=owner_control_approval_request_digest(request),
        decision="approved",
        channel_binding_sha256=owner_control_channel_binding_sha256(binding),
        confirmed_at=request.issued_at,
    )
    signature = private_key.sign(owner_control_signature_payload_bytes(response))
    return OwnerControlConfirmationEnvelope(
        channel_binding=binding,
        challenge_response=response,
        signature_algorithm="ed25519",
        signature=_base64url(signature),
    )


def _seed_issue_provenance(
    store: PostgresRecordStore,
    *,
    expires_at: str = "2030-01-01T00:00:00+00:00",
    owner_ids: tuple[int, ...] = (100001,),
    roles: tuple[Literal["read_only", "admin"], ...] = (),
    seed_policy: bool = True,
) -> PrivilegedOperationRecord:
    request = ManagedSecretReencryptionPlanInput(reason="Rotate retained keys", source_label="test")
    evidence = ManagedSecretReencryptionHumanEvidence(
        result_status="ok",
        plan_digest="a" * 64,
        configured_secret_count=3,
        rotation_candidate_count=2,
        unchanged_count=1,
        unreadable_secret_count=0,
        active_key_id="redacted-key",
        retirement_blocked_key_ids=(),
        retirement_ready_key_ids=(),
        legacy_compatibility_key_loaded=False,
    )
    actor = PrivilegedOperationActor(identity_type="github_human", github_id=44, login="requester")
    record = PrivilegedOperationRecord(
        operation_id="privileged-operation-0123456789abcdef0123456789abcdef",
        descriptor_id="managed-secret-reencryption",
        descriptor_version=1,
        safety_class="secret_backed",
        status="planned",
        source_event_id="owner-control-test",
        requested_by=actor,
        request=request,
        request_digest=privileged_operation_request_digest(request),
        evidence=evidence,
        evidence_digest=privileged_operation_evidence_digest(evidence),
        created_at="2026-08-28T00:00:00+00:00",
        updated_at="2026-08-28T00:00:00+00:00",
        expires_at=expires_at,
    )
    event = PrivilegedOperationEventRecord(
        operation_id=record.operation_id,
        sequence=1,
        action="planned",
        occurred_at=record.created_at,
        source_kind="browser_api",
        source_event_id=record.source_event_id,
        actor=actor,
        resulting_record_digest=privileged_operation_record_digest(record),
    )
    store.write_privileged_operation_plan(record, event)
    policy_record = _owner_control_policy_record(
        action=PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,
        owner_ids=owner_ids,
        roles=roles,
    )
    if seed_policy:
        store.seed_authz_policy_if_absent(policy_record)
    return record


def _owner_control_policy_record(
    *,
    action: str,
    owner_ids: tuple[int, ...] = (100001,),
    roles: tuple[Literal["read_only", "admin"], ...] = (),
    schema_version: Literal[1, 2] = 2,
) -> LaunchplaneAuthzPolicyRecord:
    policy = LaunchplaneAuthzPolicy(
        schema_version=schema_version,
        github_humans=(
            GitHubHumanPolicyRule(
                managed_set_id="owner-control-tests" if schema_version == 2 else None,
                managed_rule_id="approve" if schema_version == 2 else None,
                github_ids=owner_ids,
                roles=roles,
                products=("launchplane",),
                contexts=("launchplane",),
                actions=(action,),
            ),
        ),
    )
    digest = authz_policy_sha256(policy)
    return LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(revision=1, policy_sha256=digest),
        revision=1,
        status="active",
        source="owner-control-tests",
        updated_at="2026-08-28T00:00:00Z",
        policy_sha256=digest,
        policy=policy,
    )


def _managed_policy_operation(*, blocked: bool) -> PrivilegedOperationRecord:
    desired_policy = LaunchplaneAuthzPolicy(schema_version=2)
    request = ManagedAuthzPolicySetProposalInput(
        managed_set_id="sensitive-managed-set",
        desired_policy=desired_policy,
        reason="private policy request reason",
    )
    diff = AuthzManagedPolicyDiff(
        managed_set_id=request.managed_set_id,
        previous_record_id="previous-policy-record",
        previous_revision=1,
        candidate_revision=2,
        previous_policy_sha256="1" * 64,
        desired_policy_sha256="2" * 64,
        desired_set_sha256="3" * 64,
        plan_sha256="4" * 64,
        changed=True,
        added_rule_count=1,
        adopted_rule_count=2,
        updated_rule_count=3,
        removed_rule_count=4,
        unchanged_rule_count=5,
        policy_safety_blocker_count=1 if blocked else 0,
    )
    evidence = ManagedAuthzPolicySetHumanEvidence(
        result_status="blocked" if blocked else "ok",
        plan_digest=diff.plan_sha256,
        diff=diff,
    )
    actor = PrivilegedOperationActor(identity_type="github_human", github_id=44, login="requester")
    return PrivilegedOperationRecord(
        operation_id="privileged-operation-fedcba9876543210fedcba9876543210",
        descriptor_id="managed-authz-policy-set",
        descriptor_version=1,
        safety_class="policy_admin",
        status="planned",
        source_event_id="owner-control-policy-test",
        requested_by=actor,
        request=request,
        request_digest=privileged_operation_request_digest(request),
        evidence=evidence,
        evidence_digest=privileged_operation_evidence_digest(evidence),
        created_at="2026-08-28T00:00:00+00:00",
        updated_at="2026-08-28T00:00:00+00:00",
        expires_at="2026-08-28T01:00:00+00:00",
    )


class OwnerControlShadowVerifierStorageTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(self.temporary_directory.name) / 'records.sqlite3'}"
        )
        self.store.ensure_schema()

    def tearDown(self) -> None:
        self.store.close()
        self.temporary_directory.cleanup()

    def test_enrollment_is_atomic_idempotent_and_provenance_immutable(self) -> None:
        binding = _binding(Ed25519PrivateKey.generate())
        claim = _host_principal_claim()

        created = self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=claim,
        )
        replayed = self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=claim,
        )
        stored = self.store.read_owner_control_enrollment_provenance(
            channel_session_id=binding.channel_session_id
        )

        self.assertEqual(replayed, created)
        self.assertEqual(stored, created.provenance)
        self.assertEqual(created.session.enrolled_at, created.provenance.enrolled_at)
        changed_claim = claim.model_copy(update={"key_custody": "hardware_backed"})
        with self.assertRaisesRegex(
            OwnerControlEnrollmentProvenanceConflictError,
            "changed immutable provenance",
        ):
            self.store.enroll_owner_control_channel_session(
                binding,
                host_principal_claim=changed_claim,
            )

    def test_enrollment_rejects_synthetic_key_without_persisting_session(self) -> None:
        binding = _binding(Ed25519PrivateKey.from_private_bytes(bytes(range(32))))

        with self.assertRaisesRegex(
            OwnerControlEnrollmentProvenanceConflictError,
            "conformance keys cannot be enrolled",
        ):
            self.store.enroll_owner_control_channel_session(
                binding,
                host_principal_claim=_host_principal_claim(),
            )

        with self.assertRaises(FileNotFoundError):
            self.store.read_owner_control_channel_session(
                channel_session_id=binding.channel_session_id
            )

    def test_challenge_issuance_fails_closed_when_provenance_is_missing(self) -> None:
        binding = _binding(Ed25519PrivateKey.generate())
        self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=_host_principal_claim(),
        )
        operation = _seed_issue_provenance(self.store)
        with self.store._engine.begin() as connection:
            connection.execute(
                text(
                    "delete from launchplane_owner_control_enrollment_provenance "
                    "where channel_session_id = :channel_session_id"
                ),
                {"channel_session_id": binding.channel_session_id},
            )

        with self.assertRaisesRegex(
            OwnerControlEnrollmentProvenanceConflictError,
            "requires enrollment provenance",
        ):
            self.store.issue_owner_control_challenge(
                OwnerControlChallengeIssueRequest(
                    channel_session_id=binding.channel_session_id,
                    operation_id=operation.operation_id,
                    expires_in_seconds=300,
                )
            )

    def test_shadow_verification_fails_closed_when_provenance_is_missing(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(private_key)
        self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=_host_principal_claim(),
        )
        operation = _seed_issue_provenance(self.store)
        issued = self.store.issue_owner_control_challenge(
            OwnerControlChallengeIssueRequest(
                channel_session_id=binding.channel_session_id,
                operation_id=operation.operation_id,
                expires_in_seconds=300,
            )
        )
        envelope = _envelope(
            private_key,
            binding=binding,
            request=issued.approval_request(),
        )
        with self.store._engine.begin() as connection:
            connection.execute(
                text(
                    "delete from launchplane_owner_control_enrollment_provenance "
                    "where channel_session_id = :channel_session_id"
                ),
                {"channel_session_id": binding.channel_session_id},
            )

        with self.assertRaisesRegex(
            OwnerControlEnrollmentProvenanceConflictError,
            "verification requires enrollment provenance",
        ):
            self.store.verify_owner_control_confirmation_shadow(envelope)
        self.assertEqual(
            self.store.list_owner_control_shadow_verification_events(
                challenge_nonce=issued.challenge_nonce
            ),
            (),
        )

    def test_verifies_once_with_exact_server_state_and_persists_shadow_events(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(private_key)
        enrolled = self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=_host_principal_claim(),
        )
        operation = _seed_issue_provenance(self.store)
        issued = self.store.issue_owner_control_challenge(
            OwnerControlChallengeIssueRequest(
                channel_session_id=binding.channel_session_id,
                operation_id=operation.operation_id,
                expires_in_seconds=300,
            )
        )
        envelope = _envelope(
            private_key,
            binding=binding,
            request=issued.approval_request(),
        )

        verified = self.store.verify_owner_control_confirmation_shadow(envelope)
        revoked = self.store.revoke_owner_control_channel_session(
            channel_session_id=binding.channel_session_id
        )
        replayed = self.store.verify_owner_control_confirmation_shadow(envelope)
        stored_challenge = self.store.read_owner_control_issued_challenge(
            challenge_nonce=issued.challenge_nonce
        )
        events = self.store.list_owner_control_shadow_verification_events(
            challenge_nonce=issued.challenge_nonce
        )

        self.assertEqual(enrolled.session.status, "enrolled")
        self.assertEqual(enrolled.session.authority_state, "inert")
        self.assertEqual(enrolled.provenance.provenance_tier, "self_asserted")
        self.assertNotEqual(issued.challenge_nonce, _request().nonce)
        self.assertEqual(issued.state, "issued")
        self.assertEqual(issued.authority_state, "inert")
        self.assertEqual(verified.verification_status, "verified")
        self.assertEqual(verified.resulting_challenge_state, "consumed")
        self.assertFalse(verified.authorizes_execution)
        self.assertEqual(verified.verifier_mode, "shadow")
        self.assertEqual(revoked.status, "revoked")
        self.assertEqual(replayed.verification_status, "rejected")
        self.assertEqual(replayed.rejection_reason, "challenge_replayed")
        self.assertEqual(stored_challenge.state, "consumed")
        self.assertEqual(stored_challenge.attempt_count, 2)
        self.assertIsNotNone(stored_challenge.consumed_at)
        self.assertIsNotNone(stored_challenge.terminal_event_id)
        self.assertEqual(
            sorted(
                (event.sequence, event.verification_status, event.rejection_reason)
                for event in events
            ),
            [(1, "verified", None), (2, "rejected", "challenge_replayed")],
        )
        self.assertTrue(all(event.verifier_mode == "shadow" for event in events))
        self.assertTrue(all(event.authorizes_execution is False for event in events))
        self.assertTrue(all(event.authority_state == "inert" for event in events))

    def test_issue_request_accepts_no_caller_authored_provenance(self) -> None:
        with self.assertRaises(ValidationError):
            OwnerControlChallengeIssueRequest.model_validate(
                {
                    "channel_session_id": "owner-control-session",
                    "operation_id": "privileged-operation-0123456789abcdef0123456789abcdef",
                    "expires_in_seconds": 300,
                    "approval_request": _request().model_dump(mode="json"),
                }
            )

    def test_issuance_is_idempotent_only_for_current_bound_provenance(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(private_key)
        self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=_host_principal_claim(),
        )
        operation = _seed_issue_provenance(self.store)
        issue_request = OwnerControlChallengeIssueRequest(
            channel_session_id=binding.channel_session_id,
            operation_id=operation.operation_id,
            expires_in_seconds=300,
        )
        before = self.store.read_privileged_operation_record(operation.operation_id)
        issued = self.store.issue_owner_control_challenge(issue_request)
        replayed = self.store.issue_owner_control_challenge(issue_request)
        after = self.store.read_privileged_operation_record(operation.operation_id)
        review = issued.approval_request().server_review.model_dump(mode="json")

        self.assertEqual(issued, replayed)
        self.assertEqual(before, after)
        self.assertEqual(issued.approval_request().request_digest, operation.request_digest)
        self.assertEqual(issued.approval_request().plan_digest, operation.evidence.plan_digest)
        self.assertNotIn("redacted-key", str(review))
        self.assertNotIn("Rotate retained keys", str(review))
        self.assertNotIn("requester", str(review))

    def test_issuance_clamps_expiry_and_refuses_ineligible_owner(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(
            private_key,
            session_id="owner-control-clamped-session",
        ).model_copy(update={"session_expires_at": "2026-08-28T12:02:00+00:00"})
        with patch.object(
            self.store,
            "_owner_control_shadow_timestamp",
            return_value="2026-08-28T12:00:00+00:00",
        ):
            self.store.enroll_owner_control_channel_session(
                binding,
                host_principal_claim=_host_principal_claim(),
            )
            operation = _seed_issue_provenance(self.store)
            issued = self.store.issue_owner_control_challenge(
                OwnerControlChallengeIssueRequest(
                    channel_session_id=binding.channel_session_id,
                    operation_id=operation.operation_id,
                    expires_in_seconds=300,
                )
            )
        self.assertEqual(issued.expires_at, binding.session_expires_at)

        second_store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(self.temporary_directory.name) / 'denied.sqlite3'}"
        )
        try:
            second_store.ensure_schema()
            second_store.enroll_owner_control_channel_session(
                _binding(private_key),
                host_principal_claim=_host_principal_claim(),
            )
            denied_operation = _seed_issue_provenance(second_store, owner_ids=(999,))
            with self.assertRaisesRegex(ValueError, "immutable GitHub-ID-only approval rule"):
                second_store.issue_owner_control_challenge(
                    OwnerControlChallengeIssueRequest(
                        channel_session_id="owner-control-session",
                        operation_id=denied_operation.operation_id,
                        expires_in_seconds=300,
                    )
                )
        finally:
            second_store.close()

    def test_issuance_floors_operation_expiry(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(private_key, session_id="owner-control-expiry-session")
        self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=_host_principal_claim(),
        )
        operation = _seed_issue_provenance(
            self.store,
            expires_at="2026-08-28T12:01:30.900000+00:00",
        )
        issue_request = OwnerControlChallengeIssueRequest(
            channel_session_id=binding.channel_session_id,
            operation_id=operation.operation_id,
            expires_in_seconds=300,
        )
        with patch.object(
            self.store,
            "_owner_control_shadow_timestamp",
            return_value="2026-08-28T12:00:00+00:00",
        ):
            issued = self.store.issue_owner_control_challenge(issue_request)
        self.assertEqual(issued.expires_at, "2026-08-28T12:01:30+00:00")

    def test_issuance_terminalizes_expired_active_challenge_before_reissue(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(private_key, session_id="owner-control-stale-session")
        self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=_host_principal_claim(),
        )
        operation = _seed_issue_provenance(
            self.store,
            expires_at="2026-08-28T12:10:00+00:00",
        )
        issue_request = OwnerControlChallengeIssueRequest(
            channel_session_id=binding.channel_session_id,
            operation_id=operation.operation_id,
            expires_in_seconds=60,
        )
        with patch.object(
            self.store,
            "_owner_control_shadow_timestamp",
            return_value="2026-08-28T12:00:00+00:00",
        ):
            issued = self.store.issue_owner_control_challenge(issue_request)
        before = self.store.read_privileged_operation_record(operation.operation_id)
        with patch.object(
            self.store,
            "_owner_control_shadow_timestamp",
            return_value="2026-08-28T12:01:01+00:00",
        ):
            reissued = self.store.issue_owner_control_challenge(issue_request)
        after = self.store.read_privileged_operation_record(operation.operation_id)
        expired = self.store.read_owner_control_issued_challenge(
            challenge_nonce=issued.challenge_nonce
        )
        lifecycle_events = self.store.list_owner_control_challenge_lifecycle_events(
            challenge_nonce=issued.challenge_nonce
        )
        shadow_events = self.store.list_owner_control_shadow_verification_events(
            challenge_nonce=issued.challenge_nonce
        )

        self.assertNotEqual(reissued.challenge_nonce, issued.challenge_nonce)
        self.assertEqual(reissued.state, "issued")
        self.assertEqual(expired.state, "expired")
        self.assertEqual(expired.attempt_count, 0)
        self.assertIsNone(expired.consumed_at)
        self.assertEqual(len(lifecycle_events), 1)
        lifecycle_event = lifecycle_events[0]
        self.assertEqual(lifecycle_event.event_id, expired.terminal_event_id)
        self.assertEqual(lifecycle_event.from_state, "issued")
        self.assertEqual(lifecycle_event.to_state, "expired")
        self.assertEqual(lifecycle_event.transition_reason, "expired")
        self.assertEqual(lifecycle_event.occurred_at, reissued.issued_at)
        self.assertFalse(lifecycle_event.authorizes_execution)
        self.assertEqual(shadow_events, ())
        self.assertEqual(before, after)

        expired_result = self.store.verify_owner_control_confirmation_shadow(
            _envelope(
                private_key,
                binding=binding,
                request=issued.approval_request(),
            )
        )
        expired_after_attempt = self.store.read_owner_control_issued_challenge(
            challenge_nonce=issued.challenge_nonce
        )
        self.assertEqual(expired_result.rejection_reason, "challenge_expired")
        self.assertEqual(expired_after_attempt.attempt_count, 1)
        self.assertEqual(expired_after_attempt.terminal_event_id, lifecycle_event.event_id)

    def test_expiry_terminalization_uses_exact_boundary_and_deterministic_event(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(private_key, session_id="owner-control-boundary-session")
        self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=_host_principal_claim(),
        )
        operation = _seed_issue_provenance(
            self.store,
            expires_at="2026-08-28T12:10:00+00:00",
        )
        with patch.object(
            self.store,
            "_owner_control_shadow_timestamp",
            return_value="2026-08-28T12:00:00+00:00",
        ):
            issued = self.store.issue_owner_control_challenge(
                OwnerControlChallengeIssueRequest(
                    channel_session_id=binding.channel_session_id,
                    operation_id=operation.operation_id,
                    expires_in_seconds=60,
                )
            )

        expired, event = terminalize_expired_owner_control_challenge_record(
            issued,
            observed_at=issued.expires_at,
        )
        self.assertEqual(expired.state, "expired")
        self.assertEqual(expired.attempt_count, issued.attempt_count)
        self.assertEqual(expired.terminal_event_id, event.event_id)
        self.assertEqual(
            event.event_id,
            owner_control_challenge_lifecycle_event_id(
                challenge_id=issued.challenge_id,
                from_state="issued",
                to_state="expired",
                transition_reason="expired",
            ),
        )
        with self.assertRaisesRegex(ValueError, "has not expired"):
            terminalize_expired_owner_control_challenge_record(
                issued,
                observed_at="2026-08-28T12:00:59+00:00",
            )

    def test_reissuance_rolls_back_terminalization_when_replacement_insert_fails(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(private_key, session_id="owner-control-rollback-session")
        self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=_host_principal_claim(),
        )
        operation = _seed_issue_provenance(
            self.store,
            expires_at="2026-08-28T12:10:00+00:00",
        )
        issue_request = OwnerControlChallengeIssueRequest(
            channel_session_id=binding.channel_session_id,
            operation_id=operation.operation_id,
            expires_in_seconds=60,
        )
        with patch.object(
            self.store,
            "_owner_control_shadow_timestamp",
            return_value="2026-08-28T12:00:00+00:00",
        ):
            issued = self.store.issue_owner_control_challenge(issue_request)

        with (
            patch.object(
                self.store,
                "_owner_control_shadow_timestamp",
                return_value="2026-08-28T12:01:01+00:00",
            ),
            patch.object(
                self.store,
                "_owner_control_issued_challenge_row_from_record",
                side_effect=RuntimeError("replacement insert failed"),
            ),
            self.assertRaisesRegex(RuntimeError, "replacement insert failed"),
        ):
            self.store.issue_owner_control_challenge(issue_request)

        unchanged = self.store.read_owner_control_issued_challenge(
            challenge_nonce=issued.challenge_nonce
        )
        self.assertEqual(unchanged.state, "issued")
        self.assertIsNone(unchanged.terminal_event_id)
        self.assertEqual(
            self.store.list_owner_control_challenge_lifecycle_events(
                challenge_nonce=issued.challenge_nonce
            ),
            (),
        )

    def test_reissuance_rolls_back_when_replacement_expires_before_commit(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(private_key, session_id="owner-control-stale-commit-session")
        self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=_host_principal_claim(),
        )
        operation = _seed_issue_provenance(
            self.store,
            expires_at="2026-08-28T12:10:00+00:00",
        )
        issue_request = OwnerControlChallengeIssueRequest(
            channel_session_id=binding.channel_session_id,
            operation_id=operation.operation_id,
            expires_in_seconds=60,
        )
        with patch.object(
            self.store,
            "_owner_control_shadow_timestamp",
            return_value="2026-08-28T12:00:00+00:00",
        ):
            issued = self.store.issue_owner_control_challenge(issue_request)

        with (
            patch.object(
                self.store,
                "_owner_control_shadow_timestamp",
                side_effect=(
                    "2026-08-28T12:01:01+00:00",
                    "2026-08-28T12:02:01+00:00",
                ),
            ),
            self.assertRaisesRegex(ValueError, "expired before issuance committed"),
        ):
            self.store.issue_owner_control_challenge(issue_request)

        unchanged = self.store.read_owner_control_issued_challenge(
            challenge_nonce=issued.challenge_nonce
        )
        self.assertEqual(unchanged.state, "issued")
        self.assertIsNone(unchanged.terminal_event_id)
        self.assertEqual(
            self.store.list_owner_control_challenge_lifecycle_events(
                challenge_nonce=issued.challenge_nonce
            ),
            (),
        )

    def test_issuance_rejects_rules_with_mutable_principal_selectors(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        denied_store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(self.temporary_directory.name) / 'selectors.sqlite3'}"
        )
        try:
            denied_store.ensure_schema()
            denied_store.enroll_owner_control_channel_session(
                _binding(private_key),
                host_principal_claim=_host_principal_claim(),
            )
            operation = _seed_issue_provenance(denied_store, roles=("read_only",))
            with self.assertRaisesRegex(ValueError, "immutable GitHub-ID-only approval rule"):
                denied_store.issue_owner_control_challenge(
                    OwnerControlChallengeIssueRequest(
                        channel_session_id="owner-control-session",
                        operation_id=operation.operation_id,
                        expires_in_seconds=300,
                    )
                )
        finally:
            denied_store.close()

    def test_issuance_requires_planned_operation_and_exactly_one_active_policy(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(private_key, session_id="owner-control-status-session")
        self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=_host_principal_claim(),
        )
        operation = _seed_issue_provenance(self.store)
        cancel_privileged_operation(
            record_store=self.store,
            operation_id=operation.operation_id,
            actor_github_id=44,
            actor_login="requester",
            source_event_id="owner-control-cancel",
            reason="Cancel before owner review",
            now=lambda: datetime.fromisoformat("2026-08-28T00:00:01+00:00"),
        )
        with self.assertRaisesRegex(ValueError, "unexpired planned operation"):
            self.store.issue_owner_control_challenge(
                OwnerControlChallengeIssueRequest(
                    channel_session_id=binding.channel_session_id,
                    operation_id=operation.operation_id,
                    expires_in_seconds=300,
                )
            )

        missing_policy_store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(self.temporary_directory.name) / 'missing-policy.sqlite3'}"
        )
        try:
            missing_policy_store.ensure_schema()
            missing_policy_store.enroll_owner_control_channel_session(
                _binding(private_key, session_id="owner-control-missing-policy-session"),
                host_principal_claim=_host_principal_claim(),
            )
            missing_policy_operation = _seed_issue_provenance(
                missing_policy_store,
                seed_policy=False,
            )
            with self.assertRaisesRegex(ValueError, "exactly one active authorization policy"):
                missing_policy_store.issue_owner_control_challenge(
                    OwnerControlChallengeIssueRequest(
                        channel_session_id="owner-control-missing-policy-session",
                        operation_id=missing_policy_operation.operation_id,
                        expires_in_seconds=300,
                    )
                )
        finally:
            missing_policy_store.close()

    def test_valid_signature_with_changed_request_never_verifies(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(private_key)
        self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=_host_principal_claim(),
        )
        operation = _seed_issue_provenance(self.store)
        issued = self.store.issue_owner_control_challenge(
            OwnerControlChallengeIssueRequest(
                channel_session_id=binding.channel_session_id,
                operation_id=operation.operation_id,
                expires_in_seconds=300,
            )
        )
        altered_request = issued.approval_request().model_copy(update={"plan_digest": "9" * 64})
        envelope = _envelope(private_key, binding=binding, request=altered_request)

        result = self.store.verify_owner_control_confirmation_shadow(envelope)
        stored_challenge = self.store.read_owner_control_issued_challenge(
            challenge_nonce=issued.challenge_nonce
        )

        self.assertEqual(result.verification_status, "rejected")
        self.assertEqual(result.rejection_reason, "stored_approval_request_mismatch")
        self.assertEqual(stored_challenge.state, "issued")
        self.assertEqual(stored_challenge.attempt_count, 1)
        self.assertIsNone(stored_challenge.consumed_at)

    def test_managed_policy_review_is_bounded_and_blocked_plans_fail_closed(self) -> None:
        policy_record = _owner_control_policy_record(action=AUTHZ_POLICY_OPERATION_APPROVE_ACTION)
        operation = _managed_policy_operation(blocked=False)
        request = derive_owner_control_approval_request(
            operation=operation,
            policy_record=policy_record,
            owner_github_id=100001,
            nonce="owner-control-nonce-0000000000000010",
            issued_at="2026-08-28T00:00:00+00:00",
            expires_at="2026-08-28T00:05:00+00:00",
        )
        later_request = derive_owner_control_approval_request(
            operation=operation,
            policy_record=policy_record,
            owner_github_id=100001,
            nonce="owner-control-nonce-0000000000000012",
            issued_at="2026-08-28T00:01:00+00:00",
            expires_at="2026-08-28T00:06:00+00:00",
        )
        self.assertEqual(request.server_review, later_request.server_review)
        review_items = {item.key: item.value for item in request.server_review.items}
        self.assertEqual(review_items["changed"], "yes")
        self.assertEqual(review_items["added_rule_count"], "1")
        self.assertEqual(review_items["adopted_rule_count"], "2")
        self.assertEqual(review_items["updated_rule_count"], "3")
        self.assertEqual(review_items["removed_rule_count"], "4")
        self.assertEqual(review_items["unchanged_rule_count"], "5")
        review_text = str(request.server_review.model_dump(mode="json"))
        self.assertNotIn("sensitive-managed-set", review_text)
        self.assertNotIn("private policy request reason", review_text)

        with self.assertRaisesRegex(
            OwnerControlChallengeProvenanceError,
            "Blocked managed-policy plans",
        ):
            derive_owner_control_approval_request(
                operation=_managed_policy_operation(blocked=True),
                policy_record=policy_record,
                owner_github_id=100001,
                nonce="owner-control-nonce-0000000000000011",
                issued_at="2026-08-28T00:00:00+00:00",
                expires_at="2026-08-28T00:05:00+00:00",
            )

    def test_owner_control_challenge_requires_schema_v2_policy(self) -> None:
        with self.assertRaisesRegex(
            OwnerControlChallengeProvenanceError,
            "schema-v2 authz policy",
        ):
            derive_owner_control_approval_request(
                operation=_managed_policy_operation(blocked=False),
                policy_record=_owner_control_policy_record(
                    action=AUTHZ_POLICY_OPERATION_APPROVE_ACTION,
                    schema_version=1,
                ),
                owner_github_id=100001,
                nonce="owner-control-nonce-0000000000000013",
                issued_at="2026-08-28T00:00:00+00:00",
                expires_at="2026-08-28T00:05:00+00:00",
            )

    def test_self_signed_unknown_session_never_creates_durable_state(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(private_key, session_id="self-signed-session")
        request = _request(nonce="owner-control-nonce-0000000000000002")
        envelope = _envelope(private_key, binding=binding, request=request)

        with self.assertRaisesRegex(FileNotFoundError, "was not issued"):
            self.store.verify_owner_control_confirmation_shadow(envelope)
        events = self.store.list_owner_control_shadow_verification_events(
            challenge_nonce=request.nonce
        )

        self.assertEqual(events, ())

    def test_rejection_audit_is_bounded_and_terminal(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(private_key)
        self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=_host_principal_claim(),
        )
        operation = _seed_issue_provenance(self.store)
        issued = self.store.issue_owner_control_challenge(
            OwnerControlChallengeIssueRequest(
                channel_session_id=binding.channel_session_id,
                operation_id=operation.operation_id,
                expires_in_seconds=300,
            )
        )
        altered_request = issued.approval_request().model_copy(update={"plan_digest": "9" * 64})
        envelope = _envelope(private_key, binding=binding, request=altered_request)

        results = tuple(
            self.store.verify_owner_control_confirmation_shadow(envelope) for _ in range(8)
        )
        with self.assertRaisesRegex(ValueError, "attempt budget is exhausted"):
            self.store.verify_owner_control_confirmation_shadow(envelope)
        stored_challenge = self.store.read_owner_control_issued_challenge(
            challenge_nonce=issued.challenge_nonce
        )
        events = self.store.list_owner_control_shadow_verification_events(
            challenge_nonce=issued.challenge_nonce
        )

        self.assertTrue(
            all(
                result.rejection_reason == "stored_approval_request_mismatch"
                for result in results[:7]
            )
        )
        self.assertEqual(results[7].rejection_reason, "attempt_budget_exhausted")
        self.assertEqual(stored_challenge.state, "rejected")
        self.assertEqual(stored_challenge.attempt_count, 8)
        self.assertEqual(len(events), 8)
        self.assertEqual(sorted(event.sequence for event in events), list(range(1, 9)))

    def test_pure_evaluation_rejects_unknown_sessions_before_signature_can_authorize(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(private_key, session_id="not-enrolled")
        request = _request(nonce="owner-control-nonce-0000000000000003")
        envelope = _envelope(private_key, binding=binding, request=request)

        evaluation = evaluate_owner_control_shadow_verification(
            envelope=envelope,
            channel_session=None,
            issued_challenge=None,
            observed_at="2026-08-28T00:00:00+00:00",
        )

        self.assertEqual(evaluation.verification_status, "rejected")
        self.assertEqual(evaluation.rejection_reason, "unknown_channel_session")
        self.assertEqual(evaluation.resulting_challenge_state, "rejected")
        self.assertFalse(evaluation.consume_challenge)

    def test_pure_evaluation_rejects_stored_state_and_signature_mismatches(self) -> None:
        private_key = Ed25519PrivateKey.generate()
        binding = _binding(private_key)
        enrolled = self.store.enroll_owner_control_channel_session(
            binding,
            host_principal_claim=_host_principal_claim(),
        )
        operation = _seed_issue_provenance(self.store)
        issued = self.store.issue_owner_control_challenge(
            OwnerControlChallengeIssueRequest(
                channel_session_id=binding.channel_session_id,
                operation_id=operation.operation_id,
                expires_in_seconds=300,
            )
        )
        valid_envelope = _envelope(
            private_key,
            binding=binding,
            request=issued.approval_request(),
        )
        wrong_signature_envelope = _envelope(
            Ed25519PrivateKey.generate(),
            binding=binding,
            request=issued.approval_request(),
        )
        cross_session_binding = binding.model_copy(
            update={"channel_session_id": "owner-control-other-session"}
        )
        cross_session_envelope = _envelope(
            private_key,
            binding=cross_session_binding,
            request=issued.approval_request(),
        )
        revoked = enrolled.session.model_copy(
            update={"status": "revoked", "revoked_at": enrolled.session.enrolled_at}
        )

        cases = (
            (
                "revoked",
                valid_envelope,
                revoked,
                issued.issued_at,
                "channel_session_revoked",
            ),
            (
                "session-expired",
                valid_envelope,
                enrolled.session,
                "2035-01-02T00:00:00+00:00",
                "channel_session_expired",
            ),
            (
                "challenge-expired",
                valid_envelope,
                enrolled.session,
                "2027-01-01T00:00:00+00:00",
                "challenge_expired",
            ),
            (
                "cross-session",
                cross_session_envelope,
                enrolled.session,
                issued.issued_at,
                "challenge_channel_session_mismatch",
            ),
            (
                "wrong-signature",
                wrong_signature_envelope,
                enrolled.session,
                issued.issued_at,
                "signature_invalid",
            ),
        )
        for name, envelope, session_record, observed_at, expected_reason in cases:
            with self.subTest(name=name):
                evaluation = evaluate_owner_control_shadow_verification(
                    envelope=envelope,
                    channel_session=session_record,
                    issued_challenge=issued,
                    observed_at=observed_at,
                )
                self.assertEqual(evaluation.verification_status, "rejected")
                self.assertEqual(evaluation.rejection_reason, expected_reason)
                self.assertFalse(evaluation.consume_challenge)


class OwnerControlShadowVerifierFreezeBoundaryTests(unittest.TestCase):
    def test_published_wire_models_remain_frozen(self) -> None:
        frozen_paths = ("control_plane/contracts/owner_control.py",)

        for path in frozen_paths:
            with self.subTest(path=path):
                result = subprocess.run(
                    ["git", "diff", "--quiet", OWNER_CONTROL_BASE_REVISION, "--", path],
                    cwd=REPOSITORY_ROOT,
                )
                self.assertEqual(result.returncode, 0, f"Frozen boundary changed: {path}")

    def test_http_transport_remains_decoupled_from_shadow_verifier(self) -> None:
        source = (REPOSITORY_ROOT / "control_plane/http_app.py").read_text(encoding="utf-8")

        for forbidden_reference in (
            "owner_control_shadow_verifier",
            "owner_control_challenge",
            "OwnerControlChallengeIssueRequest",
            "evaluate_owner_control_shadow_verification",
            "/owner-control",
        ):
            with self.subTest(forbidden_reference=forbidden_reference):
                self.assertNotIn(forbidden_reference, source)

    def test_shadow_verifier_contract_has_no_transport_or_execution_coupling(self) -> None:
        for path in (
            "control_plane/contracts/owner_control_shadow_verifier.py",
            "control_plane/owner_control_challenge.py",
        ):
            source = (REPOSITORY_ROOT / path).read_text(encoding="utf-8")
            for forbidden_reference in (
                "fastapi",
                "http_app",
                "http_routes",
                "filesystem",
                "privileged_operation_service",
                "privileged_operation_worker",
                "transition_privileged_operation",
                "approve_privileged_operation",
                "outbox",
                "os.environ",
            ):
                with self.subTest(path=path, forbidden_reference=forbidden_reference):
                    self.assertNotIn(forbidden_reference, source)

    def test_privileged_operation_paths_do_not_import_shadow_verifier(self) -> None:
        for path in (
            "control_plane/privileged_operation_service.py",
            "control_plane/privileged_operation_worker.py",
            "control_plane/http_routes/privileged_operations.py",
        ):
            with self.subTest(path=path):
                source = (REPOSITORY_ROOT / path).read_text(encoding="utf-8")
                self.assertNotIn("owner_control_shadow_verifier", source)
                self.assertNotIn("owner_control_challenge", source)

    def test_docs_keep_shadow_state_inert_and_unrouted(self) -> None:
        owner_control_doc = (REPOSITORY_ROOT / "docs/owner-control-channel.md").read_text(
            encoding="utf-8"
        )
        authorization_doc = (REPOSITORY_ROOT / "docs/authorization-authority.md").read_text(
            encoding="utf-8"
        )
        privileged_operation_doc = (REPOSITORY_ROOT / "docs/privileged-operations.md").read_text(
            encoding="utf-8"
        )

        self.assertIn("at most eight audited verification attempts", owner_control_doc)
        self.assertIn("Unknown challenge nonces create no durable", owner_control_doc)
        self.assertIn("`authority_state = 'inert'`", authorization_doc)
        self.assertIn("exactly one ID-only managed rule", authorization_doc)
        self.assertIn(
            "browser approval remains the only active approval transport", privileged_operation_doc
        )
