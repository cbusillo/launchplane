from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatchcase
import hashlib
import json
import re
from typing import Any, Literal, Protocol, TypeAlias, cast, overload

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.authz_scope import (
    exact_instance_workflow_authz_actions,
    instance_pinned_workflow_authz_actions,
    operational_readiness_authz_actions,
)
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.authz_access_read import (
    AuthzManagedSetCollectionSummary,
    AuthzManagedSetSummary,
    AuthzPolicyCandidatePreviewRequest,
    AuthzPolicyCandidatePreviewResponse,
    AuthzPolicyCandidateProbeDelta,
    AuthzPolicyCandidateProbeResult,
    AuthzPolicyCandidateReadinessReason,
    AuthzPolicyCandidateReadinessSummary,
    AuthzPolicyCandidateStructuralDiff,
    AuthzPolicyCandidateSummary,
    AuthzPolicyHealthReasonCode,
    AuthzPolicyHealthSnapshot,
    AuthzPolicyHealthState,
    AuthzPolicyHealthSummary,
    AuthzPolicyRecordSummary,
    AuthzPrincipalRuleCounts,
    AuthzPolicyPrincipalType,
    AuthzReachableAdministratorSummary,
    EffectiveAccessDecision,
    EffectiveAccessRequestSummary,
)
from control_plane.contracts.owner_acceptance import (
    OWNER_ACCEPTANCE_EVENT_WRITE_ACTION,
    OWNER_ACCEPTANCE_READ_ACTION,
)
from control_plane.contracts.product_owner import (
    PRODUCT_OWNER_POLICY_READ_ACTION,
    PRODUCT_OWNER_POLICY_WRITE_ACTION,
    PRODUCT_OWNER_REQUIREMENT_READ_ACTION,
    PRODUCT_OWNER_REQUIREMENT_WRITE_ACTION,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    AuthzInstanceSelectors,
    GitHubActionsIdentity,
    GitHubActionsPolicyRule,
    GitHubHumanPolicyRule,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LocalAdminIdentity,
    LocalAdminPolicyRule,
    LocalOperatorIdentity,
    LocalOperatorPolicyRule,
    TerminalAgentIdentity,
    TerminalAgentPolicyRule,
    action_safety,
    migrate_authz_policy_to_schema_v2,
)


TimestampProvider = Callable[[], str]


class AuthzPolicyRecordStore(Protocol):
    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]: ...


def _require_expected_authz_policy(
    *,
    current_record: LaunchplaneAuthzPolicyRecord,
    expected_policy_sha256: str,
) -> None:
    if expected_policy_sha256 and current_record.policy_sha256 != expected_policy_sha256:
        raise AuthzPolicyConflictError(
            "Launchplane active authz policy changed after the caller was authorized."
        )


AuthzPolicyRule: TypeAlias = (
    GitHubActionsPolicyRule
    | GitHubHumanPolicyRule
    | TerminalAgentPolicyRule
    | LocalOperatorPolicyRule
    | LocalAdminPolicyRule
)
AuthzApplyingIdentity: TypeAlias = (
    GitHubActionsIdentity
    | GitHubHumanIdentity
    | TerminalAgentIdentity
    | LocalOperatorIdentity
    | LocalAdminIdentity
)
AuthzPrincipalType: TypeAlias = AuthzPolicyPrincipalType
_AUTHZ_PRINCIPAL_TYPES: tuple[AuthzPrincipalType, ...] = (
    "github_actions",
    "github_humans",
    "terminal_agents",
    "local_operators",
    "local_admins",
)
_AUTHZ_POLICY_HEALTH_MANAGED_SET_LIMIT = 100


@dataclass(frozen=True)
class AuthzRuleLocation:
    principal_type: AuthzPrincipalType
    index: int


@dataclass(frozen=True)
class AuthzManagedRuleEntry:
    principal_type: AuthzPrincipalType
    rule: AuthzPolicyRule


@dataclass(frozen=True)
class AuthzPolicyRuleEntry:
    principal_type: AuthzPrincipalType
    rule: AuthzPolicyRule


def _authz_policy_rule_collections(
    policy: LaunchplaneAuthzPolicy,
) -> tuple[tuple[AuthzPrincipalType, tuple[AuthzPolicyRule, ...]], ...]:
    return (
        ("github_actions", policy.github_actions),
        ("github_humans", policy.github_humans),
        ("terminal_agents", policy.terminal_agents),
        ("local_operators", policy.local_operators),
        ("local_admins", policy.local_admins),
    )


_AUTHZ_RULE_SELECTOR_FIELDS = (
    "workflow_refs",
    "job_workflow_refs",
    "event_names",
    "refs",
    "environments",
    "products",
    "contexts",
    "instances",
    "actions",
    "logins",
    "organizations",
    "teams",
    "roles",
    "subjects",
    "token_labels",
)


@overload
def _normalize_authz_rule(rule: GitHubActionsPolicyRule) -> GitHubActionsPolicyRule: ...


@overload
def _normalize_authz_rule(rule: GitHubHumanPolicyRule) -> GitHubHumanPolicyRule: ...


@overload
def _normalize_authz_rule(rule: TerminalAgentPolicyRule) -> TerminalAgentPolicyRule: ...


@overload
def _normalize_authz_rule(rule: LocalOperatorPolicyRule) -> LocalOperatorPolicyRule: ...


@overload
def _normalize_authz_rule(rule: LocalAdminPolicyRule) -> LocalAdminPolicyRule: ...


def _normalize_authz_rule(rule: AuthzPolicyRule) -> AuthzPolicyRule:
    updates: dict[str, object] = {}
    for field_name in _AUTHZ_RULE_SELECTOR_FIELDS:
        if not hasattr(rule, field_name):
            continue
        values = cast(tuple[str, ...], getattr(rule, field_name))
        updates[field_name] = tuple(sorted({value.strip() for value in values if value.strip()}))
    return rule.model_copy(update=updates)


def _normalize_desired_authz_policy(policy: LaunchplaneAuthzPolicy) -> LaunchplaneAuthzPolicy:
    normalized_collections = {
        principal_type: tuple(
            sorted(
                (_normalize_authz_rule(rule) for rule in rules),
                key=lambda rule: rule.managed_rule_id or "",
            )
        )
        for principal_type, rules in _authz_policy_rule_collections(policy)
    }
    return LaunchplaneAuthzPolicy.model_validate(
        {"schema_version": policy.schema_version, **normalized_collections}
    )


_MANAGED_AUTHZ_SET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
_IMMUTABLE_JOB_WORKFLOW_REF_PATTERN = re.compile(r"^[^@]+/\.github/workflows/[^@]+@[0-9a-f]{40}$")
_WORKFLOW_REF_GLOB_CHARACTERS = frozenset("*?[")
_IMMUTABLE_WORKFLOW_ACTION_SAFETIES = frozenset(
    {"prod", "destructive", "secret_backed", "policy_admin"}
)
_IMMUTABLE_GITHUB_HUMAN_ACTION_SAFETIES = frozenset(
    {"prod", "destructive", "secret_backed", "policy_admin"}
)
_MANAGED_AUTHZ_RECONCILE_SOURCE = "service:authz-managed-rule-set-reconcile"
_AUTHZ_POLICY_ADMIN_ACTION = "authz_policy_grant.write"
_OWNER_ACCEPTANCE_MANAGED_SET_ID = "operator.owner-acceptance"
_OWNER_ACCEPTANCE_ACTIONS = frozenset(
    {OWNER_ACCEPTANCE_READ_ACTION, OWNER_ACCEPTANCE_EVENT_WRITE_ACTION}
)
_OWNER_ACCEPTANCE_READ_ONLY_ACTIONS = frozenset({OWNER_ACCEPTANCE_READ_ACTION})
_OWNER_ACCEPTANCE_PERMITTED_ACTION_SETS = (
    _OWNER_ACCEPTANCE_READ_ONLY_ACTIONS,
    _OWNER_ACCEPTANCE_ACTIONS,
)
_PRODUCT_OWNER_POLICY_ADMIN_MANAGED_SET_ID = "operator.product-owner-policy-admin"
_PRODUCT_OWNER_POLICY_ADMIN_ACTIONS = frozenset(
    {
        PRODUCT_OWNER_POLICY_READ_ACTION,
        PRODUCT_OWNER_POLICY_WRITE_ACTION,
        PRODUCT_OWNER_REQUIREMENT_READ_ACTION,
        PRODUCT_OWNER_REQUIREMENT_WRITE_ACTION,
    }
)
AuthzSchemaMigrationMode: TypeAlias = Literal["reject", "migrate_v1_to_v2"]
AuthzUnmanagedAdoptionMode: TypeAlias = Literal["reject", "adopt_matching"]


def _validate_owner_acceptance_managed_set(policy: LaunchplaneAuthzPolicy) -> None:
    if any(
        rules
        for principal_type, rules in _authz_policy_rule_collections(policy)
        if principal_type != "github_humans"
    ):
        raise ValueError("Owner Acceptance managed authz may contain only GitHub human rules.")
    for rule in policy.github_humans:
        if not rule.github_ids or rule.logins or rule.organizations or rule.teams:
            raise ValueError(
                "Owner Acceptance managed authz rules require only immutable GitHub IDs."
            )
        actions = frozenset(rule.actions)
        expected_roles = (
            ("admin", "read_only")
            if actions == _OWNER_ACCEPTANCE_READ_ONLY_ACTIONS
            else ("read_only",)
        )
        if tuple(sorted(rule.roles)) != expected_roles:
            raise ValueError(
                "Owner Acceptance viewer rules require admin and read_only roles; "
                "Owner candidate rules require only the read_only role."
            )
        if rule.products != ("launchplane",) or rule.contexts != ("owner-acceptance",):
            raise ValueError(
                "Owner Acceptance managed authz rules require the exact Launchplane workbench scope."
            )
        if rule.instances:
            raise ValueError(
                "Owner Acceptance managed authz rules cannot declare instance selectors."
            )
        if actions not in _OWNER_ACCEPTANCE_PERMITTED_ACTION_SETS:
            raise ValueError(
                "Owner Acceptance managed authz rules require either the read action alone or "
                "the read and event-write actions together."
            )


