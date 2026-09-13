"""Closed authorization candidates compiled into standard managed-policy plans."""

from __future__ import annotations

from typing import Final, Literal, assert_never

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


ORDINARY_AGENT_DELIVERY_ADMINISTRATION_CANDIDATE_ID: Final = (
    "ordinary-agent-delivery-administration"
)
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

ADMINISTRATOR_PRODUCT_EVIDENCE_READ_CANDIDATE_ID: Final = "administrator-product-evidence-read"
ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID = "operator.product-evidence-read"
ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID = "administrator-context-reader"
ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID = "administrator-environment-reader"
ADMINISTRATOR_PRODUCT_EVIDENCE_READ_ACTIONS = ("product_environment.read",)
ADMINISTRATOR_PRODUCT_EVIDENCE_READ_REASON = (
    "Prepare read-only administrator access to product evidence."
)
ADMINISTRATOR_PRODUCT_EVIDENCE_READ_RELATED_ISSUE = "#2058"

AuthorizationCandidateId = Literal[
    "ordinary-agent-delivery-administration",
    "administrator-product-evidence-read",
]
AuthorizationCandidateIntent = Literal["add", "remove"]
AuthorizationCandidateState = Literal["available", "active", "conflict"]
_AdministratorProductEvidenceReadState = Literal["absent", "legacy", "current", "conflict"]


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


def _has_only_github_human_rules(policy: LaunchplaneAuthzPolicy) -> bool:
    return not any(
        principal_rules
        for principal_rules in (
            policy.github_actions,
            policy.terminal_agents,
            policy.local_operators,
            policy.local_admins,
            policy.ordinary_agents,
        )
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
        return _has_only_github_human_rules(request.desired_policy)
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
        and _has_only_github_human_rules(request.desired_policy)
    )


def _administrator_product_evidence_read_state(
    policy: LaunchplaneAuthzPolicy,
    *,
    github_id: int,
) -> _AdministratorProductEvidenceReadState:
    if github_id < 1 or policy.schema_version not in (2, 3):
        return "conflict"
    managed_rules = tuple(
        (principal_type, rule)
        for principal_type, rule in _rules(policy)
        if getattr(rule, "managed_set_id", None)
        == ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID
    )
    if not managed_rules:
        return "absent"
    if len(managed_rules) != 2:
        return "conflict"
    rules_by_id = {
        getattr(rule, "managed_rule_id", None): (principal_type, rule)
        for principal_type, rule in managed_rules
    }
    if set(rules_by_id) != {
        ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID,
        ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID,
    }:
        return "conflict"
    environment_rule_contexts: tuple[str, ...] | None = None
    for managed_rule_id, (principal_type, rule) in rules_by_id.items():
        if not isinstance(rule, GitHubHumanPolicyRule):
            return "conflict"
        if managed_rule_id == ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID:
            expected_rule_instances: tuple[str, ...] | None = ()
        elif managed_rule_id == ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID:
            expected_rule_instances = ("*",)
            environment_rule_contexts = rule.contexts
        else:
            expected_rule_instances = None
        if (
            expected_rule_instances is None
            or principal_type != "github_humans"
            or rule.github_ids != (github_id,)
            or rule.roles != ("admin",)
            or rule.products
            or (
                rule.contexts != ("launchplane",)
                if managed_rule_id == ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID
                else rule.contexts not in (("launchplane",), ())
            )
            or rule.actions != ADMINISTRATOR_PRODUCT_EVIDENCE_READ_ACTIONS
            or rule.instances != expected_rule_instances
            or rule.logins
            or rule.organizations
            or rule.teams
        ):
            return "conflict"
    return "legacy" if environment_rule_contexts == ("launchplane",) else "current"


def administrator_product_evidence_read_state(
    policy: LaunchplaneAuthzPolicy,
    *,
    github_id: int,
) -> AuthorizationCandidateState:
    """Report the candidate's public availability without exposing legacy detail."""
    state = _administrator_product_evidence_read_state(policy, github_id=github_id)
    if state == "absent":
        return "available"
    if state in ("legacy", "current"):
        return "active"
    return "conflict"


def compile_administrator_product_evidence_read_candidate(
    *,
    current_policy: LaunchplaneAuthzPolicy,
    github_id: int,
    intent: AuthorizationCandidateIntent,
) -> tuple[Literal["planned", "already_satisfied"], ManagedAuthzPolicySetProposalInput | None]:
    state = _administrator_product_evidence_read_state(current_policy, github_id=github_id)
    if state == "conflict":
        raise AuthorizationCandidatePreparationError(
            "candidate_set_conflict",
            "The authorization candidate conflicts with current policy state.",
        )
    if (intent == "add" and state == "current") or (intent == "remove" and state == "absent"):
        return "already_satisfied", None
    desired_policy = LaunchplaneAuthzPolicy(
        schema_version=current_policy.schema_version,
        github_humans=(
            (
                GitHubHumanPolicyRule(
                    managed_set_id=ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID,
                    managed_rule_id=ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID,
                    github_ids=(github_id,),
                    roles=("admin",),
                    contexts=("launchplane",),
                    actions=ADMINISTRATOR_PRODUCT_EVIDENCE_READ_ACTIONS,
                ),
                GitHubHumanPolicyRule(
                    managed_set_id=ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID,
                    managed_rule_id=ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID,
                    github_ids=(github_id,),
                    roles=("admin",),
                    contexts=(),
                    instances=("*",),
                    actions=ADMINISTRATOR_PRODUCT_EVIDENCE_READ_ACTIONS,
                ),
            )
            if intent == "add"
            else ()
        ),
    )
    return (
        "planned",
        ManagedAuthzPolicySetProposalInput(
            managed_set_id=ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID,
            desired_policy=desired_policy,
            schema_migration="reject",
            administrator_quorum_change=None,
            reason=ADMINISTRATOR_PRODUCT_EVIDENCE_READ_REASON,
            related_issue=ADMINISTRATOR_PRODUCT_EVIDENCE_READ_RELATED_ISSUE,
        ),
    )


