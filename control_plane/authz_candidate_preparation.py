"""Closed authorization candidates compiled into standard managed-policy plans."""

from __future__ import annotations

import hashlib
from typing import Final, Literal, assert_never

from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationRecord,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.contracts.privileged_operation import (
    ORDINARY_AGENT_DELIVERY_ACTIVATION_APPROVE_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_CANCEL_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_PLAN_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_READ_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_REVOKE_ACTION,
    ManagedAuthzPolicySetProposalInput,
    OrdinaryAgentDeliveryPolicyIntent,
)
from control_plane.contracts.ordinary_agent import (
    OrdinaryAgentAction,
    OrdinaryAgentPolicyRule,
    OrdinaryAgentTarget,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubHumanPolicyRule,
    LaunchplaneAuthzPolicy,
    TerminalAgentIdentity,
    TerminalAgentPolicyRule,
    authz_selector_matches,
)
from control_plane.contracts.ordinary_agent_client import ORDINARY_AGENT_ENROLLMENT_PROPOSE_ACTION


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
    "ordinary-agent-enrollment-requester",
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


OrdinaryAgentPolicyPreparationReason = Literal[
    "ordinary_agent_policy_schema_unavailable",
    "ordinary_agent_target_unavailable",
    "ordinary_agent_rule_conflict",
]


class OrdinaryAgentPolicyPreparationError(ValueError):
    """A safe, authored diagnostic for first-client policy preparation."""

    def __init__(self, reason_code: OrdinaryAgentPolicyPreparationReason, message: str) -> None:
        super().__init__(message)
        self.reason_code = reason_code


ORDINARY_AGENT_DELIVERY_POLICY_MANAGED_SET_PREFIX = "ordinary-agent.delivery."
ORDINARY_AGENT_DELIVERY_POLICY_MANAGED_RULE_ID = "delivery"
ORDINARY_AGENT_DELIVERY_POLICY_ACTIONS: tuple[OrdinaryAgentAction, ...] = (
    "self_read",
    "preflight",
    "guarded_merge",
)

TERMINAL_ENROLLMENT_POLICY_MANAGED_SET_ID = "terminal-agent.ordinary-agent-enrollment"
TERMINAL_ENROLLMENT_POLICY_MANAGED_RULE_ID = "requester"
TERMINAL_ENROLLMENT_POLICY_ACTIONS = (ORDINARY_AGENT_ENROLLMENT_PROPOSE_ACTION,)
TERMINAL_ENROLLMENT_POLICY_REASON = "Prepare terminal client connection requests."
TERMINAL_ENROLLMENT_POLICY_RELATED_ISSUE = "#2369"
TerminalEnrollmentCapabilityState = Literal[
    "configured_identity_absent",
    "ready",
    "missing",
    "unmanaged",
    "mismatched",
    "ambiguous",
    "unavailable",
]


def terminal_enrollment_capability_state(
    *,
    policy: LaunchplaneAuthzPolicy,
    identity: TerminalAgentIdentity | None,
) -> TerminalEnrollmentCapabilityState:
    """Classify the exact managed capability enforced by enrollment ingress."""
    if identity is None:
        return "configured_identity_absent"
    if not _is_exact_terminal_selector(identity.subject) or not _is_exact_terminal_selector(
        identity.token_label
    ):
        return "unavailable"
    if policy.schema_version not in (2, 3):
        return "unavailable"
    matching = tuple(
        rule
        for rule in policy.terminal_agents
        if rule.managed_set_id
        and rule.managed_rule_id
        and rule.allows(
            identity=identity,
            action=ORDINARY_AGENT_ENROLLMENT_PROPOSE_ACTION,
            product="launchplane",
            context="launchplane",
            target=AuthorizationTarget(scope="global"),
            schema_version=policy.schema_version,
        )
    )
    if len(matching) == 1:
        return "ready"
    if len(matching) > 1:
        return "ambiguous"
    occupied = tuple(
        rule
        for _principal_type, rule in _rules(policy)
        if getattr(rule, "managed_set_id", None) == TERMINAL_ENROLLMENT_POLICY_MANAGED_SET_ID
    )
    if occupied:
        return "mismatched" if len(occupied) == 1 else "ambiguous"
    overlapping_rules = tuple(
        rule
        for rule in policy.terminal_agents
        if _terminal_identity_matches(rule, identity)
        and rule.allows_scope(
            action=ORDINARY_AGENT_ENROLLMENT_PROPOSE_ACTION,
            product="launchplane",
            context="launchplane",
            target=AuthorizationTarget(scope="global"),
            schema_version=policy.schema_version,
        )
    )
    if not overlapping_rules:
        return "missing"
    if len(overlapping_rules) > 1:
        return "ambiguous"
    if not overlapping_rules[0].managed_set_id or not overlapping_rules[0].managed_rule_id:
        return "unmanaged"
    return "mismatched"


