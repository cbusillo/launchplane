"""Closed authorization candidates compiled into standard managed-policy plans."""

from __future__ import annotations

import hashlib
from typing import Final, Literal, assert_never

from control_plane.contracts.ordinary_agent_activation import (
    OrdinaryAgentDeliveryActivationRecord,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.authz_scope import DOKPLOY_TARGET_LANE_SETUP_ACTION
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    is_exclusive_product_context,
    product_context_owner_map,
)
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.contracts.privileged_operation import (
    ORDINARY_AGENT_DELIVERY_ACTIVATION_APPROVE_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_CANCEL_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_PLAN_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_READ_ACTION,
    ORDINARY_AGENT_DELIVERY_ACTIVATION_REVOKE_ACTION,
    ManagedAuthzPolicySetProposalInput,
    AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
    MERGE_TRAIN_POLICY_OPERATION_PROPOSE_ACTION,
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
    LocalOperatorIdentity,
    LocalOperatorPolicyRule,
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

LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"

AGENT_PRODUCT_SETUP_CANDIDATE_ID: Final = "agent-product-setup"
AGENT_PRODUCT_SETUP_MANAGED_SET_ID = "operator.agent-product-setup"
AGENT_PRODUCT_SETUP_REASON = (
    "Prepare agent product setup: testing settings, the testing compose target and the "
    "production backup policy of the selected products."
)
AGENT_PRODUCT_SETUP_RELATED_ISSUE = "#2766"
AGENT_PRODUCT_SETUP_MAX_PRODUCTS = 20
# Each selected product gets exactly these rules, bound to one lane each:
# (rule id suffix, lane, actions).
_AGENT_PRODUCT_SETUP_RULE_SHAPES: Final = (
    ("testing-config", "testing", ("product_config.plan", "product_config.apply")),
    ("prod-backup-policy", "prod", ("production_backup_authority.write",)),
    ("testing-target", "testing", (DOKPLOY_TARGET_LANE_SETUP_ACTION,)),
)

AGENT_POLICY_PROPOSER_CANDIDATE_ID: Final = "agent-policy-proposer"
AGENT_POLICY_PROPOSER_MANAGED_SET_ID = "operator.agent-policy-proposer"
AGENT_POLICY_PROPOSER_MANAGED_RULE_ID = "proposer"
AGENT_POLICY_PROPOSER_ACTIONS = (
    AUTHZ_POLICY_OPERATION_PROPOSE_ACTION,
    MERGE_TRAIN_POLICY_OPERATION_PROPOSE_ACTION,
)
AGENT_POLICY_PROPOSER_REASON = "Prepare agent policy proposals for Director review."
AGENT_POLICY_PROPOSER_RELATED_ISSUE = "#2586"

AuthorizationCandidateId = Literal[
    "agent-policy-proposer",
    "ordinary-agent-delivery-administration",
    "administrator-product-evidence-read",
    "ordinary-agent-enrollment-requester",
    "agent-product-setup",
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
    "candidate_principal_unavailable",
    "candidate_products_required",
    "candidate_product_unavailable",
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


_AgentProductSetupState = Literal["absent", "present", "conflict"]
# (product, lane context, subject, token label) for one product's prepared rules.
_AgentProductSetupGrant = tuple[str, str, str, str]


def _agent_product_setup_rules(
    *,
    product: str,
    context: str,
    subject: str,
    token_label: str,
) -> tuple[LocalOperatorPolicyRule, ...]:
    return tuple(
        LocalOperatorPolicyRule(
            managed_set_id=AGENT_PRODUCT_SETUP_MANAGED_SET_ID,
            managed_rule_id=f"{product}.{suffix}",
            subjects=(subject,),
            token_labels=(token_label,),
            products=(product,),
            contexts=(context,),
            instances=(instance,),
            actions=actions,
        )
        for suffix, instance, actions in _AGENT_PRODUCT_SETUP_RULE_SHAPES
    )


def _sorted_actions(rule: LocalOperatorPolicyRule) -> LocalOperatorPolicyRule:
    return rule.model_copy(update={"actions": tuple(sorted(rule.actions))})


def _agent_product_setup_grants(
    rules: tuple[LocalOperatorPolicyRule, ...],
) -> tuple[_AgentProductSetupGrant, ...] | None:
    """Return each product's grant when the rules are exactly the prepared shape."""
    groups: dict[str, list[LocalOperatorPolicyRule]] = {}
    for rule in rules:
        product, separator, _ = (rule.managed_rule_id or "").rpartition(".")
        if (
            rule.managed_set_id != AGENT_PRODUCT_SETUP_MANAGED_SET_ID
            or not separator
            or not _is_exact_terminal_selector(product)
            or len(rule.contexts) != 1
            or len(rule.subjects) != 1
            or len(rule.token_labels) != 1
        ):
            return None
        groups.setdefault(product, []).append(rule)
    grants: list[_AgentProductSetupGrant] = []
    for product, group in sorted(groups.items()):
        first = group[0]
        grant = (product, first.contexts[0], first.subjects[0], first.token_labels[0])
        if not all(_is_exact_terminal_selector(value) for value in grant[1:]):
            return None
        expected = _agent_product_setup_rules(
            product=product, context=grant[1], subject=grant[2], token_label=grant[3]
        )
        by_id = {rule.managed_rule_id: _sorted_actions(rule) for rule in group}
        if len(by_id) != len(group) or by_id != {
            rule.managed_rule_id: _sorted_actions(rule) for rule in expected
        }:
            return None
        grants.append(grant)
    if not grants or len({grant[2:] for grant in grants}) != 1:
        return None
    return tuple(grants)


def _agent_product_setup_rule_state(
    policy: LaunchplaneAuthzPolicy,
) -> tuple[_AgentProductSetupState, tuple[_AgentProductSetupGrant, ...]]:
    owned_rules = tuple(
        (principal_type, rule)
        for principal_type, rule in _rules(policy)
        if getattr(rule, "managed_set_id", None) == AGENT_PRODUCT_SETUP_MANAGED_SET_ID
    )
    if not owned_rules:
        return "absent", ()
    if policy.schema_version not in (2, 3) or any(
        principal_type != "local_operators" or not isinstance(rule, LocalOperatorPolicyRule)
        for principal_type, rule in owned_rules
    ):
        return "conflict", ()
    grants = _agent_product_setup_grants(
        tuple(rule for _, rule in owned_rules if isinstance(rule, LocalOperatorPolicyRule))
    )
    if grants is None:
        return "conflict", ()
    return "present", grants


def agent_product_setup_state(
    policy: LaunchplaneAuthzPolicy,
    *,
    identity: LocalOperatorIdentity | None,
) -> AuthorizationCandidateState:
    """Report whether the set is absent, the exact prepared shape, or foreign."""
    state, grants = _agent_product_setup_rule_state(policy)
    if state == "absent":
        return "available"
    if state == "conflict":
        return "conflict"
    if identity is None or grants[0][2:] != (identity.subject, identity.token_label):
        return "conflict"
    return "active"


def agent_product_setup_products(policy: LaunchplaneAuthzPolicy) -> tuple[str, ...]:
    """Products the current set covers, or none when it is absent or foreign."""
    state, grants = _agent_product_setup_rule_state(policy)
    return tuple(grant[0] for grant in grants) if state == "present" else ()


def product_context_owners(record_store: object) -> dict[str, frozenset[str]]:
    """Map each lane or historical context, case-folded, to the products using it."""
    lister = getattr(record_store, "list_product_profile_records", None)
    if not callable(lister):
        raise TypeError("Agent product setup requires product profile storage.")
    return product_context_owner_map(
        record
        if isinstance(record, LaunchplaneProductProfileRecord)
        else LaunchplaneProductProfileRecord.model_validate(record)
        for record in lister()
    )


def is_exclusive_product_lane_context(
    *, context: str, product: str, owners: dict[str, frozenset[str]]
) -> bool:
    """True when ``context`` is canonical, not Launchplane's, and only ``product`` uses it."""
    return is_exclusive_product_context(context=context, product=product, owners=owners)


def agent_product_setup_grants_match_records(
    *,
    record_store: object,
    grants: tuple[_AgentProductSetupGrant, ...],
) -> bool:
    """Check that every grant's context is its product's own, exclusively."""
    try:
        owners = product_context_owners(record_store)
        selected = _require_agent_product_setup_lanes(
            record_store=record_store,
            products=tuple(grant[0] for grant in grants),
            owners=owners,
        )
    except (TypeError, AuthorizationCandidatePreparationError):
        return False
    return selected == tuple((grant[0], grant[1]) for grant in grants)


def normalize_agent_product_setup_products(products: tuple[str, ...]) -> tuple[str, ...]:
    """Dedupe and sort a browser product selection without trusting its order."""
    return tuple(sorted({product.strip() for product in products if product.strip()}))


def _require_agent_product_setup_lanes(
    *,
    record_store: object,
    products: tuple[str, ...],
    owners: dict[str, frozenset[str]] | None = None,
) -> tuple[tuple[str, str], ...]:
    """Return each selected product with its own exclusive lane context, or refuse."""
    normalized = normalize_agent_product_setup_products(products)
    if not normalized:
        raise AuthorizationCandidatePreparationError(
            "candidate_products_required",
            "Agent product setup requires at least one selected product.",
        )
    if len(normalized) > AGENT_PRODUCT_SETUP_MAX_PRODUCTS:
        raise AuthorizationCandidatePreparationError(
            "candidate_product_unavailable",
            "Agent product setup accepts a bounded product selection.",
        )
    if any(not _is_exact_terminal_selector(product) for product in normalized):
        raise AuthorizationCandidatePreparationError(
            "candidate_product_unavailable",
            "Agent product setup requires exact product identifiers.",
        )
    reader = getattr(record_store, "read_product_profile_record", None)
    if not callable(reader):
        raise TypeError("Agent product setup preparation requires product profile storage.")
    if owners is None:
        owners = product_context_owners(record_store)
    selected: list[tuple[str, str]] = []
    for product in normalized:
        try:
            record = reader(product)
        except FileNotFoundError as error:
            raise AuthorizationCandidatePreparationError(
                "candidate_product_unavailable",
                "A selected product has no product profile record.",
            ) from error
        profile = (
            record
            if isinstance(record, LaunchplaneProductProfileRecord)
            else LaunchplaneProductProfileRecord.model_validate(record)
        )
        contexts = {lane.context.strip() for lane in profile.lanes if lane.context.strip()}
        context = next(iter(contexts)) if len(contexts) == 1 else ""
        if profile.product != product or not is_exclusive_product_lane_context(
            context=context, product=product, owners=owners
        ):
            raise AuthorizationCandidatePreparationError(
                "candidate_product_unavailable",
                "A selected product needs one lowercase lane context that no other product uses.",
            )
        selected.append((product, context))
    return tuple(selected)


def compile_agent_product_setup_candidate(
    *,
    current_policy: LaunchplaneAuthzPolicy,
    identity: LocalOperatorIdentity | None,
    intent: AuthorizationCandidateIntent,
    products: tuple[str, ...],
    record_store: object,
) -> tuple[Literal["planned", "already_satisfied"], ManagedAuthzPolicySetProposalInput | None]:
    """Compile the closed product setup set for the service-configured `local_operator` identity."""
    state, existing_grants = _agent_product_setup_rule_state(current_policy)
    if state == "conflict":
        raise AuthorizationCandidatePreparationError(
            "candidate_set_conflict",
            "The agent product setup set is occupied or has an unexpected shape.",
        )
    if current_policy.schema_version not in (2, 3):
        raise AuthorizationCandidatePreparationError(
            "candidate_set_conflict",
            "Agent product setup requires authorization policy version 2 or 3.",
        )
    if intent == "remove":
        if state == "absent":
            return "already_satisfied", None
        desired_rules: tuple[LocalOperatorPolicyRule, ...] = ()
    else:
        if (
            identity is None
            or not _is_exact_terminal_selector(identity.subject)
            or not _is_exact_terminal_selector(identity.token_label)
        ):
            raise AuthorizationCandidatePreparationError(
                "candidate_principal_unavailable",
                "No exact configured local_operator identity is available.",
            )
        selected = _require_agent_product_setup_lanes(record_store=record_store, products=products)
        desired_rules = tuple(
            rule
            for product, context in selected
            for rule in _agent_product_setup_rules(
                product=product,
                context=context,
                subject=identity.subject,
                token_label=identity.token_label,
            )
        )
        if existing_grants:
            if existing_grants[0][2:] != (identity.subject, identity.token_label):
                raise AuthorizationCandidatePreparationError(
                    "candidate_set_conflict",
                    "The agent product setup set belongs to a different local_operator identity.",
                )
            if existing_grants == tuple(
                (product, context, identity.subject, identity.token_label)
                for product, context in selected
            ):
                return "already_satisfied", None
    return (
        "planned",
        ManagedAuthzPolicySetProposalInput(
            managed_set_id=AGENT_PRODUCT_SETUP_MANAGED_SET_ID,
            desired_policy=LaunchplaneAuthzPolicy(
                schema_version=current_policy.schema_version,
                local_operators=desired_rules,
            ),
            schema_migration="reject",
            administrator_quorum_change=None,
            reason=AGENT_PRODUCT_SETUP_REASON,
            related_issue=AGENT_PRODUCT_SETUP_RELATED_ISSUE,
        ),
    )


def agent_product_setup_request_grants(
    request: ManagedAuthzPolicySetProposalInput,
    *,
    intent: AuthorizationCandidateIntent,
) -> tuple[_AgentProductSetupGrant, ...] | None:
    """Recognize the exact product setup shape, independently of audit wording.

    Returns each product's grant for an add, an empty tuple for a removal, and
    ``None`` when the request is not this candidate's shape.
    """
    desired = request.desired_policy
    if (
        request.managed_set_id != AGENT_PRODUCT_SETUP_MANAGED_SET_ID
        or request.schema_migration != "reject"
        or request.administrator_quorum_change is not None
        or request.ordinary_agent_preparation_context is not None
        or desired.schema_version not in (2, 3)
        or desired.administrator_quorum is not None
        or desired.github_actions
        or desired.github_humans
        or desired.terminal_agents
        or desired.local_admins
        or desired.ordinary_agents
    ):
        return None
    rules = desired.local_operators
    if intent == "remove":
        return () if not rules else None
    return _agent_product_setup_grants(rules)


def _agent_policy_proposer_rule(identity: LocalOperatorIdentity) -> LocalOperatorPolicyRule:
    return LocalOperatorPolicyRule(
        managed_set_id=AGENT_POLICY_PROPOSER_MANAGED_SET_ID,
        managed_rule_id=AGENT_POLICY_PROPOSER_MANAGED_RULE_ID,
        subjects=(identity.subject,),
        token_labels=(identity.token_label,),
        products=("launchplane",),
        contexts=(LAUNCHPLANE_SERVICE_CONTEXT,),
        actions=AGENT_POLICY_PROPOSER_ACTIONS,
    )


def agent_policy_proposer_request_matches(
    request: ManagedAuthzPolicySetProposalInput,
    *,
    identity: LocalOperatorIdentity | None,
    intent: AuthorizationCandidateIntent,
) -> bool:
    policy = request.desired_policy
    if (
        request.managed_set_id != AGENT_POLICY_PROPOSER_MANAGED_SET_ID
        or request.schema_migration != "reject"
        or request.administrator_quorum_change is not None
        or request.reason != AGENT_POLICY_PROPOSER_REASON
        or request.related_issue != AGENT_POLICY_PROPOSER_RELATED_ISSUE
        or policy.github_actions
        or policy.github_humans
        or policy.terminal_agents
        or policy.local_admins
        or policy.ordinary_agents
    ):
        return False
    if intent == "remove":
        return not policy.local_operators
    return identity is not None and policy.local_operators == (
        _agent_policy_proposer_rule(identity),
    )


def compile_agent_policy_proposer_candidate(
    *,
    current_policy: LaunchplaneAuthzPolicy,
    identity: LocalOperatorIdentity | None,
    intent: AuthorizationCandidateIntent,
) -> tuple[Literal["planned", "already_satisfied"], ManagedAuthzPolicySetProposalInput | None]:
    if current_policy.schema_version not in (2, 3):
        raise AuthorizationCandidatePreparationError(
            "candidate_set_conflict", "Agent proposals require authorization policy version 2 or 3."
        )
    schema_version: Literal[2, 3] = 2 if current_policy.schema_version == 2 else 3
    owned = tuple(
        (kind, rule)
        for kind, rule in _rules(current_policy)
        if getattr(rule, "managed_set_id", None) == AGENT_POLICY_PROPOSER_MANAGED_SET_ID
    )
    if intent == "add" and (
        identity is None
        or not identity.subject
        or not identity.token_label
        or any(character in identity.subject + identity.token_label for character in "*?[]")
    ):
        raise AuthorizationCandidatePreparationError(
            "candidate_principal_unavailable",
            "No exact configured local_operator identity is available.",
        )
    if intent == "remove":
        if not owned:
            return "already_satisfied", None
        return "planned", ManagedAuthzPolicySetProposalInput(
            managed_set_id=AGENT_POLICY_PROPOSER_MANAGED_SET_ID,
            desired_policy=LaunchplaneAuthzPolicy(schema_version=schema_version),
            reason=AGENT_POLICY_PROPOSER_REASON,
            related_issue=AGENT_POLICY_PROPOSER_RELATED_ISSUE,
        )
    if owned:
        if (
            len(owned) != 1
            or owned[0][0] != "local_operators"
            or not isinstance(owned[0][1], LocalOperatorPolicyRule)
        ):
            raise AuthorizationCandidatePreparationError(
                "candidate_set_conflict", "The agent proposer set is occupied by another shape."
            )
        existing = owned[0][1]
        assert isinstance(existing, LocalOperatorPolicyRule)
        if len(existing.subjects) != 1 or len(existing.token_labels) != 1:
            raise AuthorizationCandidatePreparationError(
                "candidate_set_conflict", "The agent proposer identity is ambiguous."
            )
        stored_identity = LocalOperatorIdentity(
            subject=existing.subjects[0], token_label=existing.token_labels[0]
        )
        if any(
            character in stored_identity.subject + stored_identity.token_label
            for character in "*?[]"
        ) or existing != _agent_policy_proposer_rule(stored_identity):
            raise AuthorizationCandidatePreparationError(
                "candidate_set_conflict", "The agent proposer set has an unexpected shape."
            )
        if intent == "add" and stored_identity != identity:
            raise AuthorizationCandidatePreparationError(
                "candidate_set_conflict", "The agent proposer set belongs to another identity."
            )
    if intent == "add":
        assert identity is not None
        if any(
            rule.managed_set_id != AGENT_POLICY_PROPOSER_MANAGED_SET_ID
            and any(
                rule.allows(
                    identity=identity,
                    action=action,
                    product="launchplane",
                    context=LAUNCHPLANE_SERVICE_CONTEXT,
                    target=AuthorizationTarget(scope="global"),
                    schema_version=schema_version,
                )
                for action in AGENT_POLICY_PROPOSER_ACTIONS
            )
            for rule in current_policy.local_operators
        ):
            raise AuthorizationCandidatePreparationError(
                "candidate_action_overlap", "Proposal authority overlaps another rule."
            )
        if owned:
            return "already_satisfied", None
        desired_rules: tuple[LocalOperatorPolicyRule, ...] = (
            _agent_policy_proposer_rule(identity),
        )
    return "planned", ManagedAuthzPolicySetProposalInput(
        managed_set_id=AGENT_POLICY_PROPOSER_MANAGED_SET_ID,
        desired_policy=LaunchplaneAuthzPolicy(
            schema_version=schema_version, local_operators=desired_rules
        ),
        reason=AGENT_POLICY_PROPOSER_REASON,
        related_issue=AGENT_POLICY_PROPOSER_RELATED_ISSUE,
    )


def compile_authorization_candidate(
    *,
    candidate_id: AuthorizationCandidateId,
    current_policy: LaunchplaneAuthzPolicy,
    github_id: int,
    intent: AuthorizationCandidateIntent,
    record_store: object,
    configured_terminal_identity: TerminalAgentIdentity | None = None,
    configured_local_operator_identity: LocalOperatorIdentity | None = None,
    products: tuple[str, ...] = (),
) -> tuple[Literal["planned", "already_satisfied"], ManagedAuthzPolicySetProposalInput | None]:
    if candidate_id == AGENT_POLICY_PROPOSER_CANDIDATE_ID:
        return compile_agent_policy_proposer_candidate(
            current_policy=current_policy,
            identity=configured_local_operator_identity,
            intent=intent,
        )
    if candidate_id == AGENT_PRODUCT_SETUP_CANDIDATE_ID:
        return compile_agent_product_setup_candidate(
            current_policy=current_policy,
            identity=configured_local_operator_identity,
            intent=intent,
            products=products,
            record_store=record_store,
        )
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
    products: tuple[str, ...] = (),
    configured_local_operator_identity: LocalOperatorIdentity | None = None,
    record_store: object = None,
) -> bool:
    if candidate_id == AGENT_POLICY_PROPOSER_CANDIDATE_ID:
        return not products and agent_policy_proposer_request_matches(
            request, identity=configured_local_operator_identity, intent=intent
        )
    if candidate_id == AGENT_PRODUCT_SETUP_CANDIDATE_ID:
        grants = agent_product_setup_request_grants(request, intent=intent)
        if grants is None:
            return False
        if intent == "remove":
            return not products
        return (
            configured_local_operator_identity is not None
            and grants[0][2:]
            == (
                configured_local_operator_identity.subject,
                configured_local_operator_identity.token_label,
            )
            and tuple(grant[0] for grant in grants)
            == normalize_agent_product_setup_products(products)
            and agent_product_setup_grants_match_records(record_store=record_store, grants=grants)
        )
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
