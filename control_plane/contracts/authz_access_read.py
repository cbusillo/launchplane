from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.service_auth import (
    AuthzDecisionReason,
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    LaunchplaneAuthzPolicy,
    TerminalAgentIdentity,
)


EFFECTIVE_ACCESS_READ_ACTION = "authz_policy_effective_access.read"
AUTHZ_DENIAL_EXPLANATION_READ_ACTION = "authz_denial_explanation.read"
AUTHZ_POLICY_HEALTH_READ_ACTION = "authz_policy_health.read"
AUTHZ_POLICY_CANDIDATE_PREVIEW_READ_ACTION = "authz_policy_candidate_preview.read"
AUTHZ_POLICY_CANDIDATE_PREVIEW_MAX_PROBES = 25
AUTHZ_POLICY_CANDIDATE_PREVIEW_MAX_RULES = 500
AuthzPolicyPrincipalType: TypeAlias = Literal[
    "github_actions",
    "github_humans",
    "terminal_agents",
    "local_operators",
    "local_admins",
]
AuthzPolicyCandidateReadinessReason: TypeAlias = Literal[
    "repository_not_exact",
    "workflow_refs_not_singleton",
    "workflow_ref_not_exact",
    "job_workflow_refs_not_singleton",
    "job_workflow_ref_not_immutable",
    "actions_not_singleton",
    "action_not_exact",
    "products_not_singleton",
    "product_not_exact",
    "contexts_not_singleton",
    "context_not_exact",
    "instances_not_singleton",
    "instance_not_exact",
]


class GitHubActionsAccessPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_type: Literal["github_actions"] = "github_actions"
    repository: str
    repository_owner: str
    workflow_ref: str
    job_workflow_ref: str = ""
    ref: str
    ref_type: str
    event_name: str
    environment: str = ""
    subject: str
    sha: str = ""
    repository_id: str = ""
    repository_owner_id: str = ""

    @field_validator(
        "repository",
        "repository_owner",
        "workflow_ref",
        "job_workflow_ref",
        "ref",
        "ref_type",
        "event_name",
        "environment",
        "subject",
        "sha",
        "repository_id",
        "repository_owner_id",
    )
    @classmethod
    def _normalize_text(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) > 2048:
            raise ValueError("GitHub Actions access principal fields are too long.")
        return normalized

    @model_validator(mode="after")
    def _validate_required_identity(self) -> "GitHubActionsAccessPrincipal":
        required_values = (
            self.repository,
            self.repository_owner,
            self.workflow_ref,
            self.ref,
            self.ref_type,
            self.event_name,
            self.subject,
        )
        if not all(required_values):
            raise ValueError("GitHub Actions access principal requires exact identity fields.")
        return self

    def to_identity(self) -> GitHubActionsIdentity:
        return GitHubActionsIdentity(
            repository=self.repository,
            repository_owner=self.repository_owner,
            workflow_ref=self.workflow_ref,
            job_workflow_ref=self.job_workflow_ref,
            ref=self.ref,
            ref_type=self.ref_type,
            event_name=self.event_name,
            environment=self.environment,
            subject=self.subject,
            sha=self.sha,
            raw_claims={},
            repository_id=self.repository_id,
            repository_owner_id=self.repository_owner_id,
        )


class GitHubHumanAccessPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal_type: Literal["github_human"] = "github_human"
    login: str
    github_id: int = Field(default=0, ge=0)
    organizations: tuple[str, ...] = ()
    teams: tuple[str, ...] = ()
    role: Literal["read_only", "admin"]

    @field_validator("login")
    @classmethod
    def _normalize_login(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("GitHub human access principal requires login.")
        if len(normalized) > 256:
            raise ValueError("GitHub human access principal login is too long.")
        return normalized

    @field_validator("organizations", "teams")
    @classmethod
    def _normalize_memberships(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        normalized = tuple(dict.fromkeys(value.strip() for value in values if value.strip()))
        if len(normalized) > 128 or any(len(value) > 512 for value in normalized):
            raise ValueError("GitHub human access principal memberships are too broad.")
        return normalized

    def to_identity(self) -> GitHubHumanIdentity:
        return GitHubHumanIdentity(
            login=self.login,
            github_id=self.github_id,
            name="",
            email="",
            organizations=frozenset(self.organizations),
            teams=frozenset(self.teams),
            role=self.role,
        )


class TokenBoundAccessPrincipal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str
    token_label: str

    @field_validator("subject", "token_label")
    @classmethod
    def _normalize_required_text(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Token-bound access principal requires subject and token label.")
        if len(normalized) > 512:
            raise ValueError("Token-bound access principal field is too long.")
        return normalized


class TerminalAgentAccessPrincipal(TokenBoundAccessPrincipal):
    principal_type: Literal["terminal_agent"] = "terminal_agent"

    def to_identity(self) -> TerminalAgentIdentity:
        return TerminalAgentIdentity(subject=self.subject, token_label=self.token_label)


class LocalOperatorAccessPrincipal(TokenBoundAccessPrincipal):
    principal_type: Literal["local_operator"] = "local_operator"

    def to_identity(self) -> LocalOperatorIdentity:
        return LocalOperatorIdentity(subject=self.subject, token_label=self.token_label)


class LocalAdminAccessPrincipal(TokenBoundAccessPrincipal):
    principal_type: Literal["local_admin"] = "local_admin"

    def to_identity(self) -> LocalAdminIdentity:
        return LocalAdminIdentity(subject=self.subject, token_label=self.token_label)


EffectiveAccessPrincipal: TypeAlias = Annotated[
    GitHubActionsAccessPrincipal
    | GitHubHumanAccessPrincipal
    | TerminalAgentAccessPrincipal
    | LocalOperatorAccessPrincipal
    | LocalAdminAccessPrincipal,
    Field(discriminator="principal_type"),
]


class EffectiveAccessEvaluateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    principal: EffectiveAccessPrincipal
    action: str
    product: str
    context: str
    target_scope: Literal["context", "instance"]
    instance: str = ""

    @field_validator("action", "product", "context")
    @classmethod
    def _normalize_required_scope(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Effective access evaluation requires an explicit scope.")
        if len(normalized) > 512:
            raise ValueError("Effective access evaluation scope is too long.")
        return normalized

    @field_validator("instance")
    @classmethod
    def _normalize_instance(cls, value: str) -> str:
        normalized = value.strip()
        if len(normalized) > 512:
            raise ValueError("Effective access evaluation instance is too long.")
        if any(character in normalized for character in "*?[]"):
            raise ValueError("Effective access evaluation requires one exact instance.")
        return normalized

    @model_validator(mode="after")
    def _validate_target_scope(self) -> "EffectiveAccessEvaluateRequest":
        if self.target_scope == "instance" and not self.instance:
            raise ValueError("Instance target scope requires one exact instance.")
        if self.target_scope == "context" and self.instance:
            raise ValueError("Context target scope cannot declare an instance.")
        return self

    def identity(self) -> LaunchplaneIdentity:
        return self.principal.to_identity()


class EffectiveAccessRequestSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    principal_type: Literal[
        "github_actions", "github_human", "terminal_agent", "local_operator", "local_admin"
    ]
    action: str
    product: str
    context: str
    target_scope: Literal["context", "instance"]
    instance: str = ""


class EffectiveAccessDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: Literal["allowed", "denied"]
    reason_code: AuthzDecisionReason


class EffectiveAccessEvaluateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    policy_record_id: str
    policy_revision: int = Field(ge=1)
    policy_sha256: str
    request: EffectiveAccessRequestSummary
    evaluation: EffectiveAccessDecision


class AuthzPolicyCandidatePreviewRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    candidate_policy: LaunchplaneAuthzPolicy
    probes: tuple[EffectiveAccessEvaluateRequest, ...] = Field(
        default=(),
        max_length=AUTHZ_POLICY_CANDIDATE_PREVIEW_MAX_PROBES,
    )

    @model_validator(mode="after")
    def _validate_candidate(self) -> "AuthzPolicyCandidatePreviewRequest":
        if self.candidate_policy.schema_version != 2:
            raise ValueError("Authorization candidate policy preview requires schema version 2.")
        rule_count = sum(
            len(rules)
            for rules in (
                self.candidate_policy.github_actions,
                self.candidate_policy.github_humans,
                self.candidate_policy.terminal_agents,
                self.candidate_policy.local_operators,
                self.candidate_policy.local_admins,
            )
        )
        if rule_count > AUTHZ_POLICY_CANDIDATE_PREVIEW_MAX_RULES:
            raise ValueError("Authorization candidate policy preview contains too many rules.")
        probe_keys = tuple(probe.model_dump_json() for probe in self.probes)
        if len(set(probe_keys)) != len(probe_keys):
            raise ValueError("Authorization candidate policy preview probes must be unique.")
        return self


class AuthzDenialExplanationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    recorded_at: str
    route_path: str
    principal_type: Literal[
        "github_actions", "github_human", "terminal_agent", "local_operator", "local_admin"
    ]
    action: str
    product: str
    context: str
    target_scope: Literal["global", "context", "instance", "preview"]
    instance_specified: bool
    reason_code: AuthzDecisionReason
    policy_record_id: str
    policy_revision: int = Field(ge=1)
    policy_sha256: str


AuthzPolicyHealthState: TypeAlias = Literal["healthy", "attention_required", "blocked"]
AuthzPolicyHealthReasonCode: TypeAlias = Literal[
    "authz_policy_admin_unreachable",
    "authz_policy_independent_admin_unreachable",
    "policy_schema_legacy",
    "unmanaged_rules_present",
    "github_actions_legacy_name_only_rules_present",
    "github_actions_privileged_unpinned_reusable_rules_present",
]


class AuthzPrincipalRuleCounts(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    github_actions: int = Field(ge=0)
    github_humans: int = Field(ge=0)
    terminal_agents: int = Field(ge=0)
    local_operators: int = Field(ge=0)
    local_admins: int = Field(ge=0)


class AuthzPolicyRecordSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str
    revision: int = Field(ge=1)
    policy_sha256: str
    updated_at: str
    schema_version: Literal[1, 2]


class AuthzPolicyHealthSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    state: AuthzPolicyHealthState
    reason_codes: tuple[AuthzPolicyHealthReasonCode, ...]
    managed_rule_count: int = Field(ge=0)
    unmanaged_rule_count: int = Field(ge=0)
    github_actions_legacy_name_only_rule_count: int = Field(ge=0)
    github_actions_privileged_unpinned_reusable_rule_count: int = Field(ge=0)


class AuthzManagedSetSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    managed_set_id: str
    rule_count: int = Field(ge=1)
    principal_rule_counts: AuthzPrincipalRuleCounts


class AuthzManagedSetCollectionSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    total_count: int = Field(ge=0)
    returned_count: int = Field(ge=0)
    truncated: bool
    items: tuple[AuthzManagedSetSummary, ...]


class AuthzReachableAdministratorSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_reachable: bool
    rule_count: int = Field(ge=0)
    managed_rule_count: int = Field(ge=0)
    unmanaged_rule_count: int = Field(ge=0)
    principal_rule_counts: AuthzPrincipalRuleCounts
    caller_has_policy_administration: bool
    independent_from_caller_reachable: bool
    independent_from_caller_rule_count: int = Field(ge=0)


class AuthzPolicyHealthSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    policy: AuthzPolicyRecordSummary
    health: AuthzPolicyHealthSummary
    managed_sets: AuthzManagedSetCollectionSummary
    reachable_administrators: AuthzReachableAdministratorSummary


class AuthzPolicyHealthResponse(AuthzPolicyHealthSnapshot):
    status: Literal["ok"] = "ok"
    trace_id: str


class AuthzPolicyCandidateSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    submitted_policy_sha256: str
    evaluated_policy_sha256: str
    normalized: bool
    schema_version: Literal[2] = 2
    rule_count: int = Field(ge=0)


class AuthzPolicyCandidateStructuralDiff(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    changed: bool
    schema_changed: bool
    active_rule_count: int = Field(ge=0)
    candidate_rule_count: int = Field(ge=0)
    added_rule_count: int = Field(ge=0)
    updated_rule_count: int = Field(ge=0)
    removed_rule_count: int = Field(ge=0)
    unchanged_rule_count: int = Field(ge=0)
    active_managed_rule_count: int = Field(ge=0)
    candidate_managed_rule_count: int = Field(ge=0)
    active_unmanaged_rule_count: int = Field(ge=0)
    candidate_unmanaged_rule_count: int = Field(ge=0)
    added_managed_set_count: int = Field(ge=0)
    removed_managed_set_count: int = Field(ge=0)
    retained_managed_set_count: int = Field(ge=0)
    changed_principal_types: tuple[AuthzPolicyPrincipalType, ...]
    active_principal_rule_counts: AuthzPrincipalRuleCounts
    candidate_principal_rule_counts: AuthzPrincipalRuleCounts


class AuthzPolicyCandidateReadinessSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    blocked_rule_count: int = Field(ge=0)
    reason_codes: tuple[AuthzPolicyCandidateReadinessReason, ...]


AuthzPolicyCandidateProbeDelta: TypeAlias = Literal[
    "unchanged",
    "granted",
    "revoked",
    "reason_changed",
]


class AuthzPolicyCandidateProbeResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    request: EffectiveAccessRequestSummary
    active_evaluation: EffectiveAccessDecision
    candidate_evaluation: EffectiveAccessDecision
    delta: AuthzPolicyCandidateProbeDelta


class AuthzPolicyCandidatePreviewSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    active_policy: AuthzPolicyRecordSummary
    candidate_policy: AuthzPolicyCandidateSummary
    diff: AuthzPolicyCandidateStructuralDiff
    active_health: AuthzPolicyHealthSummary
    candidate_health: AuthzPolicyHealthSummary
    active_reachable_administrators: AuthzReachableAdministratorSummary
    candidate_reachable_administrators: AuthzReachableAdministratorSummary
    candidate_readiness: AuthzPolicyCandidateReadinessSummary
    probes: tuple[AuthzPolicyCandidateProbeResult, ...]


class AuthzPolicyCandidatePreviewResponse(AuthzPolicyCandidatePreviewSnapshot):
    status: Literal["ok"] = "ok"
    trace_id: str
