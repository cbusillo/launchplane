from __future__ import annotations

from collections import Counter
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.authz_grant_service import (
    AuthzManagedPolicyReconcileEnvelope,
    authz_policy_allows_immutable_github_id_administration,
    strict_immutable_github_human_administrator_ids,
)
from control_plane.authz_policy_activation import (
    AUTHZ_POLICY_OPERATION_ACTIVATION_ACTIONS,
    AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_SET_ID,
    authz_policy_operation_activation_github_id,
    authz_policy_operation_activation_state,
    build_authz_policy_operation_activation_reconcile_request,
)
from control_plane.contracts.privileged_operation import (
    AUTHZ_POLICY_OPERATION_APPROVE_ACTION,
    AUTHZ_POLICY_OPERATION_CANCEL_ACTION,
    AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
    AUTHZ_POLICY_OPERATION_READ_ACTION,
    AUTHZ_POLICY_OPERATION_REVOKE_ACTION,
)
from control_plane.service_auth import (
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
    TerminalAgentPolicyRule,
    effective_administrator_quorum,
)


AUTHZ_POLICY_RECOVERY_ROUTE_PREFIX = "/v1/authz-policies/privileged-policy-operations/recovery"
AUTHZ_POLICY_RECOVERY_BOOTSTRAP_MANAGED_SET_ID = "operator.privileged-operation-bootstrap"
AUTHZ_POLICY_RECOVERY_BOOTSTRAP_HUMAN_RULE_ID = "github-owner-policy-operation-review"
AUTHZ_POLICY_RECOVERY_BOOTSTRAP_TERMINAL_RULE_ID = "terminal-agent-policy-operation-propose"
AUTHZ_POLICY_RECOVERY_SOURCE = "service:authz-policy-operation-recovery"
AUTHZ_POLICY_RECOVERY_RELATED_ISSUE = "#2277"
AUTHZ_POLICY_RECOVERY_CONFIRMATION_ACKNOWLEDGEMENT = "I confirm this exact code-defined recovery changes Launchplane policy administration under quorum one."

AuthzPolicyRecoveryCandidateId = Literal[
    "reset-unconfirmed-privileged-policy-operation-activation",
    "activate-privileged-policy-operation",
    "retire-privileged-operation-bootstrap",
]

_RECOVERY_ACTIONS = frozenset(AUTHZ_POLICY_OPERATION_ACTIVATION_ACTIONS)
_BOOTSTRAP_HUMAN_ACTIONS = frozenset(
    {
        AUTHZ_POLICY_OPERATION_READ_ACTION,
        AUTHZ_POLICY_OPERATION_CANCEL_ACTION,
        AUTHZ_POLICY_OPERATION_APPROVE_ACTION,
        AUTHZ_POLICY_OPERATION_REVOKE_ACTION,
    }
)