def _is_administrator_product_evidence_read_request(
    request: ManagedAuthzPolicySetProposalInput,
    *,
    environment_contexts: tuple[str, ...],
) -> bool:
    """Recognize the exact authority shape, independently of audit wording."""
    if (
        request.managed_set_id != ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID
        or request.schema_migration != "reject"
        or request.administrator_quorum_change is not None
        or request.desired_policy.schema_version not in (2, 3)
        or request.desired_policy.administrator_quorum is not None
        or not _has_only_github_human_rules(request.desired_policy)
    ):
        return False
    rules = request.desired_policy.github_humans
    if not rules:
        return True
    if len(rules) != 2:
        return False
    rules_by_id = {rule.managed_rule_id: rule for rule in rules}
    if set(rules_by_id) != {
        ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID,
        ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID,
    }:
        return False
    github_ids = {rule.github_ids for rule in rules}
    if len(github_ids) != 1:
        return False
    for managed_rule_id, rule in rules_by_id.items():
        if managed_rule_id == ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID:
            expected_rule_instances: tuple[str, ...] | None = ()
        elif managed_rule_id == ADMINISTRATOR_PRODUCT_EVIDENCE_ENVIRONMENT_RULE_ID:
            expected_rule_instances = ("*",)
        else:
            expected_rule_instances = None
        if (
            expected_rule_instances is None
            or rule.managed_set_id != ADMINISTRATOR_PRODUCT_EVIDENCE_READ_MANAGED_SET_ID
            or len(rule.github_ids) != 1
            or rule.roles != ("admin",)
            or rule.products
            or (
                rule.contexts != ("launchplane",)
                if managed_rule_id == ADMINISTRATOR_PRODUCT_EVIDENCE_CONTEXT_RULE_ID
                else rule.contexts != environment_contexts
            )
            or rule.actions != ADMINISTRATOR_PRODUCT_EVIDENCE_READ_ACTIONS
            or rule.instances != expected_rule_instances
            or rule.logins
            or rule.organizations
            or rule.teams
        ):
            return False
    return True


def is_administrator_product_evidence_read_request(
    request: ManagedAuthzPolicySetProposalInput,
) -> bool:
    """Recognize the corrected shape used for new candidate preparation and replay."""
    return _is_administrator_product_evidence_read_request(request, environment_contexts=())


def is_legacy_administrator_product_evidence_read_request(
    request: ManagedAuthzPolicySetProposalInput,
) -> bool:
    """Recognize only persisted pre-correction product-evidence records."""
    return _is_administrator_product_evidence_read_request(
        request,
        environment_contexts=("launchplane",),
    )


def compile_authorization_candidate(
    *,
    candidate_id: AuthorizationCandidateId,
    current_policy: LaunchplaneAuthzPolicy,
    github_id: int,
    intent: AuthorizationCandidateIntent,
    record_store: object,
) -> tuple[Literal["planned", "already_satisfied"], ManagedAuthzPolicySetProposalInput | None]:
    if candidate_id == ORDINARY_AGENT_DELIVERY_ADMINISTRATION_CANDIDATE_ID:
        return compile_ordinary_agent_delivery_administration_candidate(
            current_policy=current_policy,
            github_id=github_id,
            intent=intent,
            record_store=record_store,
        )
    if candidate_id == ADMINISTRATOR_PRODUCT_EVIDENCE_READ_CANDIDATE_ID:
        return compile_administrator_product_evidence_read_candidate(
            current_policy=current_policy,
            github_id=github_id,
            intent=intent,
        )
    assert_never(candidate_id)


def authorization_candidate_request_matches(
    *,
    candidate_id: AuthorizationCandidateId,
    request: ManagedAuthzPolicySetProposalInput,
    github_id: int,
    intent: AuthorizationCandidateIntent,
) -> bool:
    if candidate_id == ORDINARY_AGENT_DELIVERY_ADMINISTRATION_CANDIDATE_ID:
        recognized = is_ordinary_agent_delivery_administration_request(request)
    elif candidate_id == ADMINISTRATOR_PRODUCT_EVIDENCE_READ_CANDIDATE_ID:
        recognized = is_administrator_product_evidence_read_request(request)
    else:
        assert_never(candidate_id)
    rules = request.desired_policy.github_humans
    return (
        recognized
        and bool(rules) == (intent == "add")
        and all(rule.github_ids == (github_id,) for rule in rules)
    )