def _validate_product_owner_policy_admin_managed_set(policy: LaunchplaneAuthzPolicy) -> None:
    if any(
        rules
        for principal_type, rules in _authz_policy_rule_collections(policy)
        if principal_type != "local_operators"
    ):
        raise ValueError(
            "Product Owner policy administration managed authz may contain only local operator rules."
        )
    for rule in policy.local_operators:
        if (
            len(rule.subjects) != 1
            or len(rule.token_labels) != 1
            or any(_contains_selector_glob(value) for value in (*rule.subjects, *rule.token_labels))
        ):
            raise ValueError(
                "Product Owner policy administration rules require one exact operator subject "
                "and token label."
            )
        if (
            len(rule.products) != 1
            or len(rule.contexts) != 1
            or any(_contains_selector_glob(value) for value in (*rule.products, *rule.contexts))
        ):
            raise ValueError(
                "Product Owner policy administration rules require one exact product and system scope."
            )
        if frozenset(rule.actions) != _PRODUCT_OWNER_POLICY_ADMIN_ACTIONS:
            raise ValueError(
                "Product Owner policy administration rules require exactly the policy and "
                "requirement read/write actions."
            )


class AuthzManagedPolicyReconcileEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[2] = 2
    product: str
    mode: Literal["dry_run", "apply"] = "dry_run"
    managed_set_id: str
    schema_migration: AuthzSchemaMigrationMode = "reject"
    unmanaged_adoption: AuthzUnmanagedAdoptionMode = "reject"
    reason: str = ""
    related_issue: str = ""
    reviewed_plan_sha256: str = ""
    desired_policy: LaunchplaneAuthzPolicy

    @model_validator(mode="after")
    def _validate_reconcile(self) -> "AuthzManagedPolicyReconcileEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Managed authz policy reconciliation requires product 'launchplane'.")
        self.product = "launchplane"
        self.managed_set_id = self.managed_set_id.strip()
        if _MANAGED_AUTHZ_SET_PATTERN.fullmatch(self.managed_set_id) is None:
            raise ValueError(
                "Managed authz policy reconciliation requires a stable managed_set_id."
            )
        self.reason = self.reason.strip()
        self.related_issue = self.related_issue.strip()
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        if self.mode == "apply":
            if not self.reason:
                raise ValueError("Managed authz policy reconciliation apply requires reason.")
            if re.fullmatch(r"[0-9a-f]{64}", self.reviewed_plan_sha256) is None:
                raise ValueError(
                    "Managed authz policy reconciliation apply requires reviewed_plan_sha256."
                )
        elif self.reviewed_plan_sha256:
            raise ValueError(
                "Managed authz policy reconciliation dry-run cannot declare reviewed_plan_sha256."
            )
        if self.desired_policy.schema_version != 2:
            raise ValueError("Managed authz desired policy must use schema version 2.")
        self.desired_policy = _normalize_desired_authz_policy(self.desired_policy)
        if self.managed_set_id == _OWNER_ACCEPTANCE_MANAGED_SET_ID:
            _validate_owner_acceptance_managed_set(self.desired_policy)
        if self.managed_set_id == _PRODUCT_OWNER_POLICY_ADMIN_MANAGED_SET_ID:
            _validate_product_owner_policy_admin_managed_set(self.desired_policy)
        for principal_type, rules in _authz_policy_rule_collections(self.desired_policy):
            for rule in rules:
                managed_rule_id = rule.managed_rule_id
                if rule.managed_set_id != self.managed_set_id or managed_rule_id is None:
                    raise ValueError(
                        "Every desired managed authz rule must declare the request managed_set_id "
                        f"and a managed_rule_id ({principal_type})."
                    )
                if not rule.actions:
                    raise ValueError(
                        "Every desired managed authz rule must declare at least one action "
                        f"({principal_type}:{managed_rule_id})."
                    )
                if isinstance(rule, GitHubActionsPolicyRule) and (
                    not rule.repository_id or not rule.repository_owner_id
                ):
                    raise ValueError(
                        "Managed GitHub Actions authz rules require immutable repository_id "
                        f"and repository_owner_id selectors ({managed_rule_id})."
                    )
                if isinstance(rule, GitHubHumanPolicyRule):
                    if not rule.roles:
                        raise ValueError(
                            "Managed GitHub human authz rules require at least one explicit role "
                            f"({managed_rule_id})."
                        )
                    if not any((rule.github_ids, rule.logins, rule.organizations, rule.teams)):
                        raise ValueError(
                            "Managed GitHub human authz rules require at least one principal "
                            f"selector ({managed_rule_id})."
                        )
                    if any(
                        _contains_selector_glob(selector)
                        for selector in (*rule.logins, *rule.organizations, *rule.teams)
                    ):
                        raise ValueError(
                            "Managed GitHub human authz rules require exact login, organization, "
                            f"and team selectors ({managed_rule_id})."
                        )
                    if (
                        "admin" in rule.roles
                        or any(
                            action_safety(action) in _IMMUTABLE_GITHUB_HUMAN_ACTION_SAFETIES
                            for action in rule.actions
                        )
                    ) and not rule.github_ids:
                        raise ValueError(
                            "Managed GitHub human admin and sensitive-action rules require "
                            f"immutable github_ids ({managed_rule_id})."
                        )
        return self


class AuthzManagedPolicyRouteResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    updated_policy: LaunchplaneAuthzPolicy
    previous_authz_policy_record: LaunchplaneAuthzPolicyRecord
    authz_policy_record: LaunchplaneAuthzPolicyRecord
    changed: bool
    result: dict[str, object]
    driver_result: dict[str, object]


AuthzManagedRuleChangeKind: TypeAlias = Literal["added", "adopted", "updated", "removed"]


class AuthzManagedRuleChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    managed_rule_id: str
    change: AuthzManagedRuleChangeKind
    previous_principal_type: AuthzPrincipalType | None = None
    desired_principal_type: AuthzPrincipalType | None = None
    previous_rule_sha256: str = ""
    desired_rule_sha256: str = ""


class AuthzManagedCompatibilityRetirement(BaseModel):
    model_config = ConfigDict(extra="forbid")

    managed_rule_id: str
    principal_type: Literal["github_actions"] = "github_actions"
    retired_rule_sha256: str
    retained_managed_rule_sha256: str
    match_type: Literal["github_actions_name_only_authorization_narrowing"] = (
        "github_actions_name_only_authorization_narrowing"
    )


AuthzOperationalReadinessBlockerCode: TypeAlias = AuthzPolicyCandidateReadinessReason
_AUTHZ_OPERATIONAL_READINESS_BLOCKER_CODES: tuple[AuthzOperationalReadinessBlockerCode, ...] = (
    "repository_not_exact",
    "workflow_refs_not_singleton",
    "workflow_ref_not_exact",
    "job_workflow_refs_not_singleton",
    "job_workflow_ref_not_immutable",
    "actions_not_singleton",
    "action_not_exact",
    "products_not_singleton",
    "product_not_exact",
    "contexts_not_singleton",
    "context_not_exact",
    "instances_not_singleton",
    "instance_not_exact",
)


class AuthzManagedOperationalReadinessBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    managed_rule_id: str
    actions: tuple[str, ...]
    reason_codes: tuple[AuthzOperationalReadinessBlockerCode, ...]


AuthzManagedPolicySafetyBlockerCode: TypeAlias = Literal[
    "authz_policy_admin_unreachable",
    "authz_policy_applying_admin_removed",
    "authz_policy_independent_admin_unreachable",
]


class AuthzManagedPolicySafetyBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: AuthzManagedPolicySafetyBlockerCode
    message: str


class AuthzManagedPolicyDiff(BaseModel):
    model_config = ConfigDict(extra="forbid")

    managed_set_id: str
    previous_record_id: str
    previous_revision: int
    candidate_revision: int
    previous_policy_sha256: str
    desired_policy_sha256: str
    desired_set_sha256: str
    plan_sha256: str
    schema_migrated: bool = False
    changed: bool = False
    authorization_changed: bool = False
    added_rule_count: int = 0
    adopted_rule_count: int = 0
    updated_rule_count: int = 0
    removed_rule_count: int = 0
    unchanged_rule_count: int = 0
    unmanaged_compatibility_candidate_count: int = 0
    retired_unmanaged_compatibility_rule_count: int = 0
    retired_unmanaged_compatibility_rules: tuple[AuthzManagedCompatibilityRetirement, ...] = ()
    policy_safety_blocker_count: int = 0
    policy_safety_blockers: tuple[AuthzManagedPolicySafetyBlocker, ...] = ()
    operational_readiness_blocked_rule_count: int = 0
    operational_readiness_blockers: tuple[AuthzManagedOperationalReadinessBlocker, ...] = ()
    changes: tuple[AuthzManagedRuleChange, ...] = ()


def _managed_policy_safety_blocker(
    code: AuthzManagedPolicySafetyBlockerCode,
) -> AuthzManagedPolicySafetyBlocker:
    messages = {
        "authz_policy_admin_unreachable": (
            "Managed authz policy reconciliation must retain at least one reachable principal "
            "that can administer Launchplane authz policy."
        ),
        "authz_policy_applying_admin_removed": (
            "Managed authz policy reconciliation must retain policy administration "
            "authority for the applying identity."
        ),
        "authz_policy_independent_admin_unreachable": (
            "Managed authz policy reconciliation must retain a reachable policy "
            "administrator independent from the applying identity."
        ),
    }
    return AuthzManagedPolicySafetyBlocker(code=code, message=messages[code])


def summarize_authz_policy_record(record: LaunchplaneAuthzPolicyRecord) -> dict[str, object]:
    immutable_repository_rule_count = sum(
        1 for rule in record.policy.github_actions if rule.repository_id
    )
    return {
        "record_id": record.record_id,
        "revision": record.revision,
        "status": record.status,
        "source": record.source,
        "updated_at": record.updated_at,
        "policy_sha256": record.policy_sha256,
        "github_actions_rule_count": len(record.policy.github_actions),
        "github_actions_immutable_repository_rule_count": immutable_repository_rule_count,
        "github_actions_legacy_name_only_rule_count": (
            len(record.policy.github_actions) - immutable_repository_rule_count
        ),
        "github_humans_rule_count": len(record.policy.github_humans),
        "terminal_agents_rule_count": len(record.policy.terminal_agents),
        "local_operators_rule_count": len(record.policy.local_operators),
        "local_admins_rule_count": len(record.policy.local_admins),
    }


