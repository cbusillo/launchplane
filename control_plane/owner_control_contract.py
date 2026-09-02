from __future__ import annotations

import base64
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Literal, get_args

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from control_plane.contracts.canonical_json import canonical_json_bytes, canonical_json_sha256
from control_plane.contracts.owner_control import (
    ApprovalRequest,
    ChannelBindingRecord,
    ChallengeResponse,
    OwnerControlConfirmationEnvelope,
    OwnerControlSignaturePayload,
    ReviewItem,
    ServerReviewPayload,
    owner_control_approval_request_digest,
    owner_control_channel_binding_sha256,
    owner_control_challenge_response_digest,
    owner_control_signature_payload,
    owner_control_signature_payload_bytes,
)
from control_plane.contracts.owner_control_enrollment_provenance import (
    OWNER_CONTROL_ENROLLMENT_CONTEXT,
    OWNER_CONTROL_ENROLLMENT_PROVENANCE_SCHEMA_VERSION,
    OWNER_CONTROL_PROVENANCE_TIER,
    OWNER_CONTROL_SERVER_CORROBORATION,
    OwnerControlEnrollmentProvenanceRecord,
    OwnerControlGestureSourceClaim,
    OwnerControlHostPrincipalClaim,
    OwnerControlKeyCustodyClaim,
    OwnerControlPrincipalSeparationClaim,
    derive_owner_control_provenance_tier,
    is_published_owner_control_synthetic_public_key,
    owner_control_host_principal_claim_sha256,
    owner_control_public_key_sha256,
)
from control_plane.contracts.owner_control_shadow_verifier import (
    OWNER_CONTROL_SHADOW_AUTHORITY_STATE,
    OWNER_CONTROL_SHADOW_MAX_ATTEMPTS,
    OWNER_CONTROL_SHADOW_VERIFIER_MODE,
    OWNER_CONTROL_SHADOW_VERIFIER_SCHEMA_VERSION,
    OwnerControlChallengeIssueRequest,
    OwnerControlChannelSessionRecord,
    OwnerControlIssuedChallengeRecord,
    OwnerControlShadowVerificationReason,
    build_owner_control_channel_session_record,
    evaluate_owner_control_shadow_verification,
    issue_owner_control_challenge_record,
    owner_control_confirmation_envelope_sha256,
    owner_control_verification_event_id,
    revoke_owner_control_channel_session_record,
    terminalize_expired_owner_control_challenge_record,
)
from control_plane.contracts.privileged_operation import (
    PrivilegedOperationDescriptorId,
    PrivilegedOperationSafetyClass,
)
from control_plane.privileged_operation_registry import list_privileged_operation_descriptors
from control_plane.privileged_operation_registry import read_privileged_operation_descriptor


OWNER_CONTROL_CONTRACT_SCHEMA_VERSION = 5
_OWNER_CONTROL_PREVIOUS_CONTRACT_SCHEMA_VERSION = 4
_OWNER_CONTROL_SIGNATURE_DECLARATION_SCHEMA_VERSION = 2
_OWNER_CONTROL_VECTOR_SCHEMA_VERSION = 1
_ARTIFACT_SYNTHETIC_PRIVATE_KEY_SEED = bytes(range(32))
_ARTIFACT_SYNTHETIC_WRONG_PRIVATE_KEY_SEED = bytes(range(31, -1, -1))
_PROVENANCE_SYNTHETIC_PUBLIC_KEY = base64.urlsafe_b64encode(bytes(32)).decode().rstrip("=")
_PRESERVED_V2_SECTION_SHA256 = {
    "canonical_json": "0c6b6454d737943d01d4621c217ff8412552a0bf0c69a0f50a761d38ac0e7d1f",
    "canonicalization_vectors": "ca481ff769bba537310c8568b56850f5d12ebc0c90ace9ea2dc39ff714daa6a8",
    "negative_confirmation_vectors": "9d529ca0f5153c8c3eb3eb4862311efc6dc3c1dc7e5df55824ebde13194eb46d",
    "negative_vectors": "232d29bc542df455c9f54a3196a2a4d41cb0912155f92d060c850df99c835b29",
    "signature_declaration": "7d9c62d55792931383d4a02ed99d31e21c67b5ce714c01d9144dc2a3bed34f72",
}
_PRESERVED_V2_DESCRIPTOR_VECTOR_SECTION_SHA256 = {
    "confirmation_golden_vectors": "58391f364a79ab321596d30200b87c9d29be366eaa1386a9ff4c242c8b38d50a",
    "golden_vectors": "6955c5c8bb228c21bc6a68a4ddb7cf22456cd51615ffe6e421b0fa04f15d9584",
}
_PRESERVED_V2_SCHEMA_SHA256 = {
    "approval_request": "6cc0379a8323715191aea8a605b594d6500a2937c6d46c45caa9cea313b3b8b4",
    "challenge_response": "1fc009f4497b88caacf1c7273c0e7e932936cd23a24f5d3e1846f235da1082d0",
    "channel_binding_record": "399e8f1ee814e60f6973290d5da0a542add174bb2926ab2cc2dff583c18c1a94",
    "owner_control_confirmation_envelope": "b3e15f59f63efdeff5d95e621af981959c6d5ec2c71e1ae15351967722ee589f",
    "owner_control_signature_payload": "002b5e22c57ac17b6f7915486e29f2d3d11aee7e0b44505050b1366c23ee8c00",
    "server_review_payload": "b4b0d9e55212190615e9d4247ff459c15309ab1a10a715df92432585b9f34855",
}
_PRESERVED_V2_DESCRIPTOR_IDS = frozenset(
    ("managed-authz-policy-set", "managed-secret-reencryption")
)
_ALL_DESCRIPTOR_IDS = frozenset(get_args(PrivilegedOperationDescriptorId))
_PRESERVED_V4_SECTION_SHA256 = {
    "canonical_json": "0c6b6454d737943d01d4621c217ff8412552a0bf0c69a0f50a761d38ac0e7d1f",
    "canonicalization_vectors": "ca481ff769bba537310c8568b56850f5d12ebc0c90ace9ea2dc39ff714daa6a8",
    "challenge_lifecycle_vectors": "81a1d62ee2c9268366e052da78221ab13d8946b276c0a06c1009296357a89807",
    "compatibility": "62115e1e9d322dad345d0a48e4523445285d05281a52a4d94a18e1fcb3d27937",
    "confirmation_golden_vectors": "763f4795b9b25d725f394d4df13f8a713e253429aa31457a84cc88c7f9f71b7a",
    "golden_vectors": "b8e51053225c281d68d6dff7d8a1e5963ac7a2396dc48a691a4da3a12fe8b303",
    "negative_confirmation_vectors": "9d529ca0f5153c8c3eb3eb4862311efc6dc3c1dc7e5df55824ebde13194eb46d",
    "negative_vectors": "232d29bc542df455c9f54a3196a2a4d41cb0912155f92d060c850df99c835b29",
    "schema_version": "4b227777d4dd1fc61c6f884f48641d02b4d121d3fd328cb08b5531fcacdabf8a",
    "schemas": "1fdb3187f24ee64f95a8f7753ec64b93d72d1f3b92ea118db00d43a798263f0c",
    "signature_declaration": "7d9c62d55792931383d4a02ed99d31e21c67b5ce714c01d9144dc2a3bed34f72",
    "verification_state_vectors": "4c199bf64618845f098ac12ef992a21111ab87a6e91e5325f962db3e8174c8df",
}


