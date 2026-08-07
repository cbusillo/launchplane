from __future__ import annotations

from collections.abc import Callable
from fnmatch import fnmatchcase
import hashlib
import json
import re
from typing import Any, Literal, Protocol, cast

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
from control_plane.contracts.owner_acceptance import (
    OWNER_ACCEPTANCE_EVENT_WRITE_ACTION,
    OWNER_ACCEPTANCE_READ_ACTION,
)
from control_plane.service_auth import (
    AuthzInstanceSelectors,
    GitHubActionsIdentity,
    GitHubActionsPolicyRule,
    GitHubHumanPolicyRule,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
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


AuthzPolicyRule = (
    GitHubActionsPolicyRule
    | GitHubHumanPolicyRule
    | TerminalAgentPolicyRule
    | LocalOperatorPolicyRule
    | LocalAdminPolicyRule
)
AuthzPrincipalType = Literal[
    "github_actions",
    "github_humans",
    "terminal_agents",
    "local_operators",
    "local_admins",
]


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
                key=lambda rule: str(rule.managed_rule_id),
            )
        )
        for principal_type, rules in _authz_policy_rule_collections(policy)
    }
    return LaunchplaneAuthzPolicy(
        schema_version=policy.schema_version,
        github_actions=cast(
            tuple[GitHubActionsPolicyRule, ...],
            normalized_collections["github_actions"],
        ),
        github_humans=cast(
            tuple[GitHubHumanPolicyRule, ...],
            normalized_collections["github_humans"],
        ),
        terminal_agents=cast(
            tuple[TerminalAgentPolicyRule, ...],
            normalized_collections["terminal_agents"],
        ),
        local_operators=cast(
            tuple[LocalOperatorPolicyRule, ...],
            normalized_collections["local_operators"],
        ),
        local_admins=cast(
            tuple[LocalAdminPolicyRule, ...],
            normalized_collections["local_admins"],
        ),
    )


