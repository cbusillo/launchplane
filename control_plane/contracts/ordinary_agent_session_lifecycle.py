from __future__ import annotations

from typing import Literal

from pydantic import Field, field_validator, model_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent import (
    OrdinaryAgentAction,
    OrdinaryAgentBudget,
    OrdinaryAgentPullRequest,
    OrdinaryAgentTarget,
    PrincipalProfile,
    StrictFrozenModel,
)


class OrdinaryAgentJobBinding(StrictFrozenModel):
    """Immutable attribution of controller history to one finite job revision."""

    request_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    scope_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    binding_revision: int = Field(ge=1, le=2**63 - 1)


class OrdinaryAgentSessionAttenuation(StrictFrozenModel):
    """Requested finite bounds; no caller-supplied approval provenance."""

    actions: tuple[OrdinaryAgentAction, ...] = Field(min_length=1)
    session_expires_at: int = Field(ge=1, le=2**63 - 1)
    lease_expires_at: int = Field(ge=1, le=2**63 - 1)
    action_limit: int = Field(ge=0, le=2**63 - 1)
    pull_request_limit: int = Field(ge=0, le=2**63 - 1)
    refresh_allowance: int = Field(ge=0, le=2**63 - 1)
    continuation_expires_at: int | None = Field(default=None, ge=1, le=2**63 - 1)

    @field_validator("actions", mode="before")
    @classmethod
    def read_actions(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_lifetimes(self) -> OrdinaryAgentSessionAttenuation:
        if len(set(self.actions)) != len(self.actions):
            raise ValueError("delegated actions must be unique")
        if self.lease_expires_at > self.session_expires_at:
            raise ValueError("standing lease cannot outlive its interactive session")
        if (
            self.continuation_expires_at is not None
            and self.continuation_expires_at <= self.session_expires_at
        ):
            raise ValueError("continuation must have an explicit later finite deadline")
        return self


class OrdinaryAgentSessionDelegation(OrdinaryAgentSessionAttenuation):
    """Server-derived provenance for an authenticated approved operation."""

    operation_id: str = Field(min_length=1, max_length=256)
    approval_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    receiver_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class OrdinaryAgentSessionRecord(StrictFrozenModel):
    schema_version: Literal[1] = 1
    session_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    credential_version: int = Field(ge=1, le=2**63 - 1)
    credential_digest: str = Field(pattern=r"^[0-9a-f]{64}$", repr=False)
    delegation: OrdinaryAgentSessionDelegation
    valid_from: int = Field(ge=0, le=2**63 - 1)
    expires_at: int = Field(ge=1, le=2**63 - 1)
    revoked_at: int | None = Field(default=None, ge=0, le=2**63 - 1)

    @model_validator(mode="after")
    def validate_lifetime(self) -> OrdinaryAgentSessionRecord:
        if self.expires_at <= self.valid_from:
            raise ValueError("session expiry must follow issuance")
        if self.expires_at != self.delegation.session_expires_at:
            raise ValueError("session expiry must preserve approved attenuation")
        return self


class OrdinaryAgentLeaseRecord(StrictFrozenModel):
    schema_version: Literal[1] = 1
    lease_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    session_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    target: OrdinaryAgentTarget
    action: OrdinaryAgentAction
    managed_set_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    managed_rule_id: str = Field(pattern=r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
    effective_decision_fingerprint: str = Field(pattern=r"^oae-fp-v1:[0-9a-f]{64}$")
    valid_from: int = Field(ge=0, le=2**63 - 1)
    expires_at: int = Field(ge=1, le=2**63 - 1)
    revoked_at: int | None = Field(default=None, ge=0, le=2**63 - 1)
    budget: OrdinaryAgentBudget
    revision: int = Field(default=1, ge=1, le=2**63 - 1)

    @model_validator(mode="after")
    def validate_lifetime(self) -> OrdinaryAgentLeaseRecord:
        if self.expires_at <= self.valid_from:
            raise ValueError("lease expiry must follow issuance")
        return self


class OrdinaryAgentFiniteRequestRecord(StrictFrozenModel):
    """One finite request is one job; execution history stays in linked records."""

    schema_version: Literal[1] = 1
    request_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    idempotency_key: str = Field(min_length=1, max_length=256)
    principal_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    session_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    lease_id: str = Field(pattern=r"^[a-z][a-z0-9_-]{2,127}$")
    target: OrdinaryAgentTarget
    base_sha: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    pull_requests: tuple[OrdinaryAgentPullRequest, ...] = Field(min_length=1)
    permitted_stack_edit_pull_requests: tuple[int, ...]
    binding_revision: int = Field(default=1, ge=1, le=2**63 - 1)
    refresh_allowance_total: int = Field(ge=0, le=2**63 - 1)
    refresh_used: int = Field(default=0, ge=0, le=2**63 - 1)
    admitted_at: int = Field(ge=0, le=2**63 - 1)
    expires_at: int = Field(ge=1, le=2**63 - 1)
    continuation_expires_at: int | None = Field(default=None, ge=1, le=2**63 - 1)
    status: Literal["waiting", "cancelled", "completed", "reconciliation_required"] = "waiting"
    cancellation_requested_at: int | None = Field(default=None, ge=0, le=2**63 - 1)
    execution_record_ids: tuple[str, ...] = ()

    @field_validator(
        "pull_requests", "permitted_stack_edit_pull_requests", "execution_record_ids", mode="before"
    )
    @classmethod
    def read_json_tuples(cls, value: object) -> object:
        return tuple(value) if isinstance(value, list) else value

    @model_validator(mode="after")
    def validate_scope_and_lifetime(self) -> OrdinaryAgentFiniteRequestRecord:
        numbers = tuple(item.number for item in self.pull_requests)
        if len(set(numbers)) != len(numbers):
            raise ValueError("finite request PRs must be unique")
        stack = self.permitted_stack_edit_pull_requests
        if len(set(stack)) != len(stack) or not set(stack).issubset(numbers):
            raise ValueError("stack edit scope must be a unique subset of request PRs")
        if self.refresh_used > self.refresh_allowance_total:
            raise ValueError("refresh spending cannot exceed original allowance")
        if self.expires_at <= self.admitted_at:
            raise ValueError("request must expire after admission")
        if (
            self.continuation_expires_at is not None
            and self.continuation_expires_at <= self.expires_at
        ):
            raise ValueError("continuation must have a later finite deadline")
        if any(not item or len(item) > 256 for item in self.execution_record_ids):
            raise ValueError("execution links must be bounded record identifiers")
        return self

    @property
    def scope_sha256(self) -> str:
        return canonical_json_sha256(
            {
                "principal_id": self.principal_id,
                "session_id": self.session_id,
                "lease_id": self.lease_id,
                "target": self.target.model_dump(mode="json"),
                "pull_requests": [item.number for item in self.pull_requests],
                "permitted_stack_edit_pull_requests": list(self.permitted_stack_edit_pull_requests),
            }
        )


class OrdinaryAgentSessionOperationView(StrictFrozenModel):
    """Public diagnostic projection, never an admission or execution capability."""

    schema_version: Literal[1] = 1
    principal_id: str
    operation_id: str
    kind: Literal["initial", "existing"]
    status: Literal["pending", "approved", "expired", "revoked", "blocked", "cancelled"]
    reason_code: str | None = None
    current_policy_actions: tuple[OrdinaryAgentAction, ...] = ()
    current_policy_execution_profile: PrincipalProfile | None = None
    requester_kind: Literal["terminal_agent", "ordinary_agent"]
    requester_subject: str
    requester_token_label: str | None = None
    credential_expires_at: int
    delivery_expires_at: int | None = None
    attenuation: OrdinaryAgentSessionAttenuation | None
    credential_id: str
    credential_version: int
    target: OrdinaryAgentTarget
    session_id: str | None = None
    session_expires_at: int | None = None
    applied: bool = False
    can_approve: bool = False


class OrdinaryAgentConnectionView(StrictFrozenModel):
    """Sanitized result of an administrator disconnect, not a credential capability."""

    schema_version: Literal[1] = 1
    principal_id: str
    status: Literal["revoked"]