def summarize_active_authz_policy_record(
    record: LaunchplaneAuthzPolicyRecord,
) -> dict[str, object]:
    summary = summarize_authz_policy_record(record)
    summary["policy_schema_version"] = record.policy.schema_version
    rules_by_principal = tuple(_authz_policy_rule_collections(record.policy))
    managed_rules = [
        {
            "managed_set_id": rule.managed_set_id,
            "managed_rule_id": rule.managed_rule_id,
            "principal_type": principal_type,
            "rule_sha256": _authz_rule_sha256(rule),
        }
        for principal_type, rules in rules_by_principal
        for rule in rules
        if rule.managed_set_id is not None and rule.managed_rule_id is not None
    ]
    unmanaged_rule_counts = {
        principal_type: sum(rule.managed_set_id is None for rule in rules)
        for principal_type, rules in rules_by_principal
    }
    summary["managed_rules"] = managed_rules
    summary["managed_rule_count"] = len(managed_rules)
    summary["unmanaged_rule_count"] = sum(unmanaged_rule_counts.values())
    summary["unmanaged_rule_counts"] = unmanaged_rule_counts
    summary["github_actions_privileged_unpinned_reusable_rule_count"] = sum(
        _github_rule_requires_immutable_workflow(rule)
        and (
            not rule.job_workflow_refs
            or any(
                _IMMUTABLE_JOB_WORKFLOW_REF_PATTERN.fullmatch(job_workflow_ref) is None
                for job_workflow_ref in rule.job_workflow_refs
            )
        )
        for rule in record.policy.github_actions
    )
    return summary


def _authz_principal_rule_counts(
    entries: list[AuthzPolicyRuleEntry],
) -> AuthzPrincipalRuleCounts:
    counts = {principal_type: 0 for principal_type in _AUTHZ_PRINCIPAL_TYPES}
    for entry in entries:
        counts[entry.principal_type] += 1
    return AuthzPrincipalRuleCounts(**counts)


def summarize_authz_policy_health(
    *,
    policy: LaunchplaneAuthzPolicy,
    caller_identity: AuthzApplyingIdentity,
) -> tuple[
    AuthzPolicyHealthSummary,
    AuthzManagedSetCollectionSummary,
    AuthzReachableAdministratorSummary,
]:
    rules_by_principal = _authz_policy_rule_collections(policy)
    rule_entries = [
        AuthzPolicyRuleEntry(principal_type=principal_type, rule=rule)
        for principal_type, rules in rules_by_principal
        for rule in rules
    ]
    managed_entries = [entry for entry in rule_entries if entry.rule.managed_set_id is not None]
    unmanaged_rule_count = len(rule_entries) - len(managed_entries)

    managed_sets: dict[str, list[AuthzPolicyRuleEntry]] = {}
    for entry in managed_entries:
        managed_set_id = entry.rule.managed_set_id
        if managed_set_id is None:
            continue
        managed_sets.setdefault(managed_set_id, []).append(entry)
    sorted_managed_set_ids = sorted(managed_sets)
    returned_managed_set_ids = sorted_managed_set_ids[:_AUTHZ_POLICY_HEALTH_MANAGED_SET_LIMIT]
    managed_set_items = tuple(
        AuthzManagedSetSummary(
            managed_set_id=managed_set_id,
            rule_count=len(managed_sets[managed_set_id]),
            principal_rule_counts=_authz_principal_rule_counts(managed_sets[managed_set_id]),
        )
        for managed_set_id in returned_managed_set_ids
    )

    administrator_entries = [
        entry for entry in rule_entries if _authz_rule_grants_policy_administration(entry.rule)
    ]
    caller_administrator_rule_count = sum(
        _authz_rule_allows_identity(
            rule=entry.rule,
            identity=caller_identity,
            schema_version=policy.schema_version,
        )
        for entry in administrator_entries
    )
    applying_github_id = (
        caller_identity.github_id if isinstance(caller_identity, GitHubHumanIdentity) else 0
    )
    independent_administrator_rule_count = sum(
        _is_strict_immutable_github_human_administrator_rule(rule)
        and any(github_id != applying_github_id for github_id in rule.github_ids)
        for rule in policy.github_humans
    )
    managed_administrator_rule_count = sum(
        entry.rule.managed_set_id is not None for entry in administrator_entries
    )

    immutable_repository_rule_count = sum(1 for rule in policy.github_actions if rule.repository_id)
    legacy_name_only_rule_count = len(policy.github_actions) - immutable_repository_rule_count
    privileged_unpinned_rule_count = sum(
        _github_rule_requires_immutable_workflow(rule)
        and (
            not rule.job_workflow_refs
            or any(
                _IMMUTABLE_JOB_WORKFLOW_REF_PATTERN.fullmatch(job_workflow_ref) is None
                for job_workflow_ref in rule.job_workflow_refs
            )
        )
        for rule in policy.github_actions
    )

    reason_codes: list[AuthzPolicyHealthReasonCode] = []
    if not administrator_entries:
        reason_codes.append("authz_policy_admin_unreachable")
    elif not independent_administrator_rule_count:
        reason_codes.append("authz_policy_independent_admin_unreachable")
    if policy.schema_version == 1:
        reason_codes.append("policy_schema_legacy")
    if unmanaged_rule_count:
        reason_codes.append("unmanaged_rules_present")
    if legacy_name_only_rule_count:
        reason_codes.append("github_actions_legacy_name_only_rules_present")
    if privileged_unpinned_rule_count:
        reason_codes.append("github_actions_privileged_unpinned_reusable_rules_present")

    health_state: AuthzPolicyHealthState
    if "authz_policy_admin_unreachable" in reason_codes:
        health_state = "blocked"
    elif reason_codes:
        health_state = "attention_required"
    else:
        health_state = "healthy"

    return (
        AuthzPolicyHealthSummary(
            state=health_state,
            reason_codes=tuple(reason_codes),
            managed_rule_count=len(managed_entries),
            unmanaged_rule_count=unmanaged_rule_count,
            github_actions_legacy_name_only_rule_count=legacy_name_only_rule_count,
            github_actions_privileged_unpinned_reusable_rule_count=privileged_unpinned_rule_count,
        ),
        AuthzManagedSetCollectionSummary(
            total_count=len(sorted_managed_set_ids),
            returned_count=len(managed_set_items),
            truncated=len(sorted_managed_set_ids) > len(managed_set_items),
            items=managed_set_items,
        ),
        AuthzReachableAdministratorSummary(
            policy_reachable=bool(administrator_entries),
            rule_count=len(administrator_entries),
            managed_rule_count=managed_administrator_rule_count,
            unmanaged_rule_count=(len(administrator_entries) - managed_administrator_rule_count),
            principal_rule_counts=_authz_principal_rule_counts(administrator_entries),
            caller_has_policy_administration=bool(caller_administrator_rule_count),
            independent_from_caller_reachable=bool(independent_administrator_rule_count),
            independent_from_caller_rule_count=independent_administrator_rule_count,
        ),
    )


def summarize_active_authz_policy_health_record(
    *,
    record: LaunchplaneAuthzPolicyRecord,
    caller_identity: AuthzApplyingIdentity,
) -> AuthzPolicyHealthSnapshot:
    health, managed_sets, reachable_administrators = summarize_authz_policy_health(
        policy=record.policy,
        caller_identity=caller_identity,
    )
    return AuthzPolicyHealthSnapshot(
        policy=AuthzPolicyRecordSummary(
            record_id=record.record_id,
            revision=record.revision,
            policy_sha256=record.policy_sha256,
            updated_at=record.updated_at,
            schema_version=record.policy.schema_version,
        ),
        health=health,
        managed_sets=managed_sets,
        reachable_administrators=reachable_administrators,
    )


def authz_policy_operator_payload(identity: AuthzApplyingIdentity) -> dict[str, object]:
    if isinstance(identity, GitHubHumanIdentity):
        return {
            "type": "github_human",
            "login": identity.login,
            "github_id": identity.github_id,
            "role": identity.role,
        }
    if isinstance(identity, TerminalAgentIdentity):
        return {
            "type": "terminal_agent",
            "subject": identity.subject,
            "token_label": identity.token_label,
        }
    if isinstance(identity, LocalOperatorIdentity):
        return {
            "type": "local_operator",
            "subject": identity.subject,
            "token_label": identity.token_label,
        }
    if isinstance(identity, LocalAdminIdentity):
        return {
            "type": "local_admin",
            "subject": identity.subject,
            "token_label": identity.token_label,
        }
    assert isinstance(identity, GitHubActionsIdentity)
    return {
        "type": "github_actions",
        "repository": identity.repository,
        "repository_id": identity.repository_id,
        "repository_owner": identity.repository_owner,
        "repository_owner_id": identity.repository_owner_id,
        "workflow_ref": identity.workflow_ref,
        "job_workflow_ref": identity.job_workflow_ref,
        "event_name": identity.event_name,
        "ref": identity.ref,
        "sha": identity.sha,
        "subject": identity.subject,
    }


class AuthzPolicyConflictError(ValueError):
    pass


class AuthzPolicySafetyError(AuthzPolicyConflictError):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class AuthzPolicyRequestError(ValueError):
    pass


def _authz_rule_sha256(rule: AuthzPolicyRule) -> str:
    canonical_json = json.dumps(
        rule.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode()).hexdigest()


@overload
def _authz_rule_without_managed_identity(
    rule: GitHubActionsPolicyRule,
) -> GitHubActionsPolicyRule: ...


@overload
def _authz_rule_without_managed_identity(
    rule: GitHubHumanPolicyRule,
) -> GitHubHumanPolicyRule: ...


@overload
def _authz_rule_without_managed_identity(
    rule: TerminalAgentPolicyRule,
) -> TerminalAgentPolicyRule: ...


@overload
def _authz_rule_without_managed_identity(
    rule: LocalOperatorPolicyRule,
) -> LocalOperatorPolicyRule: ...


@overload
def _authz_rule_without_managed_identity(rule: LocalAdminPolicyRule) -> LocalAdminPolicyRule: ...


def _authz_rule_without_managed_identity(rule: AuthzPolicyRule) -> AuthzPolicyRule:
    return rule.model_copy(update={"managed_set_id": None, "managed_rule_id": None})


def _github_rule_requires_immutable_workflow(
    rule: GitHubActionsPolicyRule,
) -> bool:
    return any(
        action in exact_instance_workflow_authz_actions()
        or (rule.instances and action in instance_pinned_workflow_authz_actions())
        or action_safety(action) in _IMMUTABLE_WORKFLOW_ACTION_SAFETIES
        for action in rule.actions
    ) or any(instance in {"*", "prod"} for instance in rule.instances)


