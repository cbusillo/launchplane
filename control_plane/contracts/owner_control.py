from __future__ import annotations

import base64
import binascii
from datetime import datetime, timezone
import re
from typing import Final, Literal

from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.canonical_json import canonical_json_bytes, canonical_json_sha256
from control_plane.contracts.privileged_operation import PrivilegedOperationDescriptorId


OWNER_CONTROL_SCHEMA_VERSION: Final[Literal[1]] = 1
OWNER_CONTROL_SIGNATURE_DOMAIN: Final[Literal["launchplane-owner-control-confirmation-v1"]] = (
    "launchplane-owner-control-confirmation-v1"
)
OWNER_CONTROL_SIGNATURE_ALGORITHM: Final[Literal["ed25519"]] = "ed25519"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_ID_PATTERN = re.compile(r"^privileged-operation-[0-9a-f]{32}$")
_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,127}$")
_REVIEW_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CANONICAL_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
_BASE64URL_PATTERN = re.compile(r"^[A-Za-z0-9_-]+$")


def _canonical_sha256(value: str, field_name: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def _canonical_identifier(value: str, field_name: str) -> str:
    if _IDENTIFIER_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a canonical identifier")
    return value


def _canonical_timestamp(value: str, field_name: str) -> str:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    canonical = parsed.astimezone(timezone.utc).isoformat()
    if value != canonical:
        raise ValueError(f"{field_name} must use canonical UTC ISO-8601 form")
    return value


def _canonical_base64url(value: str, field_name: str, expected_length: int) -> str:
    if _BASE64URL_PATTERN.fullmatch(value) is None or len(value) % 4 == 1:
        raise ValueError(f"{field_name} must be unpadded base64url")
    try:
        decoded = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (binascii.Error, ValueError) as error:
        raise ValueError(f"{field_name} must be unpadded base64url") from error
    if len(decoded) != expected_length:
        raise ValueError(f"{field_name} must encode exactly {expected_length} bytes")
    if base64.urlsafe_b64encode(decoded).decode("ascii").rstrip("=") != value:
        raise ValueError(f"{field_name} must use canonical unpadded base64url")
    return value


def _decode_canonical_base64url(value: str, field_name: str, expected_length: int) -> bytes:
    _canonical_base64url(value, field_name, expected_length)
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class ReviewItem(BaseModel):
    """One server-authored, display-ready field in an owner review payload."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    key: str = Field(pattern=_REVIEW_KEY_PATTERN.pattern)
    label: str = Field(min_length=1, max_length=120)
    value: str = Field(min_length=1, max_length=4000)

    @model_validator(mode="after")
    def _validate_review_item(self) -> "ReviewItem":
        if _REVIEW_KEY_PATTERN.fullmatch(self.key) is None:
            raise ValueError("review item key must be canonical snake case")
        if self.label != self.label.strip() or self.value != self.value.strip():
            raise ValueError("review item labels and values must not have surrounding whitespace")
        return self


class ServerReviewPayload(BaseModel):
    """Canonical server-authored review content rendered for one owner decision."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = OWNER_CONTROL_SCHEMA_VERSION
    review_id: str = Field(pattern=_IDENTIFIER_PATTERN.pattern)
    title: str = Field(min_length=1, max_length=200)
    summary: str = Field(min_length=1, max_length=4000)
    items: tuple[ReviewItem, ...] = Field(min_length=1, max_length=32)

    @model_validator(mode="after")
    def _validate_review_payload(self) -> "ServerReviewPayload":
        if self.schema_version != OWNER_CONTROL_SCHEMA_VERSION:
            raise ValueError("Unsupported owner-control review payload schema version.")
        _canonical_identifier(self.review_id, "review_id")
        if self.title != self.title.strip() or self.summary != self.summary.strip():
            raise ValueError("review title and summary must not have surrounding whitespace")
        if len({item.key for item in self.items}) != len(self.items):
            raise ValueError("review item keys must be unique")
        return self


class ApprovalRequest(BaseModel):
    """Exact, versioned owner-control challenge input issued by Launchplane."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = OWNER_CONTROL_SCHEMA_VERSION
    operation_id: str = Field(pattern=_OPERATION_ID_PATTERN.pattern)
    descriptor_id: PrivilegedOperationDescriptorId
    descriptor_version: Literal[1]
    request_digest: str = Field(pattern=_SHA256_PATTERN.pattern)
    plan_digest: str = Field(pattern=_SHA256_PATTERN.pattern)
    evidence_digest: str = Field(pattern=_SHA256_PATTERN.pattern)
    pre_state_digest: str = Field(pattern=_SHA256_PATTERN.pattern)
    policy_record_id: str = Field(pattern=_IDENTIFIER_PATTERN.pattern)
    policy_revision: int = Field(ge=1, le=2**63 - 1)
    policy_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    owner_github_id: int = Field(ge=1, le=2**63 - 1)
    server_review: ServerReviewPayload
    nonce: str = Field(pattern=_NONCE_PATTERN.pattern)
    issued_at: str = Field(pattern=_CANONICAL_TIMESTAMP_PATTERN.pattern)
    expires_at: str = Field(pattern=_CANONICAL_TIMESTAMP_PATTERN.pattern)

    @model_validator(mode="after")
    def _validate_approval_request(self) -> "ApprovalRequest":
        if self.schema_version != OWNER_CONTROL_SCHEMA_VERSION:
            raise ValueError("Unsupported owner-control approval request schema version.")
        if _OPERATION_ID_PATTERN.fullmatch(self.operation_id) is None:
            raise ValueError("operation_id must be a canonical privileged-operation identifier")
        if self.descriptor_version != 1:
            raise ValueError("Unsupported owner-control privileged-operation descriptor version.")
        for field_name in (
            "request_digest",
            "plan_digest",
            "evidence_digest",
            "pre_state_digest",
            "policy_sha256",
        ):
            _canonical_sha256(str(getattr(self, field_name)), field_name)
        _canonical_identifier(self.policy_record_id, "policy_record_id")
        if _NONCE_PATTERN.fullmatch(self.nonce) is None:
            raise ValueError("nonce must be a canonical owner-control nonce")
        _canonical_timestamp(self.issued_at, "issued_at")
        _canonical_timestamp(self.expires_at, "expires_at")
        issued_at = datetime.fromisoformat(self.issued_at)
        expires_at = datetime.fromisoformat(self.expires_at)
        if expires_at <= issued_at:
            raise ValueError("expires_at must be later than issued_at")
        return self


class ChallengeResponse(BaseModel):
    """Owner-channel confirmation bound to one exact approval request."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = OWNER_CONTROL_SCHEMA_VERSION
    approval_request: ApprovalRequest
    approval_request_digest: str = Field(pattern=_SHA256_PATTERN.pattern)
    decision: Literal["approved"]
    channel_binding_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    confirmed_at: str = Field(pattern=_CANONICAL_TIMESTAMP_PATTERN.pattern)

    @model_validator(mode="after")
    def _validate_challenge_response(self) -> "ChallengeResponse":
        if self.schema_version != OWNER_CONTROL_SCHEMA_VERSION:
            raise ValueError("Unsupported owner-control challenge response schema version.")
        _canonical_sha256(self.approval_request_digest, "approval_request_digest")
        if self.approval_request_digest != owner_control_approval_request_digest(
            self.approval_request
        ):
            raise ValueError("approval_request_digest must bind the exact approval request")
        _canonical_sha256(self.channel_binding_sha256, "channel_binding_sha256")
        _canonical_timestamp(self.confirmed_at, "confirmed_at")
        confirmed_at = datetime.fromisoformat(self.confirmed_at)
        issued_at = datetime.fromisoformat(self.approval_request.issued_at)
        expires_at = datetime.fromisoformat(self.approval_request.expires_at)
        if confirmed_at < issued_at:
            raise ValueError("confirmed_at must not be earlier than challenge issuance")
        if confirmed_at > expires_at:
            raise ValueError("confirmed_at must not be later than the approval request expiry")
        return self


class ChannelBindingRecord(BaseModel):
    """Immutable versioned binding between an owner key and one channel session."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = OWNER_CONTROL_SCHEMA_VERSION
    channel_session_id: str = Field(pattern=_IDENTIFIER_PATTERN.pattern)
    owner_github_id: int = Field(ge=1, le=2**63 - 1)
    signature_algorithm: Literal["ed25519"] = OWNER_CONTROL_SIGNATURE_ALGORITHM
    owner_public_key: str = Field(
        min_length=43,
        max_length=43,
        pattern=_BASE64URL_PATTERN.pattern,
    )
    session_issued_at: str = Field(pattern=_CANONICAL_TIMESTAMP_PATTERN.pattern)
    session_expires_at: str = Field(pattern=_CANONICAL_TIMESTAMP_PATTERN.pattern)

    @model_validator(mode="after")
    def _validate_channel_binding_record(self) -> "ChannelBindingRecord":
        if self.schema_version != OWNER_CONTROL_SCHEMA_VERSION:
            raise ValueError("Unsupported owner-control channel binding schema version.")
        _canonical_identifier(self.channel_session_id, "channel_session_id")
        if self.signature_algorithm != OWNER_CONTROL_SIGNATURE_ALGORITHM:
            raise ValueError("Unsupported owner-control signature algorithm.")
        _canonical_base64url(self.owner_public_key, "owner_public_key", 32)
        _canonical_timestamp(self.session_issued_at, "session_issued_at")
        _canonical_timestamp(self.session_expires_at, "session_expires_at")
        session_issued_at = datetime.fromisoformat(self.session_issued_at)
        session_expires_at = datetime.fromisoformat(self.session_expires_at)
        if session_expires_at <= session_issued_at:
            raise ValueError("session_expires_at must be later than session_issued_at")
        return self


class OwnerControlSignaturePayload(BaseModel):
    """Exact domain-separated payload authenticated by an owner signature."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = OWNER_CONTROL_SCHEMA_VERSION
    domain: Literal["launchplane-owner-control-confirmation-v1"] = OWNER_CONTROL_SIGNATURE_DOMAIN
    challenge_response: ChallengeResponse

    @model_validator(mode="after")
    def _validate_signature_payload(self) -> "OwnerControlSignaturePayload":
        if self.schema_version != OWNER_CONTROL_SCHEMA_VERSION:
            raise ValueError("Unsupported owner-control signature payload schema version.")
        if self.domain != OWNER_CONTROL_SIGNATURE_DOMAIN:
            raise ValueError("Unsupported owner-control signature payload domain.")
        return self


class OwnerControlConfirmationEnvelope(BaseModel):
    """Signed owner confirmation bound to one channel session and challenge."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = OWNER_CONTROL_SCHEMA_VERSION
    channel_binding: ChannelBindingRecord
    challenge_response: ChallengeResponse
    signature_algorithm: Literal["ed25519"] = OWNER_CONTROL_SIGNATURE_ALGORITHM
    signature: str = Field(
        min_length=86,
        max_length=86,
        pattern=_BASE64URL_PATTERN.pattern,
    )

    @model_validator(mode="after")
    def _validate_confirmation_envelope(self) -> "OwnerControlConfirmationEnvelope":
        if self.schema_version != OWNER_CONTROL_SCHEMA_VERSION:
            raise ValueError("Unsupported owner-control confirmation envelope schema version.")
        if self.signature_algorithm != OWNER_CONTROL_SIGNATURE_ALGORITHM:
            raise ValueError("Unsupported owner-control signature algorithm.")
        _canonical_base64url(self.signature, "signature", 64)
        if self.challenge_response.channel_binding_sha256 != owner_control_channel_binding_sha256(
            self.channel_binding
        ):
            raise ValueError(
                "challenge_response channel binding digest does not match the binding record"
            )
        if (
            self.challenge_response.approval_request.owner_github_id
            != self.channel_binding.owner_github_id
        ):
            raise ValueError(
                "channel binding owner identity does not match the approval request owner"
            )
        session_issued_at = datetime.fromisoformat(self.channel_binding.session_issued_at)
        session_expires_at = datetime.fromisoformat(self.channel_binding.session_expires_at)
        request_issued_at = datetime.fromisoformat(
            self.challenge_response.approval_request.issued_at
        )
        request_expires_at = datetime.fromisoformat(
            self.challenge_response.approval_request.expires_at
        )
        confirmed_at = datetime.fromisoformat(self.challenge_response.confirmed_at)
        if request_issued_at < session_issued_at or request_expires_at > session_expires_at:
            raise ValueError("approval request bounds must be inside the channel session interval")
        if confirmed_at < session_issued_at or confirmed_at > session_expires_at:
            raise ValueError("confirmation time must be inside the channel session interval")
        return self


def owner_control_approval_request_digest(request: ApprovalRequest) -> str:
    """Return the canonical digest shared by Launchplane and the owner-control host."""

    return canonical_json_sha256(request.model_dump(mode="json"))


def owner_control_challenge_response_digest(response: ChallengeResponse) -> str:
    """Return the canonical digest of an owner-control challenge response."""

    return canonical_json_sha256(response.model_dump(mode="json"))


def owner_control_channel_binding_sha256(binding: ChannelBindingRecord) -> str:
    """Return the digest of the exact canonical channel binding record payload."""

    return canonical_json_sha256(binding.model_dump(mode="json"))


def owner_control_signature_payload(response: ChallengeResponse) -> OwnerControlSignaturePayload:
    """Build the exact domain-separated payload covered by an owner signature."""

    return OwnerControlSignaturePayload(challenge_response=response)


def owner_control_signature_payload_bytes(response: ChallengeResponse) -> bytes:
    """Return canonical JSON bytes for the exact owner signature preimage."""

    return canonical_json_bytes(owner_control_signature_payload(response).model_dump(mode="json"))


def verify_owner_control_confirmation_signature(
    envelope: OwnerControlConfirmationEnvelope,
) -> bool:
    """Verify only the envelope's internal Ed25519 proof, not server authorization state."""

    try:
        validated_envelope = OwnerControlConfirmationEnvelope.model_validate_json(
            canonical_json_bytes(envelope.model_dump(mode="json"))
        )
        public_key = Ed25519PublicKey.from_public_bytes(
            _decode_canonical_base64url(
                validated_envelope.channel_binding.owner_public_key, "owner_public_key", 32
            )
        )
        signature = _decode_canonical_base64url(validated_envelope.signature, "signature", 64)
        public_key.verify(
            signature,
            owner_control_signature_payload_bytes(validated_envelope.challenge_response),
        )
    except (AttributeError, InvalidSignature, TypeError, ValueError):
        return False
    return True
