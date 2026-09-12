"""Closed authorization candidates compiled into standard managed-policy plans."""

from __future__ import annotations

from typing import Literal

from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationRecord,
)
from control_plane.contracts.privileged_operation import (
    ORDINARY_AGENT_DELIVERY_ACTIVATION_APPROVE_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_CANCEL_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_PLAN_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_READ_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_REVOKE_ACTION,
    ManagedAuthzPolicySetProposalInput,
)
from control_plane.service_auth import GitHubHumanPolicyRule, LaunchplaneAuthzPolicy


ORDINARY_AGENT_DELIVERY_ADMINISTRATION_CANDIDATE_ID = "ordinary-agent-delivery-administration"
ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID = (
    "operator.ordinary-agent-delivery-administration"
)
ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID = "pilot-administrator"
ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS = (
    ORDINARY_AGENT_DELIVERY_ACTIVATION_APPROVE_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_CANCEL_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_PLAN_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_READ_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_REVOKE_ACTION,
)
ORDINARY_AGENT_DELIVERY_ADMINISTRATION_REASON = (
    "Prepare bounded ordinary-agent delivery administration."
)
ORDINARY_AGENT_DELIVERY_ADMINISTRATION_RELATED_ISSUE = "#2369"

AuthorizationCandidateIntent = Literal["add", "remove"]
AuthorizationCandidateState = Literal["available", "active", "conflict"]


AuthorizationCandidatePreparationReason = Literal[
    "candidate_set_conflict",
    "candidate_action_overlap",
    "current_activation_requires_stop",
    "activation_storage_unavailable",
    "activation_history_truncated",
]