def _contains_selector_glob(value: str) -> bool:
    return any(character in value for character in _WORKFLOW_REF_GLOB_CHARACTERS)


def is_immutable_job_workflow_ref(value: str) -> bool:
    return _IMMUTABLE_JOB_WORKFLOW_REF_PATTERN.fullmatch(
        value
    ) is not None and not _contains_selector_glob(value)


def operational_readiness_rule_selector_blockers(
    rule: GitHubActionsPolicyRule,
) -> tuple[AuthzOperationalReadinessBlockerCode, ...]:
    blockers: list[AuthzOperationalReadinessBlockerCode] = []
    if _contains_selector_glob(rule.repository):
        blockers.append("repository_not_exact")
    if len(rule.workflow_refs) != 1:
        blockers.append("workflow_refs_not_singleton")
    elif _contains_selector_glob(rule.workflow_refs[0]):
        blockers.append("workflow_ref_not_exact")
    if len(rule.job_workflow_refs) != 1:
        blockers.append("job_workflow_refs_not_singleton")
    elif not is_immutable_job_workflow_ref(rule.job_workflow_refs[0]):
        blockers.append("job_workflow_ref_not_immutable")
    if len(rule.actions) != 1:
        blockers.append("actions_not_singleton")
    elif _contains_selector_glob(rule.actions[0]):
        blockers.append("action_not_exact")
    if len(rule.products) != 1:
        blockers.append("products_not_singleton")
    elif _contains_selector_glob(rule.products[0]):
        blockers.append("product_not_exact")
    if len(rule.contexts) != 1:
        blockers.append("contexts_not_singleton")
    elif _contains_selector_glob(rule.contexts[0]):
        blockers.append("context_not_exact")
    if len(rule.instances) != 1:
        blockers.append("instances_not_singleton")
    elif _contains_selector_glob(rule.instances[0]):
        blockers.append("instance_not_exact")
    return tuple(blockers)


def _managed_operational_readiness_blockers(
    *,
    desired_policy: LaunchplaneAuthzPolicy,
    managed_set_id: str,
) -> tuple[AuthzManagedOperationalReadinessBlocker, ...]:
    readiness_actions = operational_readiness_authz_actions()
    blockers: list[AuthzManagedOperationalReadinessBlocker] = []
    for rule in desired_policy.github_actions:
        if rule.managed_set_id != managed_set_id:
            continue
        matching_actions = tuple(
            action
            for action in readiness_actions
            if any(fnmatchcase(action, selector) for selector in rule.actions)
        )
        if not matching_actions:
            continue
        reason_codes = operational_readiness_rule_selector_blockers(rule)
        if not reason_codes:
            continue
        if rule.managed_rule_id is None:
            raise AuthzPolicyRequestError(
                "Operational-readiness managed authz rules require managed_rule_id."
            )
        blockers.append(
            AuthzManagedOperationalReadinessBlocker(
                managed_rule_id=rule.managed_rule_id,
                actions=matching_actions,
                reason_codes=reason_codes,
            )
        )
    return tuple(blockers)


def _candidate_managed_set_policies(
    policy: LaunchplaneAuthzPolicy,
) -> dict[str, LaunchplaneAuthzPolicy]:
    grouped: dict[str, dict[AuthzPrincipalType, list[AuthzPolicyRule]]] = {}
    for principal_type, rules in _authz_policy_rule_collections(policy):
        for rule in rules:
            if rule.managed_set_id is None:
                continue
            collections = grouped.setdefault(
                rule.managed_set_id,
                {candidate_type: [] for candidate_type in _AUTHZ_PRINCIPAL_TYPES},
            )
            collections[principal_type].append(rule)
    return {
        managed_set_id: LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                **{
                    principal_type: tuple(collections[principal_type])
                    for principal_type in _AUTHZ_PRINCIPAL_TYPES
                },
            }
        )
        for managed_set_id, collections in grouped.items()
    }


def validate_authz_candidate_policy(
    policy: LaunchplaneAuthzPolicy,
) -> LaunchplaneAuthzPolicy:
    if policy.schema_version != 2:
        raise ValueError("Authorization candidate policy preview requires schema version 2.")
    normalized_policy = _normalize_desired_authz_policy(policy)
    for managed_set_id, managed_set_policy in sorted(
        _candidate_managed_set_policies(normalized_policy).items()
    ):
        AuthzManagedPolicyReconcileEnvelope(
            product="launchplane",
            managed_set_id=managed_set_id,
            desired_policy=managed_set_policy,
        )
    return normalized_policy


def _authz_policy_rule_count(policy: LaunchplaneAuthzPolicy) -> int:
    return sum(len(rules) for _, rules in _authz_policy_rule_collections(policy))


def _authz_policy_principal_rule_counts(
    policy: LaunchplaneAuthzPolicy,
) -> AuthzPrincipalRuleCounts:
    return AuthzPrincipalRuleCounts(
        **{
            principal_type: len(rules)
            for principal_type, rules in _authz_policy_rule_collections(policy)
        }
    )


def _authz_policy_managed_rule_entries(
    policy: LaunchplaneAuthzPolicy,
) -> dict[tuple[str, str], AuthzPolicyRuleEntry]:
    return {
        (rule.managed_set_id, rule.managed_rule_id): AuthzPolicyRuleEntry(
            principal_type=principal_type,
            rule=rule,
        )
        for principal_type, rules in _authz_policy_rule_collections(policy)
        for rule in rules
        if rule.managed_set_id is not None and rule.managed_rule_id is not None
    }


def _authz_policy_unmanaged_rule_hashes(
    policy: LaunchplaneAuthzPolicy,
) -> dict[AuthzPrincipalType, Counter[str]]:
    rule_hashes: dict[AuthzPrincipalType, Counter[str]] = {
        "github_actions": Counter(),
        "github_humans": Counter(),
        "terminal_agents": Counter(),
        "local_operators": Counter(),
        "local_admins": Counter(),
    }
    for principal_type, rules in _authz_policy_rule_collections(policy):
        for rule in rules:
            if rule.managed_set_id is None:
                rule_hashes[principal_type][_authz_rule_sha256(rule)] += 1
    return rule_hashes


def build_authz_candidate_policy_structural_diff(
    *,
    active_policy: LaunchplaneAuthzPolicy,
    candidate_policy: LaunchplaneAuthzPolicy,
) -> AuthzPolicyCandidateStructuralDiff:
    active_managed = _authz_policy_managed_rule_entries(active_policy)
    candidate_managed = _authz_policy_managed_rule_entries(candidate_policy)
    active_unmanaged = _authz_policy_unmanaged_rule_hashes(active_policy)
    candidate_unmanaged = _authz_policy_unmanaged_rule_hashes(candidate_policy)

    managed_keys = active_managed.keys() | candidate_managed.keys()
    added_managed_rule_count = 0
    removed_managed_rule_count = 0
    updated_managed_rule_count = 0
    unchanged_managed_rule_count = 0
    changed_principal_types: set[AuthzPrincipalType] = set()
    for managed_key in managed_keys:
        active_entry = active_managed.get(managed_key)
        candidate_entry = candidate_managed.get(managed_key)
        if active_entry is None:
            assert candidate_entry is not None
            added_managed_rule_count += 1
            changed_principal_types.add(candidate_entry.principal_type)
            continue
        if candidate_entry is None:
            removed_managed_rule_count += 1
            changed_principal_types.add(active_entry.principal_type)
            continue
        if active_entry.principal_type == candidate_entry.principal_type and _authz_rule_sha256(
            active_entry.rule
        ) == _authz_rule_sha256(candidate_entry.rule):
            unchanged_managed_rule_count += 1
            continue
        updated_managed_rule_count += 1
        changed_principal_types.update(
            (active_entry.principal_type, candidate_entry.principal_type)
        )

    added_unmanaged_rule_count = 0
    removed_unmanaged_rule_count = 0
    unchanged_unmanaged_rule_count = 0
    for principal_type in _AUTHZ_PRINCIPAL_TYPES:
        active_hashes = active_unmanaged[principal_type]
        candidate_hashes = candidate_unmanaged[principal_type]
        for rule_sha256 in active_hashes.keys() | candidate_hashes.keys():
            active_count = active_hashes[rule_sha256]
            candidate_count = candidate_hashes[rule_sha256]
            unchanged_unmanaged_rule_count += min(active_count, candidate_count)
            if candidate_count > active_count:
                added_unmanaged_rule_count += candidate_count - active_count
                changed_principal_types.add(principal_type)
            elif active_count > candidate_count:
                removed_unmanaged_rule_count += active_count - candidate_count
                changed_principal_types.add(principal_type)

    active_managed_sets = {managed_set_id for managed_set_id, _ in active_managed}
    candidate_managed_sets = {managed_set_id for managed_set_id, _ in candidate_managed}
    added_rule_count = added_managed_rule_count + added_unmanaged_rule_count
    removed_rule_count = removed_managed_rule_count + removed_unmanaged_rule_count
    schema_changed = active_policy.schema_version != candidate_policy.schema_version
    changed = bool(
        schema_changed or added_rule_count or updated_managed_rule_count or removed_rule_count
    )
    return AuthzPolicyCandidateStructuralDiff(
        changed=changed,
        schema_changed=schema_changed,
        active_rule_count=_authz_policy_rule_count(active_policy),
        candidate_rule_count=_authz_policy_rule_count(candidate_policy),
        added_rule_count=added_rule_count,
        updated_rule_count=updated_managed_rule_count,
        removed_rule_count=removed_rule_count,
        unchanged_rule_count=(unchanged_managed_rule_count + unchanged_unmanaged_rule_count),
        active_managed_rule_count=len(active_managed),
        candidate_managed_rule_count=len(candidate_managed),
        active_unmanaged_rule_count=sum(
            sum(rule_hashes.values()) for rule_hashes in active_unmanaged.values()
        ),
        candidate_unmanaged_rule_count=sum(
            sum(rule_hashes.values()) for rule_hashes in candidate_unmanaged.values()
        ),
        added_managed_set_count=len(candidate_managed_sets - active_managed_sets),
        removed_managed_set_count=len(active_managed_sets - candidate_managed_sets),
        retained_managed_set_count=len(active_managed_sets & candidate_managed_sets),
        changed_principal_types=tuple(
            principal_type
            for principal_type in _AUTHZ_PRINCIPAL_TYPES
            if principal_type in changed_principal_types
        ),
        active_principal_rule_counts=_authz_policy_principal_rule_counts(active_policy),
        candidate_principal_rule_counts=_authz_policy_principal_rule_counts(candidate_policy),
    )


