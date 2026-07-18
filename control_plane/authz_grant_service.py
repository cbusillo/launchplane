from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
import re
from typing import Any, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.authz_scope import (
    exclusively_instance_scoped_authz_actions,
    instance_scoped_authz_actions,
)
from control_plane.contracts.authz_policy_record import (
    AuthzPolicyCompareWriteResult,
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.service_auth import (
    AuthzInstanceSelectors,
    AuthzPolicySchemaVersion,
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


def _validate_instance_scoped_grant(
    *,
    schema_version: AuthzPolicySchemaVersion,
    actions: tuple[str, ...],
    instances: tuple[str, ...],
) -> None:
    if schema_version == 1:
        if instances:
            raise ValueError("Schema-v1 authz grants cannot declare instances.")
        return
    requested_actions = set(actions)
    instance_actions = instance_scoped_authz_actions()
    exclusively_instance_actions = exclusively_instance_scoped_authz_actions()
    if requested_actions & exclusively_instance_actions and not instances:
        raise ValueError("Schema-v2 instance-scoped authz grants require instances.")
    non_instance_actions = requested_actions - instance_actions
    if instances and non_instance_actions:
        raise ValueError(
            "Schema-v2 authz grants can only declare instances for instance-scoped actions."
        )


class AuthzPolicyRecordStore(Protocol):
    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]: ...

    def compare_and_write_authz_policy_record(
        self,
        *,
        expected_record: LaunchplaneAuthzPolicyRecord,
        replacement_record: LaunchplaneAuthzPolicyRecord | None,
    ) -> AuthzPolicyCompareWriteResult: ...


def _require_expected_authz_policy(
    *,
    current_record: LaunchplaneAuthzPolicyRecord,
    expected_policy_sha256: str,
) -> None:
    if expected_policy_sha256 and current_record.policy_sha256 != expected_policy_sha256:
        raise AuthzPolicyConflictError(
            "Launchplane active authz policy changed after the caller was authorized."
        )


class AuthzPolicyGitHubActionsGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    repository_id: str = ""
    repository_owner_id: str = ""
    workflow_refs: tuple[str, ...] = ()
    job_workflow_refs: tuple[str, ...] = ()
    event_names: tuple[str, ...] = ()
    refs: tuple[str, ...] = ()
    environments: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()
    instances: AuthzInstanceSelectors = ()
    actions: tuple[str, ...]
    source_label: str = "service:authz-policy-grant"

    @staticmethod
    def _normalized_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value.strip() for value in values if value.strip())

    @model_validator(mode="after")
    def _validate_grant(self) -> "AuthzPolicyGitHubActionsGrant":
        self.repository = self.repository.strip()
        if not self.repository:
            raise ValueError("Authz policy grant requires repository.")
        self.repository_id = self.repository_id.strip()
        self.repository_owner_id = self.repository_owner_id.strip()
        if bool(self.repository_id) != bool(self.repository_owner_id):
            raise ValueError(
                "Authz policy grant requires both repository_id and repository_owner_id "
                "when either immutable identifier is declared."
            )
        for label, value in (
            ("repository_id", self.repository_id),
            ("repository_owner_id", self.repository_owner_id),
        ):
            if value and not value.isdecimal():
                raise ValueError(f"Authz policy grant {label} must be a numeric GitHub ID.")
        self.workflow_refs = self._normalized_tuple(self.workflow_refs)
        self.job_workflow_refs = self._normalized_tuple(self.job_workflow_refs)
        self.event_names = self._normalized_tuple(self.event_names)
        self.refs = self._normalized_tuple(self.refs)
        self.environments = self._normalized_tuple(self.environments)
        self.products = self._normalized_tuple(self.products)
        self.contexts = self._normalized_tuple(self.contexts)
        self.instances = self._normalized_tuple(self.instances)
        self.actions = self._normalized_tuple(self.actions)
        if not self.actions:
            raise ValueError("Authz policy grant requires at least one action.")
        self.source_label = self.source_label.strip() or "service:authz-policy-grant"
        return self

    def to_policy_rule(self) -> GitHubActionsPolicyRule:
        return GitHubActionsPolicyRule(
            repository=self.repository,
            repository_id=self.repository_id,
            repository_owner_id=self.repository_owner_id,
            workflow_refs=self.workflow_refs,
            job_workflow_refs=self.job_workflow_refs,
            event_names=self.event_names,
            refs=self.refs,
            environments=self.environments,
            products=self.products,
            contexts=self.contexts,
            instances=self.instances,
            actions=self.actions,
        )


class AuthzPolicyGitHubActionsRemoval(AuthzPolicyGitHubActionsGrant):
    repository_id: str = ""
    repository_owner_id: str = ""


class AuthzPolicyGitHubActionsGrantEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: AuthzPolicySchemaVersion = 1
    product: str
    mode: Literal["dry_run", "apply"] = "apply"
    reason: str = ""
    related_issue: str = ""
    grant: AuthzPolicyGitHubActionsGrant

    @model_validator(mode="after")
    def _validate_alignment(self) -> "AuthzPolicyGitHubActionsGrantEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Authz policy grant writes require product 'launchplane'.")
        self.product = "launchplane"
        self.reason = self.reason.strip()
        self.related_issue = self.related_issue.strip()
        if self.mode == "apply" and not self.reason:
            raise ValueError("Authz policy grant apply requires reason.")
        if not self.grant.repository_id or not self.grant.repository_owner_id:
            raise ValueError(
                "GitHub Actions authz grants require immutable repository_id and "
                "repository_owner_id selectors."
            )
        _validate_instance_scoped_grant(
            schema_version=self.schema_version,
            actions=self.grant.actions,
            instances=self.grant.instances,
        )
        return self


class AuthzPolicyGitHubActionsRemovalEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: AuthzPolicySchemaVersion = 1
    product: str
    mode: Literal["dry_run", "apply"] = "dry_run"
    reason: str = ""
    related_issue: str = ""
    removal: AuthzPolicyGitHubActionsRemoval

    @model_validator(mode="after")
    def _validate_alignment(self) -> "AuthzPolicyGitHubActionsRemovalEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Authz policy removals require product 'launchplane'.")
        self.product = "launchplane"
        self.reason = self.reason.strip()
        self.related_issue = self.related_issue.strip()
        if self.mode == "apply" and not self.reason:
            raise ValueError("Authz policy removal apply requires reason.")
        if self.schema_version == 1 and (
            self.removal.repository_id or self.removal.repository_owner_id
        ):
            raise ValueError(
                "Schema-v1 authz policy removals cannot declare immutable repository IDs."
            )
        _validate_instance_scoped_grant(
            schema_version=self.schema_version,
            actions=self.removal.actions,
            instances=self.removal.instances,
        )
        return self


class AuthzPolicyGitHubHumanGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    logins: tuple[str, ...] = ()
    organizations: tuple[str, ...] = ()
    teams: tuple[str, ...] = ()
    roles: tuple[Literal["read_only", "admin"], ...] = ()
    products: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()
    instances: AuthzInstanceSelectors = ()
    actions: tuple[str, ...]
    source_label: str = "service:authz-human-policy-grant"

    @staticmethod
    def _normalized_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value.strip() for value in values if value.strip())

    @model_validator(mode="after")
    def _validate_grant(self) -> "AuthzPolicyGitHubHumanGrant":
        self.logins = self._normalized_tuple(self.logins)
        self.organizations = self._normalized_tuple(self.organizations)
        self.teams = self._normalized_tuple(self.teams)
        self.products = self._normalized_tuple(self.products)
        self.contexts = self._normalized_tuple(self.contexts)
        self.instances = self._normalized_tuple(self.instances)
        self.actions = self._normalized_tuple(self.actions)
        if not (self.logins or self.organizations or self.teams):
            raise ValueError("Authz human policy grant requires a login, organization, or team.")
        if not self.actions:
            raise ValueError("Authz human policy grant requires at least one action.")
        self.source_label = self.source_label.strip() or "service:authz-human-policy-grant"
        return self

    def to_policy_rule(self) -> GitHubHumanPolicyRule:
        return GitHubHumanPolicyRule(
            logins=self.logins,
            organizations=self.organizations,
            teams=self.teams,
            roles=self.roles,
            products=self.products,
            contexts=self.contexts,
            instances=self.instances,
            actions=self.actions,
        )


class AuthzPolicyGitHubHumanGrantEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: AuthzPolicySchemaVersion = 1
    product: str
    mode: Literal["dry_run", "apply"] = "apply"
    reason: str = ""
    related_issue: str = ""
    grant: AuthzPolicyGitHubHumanGrant

    @model_validator(mode="after")
    def _validate_alignment(self) -> "AuthzPolicyGitHubHumanGrantEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Authz human policy grant writes require product 'launchplane'.")
        self.product = "launchplane"
        self.reason = self.reason.strip()
        self.related_issue = self.related_issue.strip()
        if self.mode == "apply" and not self.reason:
            raise ValueError("Authz human policy grant apply requires reason.")
        _validate_instance_scoped_grant(
            schema_version=self.schema_version,
            actions=self.grant.actions,
            instances=self.grant.instances,
        )
        return self


class AuthzPolicyTerminalAgentGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subjects: tuple[str, ...] = ()
    token_labels: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()
    instances: AuthzInstanceSelectors = ()
    actions: tuple[str, ...]
    source_label: str = "service:authz-terminal-agent-policy-grant"

    @staticmethod
    def _normalized_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value.strip() for value in values if value.strip())

    @model_validator(mode="after")
    def _validate_grant(self) -> "AuthzPolicyTerminalAgentGrant":
        self.subjects = self._normalized_tuple(self.subjects)
        self.token_labels = self._normalized_tuple(self.token_labels)
        self.products = self._normalized_tuple(self.products)
        self.contexts = self._normalized_tuple(self.contexts)
        self.instances = self._normalized_tuple(self.instances)
        self.actions = self._normalized_tuple(self.actions)
        if not self.subjects:
            raise ValueError("Authz terminal-agent policy grant requires a subject.")
        if not self.token_labels:
            raise ValueError("Authz terminal-agent policy grant requires a token label.")
        if not self.actions:
            raise ValueError("Authz terminal-agent policy grant requires at least one action.")
        self.source_label = self.source_label.strip() or "service:authz-terminal-agent-policy-grant"
        return self

    def to_policy_rule(self) -> TerminalAgentPolicyRule:
        return TerminalAgentPolicyRule(
            subjects=self.subjects,
            token_labels=self.token_labels,
            products=self.products,
            contexts=self.contexts,
            instances=self.instances,
            actions=self.actions,
        )


class AuthzPolicyTerminalAgentGrantEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: AuthzPolicySchemaVersion = 1
    product: str
    mode: Literal["dry_run", "apply"] = "apply"
    reason: str = ""
    related_issue: str = ""
    grant: AuthzPolicyTerminalAgentGrant

    @model_validator(mode="after")
    def _validate_alignment(self) -> "AuthzPolicyTerminalAgentGrantEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError(
                "Authz terminal-agent policy grant writes require product 'launchplane'."
            )
        self.product = "launchplane"
        self.reason = self.reason.strip()
        self.related_issue = self.related_issue.strip()
        if self.mode == "apply" and not self.reason:
            raise ValueError("Authz terminal-agent policy grant apply requires reason.")
        _validate_instance_scoped_grant(
            schema_version=self.schema_version,
            actions=self.grant.actions,
            instances=self.grant.instances,
        )
        return self


class AuthzPolicyLocalOperatorGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subjects: tuple[str, ...] = ()
    token_labels: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()
    instances: AuthzInstanceSelectors = ()
    actions: tuple[str, ...]
    source_label: str = "service:authz-local-operator-policy-grant"

    @staticmethod
    def _normalized_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value.strip() for value in values if value.strip())

    @model_validator(mode="after")
    def _validate_grant(self) -> "AuthzPolicyLocalOperatorGrant":
        self.subjects = self._normalized_tuple(self.subjects)
        self.token_labels = self._normalized_tuple(self.token_labels)
        self.products = self._normalized_tuple(self.products)
        self.contexts = self._normalized_tuple(self.contexts)
        self.instances = self._normalized_tuple(self.instances)
        self.actions = self._normalized_tuple(self.actions)
        if not self.subjects:
            raise ValueError("Authz local-operator policy grant requires a subject.")
        if not self.token_labels:
            raise ValueError("Authz local-operator policy grant requires a token label.")
        if not self.actions:
            raise ValueError("Authz local-operator policy grant requires at least one action.")
        self.source_label = self.source_label.strip() or "service:authz-local-operator-policy-grant"
        return self

    def to_policy_rule(self) -> LocalOperatorPolicyRule:
        return LocalOperatorPolicyRule(
            subjects=self.subjects,
            token_labels=self.token_labels,
            products=self.products,
            contexts=self.contexts,
            instances=self.instances,
            actions=self.actions,
        )


class AuthzPolicyLocalOperatorGrantEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: AuthzPolicySchemaVersion = 1
    product: str
    mode: Literal["dry_run", "apply"] = "apply"
    reason: str = ""
    related_issue: str = ""
    grant: AuthzPolicyLocalOperatorGrant

    @model_validator(mode="after")
    def _validate_alignment(self) -> "AuthzPolicyLocalOperatorGrantEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError(
                "Authz local-operator policy grant writes require product 'launchplane'."
            )
        self.product = "launchplane"
        self.reason = self.reason.strip()
        self.related_issue = self.related_issue.strip()
        if self.mode == "apply" and not self.reason:
            raise ValueError("Authz local-operator policy grant apply requires reason.")
        _validate_instance_scoped_grant(
            schema_version=self.schema_version,
            actions=self.grant.actions,
            instances=self.grant.instances,
        )
        return self


