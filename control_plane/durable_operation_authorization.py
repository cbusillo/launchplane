from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import click

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
    DurableOperationCancellation,
    DurableOperationCallerIdentity,
    DurableOperationReconciliationAttestation,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubActionsIdentity,
    GitHubHumanIdentity,
    LaunchplaneAuthzPolicy,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
)


class DurableOperationAuthorizationCaptureError(ValueError):
    pass


class ManagedRuleAuthorizationError(ValueError):
    pass


class DurableOperationAuthorizationDeniedError(click.ClickException):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass
class DurableOperationAuthorizationGuard:
    authorization: DurableOperationAuthorization | None
    policy_record_reader: Callable[[], LaunchplaneAuthzPolicyRecord]
    provider_effect_authorized: bool = False
    denial_error: DurableOperationAuthorizationDeniedError | None = None

    def authorize_execution(self) -> None:
        try:
            self._authorize()
        except DurableOperationAuthorizationDeniedError as error:
            self.denial_error = error
            raise

    def checkpoint_provider_effect(self, _phase: str) -> None:
        if self.provider_effect_authorized:
            return
        try:
            self._authorize()
        except DurableOperationAuthorizationDeniedError as error:
            self.denial_error = error
            raise
        self.provider_effect_authorized = True

    def _authorize(self) -> None:
        if self.authorization is None:
            raise DurableOperationAuthorizationDeniedError(
                code="operation_authorization_provenance_missing",
                message=(
                    "Legacy durable operation lacks authorization provenance and cannot execute."
                ),
            )
        try:
            policy_record = self.policy_record_reader()
        except Exception as error:
            raise DurableOperationAuthorizationDeniedError(
                code="operation_authorization_policy_unavailable",
                message="The active authorization policy is unavailable for durable execution.",
            ) from error
        if not durable_operation_authorization_allows(
            authorization=self.authorization,
            policy_record=policy_record,
        ):
            raise DurableOperationAuthorizationDeniedError(
                code="operation_authorization_revoked",
                message=(
                    "Durable operation authorization was removed, narrowed, or no longer "
                    "matches the recorded caller and target."
                ),
            )


@dataclass(frozen=True, slots=True)
class ManagedRuleIdentity:
    managed_set_id: str
    managed_rule_id: str


def read_active_authz_policy_record(record_store: object) -> LaunchplaneAuthzPolicyRecord:
    list_records = getattr(record_store, "list_authz_policy_records", None)
    if not callable(list_records):
        raise TypeError("Durable operation workers require authz policy record storage.")
    records = list_records(status="active", limit=2)
    if len(records) != 1:
        raise LookupError("Durable operation workers require exactly one active authz policy.")
    record = records[0]
    if not isinstance(record, LaunchplaneAuthzPolicyRecord):
        record = LaunchplaneAuthzPolicyRecord.model_validate(record)
    return record


def capture_durable_operation_authorization(
    *,
    identity: LaunchplaneIdentity,
    action: str,
    product: str,
    context: str,
    instances: tuple[str, ...],
    policy_record: LaunchplaneAuthzPolicyRecord,
    authorized_at: str,
) -> DurableOperationAuthorization:
    if policy_record.policy.schema_version != 2:
        raise DurableOperationAuthorizationCaptureError(
            "Durable operations require schema-v2 managed authz policy."
        )
    target = AuthorizationTarget(scope="instance", instances=instances)
    try:
        managed_rule = require_single_managed_rule_identity(
            policy=policy_record.policy,
            identity=identity,
            action=action,
            product=product,
            context=context,
            target=target,
        )
    except ManagedRuleAuthorizationError as error:
        raise DurableOperationAuthorizationCaptureError(
            "Durable operations require exactly one managed authz rule match."
        ) from error
    return DurableOperationAuthorization(
        action=action,
        product=product,
        context=context,
        instances=instances,
        managed_set_id=managed_rule.managed_set_id,
        managed_rule_id=managed_rule.managed_rule_id,
        policy_record_id=policy_record.record_id,
        policy_revision=policy_record.revision,
        policy_schema_version=2,
        policy_sha256=policy_record.policy_sha256,
        policy_source=policy_record.source,
        authorized_at=authorized_at,
        caller=durable_operation_caller_identity(identity),
    )