def _candidate_policy_readiness(
    policy: LaunchplaneAuthzPolicy,
) -> AuthzPolicyCandidateReadinessSummary:
    blockers = tuple(
        blocker
        for managed_set_id, managed_set_policy in sorted(
            _candidate_managed_set_policies(policy).items()
        )
        for blocker in _managed_operational_readiness_blockers(
            desired_policy=managed_set_policy,
            managed_set_id=managed_set_id,
        )
    )
    reason_codes: set[AuthzPolicyCandidateReadinessReason] = {
        reason_code for blocker in blockers for reason_code in blocker.reason_codes
    }
    return AuthzPolicyCandidateReadinessSummary(
        blocked_rule_count=len(blockers),
        reason_codes=tuple(
            reason_code
            for reason_code in _AUTHZ_OPERATIONAL_READINESS_BLOCKER_CODES
            if reason_code in reason_codes
        ),
    )


def preview_authz_candidate_policy(
    *,
    active_record: LaunchplaneAuthzPolicyRecord,
    caller_identity: AuthzApplyingIdentity,
    request: AuthzPolicyCandidatePreviewRequest,
    trace_id: str,
) -> AuthzPolicyCandidatePreviewResponse:
    submitted_candidate_sha256 = authz_policy_sha256(request.candidate_policy)
    candidate_policy = validate_authz_candidate_policy(request.candidate_policy)
    evaluated_candidate_sha256 = authz_policy_sha256(candidate_policy)
    normalized_active_policy = _normalize_desired_authz_policy(active_record.policy)
    _validate_github_managed_workflow_transition(
        current_rules=active_record.policy.github_actions,
        desired_rules=tuple(
            rule for rule in candidate_policy.github_actions if rule.managed_set_id is not None
        ),
    )
    active_health, _, active_reachable_administrators = summarize_authz_policy_health(
        policy=active_record.policy,
        caller_identity=caller_identity,
    )
    candidate_health, _, candidate_reachable_administrators = summarize_authz_policy_health(
        policy=candidate_policy,
        caller_identity=caller_identity,
    )
    probe_results: list[AuthzPolicyCandidateProbeResult] = []
    for probe in request.probes:
        target = (
            AuthorizationTarget(scope="instance", instances=(probe.instance,))
            if probe.target_scope == "instance"
            else AuthorizationTarget(scope="context")
        )
        active_evaluation = active_record.policy.evaluate(
            identity=probe.identity(),
            action=probe.action,
            product=probe.product,
            context=probe.context,
            target=target,
            record_context=False,
        )
        candidate_evaluation = candidate_policy.evaluate(
            identity=probe.identity(),
            action=probe.action,
            product=probe.product,
            context=probe.context,
            target=target,
            record_context=False,
        )
        delta: AuthzPolicyCandidateProbeDelta
        if active_evaluation.decision == candidate_evaluation.decision:
            delta = (
                "unchanged"
                if active_evaluation.reason_code == candidate_evaluation.reason_code
                else "reason_changed"
            )
        elif candidate_evaluation.decision == "allowed":
            delta = "granted"
        else:
            delta = "revoked"
        probe_results.append(
            AuthzPolicyCandidateProbeResult(
                request=EffectiveAccessRequestSummary(
                    principal_type=probe.principal.principal_type,
                    action=probe.action,
                    product=probe.product,
                    context=probe.context,
                    target_scope=probe.target_scope,
                    instance=probe.instance,
                ),
                active_evaluation=EffectiveAccessDecision(
                    decision=active_evaluation.decision,
                    reason_code=active_evaluation.reason_code,
                ),
                candidate_evaluation=EffectiveAccessDecision(
                    decision=candidate_evaluation.decision,
                    reason_code=candidate_evaluation.reason_code,
                ),
                delta=delta,
            )
        )
    return AuthzPolicyCandidatePreviewResponse(
        trace_id=trace_id,
        active_policy=AuthzPolicyRecordSummary(
            record_id=active_record.record_id,
            revision=active_record.revision,
            policy_sha256=active_record.policy_sha256,
            updated_at=active_record.updated_at,
            schema_version=active_record.policy.schema_version,
        ),
        candidate_policy=AuthzPolicyCandidateSummary(
            submitted_policy_sha256=submitted_candidate_sha256,
            evaluated_policy_sha256=evaluated_candidate_sha256,
            normalized=submitted_candidate_sha256 != evaluated_candidate_sha256,
            rule_count=_authz_policy_rule_count(candidate_policy),
        ),
        diff=build_authz_candidate_policy_structural_diff(
            active_policy=normalized_active_policy,
            candidate_policy=candidate_policy,
        ),
        active_health=active_health,
        candidate_health=candidate_health,
        active_reachable_administrators=active_reachable_administrators,
        candidate_reachable_administrators=candidate_reachable_administrators,
        candidate_readiness=_candidate_policy_readiness(candidate_policy),
        probes=tuple(probe_results),
    )


def _github_transition_selector_payload(rule: GitHubActionsPolicyRule) -> dict[str, Any]:
    return _normalize_authz_rule(rule).model_dump(
        mode="json",
        exclude={
            "managed_set_id",
            "managed_rule_id",
            "repository_id",
            "repository_owner_id",
            "job_workflow_refs",
        },
    )


def _github_rule_matches_with_identity_narrowing(
    *, current_rule: GitHubActionsPolicyRule, desired_rule: GitHubActionsPolicyRule
) -> bool:
    excluded_fields = {
        "managed_set_id",
        "managed_rule_id",
        "repository_id",
        "repository_owner_id",
    }
    normalized_current_rule = _normalize_authz_rule(current_rule)
    normalized_desired_rule = _normalize_authz_rule(desired_rule)
    if normalized_current_rule.model_dump(
        mode="json", exclude=excluded_fields
    ) != normalized_desired_rule.model_dump(mode="json", exclude=excluded_fields):
        return False
    return current_rule.repository_id in {
        "",
        desired_rule.repository_id,
    } and current_rule.repository_owner_id in {"", desired_rule.repository_owner_id}


def _github_rule_preserves_existing_mutable_ref(
    *,
    current_rule: GitHubActionsPolicyRule,
    desired_rule: GitHubActionsPolicyRule,
    mutable_job_workflow_ref: str,
) -> bool:
    return (
        _github_rule_transition_base_matches(
            current_rule=current_rule,
            desired_rule=desired_rule,
        )
        and mutable_job_workflow_ref in current_rule.job_workflow_refs
    )


def _github_rule_transition_base_matches(
    *,
    current_rule: GitHubActionsPolicyRule,
    desired_rule: GitHubActionsPolicyRule,
) -> bool:
    if current_rule.managed_set_id not in {None, desired_rule.managed_set_id}:
        return False
    if (
        current_rule.managed_set_id == desired_rule.managed_set_id
        and current_rule.managed_rule_id != desired_rule.managed_rule_id
    ):
        return False
    if _github_transition_selector_payload(current_rule) != _github_transition_selector_payload(
        desired_rule
    ):
        return False
    if current_rule.repository_id not in {"", desired_rule.repository_id}:
        return False
    if current_rule.repository_owner_id not in {"", desired_rule.repository_owner_id}:
        return False
    return True


def _validate_github_managed_workflow_transition(
    *,
    current_rules: tuple[AuthzPolicyRule, ...],
    desired_rules: tuple[AuthzPolicyRule, ...],
) -> None:
    github_current_rules = tuple(
        rule for rule in current_rules if isinstance(rule, GitHubActionsPolicyRule)
    )
    for desired_rule in desired_rules:
        assert isinstance(desired_rule, GitHubActionsPolicyRule)
        if not _github_rule_requires_immutable_workflow(desired_rule):
            continue
        managed_rule_id = desired_rule.managed_rule_id
        if managed_rule_id is None:
            raise AuthzPolicyRequestError(
                "High-privilege managed GitHub Actions rules require managed_rule_id."
            )
        if not desired_rule.workflow_refs or any(
            _contains_selector_glob(workflow_ref) for workflow_ref in desired_rule.workflow_refs
        ):
            raise AuthzPolicyRequestError(
                "High-privilege managed GitHub Actions rules require exact caller workflow_refs "
                f"({managed_rule_id})."
            )
        if not desired_rule.job_workflow_refs:
            raise AuthzPolicyRequestError(
                "High-privilege managed GitHub Actions rules require an exact reviewed reusable "
                f"workflow identity ({managed_rule_id})."
            )
        if not any(
            is_immutable_job_workflow_ref(job_workflow_ref)
            for job_workflow_ref in desired_rule.job_workflow_refs
        ):
            raise AuthzPolicyRequestError(
                "High-privilege managed GitHub Actions rules require at least one reusable "
                "workflow identity pinned to a full commit SHA "
                f"({managed_rule_id})."
            )
        for job_workflow_ref in desired_rule.job_workflow_refs:
            if is_immutable_job_workflow_ref(job_workflow_ref):
                continue
            if _contains_selector_glob(job_workflow_ref) or not any(
                _github_rule_preserves_existing_mutable_ref(
                    current_rule=current_rule,
                    desired_rule=desired_rule,
                    mutable_job_workflow_ref=job_workflow_ref,
                )
                for current_rule in github_current_rules
            ):
                raise AuthzPolicyRequestError(
                    "High-privilege managed GitHub Actions rules can preserve a mutable reusable "
                    "workflow ref only when the active policy already authorizes that exact ref; "
                    f"new refs must use a full commit SHA ({managed_rule_id})."
                )


def _managed_rule_adoption_matches(
    *, current_rule: AuthzPolicyRule, desired_rule: AuthzPolicyRule
) -> bool:
    if current_rule.managed_set_id is not None:
        return False
    normalized_current_rule = _normalize_authz_rule(
        _authz_rule_without_managed_identity(current_rule)
    )
    normalized_desired_rule = _normalize_authz_rule(
        _authz_rule_without_managed_identity(desired_rule)
    )
    if normalized_current_rule == normalized_desired_rule:
        return True
    if not isinstance(current_rule, GitHubActionsPolicyRule) or not isinstance(
        desired_rule, GitHubActionsPolicyRule
    ):
        return False
    if _github_rule_matches_with_identity_narrowing(
        current_rule=current_rule,
        desired_rule=desired_rule,
    ):
        return True
    return _github_rule_transition_base_matches(
        current_rule=current_rule,
        desired_rule=desired_rule,
    ) and set(current_rule.job_workflow_refs).issubset(desired_rule.job_workflow_refs)