def compile_terminal_enrollment_policy_candidate(
    *,
    current_policy: LaunchplaneAuthzPolicy,
    identity: TerminalAgentIdentity | None,
    intent: AuthorizationCandidateIntent = "add",
) -> tuple[Literal["planned", "already_satisfied"], ManagedAuthzPolicySetProposalInput | None]:
    """Compile the one narrow terminal enrollment rule when it is unambiguous."""
    owned_rules = tuple(
        (principal_type, rule)
        for principal_type, rule in _rules(current_policy)
        if getattr(rule, "managed_set_id", None) == TERMINAL_ENROLLMENT_POLICY_MANAGED_SET_ID
    )
    if intent == "remove":
        if not owned_rules:
            return "already_satisfied", None
        if (
            len(owned_rules) != 1
            or owned_rules[0][0] != "terminal_agents"
            or not isinstance(owned_rules[0][1], TerminalAgentPolicyRule)
            or not _is_exact_terminal_enrollment_rule(owned_rules[0][1])
        ):
            raise OrdinaryAgentPolicyPreparationError(
                "ordinary_agent_rule_conflict",
                "The terminal enrollment capability is not the exact managed rule Launchplane prepared.",
            )
        return (
            "planned",
            ManagedAuthzPolicySetProposalInput(
                managed_set_id=TERMINAL_ENROLLMENT_POLICY_MANAGED_SET_ID,
                desired_policy=LaunchplaneAuthzPolicy(schema_version=current_policy.schema_version),
                reason=TERMINAL_ENROLLMENT_POLICY_REASON,
                related_issue=TERMINAL_ENROLLMENT_POLICY_RELATED_ISSUE,
            ),
        )
    desired_rule = (
        TerminalAgentPolicyRule(
            managed_set_id=TERMINAL_ENROLLMENT_POLICY_MANAGED_SET_ID,
            managed_rule_id=TERMINAL_ENROLLMENT_POLICY_MANAGED_RULE_ID,
            subjects=(identity.subject,),
            token_labels=(identity.token_label,),
            products=("launchplane",),
            contexts=("launchplane",),
            actions=TERMINAL_ENROLLMENT_POLICY_ACTIONS,
        )
        if identity is not None
        else None
    )
    state = terminal_enrollment_capability_state(policy=current_policy, identity=identity)
    if state == "ready":
        return "already_satisfied", None
    errors: dict[TerminalEnrollmentCapabilityState, str] = {
        "configured_identity_absent": "No configured terminal client identity is available.",
        "unavailable": "Terminal enrollment requires authorization policy version 2 or 3.",
        "unmanaged": "The configured terminal client has an unmanaged enrollment rule.",
        "mismatched": "The configured terminal client has a different managed enrollment rule.",
        "ambiguous": "The configured terminal client has ambiguous enrollment rules.",
        "missing": "",
        "ready": "",
    }
    if state != "missing":
        raise OrdinaryAgentPolicyPreparationError("ordinary_agent_rule_conflict", errors[state])
    if identity is None:
        raise AssertionError("missing terminal identity was classified as missing")
    assert desired_rule is not None
    return (
        "planned",
        ManagedAuthzPolicySetProposalInput(
            managed_set_id=TERMINAL_ENROLLMENT_POLICY_MANAGED_SET_ID,
            desired_policy=LaunchplaneAuthzPolicy(
                schema_version=current_policy.schema_version,
                terminal_agents=(desired_rule,),
            ),
            reason=TERMINAL_ENROLLMENT_POLICY_REASON,
            related_issue=TERMINAL_ENROLLMENT_POLICY_RELATED_ISSUE,
        ),
    )


def _terminal_identity_matches(
    rule: TerminalAgentPolicyRule, identity: TerminalAgentIdentity
) -> bool:
    return (not rule.subjects or authz_selector_matches(identity.subject, rule.subjects)) and (
        not rule.token_labels or authz_selector_matches(identity.token_label, rule.token_labels)
    )


def _is_exact_terminal_selector(value: str) -> bool:
    return bool(value.strip()) and not any(character in value for character in "*?[")


def _is_exact_terminal_enrollment_rule(rule: TerminalAgentPolicyRule) -> bool:
    return (
        rule.managed_set_id == TERMINAL_ENROLLMENT_POLICY_MANAGED_SET_ID
        and rule.managed_rule_id == TERMINAL_ENROLLMENT_POLICY_MANAGED_RULE_ID
        and len(rule.subjects) == 1
        and len(rule.token_labels) == 1
        and _is_exact_terminal_selector(rule.subjects[0])
        and _is_exact_terminal_selector(rule.token_labels[0])
        and rule.products == ("launchplane",)
        and rule.contexts == ("launchplane",)
        and rule.actions == TERMINAL_ENROLLMENT_POLICY_ACTIONS
        and not rule.instances
    )


def ordinary_agent_delivery_policy_managed_set_id(principal_id: str) -> str:
    digest = hashlib.sha256(f"ordinary-agent-policy:{principal_id}".encode()).hexdigest()[:48]
    return f"{ORDINARY_AGENT_DELIVERY_POLICY_MANAGED_SET_PREFIX}{digest}"