def require_single_managed_rule_identity(
    *,
    policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    action: str,
    product: str,
    context: str,
    target: AuthorizationTarget,
) -> ManagedRuleIdentity:
    if policy.schema_version != 2:
        raise ManagedRuleAuthorizationError(
            "Managed-rule authorization requires schema-v2 authz policy."
        )
    matches = _matching_managed_rule_identities(
        policy=policy,
        identity=identity,
        action=action,
        product=product,
        context=context,
        target=target,
    )
    if len(matches) != 1:
        raise ManagedRuleAuthorizationError(
            "Managed-rule authorization requires exactly one matching managed authz rule."
        )
    managed_set_id, managed_rule_id = matches[0]
    return ManagedRuleIdentity(
        managed_set_id=managed_set_id,
        managed_rule_id=managed_rule_id,
    )


def require_single_managed_github_id_rule_identity(
    *,
    policy: LaunchplaneAuthzPolicy,
    identity: GitHubHumanIdentity,
    action: str,
    product: str,
    context: str,
    target: AuthorizationTarget,
) -> ManagedRuleIdentity:
    managed_identity = require_single_managed_rule_identity(
        policy=policy,
        identity=identity,
        action=action,
        product=product,
        context=context,
        target=target,
    )
    matching_rules = tuple(
        rule
        for rule in policy.github_humans
        if rule.managed_set_id == managed_identity.managed_set_id
        and rule.managed_rule_id == managed_identity.managed_rule_id
    )
    if len(matching_rules) != 1:
        raise ManagedRuleAuthorizationError(
            "GitHub-ID authorization requires exactly one matching managed rule."
        )
    rule = matching_rules[0]
    if not rule.github_ids or identity.github_id not in rule.github_ids:
        raise ManagedRuleAuthorizationError(
            "GitHub-ID authorization requires an explicit immutable GitHub-ID selector."
        )
    return managed_identity


def managed_github_id_rule_allows(
    *,
    policy: LaunchplaneAuthzPolicy,
    github_id: int,
    managed_set_id: str,
    managed_rule_id: str,
    action: str,
    product: str,
    context: str,
    target: AuthorizationTarget,
) -> bool:
    if policy.schema_version != 2 or github_id < 1:
        return False
    matching_rules = tuple(
        rule
        for rule in policy.github_humans
        if rule.managed_set_id == managed_set_id and rule.managed_rule_id == managed_rule_id
    )
    if len(matching_rules) != 1:
        return False
    rule = matching_rules[0]
    if not rule.github_ids or github_id not in rule.github_ids:
        return False
    return bool(
        rule.allows_scope(
            action=action,
            product=product,
            context=context,
            target=target,
            schema_version=policy.schema_version,
        )
    )


def durable_operation_authorization_allows(
    *,
    authorization: DurableOperationAuthorization,
    policy_record: LaunchplaneAuthzPolicyRecord,
) -> bool:
    if policy_record.status != "active" or policy_record.policy.schema_version != 2:
        return False
    identity = launchplane_identity_from_durable_caller(authorization.caller)
    target = AuthorizationTarget(scope="instance", instances=authorization.instances)
    return _managed_rule_allows(
        policy=policy_record.policy,
        identity=identity,
        managed_set_id=authorization.managed_set_id,
        managed_rule_id=authorization.managed_rule_id,
        action=authorization.action,
        product=authorization.product,
        context=authorization.context,
        target=target,
    )


def durable_operation_caller_identity(
    identity: LaunchplaneIdentity,
) -> DurableOperationCallerIdentity:
    if isinstance(identity, GitHubActionsIdentity):
        return DurableOperationCallerIdentity(
            identity_type="github_actions",
            subject=identity.subject,
            repository=identity.repository,
            repository_owner=identity.repository_owner,
            repository_id=identity.repository_id,
            repository_owner_id=identity.repository_owner_id,
            workflow_ref=identity.workflow_ref,
            job_workflow_ref=identity.job_workflow_ref,
            ref=identity.ref,
            ref_type=identity.ref_type,
            event_name=identity.event_name,
            environment=identity.environment,
            sha=identity.sha,
        )
    if isinstance(identity, GitHubHumanIdentity):
        return DurableOperationCallerIdentity(
            identity_type="github_human",
            login=identity.login,
            github_id=identity.github_id,
            organizations=tuple(sorted(identity.organizations)),
            teams=tuple(sorted(identity.teams)),
            role=identity.role,
        )
    if isinstance(identity, TerminalAgentIdentity):
        return DurableOperationCallerIdentity(
            identity_type="terminal_agent",
            subject=identity.subject,
            token_label=identity.token_label,
        )
    if isinstance(identity, LocalOperatorIdentity):
        return DurableOperationCallerIdentity(
            identity_type="local_operator",
            subject=identity.subject,
            token_label=identity.token_label,
        )
    if isinstance(identity, LocalAdminIdentity):
        return DurableOperationCallerIdentity(
            identity_type="local_admin",
            subject=identity.subject,
            token_label=identity.token_label,
        )
    raise TypeError(f"Unsupported Launchplane identity type: {type(identity).__name__}")


