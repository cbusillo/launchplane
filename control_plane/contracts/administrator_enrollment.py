from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.authz_policy_record import build_authz_policy_record_id


ADMINISTRATOR_ENROLLMENT_SCHEMA_VERSION: Final[Literal[1]] = 1
ADMINISTRATOR_ENROLLMENT_CHALLENGE_SECONDS: Final = 30 * 60
ADMINISTRATOR_ENROLLMENT_AUTHORITY_STATE: Final[Literal["inert"]] = "inert"
ADMINISTRATOR_ENROLLMENT_POLICY_BRIDGE_NOT_APPLIED: Final[Literal["not_applied"]] = "not_applied"
ADMINISTRATOR_ENROLLMENT_POLICY_BRIDGE_APPLIED: Final[Literal["applied"]] = "applied"

AdministratorEnrollmentState = Literal[
    "issued",
    "control_proven",
    "withdrawn",
    "expired",
    "enrolled",
]
AdministratorEnrollmentPolicyBridgeState = Literal["not_applied", "applied"]
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_ENROLLMENT_ID_PATTERN = re.compile(r"^administrator-enrollment-[a-z0-9][a-z0-9_-]{7,127}$")


class AdministratorEnrollmentConflictError(ValueError):
    """Raised when an inert enrollment lifecycle transition is not allowed."""