class AuthorizationCandidatePreparationError(ValueError):
    """The closed candidate cannot be safely compiled from current state."""

    def __init__(
        self,
        reason_code: AuthorizationCandidatePreparationReason,
        message: str,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def _rules(policy: LaunchplaneAuthzPolicy) -> tuple[tuple[str, object], ...]:
    return tuple(
        (principal_type, rule)
        for principal_type, rules in (
            ("github_actions", policy.github_actions),
            ("github_humans", policy.github_humans),
            ("terminal_agents", policy.terminal_agents),
            ("local_operators", policy.local_operators),
            ("local_admins", policy.local_admins),
            ("ordinary_agents", policy.ordinary_agents),
        )
        for rule in rules
    )


def ordinary_agent_delivery_administration_state(
    policy: LaunchplaneAuthzPolicy,
    *,
    github_id: int,
) -> AuthorizationCandidateState:
    if github_id < 1:
        return "conflict"
    managed_rules = tuple(
        (principal_type, rule)
        for principal_type, rule in _rules(policy)
        if getattr(rule, "managed_set_id", None)
        == ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID
    )
    if not managed_rules:
        return "available"
    if len(managed_rules) != 1:
        return "conflict"
    principal_type, rule = managed_rules[0]
    if (
        principal_type != "github_humans"
        or not isinstance(rule, GitHubHumanPolicyRule)
        or rule.managed_rule_id != ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID
        or rule.github_ids != (github_id,)
        or rule.roles != ("admin",)
        or rule.products != ("launchplane",)
        or rule.contexts != ("launchplane",)
        or rule.actions != ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS
        or rule.logins
        or rule.organizations
        or rule.teams
        or rule.instances
    ):
        return "conflict"
    return "active"


def ordinary_agent_delivery_administration_github_id(
    policy: LaunchplaneAuthzPolicy,
) -> int:
    candidates = tuple(
        rule.github_ids[0]
        for rule in policy.github_humans
        if rule.managed_set_id == ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID
        and len(rule.github_ids) == 1
    )
    if len(candidates) != 1:
        return 0
    github_id = candidates[0]
    return (
        github_id
        if ordinary_agent_delivery_administration_state(policy, github_id=github_id) == "active"
        else 0
    )


def _has_explicit_action_overlap(policy: LaunchplaneAuthzPolicy) -> bool:
    actions = set(ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS)
    return any(
        getattr(rule, "managed_set_id", None)
        != ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID
        and bool(actions.intersection(getattr(rule, "actions", ())))
        for _principal_type, rule in _rules(policy)
    )


def _require_no_current_activation(record_store: object) -> None:
    reader = getattr(record_store, "list_ordinary_agent_delivery_activation_records", None)
    if not callable(reader):
        raise AuthorizationCandidatePreparationError(
            "activation_storage_unavailable", "Removal requires ordinary-agent activation storage."
        )
    try:
        records = tuple(reader(limit=1001))
        if len(records) > 1000:
            raise AuthorizationCandidatePreparationError(
                "activation_history_truncated",
                "Removal cannot prove activation state within the bounded history read.",
            )
        activations = tuple(
            record
            if isinstance(record, OrdinaryAgentDeliveryActivationRecord)
            else OrdinaryAgentDeliveryActivationRecord.model_validate(record)
            for record in records
        )
    except AuthorizationCandidatePreparationError:
        raise
    except (RuntimeError, TypeError, ValueError) as error:
        raise AuthorizationCandidatePreparationError(
            "activation_storage_unavailable",
            "Removal could not verify ordinary-agent activation state.",
        ) from error
    if any(
        not activation.revoked_at and not activation.superseded_by_activation_id
        for activation in activations
    ):
        raise AuthorizationCandidatePreparationError(
            "current_activation_requires_stop",
            "Removal is blocked while an ordinary-agent delivery activation is current.",
        )


def compile_ordinary_agent_delivery_administration_candidate(
    *,
    current_policy: LaunchplaneAuthzPolicy,
    github_id: int,
    intent: AuthorizationCandidateIntent,
    record_store: object,
) -> tuple[Literal["planned", "already_satisfied"], ManagedAuthzPolicySetProposalInput | None]:
    state = ordinary_agent_delivery_administration_state(
        current_policy,
        github_id=github_id,
    )
    if state == "conflict":
        raise AuthorizationCandidatePreparationError(
            "candidate_set_conflict",
            "The authorization candidate conflicts with current policy state.",
        )
    if intent == "add" and _has_explicit_action_overlap(current_policy):
        raise AuthorizationCandidatePreparationError(
            "candidate_action_overlap",
            "The authorization candidate actions overlap another policy rule.",
        )
    desired_active = intent == "add"
    if (state == "active") == desired_active:
        return "already_satisfied", None
    if intent == "remove":
        _require_no_current_activation(record_store)
    desired_policy = LaunchplaneAuthzPolicy(
        schema_version=current_policy.schema_version,
        github_humans=(
            (
                GitHubHumanPolicyRule(
                    managed_set_id=(ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID),
                    managed_rule_id=(ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID),
                    github_ids=(github_id,),
                    roles=("admin",),
                    products=("launchplane",),
                    contexts=("launchplane",),
                    actions=ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS,
                ),
            )
            if intent == "add"
            else ()
        ),
    )
    return (
        "planned",
        ManagedAuthzPolicySetProposalInput(
            managed_set_id=ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID,
            desired_policy=desired_policy,
            schema_migration="reject",
            administrator_quorum_change=None,
            reason=ORDINARY_AGENT_DELIVERY_ADMINISTRATION_REASON,
            related_issue=ORDINARY_AGENT_DELIVERY_ADMINISTRATION_RELATED_ISSUE,
        ),
    )


def is_ordinary_agent_delivery_administration_request(
    request: ManagedAuthzPolicySetProposalInput,
) -> bool:
    """Recognize only the exact closed candidate for safe semantic projection."""
    if (
        request.managed_set_id != ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID
        or request.schema_migration != "reject"
        or request.administrator_quorum_change is not None
    ):
        return False
    rules = request.desired_policy.github_humans
    if not rules:
        return not any(
            principal_rules
            for principal_rules in (
                request.desired_policy.github_actions,
                request.desired_policy.terminal_agents,
                request.desired_policy.local_operators,
                request.desired_policy.local_admins,
                request.desired_policy.ordinary_agents,
            )
        )
    rule = rules[0]
    return (
        len(rules) == 1
        and rule.managed_set_id == ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_SET_ID
        and rule.managed_rule_id == ORDINARY_AGENT_DELIVERY_ADMINISTRATION_MANAGED_RULE_ID
        and len(rule.github_ids) == 1
        and rule.roles == ("admin",)
        and rule.products == ("launchplane",)
        and rule.contexts == ("launchplane",)
        and rule.actions == ORDINARY_AGENT_DELIVERY_ADMINISTRATION_ACTIONS
        and not rule.logins
        and not rule.organizations
        and not rule.teams
        and not rule.instances
        and not any(
            principal_rules
            for principal_rules in (
                request.desired_policy.github_actions,
                request.desired_policy.terminal_agents,
                request.desired_policy.local_operators,
                request.desired_policy.local_admins,
                request.desired_policy.ordinary_agents,
            )
        )
    )