def build_durable_operation_cancellation(
    *,
    identity: LaunchplaneIdentity,
    reason: str,
    cancelled_at: str,
    reconciliation_attestation: DurableOperationReconciliationAttestation | None = None,
) -> DurableOperationCancellation:
    return DurableOperationCancellation(
        reason=reason,
        cancelled_at=cancelled_at,
        caller=durable_operation_caller_identity(identity),
        reconciliation_attestation=reconciliation_attestation,
    )


def launchplane_identity_from_durable_caller(
    caller: DurableOperationCallerIdentity,
) -> LaunchplaneIdentity:
    if caller.identity_type == "github_actions":
        return GitHubActionsIdentity(
            repository=caller.repository,
            repository_owner=caller.repository_owner,
            repository_id=caller.repository_id,
            repository_owner_id=caller.repository_owner_id,
            workflow_ref=caller.workflow_ref,
            job_workflow_ref=caller.job_workflow_ref,
            ref=caller.ref,
            ref_type=caller.ref_type,
            event_name=caller.event_name,
            environment=caller.environment,
            subject=caller.subject,
            sha=caller.sha,
            raw_claims={},
        )
    if caller.identity_type == "github_human":
        return GitHubHumanIdentity(
            login=caller.login,
            github_id=caller.github_id,
            name="",
            email="",
            organizations=frozenset(caller.organizations),
            teams=frozenset(caller.teams),
            role="admin" if caller.role == "admin" else "read_only",
        )
    if caller.identity_type == "terminal_agent":
        return TerminalAgentIdentity(subject=caller.subject, token_label=caller.token_label)
    if caller.identity_type == "local_operator":
        return LocalOperatorIdentity(subject=caller.subject, token_label=caller.token_label)
    if caller.identity_type == "local_admin":
        return LocalAdminIdentity(subject=caller.subject, token_label=caller.token_label)
    raise TypeError(f"Unsupported durable caller identity type: {caller.identity_type}")


def _matching_managed_rule_identities(
    *,
    policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    action: str,
    product: str,
    context: str,
    target: AuthorizationTarget,
) -> tuple[tuple[str, str], ...]:
    matches: list[tuple[str, str]] = []
    rules: tuple[Any, ...] = _rules_for_identity(policy=policy, identity=identity)
    for rule in rules:
        if rule.managed_set_id is None or rule.managed_rule_id is None:
            continue
        if rule.allows(
            identity=identity,
            action=action,
            product=product,
            context=context,
            target=target,
            schema_version=policy.schema_version,
        ):
            matches.append((rule.managed_set_id, rule.managed_rule_id))
    return tuple(matches)


def _managed_rule_allows(
    *,
    policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
    managed_set_id: str,
    managed_rule_id: str,
    action: str,
    product: str,
    context: str,
    target: AuthorizationTarget,
) -> bool:
    rules: tuple[Any, ...] = _rules_for_identity(policy=policy, identity=identity)
    matches = tuple(
        rule
        for rule in rules
        if rule.managed_set_id == managed_set_id and rule.managed_rule_id == managed_rule_id
    )
    if len(matches) != 1:
        return False
    return bool(
        matches[0].allows(
            identity=identity,
            action=action,
            product=product,
            context=context,
            target=target,
            schema_version=policy.schema_version,
        )
    )


def _rules_for_identity(
    *,
    policy: LaunchplaneAuthzPolicy,
    identity: LaunchplaneIdentity,
) -> tuple[Any, ...]:
    if isinstance(identity, GitHubActionsIdentity):
        return policy.github_actions
    if isinstance(identity, GitHubHumanIdentity):
        return policy.github_humans
    if isinstance(identity, TerminalAgentIdentity):
        return policy.terminal_agents
    if isinstance(identity, LocalOperatorIdentity):
        return policy.local_operators
    return policy.local_admins
