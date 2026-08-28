from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hmac
import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.canonical_json import canonical_json_bytes, canonical_json_sha256
from control_plane.contracts.owner_control import (
    ApprovalRequest,
    ChannelBindingRecord,
    OwnerControlConfirmationEnvelope,
    owner_control_approval_request_digest,
    owner_control_channel_binding_sha256,
    verify_owner_control_confirmation_signature,
)
from control_plane.contracts.privileged_operation import PrivilegedOperationDescriptorId


OWNER_CONTROL_SHADOW_VERIFIER_SCHEMA_VERSION: Final[Literal[1]] = 1
OWNER_CONTROL_SHADOW_VERIFIER_MODE: Final[Literal["shadow"]] = "shadow"
OWNER_CONTROL_SHADOW_AUTHORITY_STATE: Final[Literal["inert"]] = "inert"
OWNER_CONTROL_SHADOW_MAX_ATTEMPTS: Final = 8
_SERVER_IDENTIFIER_PATTERN = re.compile(r"^[a-z][a-z0-9_-]{0,127}$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CANONICAL_TIMESTAMP_PATTERN = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\+00:00$")

OwnerControlChannelSessionStatus = Literal["enrolled", "revoked"]
OwnerControlChallengeState = Literal["issued", "consumed", "expired", "rejected"]
OwnerControlShadowVerificationStatus = Literal["verified", "rejected"]
OwnerControlShadowVerificationReason = Literal[
    "unknown_channel_session",
    "unknown_challenge",
    "channel_session_revoked",
    "channel_session_expired",
    "challenge_channel_session_mismatch",
    "challenge_expired",
    "challenge_replayed",
    "stored_binding_mismatch",
    "stored_approval_request_mismatch",
    "signature_invalid",
    "attempt_budget_exhausted",
]


class OwnerControlShadowVerifierConflictError(ValueError):
    """Raised when immutable owner-control verifier state conflicts."""


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


def _canonical_wire_json(model: BaseModel) -> str:
    return canonical_json_bytes(model.model_dump(mode="json")).decode()


