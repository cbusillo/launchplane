from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.ordinary_agent import OrdinaryAgentTarget


CustodyEffectProfile = Literal[
    "guarded_merge",
    "head_refresh",
    "close_pull_request",
    "comment_pull_request",
    "label_pull_request",
]
CustodyIssueState = Literal[
    "minting",
    "issued",
    "issue_unknown",
    "cleanup_unknown",
    "closed",
]
CustodyCloseReason = Literal["not_dispatched", "confirmed_revoked", "known_expired"]
GITHUB_TOKEN_MAXIMUM_LIFETIME_SECONDS = 60 * 60
KNOWN_TOKEN_CLOCK_SKEW_SECONDS = 60


class OrdinaryAgentCustodyConflictError(ValueError):
    pass


class StrictCustodyModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class OrdinaryAgentCustodyCandidate(StrictCustodyModel):
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    repository_id: int = Field(gt=0, le=2**63 - 1)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=140)
    base_branch: str = Field(min_length=1, max_length=255)
    credential_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_version: int = Field(ge=1, le=2**63 - 1)
    secret_id: str = Field(min_length=1, max_length=256)
    secret_binding_id: str = Field(min_length=1, max_length=256)
    secret_version_id: str = Field(min_length=1, max_length=256)
    expected_app_id: int = Field(gt=0, le=2**63 - 1)
    effect_profile: CustodyEffectProfile

    @model_validator(mode="after")
    def validate_target(self) -> OrdinaryAgentCustodyCandidate:
        OrdinaryAgentTarget(
            repository_id=self.repository_id,
            repository=self.repository,
            base_branch=self.base_branch,
        )
        return self


class OrdinaryAgentCustodyIssueAttempt(StrictCustodyModel):
    schema_version: Literal[1] = 1
    attempt_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    idempotency_key_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    request_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    repository_id: int = Field(gt=0, le=2**63 - 1)
    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$", max_length=140)
    base_branch: str = Field(min_length=1, max_length=255)
    credential_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_version: int = Field(ge=1, le=2**63 - 1)
    secret_id: str = Field(min_length=1, max_length=256)
    secret_binding_id: str = Field(min_length=1, max_length=256)
    secret_version_id: str = Field(min_length=1, max_length=256)
    expected_app_id: int = Field(gt=0, le=2**63 - 1)
    effect_profile: CustodyEffectProfile
    requested_permissions: tuple[str, ...] = Field(min_length=1)
    state: CustodyIssueState
    mint_started_at: str
    dispatch_deadline: str
    app_id: int | None = Field(default=None, gt=0, le=2**63 - 1)
    installation_id: int | None = Field(default=None, gt=0, le=2**63 - 1)
    token_expires_at: str | None = None
    residual_expires_at: str | None = None
    closed_at: str | None = None
    close_reason: CustodyCloseReason | None = None
    updated_at: str

    @field_validator("requested_permissions", mode="before")
    @classmethod
    def normalize_requested_permissions(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_state_evidence(self) -> OrdinaryAgentCustodyIssueAttempt:
        started = _parse_timestamp(self.mint_started_at, "mint_started_at")
        deadline = _parse_timestamp(self.dispatch_deadline, "dispatch_deadline")
        _parse_timestamp(self.updated_at, "updated_at")
        if deadline <= started:
            raise ValueError("custody dispatch deadline must follow mint start")
        if len(set(self.requested_permissions)) != len(self.requested_permissions):
            raise ValueError("custody requested permissions must be unique")
        provider_evidence = (
            self.app_id,
            self.installation_id,
            self.token_expires_at,
            self.residual_expires_at,
        )
        if any(value is not None for value in provider_evidence) and any(
            value is None for value in provider_evidence
        ):
            raise ValueError("custody provider evidence must be complete or absent")
        if self.token_expires_at is not None:
            token_expiry = _parse_timestamp(self.token_expires_at, "token_expires_at")
            residual_expiry = _parse_timestamp(
                self.residual_expires_at or "", "residual_expires_at"
            )
            if residual_expiry <= token_expiry:
                raise ValueError("custody residual expiry must follow provider token expiry")
        if self.state == "issued":
            if any(value is None for value in provider_evidence):
                raise ValueError("issued custody attempt requires complete provider evidence")
        elif self.state == "cleanup_unknown":
            if self.app_id is None or self.installation_id is None:
                raise ValueError("cleanup uncertainty requires provider identity evidence")
        elif self.state in {"minting", "issue_unknown"} and any(
            value is not None for value in provider_evidence
        ):
            raise ValueError(f"{self.state} custody attempt cannot claim provider token evidence")
        if self.state == "closed":
            if self.closed_at is None or self.close_reason is None:
                raise ValueError("closed custody attempt requires closure evidence")
            _parse_timestamp(self.closed_at, "closed_at")
        elif self.closed_at is not None or self.close_reason is not None:
            raise ValueError("active custody attempt cannot claim closure")
        return self


def _parse_timestamp(value: str, field_name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"custody {field_name} is malformed") from error
    if parsed.tzinfo is None:
        raise ValueError(f"custody {field_name} must include a timezone")
    return parsed.astimezone(timezone.utc)
