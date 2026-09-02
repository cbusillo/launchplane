from __future__ import annotations

import asyncio
from fnmatch import fnmatchcase
import re
import secrets
import tomllib
from dataclasses import dataclass
from pathlib import Path
from threading import local
from typing import Annotated, Any, Literal, Protocol, TypeAlias, cast
from weakref import WeakKeyDictionary

import jwt
from pydantic import (
    AfterValidator,
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)

from control_plane.authz_scope import (
    exact_instance_workflow_authz_actions,
    exclusively_instance_scoped_authz_actions,
    instance_scoped_authz_actions,
)


GITHUB_ACTIONS_OIDC_ISSUER = "https://token.actions.githubusercontent.com"


@dataclass(frozen=True)
class GitHubActionsIdentity:
    repository: str
    repository_owner: str
    workflow_ref: str
    job_workflow_ref: str
    ref: str
    ref_type: str
    event_name: str
    environment: str
    subject: str
    sha: str
    raw_claims: dict[str, object]
    repository_id: str = ""
    repository_owner_id: str = ""


@dataclass(frozen=True)
class GitHubHumanIdentity:
    login: str
    github_id: int
    name: str
    email: str
    organizations: frozenset[str]
    teams: frozenset[str]
    role: Literal["read_only", "admin"]


@dataclass(frozen=True)
class TerminalAgentIdentity:
    subject: str
    token_label: str


@dataclass(frozen=True)
class LocalOperatorIdentity:
    subject: str
    token_label: str


@dataclass(frozen=True)
class LocalAdminIdentity:
    subject: str
    token_label: str


LaunchplaneIdentity: TypeAlias = (
    GitHubActionsIdentity
    | GitHubHumanIdentity
    | TerminalAgentIdentity
    | LocalOperatorIdentity
    | LocalAdminIdentity
)
AuthorizationScope: TypeAlias = Literal["global", "context", "instance", "preview"]
AuthzPolicySchemaVersion: TypeAlias = Literal[1, 2]
AgentConsumerSubjectType: TypeAlias = Literal[
    "github_actions", "github_human", "terminal_agent", "local_operator", "local_admin"
]
AgentConsumerAccessProfile: TypeAlias = Literal[
    "automation_worker",
    "human_admin",
    "limited_remote_user",
    "owner_local_agent",
]
AgentConsumerActionSafety: TypeAlias = Literal[
    "read",
    "safe_write",
    "mutation",
    "prod",
    "destructive",
    "secret_backed",
    "policy_admin",
]
AgentAuthzDecision: TypeAlias = Literal["allowed", "denied"]
AuthzDecisionReason: TypeAlias = Literal[
    "allowed",
    "authz_policy_schema_incompatible",
    "instance_scope_required",
    "principal_role_restricted",
    "principal_binding_invalid",
    "no_matching_grant",
]


class AuthzEvaluation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    decision: AgentAuthzDecision
    reason_code: AuthzDecisionReason
    principal_type: AgentConsumerSubjectType
    action: str
    product: str
    context: str
    target_scope: AuthorizationScope
    instance_specified: bool


_AUTHZ_EVALUATION_BY_TASK: WeakKeyDictionary[asyncio.Task[Any], AuthzEvaluation] = (
    WeakKeyDictionary()
)
_AUTHZ_EVALUATION_BY_THREAD = local()


def _current_asyncio_task() -> asyncio.Task[Any] | None:
    try:
        return asyncio.current_task()
    except RuntimeError:
        return None


def current_authz_evaluation() -> AuthzEvaluation | None:
    task = _current_asyncio_task()
    if task is not None:
        return _AUTHZ_EVALUATION_BY_TASK.get(task)
    evaluation = getattr(_AUTHZ_EVALUATION_BY_THREAD, "evaluation", None)
    return evaluation if isinstance(evaluation, AuthzEvaluation) else None


def clear_authz_evaluation() -> None:
    task = _current_asyncio_task()
    if task is not None:
        _AUTHZ_EVALUATION_BY_TASK.pop(task, None)
    _AUTHZ_EVALUATION_BY_THREAD.evaluation = None


def _authz_principal_type(identity: LaunchplaneIdentity) -> AgentConsumerSubjectType:
    if isinstance(identity, GitHubHumanIdentity):
        return "github_human"
    if isinstance(identity, TerminalAgentIdentity):
        return "terminal_agent"
    if isinstance(identity, LocalOperatorIdentity):
        return "local_operator"
    if isinstance(identity, LocalAdminIdentity):
        return "local_admin"
    return "github_actions"