def compile_ordinary_agent_delivery_policy_candidate(
    *,
    current_policy: LaunchplaneAuthzPolicy,
    intent: OrdinaryAgentDeliveryPolicyIntent,
    inventory: RepositoryInventoryRecord,
    merge_policy: MergeTrainPolicyRecord,
) -> tuple[Literal["planned", "already_satisfied"], ManagedAuthzPolicySetProposalInput | None]:
    """Compile one server-resolved ordinary rule into the existing policy plan."""
    if current_policy.schema_version not in (2, 3):
        raise OrdinaryAgentPolicyPreparationError(
            "ordinary_agent_policy_schema_unavailable",
            "Client access requires authorization policy version 2 or 3.",
        )
    repository_id = int(intent.repository_id)
    if inventory.inventory_state != "tracked" or int(inventory.repository_id) != repository_id:
        raise OrdinaryAgentPolicyPreparationError(
            "ordinary_agent_target_unavailable",
            "The selected project is no longer tracked. Check setup prerequisites again.",
        )
    repository = inventory.repository
    configured = tuple(
        target
        for target in merge_policy.policy.policies
        if target.repository.casefold() == repository.casefold()
        and target.base_branch == intent.base_branch
    )
    if len(configured) != 1:
        raise OrdinaryAgentPolicyPreparationError(
            "ordinary_agent_target_unavailable",
            "The selected delivery branch is not configured. Check setup prerequisites and choose a recorded branch.",
        )
    managed_set_id = ordinary_agent_delivery_policy_managed_set_id(intent.principal_id)
    managed_rule_id = ORDINARY_AGENT_DELIVERY_POLICY_MANAGED_RULE_ID
    target = OrdinaryAgentTarget(
        repository_id=repository_id,
        repository=repository,
        base_branch=intent.base_branch,
    )
    desired_rule = OrdinaryAgentPolicyRule(
        managed_set_id=managed_set_id,
        managed_rule_id=managed_rule_id,
        principal_id=intent.principal_id,
        target=target,
        actions=ORDINARY_AGENT_DELIVERY_POLICY_ACTIONS,
    )
    occupied_set = tuple(
        rule
        for _principal_type, rule in _rules(current_policy)
        if getattr(rule, "managed_set_id", None) == managed_set_id
    )
    if occupied_set and occupied_set != (desired_rule,):
        raise OrdinaryAgentPolicyPreparationError(
            "ordinary_agent_rule_conflict",
            "This client identity already has conflicting access. Prepare a new client setup.",
        )
    existing = tuple(
        rule for rule in current_policy.ordinary_agents if rule.principal_id == intent.principal_id
    )
    if existing:
        if len(existing) == 1 and existing[0] == desired_rule:
            return "already_satisfied", None
        raise OrdinaryAgentPolicyPreparationError(
            "ordinary_agent_rule_conflict",
            "This client identity already has different access. Prepare a new client setup.",
        )
    desired_policy = LaunchplaneAuthzPolicy(
        schema_version=3,
        ordinary_agents=(desired_rule,),
    )
    migration: Literal["reject", "migrate_v2_to_v3"] = (
        "migrate_v2_to_v3" if current_policy.schema_version == 2 else "reject"
    )
    return (
        "planned",
        ManagedAuthzPolicySetProposalInput(
            managed_set_id=managed_set_id,
            desired_policy=desired_policy,
            schema_migration=migration,
            reason=f"Prepare ordinary-agent delivery for {intent.client_label}.",
            related_issue="#2423",
        ),
    )


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
    configured_terminal_identity: TerminalAgentIdentity | None = None,
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
    if candidate_id == "ordinary-agent-enrollment-requester":
        try:
            return compile_terminal_enrollment_policy_candidate(
                current_policy=current_policy,
                identity=configured_terminal_identity,
                intent=intent,
            )
        except OrdinaryAgentPolicyPreparationError as error:
            raise AuthorizationCandidatePreparationError(
                "candidate_set_conflict", str(error)
            ) from error
    assert_never(candidate_id)


def is_terminal_enrollment_requester_request(
    request: ManagedAuthzPolicySetProposalInput, *, intent: AuthorizationCandidateIntent
) -> bool:
    """Recognize the narrow server-derived terminal enrollment candidate."""
    rules = request.desired_policy.terminal_agents
    common = (
        request.managed_set_id == TERMINAL_ENROLLMENT_POLICY_MANAGED_SET_ID
        and request.schema_migration == "reject"
        and request.administrator_quorum_change is None
        and request.reason == TERMINAL_ENROLLMENT_POLICY_REASON
        and request.related_issue == TERMINAL_ENROLLMENT_POLICY_RELATED_ISSUE
        and not request.desired_policy.github_actions
        and not request.desired_policy.github_humans
        and not request.desired_policy.local_operators
        and not request.desired_policy.local_admins
        and not request.desired_policy.ordinary_agents
    )
    if intent == "remove":
        return common and not rules
    return common and len(rules) == 1 and _is_exact_terminal_enrollment_rule(rules[0])


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
    elif candidate_id == "ordinary-agent-enrollment-requester":
        return is_terminal_enrollment_requester_request(request, intent=intent)
    else:
        assert_never(candidate_id)
    rules = request.desired_policy.github_humans
    return (
        recognized
        and bool(rules) == (intent == "add")
        and all(rule.github_ids == (github_id,) for rule in rules)
    )
