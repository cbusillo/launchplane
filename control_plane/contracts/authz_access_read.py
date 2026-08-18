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
    TerminalAgentIdentity,
)


EFFECTIVE_ACCESS_READ_ACTION = "authz_policy_effective_access.read"
AUTHZ_DENIAL_EXPLANATION_READ_ACTION = "authz_denial_explanation.read"


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