class AuthzPolicyRecoveryCandidateRequest(BaseModel):
    """Closed recovery-candidate request; callers cannot submit policy bodies."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: AuthzPolicyRecoveryCandidateId
    reason: str = Field(min_length=1, max_length=1000)

    @field_validator("reason")
    @classmethod
    def _normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Recovery candidate requires a reason.")
        return normalized


class AuthzPolicyRecoveryCandidateApplyRequest(AuthzPolicyRecoveryCandidateRequest):
    reviewed_plan_sha256: str = Field(min_length=64, max_length=64)
    solo_administration_confirmation_id: str = Field(min_length=1, max_length=160)

    @field_validator("reviewed_plan_sha256")
    @classmethod
    def _normalize_plan_digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("Recovery candidate apply requires a reviewed dry-run digest.")
        return normalized

    @field_validator("solo_administration_confirmation_id")
    @classmethod
    def _normalize_confirmation_id(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError(
                "Recovery candidate apply requires a solo-administration confirmation."
            )
        return normalized


class AuthzPolicyRecoveryConfirmationIssueRequest(BaseModel):
    """Closed-form confirmation issuance for a code-defined recovery candidate."""

    model_config = ConfigDict(extra="forbid")

    candidate_id: AuthzPolicyRecoveryCandidateId
    reason: str = Field(min_length=1, max_length=1000)
    reviewed_plan_sha256: str = Field(min_length=64, max_length=64)
    acknowledgement: str

    @field_validator("reason")
    @classmethod
    def _normalize_reason(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("Recovery confirmation requires a reason.")
        return normalized

    @field_validator("reviewed_plan_sha256")
    @classmethod
    def _normalize_plan_digest(cls, value: str) -> str:
        normalized = value.strip().lower()
        if len(normalized) != 64 or any(
            character not in "0123456789abcdef" for character in normalized
        ):
            raise ValueError("Recovery confirmation requires a reviewed dry-run digest.")
        return normalized

    @field_validator("acknowledgement")
    @classmethod
    def _require_truthful_acknowledgement(cls, value: str) -> str:
        if value != AUTHZ_POLICY_RECOVERY_CONFIRMATION_ACKNOWLEDGEMENT:
            raise ValueError(
                "Recovery confirmation acknowledgement text does not match the warning."
            )
        return value


def recovery_candidate_idempotency_scope(
    *, candidate_id: AuthzPolicyRecoveryCandidateId, github_id: int
) -> str:
    if github_id < 1:
        raise ValueError("Recovery candidate requires an immutable GitHub ID.")
    return f"authz-policy-recovery:{candidate_id}:{github_id}"


def _bootstrap_rule_entries(policy: LaunchplaneAuthzPolicy) -> tuple[tuple[str, object], ...]:
    return tuple(
        (principal_type, rule)
        for principal_type, rules in (
            ("github_humans", policy.github_humans),
            ("terminal_agents", policy.terminal_agents),
            ("github_actions", policy.github_actions),
            ("local_operators", policy.local_operators),
            ("local_admins", policy.local_admins),
        )
        for rule in rules
        if getattr(rule, "managed_set_id", None) == AUTHZ_POLICY_RECOVERY_BOOTSTRAP_MANAGED_SET_ID
    )


def _validate_bootstrap_human_rule(*, rule: object, github_id: int) -> GitHubHumanPolicyRule:
    if not isinstance(rule, GitHubHumanPolicyRule):
        raise ValueError("Recovery bootstrap set has an unexpected GitHub-human rule shape.")
    if (
        rule.managed_rule_id != AUTHZ_POLICY_RECOVERY_BOOTSTRAP_HUMAN_RULE_ID
        or rule.github_ids != (github_id,)
        or rule.roles != ("admin",)
        or rule.products != ("launchplane",)
        or rule.contexts != ("launchplane",)
        or rule.logins
        or rule.organizations
        or rule.teams
        or rule.instances
        or frozenset(rule.actions).intersection(_RECOVERY_ACTIONS) != _BOOTSTRAP_HUMAN_ACTIONS
    ):
        raise ValueError("Recovery bootstrap human rule does not match the closed bridge contract.")
    return rule


def _validate_bootstrap_terminal_rule(*, rule: object) -> TerminalAgentPolicyRule:
    if not isinstance(rule, TerminalAgentPolicyRule):
        raise ValueError("Recovery bootstrap set has an unexpected terminal-agent rule shape.")
    if (
        rule.managed_rule_id != AUTHZ_POLICY_RECOVERY_BOOTSTRAP_TERMINAL_RULE_ID
        or len(rule.subjects) != 1
        or len(rule.token_labels) != 1
        or rule.products != ("launchplane",)
        or rule.contexts != ("launchplane",)
        or rule.instances
        or rule.actions != (AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,)
    ):
        raise ValueError(
            "Recovery bootstrap terminal rule does not match the closed bridge contract."
        )
    return rule


def _bootstrap_retirement_desired_policy(
    *, policy: LaunchplaneAuthzPolicy, github_id: int
) -> LaunchplaneAuthzPolicy:
    entries = _bootstrap_rule_entries(policy)
    if len(entries) != 2:
        raise ValueError(
            "Recovery bootstrap set must contain exactly the temporary human and terminal rules."
        )
    expected_human = False
    expected_terminal = False
    retained_human_rule: GitHubHumanPolicyRule | None = None
    for principal_type, rule in entries:
        if principal_type == "github_humans":
            human_rule = _validate_bootstrap_human_rule(rule=rule, github_id=github_id)
            residual_actions = tuple(
                action for action in human_rule.actions if action not in _RECOVERY_ACTIONS
            )
            if residual_actions:
                retained_human_rule = human_rule.model_copy(update={"actions": residual_actions})
            expected_human = True
        elif principal_type == "terminal_agents":
            _validate_bootstrap_terminal_rule(rule=rule)
            expected_terminal = True
        else:
            raise ValueError("Recovery bootstrap set contains an unsupported principal type.")
    if not (expected_human and expected_terminal):
        raise ValueError(
            "Recovery bootstrap set does not contain the expected temporary bridge rules."
        )
    return LaunchplaneAuthzPolicy(
        schema_version=2,
        github_humans=(retained_human_rule,) if retained_human_rule is not None else (),
    )


def build_authz_policy_recovery_candidate_reconcile_request(
    *,
    policy: LaunchplaneAuthzPolicy,
    github_id: int,
    candidate_id: AuthzPolicyRecoveryCandidateId,
    mode: Literal["dry_run", "apply"],
    reason: str,
    reviewed_plan_sha256: str = "",
) -> AuthzManagedPolicyReconcileEnvelope:
    """Compile one audited recovery candidate without accepting raw policy input."""

    if github_id < 1:
        raise ValueError("Recovery candidate requires an immutable GitHub ID.")
    activation_state = authz_policy_operation_activation_state(policy)
    if candidate_id == "activate-privileged-policy-operation":
        if activation_state != "available":
            raise ValueError("Fresh activation requires an empty activation managed set.")
        return build_authz_policy_operation_activation_reconcile_request(
            github_id=github_id,
            mode=mode,
            reason=reason,
            reviewed_plan_sha256=reviewed_plan_sha256,
        )
    if candidate_id == "reset-unconfirmed-privileged-policy-operation-activation":
        if activation_state != "active":
            raise ValueError("Activation reset requires the exact active activation managed set.")
        desired_policy = LaunchplaneAuthzPolicy(schema_version=2)
        managed_set_id = AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_SET_ID
    elif candidate_id == "retire-privileged-operation-bootstrap":
        if (
            activation_state != "active"
            or authz_policy_operation_activation_github_id(policy) != github_id
        ):
            raise ValueError(
                "Bootstrap retirement requires the exact active activation set for the caller."
            )
        desired_policy = _bootstrap_retirement_desired_policy(policy=policy, github_id=github_id)
        managed_set_id = AUTHZ_POLICY_RECOVERY_BOOTSTRAP_MANAGED_SET_ID
    else:
        raise ValueError("Recovery candidate is not recognized.")
    return AuthzManagedPolicyReconcileEnvelope(
        product="launchplane",
        mode=mode,
        managed_set_id=managed_set_id,
        reason=reason,
        related_issue=AUTHZ_POLICY_RECOVERY_RELATED_ISSUE,
        reviewed_plan_sha256=reviewed_plan_sha256,
        desired_policy=desired_policy,
    )


def recovery_candidate_requires_solo_confirmation(policy: LaunchplaneAuthzPolicy) -> bool:
    return effective_administrator_quorum(policy) == 1


def recovery_candidate_is_safe(
    *,
    candidate_id: AuthzPolicyRecoveryCandidateId,
    policy: LaunchplaneAuthzPolicy,
    github_id: int,
) -> bool:
    if not authz_policy_allows_immutable_github_id_administration(
        policy=policy, github_id=github_id
    ):
        return False
    if (
        recovery_candidate_requires_solo_confirmation(policy)
        and len(strict_immutable_github_human_administrator_ids(policy)) != 1
    ):
        return False
    activation_state = authz_policy_operation_activation_state(policy)
    if candidate_id == "activate-privileged-policy-operation":
        return (
            activation_state == "active"
            and authz_policy_operation_activation_github_id(policy) == github_id
        )
    if candidate_id == "reset-unconfirmed-privileged-policy-operation-activation":
        return activation_state == "available"
    if candidate_id == "retire-privileged-operation-bootstrap":
        cardinality = recovery_action_match_cardinality(policy=policy, github_id=github_id)
        return (
            activation_state == "active"
            and all(value == 1 for value in cardinality["activation"].values())
            and all(value == 0 for value in cardinality["bootstrap"].values())
        )
    return False


def recovery_action_match_cardinality(
    *, policy: LaunchplaneAuthzPolicy, github_id: int
) -> dict[str, dict[str, int]]:
    """Return bounded action cardinalities only; never expose selectors or rule bodies."""

    activation_counts: Counter[str] = Counter()
    bootstrap_counts: Counter[str] = Counter()
    for human_rule in policy.github_humans:
        if human_rule.github_ids != (github_id,):
            continue
        if human_rule.managed_set_id == AUTHZ_POLICY_OPERATION_ACTIVATION_MANAGED_SET_ID:
            for action in _RECOVERY_ACTIONS.intersection(human_rule.actions):
                activation_counts[action] += 1
        if human_rule.managed_set_id == AUTHZ_POLICY_RECOVERY_BOOTSTRAP_MANAGED_SET_ID:
            for action in _RECOVERY_ACTIONS.intersection(human_rule.actions):
                bootstrap_counts[action] += 1
    for terminal_rule in policy.terminal_agents:
        if terminal_rule.managed_set_id != AUTHZ_POLICY_RECOVERY_BOOTSTRAP_MANAGED_SET_ID:
            continue
        for action in _RECOVERY_ACTIONS.intersection(terminal_rule.actions):
            bootstrap_counts[action] += 1
    return {
        "activation": {
            action: activation_counts[action]
            for action in AUTHZ_POLICY_OPERATION_ACTIVATION_ACTIONS
        },
        "bootstrap": {
            action: bootstrap_counts[action] for action in AUTHZ_POLICY_OPERATION_ACTIVATION_ACTIONS
        },
    }
