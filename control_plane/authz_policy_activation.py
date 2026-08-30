from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.authz_grant_service import (
    AuthzManagedPolicyDiff,
    AuthzManagedPolicyReconcileEnvelope,
    authz_policy_allows_immutable_github_id_administration,
    authz_policy_retains_independent_github_id_administration,
)
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.privileged_operation import (
    AUTHZ_POLICY_OPERATION_APPROVE_ACTION,
    AUTHZ_POLICY_OPERATION_CANCEL_ACTION,
    AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
    AUTHZ_POLICY_OPERATION_READ_ACTION,
    AUTHZ_POLICY_OPERATION_REVOKE_ACTION,
)
from control_plane.service_auth import GitHubHumanPolicyRule, LaunchplaneAuthzPolicy


AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_SET_ID = "operator.privileged-policy-operation"
AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_RULE_ID = "github-human-policy-operator"
AUTHZ_POLICY_OPERATION_ACTIVATION_SOURCE = "service:authz-policy-operation-activation"
AUTHZ_POLICY_OPERATION_ACTIVATION_ACTIONS = (
    AUTHZ_POLICY_OPERATION_APPROVE_ACTION,
    AUTHZ_POLICY_OPERATION_CANCEL_ACTION,
    AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
    AUTHZ_POLICY_OPERATION_READ_ACTION,
    AUTHZ_POLICY_OPERATION_REVOKE_ACTION,
)
AuthzPolicyOperationActivationState = Literal["available", "active", "conflict"]


class AuthzPolicyOperationActivationDryRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Activation dry-run requires reason.")
        return normalized


class AuthzPolicyOperationActivationApplyRequest(AuthzPolicyOperationActivationDryRunRequest):
    reviewed_plan_sha256: str = Field(min_length=64, max_length=64)

    @field_validator("reviewed_plan_sha256")
    @classmethod
    def _normalize_reviewed_plan_sha256(cls, value: str) -> str:
        normalized = value.strip().lower()
        if re.fullmatch(r"[0-9a-f]{64}", normalized) is None:
            raise ValueError("Activation apply requires a reviewed dry-run digest.")
        return normalized


def build_authz_policy_operation_activation_reconcile_request(
    *,
    github_id: int,
    mode: Literal["dry_run", "apply"],
    reason: str,
    reviewed_plan_sha256: str = "",
) -> AuthzManagedPolicyReconcileEnvelope:
    if github_id < 1:
        raise ValueError("Activation requires an immutable GitHub ID.")
    desired_policy = LaunchplaneAuthzPolicy(
        schema_version=2,
        github_humans=(
            GitHubHumanPolicyRule(
                managed_set_id=AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_SET_ID,
                managed_rule_id=AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_RULE_ID,
                github_ids=(github_id,),
                roles=("admin",),
                products=("launchplane",),
                contexts=("launchplane",),
                actions=AUTHZ_POLICY_OPERATION_ACTIVATION_ACTIONS,
            ),
        ),
    )
    return AuthzManagedPolicyReconcileEnvelope(
        product="launchplane",
        mode=mode,
        managed_set_id=AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_SET_ID,
        reason=reason,
        related_issue="#2277",
        reviewed_plan_sha256=reviewed_plan_sha256,
        desired_policy=desired_policy,
    )


def authz_policy_operation_activation_state(
    policy: LaunchplaneAuthzPolicy,
) -> AuthzPolicyOperationActivationState:
    matching_rules = tuple(
        (principal_type, rule)
        for principal_type, rules in (
            ("github_actions", policy.github_actions),
            ("github_humans", policy.github_humans),
            ("terminal_agents", policy.terminal_agents),
            ("local_operators", policy.local_operators),
            ("local_admins", policy.local_admins),
        )
        for rule in rules
        if rule.managed_set_id == AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_SET_ID
    )
    if not matching_rules:
        return "available"
    if len(matching_rules) != 1:
        return "conflict"
    principal_type, rule = matching_rules[0]
    if (
        principal_type != "github_humans"
        or not isinstance(rule, GitHubHumanPolicyRule)
        or rule.managed_rule_id != AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_RULE_ID
        or len(rule.github_ids) != 1
        or rule.roles != ("admin",)
        or rule.products != ("launchplane",)
        or rule.contexts != ("launchplane",)
        or rule.actions != AUTHZ_POLICY_OPERATION_ACTIVATION_ACTIONS
        or rule.logins
        or rule.organizations
        or rule.teams
        or rule.instances
    ):
        return "conflict"
    return "active"


def authz_policy_operation_activation_github_id(
    policy: LaunchplaneAuthzPolicy,
) -> int:
    if authz_policy_operation_activation_state(policy) != "active":
        return 0
    rule = next(
        rule
        for rule in policy.github_humans
        if rule.managed_set_id == AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_SET_ID
    )
    return rule.github_ids[0]


def authz_policy_operation_activation_idempotency_scope(github_id: int) -> str:
    if github_id < 1:
        raise ValueError("Activation idempotency requires an immutable GitHub ID.")
    return f"github-human-id|{github_id}"


def authz_policy_operation_activation_evidence(
    *,
    mode: Literal["dry_run", "apply"],
    applying_github_id: int,
    previous_record: LaunchplaneAuthzPolicyRecord,
    resulting_record: LaunchplaneAuthzPolicyRecord,
    candidate_policy: LaunchplaneAuthzPolicy,
    diff: AuthzManagedPolicyDiff,
    changed: bool,
) -> dict[str, object]:
    return {
        "mode": mode,
        "bridge_state": "retired" if mode == "apply" else "available",
        "managed_set_id": AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_SET_ID,
        "managed_rule_id": AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_RULE_ID,
        "principal_type": "github_human",
        "actions": list(AUTHZ_POLICY_OPERATION_ACTIVATION_ACTIONS),
        "product": "launchplane",
        "context": "launchplane",
        "changed": changed,
        "observed_record_id": previous_record.record_id,
        "observed_revision": previous_record.revision,
        "observed_policy_sha256": previous_record.policy_sha256,
        "candidate_revision": diff.candidate_revision,
        "candidate_policy_sha256": diff.desired_policy_sha256,
        "desired_set_sha256": diff.desired_set_sha256,
        "review_digest": diff.plan_sha256,
        "applying_admin_retained": authz_policy_allows_immutable_github_id_administration(
            policy=candidate_policy,
            github_id=applying_github_id,
        ),
        "independent_admin_reachable": (
            authz_policy_retains_independent_github_id_administration(
                policy=candidate_policy,
                applying_github_id=applying_github_id,
            )
        ),
        "policy_safety_blockers": [
            blocker.model_dump(mode="json") for blocker in diff.policy_safety_blockers
        ],
        "resulting_policy": {
            "record_id": resulting_record.record_id,
            "revision": resulting_record.revision,
            "policy_sha256": resulting_record.policy_sha256,
            "activation_state": authz_policy_operation_activation_state(resulting_record.policy),
        },
    }
