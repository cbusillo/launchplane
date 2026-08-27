from __future__ import annotations

from datetime import datetime, timezone
import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.privileged_operation import PrivilegedOperationDescriptorId


OWNER_CONTROL_SCHEMA_VERSION: Final[Literal[1]] = 1

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_OPERATION_ID_PATTERN = re.compile(r"^privileged-operation-[0-9a-f]{32}$")
_NONCE_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,127}$")
_REVIEW_KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_CANONICAL_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")


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


def owner_control_approval_request_digest(request: ApprovalRequest) -> str:
    """Return the canonical digest shared by Launchplane and the owner-control host."""

    return canonical_json_sha256(request.model_dump(mode="json"))


def owner_control_challenge_response_digest(response: ChallengeResponse) -> str:
    """Return the canonical digest of an owner-control challenge response."""

    return canonical_json_sha256(response.model_dump(mode="json"))
