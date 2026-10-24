from __future__ import annotations

from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.service_auth import LaunchplaneAuthzPolicy


AUTHZ_POLICY_SCHEMA_V3_TRANSITION_DENIED = "authz_policy_schema_v3_transition_denied"


class AuthzPolicySchemaV3TransitionDeniedError(ValueError):
    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(AUTHZ_POLICY_SCHEMA_V3_TRANSITION_DENIED)


class _StrictFrozenModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class AuthzPolicyImmutableHumanCallerBinding(_StrictFrozenModel):
    kind: Literal["immutable_github_human"] = "immutable_github_human"
    github_id: int = Field(gt=0)


class AuthzPolicyGitHubActionsCallerBinding(_StrictFrozenModel):
    kind: Literal["github_actions"] = "github_actions"
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


AuthzPolicySchemaV3CallerBinding: TypeAlias = Annotated[
    AuthzPolicyImmutableHumanCallerBinding | AuthzPolicyGitHubActionsCallerBinding,
    Field(discriminator="kind"),
]


class AuthzPolicySchemaV3MaintenanceEvidence(_StrictFrozenModel):
    schema_version: Literal[1] = 1
    transition: Literal["maintenance"] = "maintenance"
    caller: AuthzPolicySchemaV3CallerBinding
    expected_record_id: str
    expected_revision: int = Field(gt=0)
    expected_policy_sha256: str
    candidate_policy_sha256: str


AuthzPolicySchemaV3WriteEvidence: TypeAlias = AuthzPolicySchemaV3MaintenanceEvidence


AuthzPolicySchemaTransitionKind: TypeAlias = Literal["legacy", "v3_maintenance"]


class AuthzPolicySchemaTransition(_StrictFrozenModel):
    kind: AuthzPolicySchemaTransitionKind


def _ordinary_rules(policy: LaunchplaneAuthzPolicy) -> dict[str, str]:
    result: dict[str, str] = {}
    for rule in policy.ordinary_agents:
        key = f"{rule.managed_set_id}\x1f{rule.managed_rule_id}"
        if key in result:
            raise AuthzPolicySchemaV3TransitionDeniedError("ordinary_rule_identity_ambiguous")
        result[key] = canonical_json_sha256(rule.model_dump(mode="json", exclude_none=True))
    return result


def _adds_or_widens_ordinary_rules(
    current_policy: LaunchplaneAuthzPolicy, candidate_policy: LaunchplaneAuthzPolicy
) -> bool:
    current = _ordinary_rules(current_policy)
    return any(
        current.get(key) != digest for key, digest in _ordinary_rules(candidate_policy).items()
    )


def classify_authz_policy_schema_v3_transition(
    current_policy: LaunchplaneAuthzPolicy,
    candidate_policy: LaunchplaneAuthzPolicy,
) -> AuthzPolicySchemaTransition:
    pair = (current_policy.schema_version, candidate_policy.schema_version)
    if pair in {(1, 1), (1, 2), (2, 2)}:
        return AuthzPolicySchemaTransition(kind="legacy")
    if pair == (3, 3) and not _adds_or_widens_ordinary_rules(current_policy, candidate_policy):
        return AuthzPolicySchemaTransition(kind="v3_maintenance")
    if pair in {(2, 3), (3, 3)}:
        raise AuthzPolicySchemaV3TransitionDeniedError("ordinary_authority_retired")
    raise AuthzPolicySchemaV3TransitionDeniedError("schema_transition_unsupported")