def _glob_selectors_cover(
    *, compatibility_values: tuple[str, ...], managed_values: tuple[str, ...]
) -> bool:
    if not compatibility_values or "*" in compatibility_values:
        return True
    if not managed_values:
        return False
    return all(
        (
            managed_value in compatibility_values
            if _contains_selector_glob(managed_value)
            else any(
                fnmatchcase(managed_value, compatibility_value)
                for compatibility_value in compatibility_values
            )
        )
        for managed_value in managed_values
    )


def _exact_selectors_cover(
    *, compatibility_values: tuple[str, ...], managed_values: tuple[str, ...]
) -> bool:
    if not compatibility_values:
        return True
    if not managed_values:
        return False
    return set(managed_values).issubset(compatibility_values)


def _instance_selectors_cover(
    *, compatibility_values: AuthzInstanceSelectors, managed_values: AuthzInstanceSelectors
) -> bool:
    if not compatibility_values or not managed_values:
        return compatibility_values == managed_values
    if compatibility_values == ("*",):
        return True
    if managed_values == ("*",):
        return False
    return set(managed_values).issubset(compatibility_values)


def _managed_github_compatibility_retirement_matches(
    *, current_rule: AuthzPolicyRule, desired_rule: AuthzPolicyRule
) -> bool:
    if not isinstance(current_rule, GitHubActionsPolicyRule) or not isinstance(
        desired_rule, GitHubActionsPolicyRule
    ):
        return False
    if (
        current_rule.managed_set_id is not None
        or current_rule.managed_rule_id is not None
        or current_rule.repository_id
        or current_rule.repository_owner_id
    ):
        return False
    normalized_current_rule = _normalize_authz_rule(current_rule)
    normalized_desired_rule = _normalize_authz_rule(desired_rule)
    return (
        normalized_current_rule.repository == normalized_desired_rule.repository
        and bool(normalized_current_rule.actions)
        and normalized_current_rule.actions == normalized_desired_rule.actions
        and _glob_selectors_cover(
            compatibility_values=normalized_current_rule.workflow_refs,
            managed_values=normalized_desired_rule.workflow_refs,
        )
        and _glob_selectors_cover(
            compatibility_values=normalized_current_rule.job_workflow_refs,
            managed_values=normalized_desired_rule.job_workflow_refs,
        )
        and _exact_selectors_cover(
            compatibility_values=normalized_current_rule.event_names,
            managed_values=normalized_desired_rule.event_names,
        )
        and _exact_selectors_cover(
            compatibility_values=normalized_current_rule.refs,
            managed_values=normalized_desired_rule.refs,
        )
        and _exact_selectors_cover(
            compatibility_values=normalized_current_rule.environments,
            managed_values=normalized_desired_rule.environments,
        )
        and _glob_selectors_cover(
            compatibility_values=normalized_current_rule.products,
            managed_values=normalized_desired_rule.products,
        )
        and _glob_selectors_cover(
            compatibility_values=normalized_current_rule.contexts,
            managed_values=normalized_desired_rule.contexts,
        )
        and _instance_selectors_cover(
            compatibility_values=normalized_current_rule.instances,
            managed_values=normalized_desired_rule.instances,
        )
    )


def _managed_rules_by_id(
    *, policy: LaunchplaneAuthzPolicy, managed_set_id: str
) -> dict[str, AuthzManagedRuleEntry]:
    managed_rules: dict[str, AuthzManagedRuleEntry] = {}
    for principal_type, rules in _authz_policy_rule_collections(policy):
        for rule in rules:
            if rule.managed_set_id != managed_set_id or rule.managed_rule_id is None:
                continue
            managed_rules[rule.managed_rule_id] = AuthzManagedRuleEntry(
                principal_type=principal_type,
                rule=rule,
            )
    return managed_rules


def _desired_managed_set_payload(policy: LaunchplaneAuthzPolicy) -> list[dict[str, object]]:
    payload: list[dict[str, object]] = []
    for principal_type, rules in _authz_policy_rule_collections(policy):
        for rule in rules:
            if rule.managed_rule_id is None:
                raise AuthzPolicyRequestError(
                    "Desired managed authz rules require managed_rule_id."
                )
            payload.append(
                {
                    "principal_type": principal_type,
                    "managed_rule_id": rule.managed_rule_id,
                    "rule": rule.model_dump(mode="json", exclude_none=True),
                }
            )
    return payload


def _authz_policy_without_managed_identities(
    policy: LaunchplaneAuthzPolicy,
) -> LaunchplaneAuthzPolicy:
    collections = {
        principal_type: tuple(_authz_rule_without_managed_identity(rule) for rule in rules)
        for principal_type, rules in _authz_policy_rule_collections(policy)
    }
    return LaunchplaneAuthzPolicy.model_validate(
        {"schema_version": policy.schema_version, **collections}
    )


def _authz_rule_grants_policy_administration(rule: AuthzPolicyRule) -> bool:
    if rule.actions and _AUTHZ_POLICY_ADMIN_ACTION not in rule.actions:
        return False
    if isinstance(rule, GitHubHumanPolicyRule):
        product_allowed = not rule.products or "launchplane" in rule.products
        context_allowed = not rule.contexts or "launchplane" in rule.contexts
    else:
        product_allowed = not rule.products or any(
            fnmatchcase("launchplane", value) for value in rule.products
        )
        context_allowed = not rule.contexts or any(
            fnmatchcase("launchplane", value) for value in rule.contexts
        )
    if not product_allowed or not context_allowed:
        return False
    if isinstance(rule, GitHubActionsPolicyRule):
        return bool(rule.repository and rule.workflow_refs)
    if isinstance(rule, GitHubHumanPolicyRule):
        return bool(rule.github_ids and "admin" in rule.roles)
    return bool(rule.subjects and rule.token_labels)


def _authz_policy_administrator_rules(
    policy: LaunchplaneAuthzPolicy,
) -> tuple[AuthzPolicyRule, ...]:
    return tuple(
        rule
        for _, rules in _authz_policy_rule_collections(policy)
        for rule in rules
        if _authz_rule_grants_policy_administration(rule)
    )


def _authz_policy_retains_administration(policy: LaunchplaneAuthzPolicy) -> bool:
    return bool(_authz_policy_administrator_rules(policy))


def _authz_rule_allows_identity(
    *,
    rule: AuthzPolicyRule,
    identity: AuthzApplyingIdentity,
    schema_version: Literal[1, 2],
) -> bool:
    identity_matches_rule = any(
        (
            (
                isinstance(rule, GitHubActionsPolicyRule)
                and isinstance(identity, GitHubActionsIdentity)
            ),
            (isinstance(rule, GitHubHumanPolicyRule) and isinstance(identity, GitHubHumanIdentity)),
            (
                isinstance(rule, TerminalAgentPolicyRule)
                and isinstance(identity, TerminalAgentIdentity)
            ),
            (
                isinstance(rule, LocalOperatorPolicyRule)
                and isinstance(identity, LocalOperatorIdentity)
            ),
            (isinstance(rule, LocalAdminPolicyRule) and isinstance(identity, LocalAdminIdentity)),
        )
    )
    if not identity_matches_rule:
        return False
    return bool(
        cast(Any, rule).allows(
            identity=identity,
            action=_AUTHZ_POLICY_ADMIN_ACTION,
            product="launchplane",
            context="launchplane",
            schema_version=schema_version,
        )
    )


def _is_strict_immutable_github_human_administrator_rule(
    rule: GitHubHumanPolicyRule,
) -> bool:
    return bool(
        rule.github_ids
        and "admin" in rule.roles
        and _AUTHZ_POLICY_ADMIN_ACTION in rule.actions
        and rule.products == ("launchplane",)
        and rule.contexts == ("launchplane",)
        and not rule.logins
        and not rule.organizations
        and not rule.teams
        and not rule.instances
    )


def _authz_policy_allows_immutable_github_id_administration(
    *,
    policy: LaunchplaneAuthzPolicy,
    github_id: int,
) -> bool:
    return github_id > 0 and any(
        _is_strict_immutable_github_human_administrator_rule(rule) and github_id in rule.github_ids
        for rule in policy.github_humans
    )


def _authz_policy_retains_independent_github_id_administration(
    *,
    policy: LaunchplaneAuthzPolicy,
    applying_github_id: int,
) -> bool:
    return any(
        _is_strict_immutable_github_human_administrator_rule(rule)
        and any(github_id != applying_github_id for github_id in rule.github_ids)
        for rule in policy.github_humans
    )