def _validate_canonical_wire_json(
    value: str,
    *,
    model_type: type[ApprovalRequest] | type[ChannelBindingRecord],
    field_name: str,
) -> ApprovalRequest | ChannelBindingRecord:
    try:
        model = model_type.model_validate_json(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must contain a valid owner-control payload") from error
    if _canonical_wire_json(model) != value:
        raise ValueError(f"{field_name} must contain exact canonical owner-control bytes")
    return model


def owner_control_challenge_id(approval_request_sha256: str) -> str:
    """Return the deterministic identifier for one exact issued approval request."""

    if _SHA256_PATTERN.fullmatch(approval_request_sha256) is None:
        raise ValueError("approval_request_sha256 must be a lowercase SHA-256 digest")
    return f"owner-control-challenge-{approval_request_sha256[:32]}"


def owner_control_verification_event_id(
    *,
    challenge_id: str,
    sequence: int,
    envelope_sha256: str,
    verification_status: OwnerControlShadowVerificationStatus,
    rejection_reason: OwnerControlShadowVerificationReason | None,
) -> str:
    """Return a deterministic append-only verification event identifier."""

    digest = canonical_json_sha256(
        {
            "challenge_id": challenge_id,
            "envelope_sha256": envelope_sha256,
            "rejection_reason": rejection_reason,
            "sequence": sequence,
            "verification_status": verification_status,
        }
    )
    return f"owner-control-shadow-event-{digest[:32]}"


class OwnerControlChannelSessionRecord(BaseModel):
    """Server-enrolled owner channel session, independent of the wire envelope."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = OWNER_CONTROL_SHADOW_VERIFIER_SCHEMA_VERSION
    channel_session_id: str = Field(pattern=_SERVER_IDENTIFIER_PATTERN.pattern)
    owner_github_id: int = Field(ge=1, le=9_223_372_036_854_775_807)
    status: OwnerControlChannelSessionStatus
    binding_json: str = Field(min_length=2, max_length=20_000)
    binding_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    enrolled_at: str
    revoked_at: str | None = None
    authority_state: Literal["inert"] = OWNER_CONTROL_SHADOW_AUTHORITY_STATE

    @model_validator(mode="after")
    def _validate_record(self) -> "OwnerControlChannelSessionRecord":
        binding = _validate_canonical_wire_json(
            self.binding_json,
            model_type=ChannelBindingRecord,
            field_name="binding_json",
        )
        assert isinstance(binding, ChannelBindingRecord)
        if self.channel_session_id != binding.channel_session_id:
            raise ValueError("channel_session_id must match the stored channel binding")
        if self.owner_github_id != binding.owner_github_id:
            raise ValueError("owner_github_id must match the stored channel binding")
        if self.binding_sha256 != owner_control_channel_binding_sha256(binding):
            raise ValueError("binding_sha256 must match the stored channel binding")
        enrolled_at = _canonical_timestamp(self.enrolled_at, "enrolled_at")
        revoked_at = (
            _canonical_timestamp(self.revoked_at, "revoked_at")
            if self.revoked_at is not None
            else None
        )
        if self.status == "enrolled" and revoked_at is not None:
            raise ValueError("enrolled channel sessions must not have revoked_at")
        if self.status == "revoked" and revoked_at is None:
            raise ValueError("revoked channel sessions must include revoked_at")
        if revoked_at is not None and revoked_at < enrolled_at:
            raise ValueError("revoked_at must not precede enrolled_at")
        binding_issued_at = _canonical_timestamp(binding.session_issued_at, "session_issued_at")
        binding_expires_at = _canonical_timestamp(binding.session_expires_at, "session_expires_at")
        if enrolled_at < binding_issued_at or enrolled_at > binding_expires_at:
            raise ValueError("enrolled_at must be inside the channel session interval")
        return self

    def channel_binding(self) -> ChannelBindingRecord:
        binding = ChannelBindingRecord.model_validate_json(self.binding_json)
        return binding


class OwnerControlChallengeIssueRequest(BaseModel):
    """Server-only challenge template whose timestamp bounds are set by the store clock."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    channel_session_id: str = Field(pattern=_SERVER_IDENTIFIER_PATTERN.pattern)
    approval_request: ApprovalRequest
    expires_in_seconds: int = Field(ge=1, le=3_600)


class OwnerControlIssuedChallengeRecord(BaseModel):
    """Server-issued, single-use owner-control challenge with exact stored bindings."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = OWNER_CONTROL_SHADOW_VERIFIER_SCHEMA_VERSION
    challenge_id: str = Field(pattern=_SERVER_IDENTIFIER_PATTERN.pattern)
    challenge_nonce: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    channel_session_id: str = Field(pattern=_SERVER_IDENTIFIER_PATTERN.pattern)
    operation_id: str
    descriptor_id: PrivilegedOperationDescriptorId
    owner_github_id: int = Field(ge=1, le=9_223_372_036_854_775_807)
    issued_at: str
    expires_at: str
    approval_request_json: str = Field(min_length=2, max_length=20_000)
    approval_request_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    binding_json: str = Field(min_length=2, max_length=20_000)
    binding_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    state: OwnerControlChallengeState = "issued"
    attempt_count: int = Field(default=0, ge=0, le=OWNER_CONTROL_SHADOW_MAX_ATTEMPTS)
    consumed_at: str | None = None
    terminal_event_id: str | None = Field(default=None, pattern=_SERVER_IDENTIFIER_PATTERN.pattern)
    authority_state: Literal["inert"] = OWNER_CONTROL_SHADOW_AUTHORITY_STATE

    @model_validator(mode="after")
    def _validate_record(self) -> "OwnerControlIssuedChallengeRecord":
        request = _validate_canonical_wire_json(
            self.approval_request_json,
            model_type=ApprovalRequest,
            field_name="approval_request_json",
        )
        binding = _validate_canonical_wire_json(
            self.binding_json,
            model_type=ChannelBindingRecord,
            field_name="binding_json",
        )
        assert isinstance(request, ApprovalRequest)
        assert isinstance(binding, ChannelBindingRecord)
        if self.challenge_nonce != request.nonce:
            raise ValueError("challenge_nonce must match the stored approval request nonce")
        expected_challenge_id = owner_control_challenge_id(
            owner_control_approval_request_digest(request)
        )
        if self.challenge_id != expected_challenge_id:
            raise ValueError("challenge_id must derive from the stored approval request")
        if self.channel_session_id != binding.channel_session_id:
            raise ValueError("channel_session_id must match the stored channel binding")
        if self.operation_id != request.operation_id:
            raise ValueError("operation_id must match the stored approval request")
        if self.descriptor_id != request.descriptor_id:
            raise ValueError("descriptor_id must match the stored approval request")
        if (
            self.owner_github_id != request.owner_github_id
            or self.owner_github_id != binding.owner_github_id
        ):
            raise ValueError("owner_github_id must match the stored request and channel binding")
        if self.approval_request_sha256 != owner_control_approval_request_digest(request):
            raise ValueError("approval_request_sha256 must match the stored approval request")
        if self.binding_sha256 != owner_control_channel_binding_sha256(binding):
            raise ValueError("binding_sha256 must match the stored channel binding")
        issued_at = _canonical_timestamp(self.issued_at, "issued_at")
        expires_at = _canonical_timestamp(self.expires_at, "expires_at")
        if request.issued_at != self.issued_at or request.expires_at != self.expires_at:
            raise ValueError(
                "stored approval request timestamps must match the challenge timestamps"
            )
        if expires_at <= issued_at:
            raise ValueError("expires_at must be later than issued_at")
        consumed_at = (
            _canonical_timestamp(self.consumed_at, "consumed_at")
            if self.consumed_at is not None
            else None
        )
        if self.state == "issued":
            if consumed_at is not None or self.terminal_event_id is not None:
                raise ValueError("issued challenges must not have terminal fields")
        elif self.terminal_event_id is None:
            raise ValueError("terminal challenges must include terminal_event_id")
        if self.state == "consumed" and consumed_at is None:
            raise ValueError("consumed challenges must include consumed_at")
        if self.state != "consumed" and consumed_at is not None:
            raise ValueError("only consumed challenges may include consumed_at")
        if consumed_at is not None and consumed_at < issued_at:
            raise ValueError("consumed_at must not precede issued_at")
        return self

    def approval_request(self) -> ApprovalRequest:
        request = ApprovalRequest.model_validate_json(self.approval_request_json)
        return request

    def channel_binding(self) -> ChannelBindingRecord:
        binding = ChannelBindingRecord.model_validate_json(self.binding_json)
        return binding


class OwnerControlShadowVerificationEventRecord(BaseModel):
    """Append-only result of one structurally valid owner-control envelope evaluation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    schema_version: Literal[1] = OWNER_CONTROL_SHADOW_VERIFIER_SCHEMA_VERSION
    event_id: str = Field(pattern=_SERVER_IDENTIFIER_PATTERN.pattern)
    challenge_id: str = Field(pattern=_SERVER_IDENTIFIER_PATTERN.pattern)
    sequence: int = Field(ge=1, le=OWNER_CONTROL_SHADOW_MAX_ATTEMPTS)
    channel_session_id: str = Field(pattern=_SERVER_IDENTIFIER_PATTERN.pattern)
    challenge_nonce: str = Field(min_length=16, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    envelope_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    approval_request_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    binding_sha256: str = Field(pattern=_SHA256_PATTERN.pattern)
    verification_status: OwnerControlShadowVerificationStatus
    rejection_reason: OwnerControlShadowVerificationReason | None = None
    resulting_challenge_state: OwnerControlChallengeState
    occurred_at: str
    verifier_mode: Literal["shadow"] = OWNER_CONTROL_SHADOW_VERIFIER_MODE
    authorizes_execution: Literal[False] = False
    authority_state: Literal["inert"] = OWNER_CONTROL_SHADOW_AUTHORITY_STATE

    @model_validator(mode="after")
    def _validate_record(self) -> "OwnerControlShadowVerificationEventRecord":
        _canonical_timestamp(self.occurred_at, "occurred_at")
        if self.verification_status == "verified" and (
            self.rejection_reason is not None or self.resulting_challenge_state != "consumed"
        ):
            raise ValueError("verified events must consume without a rejection_reason")
        if self.verification_status == "rejected" and self.rejection_reason is None:
            raise ValueError("rejected events must include a rejection_reason")
        return self


class OwnerControlShadowVerificationResult(BaseModel):
    """Non-authorizing result returned only after its append-only audit event is stored."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    event_id: str = Field(pattern=_SERVER_IDENTIFIER_PATTERN.pattern)
    challenge_id: str = Field(pattern=_SERVER_IDENTIFIER_PATTERN.pattern)
    sequence: int = Field(ge=1, le=OWNER_CONTROL_SHADOW_MAX_ATTEMPTS)
    verification_status: OwnerControlShadowVerificationStatus
    rejection_reason: OwnerControlShadowVerificationReason | None = None
    resulting_challenge_state: OwnerControlChallengeState
    verifier_mode: Literal["shadow"] = OWNER_CONTROL_SHADOW_VERIFIER_MODE
    authorizes_execution: Literal[False] = False
    authority_state: Literal["inert"] = OWNER_CONTROL_SHADOW_AUTHORITY_STATE

    @model_validator(mode="after")
    def _validate_result(self) -> "OwnerControlShadowVerificationResult":
        if self.verification_status == "verified" and (
            self.rejection_reason is not None or self.resulting_challenge_state != "consumed"
        ):
            raise ValueError("verified results must consume without a rejection_reason")
        if self.verification_status == "rejected" and self.rejection_reason is None:
            raise ValueError("rejected results must include a rejection_reason")
        return self


class OwnerControlShadowVerificationEvaluation(BaseModel):
    """Pure exact-state evaluation used inside the transactional storage operation."""

    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)

    verification_status: OwnerControlShadowVerificationStatus
    rejection_reason: OwnerControlShadowVerificationReason | None = None
    consume_challenge: bool = False
    resulting_challenge_state: OwnerControlChallengeState

    @model_validator(mode="after")
    def _validate_evaluation(self) -> "OwnerControlShadowVerificationEvaluation":
        if self.verification_status == "verified":
            if (
                self.rejection_reason is not None
                or not self.consume_challenge
                or self.resulting_challenge_state != "consumed"
            ):
                raise ValueError(
                    "verified evaluations must consume one challenge without a rejection"
                )
        elif self.rejection_reason is None or self.consume_challenge:
            raise ValueError(
                "rejected evaluations must include a reason and not consume a challenge"
            )
        return self


def build_owner_control_channel_session_record(
    *,
    binding: ChannelBindingRecord,
    enrolled_at: str,
) -> OwnerControlChannelSessionRecord:
    """Build an active server-enrolled record from an exact wire binding."""

    binding_json = _canonical_wire_json(binding)
    return OwnerControlChannelSessionRecord(
        channel_session_id=binding.channel_session_id,
        owner_github_id=binding.owner_github_id,
        status="enrolled",
        binding_json=binding_json,
        binding_sha256=owner_control_channel_binding_sha256(binding),
        enrolled_at=enrolled_at,
    )


def revoke_owner_control_channel_session_record(
    record: OwnerControlChannelSessionRecord,
    *,
    revoked_at: str,
) -> OwnerControlChannelSessionRecord:
    """Return the one-way revoked successor for a stored channel session."""

    if record.status == "revoked":
        return record
    return record.model_copy(update={"status": "revoked", "revoked_at": revoked_at})


def issue_owner_control_challenge_record(
    *,
    issue_request: OwnerControlChallengeIssueRequest,
    session: OwnerControlChannelSessionRecord,
    challenge_nonce: str,
    issued_at: str,
) -> OwnerControlIssuedChallengeRecord:
    """Materialize a challenge with DB-clock issuance and expiry timestamps."""

    if session.status != "enrolled":
        raise OwnerControlShadowVerifierConflictError("Channel session is not enrolled.")
    if issue_request.channel_session_id != session.channel_session_id:
        raise OwnerControlShadowVerifierConflictError(
            "Challenge issue session does not match enrollment."
        )
    if issue_request.approval_request.owner_github_id != session.owner_github_id:
        raise OwnerControlShadowVerifierConflictError("Challenge owner does not match enrollment.")
    issued_at_value = _canonical_timestamp(issued_at, "issued_at")
    expires_at = (issued_at_value + timedelta(seconds=issue_request.expires_in_seconds)).isoformat()
    binding = session.channel_binding()
    session_expires_at = _canonical_timestamp(binding.session_expires_at, "session_expires_at")
    if issued_at_value < _canonical_timestamp(binding.session_issued_at, "session_issued_at"):
        raise OwnerControlShadowVerifierConflictError("Channel session has not started.")
    if datetime.fromisoformat(expires_at) > session_expires_at:
        raise OwnerControlShadowVerifierConflictError(
            "Challenge expiry exceeds the channel session."
        )
    request = issue_request.approval_request.model_copy(
        update={
            "nonce": challenge_nonce,
            "issued_at": issued_at,
            "expires_at": expires_at,
        }
    )
    request_json = _canonical_wire_json(request)
    approval_request_sha256 = owner_control_approval_request_digest(request)
    return OwnerControlIssuedChallengeRecord(
        challenge_id=owner_control_challenge_id(approval_request_sha256),
        challenge_nonce=request.nonce,
        channel_session_id=session.channel_session_id,
        operation_id=request.operation_id,
        descriptor_id=request.descriptor_id,
        owner_github_id=session.owner_github_id,
        issued_at=issued_at,
        expires_at=expires_at,
        approval_request_json=request_json,
        approval_request_sha256=approval_request_sha256,
        binding_json=session.binding_json,
        binding_sha256=session.binding_sha256,
    )


def evaluate_owner_control_shadow_verification(
    *,
    envelope: OwnerControlConfirmationEnvelope,
    channel_session: OwnerControlChannelSessionRecord | None,
    issued_challenge: OwnerControlIssuedChallengeRecord | None,
    observed_at: str,
) -> OwnerControlShadowVerificationEvaluation:
    """Evaluate one envelope against exact stored server state without authorizing execution."""

    observed_at_value = _canonical_timestamp(observed_at, "observed_at")
    if channel_session is None:
        return OwnerControlShadowVerificationEvaluation(
            verification_status="rejected",
            rejection_reason="unknown_channel_session",
            resulting_challenge_state="rejected",
        )
    if issued_challenge is None:
        return OwnerControlShadowVerificationEvaluation(
            verification_status="rejected",
            rejection_reason="unknown_challenge",
            resulting_challenge_state="rejected",
        )
    if (
        issued_challenge.channel_session_id != channel_session.channel_session_id
        or envelope.channel_binding.channel_session_id != issued_challenge.channel_session_id
    ):
        return OwnerControlShadowVerificationEvaluation(
            verification_status="rejected",
            rejection_reason="challenge_channel_session_mismatch",
            resulting_challenge_state="rejected",
        )
    if channel_session.status != "enrolled":
        return OwnerControlShadowVerificationEvaluation(
            verification_status="rejected",
            rejection_reason="channel_session_revoked",
            resulting_challenge_state="rejected",
        )
    session_binding = channel_session.channel_binding()
    if not (
        _canonical_timestamp(session_binding.session_issued_at, "session_issued_at")
        <= observed_at_value
        <= _canonical_timestamp(session_binding.session_expires_at, "session_expires_at")
    ):
        return OwnerControlShadowVerificationEvaluation(
            verification_status="rejected",
            rejection_reason="channel_session_expired",
            resulting_challenge_state="expired",
        )
    if issued_challenge.state == "consumed":
        return OwnerControlShadowVerificationEvaluation(
            verification_status="rejected",
            rejection_reason="challenge_replayed",
            resulting_challenge_state="consumed",
        )
    if issued_challenge.state == "expired":
        return OwnerControlShadowVerificationEvaluation(
            verification_status="rejected",
            rejection_reason="challenge_expired",
            resulting_challenge_state="expired",
        )
    if issued_challenge.state == "rejected":
        return OwnerControlShadowVerificationEvaluation(
            verification_status="rejected",
            rejection_reason="attempt_budget_exhausted",
            resulting_challenge_state="rejected",
        )
    if not (
        _canonical_timestamp(issued_challenge.issued_at, "issued_at")
        <= observed_at_value
        <= _canonical_timestamp(issued_challenge.expires_at, "expires_at")
    ):
        return OwnerControlShadowVerificationEvaluation(
            verification_status="rejected",
            rejection_reason="challenge_expired",
            resulting_challenge_state="expired",
        )
    envelope_binding_json = _canonical_wire_json(envelope.channel_binding)
    if (
        envelope_binding_json != channel_session.binding_json
        or envelope_binding_json != issued_challenge.binding_json
        or not hmac.compare_digest(
            owner_control_channel_binding_sha256(envelope.channel_binding),
            channel_session.binding_sha256,
        )
        or not hmac.compare_digest(
            envelope.challenge_response.channel_binding_sha256,
            issued_challenge.binding_sha256,
        )
    ):
        return OwnerControlShadowVerificationEvaluation(
            verification_status="rejected",
            rejection_reason="stored_binding_mismatch",
            resulting_challenge_state="issued",
        )
    envelope_request_json = _canonical_wire_json(envelope.challenge_response.approval_request)
    if (
        envelope_request_json != issued_challenge.approval_request_json
        or not hmac.compare_digest(
            owner_control_approval_request_digest(envelope.challenge_response.approval_request),
            issued_challenge.approval_request_sha256,
        )
        or not hmac.compare_digest(
            envelope.challenge_response.approval_request_digest,
            issued_challenge.approval_request_sha256,
        )
    ):
        return OwnerControlShadowVerificationEvaluation(
            verification_status="rejected",
            rejection_reason="stored_approval_request_mismatch",
            resulting_challenge_state="issued",
        )
    if not verify_owner_control_confirmation_signature(envelope):
        return OwnerControlShadowVerificationEvaluation(
            verification_status="rejected",
            rejection_reason="signature_invalid",
            resulting_challenge_state="issued",
        )
    return OwnerControlShadowVerificationEvaluation(
        verification_status="verified",
        consume_challenge=True,
        resulting_challenge_state="consumed",
    )


def owner_control_confirmation_envelope_sha256(envelope: OwnerControlConfirmationEnvelope) -> str:
    """Return the bounded canonical digest retained by shadow-verifier audit records."""

    return canonical_json_sha256(envelope.model_dump(mode="json"))