class AuthzPolicyLocalAdminGrant(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subjects: tuple[str, ...] = ()
    token_labels: tuple[str, ...] = ()
    products: tuple[str, ...] = ()
    contexts: tuple[str, ...] = ()
    instances: AuthzInstanceSelectors = ()
    actions: tuple[str, ...]
    source_label: str = "service:authz-local-admin-policy-grant"

    @staticmethod
    def _normalized_tuple(values: tuple[str, ...]) -> tuple[str, ...]:
        return tuple(value.strip() for value in values if value.strip())

    @model_validator(mode="after")
    def _validate_grant(self) -> "AuthzPolicyLocalAdminGrant":
        self.subjects = self._normalized_tuple(self.subjects)
        self.token_labels = self._normalized_tuple(self.token_labels)
        self.products = self._normalized_tuple(self.products)
        self.contexts = self._normalized_tuple(self.contexts)
        self.instances = self._normalized_tuple(self.instances)
        self.actions = self._normalized_tuple(self.actions)
        if not self.subjects:
            raise ValueError("Authz local-admin policy grant requires a subject.")
        if not self.token_labels:
            raise ValueError("Authz local-admin policy grant requires a token label.")
        if not self.actions:
            raise ValueError("Authz local-admin policy grant requires at least one action.")
        self.source_label = self.source_label.strip() or "service:authz-local-admin-policy-grant"
        return self

    def to_policy_rule(self) -> LocalAdminPolicyRule:
        return LocalAdminPolicyRule(
            subjects=self.subjects,
            token_labels=self.token_labels,
            products=self.products,
            contexts=self.contexts,
            instances=self.instances,
            actions=self.actions,
        )


class AuthzPolicyLocalAdminGrantEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: AuthzPolicySchemaVersion = 1
    product: str
    mode: Literal["dry_run", "apply"] = "apply"
    reason: str = ""
    related_issue: str = ""
    grant: AuthzPolicyLocalAdminGrant

    @model_validator(mode="after")
    def _validate_alignment(self) -> "AuthzPolicyLocalAdminGrantEnvelope":
        if self.product.strip() != "launchplane":
            raise ValueError("Authz local-admin policy grant writes require product 'launchplane'.")
        self.product = "launchplane"
        self.reason = self.reason.strip()
        self.related_issue = self.related_issue.strip()
        if self.mode == "apply" and not self.reason:
            raise ValueError("Authz local-admin policy grant apply requires reason.")
        _validate_instance_scoped_grant(
            schema_version=self.schema_version,
            actions=self.grant.actions,
            instances=self.grant.instances,
        )
        return self


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
AuthzSchemaMigrationMode = Literal["reject", "migrate_v1_to_v2"]
AuthzUnmanagedAdoptionMode = Literal["reject", "adopt_matching"]


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


AuthzPolicyGrant = (
    AuthzPolicyGitHubActionsGrant
    | AuthzPolicyGitHubHumanGrant
    | AuthzPolicyTerminalAgentGrant
    | AuthzPolicyLocalOperatorGrant
    | AuthzPolicyLocalAdminGrant
)
AuthzPolicyGrantEnvelope = (
    AuthzPolicyGitHubActionsGrantEnvelope
    | AuthzPolicyGitHubHumanGrantEnvelope
    | AuthzPolicyTerminalAgentGrantEnvelope
    | AuthzPolicyLocalOperatorGrantEnvelope
    | AuthzPolicyLocalAdminGrantEnvelope
)
AuthzPolicyRouteEnvelope = (
    AuthzPolicyGrantEnvelope
    | AuthzPolicyGitHubActionsRemovalEnvelope
    | AuthzManagedPolicyReconcileEnvelope
)


class AuthzPolicyRouteResult(BaseModel):
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
    summary["managed_rules"] = [
        {
            "managed_set_id": rule.managed_set_id,
            "managed_rule_id": rule.managed_rule_id,
            "principal_type": principal_type,
            "rule_sha256": _authz_rule_sha256(rule),
        }
        for principal_type, rules in _authz_policy_rule_collections(record.policy)
        for rule in rules
        if rule.managed_set_id is not None and rule.managed_rule_id is not None
    ]
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


def _github_actions_grant_match_counts(
    *,
    current_policy: LaunchplaneAuthzPolicy,
    desired_rule: GitHubActionsPolicyRule,
) -> tuple[int, int]:
    exact_match_count = sum(1 for rule in current_policy.github_actions if rule == desired_rule)
    if not desired_rule.repository_id:
        return exact_match_count, 0
    legacy_rule = desired_rule.model_copy(update={"repository_id": "", "repository_owner_id": ""})
    legacy_match_count = sum(1 for rule in current_policy.github_actions if rule == legacy_rule)
    return exact_match_count, legacy_match_count


def authz_policy_grant_diff(
    *, current_policy: LaunchplaneAuthzPolicy, grant: AuthzPolicyGrant
) -> dict[str, object]:
    desired_rule = grant.to_policy_rule()
    exact_match_count = 0
    legacy_match_count = 0
    github_actions_count_delta = 0
    if isinstance(grant, AuthzPolicyGitHubHumanGrant):
        changed = not any(rule == desired_rule for rule in current_policy.github_humans)
    elif isinstance(grant, AuthzPolicyTerminalAgentGrant):
        changed = not any(rule == desired_rule for rule in current_policy.terminal_agents)
    elif isinstance(grant, AuthzPolicyLocalOperatorGrant):
        changed = not any(rule == desired_rule for rule in current_policy.local_operators)
    elif isinstance(grant, AuthzPolicyLocalAdminGrant):
        changed = not any(rule == desired_rule for rule in current_policy.local_admins)
    else:
        assert isinstance(desired_rule, GitHubActionsPolicyRule)
        exact_match_count, legacy_match_count = _github_actions_grant_match_counts(
            current_policy=current_policy,
            desired_rule=desired_rule,
        )
        if desired_rule.repository_id:
            changed = exact_match_count != 1 or legacy_match_count > 0
            if changed:
                github_actions_count_delta = 1 - exact_match_count - legacy_match_count
        else:
            changed = exact_match_count == 0
            if changed:
                github_actions_count_delta = 1
    return {
        "changed": changed,
        "matched_exact_github_actions_rule_count": exact_match_count,
        "upgraded_legacy_github_actions_rule_count": legacy_match_count,
        "previous_github_actions_rule_count": len(current_policy.github_actions),
        "new_github_actions_rule_count": len(current_policy.github_actions)
        + github_actions_count_delta,
        "previous_github_humans_rule_count": len(current_policy.github_humans),
        "new_github_humans_rule_count": len(current_policy.github_humans)
        + int(changed and isinstance(grant, AuthzPolicyGitHubHumanGrant)),
        "previous_terminal_agents_rule_count": len(current_policy.terminal_agents),
        "new_terminal_agents_rule_count": len(current_policy.terminal_agents)
        + int(changed and isinstance(grant, AuthzPolicyTerminalAgentGrant)),
        "previous_local_operators_rule_count": len(current_policy.local_operators),
        "new_local_operators_rule_count": len(current_policy.local_operators)
        + int(changed and isinstance(grant, AuthzPolicyLocalOperatorGrant)),
        "previous_local_admins_rule_count": len(current_policy.local_admins),
        "new_local_admins_rule_count": len(current_policy.local_admins)
        + int(changed and isinstance(grant, AuthzPolicyLocalAdminGrant)),
    }


def authz_policy_github_actions_removal_diff(
    *, current_policy: LaunchplaneAuthzPolicy, removal: AuthzPolicyGitHubActionsRemoval
) -> dict[str, object]:
    desired_rule = removal.to_policy_rule()
    matched_rule_count = sum(1 for rule in current_policy.github_actions if rule == desired_rule)
    changed = matched_rule_count > 0
    return {
        "changed": changed,
        "matched_rule_count": matched_rule_count,
        "removed_rule_count": matched_rule_count if changed else 0,
        "previous_github_actions_rule_count": len(current_policy.github_actions),
        "new_github_actions_rule_count": len(current_policy.github_actions) - matched_rule_count,
        "previous_github_humans_rule_count": len(current_policy.github_humans),
        "new_github_humans_rule_count": len(current_policy.github_humans),
        "previous_terminal_agents_rule_count": len(current_policy.terminal_agents),
        "new_terminal_agents_rule_count": len(current_policy.terminal_agents),
        "previous_local_operators_rule_count": len(current_policy.local_operators),
        "new_local_operators_rule_count": len(current_policy.local_operators),
        "previous_local_admins_rule_count": len(current_policy.local_admins),
        "new_local_admins_rule_count": len(current_policy.local_admins),
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
        action_safety(action) in _IMMUTABLE_WORKFLOW_ACTION_SAFETIES for action in rule.actions
    ) or any(instance in {"*", "prod"} for instance in rule.instances)


def _contains_workflow_ref_glob(value: str) -> bool:
    return any(character in value for character in _WORKFLOW_REF_GLOB_CHARACTERS)


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
            _contains_workflow_ref_glob(workflow_ref) for workflow_ref in desired_rule.workflow_refs
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
            _IMMUTABLE_JOB_WORKFLOW_REF_PATTERN.fullmatch(job_workflow_ref)
            for job_workflow_ref in desired_rule.job_workflow_refs
        ):
            raise AuthzPolicyRequestError(
                "High-privilege managed GitHub Actions rules require at least one reusable "
                "workflow identity pinned to a full commit SHA "
                f"({desired_rule.managed_rule_id})."
            )
        for job_workflow_ref in desired_rule.job_workflow_refs:
            if _IMMUTABLE_JOB_WORKFLOW_REF_PATTERN.fullmatch(job_workflow_ref):
                continue
            if _contains_workflow_ref_glob(job_workflow_ref) or not any(
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
    return (
        _github_rule_transition_base_matches(
            current_rule=current_rule,
            desired_rule=desired_rule,
        )
        and set(current_rule.job_workflow_refs).issubset(desired_rule.job_workflow_refs)
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
) -> tuple[LaunchplaneAuthzPolicy, tuple[AuthzManagedRuleChange, ...], int]:
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

    updated_collections: dict[AuthzPrincipalType, list[AuthzPolicyRule]] = {
        principal_type: [] for principal_type in current_collections
    }
    placed_desired_rule_ids: set[str] = set()
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
    return updated_policy, tuple(changes), unchanged_rule_count


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
    updated_policy, changes, unchanged_rule_count = _reconcile_managed_policy(
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
        "contract_version": 1,
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
        changes=changes,
    )
    return current_policy, current_record, updated_policy, diff


def authz_policy_grant_audit_payload(
    *,
    request: AuthzPolicyGrantEnvelope,
    identity: LaunchplaneIdentity,
    previous_record: LaunchplaneAuthzPolicyRecord,
    new_record: LaunchplaneAuthzPolicyRecord | None,
    changed: bool,
    trace_id: str,
    now_timestamp: TimestampProvider,
) -> dict[str, object]:
    principal_type = "github_actions"
    if isinstance(request.grant, AuthzPolicyGitHubHumanGrant):
        principal_type = "github_human"
    elif isinstance(request.grant, AuthzPolicyTerminalAgentGrant):
        principal_type = "terminal_agent"
    elif isinstance(request.grant, AuthzPolicyLocalOperatorGrant):
        principal_type = "local_operator"
    elif isinstance(request.grant, AuthzPolicyLocalAdminGrant):
        principal_type = "local_admin"
    return {
        "mode": request.mode,
        "reason": request.reason,
        "related_issue": request.related_issue,
        "principal_type": principal_type,
        "operator": authz_policy_operator_payload(identity),
        "requested_grant": request.grant.to_policy_rule().model_dump(mode="json"),
        "previous_policy_record_id": previous_record.record_id,
        "previous_policy_sha256": previous_record.policy_sha256,
        "new_policy_record_id": new_record.record_id
        if new_record is not None
        else previous_record.record_id,
        "new_policy_sha256": new_record.policy_sha256
        if new_record is not None
        else previous_record.policy_sha256,
        "changed": changed,
        "trace_id": trace_id,
        "updated_at": new_record.updated_at if new_record is not None else now_timestamp(),
    }


def authz_policy_github_actions_removal_audit_payload(
    *,
    request: AuthzPolicyGitHubActionsRemovalEnvelope,
    identity: LaunchplaneIdentity,
    previous_record: LaunchplaneAuthzPolicyRecord,
    new_record: LaunchplaneAuthzPolicyRecord | None,
    changed: bool,
    trace_id: str,
    now_timestamp: TimestampProvider,
) -> dict[str, object]:
    return {
        "mode": request.mode,
        "reason": request.reason,
        "related_issue": request.related_issue,
        "principal_type": "github_actions",
        "operator": authz_policy_operator_payload(identity),
        "requested_removal": request.removal.to_policy_rule().model_dump(mode="json"),
        "previous_policy_record_id": previous_record.record_id,
        "previous_policy_sha256": previous_record.policy_sha256,
        "new_policy_record_id": new_record.record_id
        if new_record is not None
        else previous_record.record_id,
        "new_policy_sha256": new_record.policy_sha256
        if new_record is not None
        else previous_record.policy_sha256,
        "changed": changed,
        "trace_id": trace_id,
        "updated_at": new_record.updated_at if new_record is not None else now_timestamp(),
    }


def authz_policy_grant_response_audit_payload(
    audit: dict[str, object],
) -> dict[str, object]:
    response_audit = dict(audit)
    operator = response_audit.get("operator")
    if isinstance(operator, dict):
        response_audit["operator"] = {"type": str(operator.get("type") or "unknown")}
    requested_grant = response_audit.pop("requested_grant", None)
    if isinstance(requested_grant, dict):
        if "repository" in requested_grant:
            response_audit["requested_grant_summary"] = {
                "principal_type": "github_actions",
                "repository": requested_grant.get("repository", ""),
                "repository_id": requested_grant.get("repository_id", ""),
                "repository_owner_id": requested_grant.get("repository_owner_id", ""),
                "workflow_ref_count": len(requested_grant.get("workflow_refs") or ()),
                "job_workflow_ref_count": len(requested_grant.get("job_workflow_refs") or ()),
                "event_names": requested_grant.get("event_names") or (),
                "products": requested_grant.get("products") or (),
                "contexts": requested_grant.get("contexts") or (),
                "instances": requested_grant.get("instances") or (),
                "actions": requested_grant.get("actions") or (),
            }
        elif "subjects" in requested_grant or "token_labels" in requested_grant:
            principal_type = str(response_audit.get("principal_type") or "terminal_agent")
            response_audit["requested_grant_summary"] = {
                "principal_type": principal_type,
                "subject_count": len(requested_grant.get("subjects") or ()),
                "token_label_count": len(requested_grant.get("token_labels") or ()),
                "products": requested_grant.get("products") or (),
                "contexts": requested_grant.get("contexts") or (),
                "instances": requested_grant.get("instances") or (),
                "actions": requested_grant.get("actions") or (),
            }
        else:
            response_audit["requested_grant_summary"] = {
                "principal_type": "github_human",
                "login_count": len(requested_grant.get("logins") or ()),
                "organization_count": len(requested_grant.get("organizations") or ()),
                "team_count": len(requested_grant.get("teams") or ()),
                "roles": requested_grant.get("roles") or (),
                "products": requested_grant.get("products") or (),
                "contexts": requested_grant.get("contexts") or (),
                "instances": requested_grant.get("instances") or (),
                "actions": requested_grant.get("actions") or (),
            }
    return response_audit


def authz_policy_github_actions_removal_response_audit_payload(
    audit: dict[str, object],
) -> dict[str, object]:
    response_audit = dict(audit)
    operator = response_audit.get("operator")
    if isinstance(operator, dict):
        response_audit["operator"] = {"type": str(operator.get("type") or "unknown")}
    requested_removal = response_audit.pop("requested_removal", None)
    if isinstance(requested_removal, dict):
        response_audit["requested_removal_summary"] = {
            "principal_type": "github_actions",
            "repository": requested_removal.get("repository", ""),
            "repository_id": requested_removal.get("repository_id", ""),
            "repository_owner_id": requested_removal.get("repository_owner_id", ""),
            "workflow_ref_count": len(requested_removal.get("workflow_refs") or ()),
            "job_workflow_ref_count": len(requested_removal.get("job_workflow_refs") or ()),
            "event_names": requested_removal.get("event_names") or (),
            "products": requested_removal.get("products") or (),
            "contexts": requested_removal.get("contexts") or (),
            "instances": requested_removal.get("instances") or (),
            "actions": requested_removal.get("actions") or (),
        }
    return response_audit


def plan_github_actions_authz_policy_grant(
    *,
    record_store: AuthzPolicyRecordStore,
    grant: AuthzPolicyGitHubActionsGrant,
) -> tuple[LaunchplaneAuthzPolicy, LaunchplaneAuthzPolicyRecord, dict[str, object]]:
    active_records = record_store.list_authz_policy_records(status="active", limit=1)
    if not active_records:
        raise ValueError("No active Launchplane authz policy record found.")
    current_record = active_records[0]
    current_policy = current_record.policy
    return (
        current_policy,
        current_record,
        authz_policy_grant_diff(
            current_policy=current_policy,
            grant=grant,
        ),
    )


def plan_github_actions_authz_policy_removal(
    *,
    record_store: AuthzPolicyRecordStore,
    removal: AuthzPolicyGitHubActionsRemoval,
) -> tuple[LaunchplaneAuthzPolicy, LaunchplaneAuthzPolicyRecord, dict[str, object]]:
    active_records = record_store.list_authz_policy_records(status="active", limit=1)
    if not active_records:
        raise ValueError("No active Launchplane authz policy record found.")
    current_record = active_records[0]
    current_policy = current_record.policy
    diff = authz_policy_github_actions_removal_diff(
        current_policy=current_policy,
        removal=removal,
    )
    if bool(diff["changed"]):
        desired_rule = removal.to_policy_rule()
        updated_policy = current_policy.model_copy(
            update={
                "github_actions": tuple(
                    rule for rule in current_policy.github_actions if rule != desired_rule
                )
            }
        )
        if _authz_policy_retains_administration(
            current_policy
        ) and not _authz_policy_retains_administration(updated_policy):
            raise AuthzPolicyConflictError(
                "Authz policy removal must retain at least one principal that can administer "
                "Launchplane authz policy."
            )
    return current_policy, current_record, diff


def plan_github_human_authz_policy_grant(
    *,
    record_store: AuthzPolicyRecordStore,
    grant: AuthzPolicyGitHubHumanGrant,
) -> tuple[LaunchplaneAuthzPolicy, LaunchplaneAuthzPolicyRecord, dict[str, object]]:
    active_records = record_store.list_authz_policy_records(status="active", limit=1)
    if not active_records:
        raise ValueError("No active Launchplane authz policy record found.")
    current_record = active_records[0]
    current_policy = current_record.policy
    return (
        current_policy,
        current_record,
        authz_policy_grant_diff(
            current_policy=current_policy,
            grant=grant,
        ),
    )


def plan_terminal_agent_authz_policy_grant(
    *,
    record_store: AuthzPolicyRecordStore,
    grant: AuthzPolicyTerminalAgentGrant,
) -> tuple[LaunchplaneAuthzPolicy, LaunchplaneAuthzPolicyRecord, dict[str, object]]:
    active_records = record_store.list_authz_policy_records(status="active", limit=1)
    if not active_records:
        raise ValueError("No active Launchplane authz policy record found.")
    current_record = active_records[0]
    current_policy = current_record.policy
    return (
        current_policy,
        current_record,
        authz_policy_grant_diff(
            current_policy=current_policy,
            grant=grant,
        ),
    )


def plan_local_operator_authz_policy_grant(
    *,
    record_store: AuthzPolicyRecordStore,
    grant: AuthzPolicyLocalOperatorGrant,
) -> tuple[LaunchplaneAuthzPolicy, LaunchplaneAuthzPolicyRecord, dict[str, object]]:
    active_records = record_store.list_authz_policy_records(status="active", limit=1)
    if not active_records:
        raise ValueError("No active Launchplane authz policy record found.")
    current_record = active_records[0]
    current_policy = current_record.policy
    return (
        current_policy,
        current_record,
        authz_policy_grant_diff(
            current_policy=current_policy,
            grant=grant,
        ),
    )


def plan_local_admin_authz_policy_grant(
    *,
    record_store: AuthzPolicyRecordStore,
    grant: AuthzPolicyLocalAdminGrant,
) -> tuple[LaunchplaneAuthzPolicy, LaunchplaneAuthzPolicyRecord, dict[str, object]]:
    active_records = record_store.list_authz_policy_records(status="active", limit=1)
    if not active_records:
        raise ValueError("No active Launchplane authz policy record found.")
    current_record = active_records[0]
    current_policy = current_record.policy
    return (
        current_policy,
        current_record,
        authz_policy_grant_diff(
            current_policy=current_policy,
            grant=grant,
        ),
    )


def _compare_and_write_authz_policy_record(
    *,
    record_store: AuthzPolicyRecordStore,
    current_record: LaunchplaneAuthzPolicyRecord,
    replacement_record: LaunchplaneAuthzPolicyRecord,
) -> None:
    write_result = record_store.compare_and_write_authz_policy_record(
        expected_record=current_record,
        replacement_record=replacement_record,
    )
    if write_result.status != "written":
        raise AuthzPolicyConflictError(
            "Launchplane authz policy changed before the requested update could be written."
        )


def write_github_actions_authz_policy_grant(
    *,
    record_store: AuthzPolicyRecordStore,
    request: AuthzPolicyGitHubActionsGrantEnvelope,
    identity: LaunchplaneIdentity,
    trace_id: str,
    now_timestamp: TimestampProvider,
    expected_policy_sha256: str = "",
) -> tuple[
    LaunchplaneAuthzPolicy,
    LaunchplaneAuthzPolicyRecord,
    bool,
    dict[str, object],
    dict[str, object],
]:
    current_policy, current_record, diff = plan_github_actions_authz_policy_grant(
        record_store=record_store,
        grant=request.grant,
    )
    _require_expected_authz_policy(
        current_record=current_record,
        expected_policy_sha256=expected_policy_sha256,
    )
    changed = bool(diff["changed"])
    desired_rule = request.grant.to_policy_rule()
    if not changed:
        audit = authz_policy_grant_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=False,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        )
        return current_policy, current_record, False, diff, audit

    legacy_rule = desired_rule.model_copy(update={"repository_id": "", "repository_owner_id": ""})
    updated_policy = current_policy.model_copy(
        update={
            "github_actions": tuple(
                rule
                for rule in current_policy.github_actions
                if rule != desired_rule and rule != legacy_rule
            )
            + (desired_rule,)
        }
    )
    updated_at = now_timestamp()
    policy_sha256 = authz_policy_sha256(updated_policy)
    record = LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            revision=current_record.revision + 1,
            policy_sha256=policy_sha256,
        ),
        revision=current_record.revision + 1,
        status="active",
        source=request.grant.source_label,
        updated_at=updated_at,
        policy_sha256=policy_sha256,
        policy=updated_policy,
        audit=authz_policy_grant_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=True,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        ),
    )
    record.audit = authz_policy_grant_audit_payload(
        request=request,
        identity=identity,
        previous_record=current_record,
        new_record=record,
        changed=True,
        trace_id=trace_id,
        now_timestamp=now_timestamp,
    )
    _compare_and_write_authz_policy_record(
        record_store=record_store,
        current_record=current_record,
        replacement_record=record,
    )
    return updated_policy, record, changed, diff, record.audit