class OwnerControlContractError(ValueError):
    pass


def _digest_for_vector(*, descriptor_id: PrivilegedOperationDescriptorId, label: str) -> str:
    return canonical_json_sha256(
        {
            "descriptor_id": descriptor_id,
            "label": label,
            "schema_version": _OWNER_CONTROL_VECTOR_SCHEMA_VERSION,
        }
    )


def _approval_request_for_descriptor(
    *,
    descriptor_id: PrivilegedOperationDescriptorId,
    safety_class: PrivilegedOperationSafetyClass,
) -> ApprovalRequest:
    vector_digest = _digest_for_vector(descriptor_id=descriptor_id, label="operation")
    review = ServerReviewPayload(
        review_id=f"review-{vector_digest[:24]}",
        title="Owner approval required",
        summary="Review this exact privileged operation before confirming.",
        items=(
            ReviewItem(
                key="descriptor",
                label="Operation class",
                value=descriptor_id,
            ),
            ReviewItem(
                key="safety_class",
                label="Safety class",
                value=safety_class,
            ),
        ),
    )
    return ApprovalRequest(
        operation_id=f"privileged-operation-{vector_digest[:32]}",
        descriptor_id=descriptor_id,
        descriptor_version=1,
        request_digest=_digest_for_vector(descriptor_id=descriptor_id, label="request"),
        plan_digest=_digest_for_vector(descriptor_id=descriptor_id, label="plan"),
        evidence_digest=_digest_for_vector(descriptor_id=descriptor_id, label="evidence"),
        pre_state_digest=_digest_for_vector(descriptor_id=descriptor_id, label="pre-state"),
        policy_record_id=f"owner-policy-{vector_digest[:20]}",
        policy_revision=1,
        policy_sha256=_digest_for_vector(descriptor_id=descriptor_id, label="policy"),
        owner_github_id=100000 + int(vector_digest[:8], 16),
        server_review=review,
        nonce=f"owner-control-{_digest_for_vector(descriptor_id=descriptor_id, label='nonce')[:32]}",
        issued_at="2030-01-02T03:00:05+00:00",
        expires_at="2030-01-02T03:09:05+00:00",
    )


def _golden_vector(
    *,
    descriptor_id: PrivilegedOperationDescriptorId,
    descriptor_version: int,
    safety_class: PrivilegedOperationSafetyClass,
) -> dict[str, Any]:
    if descriptor_version != 1:
        raise OwnerControlContractError(
            f"Unsupported owner-control descriptor version: {descriptor_version}"
        )
    request = _approval_request_for_descriptor(
        descriptor_id=descriptor_id,
        safety_class=safety_class,
    )
    response = ChallengeResponse(
        approval_request=request,
        approval_request_digest=owner_control_approval_request_digest(request),
        decision="approved",
        channel_binding_sha256=_digest_for_vector(
            descriptor_id=descriptor_id,
            label="channel-binding",
        ),
        confirmed_at="2030-01-02T03:04:05+00:00",
    )
    request_payload = request.model_dump(mode="json")
    response_payload = response.model_dump(mode="json")
    return {
        "descriptor_id": descriptor_id,
        "descriptor_version": descriptor_version,
        "approval_request": {
            "canonical_json": canonical_json_bytes(request_payload).decode(),
            "payload": request_payload,
            "sha256": owner_control_approval_request_digest(request),
        },
        "challenge_response": {
            "canonical_json": canonical_json_bytes(response_payload).decode(),
            "payload": response_payload,
            "sha256": owner_control_challenge_response_digest(response),
        },
    }


def _artifact_synthetic_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_ARTIFACT_SYNTHETIC_PRIVATE_KEY_SEED)


def _artifact_synthetic_wrong_private_key() -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(_ARTIFACT_SYNTHETIC_WRONG_PRIVATE_KEY_SEED)


