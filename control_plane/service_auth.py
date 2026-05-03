from __future__ import annotations

from fnmatch import fnmatchcase
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


LaunchplaneIdentity = GitHubActionsIdentity | GitHubHumanIdentity


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


class LaunchplaneAuthzPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    github_actions: tuple[GitHubActionsPolicyRule, ...] = ()
    github_humans: tuple[GitHubHumanPolicyRule, ...] = ()

    def allows(
        self, *, identity: LaunchplaneIdentity, action: str, product: str, context: str
    ) -> bool:
        if isinstance(identity, GitHubHumanIdentity):
            return any(
                rule.allows(identity=identity, action=action, product=product, context=context)
                for rule in self.github_humans
            )
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