def write_github_actions_authz_policy_removal(
    *,
    record_store: AuthzPolicyRecordStore,
    request: AuthzPolicyGitHubActionsRemovalEnvelope,
    identity: LaunchplaneIdentity,
    trace_id: str,
    now_timestamp: TimestampProvider,
    expected_policy_sha256: str = "",
) -> tuple[
    LaunchplaneAuthzPolicy,
    LaunchplaneAuthzPolicyRecord,
    bool,
    dict[str, object],
    dict[str, object],
]:
    current_policy, current_record, diff = plan_github_actions_authz_policy_removal(
        record_store=record_store,
        removal=request.removal,
    )
    _require_expected_authz_policy(
        current_record=current_record,
        expected_policy_sha256=expected_policy_sha256,
    )
    changed = bool(diff["changed"])
    desired_rule = request.removal.to_policy_rule()
    if not changed:
        audit = authz_policy_github_actions_removal_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=False,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        )
        return current_policy, current_record, False, diff, audit

    updated_policy = current_policy.model_copy(
        update={
            "github_actions": tuple(
                rule for rule in current_policy.github_actions if rule != desired_rule
            )
        }
    )
    updated_at = now_timestamp()
    policy_sha256 = authz_policy_sha256(updated_policy)
    record = LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            revision=current_record.revision + 1,
            policy_sha256=policy_sha256,
        ),
        revision=current_record.revision + 1,
        status="active",
        source=request.removal.source_label,
        updated_at=updated_at,
        policy_sha256=policy_sha256,
        policy=updated_policy,
        audit=authz_policy_github_actions_removal_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=True,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        ),
    )
    record.audit = authz_policy_github_actions_removal_audit_payload(
        request=request,
        identity=identity,
        previous_record=current_record,
        new_record=record,
        changed=True,
        trace_id=trace_id,
        now_timestamp=now_timestamp,
    )
    _compare_and_write_authz_policy_record(
        record_store=record_store,
        current_record=current_record,
        replacement_record=record,
    )
    return updated_policy, record, changed, diff, record.audit