def _record_authz_evaluation(
    *,
    identity: LaunchplaneIdentity,
    action: str,
    product: str,
    context: str,
    target: AuthorizationTarget,
    allowed: bool,
    reason_code: AuthzDecisionReason,
    record_context: bool,
) -> AuthzEvaluation:
    evaluation = AuthzEvaluation(
        decision="allowed" if allowed else "denied",
        reason_code=reason_code,
        principal_type=_authz_principal_type(identity),
        action=action,
        product=product,
        context=context,
        target_scope=target.scope,
        instance_specified=bool(target.instances),
    )
    if record_context:
        task = _current_asyncio_task()
        if task is not None:
            _AUTHZ_EVALUATION_BY_TASK[task] = evaluation
        else:
            _AUTHZ_EVALUATION_BY_THREAD.evaluation = evaluation
    return evaluation


def _validated_instance_selectors(instances: tuple[str, ...]) -> tuple[str, ...]:
    normalized_instances = tuple(
        dict.fromkeys(instance.strip() for instance in instances if instance.strip())
    )
    if "*" in normalized_instances and normalized_instances != ("*",):
        raise ValueError("Authz instance wildcard must be the only instance selector.")
    for instance in normalized_instances:
        if instance != "*" and any(character in instance for character in "*?[]"):
            raise ValueError("Authz instance selectors must be exact or the lone '*' wildcard.")
    return normalized_instances


AuthzInstanceSelectors = Annotated[
    tuple[str, ...],
    AfterValidator(_validated_instance_selectors),
]


_MANAGED_AUTHZ_ID_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,127}$")


class ManagedAuthzPolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    managed_set_id: str | None = None
    managed_rule_id: str | None = None

    @model_validator(mode="after")
    def _validate_managed_identity(self) -> "ManagedAuthzPolicyRule":
        managed_set_id = (self.managed_set_id or "").strip()
        managed_rule_id = (self.managed_rule_id or "").strip()
        if bool(managed_set_id) != bool(managed_rule_id):
            raise ValueError(
                "Managed authz policy rules require both managed_set_id and managed_rule_id."
            )
        if not managed_set_id:
            self.managed_set_id = None
            self.managed_rule_id = None
            return self
        for label, value in (
            ("managed_set_id", managed_set_id),
            ("managed_rule_id", managed_rule_id),
        ):
            if _MANAGED_AUTHZ_ID_PATTERN.fullmatch(value) is None:
                raise ValueError(
                    f"Authz {label} must be a lowercase stable identifier using "
                    "letters, numbers, '.', '_', ':', '/', or '-'."
                )
        self.managed_set_id = managed_set_id
        self.managed_rule_id = managed_rule_id
        return self


class AuthorizationTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scope: AuthorizationScope
    instances: AuthzInstanceSelectors = ()

    @model_validator(mode="after")
    def _validate_scope(self) -> "AuthorizationTarget":
        if self.scope == "instance" and not self.instances:
            raise ValueError("Instance-scoped authorization requires at least one instance.")
        if self.scope != "instance" and self.instances:
            raise ValueError("Only instance-scoped authorization can declare instances.")
        return self


def _instances_allowed(
    *,
    allowed_instances: AuthzInstanceSelectors,
    target: AuthorizationTarget | None,
    schema_version: AuthzPolicySchemaVersion,
) -> bool:
    if target is None or target.scope != "instance":
        return not allowed_instances
    if not allowed_instances:
        return schema_version == 1
    if allowed_instances == ("*",):
        return True
    return set(target.instances).issubset(allowed_instances)


def authz_selector_matches(value: str, selectors: tuple[str, ...]) -> bool:
    normalized_value = value.strip()
    return any(fnmatchcase(normalized_value, selector) for selector in selectors)


class ScopedAuthzPolicyRule(ManagedAuthzPolicyRule):
    products: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()
    instances: AuthzInstanceSelectors = ()
    actions: tuple[str, ...] = ()

    def _scope_selector_matches(self, value: str, selectors: tuple[str, ...]) -> bool:
        return value in selectors

    def allows_scope(
        self,
        *,
        action: str,
        product: str,
        context: str,
        target: AuthorizationTarget | None,
        schema_version: AuthzPolicySchemaVersion,
    ) -> bool:
        return (
            (not self.products or self._scope_selector_matches(product, self.products))
            and (not self.contexts or self._scope_selector_matches(context, self.contexts))
            and _instances_allowed(
                allowed_instances=self.instances,
                target=target,
                schema_version=schema_version,
            )
            and (not self.actions or action in self.actions)
        )


class PatternScopedAuthzPolicyRule(ScopedAuthzPolicyRule):
    def _scope_selector_matches(self, value: str, selectors: tuple[str, ...]) -> bool:
        return authz_selector_matches(value, selectors)


class TokenBoundAuthzPolicyRule(ScopedAuthzPolicyRule):
    subjects: tuple[str, ...] = ()
    token_labels: tuple[str, ...] = ()

    def allows_token_bound_identity(
        self,
        *,
        subject: str,
        token_label: str,
        action: str,
        product: str,
        context: str,
        target: AuthorizationTarget | None,
        schema_version: AuthzPolicySchemaVersion,
    ) -> bool:
        return (
            (not self.subjects or authz_selector_matches(subject, self.subjects))
            and (not self.token_labels or authz_selector_matches(token_label, self.token_labels))
            and self.allows_scope(
                action=action,
                product=product,
                context=context,
                target=target,
                schema_version=schema_version,
            )
        )


