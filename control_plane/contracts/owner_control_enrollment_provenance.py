from __future__ import annotations

import base64
from datetime import datetime, timezone
import hashlib
import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.canonical_json import canonical_json_bytes, canonical_json_sha256
from control_plane.contracts.owner_control import (
    ChannelBindingRecord,
    owner_control_channel_binding_sha256,
)
from control_plane.contracts.owner_control_shadow_verifier import (
    OWNER_CONTROL_SHADOW_AUTHORITY_STATE,
    OwnerControlChannelSessionRecord,
)


OWNER_CONTROL_ENROLLMENT_PROVENANCE_SCHEMA_VERSION: Final[Literal[1]] = 1
OWNER_CONTROL_ENROLLMENT_CONTEXT: Final[Literal["postgres_record_store"]] = "postgres_record_store"
OWNER_CONTROL_SERVER_CORROBORATION: Final[Literal["none"]] = "none"
OWNER_CONTROL_PROVENANCE_TIER: Final[Literal["self_asserted"]] = "self_asserted"
_OPAQUE_IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,255}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")
_PUBLISHED_SYNTHETIC_PUBLIC_KEY_SHA256: Final = frozenset(
    {
        "141ddf2e77d4f690748cf74ecd390d44687d477b31b8931fa37abd02c35dbaba",
        "56475aa75463474c0285df5dbf2bcab73da651358839e9b77481b2eab107708c",
        "66687aadf862bd776c8fc18b8e9f8e20089714856ee233b3902a591d0d5f2925",
    }
)

OwnerControlPrincipalSeparationClaim = Literal[
    "not_claimed",
    "shared_runtime",
    "separate_os_principal",
]
OwnerControlKeyCustodyClaim = Literal[
    "not_claimed",
    "software_backed",
    "hardware_backed",
]
OwnerControlGestureSourceClaim = Literal[
    "not_claimed",
    "local_interactive",
]
OwnerControlServerObservedCorroboration = Literal["none"]
OwnerControlProvenanceTier = Literal["self_asserted"]


class OwnerControlEnrollmentProvenanceConflictError(ValueError):
    """Raised when immutable enrollment provenance is absent or conflicts."""


def _canonical_timestamp(value: str, field_name: str) -> datetime:
    if _CANONICAL_TIMESTAMP_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must use whole-second canonical UTC form")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    if value != parsed.astimezone(timezone.utc).isoformat():
        raise ValueError(f"{field_name} must use canonical ISO-8601 form")
    return parsed