def write_github_human_authz_policy_grant(
    *,
    record_store: AuthzPolicyRecordStore,
    request: AuthzPolicyGitHubHumanGrantEnvelope,
    identity: LaunchplaneIdentity,
    trace_id: str,
    now_timestamp: TimestampProvider,
    expected_policy_sha256: str = "",
) -> tuple[
    LaunchplaneAuthzPolicy,
    LaunchplaneAuthzPolicyRecord,
    bool,
    dict[str, object],
    dict[str, object],
]:
    current_policy, current_record, diff = plan_github_human_authz_policy_grant(
        record_store=record_store,
        grant=request.grant,
    )
    _require_expected_authz_policy(
        current_record=current_record,
        expected_policy_sha256=expected_policy_sha256,
    )
    changed = bool(diff["changed"])
    desired_rule = request.grant.to_policy_rule()
    if not changed:
        audit = authz_policy_grant_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=False,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        )
        return current_policy, current_record, False, diff, audit

    updated_policy = current_policy.model_copy(
        update={"github_humans": current_policy.github_humans + (desired_rule,)}
    )
    updated_at = now_timestamp()
    policy_sha256 = authz_policy_sha256(updated_policy)
    record = LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            revision=current_record.revision + 1,
            policy_sha256=policy_sha256,
        ),
        revision=current_record.revision + 1,
        status="active",
        source=request.grant.source_label,
        updated_at=updated_at,
        policy_sha256=policy_sha256,
        policy=updated_policy,
        audit=authz_policy_grant_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=True,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        ),
    )
    record.audit = authz_policy_grant_audit_payload(
        request=request,
        identity=identity,
        previous_record=current_record,
        new_record=record,
        changed=True,
        trace_id=trace_id,
        now_timestamp=now_timestamp,
    )
    _compare_and_write_authz_policy_record(
        record_store=record_store,
        current_record=current_record,
        replacement_record=record,
    )
    return updated_policy, record, changed, diff, record.audit