class PatternTokenBoundAuthzPolicyRule(TokenBoundAuthzPolicyRule, PatternScopedAuthzPolicyRule):
    pass


class TokenVerifier(Protocol):
    def verify(self, token: str) -> GitHubActionsIdentity: ...


class BearerIdentityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    every_code_worker_token: str = ""
    engineering_review_worker_runtime_id: str = ""
    engineering_review_worker_host: str = ""
    local_admin_token: str = ""
    local_admin_subject: str = ""
    local_admin_token_label: str = ""
    local_operator_token: str = ""
    local_operator_subject: str = ""
    local_operator_token_label: str = ""
    terminal_agent_token: str = ""
    terminal_agent_subject: str = ""
    terminal_agent_token_label: str = ""


def read_bearer_token(authorization_header: str) -> str:
    header = authorization_header.strip()
    if not header:
        raise PermissionError("Authorization header is required.")
    scheme, _, token = header.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise PermissionError("Authorization header must use Bearer token format.")
    return token.strip()


def bearer_identity_from_token(
    *, token: str, config: BearerIdentityConfig
) -> TerminalAgentIdentity | LocalOperatorIdentity | LocalAdminIdentity | None:
    if config.local_admin_token.strip() and secrets.compare_digest(
        token, config.local_admin_token.strip()
    ):
        return LocalAdminIdentity(
            subject=_required_bearer_identity_config_value(
                config.local_admin_subject,
                "LAUNCHPLANE_LOCAL_ADMIN_SUBJECT",
            ),
            token_label=_required_bearer_identity_config_value(
                config.local_admin_token_label,
                "LAUNCHPLANE_LOCAL_ADMIN_TOKEN_LABEL",
            ),
        )
    if config.local_operator_token.strip() and secrets.compare_digest(
        token, config.local_operator_token.strip()
    ):
        return LocalOperatorIdentity(
            subject=_required_bearer_identity_config_value(
                config.local_operator_subject,
                "LAUNCHPLANE_LOCAL_OPERATOR_SUBJECT",
            ),
            token_label=_required_bearer_identity_config_value(
                config.local_operator_token_label,
                "LAUNCHPLANE_LOCAL_OPERATOR_TOKEN_LABEL",
            ),
        )
    if config.terminal_agent_token.strip() and secrets.compare_digest(
        token, config.terminal_agent_token.strip()
    ):
        return TerminalAgentIdentity(
            subject=_required_bearer_identity_config_value(
                config.terminal_agent_subject,
                "LAUNCHPLANE_TERMINAL_AGENT_SUBJECT",
            ),
            token_label=_required_bearer_identity_config_value(
                config.terminal_agent_token_label,
                "LAUNCHPLANE_TERMINAL_AGENT_TOKEN_LABEL",
            ),
        )
    return None