_MANAGED_AUTHZ_SET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._:/-]{0,127}$")
_IMMUTABLE_JOB_WORKFLOW_REF_PATTERN = re.compile(r"^[^@]+/\.github/workflows/[^@]+@[0-9a-f]{40}$")
_WORKFLOW_REF_GLOB_CHARACTERS = frozenset("*?[")
_IMMUTABLE_WORKFLOW_ACTION_SAFETIES = frozenset(
    {"prod", "destructive", "secret_backed", "policy_admin"}
)
_MANAGED_AUTHZ_RECONCILE_SOURCE = "service:authz-managed-rule-set-reconcile"
_AUTHZ_POLICY_ADMIN_ACTION = "authz_policy_grant.write"
_OWNER_ACCEPTANCE_MANAGED_SET_ID = "operator.owner-acceptance"
_OWNER_ACCEPTANCE_ACTIONS = frozenset(
    {OWNER_ACCEPTANCE_READ_ACTION, OWNER_ACCEPTANCE_EVENT_WRITE_ACTION}
)
AuthzSchemaMigrationMode = Literal["reject", "migrate_v1_to_v2"]
AuthzUnmanagedAdoptionMode = Literal["reject", "adopt_matching"]


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
        if rule.roles != ("read_only",):
            raise ValueError("Owner Acceptance managed authz rules require the read_only role.")
        if rule.products != ("launchplane",) or rule.contexts != ("owner-acceptance",):
            raise ValueError(
                "Owner Acceptance managed authz rules require the exact Launchplane workbench scope."
            )
        if rule.instances:
            raise ValueError(
                "Owner Acceptance managed authz rules cannot declare instance selectors."
            )
        if frozenset(rule.actions) != _OWNER_ACCEPTANCE_ACTIONS:
            raise ValueError(
                "Owner Acceptance managed authz rules require only the read and event-write actions."
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
        for principal_type, rules in _authz_policy_rule_collections(self.desired_policy):
            for rule in rules:
                if rule.managed_set_id != self.managed_set_id or not rule.managed_rule_id:
                    raise ValueError(
                        "Every desired managed authz rule must declare the request managed_set_id "
                        f"and a managed_rule_id ({principal_type})."
                    )
                if not rule.actions:
                    raise ValueError(
                        "Every desired managed authz rule must declare at least one action "
                        f"({principal_type}:{rule.managed_rule_id})."
                    )
                if isinstance(rule, GitHubActionsPolicyRule) and (
                    not rule.repository_id or not rule.repository_owner_id
                ):
                    raise ValueError(
                        "Managed GitHub Actions authz rules require immutable repository_id "
                        f"and repository_owner_id selectors ({rule.managed_rule_id})."
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


AuthzManagedRuleChangeKind = Literal["added", "adopted", "updated", "removed"]


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


AuthzOperationalReadinessBlockerCode = Literal[
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
]


class AuthzManagedOperationalReadinessBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    managed_rule_id: str
    actions: tuple[str, ...]
    reason_codes: tuple[AuthzOperationalReadinessBlockerCode, ...]


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
    operational_readiness_blocked_rule_count: int = 0
    operational_readiness_blockers: tuple[AuthzManagedOperationalReadinessBlocker, ...] = ()
    changes: tuple[AuthzManagedRuleChange, ...] = ()


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


def authz_policy_operator_payload(identity: LaunchplaneIdentity) -> dict[str, object]:
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


class AuthzPolicyRequestError(ValueError):
    pass


def _authz_rule_sha256(rule: AuthzPolicyRule) -> str:
    canonical_json = json.dumps(
        rule.model_dump(mode="json", exclude_none=True),
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()


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
        blockers.append(
            AuthzManagedOperationalReadinessBlocker(
                managed_rule_id=str(rule.managed_rule_id),
                actions=matching_actions,
                reason_codes=reason_codes,
            )
        )
    return tuple(blockers)


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
        if not desired_rule.workflow_refs or any(
            _contains_selector_glob(workflow_ref) for workflow_ref in desired_rule.workflow_refs
        ):
            raise AuthzPolicyRequestError(
                "High-privilege managed GitHub Actions rules require exact caller workflow_refs "
                f"({desired_rule.managed_rule_id})."
            )
        if not desired_rule.job_workflow_refs:
            raise AuthzPolicyRequestError(
                "High-privilege managed GitHub Actions rules require an exact reviewed reusable "
                f"workflow identity ({desired_rule.managed_rule_id})."
            )
        if not any(
            is_immutable_job_workflow_ref(job_workflow_ref)
            for job_workflow_ref in desired_rule.job_workflow_refs
        ):
            raise AuthzPolicyRequestError(
                "High-privilege managed GitHub Actions rules require at least one reusable "
                "workflow identity pinned to a full commit SHA "
                f"({desired_rule.managed_rule_id})."
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
                    f"new refs must use a full commit SHA ({desired_rule.managed_rule_id})."
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
    normalized_current_rule = cast(GitHubActionsPolicyRule, _normalize_authz_rule(current_rule))
    normalized_desired_rule = cast(GitHubActionsPolicyRule, _normalize_authz_rule(desired_rule))
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
) -> dict[str, tuple[AuthzPrincipalType, AuthzPolicyRule]]:
    return {
        str(rule.managed_rule_id): (principal_type, rule)
        for principal_type, rules in _authz_policy_rule_collections(policy)
        for rule in rules
        if rule.managed_set_id == managed_set_id and rule.managed_rule_id is not None
    }


def _desired_managed_set_payload(policy: LaunchplaneAuthzPolicy) -> list[dict[str, object]]:
    return [
        {
            "principal_type": principal_type,
            "managed_rule_id": str(rule.managed_rule_id),
            "rule": rule.model_dump(mode="json", exclude_none=True),
        }
        for principal_type, rules in _authz_policy_rule_collections(policy)
        for rule in rules
    ]


def _authz_policy_without_managed_identities(
    policy: LaunchplaneAuthzPolicy,
) -> LaunchplaneAuthzPolicy:
    collections = {
        principal_type: tuple(_authz_rule_without_managed_identity(rule) for rule in rules)
        for principal_type, rules in _authz_policy_rule_collections(policy)
    }
    return LaunchplaneAuthzPolicy(
        schema_version=policy.schema_version,
        github_actions=cast(tuple[GitHubActionsPolicyRule, ...], collections["github_actions"]),
        github_humans=cast(tuple[GitHubHumanPolicyRule, ...], collections["github_humans"]),
        terminal_agents=cast(tuple[TerminalAgentPolicyRule, ...], collections["terminal_agents"]),
        local_operators=cast(tuple[LocalOperatorPolicyRule, ...], collections["local_operators"]),
        local_admins=cast(tuple[LocalAdminPolicyRule, ...], collections["local_admins"]),
    )


def _authz_policy_retains_administration(policy: LaunchplaneAuthzPolicy) -> bool:
    def grants_policy_administration(rule: object) -> bool:
        actions = getattr(rule, "actions", ())
        products = getattr(rule, "products", ())
        contexts = getattr(rule, "contexts", ())
        return (
            (not actions or _AUTHZ_POLICY_ADMIN_ACTION in actions)
            and (not products or "launchplane" in products)
            and (not contexts or "launchplane" in contexts)
        )

    return (
        any(grants_policy_administration(rule) for rule in policy.github_actions)
        or any(
            grants_policy_administration(rule) and (not rule.roles or "admin" in rule.roles)
            for rule in policy.github_humans
        )
        or any(grants_policy_administration(rule) for rule in policy.local_operators)
        or any(grants_policy_administration(rule) for rule in policy.local_admins)
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
    current_collections = dict(_authz_policy_rule_collections(current_policy))
    adoption_locations: dict[tuple[AuthzPrincipalType, int], str] = {}
    adopted_rule_ids: set[str] = set()
    for managed_rule_id, (desired_principal_type, desired_rule) in sorted(
        desired_managed_rules.items()
    ):
        if managed_rule_id in current_managed_rules:
            continue
        candidates = tuple(
            (principal_type, index)
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
        if candidate[0] != desired_principal_type or candidate in adoption_locations:
            raise AuthzPolicyConflictError(
                "Managed authz policy adoption is ambiguous across desired managed identities."
            )
        adoption_locations[candidate] = managed_rule_id
        adopted_rule_ids.add(managed_rule_id)

    retirement_matches_by_location: dict[tuple[AuthzPrincipalType, int], tuple[str, ...]] = {}
    retirement_locations_by_managed_rule_id: dict[
        str, tuple[tuple[AuthzPrincipalType, int], ...]
    ] = {}
    for principal_type, rules in _authz_policy_rule_collections(current_policy):
        for index, current_rule in enumerate(rules):
            if principal_type != "github_actions" or current_rule.managed_set_id is not None:
                continue
            matching_managed_rule_ids = tuple(
                managed_rule_id
                for managed_rule_id, (desired_principal_type, desired_rule) in sorted(
                    desired_managed_rules.items()
                )
                if desired_principal_type == "github_actions"
                and _managed_github_compatibility_retirement_matches(
                    current_rule=current_rule,
                    desired_rule=desired_rule,
                )
            )
            if matching_managed_rule_ids:
                retirement_matches_by_location[(principal_type, index)] = matching_managed_rule_ids
                for managed_rule_id in matching_managed_rule_ids:
                    retirement_locations_by_managed_rule_id[managed_rule_id] = (
                        *retirement_locations_by_managed_rule_id.get(managed_rule_id, ()),
                        (principal_type, index),
                    )

    retirement_candidates = {
        location: managed_rule_ids[0]
        for location, managed_rule_ids in retirement_matches_by_location.items()
        if len(managed_rule_ids) == 1
        and len(retirement_locations_by_managed_rule_id[managed_rule_ids[0]]) == 1
        and current_managed_rules.get(managed_rule_ids[0])
        == desired_managed_rules[managed_rule_ids[0]]
    }
    retirement_locations: dict[tuple[AuthzPrincipalType, int], str] = {}
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
        principal_type: [] for principal_type in current_collections
    }
    placed_desired_rule_ids: set[str] = set()
    compatibility_retirements: list[AuthzManagedCompatibilityRetirement] = []
    for principal_type, current_rules in _authz_policy_rule_collections(current_policy):
        for index, current_rule in enumerate(current_rules):
            if current_rule.managed_set_id == managed_set_id:
                managed_rule_id = str(current_rule.managed_rule_id)
                desired_entry = desired_managed_rules.get(managed_rule_id)
                if desired_entry is not None and desired_entry[0] == principal_type:
                    updated_collections[principal_type].append(desired_entry[1])
                    placed_desired_rule_ids.add(managed_rule_id)
                continue
            adopted_rule_id = adoption_locations.get((principal_type, index))
            if adopted_rule_id is not None:
                updated_collections[principal_type].append(
                    desired_managed_rules[adopted_rule_id][1]
                )
                placed_desired_rule_ids.add(adopted_rule_id)
                continue
            retired_managed_rule_id = retirement_locations.get((principal_type, index))
            if retired_managed_rule_id is not None:
                desired_rule = desired_managed_rules[retired_managed_rule_id][1]
                compatibility_retirements.append(
                    AuthzManagedCompatibilityRetirement(
                        managed_rule_id=retired_managed_rule_id,
                        retired_rule_sha256=_authz_rule_sha256(current_rule),
                        retained_managed_rule_sha256=_authz_rule_sha256(desired_rule),
                    )
                )
                continue
            updated_collections[principal_type].append(current_rule)

    for managed_rule_id, (principal_type, desired_rule) in sorted(desired_managed_rules.items()):
        if managed_rule_id in placed_desired_rule_ids:
            continue
        updated_collections[principal_type].append(desired_rule)

    updated_policy = LaunchplaneAuthzPolicy(
        schema_version=2,
        github_actions=cast(
            tuple[GitHubActionsPolicyRule, ...],
            tuple(updated_collections["github_actions"]),
        ),
        github_humans=cast(
            tuple[GitHubHumanPolicyRule, ...],
            tuple(updated_collections["github_humans"]),
        ),
        terminal_agents=cast(
            tuple[TerminalAgentPolicyRule, ...],
            tuple(updated_collections["terminal_agents"]),
        ),
        local_operators=cast(
            tuple[LocalOperatorPolicyRule, ...],
            tuple(updated_collections["local_operators"]),
        ),
        local_admins=cast(
            tuple[LocalAdminPolicyRule, ...],
            tuple(updated_collections["local_admins"]),
        ),
    )
    changes: list[AuthzManagedRuleChange] = []
    unchanged_rule_count = 0
    for managed_rule_id in sorted(current_managed_rules.keys() | desired_managed_rules.keys()):
        current_entry = current_managed_rules.get(managed_rule_id)
        desired_entry = desired_managed_rules.get(managed_rule_id)
        if current_entry is None:
            assert desired_entry is not None
            changes.append(
                AuthzManagedRuleChange(
                    managed_rule_id=managed_rule_id,
                    change="adopted" if managed_rule_id in adopted_rule_ids else "added",
                    desired_principal_type=desired_entry[0],
                    desired_rule_sha256=_authz_rule_sha256(desired_entry[1]),
                )
            )
            continue
        if desired_entry is None:
            changes.append(
                AuthzManagedRuleChange(
                    managed_rule_id=managed_rule_id,
                    change="removed",
                    previous_principal_type=current_entry[0],
                    previous_rule_sha256=_authz_rule_sha256(current_entry[1]),
                )
            )
            continue
        if current_entry == desired_entry:
            unchanged_rule_count += 1
            continue
        changes.append(
            AuthzManagedRuleChange(
                managed_rule_id=managed_rule_id,
                change="updated",
                previous_principal_type=current_entry[0],
                desired_principal_type=desired_entry[0],
                previous_rule_sha256=_authz_rule_sha256(current_entry[1]),
                desired_rule_sha256=_authz_rule_sha256(desired_entry[1]),
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
    if _authz_policy_retains_administration(
        base_policy
    ) and not _authz_policy_retains_administration(updated_policy):
        raise AuthzPolicyConflictError(
            "Managed authz policy reconciliation must retain at least one principal that can "
            "administer Launchplane authz policy."
        )
    desired_policy_sha256 = authz_policy_sha256(updated_policy)
    changed = current_record.policy_sha256 != desired_policy_sha256
    desired_set_payload = _desired_managed_set_payload(request.desired_policy)
    desired_set_sha256 = hashlib.sha256(
        json.dumps(
            desired_set_payload,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
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
        json.dumps(plan_payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
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
    identity: LaunchplaneIdentity,
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
    identity: LaunchplaneIdentity,
    trace_id: str,
    now_timestamp: TimestampProvider,
    authorized_policy_sha256: str = "",
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
    if managed_diff.retired_unmanaged_compatibility_rule_count and not updated_policy.allows(
        identity=identity,
        action=_AUTHZ_POLICY_ADMIN_ACTION,
        product=request.product,
        context="launchplane",
    ):
        raise AuthzPolicyConflictError(
            "Managed authz compatibility retirement must retain policy administration "
            "authority for the applying identity."
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
