from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from control_plane.contracts.canonical_json import canonical_json_bytes, canonical_json_sha256
from control_plane.contracts.owner_control import (
    ApprovalRequest,
    ChallengeResponse,
    ReviewItem,
    ServerReviewPayload,
    owner_control_approval_request_digest,
    owner_control_challenge_response_digest,
)
from control_plane.contracts.privileged_operation import (
    PrivilegedOperationDescriptorId,
    PrivilegedOperationSafetyClass,
)
from control_plane.privileged_operation_registry import list_privileged_operation_descriptors
from control_plane.privileged_operation_registry import read_privileged_operation_descriptor


OWNER_CONTROL_CONTRACT_SCHEMA_VERSION = 1


class OwnerControlContractError(ValueError):
    pass


def _digest_for_vector(*, descriptor_id: PrivilegedOperationDescriptorId, label: str) -> str:
    return canonical_json_sha256(
        {
            "descriptor_id": descriptor_id,
            "label": label,
            "schema_version": OWNER_CONTROL_CONTRACT_SCHEMA_VERSION,
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
            "canonical_json": canonical_json_bytes(request_payload).decode("utf-8"),
            "payload": request_payload,
            "sha256": owner_control_approval_request_digest(request),
        },
        "challenge_response": {
            "canonical_json": canonical_json_bytes(response_payload).decode("utf-8"),
            "payload": response_payload,
            "sha256": owner_control_challenge_response_digest(response),
        },
    }


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
            "canonical_json": canonical_json_bytes(payload).decode("utf-8"),
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


def _build_owner_control_contract() -> dict[str, Any]:
    descriptors = list_privileged_operation_descriptors()
    return {
        "schema_version": OWNER_CONTROL_CONTRACT_SCHEMA_VERSION,
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
        "canonicalization_vectors": _canonicalization_vectors(),
        "schemas": {
            "approval_request": ApprovalRequest.model_json_schema(),
            "challenge_response": ChallengeResponse.model_json_schema(),
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
        "negative_vectors": _negative_vectors(),
    }


def build_owner_control_contract() -> dict[str, Any]:
    """Build the deterministic, public owner-control conformance artifact."""

    return _build_owner_control_contract()


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
            "canonical_json",
            "canonicalization_vectors",
            "schemas",
            "golden_vectors",
            "negative_vectors",
        },
        "root",
    )
    if artifact["schema_version"] != OWNER_CONTROL_CONTRACT_SCHEMA_VERSION:
        raise OwnerControlContractError("Unsupported owner-control contract schema version")
    expected = _build_owner_control_contract()
    if artifact["canonical_json"] != expected["canonical_json"]:
        raise OwnerControlContractError("Owner-control canonical JSON declaration drifted")
    if artifact["canonicalization_vectors"] != expected["canonicalization_vectors"]:
        raise OwnerControlContractError("Owner-control canonicalization vectors drifted")
    if artifact["schemas"] != expected["schemas"]:
        raise OwnerControlContractError("Owner-control JSON schemas drifted")
    vectors = artifact["golden_vectors"]
    if not isinstance(vectors, list):
        raise OwnerControlContractError("Owner-control golden vectors must be a list")
    if vectors != expected["golden_vectors"]:
        raise OwnerControlContractError("Owner-control golden vectors drifted from the registry")
    if artifact["negative_vectors"] != expected["negative_vectors"]:
        raise OwnerControlContractError("Owner-control negative vectors drifted")


def write_owner_control_contract(output_path: Path) -> Path:
    artifact = build_owner_control_contract()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(artifact, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    return output_path
