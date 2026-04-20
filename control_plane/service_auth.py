from __future__ import annotations

import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

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
        self._jwk_client = jwk_client or jwt.PyJWKClient(jwks_url)

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

    def allows(self, *, identity: GitHubActionsIdentity, action: str, product: str, context: str) -> bool:
        if self.repository.strip() != identity.repository:
            return False
        if self.workflow_refs and identity.workflow_ref not in self.workflow_refs:
            return False
        if self.job_workflow_refs and identity.job_workflow_ref not in self.job_workflow_refs:
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


class HarborAuthzPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    github_actions: tuple[GitHubActionsPolicyRule, ...] = ()

    def allows(self, *, identity: GitHubActionsIdentity, action: str, product: str, context: str) -> bool:
        return any(
            rule.allows(identity=identity, action=action, product=product, context=context)
            for rule in self.github_actions
        )


def load_authz_policy(policy_file: Path) -> HarborAuthzPolicy:
    with policy_file.open("rb") as handle:
        payload = tomllib.load(handle)
    return HarborAuthzPolicy.model_validate(payload)
