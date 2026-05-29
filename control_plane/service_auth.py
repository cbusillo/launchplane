from __future__ import annotations

from fnmatch import fnmatchcase
import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol

import jwt
from pydantic import BaseModel, ConfigDict, Field


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


LaunchplaneIdentity = (
    GitHubActionsIdentity | GitHubHumanIdentity | TerminalAgentIdentity | LocalOperatorIdentity
)
AgentConsumerSubjectType = Literal[
    "github_actions", "github_human", "terminal_agent", "local_operator"
]
AgentConsumerAccessProfile = Literal[
    "automation_worker",
    "human_admin",
    "limited_remote_user",
    "owner_local_agent",
]
AgentConsumerActionSafety = Literal[
    "read",
    "safe_write",
    "mutation",
    "prod",
    "destructive",
    "secret_backed",
    "policy_admin",
]
AgentAuthzDecision = Literal["allowed", "denied"]
LOCAL_OPERATOR_ALLOWED_ACTIONS = frozenset(
    {
        "merge_train.policy_targets",
        "public_ingress_monitor.run_once",
        "public_ingress_notification_policy.apply",
        "product_config.plan",
        "product_config.apply",
        "work_graph.issue_inbox.reconcile",
    }
)


class TokenVerifier(Protocol):
    def verify(self, token: str) -> GitHubActionsIdentity: ...


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
        workflow_ref = str(claims.get("workflow_ref", "")).strip()
        if not repository:
            raise ValueError("OIDC token is missing repository claim.")
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
        )


class GitHubActionsPolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    workflow_refs: tuple[str, ...] = ()
    job_workflow_refs: tuple[str, ...] = ()
    event_names: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()

    @staticmethod
    def _matches_claim(value: str, allowed_values: tuple[str, ...]) -> bool:
        normalized_value = value.strip()
        return any(fnmatchcase(normalized_value, allowed_value) for allowed_value in allowed_values)

    def allows(
        self, *, identity: GitHubActionsIdentity, action: str, product: str, context: str
    ) -> bool:
        if self.repository.strip() != identity.repository:
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
        if self.products and product not in self.products:
            return False
        if self.contexts and context not in self.contexts:
            return False
        if self.actions and action not in self.actions:
            return False
        return True


class GitHubHumanPolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logins: tuple[str, ...] = ()
    organizations: tuple[str, ...] = ()
    teams: tuple[str, ...] = ()
    roles: tuple[Literal["read_only", "admin"], ...] = ()
    products: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()

    @staticmethod
    def _matches_any(value: str, allowed_values: tuple[str, ...]) -> bool:
        normalized_value = value.strip()
        return any(fnmatchcase(normalized_value, allowed_value) for allowed_value in allowed_values)

    @staticmethod
    def _intersects(values: frozenset[str], allowed_values: tuple[str, ...]) -> bool:
        return any(
            fnmatchcase(value.strip(), allowed_value)
            for value in values
            for allowed_value in allowed_values
        )

    def allows(
        self, *, identity: GitHubHumanIdentity, action: str, product: str, context: str
    ) -> bool:
        if self.logins and not self._matches_any(identity.login, self.logins):
            return False
        if self.organizations and not self._intersects(identity.organizations, self.organizations):
            return False
        if self.teams and not self._intersects(identity.teams, self.teams):
            return False
        if self.roles and identity.role not in self.roles:
            return False
        if self.products and product not in self.products:
            return False
        if self.contexts and context not in self.contexts:
            return False
        if self.actions and action not in self.actions:
            return False
        return True

    def matches_principal(
        self,
        *,
        login: str,
        organizations: frozenset[str],
        teams: frozenset[str],
        role: Literal["read_only", "admin"],
    ) -> bool:
        if self.logins and not self._matches_any(login, self.logins):
            return False
        if self.organizations and not self._intersects(organizations, self.organizations):
            return False
        if self.teams and not self._intersects(teams, self.teams):
            return False
        if self.roles and role not in self.roles:
            return False
        return True


class TerminalAgentPolicyRule(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subjects: tuple[str, ...] = ()
    token_labels: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()
    actions: tuple[str, ...] = ()

    @staticmethod
    def _matches_any(value: str, allowed_values: tuple[str, ...]) -> bool:
        normalized_value = value.strip()
        return any(fnmatchcase(normalized_value, allowed_value) for allowed_value in allowed_values)

    def allows(
        self, *, identity: TerminalAgentIdentity, action: str, product: str, context: str
    ) -> bool:
        if self.subjects and not self._matches_any(identity.subject, self.subjects):
            return False
        if self.token_labels and not self._matches_any(identity.token_label, self.token_labels):
            return False
        if self.products and product not in self.products:
            return False
        if self.contexts and context not in self.contexts:
            return False
        if self.actions and action not in self.actions:
            return False
        return True


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
    if normalized_action.startswith("authz_policy"):
        return "policy_admin"
    if "secret" in action_parts or normalized_action.endswith(".secret"):
        return "secret_backed"
    if any(part in action_parts for part in ("destroy", "cleanup", "delete", "rollback")):
        return "destructive"
    if "prod" in action_parts or "promotion" in action_parts:
        return "prod"
    if normalized_action.endswith(".read") or normalized_action == "work_graph.rank":
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


def local_operator_action_allowed(*, identity: LocalOperatorIdentity, action: str) -> bool:
    return (
        bool(identity.subject.strip())
        and bool(identity.token_label.strip())
        and action.strip() in LOCAL_OPERATOR_ALLOWED_ACTIONS
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
            approval_capable=False,
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

    schema_version: int = Field(default=1, ge=1)
    github_actions: tuple[GitHubActionsPolicyRule, ...] = ()
    github_humans: tuple[GitHubHumanPolicyRule, ...] = ()
    terminal_agents: tuple[TerminalAgentPolicyRule, ...] = ()

    def allows(
        self, *, identity: LaunchplaneIdentity, action: str, product: str, context: str
    ) -> bool:
        if isinstance(identity, GitHubHumanIdentity):
            if identity.role == "read_only" and not limited_remote_user_action_allowed(action):
                return False
            return any(
                rule.allows(identity=identity, action=action, product=product, context=context)
                for rule in self.github_humans
            )
        if isinstance(identity, TerminalAgentIdentity):
            return any(
                rule.allows(identity=identity, action=action, product=product, context=context)
                for rule in self.terminal_agents
            )
        if isinstance(identity, LocalOperatorIdentity):
            return local_operator_action_allowed(identity=identity, action=action)
        return any(
            rule.allows(identity=identity, action=action, product=product, context=context)
            for rule in self.github_actions
        )

    def human_role_for(
        self,
        *,
        login: str,
        organizations: frozenset[str],
        teams: frozenset[str],
    ) -> Literal["read_only", "admin"] | None:
        if any(
            rule.matches_principal(
                login=login, organizations=organizations, teams=teams, role="admin"
            )
            for rule in self.github_humans
        ):
            return "admin"
        if any(
            rule.matches_principal(
                login=login, organizations=organizations, teams=teams, role="read_only"
            )
            for rule in self.github_humans
        ):
            return "read_only"
        return None


def parse_authz_policy_toml(policy_toml: str) -> LaunchplaneAuthzPolicy:
    return LaunchplaneAuthzPolicy.model_validate(tomllib.loads(policy_toml))


def load_authz_policy(policy_file: Path) -> LaunchplaneAuthzPolicy:
    return parse_authz_policy_toml(policy_file.read_text(encoding="utf-8"))
