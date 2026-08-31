from __future__ import annotations

from datetime import UTC, datetime, timedelta
import hashlib
import hmac
import json
import re
from typing import Final, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SOLO_ADMINISTRATION_CONFIRMATION_SCHEMA_VERSION: Final[Literal[1]] = 1
SOLO_ADMINISTRATION_CONFIRMATION_TTL_SECONDS: Final = 5 * 60
SOLO_ADMINISTRATION_CONFIRMATION_AUTHORITY_STATE: Final[Literal["inert"]] = "inert"

SoloAdministrationConfirmationState = Literal["issued", "consumed", "revoked", "expired"]

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")
_CONFIRMATION_ID_PATTERN = re.compile(
    r"^solo-administration-confirmation-[a-z0-9][a-z0-9_-]{7,127}$"
)


class SoloAdministrationConfirmationConflictError(ValueError):
    """Raised when a confirmation lifecycle transition cannot be applied."""


def _normalize_token(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must not be empty")
    return normalized


def _normalize_sha256(value: str, field_name: str) -> str:
    normalized = _normalize_token(value, field_name).lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _parse_timestamp(value: str, field_name: str) -> datetime:
    normalized = _normalize_token(value, field_name).replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as error:
        raise ValueError(f"{field_name} must be ISO-8601") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    normalized_datetime = parsed.astimezone(UTC)
    if normalized_datetime.microsecond:
        raise ValueError(f"{field_name} must use whole-second precision")
    return normalized_datetime


def _timestamp(value: str, field_name: str) -> str:
    return _parse_timestamp(value, field_name).isoformat()


def solo_administration_confirmation_acknowledgement_sha256(acknowledgement: str) -> str:
    """Digest acknowledgement text without returning or retaining plaintext."""

    normalized = _normalize_token(acknowledgement, "acknowledgement")
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def verify_solo_administration_acknowledgement(
    acknowledgement: str,
    acknowledgement_sha256: str,
) -> bool:
    """Verify acknowledgement text against its persisted digest in constant time."""

    try:
        expected_digest = _normalize_sha256(acknowledgement_sha256, "acknowledgement_sha256")
        supplied_digest = solo_administration_confirmation_acknowledgement_sha256(acknowledgement)
    except ValueError:
        return False
    return hmac.compare_digest(expected_digest, supplied_digest)


def solo_administration_acknowledgement_sha256(acknowledgement: str) -> str:
    return solo_administration_confirmation_acknowledgement_sha256(acknowledgement)


def verify_solo_administration_confirmation_acknowledgement(
    acknowledgement: str,
    acknowledgement_sha256: str,
) -> bool:
    return verify_solo_administration_acknowledgement(acknowledgement, acknowledgement_sha256)


def build_solo_administration_confirmation_id(
    *,
    reviewed_plan_sha256: str,
    human_session_id: str,
    idempotency_scope_sha256: str,
    idempotency_key_sha256: str,
) -> str:
    """Build the stable confirmation identity for one reviewed binding."""

    binding = {
        "human_session_id": _normalize_token(human_session_id, "human_session_id"),
        "idempotency_key_sha256": _normalize_sha256(
            idempotency_key_sha256, "idempotency_key_sha256"
        ),
        "idempotency_scope_sha256": _normalize_sha256(
            idempotency_scope_sha256, "idempotency_scope_sha256"
        ),
        "reviewed_plan_sha256": _normalize_sha256(reviewed_plan_sha256, "reviewed_plan_sha256"),
    }
    digest = hashlib.sha256(
        json.dumps(binding, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return f"solo-administration-confirmation-{digest}"


class SoloAdministrationConfirmationRecord(BaseModel):
    """Inert, single-use evidence for a reviewed solo-administration apply."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = SOLO_ADMINISTRATION_CONFIRMATION_SCHEMA_VERSION
    confirmation_id: str
    state: SoloAdministrationConfirmationState = "issued"
    active_policy_record_id: str
    active_policy_revision: int = Field(gt=0)
    active_policy_sha256: str
    candidate_policy_sha256: str
    candidate_administrator_quorum: Literal[1] = 1
    candidate_distinct_human_administrator_count: Literal[1] = 1
    reviewed_plan_sha256: str
    human_session_id: str
    github_id: int = Field(gt=0)
    idempotency_scope_sha256: str
    idempotency_key_sha256: str
    acknowledgement_sha256: str
    created_at: str
    expires_at: str
    terminal_at: str | None = None
    authority_state: Literal["inert"] = SOLO_ADMINISTRATION_CONFIRMATION_AUTHORITY_STATE
    authorizes_policy: Literal[False] = False

    @field_validator("confirmation_id")
    @classmethod
    def _validate_confirmation_id(cls, value: str) -> str:
        normalized = _normalize_token(value, "confirmation_id")
        if _CONFIRMATION_ID_PATTERN.fullmatch(normalized) is None:
            raise ValueError("confirmation_id has an invalid format")
        return normalized

    @field_validator("active_policy_record_id", "human_session_id")
    @classmethod
    def _validate_tokens(cls, value: str, info: object) -> str:
        return _normalize_token(value, str(getattr(info, "field_name", "value")))

    @field_validator(
        "active_policy_sha256",
        "candidate_policy_sha256",
        "reviewed_plan_sha256",
        "idempotency_scope_sha256",
        "idempotency_key_sha256",
        "acknowledgement_sha256",
    )
    @classmethod
    def _validate_digests(cls, value: str, info: object) -> str:
        return _normalize_sha256(value, str(getattr(info, "field_name", "digest")))

    @field_validator("created_at", "expires_at", "terminal_at")
    @classmethod
    def _validate_timestamps(cls, value: str | None, info: object) -> str | None:
        if value is None:
            return None
        return _timestamp(value, str(getattr(info, "field_name", "timestamp")))

    @model_validator(mode="after")
    def _validate_lifecycle(self) -> "SoloAdministrationConfirmationRecord":
        created_at = _parse_timestamp(self.created_at, "created_at")
        expires_at = _parse_timestamp(self.expires_at, "expires_at")
        if expires_at - created_at != timedelta(
            seconds=SOLO_ADMINISTRATION_CONFIRMATION_TTL_SECONDS
        ):
            raise ValueError("solo-administration confirmation TTL must be exactly five minutes")
        if self.confirmation_id != build_solo_administration_confirmation_id(
            reviewed_plan_sha256=self.reviewed_plan_sha256,
            human_session_id=self.human_session_id,
            idempotency_scope_sha256=self.idempotency_scope_sha256,
            idempotency_key_sha256=self.idempotency_key_sha256,
        ):
            raise ValueError("confirmation_id does not match its reviewed binding")
        if self.state == "issued":
            if self.terminal_at is not None:
                raise ValueError("issued confirmation must not have terminal evidence")
        elif self.terminal_at is None:
            raise ValueError("terminal confirmation requires terminal evidence")
        else:
            terminal_at = _parse_timestamp(self.terminal_at, "terminal_at")
            if terminal_at < created_at:
                raise ValueError("terminal_at must not precede created_at")
            if self.state == "expired":
                if terminal_at < expires_at:
                    raise ValueError("expired confirmation must terminate at or after expiry")
            elif terminal_at >= expires_at:
                raise ValueError("consumed or revoked confirmation must terminate before expiry")
        return self


def issue_solo_administration_confirmation(
    *,
    active_policy_record_id: str,
    active_policy_revision: int,
    active_policy_sha256: str,
    candidate_policy_sha256: str,
    reviewed_plan_sha256: str,
    human_session_id: str,
    github_id: int,
    idempotency_scope_sha256: str,
    idempotency_key_sha256: str,
    acknowledgement_sha256: str,
    created_at: str,
) -> SoloAdministrationConfirmationRecord:
    """Create an issued confirmation with its fixed five-minute lifetime."""

    created = _parse_timestamp(created_at, "created_at")
    return SoloAdministrationConfirmationRecord(
        confirmation_id=build_solo_administration_confirmation_id(
            reviewed_plan_sha256=reviewed_plan_sha256,
            human_session_id=human_session_id,
            idempotency_scope_sha256=idempotency_scope_sha256,
            idempotency_key_sha256=idempotency_key_sha256,
        ),
        active_policy_record_id=active_policy_record_id,
        active_policy_revision=active_policy_revision,
        active_policy_sha256=active_policy_sha256,
        candidate_policy_sha256=candidate_policy_sha256,
        reviewed_plan_sha256=reviewed_plan_sha256,
        human_session_id=human_session_id,
        github_id=github_id,
        idempotency_scope_sha256=idempotency_scope_sha256,
        idempotency_key_sha256=idempotency_key_sha256,
        acknowledgement_sha256=acknowledgement_sha256,
        created_at=created.isoformat(),
        expires_at=(
            created + timedelta(seconds=SOLO_ADMINISTRATION_CONFIRMATION_TTL_SECONDS)
        ).isoformat(),
    )


def _validated_update(
    record: SoloAdministrationConfirmationRecord, **updates: object
) -> SoloAdministrationConfirmationRecord:
    return SoloAdministrationConfirmationRecord.model_validate(record.model_dump() | updates)


def revoke_solo_administration_confirmation(
    record: SoloAdministrationConfirmationRecord, *, terminal_at: str
) -> SoloAdministrationConfirmationRecord:
    timestamp = _parse_timestamp(terminal_at, "terminal_at")
    if record.state == "revoked" and record.terminal_at == timestamp.isoformat():
        return record
    if record.state != "issued":
        raise SoloAdministrationConfirmationConflictError(
            "solo-administration confirmation is no longer revocable"
        )
    if not (
        _parse_timestamp(record.created_at, "created_at")
        <= timestamp
        < _parse_timestamp(record.expires_at, "expires_at")
    ):
        raise SoloAdministrationConfirmationConflictError(
            "solo-administration confirmation is outside its revocation window"
        )
    return _validated_update(record, state="revoked", terminal_at=timestamp.isoformat())


def expire_solo_administration_confirmation(
    record: SoloAdministrationConfirmationRecord, *, terminal_at: str
) -> SoloAdministrationConfirmationRecord:
    timestamp = _parse_timestamp(terminal_at, "terminal_at")
    if record.state == "expired" and record.terminal_at == timestamp.isoformat():
        return record
    if record.state != "issued":
        raise SoloAdministrationConfirmationConflictError(
            "solo-administration confirmation is not expirable"
        )
    if timestamp < _parse_timestamp(record.expires_at, "expires_at"):
        raise SoloAdministrationConfirmationConflictError(
            "solo-administration confirmation has not expired"
        )
    return _validated_update(record, state="expired", terminal_at=timestamp.isoformat())


def consume_solo_administration_confirmation(
    record: SoloAdministrationConfirmationRecord,
    *,
    active_policy_record_id: str,
    active_policy_revision: int,
    active_policy_sha256: str,
    candidate_policy_sha256: str,
    candidate_administrator_quorum: int,
    candidate_distinct_human_administrator_count: int,
    reviewed_plan_sha256: str,
    human_session_id: str,
    github_id: int,
    idempotency_scope_sha256: str,
    idempotency_key_sha256: str,
    acknowledgement_sha256: str,
    terminal_at: str,
) -> SoloAdministrationConfirmationRecord:
    """Consume exactly one matching, unexpired confirmation."""

    timestamp = _parse_timestamp(terminal_at, "terminal_at")
    if record.state != "issued":
        raise SoloAdministrationConfirmationConflictError(
            "solo-administration confirmation is no longer consumable"
        )
    if not (
        _parse_timestamp(record.created_at, "created_at")
        <= timestamp
        < _parse_timestamp(record.expires_at, "expires_at")
    ):
        raise SoloAdministrationConfirmationConflictError(
            "solo-administration confirmation is expired"
        )
    supplied_values = (
        (
            record.active_policy_record_id,
            _normalize_token(active_policy_record_id, "active_policy_record_id"),
        ),
        (record.active_policy_revision, active_policy_revision),
        (
            record.active_policy_sha256,
            _normalize_sha256(active_policy_sha256, "active_policy_sha256"),
        ),
        (
            record.candidate_policy_sha256,
            _normalize_sha256(candidate_policy_sha256, "candidate_policy_sha256"),
        ),
        (record.candidate_administrator_quorum, candidate_administrator_quorum),
        (
            record.candidate_distinct_human_administrator_count,
            candidate_distinct_human_administrator_count,
        ),
        (
            record.reviewed_plan_sha256,
            _normalize_sha256(reviewed_plan_sha256, "reviewed_plan_sha256"),
        ),
        (record.human_session_id, _normalize_token(human_session_id, "human_session_id")),
        (record.github_id, github_id),
        (
            record.idempotency_scope_sha256,
            _normalize_sha256(idempotency_scope_sha256, "idempotency_scope_sha256"),
        ),
        (
            record.idempotency_key_sha256,
            _normalize_sha256(idempotency_key_sha256, "idempotency_key_sha256"),
        ),
        (
            record.acknowledgement_sha256,
            _normalize_sha256(acknowledgement_sha256, "acknowledgement_sha256"),
        ),
    )
    if any(
        not hmac.compare_digest(str(expected), str(supplied))
        for expected, supplied in supplied_values
    ):
        raise SoloAdministrationConfirmationConflictError(
            "solo-administration confirmation binding does not match"
        )
    return _validated_update(record, state="consumed", terminal_at=timestamp.isoformat())