def _timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError(f"{field_name} must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    normalized = parsed.astimezone(UTC)
    if normalized.microsecond:
        raise ValueError(f"{field_name} must use whole-second precision")
    return normalized


def _sha256(value: str, field_name: str) -> str:
    if _SHA256_PATTERN.fullmatch(value) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return value


def administrator_enrollment_challenge_sha256(challenge: str) -> str:
    """Digest a transient opaque challenge without retaining its plaintext."""

    if not challenge:
        raise ValueError("administrator enrollment challenge must not be empty")
    return hashlib.sha256(challenge.encode("utf-8")).hexdigest()


class AdministratorEnrollmentRecord(BaseModel):
    """Inert evidence for a separately gated policy-administrator enrollment."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = ADMINISTRATOR_ENROLLMENT_SCHEMA_VERSION
    enrollment_id: str
    state: AdministratorEnrollmentState = "issued"
    proposer_github_id: int = Field(gt=0)
    candidate_github_id: int | None = Field(default=None, gt=0)
    challenge_sha256: str
    reason: str = Field(min_length=1, max_length=500)
    provenance_sha256: str
    created_at: str
    expires_at: str
    control_proven_at: str | None = None
    withdrawn_at: str | None = None
    expired_at: str | None = None
    enrolled_at: str | None = None
    enrolled_policy_record_id: str | None = Field(default=None, min_length=1, max_length=255)
    enrolled_policy_revision: int | None = Field(default=None, gt=0)
    enrolled_policy_sha256: str | None = None
    reviewed_plan_sha256: str | None = None
    bridge_idempotency_key_sha256: str | None = None
    authority_state: Literal["inert"] = ADMINISTRATOR_ENROLLMENT_AUTHORITY_STATE
    authorizes_policy: Literal[False] = False
    policy_bridge_state: AdministratorEnrollmentPolicyBridgeState = (
        ADMINISTRATOR_ENROLLMENT_POLICY_BRIDGE_NOT_APPLIED
    )

    @field_validator("reason")
    @classmethod
    def _normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("administrator enrollment reason must not be empty")
        return normalized

    @field_validator("enrolled_policy_record_id")
    @classmethod
    def _normalize_policy_record_id(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("enrolled_policy_record_id must not be empty")
        return normalized

    @field_validator(
        "created_at",
        "expires_at",
        "control_proven_at",
        "withdrawn_at",
        "expired_at",
        "enrolled_at",
    )
    @classmethod
    def _normalize_timestamp(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        field_name = str(getattr(info, "field_name", "timestamp"))
        return _timestamp(value, field_name).isoformat()

    @field_validator(
        "challenge_sha256",
        "provenance_sha256",
        "enrolled_policy_sha256",
        "reviewed_plan_sha256",
        "bridge_idempotency_key_sha256",
    )
    @classmethod
    def _validate_sha256(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        field_name = getattr(info, "field_name", "sha256")
        return _sha256(value, str(field_name))

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> "AdministratorEnrollmentRecord":
        if _ENROLLMENT_ID_PATTERN.fullmatch(self.enrollment_id) is None:
            raise ValueError("enrollment_id must use the administrator-enrollment identifier form")
        created_at = _timestamp(self.created_at, "created_at")
        expires_at = _timestamp(self.expires_at, "expires_at")
        if expires_at - created_at != timedelta(seconds=ADMINISTRATOR_ENROLLMENT_CHALLENGE_SECONDS):
            raise ValueError(
                "administrator enrollment challenge must expire exactly 30 minutes after creation"
            )
        control_proven_at = (
            _timestamp(self.control_proven_at, "control_proven_at")
            if self.control_proven_at is not None
            else None
        )
        withdrawn_at = (
            _timestamp(self.withdrawn_at, "withdrawn_at") if self.withdrawn_at is not None else None
        )
        expired_at = (
            _timestamp(self.expired_at, "expired_at") if self.expired_at is not None else None
        )
        enrolled_at = (
            _timestamp(self.enrolled_at, "enrolled_at") if self.enrolled_at is not None else None
        )
        if (self.candidate_github_id is None) != (control_proven_at is None):
            raise ValueError(
                "candidate_github_id and control_proven_at must be present or absent together"
            )
        if self.candidate_github_id == self.proposer_github_id:
            raise ValueError("candidate_github_id must differ from proposer_github_id")
        if control_proven_at is not None and not (created_at <= control_proven_at < expires_at):
            raise ValueError("control_proven_at must be within the enrollment challenge window")
        policy_evidence = (
            self.enrolled_policy_record_id,
            self.enrolled_policy_revision,
            self.enrolled_policy_sha256,
            self.reviewed_plan_sha256,
            self.bridge_idempotency_key_sha256,
        )
        has_policy_evidence = all(value is not None for value in policy_evidence)
        has_partial_policy_evidence = any(value is not None for value in policy_evidence)
        if has_partial_policy_evidence and not has_policy_evidence:
            raise ValueError("administrator enrollment policy evidence must be complete")
        if has_policy_evidence:
            assert self.enrolled_policy_revision is not None
            assert self.enrolled_policy_sha256 is not None
            if self.enrolled_policy_record_id != build_authz_policy_record_id(
                revision=self.enrolled_policy_revision,
                policy_sha256=self.enrolled_policy_sha256,
            ):
                raise ValueError(
                    "enrolled policy record ID must match its revision and policy digest"
                )
        if self.state == "issued":
            if any(
                value is not None
                for value in (
                    self.candidate_github_id,
                    self.control_proven_at,
                    self.withdrawn_at,
                    self.expired_at,
                    self.enrolled_at,
                    *policy_evidence,
                )
            ):
                raise ValueError("issued enrollment must contain only issuance evidence")
        elif self.state == "control_proven":
            if control_proven_at is None or any(
                value is not None
                for value in (
                    self.withdrawn_at,
                    self.expired_at,
                    self.enrolled_at,
                    *policy_evidence,
                )
            ):
                raise ValueError(
                    "control-proven enrollment requires only candidate control evidence"
                )
        elif self.state == "withdrawn":
            if withdrawn_at is None or any(
                value is not None for value in (self.expired_at, self.enrolled_at, *policy_evidence)
            ):
                raise ValueError("withdrawn enrollment requires only withdrawal evidence")
            minimum_withdrawal_time = control_proven_at or created_at
            if not (minimum_withdrawal_time <= withdrawn_at < expires_at):
                raise ValueError("withdrawn_at must be within the active enrollment window")
        elif self.state == "expired":
            if expired_at is None or any(
                value is not None
                for value in (self.withdrawn_at, self.enrolled_at, *policy_evidence)
            ):
                raise ValueError("expired enrollment requires only expiry evidence")
            if expired_at < expires_at:
                raise ValueError("expired_at must not precede expires_at")
        else:
            if control_proven_at is None or enrolled_at is None or not has_policy_evidence:
                raise ValueError(
                    "enrolled enrollment requires control proof and complete policy read-back evidence"
                )
            if self.withdrawn_at is not None or self.expired_at is not None:
                raise ValueError(
                    "enrolled enrollment must not contain withdrawal or expiry evidence"
                )
            if not (control_proven_at <= enrolled_at < expires_at):
                raise ValueError("enrolled_at must be within the proven-control window")
        expected_bridge_state = (
            ADMINISTRATOR_ENROLLMENT_POLICY_BRIDGE_APPLIED
            if self.state == "enrolled"
            else ADMINISTRATOR_ENROLLMENT_POLICY_BRIDGE_NOT_APPLIED
        )
        if self.policy_bridge_state != expected_bridge_state:
            raise ValueError("policy_bridge_state must match administrator enrollment lifecycle")
        return self


def _validated_update(
    record: AdministratorEnrollmentRecord, **updates: object
) -> AdministratorEnrollmentRecord:
    return AdministratorEnrollmentRecord.model_validate(record.model_dump() | updates)


def prove_administrator_enrollment_control(
    record: AdministratorEnrollmentRecord,
    *,
    challenge: str,
    server_derived_candidate_github_id: int,
    control_proven_at: str,
) -> AdministratorEnrollmentRecord:
    challenge_matches = hmac.compare_digest(
        record.challenge_sha256, administrator_enrollment_challenge_sha256(challenge)
    )
    if not challenge_matches:
        raise AdministratorEnrollmentConflictError(
            "administrator enrollment challenge does not match"
        )
    timestamp = _timestamp(control_proven_at, "control_proven_at")
    created_at = _timestamp(record.created_at, "created_at")
    expires_at = _timestamp(record.expires_at, "expires_at")
    if not (created_at <= timestamp < expires_at):
        raise AdministratorEnrollmentConflictError(
            "administrator enrollment challenge is outside its active window"
        )
    if record.state == "control_proven":
        if (
            record.candidate_github_id == server_derived_candidate_github_id
            and record.control_proven_at == timestamp.isoformat()
        ):
            return record
        raise AdministratorEnrollmentConflictError(
            "administrator enrollment control proof conflicts with persisted evidence"
        )
    if record.state != "issued":
        raise AdministratorEnrollmentConflictError(
            "administrator enrollment is no longer claimable"
        )
    if server_derived_candidate_github_id == record.proposer_github_id:
        raise AdministratorEnrollmentConflictError(
            "administrator enrollment candidate must differ from proposer"
        )
    return _validated_update(
        record,
        state="control_proven",
        candidate_github_id=server_derived_candidate_github_id,
        control_proven_at=timestamp.isoformat(),
    )


def expire_administrator_enrollment(
    record: AdministratorEnrollmentRecord, *, expired_at: str
) -> AdministratorEnrollmentRecord:
    timestamp = _timestamp(expired_at, "expired_at")
    if record.state == "expired":
        if record.expired_at == timestamp.isoformat():
            return record
        raise AdministratorEnrollmentConflictError(
            "administrator enrollment expiry conflicts with persisted evidence"
        )
    if record.state not in {"issued", "control_proven"} or timestamp < _timestamp(
        record.expires_at, "expires_at"
    ):
        raise AdministratorEnrollmentConflictError(
            "administrator enrollment is not eligible for expiry"
        )
    return _validated_update(record, state="expired", expired_at=timestamp.isoformat())


def withdraw_administrator_enrollment(
    record: AdministratorEnrollmentRecord, *, proposer_github_id: int, withdrawn_at: str
) -> AdministratorEnrollmentRecord:
    timestamp = _timestamp(withdrawn_at, "withdrawn_at")
    if proposer_github_id != record.proposer_github_id:
        raise AdministratorEnrollmentConflictError(
            "only the immutable proposer may withdraw enrollment"
        )
    if record.state == "withdrawn":
        if record.withdrawn_at == timestamp.isoformat():
            return record
        raise AdministratorEnrollmentConflictError(
            "administrator enrollment withdrawal conflicts with persisted evidence"
        )
    if record.state not in {"issued", "control_proven"}:
        raise AdministratorEnrollmentConflictError("administrator enrollment is terminal")
    minimum_withdrawal_time = (
        _timestamp(record.control_proven_at, "control_proven_at")
        if record.control_proven_at is not None
        else _timestamp(record.created_at, "created_at")
    )
    if not (minimum_withdrawal_time <= timestamp < _timestamp(record.expires_at, "expires_at")):
        raise AdministratorEnrollmentConflictError(
            "administrator enrollment withdrawal is outside its active window"
        )
    return _validated_update(record, state="withdrawn", withdrawn_at=timestamp.isoformat())


def complete_administrator_enrollment(
    record: AdministratorEnrollmentRecord,
    *,
    server_derived_candidate_github_id: int,
    enrolled_at: str,
    enrolled_policy_record_id: str,
    enrolled_policy_revision: int,
    enrolled_policy_sha256: str,
    reviewed_plan_sha256: str,
    bridge_idempotency_key_sha256: str,
) -> AdministratorEnrollmentRecord:
    timestamp = _timestamp(enrolled_at, "enrolled_at")
    if record.state == "enrolled":
        requested_evidence = (
            server_derived_candidate_github_id,
            timestamp.isoformat(),
            enrolled_policy_record_id.strip(),
            enrolled_policy_revision,
            _sha256(enrolled_policy_sha256, "enrolled_policy_sha256"),
            _sha256(reviewed_plan_sha256, "reviewed_plan_sha256"),
            _sha256(bridge_idempotency_key_sha256, "bridge_idempotency_key_sha256"),
        )
        persisted_evidence = (
            record.candidate_github_id,
            record.enrolled_at,
            record.enrolled_policy_record_id,
            record.enrolled_policy_revision,
            record.enrolled_policy_sha256,
            record.reviewed_plan_sha256,
            record.bridge_idempotency_key_sha256,
        )
        if requested_evidence == persisted_evidence:
            return record
        raise AdministratorEnrollmentConflictError(
            "administrator enrollment completion conflicts with persisted evidence"
        )
    if (
        record.state != "control_proven"
        or record.candidate_github_id != server_derived_candidate_github_id
        or record.control_proven_at is None
    ):
        raise AdministratorEnrollmentConflictError(
            "administrator enrollment completion requires the control-proven candidate"
        )
    if not (
        _timestamp(record.control_proven_at, "control_proven_at")
        <= timestamp
        < _timestamp(record.expires_at, "expires_at")
    ):
        raise AdministratorEnrollmentConflictError(
            "administrator enrollment completion is outside its proven-control window"
        )
    return _validated_update(
        record,
        state="enrolled",
        enrolled_at=timestamp.isoformat(),
        enrolled_policy_record_id=enrolled_policy_record_id.strip(),
        enrolled_policy_revision=enrolled_policy_revision,
        enrolled_policy_sha256=enrolled_policy_sha256,
        reviewed_plan_sha256=reviewed_plan_sha256,
        bridge_idempotency_key_sha256=bridge_idempotency_key_sha256,
        policy_bridge_state=ADMINISTRATOR_ENROLLMENT_POLICY_BRIDGE_APPLIED,
    )