def write_terminal_agent_authz_policy_grant(
    *,
    record_store: AuthzPolicyRecordStore,
    request: AuthzPolicyTerminalAgentGrantEnvelope,
    identity: LaunchplaneIdentity,
    trace_id: str,
    now_timestamp: TimestampProvider,
    expected_policy_sha256: str = "",
) -> tuple[
    LaunchplaneAuthzPolicy,
    LaunchplaneAuthzPolicyRecord,
    bool,
    dict[str, object],
    dict[str, object],
]:
    current_policy, current_record, diff = plan_terminal_agent_authz_policy_grant(
        record_store=record_store,
        grant=request.grant,
    )
    _require_expected_authz_policy(
        current_record=current_record,
        expected_policy_sha256=expected_policy_sha256,
    )
    changed = bool(diff["changed"])
    desired_rule = request.grant.to_policy_rule()
    if not changed:
        audit = authz_policy_grant_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=False,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        )
        return current_policy, current_record, False, diff, audit

    updated_policy = current_policy.model_copy(
        update={"terminal_agents": current_policy.terminal_agents + (desired_rule,)}
    )
    updated_at = now_timestamp()
    policy_sha256 = authz_policy_sha256(updated_policy)
    record = LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            revision=current_record.revision + 1,
            policy_sha256=policy_sha256,
        ),
        revision=current_record.revision + 1,
        status="active",
        source=request.grant.source_label,
        updated_at=updated_at,
        policy_sha256=policy_sha256,
        policy=updated_policy,
        audit=authz_policy_grant_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=True,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        ),
    )
    record.audit = authz_policy_grant_audit_payload(
        request=request,
        identity=identity,
        previous_record=current_record,
        new_record=record,
        changed=True,
        trace_id=trace_id,
        now_timestamp=now_timestamp,
    )
    _compare_and_write_authz_policy_record(
        record_store=record_store,
        current_record=current_record,
        replacement_record=record,
    )
    return updated_policy, record, changed, diff, record.audit