def _required_bearer_identity_config_value(value: str, env_var_name: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise PermissionError(f"{env_var_name} is required for configured bearer auth.")
    return normalized_value


class GitHubOidcVerifier:
    def __init__(
        self,
        *,
        audience: str,
        issuer: str = GITHUB_ACTIONS_OIDC_ISSUER,
        jwks_url: str = f"{GITHUB_ACTIONS_OIDC_ISSUER}/.well-known/jwks",
        jwk_client: jwt.PyJWKClient | None = None,
    ) -> None:
        self._audience = audience.strip()
        self._issuer = issuer.strip()
        resolved_jwks_url = jwks_url.strip()
        if not self._audience:
            raise ValueError("OIDC verifier requires audience.")
        if not self._issuer:
            raise ValueError("OIDC verifier requires issuer.")
        if not resolved_jwks_url:
            raise ValueError("OIDC verifier requires jwks_url.")
        self._jwk_client = jwk_client or jwt.PyJWKClient(resolved_jwks_url)

    def verify(self, token: str) -> GitHubActionsIdentity:
        normalized_token = token.strip()
        if not normalized_token:
            raise ValueError("OIDC bearer token is required.")
        signing_key = self._jwk_client.get_signing_key_from_jwt(normalized_token)
        claims = jwt.decode(
            normalized_token,
            signing_key.key,
            algorithms=["RS256"],
            audience=self._audience,
            issuer=self._issuer,
        )
        repository = str(claims.get("repository", "")).strip()
        repository_owner = str(claims.get("repository_owner", "")).strip()
        repository_id = str(claims.get("repository_id", "")).strip()
        repository_owner_id = str(claims.get("repository_owner_id", "")).strip()
        workflow_ref = str(claims.get("workflow_ref", "")).strip()
        if not repository:
            raise ValueError("OIDC token is missing repository claim.")
        if not repository_owner:
            raise ValueError("OIDC token is missing repository_owner claim.")
        if not repository_id or not repository_id.isdecimal():
            raise ValueError("OIDC token is missing numeric repository_id claim.")
        if not repository_owner_id or not repository_owner_id.isdecimal():
            raise ValueError("OIDC token is missing numeric repository_owner_id claim.")
        if not workflow_ref:
            raise ValueError("OIDC token is missing workflow_ref claim.")
        return GitHubActionsIdentity(
            repository=repository,
            repository_owner=repository_owner,
            workflow_ref=workflow_ref,
            job_workflow_ref=str(claims.get("job_workflow_ref", "")).strip(),
            ref=str(claims.get("ref", "")).strip(),
            ref_type=str(claims.get("ref_type", "")).strip(),
            event_name=str(claims.get("event_name", "")).strip(),
            environment=str(claims.get("environment", "")).strip(),
            subject=str(claims.get("sub", "")).strip(),
            sha=str(claims.get("sha", "")).strip(),
            raw_claims=dict(claims),
            repository_id=repository_id,
            repository_owner_id=repository_owner_id,
        )


class GitHubActionsPolicyRule(PatternScopedAuthzPolicyRule):
    model_config = ConfigDict(extra="forbid")

    repository: str
    repository_id: str = ""
    repository_owner_id: str = ""
    workflow_refs: tuple[str, ...] = ()
    job_workflow_refs: tuple[str, ...] = ()
    event_names: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_repository_identity(self) -> "GitHubActionsPolicyRule":
        self.repository = self.repository.strip()
        self.repository_id = self.repository_id.strip()
        self.repository_owner_id = self.repository_owner_id.strip()
        if bool(self.repository_id) != bool(self.repository_owner_id):
            raise ValueError(
                "GitHub Actions authz rules require both repository_id and "
                "repository_owner_id when either immutable identifier is declared."
            )
        for label, value in (
            ("repository_id", self.repository_id),
            ("repository_owner_id", self.repository_owner_id),
        ):
            if value and not value.isdecimal():
                raise ValueError(f"GitHub Actions authz {label} must be a numeric GitHub ID.")
        return self

    @staticmethod
    def _matches_claim(value: str, allowed_values: tuple[str, ...]) -> bool:
        return authz_selector_matches(value, allowed_values)

    def allows(
        self,
        *,
        identity: GitHubActionsIdentity,
        action: str,
        product: str,
        context: str,
        target: AuthorizationTarget | None = None,
        schema_version: AuthzPolicySchemaVersion = 1,
    ) -> bool:
        if self.repository.strip() != identity.repository:
            return False
        if self.repository_id and self.repository_id != identity.repository_id:
            return False
        if self.repository_owner_id and self.repository_owner_id != identity.repository_owner_id:
            return False
        if self.workflow_refs and not self._matches_claim(
            identity.workflow_ref, self.workflow_refs
        ):
            return False
        if self.job_workflow_refs and not self._matches_claim(
            identity.job_workflow_ref, self.job_workflow_refs
        ):
            return False
        if self.event_names and identity.event_name not in self.event_names:
            return False
        if self.refs and identity.ref not in self.refs:
            return False
        if self.environments and identity.environment not in self.environments:
            return False
        return self.allows_scope(
            action=action,
            product=product,
            context=context,
            target=target,
            schema_version=schema_version,
        )


class GitHubHumanPolicyRule(ScopedAuthzPolicyRule):
    model_config = ConfigDict(extra="forbid")

    github_ids: tuple[int, ...] = ()
    logins: tuple[str, ...] = ()
    organizations: tuple[str, ...] = ()
    teams: tuple[str, ...] = ()
    roles: tuple[Literal["read_only", "admin"], ...] = ()

    @model_validator(mode="after")
    def _validate_github_ids(self) -> "GitHubHumanPolicyRule":
        if any(github_id < 1 for github_id in self.github_ids):
            raise ValueError("GitHub human policy rule github_ids must be positive.")
        self.github_ids = tuple(dict.fromkeys(self.github_ids))
        return self

    @staticmethod
    def _matches_any(value: str, allowed_values: tuple[str, ...]) -> bool:
        return authz_selector_matches(value, allowed_values)

    @staticmethod
    def _intersects(values: frozenset[str], allowed_values: tuple[str, ...]) -> bool:
        return any(
            fnmatchcase(value.strip(), allowed_value)
            for value in values
            for allowed_value in allowed_values
        )

    def allows(
        self,
        *,
        identity: GitHubHumanIdentity,
        action: str,
        product: str,
        context: str,
        target: AuthorizationTarget | None = None,
        schema_version: AuthzPolicySchemaVersion = 1,
    ) -> bool:
        if not self.matches_principal(
            github_id=identity.github_id,
            login=identity.login,
            organizations=identity.organizations,
            teams=identity.teams,
            role=identity.role,
        ):
            return False
        return self.allows_scope(
            action=action,
            product=product,
            context=context,
            target=target,
            schema_version=schema_version,
        )

    def matches_principal(
        self,
        *,
        github_id: int,
        login: str,
        organizations: frozenset[str],
        teams: frozenset[str],
        role: Literal["read_only", "admin"],
    ) -> bool:
        if self.github_ids and github_id not in self.github_ids:
            return False
        if self.logins and not self._matches_any(login, self.logins):
            return False
        if self.organizations and not self._intersects(organizations, self.organizations):
            return False
        if self.teams and not self._intersects(teams, self.teams):
            return False
        if self.roles and role not in self.roles:
            return False
        return True


class TerminalAgentPolicyRule(TokenBoundAuthzPolicyRule):
    model_config = ConfigDict(extra="forbid")

    def allows(
        self,
        *,
        identity: TerminalAgentIdentity,
        action: str,
        product: str,
        context: str,
        target: AuthorizationTarget | None = None,
        schema_version: AuthzPolicySchemaVersion = 1,
    ) -> bool:
        return self.allows_token_bound_identity(
            subject=identity.subject,
            token_label=identity.token_label,
            action=action,
            product=product,
            context=context,
            target=target,
            schema_version=schema_version,
        )


class LocalOperatorPolicyRule(PatternTokenBoundAuthzPolicyRule):
    model_config = ConfigDict(extra="forbid")

    def allows(
        self,
        *,
        identity: LocalOperatorIdentity,
        action: str,
        product: str,
        context: str,
        target: AuthorizationTarget | None = None,
        schema_version: AuthzPolicySchemaVersion = 1,
    ) -> bool:
        return self.allows_token_bound_identity(
            subject=identity.subject,
            token_label=identity.token_label,
            action=action,
            product=product,
            context=context,
            target=target,
            schema_version=schema_version,
        )


class LocalAdminPolicyRule(PatternTokenBoundAuthzPolicyRule):
    model_config = ConfigDict(extra="forbid")

    def allows(
        self,
        *,
        identity: LocalAdminIdentity,
        action: str,
        product: str,
        context: str,
        target: AuthorizationTarget | None = None,
        schema_version: AuthzPolicySchemaVersion = 1,
    ) -> bool:
        return self.allows_token_bound_identity(
            subject=identity.subject,
            token_label=identity.token_label,
            action=action,
            product=product,
            context=context,
            target=target,
            schema_version=schema_version,
        )


class AgentConsumerSubject(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject_type: AgentConsumerSubjectType
    subject: str
    display_label: str
    access_profile: AgentConsumerAccessProfile
    role: Literal["read_only", "admin", "worker", "operator"] = "read_only"
    product: str = ""
    context: str = ""
    action: str = ""
    action_safety: AgentConsumerActionSafety = "read"
    read_only_context: bool = False
    approval_capable: bool = False


class AgentAuthzAudit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: AgentAuthzDecision
    reason_code: str
    subject: AgentConsumerSubject
    action: str
    product: str
    context: str
    policy_source: str
    policy_sha256: str
    source_kind: Literal["authz_policy"] = "authz_policy"


def action_safety(action: str) -> AgentConsumerActionSafety:
    normalized_action = action.strip()
    if not normalized_action:
        return "read"
    action_parts = tuple(part for part in re.split(r"[_.-]+", normalized_action) if part)
    if normalized_action.startswith(
        ("authz_policy", "merge_train_policy_operation")
    ) or normalized_action in {
        "change_impact_policy.write",
        "engineering_review_authority.write",
        "product_owner_policy.write",
        "product_owner_requirement.write",
        "product_owner_routing.write",
    }:
        return "policy_admin"
    if "secret" in action_parts or normalized_action.endswith(".secret"):
        return "secret_backed"
    if any(
        part in action_parts for part in ("destroy", "cleanup", "delete", "rollback", "restore")
    ):
        return "destructive"
    if "prod" in action_parts or "promotion" in action_parts:
        return "prod"
    if normalized_action in {"authz_diagnostic.evaluate", "work_graph.rank"} or (
        normalized_action.endswith(".read")
    ):
        return "read"
    if (
        normalized_action.endswith(".write")
        or "rerun" in action_parts
        or "reconcile" in action_parts
    ):
        return "safe_write"
    return "mutation"


def limited_remote_user_action_allowed(action: str) -> bool:
    return action_safety(action) in {"read", "safe_write"}


def local_operator_identity_valid(*, identity: LocalOperatorIdentity, action: str) -> bool:
    return (
        bool(identity.subject.strip())
        and bool(identity.token_label.strip())
        and bool(action.strip())
    )


def local_admin_identity_valid(*, identity: LocalAdminIdentity, action: str) -> bool:
    return (
        bool(identity.subject.strip())
        and bool(identity.token_label.strip())
        and bool(action.strip())
    )


def agent_consumer_subject(
    *, identity: LaunchplaneIdentity, action: str = "", product: str = "", context: str = ""
) -> AgentConsumerSubject:
    safety = action_safety(action)
    if isinstance(identity, GitHubHumanIdentity):
        return AgentConsumerSubject(
            subject_type="github_human",
            subject=identity.login,
            display_label=identity.login,
            access_profile=("human_admin" if identity.role == "admin" else "limited_remote_user"),
            role=identity.role,
            product=product,
            context=context,
            action=action,
            action_safety=safety,
            read_only_context=identity.role == "read_only",
            approval_capable=identity.role == "admin",
        )
    if isinstance(identity, TerminalAgentIdentity):
        return AgentConsumerSubject(
            subject_type="terminal_agent",
            subject=identity.subject,
            display_label=identity.token_label,
            access_profile="owner_local_agent",
            role="worker",
            product=product,
            context=context,
            action=action,
            action_safety=safety,
            read_only_context=True,
        )
    if isinstance(identity, LocalOperatorIdentity):
        return AgentConsumerSubject(
            subject_type="local_operator",
            subject=identity.subject,
            display_label=identity.token_label,
            access_profile="owner_local_agent",
            role="operator",
            product=product,
            context=context,
            action=action,
            action_safety=safety,
            read_only_context=safety == "read",
            approval_capable=safety in {"mutation", "prod", "destructive", "secret_backed"},
        )
    if isinstance(identity, LocalAdminIdentity):
        return AgentConsumerSubject(
            subject_type="local_admin",
            subject=identity.subject,
            display_label=identity.token_label,
            access_profile="owner_local_agent",
            role="admin",
            product=product,
            context=context,
            action=action,
            action_safety=safety,
            read_only_context=safety == "read",
            approval_capable=True,
        )
    return AgentConsumerSubject(
        subject_type="github_actions",
        subject=identity.subject or identity.workflow_ref,
        display_label=identity.repository,
        access_profile="automation_worker",
        role="worker",
        product=product,
        context=context,
        action=action,
        action_safety=safety,
        read_only_context=safety == "read",
        approval_capable=safety
        in {"mutation", "prod", "destructive", "secret_backed", "policy_admin"},
    )


def agent_authz_audit(
    *,
    identity: LaunchplaneIdentity,
    action: str,
    product: str,
    context: str,
    decision: AgentAuthzDecision,
    reason_code: str,
    policy_source: str,
    policy_sha256: str,
) -> AgentAuthzAudit:
    return AgentAuthzAudit(
        decision=decision,
        reason_code=reason_code,
        subject=agent_consumer_subject(
            identity=identity,
            action=action,
            product=product,
            context=context,
        ),
        action=action,
        product=product,
        context=context,
        policy_source=policy_source,
        policy_sha256=policy_sha256,
    )


class LaunchplaneAuthzPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: AuthzPolicySchemaVersion = 1
    administrator_quorum: int | None = Field(default=None, ge=1)
    github_actions: tuple[GitHubActionsPolicyRule, ...] = ()
    github_humans: tuple[GitHubHumanPolicyRule, ...] = ()
    terminal_agents: tuple[TerminalAgentPolicyRule, ...] = ()
    local_operators: tuple[LocalOperatorPolicyRule, ...] = ()
    local_admins: tuple[LocalAdminPolicyRule, ...] = ()

    @model_validator(mode="after")
    def _validate_instance_rule_schema(self) -> "LaunchplaneAuthzPolicy":
        if self.schema_version == 1 and self.administrator_quorum is not None:
            raise ValueError("Schema-v1 authz policies cannot declare administrator_quorum.")
        instance_actions = instance_scoped_authz_actions()
        exclusively_instance_actions = exclusively_instance_scoped_authz_actions()
        managed_identities: set[tuple[str, str]] = set()
        for rules in (
            self.github_actions,
            self.github_humans,
            self.terminal_agents,
            self.local_operators,
            self.local_admins,
        ):
            for rule in rules:
                if rule.managed_set_id is not None and rule.managed_rule_id is not None:
                    managed_identity = (rule.managed_set_id, rule.managed_rule_id)
                    if managed_identity in managed_identities:
                        raise ValueError(
                            "Authz managed rule identities must be unique across the policy."
                        )
                    managed_identities.add(managed_identity)
                if self.schema_version == 1 and rule.instances:
                    raise ValueError("Schema-v1 authz policy rules cannot declare instances.")
                if self.schema_version == 1 and rule.managed_set_id is not None:
                    raise ValueError("Schema-v1 authz policy rules cannot declare managed IDs.")
                if self.schema_version != 2 or not rule.actions:
                    continue
                requested_actions = set(rule.actions)
                if requested_actions & exclusively_instance_actions and not rule.instances:
                    raise ValueError(
                        "Schema-v2 instance-scoped authz policy rules require instances."
                    )
                if rule.instances and requested_actions - instance_actions:
                    raise ValueError(
                        "Schema-v2 authz policy rules can only declare instances for "
                        "instance-scoped actions."
                    )
        return self

    @model_serializer(mode="wrap")
    def _serialize_policy(self, handler: SerializerFunctionWrapHandler) -> dict[str, Any]:
        payload = cast(dict[str, Any], handler(self))
        for rule in payload.get("github_actions", ()):
            if not rule.get("repository_id"):
                rule.pop("repository_id", None)
            if not rule.get("repository_owner_id"):
                rule.pop("repository_owner_id", None)
        for rule in payload.get("github_humans", ()):
            if not rule.get("github_ids"):
                rule.pop("github_ids", None)
        if self.administrator_quorum is None:
            payload.pop("administrator_quorum", None)
        if self.schema_version == 1:
            for rule_collection_name in (
                "github_actions",
                "github_humans",
                "terminal_agents",
                "local_operators",
                "local_admins",
            ):
                for rule in payload.get(rule_collection_name, ()):
                    rule.pop("instances", None)
                    rule.pop("managed_set_id", None)
                    rule.pop("managed_rule_id", None)
        return payload

    def evaluate(
        self,
        *,
        identity: LaunchplaneIdentity,
        action: str,
        product: str,
        context: str,
        target: AuthorizationTarget | None = None,
        record_context: bool = True,
    ) -> AuthzEvaluation:
        resolved_target = target or AuthorizationTarget(scope="context")
        if self.schema_version != 2 and action in exact_instance_workflow_authz_actions():
            return _record_authz_evaluation(
                identity=identity,
                action=action,
                product=product,
                context=context,
                target=resolved_target,
                allowed=False,
                reason_code="authz_policy_schema_incompatible",
                record_context=record_context,
            )
        if (
            self.schema_version == 2
            and action in exclusively_instance_scoped_authz_actions()
            and resolved_target.scope != "instance"
        ):
            return _record_authz_evaluation(
                identity=identity,
                action=action,
                product=product,
                context=context,
                target=resolved_target,
                allowed=False,
                reason_code="instance_scope_required",
                record_context=record_context,
            )
        if isinstance(identity, GitHubHumanIdentity):
            if identity.role == "read_only" and not limited_remote_user_action_allowed(action):
                return _record_authz_evaluation(
                    identity=identity,
                    action=action,
                    product=product,
                    context=context,
                    target=resolved_target,
                    allowed=False,
                    reason_code="principal_role_restricted",
                    record_context=record_context,
                )
            allowed = any(
                rule.allows(
                    identity=identity,
                    action=action,
                    product=product,
                    context=context,
                    target=resolved_target,
                    schema_version=self.schema_version,
                )
                for rule in self.github_humans
            )
        elif isinstance(identity, TerminalAgentIdentity):
            allowed = any(
                rule.allows(
                    identity=identity,
                    action=action,
                    product=product,
                    context=context,
                    target=resolved_target,
                    schema_version=self.schema_version,
                )
                for rule in self.terminal_agents
            )
        elif isinstance(identity, LocalOperatorIdentity):
            if not local_operator_identity_valid(identity=identity, action=action):
                return _record_authz_evaluation(
                    identity=identity,
                    action=action,
                    product=product,
                    context=context,
                    target=resolved_target,
                    allowed=False,
                    reason_code="principal_binding_invalid",
                    record_context=record_context,
                )
            allowed = any(
                rule.allows(
                    identity=identity,
                    action=action,
                    product=product,
                    context=context,
                    target=resolved_target,
                    schema_version=self.schema_version,
                )
                for rule in self.local_operators
            )
        elif isinstance(identity, LocalAdminIdentity):
            if not local_admin_identity_valid(identity=identity, action=action):
                return _record_authz_evaluation(
                    identity=identity,
                    action=action,
                    product=product,
                    context=context,
                    target=resolved_target,
                    allowed=False,
                    reason_code="principal_binding_invalid",
                    record_context=record_context,
                )
            allowed = any(
                rule.allows(
                    identity=identity,
                    action=action,
                    product=product,
                    context=context,
                    target=resolved_target,
                    schema_version=self.schema_version,
                )
                for rule in self.local_admins
            )
        else:
            allowed = any(
                rule.allows(
                    identity=identity,
                    action=action,
                    product=product,
                    context=context,
                    target=resolved_target,
                    schema_version=self.schema_version,
                )
                for rule in self.github_actions
            )
        return _record_authz_evaluation(
            identity=identity,
            action=action,
            product=product,
            context=context,
            target=resolved_target,
            allowed=allowed,
            reason_code="allowed" if allowed else "no_matching_grant",
            record_context=record_context,
        )

    def allows(
        self,
        *,
        identity: LaunchplaneIdentity,
        action: str,
        product: str,
        context: str,
        target: AuthorizationTarget | None = None,
    ) -> bool:
        return (
            self.evaluate(
                identity=identity,
                action=action,
                product=product,
                context=context,
                target=target,
            ).decision
            == "allowed"
        )

    def allows_product_instance_preflight(
        self,
        *,
        identity: LaunchplaneIdentity,
        action: str,
        product: str,
        instance: str,
    ) -> bool:
        """Authorize a product/instance request before resolving its private context.

        Callers must re-check the resolved context before performing a mutation.
        """
        policy = self.model_copy(deep=True)
        for collection_name in (
            "github_actions",
            "github_humans",
            "terminal_agents",
            "local_operators",
            "local_admins",
        ):
            for rule in getattr(policy, collection_name):
                rule.contexts = ()
        return policy.allows(
            identity=identity,
            action=action,
            product=product,
            context="",
            target=AuthorizationTarget(scope="instance", instances=(instance,)),
        )

    def human_role_for(
        self,
        *,
        github_id: int = 0,
        login: str,
        organizations: frozenset[str],
        teams: frozenset[str],
    ) -> Literal["read_only", "admin"] | None:
        if any(
            rule.matches_principal(
                github_id=github_id,
                login=login,
                organizations=organizations,
                teams=teams,
                role="admin",
            )
            for rule in self.github_humans
        ):
            return "admin"
        if any(
            rule.matches_principal(
                github_id=github_id,
                login=login,
                organizations=organizations,
                teams=teams,
                role="read_only",
            )
            for rule in self.github_humans
        ):
            return "read_only"
        return None


def matching_github_human_policy_rules(
    *,
    policy: LaunchplaneAuthzPolicy,
    identity: GitHubHumanIdentity,
    action: str,
    product: str,
    context: str,
    target: AuthorizationTarget | None = None,
    managed_only: bool = False,
) -> tuple[GitHubHumanPolicyRule, ...]:
    resolved_target = target or AuthorizationTarget(scope="context")
    if identity.role == "read_only" and not limited_remote_user_action_allowed(action):
        return ()
    return tuple(
        rule
        for rule in policy.github_humans
        if (not managed_only or rule.managed_set_id is not None)
        and (not managed_only or rule.managed_rule_id is not None)
        and rule.allows(
            identity=identity,
            action=action,
            product=product,
            context=context,
            target=resolved_target,
            schema_version=policy.schema_version,
        )
    )


def migrate_authz_policy_to_schema_v2(
    policy: LaunchplaneAuthzPolicy,
) -> LaunchplaneAuthzPolicy:
    if policy.schema_version == 2:
        return policy

    exclusively_instance_actions = exclusively_instance_scoped_authz_actions()
    instance_actions = instance_scoped_authz_actions()

    def migrated_rules(rule: Any) -> tuple[Any, ...]:
        if not rule.actions:
            return (
                rule.model_copy(update={"instances": ()}),
                rule.model_copy(update={"instances": ("*",)}),
            )
        context_actions = tuple(
            action for action in rule.actions if action not in exclusively_instance_actions
        )
        scoped_instance_actions = tuple(
            action for action in rule.actions if action in instance_actions
        )
        migrated: list[Any] = []
        if context_actions:
            migrated.append(rule.model_copy(update={"actions": context_actions, "instances": ()}))
        if scoped_instance_actions:
            migrated.append(
                rule.model_copy(update={"actions": scoped_instance_actions, "instances": ("*",)})
            )
        return tuple(migrated)

    return LaunchplaneAuthzPolicy(
        schema_version=2,
        administrator_quorum=policy.administrator_quorum,
        github_actions=tuple(
            migrated_rule
            for rule in policy.github_actions
            for migrated_rule in migrated_rules(rule)
        ),
        github_humans=tuple(
            migrated_rule for rule in policy.github_humans for migrated_rule in migrated_rules(rule)
        ),
        terminal_agents=tuple(
            migrated_rule
            for rule in policy.terminal_agents
            for migrated_rule in migrated_rules(rule)
        ),
        local_operators=tuple(
            migrated_rule
            for rule in policy.local_operators
            for migrated_rule in migrated_rules(rule)
        ),
        local_admins=tuple(
            migrated_rule for rule in policy.local_admins for migrated_rule in migrated_rules(rule)
        ),
    )


def effective_administrator_quorum(policy: LaunchplaneAuthzPolicy) -> int:
    return policy.administrator_quorum or 2


def parse_authz_policy_toml(policy_toml: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(tomllib.loads(policy_toml))


def load_authz_policy(policy_file: Path) -> LaunchplaneAuthzPolicy:
    return parse_authz_policy_toml(policy_file.read_text(encoding="utf-8"))