def _reconcile_managed_policy(
    *,
    current_policy: LaunchplaneAuthzPolicy,
    desired_policy: LaunchplaneAuthzPolicy,
    managed_set_id: str,
    unmanaged_adoption: AuthzUnmanagedAdoptionMode,
) -> tuple[
    LaunchplaneAuthzPolicy,
    tuple[AuthzManagedRuleChange, ...],
    int,
    int,
    tuple[AuthzManagedCompatibilityRetirement, ...],
]:
    current_managed_rules = _managed_rules_by_id(
        policy=current_policy,
        managed_set_id=managed_set_id,
    )
    desired_managed_rules = _managed_rules_by_id(
        policy=desired_policy,
        managed_set_id=managed_set_id,
    )
    adoption_locations: dict[AuthzRuleLocation, str] = {}
    adopted_rule_ids: set[str] = set()
    for managed_rule_id in sorted(desired_managed_rules):
        desired_entry = desired_managed_rules[managed_rule_id]
        desired_principal_type = desired_entry.principal_type
        desired_rule = desired_entry.rule
        if managed_rule_id in current_managed_rules:
            continue
        candidates: tuple[AuthzRuleLocation, ...] = tuple(
            AuthzRuleLocation(principal_type=principal_type, index=index)
            for principal_type, rules in _authz_policy_rule_collections(current_policy)
            for index, current_rule in enumerate(rules)
            if current_rule.managed_set_id is None
            and _managed_rule_adoption_matches(
                current_rule=current_rule,
                desired_rule=desired_rule,
            )
        )
        if len(candidates) > 1:
            raise AuthzPolicyConflictError(
                "Managed authz policy adoption is ambiguous because multiple unmanaged rules "
                f"match {managed_rule_id!r}."
            )
        if not candidates:
            if isinstance(desired_rule, GitHubActionsPolicyRule) and any(
                isinstance(current_rule, GitHubActionsPolicyRule)
                and current_rule.managed_set_id is None
                and _github_rule_transition_base_matches(
                    current_rule=current_rule,
                    desired_rule=desired_rule,
                )
                and bool(set(current_rule.job_workflow_refs) - set(desired_rule.job_workflow_refs))
                for _, rules in _authz_policy_rule_collections(current_policy)
                for current_rule in rules
            ):
                raise AuthzPolicyConflictError(
                    "Managed authz policy must include the active reusable workflow refs in the "
                    "reviewed overlap plan before narrowing the same managed rule."
                )
            continue
        if unmanaged_adoption != "adopt_matching":
            raise AuthzPolicyConflictError(
                "Managed authz policy would adopt an unmanaged rule; repeat the reviewed plan "
                "with unmanaged_adoption='adopt_matching'."
            )
        candidate = candidates[0]
        if candidate.principal_type != desired_principal_type or candidate in adoption_locations:
            raise AuthzPolicyConflictError(
                "Managed authz policy adoption is ambiguous across desired managed identities."
            )
        adoption_locations[candidate] = managed_rule_id
        adopted_rule_ids.add(managed_rule_id)

    retirement_matches_by_location: dict[AuthzRuleLocation, tuple[str, ...]] = {}
    retirement_locations_by_managed_rule_id: dict[str, tuple[AuthzRuleLocation, ...]] = {}
    for principal_type, rules in _authz_policy_rule_collections(current_policy):
        for index, current_rule in enumerate(rules):
            if principal_type != "github_actions" or current_rule.managed_set_id is not None:
                continue
            matching_managed_rule_ids = tuple(
                managed_rule_id
                for managed_rule_id, desired_entry in sorted(desired_managed_rules.items())
                if desired_entry.principal_type == "github_actions"
                and _managed_github_compatibility_retirement_matches(
                    current_rule=current_rule,
                    desired_rule=desired_entry.rule,
                )
            )
            if matching_managed_rule_ids:
                location = AuthzRuleLocation(principal_type=principal_type, index=index)
                retirement_matches_by_location[location] = matching_managed_rule_ids
                for managed_rule_id in matching_managed_rule_ids:
                    retirement_locations_by_managed_rule_id[managed_rule_id] = (
                        *retirement_locations_by_managed_rule_id.get(managed_rule_id, ()),
                        location,
                    )

    retirement_candidates: dict[AuthzRuleLocation, str] = {
        location: managed_rule_ids[0]
        for location, managed_rule_ids in retirement_matches_by_location.items()
        if len(managed_rule_ids) == 1
        and len(retirement_locations_by_managed_rule_id[managed_rule_ids[0]]) == 1
        and current_managed_rules.get(managed_rule_ids[0])
        == desired_managed_rules[managed_rule_ids[0]]
    }
    retirement_locations: dict[AuthzRuleLocation, str] = {}
    if unmanaged_adoption == "adopt_matching":
        ambiguous_locations = tuple(
            location
            for location, managed_rule_ids in retirement_matches_by_location.items()
            if len(managed_rule_ids) > 1
        )
        ambiguous_managed_rule_ids = tuple(
            managed_rule_id
            for managed_rule_id, locations in retirement_locations_by_managed_rule_id.items()
            if len(locations) > 1
        )
        if ambiguous_locations or ambiguous_managed_rule_ids:
            raise AuthzPolicyConflictError(
                "Managed authz compatibility retirement is ambiguous; each unmanaged rule and "
                "managed identity must have exactly one safe match."
            )
        for location, managed_rule_id in retirement_candidates.items():
            if location in adoption_locations:
                raise AuthzPolicyConflictError(
                    "Managed authz compatibility retirement conflicts with unmanaged adoption."
                )
            retirement_locations[location] = managed_rule_id

    updated_collections: dict[AuthzPrincipalType, list[AuthzPolicyRule]] = {
        principal_type: [] for principal_type in _AUTHZ_PRINCIPAL_TYPES
    }
    placed_desired_rule_ids: set[str] = set()
    compatibility_retirements: list[AuthzManagedCompatibilityRetirement] = []
    for principal_type, current_rules in _authz_policy_rule_collections(current_policy):
        for index, current_rule in enumerate(current_rules):
            if current_rule.managed_set_id == managed_set_id:
                if current_rule.managed_rule_id is None:
                    raise AuthzPolicyConflictError(
                        "Managed authz policy contains a managed rule without managed_rule_id."
                    )
                managed_rule_id = current_rule.managed_rule_id
                matching_desired_entry = desired_managed_rules.get(managed_rule_id)
                if (
                    matching_desired_entry is not None
                    and matching_desired_entry.principal_type == principal_type
                ):
                    updated_collections[principal_type].append(matching_desired_entry.rule)
                    placed_desired_rule_ids.add(managed_rule_id)
                continue
            location = AuthzRuleLocation(principal_type=principal_type, index=index)
            adopted_rule_id = adoption_locations.get(location)
            if adopted_rule_id is not None:
                updated_collections[principal_type].append(
                    desired_managed_rules[adopted_rule_id].rule
                )
                placed_desired_rule_ids.add(adopted_rule_id)
                continue
            retired_managed_rule_id = retirement_locations.get(location)
            if retired_managed_rule_id is not None:
                desired_rule = desired_managed_rules[retired_managed_rule_id].rule
                compatibility_retirements.append(
                    AuthzManagedCompatibilityRetirement(
                        managed_rule_id=retired_managed_rule_id,
                        retired_rule_sha256=_authz_rule_sha256(current_rule),
                        retained_managed_rule_sha256=_authz_rule_sha256(desired_rule),
                    )
                )
                continue
            updated_collections[principal_type].append(current_rule)

    for managed_rule_id in sorted(desired_managed_rules):
        if managed_rule_id in placed_desired_rule_ids:
            continue
        unplaced_desired_entry = desired_managed_rules[managed_rule_id]
        updated_collections[unplaced_desired_entry.principal_type].append(
            unplaced_desired_entry.rule
        )

    updated_policy = LaunchplaneAuthzPolicy.model_validate(
        {"schema_version": 2, **updated_collections}
    )
    changes: list[AuthzManagedRuleChange] = []
    unchanged_rule_count = 0
    for managed_rule_id in sorted(current_managed_rules.keys() | desired_managed_rules.keys()):
        current_entry = current_managed_rules.get(managed_rule_id)
        desired_change_entry = desired_managed_rules.get(managed_rule_id)
        if current_entry is None:
            assert desired_change_entry is not None
            changes.append(
                AuthzManagedRuleChange(
                    managed_rule_id=managed_rule_id,
                    change="adopted" if managed_rule_id in adopted_rule_ids else "added",
                    desired_principal_type=desired_change_entry.principal_type,
                    desired_rule_sha256=_authz_rule_sha256(desired_change_entry.rule),
                )
            )
            continue
        if desired_change_entry is None:
            changes.append(
                AuthzManagedRuleChange(
                    managed_rule_id=managed_rule_id,
                    change="removed",
                    previous_principal_type=current_entry.principal_type,
                    previous_rule_sha256=_authz_rule_sha256(current_entry.rule),
                )
            )
            continue
        if current_entry == desired_change_entry:
            unchanged_rule_count += 1
            continue
        changes.append(
            AuthzManagedRuleChange(
                managed_rule_id=managed_rule_id,
                change="updated",
                previous_principal_type=current_entry.principal_type,
                desired_principal_type=desired_change_entry.principal_type,
                previous_rule_sha256=_authz_rule_sha256(current_entry.rule),
                desired_rule_sha256=_authz_rule_sha256(desired_change_entry.rule),
            )
        )
    return (
        updated_policy,
        tuple(changes),
        unchanged_rule_count,
        len(retirement_candidates),
        tuple(compatibility_retirements),
    )