def write_local_operator_authz_policy_grant(
    *,
    record_store: AuthzPolicyRecordStore,
    request: AuthzPolicyLocalOperatorGrantEnvelope,
    identity: LaunchplaneIdentity,
    trace_id: str,
    now_timestamp: TimestampProvider,
    expected_policy_sha256: str = "",
) -> tuple[
    LaunchplaneAuthzPolicy,
    LaunchplaneAuthzPolicyRecord,
    bool,
    dict[str, object],
    dict[str, object],
]:
    current_policy, current_record, diff = plan_local_operator_authz_policy_grant(
        record_store=record_store,
        grant=request.grant,
    )
    _require_expected_authz_policy(
        current_record=current_record,
        expected_policy_sha256=expected_policy_sha256,
    )
    changed = bool(diff["changed"])
    desired_rule = request.grant.to_policy_rule()
    if not changed:
        audit = authz_policy_grant_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=False,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        )
        return current_policy, current_record, False, diff, audit

    updated_policy = current_policy.model_copy(
        update={"local_operators": current_policy.local_operators + (desired_rule,)}
    )
    updated_at = now_timestamp()
    policy_sha256 = authz_policy_sha256(updated_policy)
    record = LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            revision=current_record.revision + 1,
            policy_sha256=policy_sha256,
        ),
        revision=current_record.revision + 1,
        status="active",
        source=request.grant.source_label,
        updated_at=updated_at,
        policy_sha256=policy_sha256,
        policy=updated_policy,
        audit=authz_policy_grant_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=True,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        ),
    )
    record.audit = authz_policy_grant_audit_payload(
        request=request,
        identity=identity,
        previous_record=current_record,
        new_record=record,
        changed=True,
        trace_id=trace_id,
        now_timestamp=now_timestamp,
    )
    _compare_and_write_authz_policy_record(
        record_store=record_store,
        current_record=current_record,
        replacement_record=record,
    )
    return updated_policy, record, changed, diff, record.audit


def write_local_admin_authz_policy_grant(
    *,
    record_store: AuthzPolicyRecordStore,
    request: AuthzPolicyLocalAdminGrantEnvelope,
    identity: LaunchplaneIdentity,
    trace_id: str,
    now_timestamp: TimestampProvider,
    expected_policy_sha256: str = "",
) -> tuple[
    LaunchplaneAuthzPolicy,
    LaunchplaneAuthzPolicyRecord,
    bool,
    dict[str, object],
    dict[str, object],
]:
    current_policy, current_record, diff = plan_local_admin_authz_policy_grant(
        record_store=record_store,
        grant=request.grant,
    )
    _require_expected_authz_policy(
        current_record=current_record,
        expected_policy_sha256=expected_policy_sha256,
    )
    changed = bool(diff["changed"])
    desired_rule = request.grant.to_policy_rule()
    if not changed:
        audit = authz_policy_grant_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=False,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        )
        return current_policy, current_record, False, diff, audit

    updated_policy = current_policy.model_copy(
        update={"local_admins": current_policy.local_admins + (desired_rule,)}
    )
    updated_at = now_timestamp()
    policy_sha256 = authz_policy_sha256(updated_policy)
    record = LaunchplaneAuthzPolicyRecord(
        record_id=build_authz_policy_record_id(
            revision=current_record.revision + 1,
            policy_sha256=policy_sha256,
        ),
        revision=current_record.revision + 1,
        status="active",
        source=request.grant.source_label,
        updated_at=updated_at,
        policy_sha256=policy_sha256,
        policy=updated_policy,
        audit=authz_policy_grant_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=True,
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        ),
    )
    record.audit = authz_policy_grant_audit_payload(
        request=request,
        identity=identity,
        previous_record=current_record,
        new_record=record,
        changed=True,
        trace_id=trace_id,
        now_timestamp=now_timestamp,
    )
    _compare_and_write_authz_policy_record(
        record_store=record_store,
        current_record=current_record,
        replacement_record=record,
    )
    return updated_policy, record, changed, diff, record.audit