class OwnerControlHostPrincipalClaim(BaseModel):
    """Caller-declared host-principal properties that confer no trust."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = OWNER_CONTROL_ENROLLMENT_PROVENANCE_SCHEMA_VERSION
    host_instance_id: str = Field(pattern=_OPAQUE_IDENTIFIER_PATTERN.pattern)
    principal_id: str = Field(pattern=_OPAQUE_IDENTIFIER_PATTERN.pattern)
    principal_separation: OwnerControlPrincipalSeparationClaim
    key_custody: OwnerControlKeyCustodyClaim
    gesture_source: OwnerControlGestureSourceClaim


def owner_control_host_principal_claim_sha256(
    claim: OwnerControlHostPrincipalClaim,
) -> str:
    return canonical_json_sha256(claim.model_dump(mode="json"))


def owner_control_public_key_sha256(public_key: str) -> str:
    padding = "=" * (-len(public_key) % 4)
    try:
        raw_key = base64.b64decode(public_key + padding, altchars=b"-_", validate=True)
    except ValueError as error:
        raise ValueError("owner_public_key must be valid unpadded base64url") from error
    if len(raw_key) != 32:
        raise ValueError("owner_public_key must decode to exactly 32 bytes")
    return hashlib.sha256(raw_key).hexdigest()


def is_published_owner_control_synthetic_public_key(public_key: str) -> bool:
    """Return whether a key is one of the public artifact-only fixtures."""

    return owner_control_public_key_sha256(public_key) in _PUBLISHED_SYNTHETIC_PUBLIC_KEY_SHA256


def derive_owner_control_provenance_tier(
    *,
    claim: OwnerControlHostPrincipalClaim,
    server_observed_corroboration: OwnerControlServerObservedCorroboration,
) -> OwnerControlProvenanceTier:
    """Derive the only currently reachable, non-authorizing provenance tier."""

    del claim
    if server_observed_corroboration != OWNER_CONTROL_SERVER_CORROBORATION:
        raise ValueError("unsupported owner-control server corroboration")
    return OWNER_CONTROL_PROVENANCE_TIER


class OwnerControlEnrollmentProvenanceRecord(BaseModel):
    """Immutable server-observed enrollment record for one exact session and claim."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = OWNER_CONTROL_ENROLLMENT_PROVENANCE_SCHEMA_VERSION
    channel_session_id: str
    owner_github_id: int = Field(ge=1, le=9_223_372_036_854_775_807)
    binding_json: str = Field(min_length=2, max_length=20_000)
    binding_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    host_principal_claim_json: str = Field(min_length=2, max_length=20_000)
    host_principal_claim_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    enrolled_at: str
    enrollment_context: Literal["postgres_record_store"] = OWNER_CONTROL_ENROLLMENT_CONTEXT
    server_observed_corroboration: Literal["none"] = OWNER_CONTROL_SERVER_CORROBORATION
    provenance_tier: Literal["self_asserted"] = OWNER_CONTROL_PROVENANCE_TIER
    authority_state: Literal["inert"] = OWNER_CONTROL_SHADOW_AUTHORITY_STATE
    authorizes_execution: Literal[False] = False

    @model_validator(mode="after")
    def _validate_record(self) -> "OwnerControlEnrollmentProvenanceRecord":
        try:
            binding = ChannelBindingRecord.model_validate_json(self.binding_json)
        except ValueError as error:
            raise ValueError("binding_json must contain a valid channel binding") from error
        if canonical_json_bytes(binding.model_dump(mode="json")).decode() != self.binding_json:
            raise ValueError("binding_json must contain exact canonical channel-binding bytes")
        if self.channel_session_id != binding.channel_session_id:
            raise ValueError("channel_session_id must match the stored channel binding")
        if self.owner_github_id != binding.owner_github_id:
            raise ValueError("owner_github_id must match the stored channel binding")
        if self.binding_sha256 != owner_control_channel_binding_sha256(binding):
            raise ValueError("binding_sha256 must match the stored channel binding")
        try:
            claim = OwnerControlHostPrincipalClaim.model_validate_json(
                self.host_principal_claim_json
            )
        except ValueError as error:
            raise ValueError(
                "host_principal_claim_json must contain a valid host-principal claim"
            ) from error
        if (
            canonical_json_bytes(claim.model_dump(mode="json")).decode()
            != self.host_principal_claim_json
        ):
            raise ValueError("host_principal_claim_json must contain exact canonical claim bytes")
        if self.host_principal_claim_sha256 != owner_control_host_principal_claim_sha256(claim):
            raise ValueError("host_principal_claim_sha256 must match the stored claim")
        enrolled_at = _canonical_timestamp(self.enrolled_at, "enrolled_at")
        issued_at = _canonical_timestamp(binding.session_issued_at, "session_issued_at")
        expires_at = _canonical_timestamp(binding.session_expires_at, "session_expires_at")
        if enrolled_at < issued_at or enrolled_at > expires_at:
            raise ValueError("enrolled_at must be inside the channel session interval")
        if self.provenance_tier != derive_owner_control_provenance_tier(
            claim=claim,
            server_observed_corroboration=self.server_observed_corroboration,
        ):
            raise ValueError("provenance_tier must match server-observed corroboration")
        return self

    def channel_binding(self) -> ChannelBindingRecord:
        return ChannelBindingRecord.model_validate_json(self.binding_json)

    def host_principal_claim(self) -> OwnerControlHostPrincipalClaim:
        return OwnerControlHostPrincipalClaim.model_validate_json(self.host_principal_claim_json)


class OwnerControlChannelEnrollment(BaseModel):
    """Atomic storage result that keeps a channel session and provenance inseparable."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    session: OwnerControlChannelSessionRecord
    provenance: OwnerControlEnrollmentProvenanceRecord

    @model_validator(mode="after")
    def _validate_pair(self) -> "OwnerControlChannelEnrollment":
        if self.session.channel_session_id != self.provenance.channel_session_id:
            raise ValueError("session and provenance must use the same channel_session_id")
        if self.session.owner_github_id != self.provenance.owner_github_id:
            raise ValueError("session and provenance must use the same owner_github_id")
        if self.session.binding_json != self.provenance.binding_json:
            raise ValueError("session and provenance must bind the same canonical bytes")
        if self.session.binding_sha256 != self.provenance.binding_sha256:
            raise ValueError("session and provenance must bind the same digest")
        if self.session.enrolled_at != self.provenance.enrolled_at:
            raise ValueError("session and provenance must share the DB enrollment time")
        return self


def build_owner_control_enrollment_provenance_record(
    *,
    binding: ChannelBindingRecord,
    claim: OwnerControlHostPrincipalClaim,
    enrolled_at: str,
) -> OwnerControlEnrollmentProvenanceRecord:
    if is_published_owner_control_synthetic_public_key(binding.owner_public_key):
        raise OwnerControlEnrollmentProvenanceConflictError(
            "Published owner-control conformance keys cannot be enrolled."
        )
    binding_json = canonical_json_bytes(binding.model_dump(mode="json")).decode()
    claim_json = canonical_json_bytes(claim.model_dump(mode="json")).decode()
    corroboration = OWNER_CONTROL_SERVER_CORROBORATION
    return OwnerControlEnrollmentProvenanceRecord(
        channel_session_id=binding.channel_session_id,
        owner_github_id=binding.owner_github_id,
        binding_json=binding_json,
        binding_sha256=owner_control_channel_binding_sha256(binding),
        host_principal_claim_json=claim_json,
        host_principal_claim_sha256=owner_control_host_principal_claim_sha256(claim),
        enrolled_at=enrolled_at,
        server_observed_corroboration=corroboration,
        provenance_tier=derive_owner_control_provenance_tier(
            claim=claim,
            server_observed_corroboration=corroboration,
        ),
    )