def plan_managed_authz_policy_reconcile(
    *,
    record_store: AuthzPolicyRecordStore,
    request: AuthzManagedPolicyReconcileEnvelope,
) -> tuple[
    LaunchplaneAuthzPolicy,
    LaunchplaneAuthzPolicyRecord,
    LaunchplaneAuthzPolicy,
    AuthzManagedPolicyDiff,
]:
    active_records = record_store.list_authz_policy_records(status="active", limit=2)
    if not active_records:
        raise ValueError("No active Launchplane authz policy record found.")
    if len(active_records) > 1:
        raise AuthzPolicyConflictError("Multiple active Launchplane authz policy records found.")
    current_record = active_records[0]
    current_policy = current_record.policy
    if current_policy.schema_version != 2 and request.schema_migration != "migrate_v1_to_v2":
        raise AuthzPolicyConflictError(
            "Managed authz policy reconciliation requires explicit "
            "schema_migration='migrate_v1_to_v2' for the active schema-v1 policy."
        )
    base_policy = migrate_authz_policy_to_schema_v2(current_policy)
    desired_collections = dict(_authz_policy_rule_collections(request.desired_policy))
    _validate_github_managed_workflow_transition(
        current_rules=dict(_authz_policy_rule_collections(base_policy))["github_actions"],
        desired_rules=desired_collections["github_actions"],
    )
    (
        updated_policy,
        changes,
        unchanged_rule_count,
        unmanaged_compatibility_candidate_count,
        compatibility_retirements,
    ) = _reconcile_managed_policy(
        current_policy=base_policy,
        desired_policy=request.desired_policy,
        managed_set_id=request.managed_set_id,
        unmanaged_adoption=request.unmanaged_adoption,
    )
    policy_safety_blockers: tuple[AuthzManagedPolicySafetyBlocker, ...] = ()
    if _authz_policy_retains_administration(
        base_policy
    ) and not _authz_policy_retains_administration(updated_policy):
        policy_safety_blockers = (_managed_policy_safety_blocker("authz_policy_admin_unreachable"),)
    desired_policy_sha256 = authz_policy_sha256(updated_policy)
    changed = current_record.policy_sha256 != desired_policy_sha256
    desired_set_payload = _desired_managed_set_payload(request.desired_policy)
    desired_set_sha256 = hashlib.sha256(
        json.dumps(
            desired_set_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    candidate_revision = current_record.revision + int(changed)
    plan_payload = {
        "contract_version": 2,
        "managed_set_id": request.managed_set_id,
        "observed_record_id": current_record.record_id,
        "observed_revision": current_record.revision,
        "observed_policy_sha256": current_record.policy_sha256,
        "candidate_revision": candidate_revision,
        "candidate_policy_sha256": desired_policy_sha256,
        "desired_set": desired_set_payload,
        "desired_set_sha256": desired_set_sha256,
        "schema_migration": request.schema_migration,
        "unmanaged_adoption": request.unmanaged_adoption,
        "reason": request.reason,
        "related_issue": request.related_issue,
    }
    plan_sha256 = hashlib.sha256(
        json.dumps(plan_payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if request.mode == "apply" and request.reviewed_plan_sha256 != plan_sha256:
        raise AuthzPolicyConflictError(
            "Managed authz policy reviewed_plan_sha256 no longer matches the active policy "
            "and desired managed rule set."
        )
    operational_readiness_blockers = _managed_operational_readiness_blockers(
        desired_policy=request.desired_policy,
        managed_set_id=request.managed_set_id,
    )
    diff = AuthzManagedPolicyDiff(
        managed_set_id=request.managed_set_id,
        previous_record_id=current_record.record_id,
        previous_revision=current_record.revision,
        candidate_revision=candidate_revision,
        previous_policy_sha256=current_record.policy_sha256,
        desired_policy_sha256=desired_policy_sha256,
        desired_set_sha256=desired_set_sha256,
        plan_sha256=plan_sha256,
        schema_migrated=current_policy.schema_version != 2,
        changed=changed,
        authorization_changed=(
            authz_policy_sha256(_authz_policy_without_managed_identities(base_policy))
            != authz_policy_sha256(_authz_policy_without_managed_identities(updated_policy))
        ),
        added_rule_count=sum(change.change == "added" for change in changes),
        adopted_rule_count=sum(change.change == "adopted" for change in changes),
        updated_rule_count=sum(change.change == "updated" for change in changes),
        removed_rule_count=sum(change.change == "removed" for change in changes),
        unchanged_rule_count=unchanged_rule_count,
        unmanaged_compatibility_candidate_count=unmanaged_compatibility_candidate_count,
        retired_unmanaged_compatibility_rule_count=len(compatibility_retirements),
        retired_unmanaged_compatibility_rules=compatibility_retirements,
        policy_safety_blocker_count=len(policy_safety_blockers),
        policy_safety_blockers=policy_safety_blockers,
        operational_readiness_blocked_rule_count=len(operational_readiness_blockers),
        operational_readiness_blockers=operational_readiness_blockers,
        changes=changes,
    )
    return current_policy, current_record, updated_policy, diff


def build_authz_managed_policy_reconcile_service_result(
    *,
    authz_policy_record: LaunchplaneAuthzPolicyRecord,
    changed: bool,
    mode: Literal["dry_run", "apply"],
    diff: dict[str, object],
    audit: dict[str, object],
) -> tuple[dict[str, object], dict[str, object]]:
    result: dict[str, object] = {
        "authz_policy_record_id": authz_policy_record.record_id,
        "authz_policy_changed": str(changed).lower(),
    }
    driver_result = {
        "authz_policy": summarize_authz_policy_record(authz_policy_record),
        "changed": changed,
        "mode": mode,
        "diff": diff,
        "audit": authz_managed_policy_reconcile_response_audit_payload(audit),
    }
    return result, driver_result


def authz_managed_policy_reconcile_response_audit_payload(
    audit: dict[str, object],
) -> dict[str, object]:
    response_audit = dict(audit)
    operator = response_audit.get("operator")
    if isinstance(operator, dict):
        response_audit["operator"] = {"type": str(operator.get("type") or "unknown")}
    return response_audit


def _dry_run_authz_policy_record(
    *, current_record: LaunchplaneAuthzPolicyRecord, audit: dict[str, object]
) -> LaunchplaneAuthzPolicyRecord:
    return LaunchplaneAuthzPolicyRecord(
        record_id=current_record.record_id,
        revision=current_record.revision,
        status=current_record.status,
        source=current_record.source,
        updated_at=current_record.updated_at,
        policy_sha256=current_record.policy_sha256,
        policy=current_record.policy,
        audit=audit,
    )


def authz_managed_policy_reconcile_audit_payload(
    *,
    request: AuthzManagedPolicyReconcileEnvelope,
    identity: AuthzApplyingIdentity,
    previous_record: LaunchplaneAuthzPolicyRecord,
    new_record: LaunchplaneAuthzPolicyRecord | None,
    diff: AuthzManagedPolicyDiff,
    trace_id: str,
    now_timestamp: TimestampProvider,
) -> dict[str, object]:
    return {
        "operation": "managed_rule_set_reconcile",
        "source": _MANAGED_AUTHZ_RECONCILE_SOURCE,
        "mode": request.mode,
        "reason": request.reason,
        "related_issue": request.related_issue,
        "managed_set_id": request.managed_set_id,
        "schema_migration": request.schema_migration,
        "unmanaged_adoption": request.unmanaged_adoption,
        "desired_set_sha256": diff.desired_set_sha256,
        "plan_sha256": diff.plan_sha256,
        "operator": authz_policy_operator_payload(identity),
        "previous_policy_record_id": previous_record.record_id,
        "previous_revision": previous_record.revision,
        "previous_policy_sha256": previous_record.policy_sha256,
        "new_policy_record_id": new_record.record_id
        if new_record is not None
        else previous_record.record_id,
        "new_revision": new_record.revision if new_record is not None else diff.candidate_revision,
        "new_policy_sha256": new_record.policy_sha256
        if new_record is not None
        else diff.desired_policy_sha256,
        "changed": diff.changed,
        "diff": diff.model_dump(mode="json"),
        "trace_id": trace_id,
        "updated_at": new_record.updated_at if new_record is not None else now_timestamp(),
    }


def execute_managed_authz_policy_reconcile(
    *,
    record_store: AuthzPolicyRecordStore,
    request: AuthzManagedPolicyReconcileEnvelope,
    identity: AuthzApplyingIdentity,
    trace_id: str,
    now_timestamp: TimestampProvider,
    authorized_policy_sha256: str = "",
    immutable_applying_github_id: int = 0,
) -> AuthzManagedPolicyRouteResult:
    active_records = record_store.list_authz_policy_records(status="active", limit=1)
    if not active_records:
        raise ValueError("No active Launchplane authz policy record found.")
    _require_expected_authz_policy(
        current_record=active_records[0],
        expected_policy_sha256=authorized_policy_sha256,
    )
    current_policy, current_record, updated_policy, managed_diff = (
        plan_managed_authz_policy_reconcile(
            record_store=record_store,
            request=request,
        )
    )
    _require_expected_authz_policy(
        current_record=current_record,
        expected_policy_sha256=authorized_policy_sha256,
    )
    policy_safety_blockers = list(managed_diff.policy_safety_blockers)
    applying_admin_retained = (
        _authz_policy_allows_immutable_github_id_administration(
            policy=updated_policy,
            github_id=immutable_applying_github_id,
        )
        if immutable_applying_github_id
        else updated_policy.allows(
            identity=identity,
            action=_AUTHZ_POLICY_ADMIN_ACTION,
            product=request.product,
            context="launchplane",
        )
    )
    if managed_diff.changed and not applying_admin_retained:
        policy_safety_blockers.append(
            _managed_policy_safety_blocker("authz_policy_applying_admin_removed")
        )
    independent_admin_retained = _authz_policy_retains_independent_github_id_administration(
        policy=updated_policy,
        applying_github_id=immutable_applying_github_id,
    )
    if managed_diff.changed and not independent_admin_retained:
        policy_safety_blockers.append(
            _managed_policy_safety_blocker("authz_policy_independent_admin_unreachable")
        )
    managed_diff = managed_diff.model_copy(
        update={
            "policy_safety_blocker_count": len(policy_safety_blockers),
            "policy_safety_blockers": tuple(policy_safety_blockers),
        }
    )
    if request.mode == "apply" and managed_diff.changed:
        blockers_by_code = {
            blocker.code: blocker for blocker in managed_diff.policy_safety_blockers
        }
        for code in (
            "authz_policy_admin_unreachable",
            "authz_policy_applying_admin_removed",
        ):
            blocker = blockers_by_code.get(code)
            if blocker is not None:
                raise AuthzPolicySafetyError(code=blocker.code, message=blocker.message)
    if (
        request.mode == "apply"
        and managed_diff.changed
        and managed_diff.operational_readiness_blocked_rule_count
    ):
        raise AuthzPolicySafetyError(
            code="authz_operational_readiness_blocked",
            message=(
                "Managed authz policy reconciliation cannot apply while operational-readiness "
                "blockers remain. Review the dry-run evidence and submit an exact candidate."
            ),
        )
    if request.mode == "apply" and managed_diff.changed:
        independent_admin_blocker = next(
            (
                blocker
                for blocker in managed_diff.policy_safety_blockers
                if blocker.code == "authz_policy_independent_admin_unreachable"
            ),
            None,
        )
        if independent_admin_blocker is not None:
            raise AuthzPolicySafetyError(
                code=independent_admin_blocker.code,
                message=independent_admin_blocker.message,
            )
    diff = managed_diff.model_dump(mode="json")
    audit = authz_managed_policy_reconcile_audit_payload(
        request=request,
        identity=identity,
        previous_record=current_record,
        new_record=None,
        diff=managed_diff,
        trace_id=trace_id,
        now_timestamp=now_timestamp,
    )
    authz_policy_record = _dry_run_authz_policy_record(
        current_record=current_record,
        audit=audit,
    )
    changed = managed_diff.changed
    if request.mode == "apply" and changed:
        updated_at = now_timestamp()
        authz_policy_record = LaunchplaneAuthzPolicyRecord(
            record_id=build_authz_policy_record_id(
                revision=managed_diff.candidate_revision,
                policy_sha256=managed_diff.desired_policy_sha256,
            ),
            revision=managed_diff.candidate_revision,
            status="active",
            source=_MANAGED_AUTHZ_RECONCILE_SOURCE,
            updated_at=updated_at,
            policy_sha256=managed_diff.desired_policy_sha256,
            policy=updated_policy,
            audit=audit,
        )
        audit = authz_managed_policy_reconcile_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=authz_policy_record,
            diff=managed_diff,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        )
        authz_policy_record.audit = audit
    result, driver_result = build_authz_managed_policy_reconcile_service_result(
        authz_policy_record=authz_policy_record,
        changed=changed,
        mode=request.mode,
        diff=diff,
        audit=audit,
    )
    return AuthzManagedPolicyRouteResult(
        updated_policy=updated_policy,
        previous_authz_policy_record=current_record,
        authz_policy_record=authz_policy_record,
        changed=changed,
        result=result,
        driver_result=driver_result,
    )