def build_authz_policy_grant_service_result(
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
        "audit": authz_policy_grant_response_audit_payload(audit),
    }
    return result, driver_result


def build_authz_policy_github_actions_removal_service_result(
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
        "audit": authz_policy_github_actions_removal_response_audit_payload(audit),
    }
    return result, driver_result


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


def execute_authz_policy_route(
    *,
    record_store: AuthzPolicyRecordStore,
    request: AuthzPolicyRouteEnvelope,
    identity: LaunchplaneIdentity,
    trace_id: str,
    now_timestamp: TimestampProvider,
    authorized_policy_sha256: str = "",
) -> AuthzPolicyRouteResult:
    active_records = record_store.list_authz_policy_records(status="active", limit=1)
    if not active_records:
        raise ValueError("No active Launchplane authz policy record found.")
    _require_expected_authz_policy(
        current_record=active_records[0],
        expected_policy_sha256=authorized_policy_sha256,
    )
    if isinstance(request, AuthzManagedPolicyReconcileEnvelope):
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
        result, driver_result = build_authz_policy_grant_service_result(
            authz_policy_record=authz_policy_record,
            changed=changed,
            mode=request.mode,
            diff=diff,
            audit=audit,
        )
        return AuthzPolicyRouteResult(
            updated_policy=updated_policy,
            previous_authz_policy_record=current_record,
            authz_policy_record=authz_policy_record,
            changed=changed,
            result=result,
            driver_result=driver_result,
        )
    active_schema_version = active_records[0].policy.schema_version
    if request.schema_version != active_schema_version:
        raise ValueError(
            "Authz policy request schema_version must match the active policy schema_version."
        )
    write_result: (
        tuple[
            LaunchplaneAuthzPolicy,
            LaunchplaneAuthzPolicyRecord,
            bool,
            dict[str, object],
            dict[str, object],
        ]
        | None
    ) = None
    if isinstance(request, AuthzPolicyGitHubActionsRemovalEnvelope):
        current_policy, current_record, diff = plan_github_actions_authz_policy_removal(
            record_store=record_store,
            removal=request.removal,
        )
        audit = authz_policy_github_actions_removal_audit_payload(
            request=request,
            identity=identity,
            previous_record=current_record,
            new_record=None,
            changed=bool(diff["changed"]),
            trace_id=trace_id,
            now_timestamp=now_timestamp,
        )
        updated_policy = current_policy
        authz_policy_record = _dry_run_authz_policy_record(
            current_record=current_record,
            audit=audit,
        )
        changed = bool(diff["changed"])
        if request.mode == "apply":
            updated_policy, authz_policy_record, changed, diff, audit = (
                write_github_actions_authz_policy_removal(
                    record_store=record_store,
                    request=request,
                    identity=identity,
                    trace_id=trace_id,
                    now_timestamp=now_timestamp,
                    expected_policy_sha256=authorized_policy_sha256,
                )
            )
        result, driver_result = build_authz_policy_github_actions_removal_service_result(
            authz_policy_record=authz_policy_record,
            changed=changed,
            mode=request.mode,
            diff=diff,
            audit=audit,
        )
        return AuthzPolicyRouteResult(
            updated_policy=updated_policy,
            previous_authz_policy_record=current_record,
            authz_policy_record=authz_policy_record,
            changed=changed,
            result=result,
            driver_result=driver_result,
        )

    if isinstance(request, AuthzPolicyGitHubActionsGrantEnvelope):
        current_policy, current_record, diff = plan_github_actions_authz_policy_grant(
            record_store=record_store,
            grant=request.grant,
        )
        if request.mode == "apply":
            write_result = write_github_actions_authz_policy_grant(
                record_store=record_store,
                request=request,
                identity=identity,
                trace_id=trace_id,
                now_timestamp=now_timestamp,
                expected_policy_sha256=authorized_policy_sha256,
            )
    elif isinstance(request, AuthzPolicyGitHubHumanGrantEnvelope):
        current_policy, current_record, diff = plan_github_human_authz_policy_grant(
            record_store=record_store,
            grant=request.grant,
        )
        if request.mode == "apply":
            write_result = write_github_human_authz_policy_grant(
                record_store=record_store,
                request=request,
                identity=identity,
                trace_id=trace_id,
                now_timestamp=now_timestamp,
                expected_policy_sha256=authorized_policy_sha256,
            )
    elif isinstance(request, AuthzPolicyTerminalAgentGrantEnvelope):
        current_policy, current_record, diff = plan_terminal_agent_authz_policy_grant(
            record_store=record_store,
            grant=request.grant,
        )
        if request.mode == "apply":
            write_result = write_terminal_agent_authz_policy_grant(
                record_store=record_store,
                request=request,
                identity=identity,
                trace_id=trace_id,
                now_timestamp=now_timestamp,
                expected_policy_sha256=authorized_policy_sha256,
            )
    elif isinstance(request, AuthzPolicyLocalOperatorGrantEnvelope):
        current_policy, current_record, diff = plan_local_operator_authz_policy_grant(
            record_store=record_store,
            grant=request.grant,
        )
        if request.mode == "apply":
            write_result = write_local_operator_authz_policy_grant(
                record_store=record_store,
                request=request,
                identity=identity,
                trace_id=trace_id,
                now_timestamp=now_timestamp,
                expected_policy_sha256=authorized_policy_sha256,
            )
    else:
        current_policy, current_record, diff = plan_local_admin_authz_policy_grant(
            record_store=record_store,
            grant=request.grant,
        )
        if request.mode == "apply":
            write_result = write_local_admin_authz_policy_grant(
                record_store=record_store,
                request=request,
                identity=identity,
                trace_id=trace_id,
                now_timestamp=now_timestamp,
                expected_policy_sha256=authorized_policy_sha256,
            )

    audit = authz_policy_grant_audit_payload(
        request=request,
        identity=identity,
        previous_record=current_record,
        new_record=None,
        changed=bool(diff["changed"]),
        trace_id=trace_id,
        now_timestamp=now_timestamp,
    )
    updated_policy = current_policy
    authz_policy_record = _dry_run_authz_policy_record(
        current_record=current_record,
        audit=audit,
    )
    changed = bool(diff["changed"])
    if write_result is not None:
        updated_policy, authz_policy_record, changed, diff, audit = write_result
    result, driver_result = build_authz_policy_grant_service_result(
        authz_policy_record=authz_policy_record,
        changed=changed,
        mode=request.mode,
        diff=diff,
        audit=audit,
    )
    return AuthzPolicyRouteResult(
        updated_policy=updated_policy,
        previous_authz_policy_record=current_record,
        authz_policy_record=authz_policy_record,
        changed=changed,
        result=result,
        driver_result=driver_result,
    )