def _base64url(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def _synthetic_owner_public_key(private_key: Ed25519PrivateKey) -> str:
    return _base64url(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
    )


def _channel_binding_for_request(
    *,
    descriptor_id: PrivilegedOperationDescriptorId,
    request: ApprovalRequest,
) -> ChannelBindingRecord:
    return ChannelBindingRecord(
        channel_session_id=f"channel-session-{_digest_for_vector(descriptor_id=descriptor_id, label='channel-session')[:32]}",
        owner_github_id=request.owner_github_id,
        signature_algorithm="ed25519",
        owner_public_key=_synthetic_owner_public_key(_artifact_synthetic_private_key()),
        session_issued_at=request.issued_at,
        session_expires_at=request.expires_at,
    )


def _confirmation_golden_vector(
    *,
    descriptor_id: PrivilegedOperationDescriptorId,
    descriptor_version: int,
    safety_class: PrivilegedOperationSafetyClass,
) -> dict[str, Any]:
    if descriptor_version != 1:
        raise OwnerControlContractError(
            f"Unsupported owner-control descriptor version: {descriptor_version}"
        )
    binding, response, envelope = _confirmation_models_for_descriptor(
        descriptor_id=descriptor_id,
        safety_class=safety_class,
    )
    signature_payload = owner_control_signature_payload(response)
    binding_payload = binding.model_dump(mode="json")
    response_payload = response.model_dump(mode="json")
    signature_payload_data = signature_payload.model_dump(mode="json")
    envelope_payload = envelope.model_dump(mode="json")
    return {
        "descriptor_id": descriptor_id,
        "descriptor_version": descriptor_version,
        "channel_binding": {
            "canonical_json": canonical_json_bytes(binding_payload).decode(),
            "payload": binding_payload,
            "sha256": owner_control_channel_binding_sha256(binding),
        },
        "challenge_response": {
            "canonical_json": canonical_json_bytes(response_payload).decode(),
            "payload": response_payload,
            "sha256": owner_control_challenge_response_digest(response),
        },
        "signature_payload": {
            "canonical_json": canonical_json_bytes(signature_payload_data).decode(),
            "payload": signature_payload_data,
            "sha256": canonical_json_sha256(signature_payload_data),
        },
        "confirmation_envelope": {
            "canonical_json": canonical_json_bytes(envelope_payload).decode(),
            "payload": envelope_payload,
            "sha256": canonical_json_sha256(envelope_payload),
        },
        "verification": "valid",
    }


def _confirmation_models_for_descriptor(
    *,
    descriptor_id: PrivilegedOperationDescriptorId,
    safety_class: PrivilegedOperationSafetyClass,
) -> tuple[ChannelBindingRecord, ChallengeResponse, OwnerControlConfirmationEnvelope]:
    request = _approval_request_for_descriptor(
        descriptor_id=descriptor_id,
        safety_class=safety_class,
    )
    binding = _channel_binding_for_request(descriptor_id=descriptor_id, request=request)
    response = ChallengeResponse(
        approval_request=request,
        approval_request_digest=owner_control_approval_request_digest(request),
        decision="approved",
        channel_binding_sha256=owner_control_channel_binding_sha256(binding),
        confirmed_at="2030-01-02T03:04:05+00:00",
    )
    envelope = OwnerControlConfirmationEnvelope(
        channel_binding=binding,
        challenge_response=response,
        signature_algorithm="ed25519",
        signature=_base64url(
            _artifact_synthetic_private_key().sign(owner_control_signature_payload_bytes(response))
        ),
    )
    return binding, response, envelope


def _signed_confirmation_envelope(
    *,
    binding: ChannelBindingRecord,
    request: ApprovalRequest,
    signer: Ed25519PrivateKey | None = None,
) -> OwnerControlConfirmationEnvelope:
    response = ChallengeResponse(
        approval_request=request,
        approval_request_digest=owner_control_approval_request_digest(request),
        decision="approved",
        channel_binding_sha256=owner_control_channel_binding_sha256(binding),
        confirmed_at="2030-01-02T03:04:05+00:00",
    )
    resolved_signer = signer or _artifact_synthetic_private_key()
    return OwnerControlConfirmationEnvelope(
        channel_binding=binding,
        challenge_response=response,
        signature_algorithm="ed25519",
        signature=_base64url(resolved_signer.sign(owner_control_signature_payload_bytes(response))),
    )


def _shadow_verifier_fixture_models() -> tuple[
    ApprovalRequest,
    ChannelBindingRecord,
    OwnerControlChannelSessionRecord,
    OwnerControlIssuedChallengeRecord,
    OwnerControlConfirmationEnvelope,
]:
    descriptor = read_privileged_operation_descriptor("managed-authz-policy-set").descriptor
    request = _approval_request_for_descriptor(
        descriptor_id=descriptor.descriptor_id,
        safety_class=descriptor.safety_class,
    )
    binding = ChannelBindingRecord(
        channel_session_id=f"channel-session-{_digest_for_vector(descriptor_id=descriptor.descriptor_id, label='verification-session')[:32]}",
        owner_github_id=request.owner_github_id,
        signature_algorithm="ed25519",
        owner_public_key=_synthetic_owner_public_key(_artifact_synthetic_private_key()),
        session_issued_at="2030-01-02T03:00:00+00:00",
        session_expires_at="2030-01-02T03:20:05+00:00",
    )
    session = build_owner_control_channel_session_record(
        binding=binding,
        enrolled_at=request.issued_at,
    )
    challenge = issue_owner_control_challenge_record(
        issue_request=OwnerControlChallengeIssueRequest(
            channel_session_id=binding.channel_session_id,
            operation_id=request.operation_id,
            expires_in_seconds=540,
        ),
        session=session,
        approval_request=request,
    )
    envelope = _signed_confirmation_envelope(binding=binding, request=request)
    return request, binding, session, challenge, envelope


def _verification_state_vector(
    *,
    name: str,
    envelope: OwnerControlConfirmationEnvelope,
    channel_session: OwnerControlChannelSessionRecord | None,
    issued_challenge: OwnerControlIssuedChallengeRecord | None,
    observed_at: str,
) -> dict[str, Any]:
    evaluation = evaluate_owner_control_shadow_verification(
        envelope=envelope,
        channel_session=channel_session,
        issued_challenge=issued_challenge,
        observed_at=observed_at,
    )
    return {
        "name": name,
        "observed_at": observed_at,
        "channel_session": (
            channel_session.model_dump(mode="json") if channel_session is not None else None
        ),
        "issued_challenge": (
            issued_challenge.model_dump(mode="json") if issued_challenge is not None else None
        ),
        "confirmation_envelope": envelope.model_dump(mode="json"),
        "expected": {
            **evaluation.model_dump(mode="json"),
            "verifier_mode": OWNER_CONTROL_SHADOW_VERIFIER_MODE,
            "authorizes_execution": False,
            "authority_state": OWNER_CONTROL_SHADOW_AUTHORITY_STATE,
        },
    }


def _terminal_challenge(
    challenge: OwnerControlIssuedChallengeRecord,
    *,
    envelope: OwnerControlConfirmationEnvelope,
    state: Literal["consumed", "rejected"],
) -> OwnerControlIssuedChallengeRecord:
    verification_status: Literal["verified", "rejected"]
    rejection_reason: OwnerControlShadowVerificationReason | None
    if state == "consumed":
        verification_status = "verified"
        rejection_reason = None
        sequence = 1
        updates: dict[str, object] = {
            "state": "consumed",
            "attempt_count": sequence,
            "consumed_at": "2030-01-02T03:04:05+00:00",
        }
    else:
        verification_status = "rejected"
        rejection_reason = "signature_invalid"
        sequence = OWNER_CONTROL_SHADOW_MAX_ATTEMPTS
        updates = {
            "state": "rejected",
            "attempt_count": sequence,
        }
    updates["terminal_event_id"] = owner_control_verification_event_id(
        challenge_id=challenge.challenge_id,
        sequence=sequence,
        envelope_sha256=owner_control_confirmation_envelope_sha256(envelope),
        verification_status=verification_status,
        rejection_reason=rejection_reason,
    )
    return OwnerControlIssuedChallengeRecord.model_validate({**challenge.model_dump(), **updates})


def _verification_state_vectors() -> list[dict[str, Any]]:
    request, binding, session, challenge, envelope = _shadow_verifier_fixture_models()
    revoked_session = revoke_owner_control_channel_session_record(
        session,
        revoked_at="2030-01-02T03:03:05+00:00",
    )
    mismatched_binding = ChannelBindingRecord(
        channel_session_id=f"channel-session-{_digest_for_vector(descriptor_id=request.descriptor_id, label='mismatched-session')[:32]}",
        owner_github_id=request.owner_github_id,
        signature_algorithm="ed25519",
        owner_public_key=binding.owner_public_key,
        session_issued_at=binding.session_issued_at,
        session_expires_at=binding.session_expires_at,
    )
    mismatched_session = build_owner_control_channel_session_record(
        binding=mismatched_binding,
        enrolled_at=request.issued_at,
    )
    alternate_binding = ChannelBindingRecord(
        channel_session_id=binding.channel_session_id,
        owner_github_id=binding.owner_github_id,
        signature_algorithm="ed25519",
        owner_public_key=binding.owner_public_key,
        session_issued_at=binding.session_issued_at,
        session_expires_at="2030-01-02T03:19:05+00:00",
    )
    alternate_request = ApprovalRequest.model_validate(
        {
            **request.model_dump(),
            "policy_revision": request.policy_revision + 1,
        }
    )
    consumed_challenge = _terminal_challenge(
        challenge,
        envelope=envelope,
        state="consumed",
    )
    rejected_challenge = _terminal_challenge(
        challenge,
        envelope=envelope,
        state="rejected",
    )
    vectors = [
        _verification_state_vector(
            name="verified",
            envelope=envelope,
            channel_session=session,
            issued_challenge=challenge,
            observed_at="2030-01-02T03:04:05+00:00",
        ),
        _verification_state_vector(
            name="unknown-channel-session",
            envelope=envelope,
            channel_session=None,
            issued_challenge=None,
            observed_at="2030-01-02T03:04:05+00:00",
        ),
        _verification_state_vector(
            name="unknown-challenge",
            envelope=envelope,
            channel_session=session,
            issued_challenge=None,
            observed_at="2030-01-02T03:04:05+00:00",
        ),
        _verification_state_vector(
            name="channel-session-revoked",
            envelope=envelope,
            channel_session=revoked_session,
            issued_challenge=challenge,
            observed_at="2030-01-02T03:04:05+00:00",
        ),
        _verification_state_vector(
            name="channel-session-expired",
            envelope=envelope,
            channel_session=session,
            issued_challenge=challenge,
            observed_at="2030-01-02T03:20:06+00:00",
        ),
        _verification_state_vector(
            name="challenge-channel-session-mismatch",
            envelope=envelope,
            channel_session=mismatched_session,
            issued_challenge=challenge,
            observed_at="2030-01-02T03:04:05+00:00",
        ),
        _verification_state_vector(
            name="challenge-expired",
            envelope=envelope,
            channel_session=session,
            issued_challenge=challenge,
            observed_at="2030-01-02T03:09:06+00:00",
        ),
        _verification_state_vector(
            name="challenge-replayed",
            envelope=envelope,
            channel_session=session,
            issued_challenge=consumed_challenge,
            observed_at="2030-01-02T03:05:05+00:00",
        ),
        _verification_state_vector(
            name="stored-binding-mismatch",
            envelope=_signed_confirmation_envelope(
                binding=alternate_binding,
                request=request,
            ),
            channel_session=session,
            issued_challenge=challenge,
            observed_at="2030-01-02T03:04:05+00:00",
        ),
        _verification_state_vector(
            name="stored-approval-request-mismatch",
            envelope=_signed_confirmation_envelope(
                binding=binding,
                request=alternate_request,
            ),
            channel_session=session,
            issued_challenge=challenge,
            observed_at="2030-01-02T03:04:05+00:00",
        ),
        _verification_state_vector(
            name="signature-invalid",
            envelope=_signed_confirmation_envelope(
                binding=binding,
                request=request,
                signer=_artifact_synthetic_wrong_private_key(),
            ),
            channel_session=session,
            issued_challenge=challenge,
            observed_at="2030-01-02T03:04:05+00:00",
        ),
        _verification_state_vector(
            name="attempt-budget-exhausted",
            envelope=envelope,
            channel_session=session,
            issued_challenge=rejected_challenge,
            observed_at="2030-01-02T03:05:05+00:00",
        ),
    ]
    expected_reasons = set(get_args(OwnerControlShadowVerificationReason))
    actual_reasons = {
        vector["expected"]["rejection_reason"]
        for vector in vectors
        if vector["expected"]["rejection_reason"] is not None
    }
    if actual_reasons != expected_reasons:
        raise OwnerControlContractError(
            "Owner-control verification vectors must cover every rejection reason"
        )
    if not any(vector["expected"]["verification_status"] == "verified" for vector in vectors):
        raise OwnerControlContractError(
            "Owner-control verification vectors must include a verified outcome"
        )
    return vectors


def _challenge_lifecycle_vectors() -> list[dict[str, Any]]:
    _, _, _, challenge, _ = _shadow_verifier_fixture_models()
    terminalized, event = terminalize_expired_owner_control_challenge_record(
        challenge,
        observed_at=challenge.expires_at,
    )
    return [
        {
            "name": "issued-to-expired-at-boundary",
            "observed_at": challenge.expires_at,
            "issued_challenge": challenge.model_dump(mode="json"),
            "expected_terminalized_challenge": terminalized.model_dump(mode="json"),
            "expected_lifecycle_event": event.model_dump(mode="json"),
        }
    ]


def _canonicalization_vectors() -> list[dict[str, Any]]:
    payloads: tuple[tuple[str, object], ...] = (
        (
            "primitive-types",
            {
                "array": [True, False, None, 7],
                "integer_max": 2**63 - 1,
                "integer_min": -(2**63),
            },
        ),
        (
            "unicode-and-escapes",
            {
                "controls": 'null\x00unit\x1fdelete\x7fline\n\ttab\\quote"',
                "text": "μ😀",
            },
        ),
        ("unicode-key-order", {"😀": 3, "Ｚ": 2, "a": 1}),
        ("empty-and-nested", {"empty_array": [], "empty_object": {}, "nested": [{"x": []}]}),
    )
    return [
        {
            "name": name,
            "payload": payload,
            "canonical_json": canonical_json_bytes(payload).decode(),
            "sha256": canonical_json_sha256(payload),
        }
        for name, payload in payloads
    ]


def _negative_vectors() -> list[dict[str, Any]]:
    descriptor = read_privileged_operation_descriptor("managed-authz-policy-set").descriptor
    request = _approval_request_for_descriptor(
        descriptor_id=descriptor.descriptor_id,
        safety_class=descriptor.safety_class,
    )
    request_payload = request.model_dump(mode="json")
    valid_response = ChallengeResponse(
        approval_request=request,
        approval_request_digest=owner_control_approval_request_digest(request),
        decision="approved",
        channel_binding_sha256=_digest_for_vector(
            descriptor_id=descriptor.descriptor_id,
            label="channel-binding",
        ),
        confirmed_at="2030-01-02T03:04:05+00:00",
    ).model_dump(mode="json")
    return [
        {
            "model": "approval_request",
            "rule": "schema-version-is-one",
            "error_location": ["schema_version"],
            "payload": {**request_payload, "schema_version": 2},
        },
        {
            "model": "approval_request",
            "rule": "descriptor-version-is-one",
            "error_location": ["descriptor_version"],
            "payload": {**request_payload, "descriptor_version": 2},
        },
        {
            "model": "approval_request",
            "rule": "digests-are-lowercase-sha256",
            "error_location": ["request_digest"],
            "payload": {**request_payload, "request_digest": "A" * 64},
        },
        {
            "model": "approval_request",
            "rule": "timestamps-use-canonical-utc-form",
            "error_location": ["expires_at"],
            "payload": {**request_payload, "expires_at": "2030-01-02T03:09:05Z"},
        },
        {
            "model": "approval_request",
            "rule": "timestamps-are-calendar-valid",
            "error_location": [],
            "payload": {**request_payload, "expires_at": "2030-02-30T03:09:05+00:00"},
        },
        {
            "model": "approval_request",
            "rule": "nonce-is-canonical",
            "error_location": ["nonce"],
            "payload": {**request_payload, "nonce": "short"},
        },
        {
            "model": "approval_request",
            "rule": "expiry-follows-issuance",
            "error_location": [],
            "payload": {**request_payload, "expires_at": request_payload["issued_at"]},
        },
        {
            "model": "challenge_response",
            "rule": "request-digest-binds-exact-request",
            "error_location": [],
            "payload": {**valid_response, "approval_request_digest": "0" * 64},
        },
        {
            "model": "challenge_response",
            "rule": "confirmation-is-not-before-issuance",
            "error_location": [],
            "payload": {**valid_response, "confirmed_at": "2030-01-02T02:59:05+00:00"},
        },
        {
            "model": "challenge_response",
            "rule": "confirmation-is-not-after-expiry",
            "error_location": [],
            "payload": {**valid_response, "confirmed_at": "2030-01-02T03:10:05+00:00"},
        },
        {
            "model": "server_review_payload",
            "rule": "review-item-keys-are-unique",
            "error_location": [],
            "payload": {
                **request_payload["server_review"],
                "items": [
                    request_payload["server_review"]["items"][0],
                    request_payload["server_review"]["items"][0],
                ],
            },
        },
        {
            "model": "server_review_payload",
            "rule": "review-text-has-no-surrounding-whitespace",
            "error_location": [],
            "payload": {**request_payload["server_review"], "title": " Owner approval required"},
        },
        {
            "model": "server_review_payload",
            "rule": "review-item-text-has-no-surrounding-whitespace",
            "error_location": ["items", 0],
            "payload": {
                **request_payload["server_review"],
                "items": [
                    {**request_payload["server_review"]["items"][0], "label": " Operation class"},
                    *request_payload["server_review"]["items"][1:],
                ],
            },
        },
    ]


def _negative_confirmation_vectors() -> list[dict[str, Any]]:
    descriptor = read_privileged_operation_descriptor("managed-authz-policy-set").descriptor
    binding, response, envelope = _confirmation_models_for_descriptor(
        descriptor_id=descriptor.descriptor_id,
        safety_class=descriptor.safety_class,
    )
    valid_payload = envelope.model_dump(mode="json")
    changed_binding = binding.model_copy(update={"owner_github_id": binding.owner_github_id + 1})
    changed_binding_payload = changed_binding.model_dump(mode="json")
    response_for_changed_binding = response.model_copy(
        update={"channel_binding_sha256": owner_control_channel_binding_sha256(changed_binding)}
    )
    changed_session_binding = binding.model_copy(
        update={"session_issued_at": "2030-01-02T03:01:05+00:00"}
    )
    response_for_changed_session = response.model_copy(
        update={
            "channel_binding_sha256": owner_control_channel_binding_sha256(changed_session_binding)
        }
    )
    cross_session_binding = binding.model_copy(
        update={"channel_session_id": "channel-session-substituted"}
    )
    response_for_cross_session = response.model_copy(
        update={
            "channel_binding_sha256": owner_control_channel_binding_sha256(cross_session_binding)
        }
    )
    wrong_key_signature = _base64url(
        _artifact_synthetic_wrong_private_key().sign(
            owner_control_signature_payload_bytes(response)
        )
    )

    return [
        {
            "model": "owner_control_confirmation_envelope",
            "rule": "schema-version-is-one",
            "error_location": ["schema_version"],
            "payload": {**valid_payload, "schema_version": 2},
        },
        {
            "model": "owner_control_confirmation_envelope",
            "rule": "signature-algorithm-is-ed25519",
            "error_location": ["signature_algorithm"],
            "payload": {**valid_payload, "signature_algorithm": "rsa"},
        },
        {
            "model": "owner_control_confirmation_envelope",
            "rule": "public-key-is-raw-32-byte-unpadded-base64url",
            "error_location": ["channel_binding", "owner_public_key"],
            "payload": {
                **valid_payload,
                "channel_binding": {**valid_payload["channel_binding"], "owner_public_key": "bad!"},
            },
        },
        {
            "model": "owner_control_confirmation_envelope",
            "rule": "signature-is-raw-64-byte-unpadded-base64url",
            "error_location": ["signature"],
            "payload": {**valid_payload, "signature": "bad!"},
        },
        {
            "model": "owner_control_confirmation_envelope",
            "rule": "public-key-base64url-has-canonical-trailing-bits",
            "error_location": ["channel_binding"],
            "error_message_contains": "owner_public_key must use canonical unpadded base64url",
            "payload": {
                **valid_payload,
                "channel_binding": {
                    **valid_payload["channel_binding"],
                    "owner_public_key": (
                        valid_payload["channel_binding"]["owner_public_key"][:-1] + "h"
                    ),
                },
            },
        },
        {
            "model": "owner_control_confirmation_envelope",
            "rule": "signature-base64url-has-canonical-trailing-bits",
            "error_location": [],
            "error_message_contains": "signature must use canonical unpadded base64url",
            "payload": {**valid_payload, "signature": valid_payload["signature"][:-1] + "R"},
        },
        {
            "model": "owner_control_confirmation_envelope",
            "rule": "owner-identity-matches",
            "error_location": [],
            "error_message_contains": "channel binding owner identity does not match",
            "payload": {
                **valid_payload,
                "channel_binding": changed_binding_payload,
                "challenge_response": response_for_changed_binding.model_dump(mode="json"),
            },
        },
        {
            "model": "owner_control_confirmation_envelope",
            "rule": "binding-digest-matches",
            "error_location": [],
            "error_message_contains": "channel binding digest does not match",
            "payload": {
                **valid_payload,
                "channel_binding": {
                    **valid_payload["channel_binding"],
                    "channel_session_id": "channel-session-substituted",
                },
            },
        },
        {
            "model": "owner_control_confirmation_envelope",
            "rule": "session-expires-after-issuance",
            "error_location": ["channel_binding"],
            "error_message_contains": "session_expires_at must be later than session_issued_at",
            "payload": {
                **valid_payload,
                "channel_binding": {
                    **valid_payload["channel_binding"],
                    "session_expires_at": valid_payload["channel_binding"]["session_issued_at"],
                },
            },
        },
        {
            "model": "owner_control_confirmation_envelope",
            "rule": "request-and-confirmation-stay-inside-session",
            "error_location": [],
            "error_message_contains": "approval request bounds must be inside",
            "payload": {
                **valid_payload,
                "channel_binding": changed_session_binding.model_dump(mode="json"),
                "challenge_response": response_for_changed_session.model_dump(mode="json"),
            },
        },
        {
            "model": "owner_control_confirmation_envelope",
            "rule": "tampered-signed-payload-is-rejected",
            "error_location": [],
            "payload": {
                **valid_payload,
                "challenge_response": {
                    **valid_payload["challenge_response"],
                    "confirmed_at": "2030-01-02T03:05:05+00:00",
                },
            },
            "verification": "invalid",
        },
        {
            "model": "owner_control_confirmation_envelope",
            "rule": "signature-from-wrong-private-key-is-rejected",
            "error_location": [],
            "payload": {**valid_payload, "signature": wrong_key_signature},
            "verification": "invalid",
        },
        {
            "model": "owner_control_confirmation_envelope",
            "rule": "cross-session-substitution-is-rejected",
            "error_location": [],
            "payload": {
                **valid_payload,
                "channel_binding": cross_session_binding.model_dump(mode="json"),
                "challenge_response": response_for_cross_session.model_dump(mode="json"),
            },
            "verification": "invalid",
        },
    ]


def _signature_declaration() -> dict[str, Any]:
    return {
        "algorithm": "ed25519",
        "domain": "launchplane-owner-control-confirmation-v1",
        "payload": "OwnerControlSignaturePayload",
        "payload_encoding": "canonical-json-utf8",
        "public_key_bytes": 32,
        "public_key_encoding": "base64url-unpadded",
        "signature_bytes": 64,
        "signature_encoding": "base64url-unpadded",
        "legacy_golden_channel_binding": "synthetic-placeholder-not-channel-binding-record",
        "contract_schema_version": _OWNER_CONTROL_SIGNATURE_DECLARATION_SCHEMA_VERSION,
    }


def _provenance_claims() -> list[OwnerControlHostPrincipalClaim]:
    return [
        OwnerControlHostPrincipalClaim(
            host_instance_id="synthetic-owner-control-host",
            principal_id="synthetic-owner-control-principal",
            principal_separation=principal_separation,
            key_custody=key_custody,
            gesture_source=gesture_source,
        )
        for principal_separation in get_args(OwnerControlPrincipalSeparationClaim)
        for key_custody in get_args(OwnerControlKeyCustodyClaim)
        for gesture_source in get_args(OwnerControlGestureSourceClaim)
    ]


def _provenance_record_for_claim(
    claim: OwnerControlHostPrincipalClaim,
) -> OwnerControlEnrollmentProvenanceRecord:
    _, signed_binding, _, _, _ = _shadow_verifier_fixture_models()
    binding = signed_binding.model_copy(
        update={"owner_public_key": _PROVENANCE_SYNTHETIC_PUBLIC_KEY}
    )
    binding_payload = binding.model_dump(mode="json")
    claim_payload = claim.model_dump(mode="json")
    return OwnerControlEnrollmentProvenanceRecord(
        channel_session_id=binding.channel_session_id,
        owner_github_id=binding.owner_github_id,
        binding_json=canonical_json_bytes(binding_payload).decode(),
        binding_sha256=owner_control_channel_binding_sha256(binding),
        host_principal_claim_json=canonical_json_bytes(claim_payload).decode(),
        host_principal_claim_sha256=owner_control_host_principal_claim_sha256(claim),
        enrolled_at="2030-01-02T03:00:05+00:00",
        enrollment_context=OWNER_CONTROL_ENROLLMENT_CONTEXT,
        server_observed_corroboration=OWNER_CONTROL_SERVER_CORROBORATION,
        provenance_tier=derive_owner_control_provenance_tier(
            claim=claim,
            server_observed_corroboration=OWNER_CONTROL_SERVER_CORROBORATION,
        ),
    )


def _provenance_vectors() -> list[dict[str, Any]]:
    vectors: list[dict[str, Any]] = []
    for claim in _provenance_claims():
        record = _provenance_record_for_claim(claim)
        claim_payload = claim.model_dump(mode="json")
        record_payload = record.model_dump(mode="json")
        vectors.append(
            {
                "claim": {
                    "canonical_json": canonical_json_bytes(claim_payload).decode(),
                    "payload": claim_payload,
                    "sha256": owner_control_host_principal_claim_sha256(claim),
                },
                "enrollment_provenance": {
                    "canonical_json": canonical_json_bytes(record_payload).decode(),
                    "payload": record_payload,
                    "sha256": canonical_json_sha256(record_payload),
                },
                "result": {
                    "authority_state": record.authority_state,
                    "authorizes_execution": record.authorizes_execution,
                    "provenance_tier": record.provenance_tier,
                    "server_observed_corroboration": record.server_observed_corroboration,
                },
            }
        )
    return vectors


def _negative_provenance_vectors() -> list[dict[str, Any]]:
    claim = _provenance_claims()[0]
    claim_payload = claim.model_dump(mode="json")
    record = _provenance_record_for_claim(claim)
    record_payload = record.model_dump(mode="json")
    published_keys = (
        _synthetic_owner_public_key(_artifact_synthetic_private_key()),
        _synthetic_owner_public_key(_artifact_synthetic_wrong_private_key()),
        _PROVENANCE_SYNTHETIC_PUBLIC_KEY,
    )
    return [
        {
            "model": "owner_control_host_principal_claim",
            "rule": "unknown-fields-are-rejected",
            "error_location": ["unexpected"],
            "payload": {**claim_payload, "unexpected": True},
        },
        {
            "model": "owner_control_host_principal_claim",
            "rule": "unknown-schema-versions-are-rejected",
            "error_location": ["schema_version"],
            "payload": {**claim_payload, "schema_version": 2},
        },
        {
            "model": "owner_control_enrollment_provenance",
            "rule": "claim-drift-is-rejected",
            "error_location": [],
            "payload": {
                **record_payload,
                "host_principal_claim_json": canonical_json_bytes(
                    {**claim_payload, "principal_id": "synthetic-changed-principal"}
                ).decode(),
            },
        },
        {
            "model": "owner_control_enrollment_provenance",
            "rule": "absent-corroboration-cannot-raise-trust",
            "error_location": ["provenance_tier"],
            "payload": {**record_payload, "provenance_tier": "corroborated"},
        },
        {
            "operation": "issue_challenge",
            "rule": "missing-enrollment-provenance-is-rejected",
            "channel_session_id": record.channel_session_id,
            "result": "reject",
        },
        *[
            {
                "operation": "enroll_channel_session",
                "rule": "published-synthetic-conformance-key-is-rejected",
                "owner_public_key_sha256": owner_control_public_key_sha256(public_key),
                "result": "reject",
                "runtime_guard_matches": is_published_owner_control_synthetic_public_key(
                    public_key
                ),
            }
            for public_key in published_keys
        ],
    ]


def _provenance_declaration() -> dict[str, Any]:
    return {
        "authority_state": OWNER_CONTROL_SHADOW_AUTHORITY_STATE,
        "authorizes_execution": False,
        "claim_source": "caller-declared",
        "enrollment_context": OWNER_CONTROL_ENROLLMENT_CONTEXT,
        "provenance_schema_version": OWNER_CONTROL_ENROLLMENT_PROVENANCE_SCHEMA_VERSION,
        "provenance_tier": OWNER_CONTROL_PROVENANCE_TIER,
        "runtime_synthetic_key_policy": "reject-published-conformance-keys",
        "server_observed_corroboration": OWNER_CONTROL_SERVER_CORROBORATION,
        "trust_derivation": "corroboration-only",
    }


def _v4_compatibility_declaration() -> dict[str, Any]:
    return {
        "container_schema_version": 4,
        "previous_container_schema_version": 3,
        "change_kind": "additive-descriptor-wire-schema-and-vectors",
        "unknown_container_versions": "reject",
        "wire_model_schema_versions": [1],
        "shadow_verifier_schema_versions": [OWNER_CONTROL_SHADOW_VERIFIER_SCHEMA_VERSION],
        "preserved_v2_section_sha256": dict(_PRESERVED_V2_SECTION_SHA256),
        "preserved_v2_descriptor_ids": sorted(_PRESERVED_V2_DESCRIPTOR_IDS),
        "preserved_v2_descriptor_vector_section_sha256": dict(
            _PRESERVED_V2_DESCRIPTOR_VECTOR_SECTION_SHA256
        ),
        "preserved_v2_schema_sha256": dict(_PRESERVED_V2_SCHEMA_SHA256),
        "schema_change": "descriptor literal expanded for managed-merge-train-policy-import",
    }


def _compatibility_declaration() -> dict[str, Any]:
    return {
        "container_schema_version": OWNER_CONTROL_CONTRACT_SCHEMA_VERSION,
        "previous_container_schema_version": _OWNER_CONTROL_PREVIOUS_CONTRACT_SCHEMA_VERSION,
        "change_kind": "additive-enrollment-provenance",
        "unknown_container_versions": "reject",
        "wire_model_schema_versions": [1],
        "shadow_verifier_schema_versions": [OWNER_CONTROL_SHADOW_VERIFIER_SCHEMA_VERSION],
        "enrollment_provenance_schema_versions": [
            OWNER_CONTROL_ENROLLMENT_PROVENANCE_SCHEMA_VERSION
        ],
        "preserved_v2_section_sha256": dict(_PRESERVED_V2_SECTION_SHA256),
        "preserved_v2_descriptor_ids": sorted(_PRESERVED_V2_DESCRIPTOR_IDS),
        "preserved_v2_descriptor_vector_section_sha256": dict(
            _PRESERVED_V2_DESCRIPTOR_VECTOR_SECTION_SHA256
        ),
        "preserved_v2_schema_sha256": dict(_PRESERVED_V2_SCHEMA_SHA256),
        "preserved_v4_section_sha256": dict(_PRESERVED_V4_SECTION_SHA256),
    }


def _validate_preserved_v2_sections(artifact: Mapping[str, Any]) -> None:
    for section, expected_sha256 in _PRESERVED_V2_SECTION_SHA256.items():
        if canonical_json_sha256(artifact[section]) != expected_sha256:
            raise OwnerControlContractError(
                f"Owner-control v2 section {section!r} changed without a compatibility break"
            )
    for section, expected_sha256 in _PRESERVED_V2_DESCRIPTOR_VECTOR_SECTION_SHA256.items():
        section_value = [
            vector
            for vector in artifact[section]
            if vector.get("descriptor_id") in _PRESERVED_V2_DESCRIPTOR_IDS
        ]
        if canonical_json_sha256(section_value) != expected_sha256:
            raise OwnerControlContractError(
                f"Owner-control v2 descriptor-scoped section {section!r} changed without a compatibility break"
            )
    for schema_name, expected_sha256 in _PRESERVED_V2_SCHEMA_SHA256.items():
        preserved_schema = _preserve_v2_descriptor_enums(artifact["schemas"][schema_name])
        if canonical_json_sha256(preserved_schema) != expected_sha256:
            raise OwnerControlContractError(
                f"Owner-control v2 schema {schema_name!r} changed without a compatibility break"
            )


def _preserve_v2_descriptor_enums(value: Any) -> Any:
    if isinstance(value, Mapping):
        normalized = {key: _preserve_v2_descriptor_enums(item) for key, item in value.items()}
        enum_values = normalized.get("enum")
        if (
            isinstance(enum_values, list)
            and len(enum_values) == len(_ALL_DESCRIPTOR_IDS)
            and set(enum_values) == _ALL_DESCRIPTOR_IDS
        ):
            normalized["enum"] = [
                descriptor_id
                for descriptor_id in enum_values
                if descriptor_id in _PRESERVED_V2_DESCRIPTOR_IDS
            ]
        return normalized
    if isinstance(value, list):
        return [_preserve_v2_descriptor_enums(item) for item in value]
    return value


def _validate_preserved_v4_sections(artifact: Mapping[str, Any]) -> None:
    for section, expected_sha256 in _PRESERVED_V4_SECTION_SHA256.items():
        if section == "schema_version":
            actual_sha256 = canonical_json_sha256(4)
        elif section == "compatibility":
            actual_sha256 = canonical_json_sha256(_v4_compatibility_declaration())
        else:
            actual_sha256 = canonical_json_sha256(artifact[section])
        if actual_sha256 != expected_sha256:
            raise OwnerControlContractError(
                f"Owner-control v4 section {section!r} changed without a compatibility break"
            )


def _build_owner_control_contract() -> dict[str, Any]:
    descriptors = list_privileged_operation_descriptors()
    return {
        "schema_version": OWNER_CONTROL_CONTRACT_SCHEMA_VERSION,
        "compatibility": _compatibility_declaration(),
        "canonical_json": {
            "encoding": "utf-8",
            "ensure_ascii": True,
            "integer_max": 2**63 - 1,
            "integer_min": -(2**63),
            "non_finite_numbers": "rejected",
            "number_domain": "signed-64-bit-integers-only",
            "object_key_order": "unicode-code-point",
            "object_keys": "strings-only",
            "separators": [",", ":"],
            "trailing_newline": False,
        },
        "signature_declaration": _signature_declaration(),
        "canonicalization_vectors": _canonicalization_vectors(),
        "schemas": {
            "approval_request": ApprovalRequest.model_json_schema(),
            "channel_binding_record": ChannelBindingRecord.model_json_schema(),
            "challenge_response": ChallengeResponse.model_json_schema(),
            "owner_control_confirmation_envelope": OwnerControlConfirmationEnvelope.model_json_schema(),
            "owner_control_signature_payload": OwnerControlSignaturePayload.model_json_schema(),
            "server_review_payload": ServerReviewPayload.model_json_schema(),
        },
        "golden_vectors": [
            _golden_vector(
                descriptor_id=descriptor.descriptor_id,
                descriptor_version=descriptor.descriptor_version,
                safety_class=descriptor.safety_class,
            )
            for descriptor in descriptors
        ],
        "confirmation_golden_vectors": [
            _confirmation_golden_vector(
                descriptor_id=descriptor.descriptor_id,
                descriptor_version=descriptor.descriptor_version,
                safety_class=descriptor.safety_class,
            )
            for descriptor in descriptors
        ],
        "negative_vectors": _negative_vectors(),
        "negative_confirmation_vectors": _negative_confirmation_vectors(),
        "verification_state_vectors": _verification_state_vectors(),
        "challenge_lifecycle_vectors": _challenge_lifecycle_vectors(),
        "provenance_declaration": _provenance_declaration(),
        "provenance_schemas": {
            "owner_control_host_principal_claim": OwnerControlHostPrincipalClaim.model_json_schema(),
            "owner_control_enrollment_provenance": OwnerControlEnrollmentProvenanceRecord.model_json_schema(),
        },
        "provenance_vectors": _provenance_vectors(),
        "negative_provenance_vectors": _negative_provenance_vectors(),
    }


def build_owner_control_contract() -> dict[str, Any]:
    """Build the deterministic, public owner-control conformance artifact."""

    artifact = _build_owner_control_contract()
    _validate_preserved_v2_sections(artifact)
    _validate_preserved_v4_sections(artifact)
    return artifact


def _require_exact_keys(value: Mapping[str, Any], expected: set[str], location: str) -> None:
    actual = set(value)
    if actual != expected:
        raise OwnerControlContractError(
            f"Unexpected owner-control contract keys at {location}: {sorted(actual)!r}"
        )


def validate_owner_control_contract(artifact: Mapping[str, Any]) -> None:
    """Fail closed when an owner-control artifact drifts from its registered contract."""

    _require_exact_keys(
        artifact,
        {
            "schema_version",
            "compatibility",
            "canonical_json",
            "signature_declaration",
            "canonicalization_vectors",
            "schemas",
            "golden_vectors",
            "confirmation_golden_vectors",
            "negative_vectors",
            "negative_confirmation_vectors",
            "verification_state_vectors",
            "challenge_lifecycle_vectors",
            "provenance_declaration",
            "provenance_schemas",
            "provenance_vectors",
            "negative_provenance_vectors",
        },
        "root",
    )
    if artifact["schema_version"] != OWNER_CONTROL_CONTRACT_SCHEMA_VERSION:
        raise OwnerControlContractError("Unsupported owner-control contract schema version")
    expected = _build_owner_control_contract()
    if artifact["compatibility"] != expected["compatibility"]:
        raise OwnerControlContractError("Owner-control compatibility declaration drifted")
    _validate_preserved_v2_sections(artifact)
    _validate_preserved_v4_sections(artifact)
    if artifact["canonical_json"] != expected["canonical_json"]:
        raise OwnerControlContractError("Owner-control canonical JSON declaration drifted")
    if artifact["signature_declaration"] != expected["signature_declaration"]:
        raise OwnerControlContractError("Owner-control signature declaration drifted")
    if artifact["canonicalization_vectors"] != expected["canonicalization_vectors"]:
        raise OwnerControlContractError("Owner-control canonicalization vectors drifted")
    if artifact["schemas"] != expected["schemas"]:
        raise OwnerControlContractError("Owner-control JSON schemas drifted")
    vectors = artifact["golden_vectors"]
    if not isinstance(vectors, list):
        raise OwnerControlContractError("Owner-control golden vectors must be a list")
    if vectors != expected["golden_vectors"]:
        raise OwnerControlContractError("Owner-control golden vectors drifted from the registry")
    if artifact["confirmation_golden_vectors"] != expected["confirmation_golden_vectors"]:
        raise OwnerControlContractError("Owner-control confirmation golden vectors drifted")
    if artifact["negative_vectors"] != expected["negative_vectors"]:
        raise OwnerControlContractError("Owner-control negative vectors drifted")
    if artifact["negative_confirmation_vectors"] != expected["negative_confirmation_vectors"]:
        raise OwnerControlContractError("Owner-control negative confirmation vectors drifted")
    if artifact["verification_state_vectors"] != expected["verification_state_vectors"]:
        raise OwnerControlContractError("Owner-control verification state vectors drifted")
    if artifact["challenge_lifecycle_vectors"] != expected["challenge_lifecycle_vectors"]:
        raise OwnerControlContractError("Owner-control challenge lifecycle vectors drifted")
    if artifact["provenance_declaration"] != expected["provenance_declaration"]:
        raise OwnerControlContractError("Owner-control provenance declaration drifted")
    if artifact["provenance_schemas"] != expected["provenance_schemas"]:
        raise OwnerControlContractError("Owner-control provenance schemas drifted")
    if artifact["provenance_vectors"] != expected["provenance_vectors"]:
        raise OwnerControlContractError("Owner-control provenance vectors drifted")
    if artifact["negative_provenance_vectors"] != expected["negative_provenance_vectors"]:
        raise OwnerControlContractError("Owner-control negative provenance vectors drifted")


def write_owner_control_contract(output_path: Path) -> Path:
    artifact = build_owner_control_contract()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path
