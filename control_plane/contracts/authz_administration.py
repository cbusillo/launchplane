from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.authz_access_read import (
    AuthzManagedSetCollectionSummary,
    AuthzPolicyHealthSummary,
    AuthzPrincipalRuleCounts,
    AuthzReachableAdministratorSummary,
)
from control_plane.contracts.privileged_operation import (
    ManagedAuthzPolicySetProposalInput,
    normalize_privileged_operation_source_event_id,
)
from control_plane.privileged_operation_service import (
    DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS,
    MAX_PRIVILEGED_OPERATION_TTL_SECONDS,
    MIN_PRIVILEGED_OPERATION_TTL_SECONDS,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy


AUTHZ_POLICY_ADMINISTRATION_READ_ACTION = "authz_policy_administration.read"
AUTHZ_POLICY_ADMINISTRATION_HISTORY_LIMIT = 50
AUTHZ_POLICY_ADMINISTRATION_MANAGED_RULE_LIMIT = 500


class AuthzPolicyAdministrationProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    revision: int = Field(ge=1)
    status: Literal["active", "superseded"]
    source: str
    updated_at: str
    policy_sha256: str
    schema_version: Literal[1, 2]


class AuthzManagedRuleIdentitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    managed_set_id: str
    managed_rule_id: str
    principal_type: Literal[
        "github_actions", "github_humans", "terminal_agents", "local_operators", "local_admins"
    ]
    rule_sha256: str


class AuthzManagedRuleIdentityCollection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    truncated: bool
    items: tuple[AuthzManagedRuleIdentitySummary, ...]


class AuthzPolicyAdministrationReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    trace_id: str
    policy: AuthzPolicyAdministrationProvenance
    principal_rule_counts: AuthzPrincipalRuleCounts
    health: AuthzPolicyHealthSummary
    managed_sets: AuthzManagedSetCollectionSummary
    reachable_administrators: AuthzReachableAdministratorSummary
    managed_rules: AuthzManagedRuleIdentityCollection


class AuthzPolicyRevisionAuditSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    audit_present: bool
    audit_sha256: str = ""
    operation: str = ""
    mode: str = ""
    managed_set_id: str = ""
    changed: bool | None = None
    diff_counts: dict[str, int] = Field(default_factory=dict)


class AuthzPolicyRevisionHistoryEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: AuthzPolicyAdministrationProvenance
    audit: AuthzPolicyRevisionAuditSummary


class AuthzPolicyRevisionHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    trace_id: str
    returned_count: int = Field(ge=0)
    truncated: bool
    revisions: tuple[AuthzPolicyRevisionHistoryEntry, ...]


class AuthzActivePolicyExportResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    trace_id: str
    policy: AuthzPolicyAdministrationProvenance
    canonical_policy: LaunchplaneAuthzPolicy


class AuthzManagedSetRollbackProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    target_revision: int = Field(ge=1)
    managed_set_id: str = Field(min_length=1, max_length=96)
    reason: str = Field(min_length=1, max_length=240)
    related_issue: str = Field(default="", max_length=128)
    source_event_id: str = Field(min_length=1, max_length=128)

    @model_validator(mode="after")
    def _validate_request(self) -> "AuthzManagedSetRollbackProposalRequest":
        if self.schema_version != 1:
            raise ValueError("Unsupported authz managed-set rollback proposal schema version.")
        self.managed_set_id = self.managed_set_id.strip()
        self.reason = self.reason.strip()
        self.related_issue = self.related_issue.strip()
        self.source_event_id = normalize_privileged_operation_source_event_id(self.source_event_id)
        if not self.managed_set_id or not self.reason:
            raise ValueError("Authz managed-set rollback proposal requires exact rollback input.")
        return self


class AuthzManagedSetRollbackProposalPayload(BaseModel):
    """Exact request shape accepted by POST /v1/privileged-operations/plans."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    descriptor_id: Literal["managed-authz-policy-set"] = "managed-authz-policy-set"
    source_event_id: str
    expires_in_seconds: int = Field(
        default=DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS,
        ge=MIN_PRIVILEGED_OPERATION_TTL_SECONDS,
        le=MAX_PRIVILEGED_OPERATION_TTL_SECONDS,
    )
    request: ManagedAuthzPolicySetProposalInput


class AuthzManagedSetRollbackProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    trace_id: str
    target_policy: AuthzPolicyAdministrationProvenance
    proposal: AuthzManagedSetRollbackProposalPayload
