from __future__ import annotations

from datetime import datetime
from typing import Annotated, Literal, TypeAlias

from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.service_auth import LaunchplaneAuthzPolicy


AUTHZ_POLICY_SOURCE_ADMISSIBLE_STATUSES = frozenset(
    {"planned", "approved", "executing", "executed"}
)
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


class AuthzPolicySchemaV3OrdinaryEnableEvidence(_StrictFrozenModel):
    schema_version: Literal[1] = 1
    transition: Literal["ordinary_enable"] = "ordinary_enable"
    caller: AuthzPolicyImmutableHumanCallerBinding
    expected_record_id: str
    expected_revision: int = Field(gt=0)
    expected_policy_sha256: str
    candidate_policy_sha256: str
    activation_id: str
    activation_revision: int = Field(gt=0)
    activation_sha256: str
    source_setup_operation_id: str
    policy_operation_id: str
    policy_request_sha256: str
    policy_evidence_sha256: str
    policy_plan_sha256: str
    desired_set_sha256: str
    repository_id: int = Field(gt=0)
    repository: str
    base_branch: str
    managed_set_id: str
    managed_rule_id: str

    @field_validator(
        "expected_policy_sha256",
        "candidate_policy_sha256",
        "activation_sha256",
        "policy_request_sha256",
        "policy_evidence_sha256",
        "policy_plan_sha256",
        "desired_set_sha256",
    )
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("transition evidence requires canonical sha256 values")
        return normalized


AuthzPolicySchemaV3WriteEvidence: TypeAlias = Annotated[
    AuthzPolicySchemaV3MaintenanceEvidence | AuthzPolicySchemaV3OrdinaryEnableEvidence,
    Field(discriminator="transition"),
]


AuthzPolicySchemaTransitionKind: TypeAlias = Literal[
    "legacy", "v2_to_v3_enable", "v3_maintenance", "v3_enable_or_expand"
]


class AuthzPolicySchemaTransition(_StrictFrozenModel):
    kind: AuthzPolicySchemaTransitionKind
    added_or_changed_ordinary_rule_keys: tuple[str, ...] = ()
    removed_ordinary_rule_keys: tuple[str, ...] = ()


def _ordinary_rules(policy: LaunchplaneAuthzPolicy) -> dict[str, str]:
    result: dict[str, str] = {}
    for rule in policy.ordinary_agents:
        key = f"{rule.managed_set_id}\x1f{rule.managed_rule_id}"
        if key in result:
            raise AuthzPolicySchemaV3TransitionDeniedError("ordinary_rule_identity_ambiguous")
        result[key] = canonical_json_sha256(rule.model_dump(mode="json", exclude_none=True))
    return result


def classify_authz_policy_schema_v3_transition(
    current_policy: LaunchplaneAuthzPolicy,
    candidate_policy: LaunchplaneAuthzPolicy,
) -> AuthzPolicySchemaTransition:
    pair = (current_policy.schema_version, candidate_policy.schema_version)
    if pair in {(1, 1), (1, 2), (2, 2)}:
        return AuthzPolicySchemaTransition(kind="legacy")
    if pair not in {(2, 3), (3, 3)}:
        raise AuthzPolicySchemaV3TransitionDeniedError("schema_transition_unsupported")
    current = _ordinary_rules(current_policy)
    candidate = _ordinary_rules(candidate_policy)
    changed = tuple(sorted(key for key, digest in candidate.items() if current.get(key) != digest))
    removed = tuple(sorted(current.keys() - candidate.keys()))
    if pair == (2, 3):
        if len(changed) != 1 or current:
            raise AuthzPolicySchemaV3TransitionDeniedError("ordinary_enable_scope_not_singleton")
        kind: AuthzPolicySchemaTransitionKind = "v2_to_v3_enable"
    elif not changed:
        kind = "v3_maintenance"
    elif len(changed) == 1:
        kind = "v3_enable_or_expand"
    else:
        raise AuthzPolicySchemaV3TransitionDeniedError("ordinary_enable_scope_not_singleton")
    return AuthzPolicySchemaTransition(
        kind=kind,
        added_or_changed_ordinary_rule_keys=changed,
        removed_ordinary_rule_keys=removed,
    )


def require_authz_policy_source_status(
    *, status: str, expires_at: str, observed_at: datetime
) -> None:
    if status not in AUTHZ_POLICY_SOURCE_ADMISSIBLE_STATUSES:
        raise AuthzPolicySchemaV3TransitionDeniedError("policy_source_status_inadmissible")
    if status in {"planned", "approved"} and observed_at >= datetime.fromisoformat(expires_at):
        raise AuthzPolicySchemaV3TransitionDeniedError("policy_source_expired")
