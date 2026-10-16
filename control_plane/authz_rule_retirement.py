"""Selected workflow rule inspection and inert, server-derived contraction plans."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.authz_grant_service import (
    _authz_rule_sha256,
    authz_policy_retains_reachable_github_id_administration,
)
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
)
from control_plane.runtime_setting_removal import request_fingerprint
from control_plane.service_auth import (
    GitHubActionsPolicyRule,
    LaunchplaneAuthzPolicy,
    effective_administrator_quorum,
    strict_immutable_github_human_administrator_ids,
)

ROUTE = "/v1/authz-policies/workflow-rule-retirement-plan"


class WorkflowRuleSelection(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    rule_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    managed_set_id: str | None = Field(default=None, max_length=256)
    managed_rule_id: str | None = Field(default=None, max_length=256)


class WorkflowRuleRetirementRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    policy_record_id: str = Field(min_length=1, max_length=256)
    policy_revision: int = Field(ge=1)
    policy_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    selection: tuple[WorkflowRuleSelection, ...] = Field(min_length=1, max_length=50)
    mode: Literal["inspect", "plan"] = "inspect"

    @model_validator(mode="after")
    def validate_unique(self) -> "WorkflowRuleRetirementRequest":
        if len({s.rule_sha256 for s in self.selection}) != len(self.selection):
            raise ValueError("Select unique workflow-rule fingerprints.")
        return self


def shared_scope(rule: GitHubActionsPolicyRule) -> bool:
    selectors = (
        rule.workflow_refs,
        rule.job_workflow_refs,
        rule.products,
        rule.contexts,
        rule.instances,
        rule.refs,
        rule.event_names,
        rule.environments,
    )
    return (
        not (rule.workflow_refs or rule.job_workflow_refs)
        or not rule.products
        or not rule.contexts
        or not rule.actions
        or any(any(c in action for c in "*?[") for action in rule.actions)
        or any(
            len(values) > 1 or any(any(c in value for c in "*?[") for value in values)
            for values in selectors
        )
    )


def workflow_rule_retirement_plan(
    active: LaunchplaneAuthzPolicyRecord, request: WorkflowRuleRetirementRequest
) -> dict[str, object]:
    if (active.record_id, active.revision, active.policy_sha256) != (
        request.policy_record_id,
        request.policy_revision,
        request.policy_sha256,
    ):
        raise ValueError("Active policy changed; refresh its revision and fingerprint.")
    selected: list[GitHubActionsPolicyRule] = []
    for selection in request.selection:
        matches = [
            rule
            for rule in active.policy.github_actions
            if _authz_rule_sha256(rule) == selection.rule_sha256
        ]
        if len(matches) != 1:
            raise ValueError("Selected rule is missing or ambiguous.")
        rule = matches[0]
        if (rule.managed_set_id, rule.managed_rule_id) != (
            selection.managed_set_id,
            selection.managed_rule_id,
        ):
            raise ValueError("Selected managed identity does not match its fingerprint.")
        selected.append(rule)
    selected_hashes = {s.rule_sha256 for s in request.selection}
    survivors = tuple(
        r for r in active.policy.github_actions if _authz_rule_sha256(r) not in selected_hashes
    )
    candidate = LaunchplaneAuthzPolicy.model_validate(
        {
            **active.policy.model_dump(mode="json"),
            "github_actions": [r.model_dump(mode="json") for r in survivors],
        }
    )
    lockout = not authz_policy_retains_reachable_github_id_administration(policy=candidate) or (
        len(strict_immutable_github_human_administrator_ids(candidate))
        < effective_administrator_quorum(candidate)
    )
    result: dict[str, object] = {
        "policy_record_id": active.record_id,
        "policy_revision": active.revision,
        "policy_sha256": active.policy_sha256,
        "selected_rules": [
            {
                "rule_sha256": _authz_rule_sha256(r),
                "management": "managed" if r.managed_set_id else "unmanaged",
                "scope": r.model_dump(mode="json", exclude_none=True),
                "broad_or_shared_scope": shared_scope(r),
            }
            for r in selected
        ],
        "dependency_verification": "unknown_requires_source_review",
        "apply_supported": False,
        "blockers": ["last_administrator_protection"] if lockout else [],
    }
    if request.mode == "plan":
        result.update(
            candidate_policy_sha256=authz_policy_sha256(candidate),
            surviving_workflow_rule_count=len(survivors),
            survivor_rules_sha256=request_fingerprint(
                [r.model_dump(mode="json") for r in survivors]
            ),
            other_principal_collections_preserved=True,
        )
        result["plan_sha256"] = request_fingerprint(result)
    return result
