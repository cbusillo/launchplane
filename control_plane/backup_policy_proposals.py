"""Backup adapter for the shared inert privileged-policy proposal lifecycle."""

from collections.abc import Callable
from datetime import datetime, timezone
from typing import Any

from control_plane.contracts.backup_policy_proposal import (
    BackupPolicyHumanEvidence,
    BackupPolicyProposalInput,
)
from control_plane.contracts.privileged_operation import (
    PrivilegedOperationActor,
    PrivilegedOperationAgentActor,
    PrivilegedOperationLocalOperatorActor,
    PrivilegedOperationApproval,
    PrivilegedOperationRecord,
    local_operator_principal_sha256,
    terminal_agent_principal_sha256,
    privileged_operation_pre_state_digest,
)
from control_plane.durable_operation_authorization import (
    DurableOperationAuthorizationCaptureError,
    capture_durable_operation_authorization,
    managed_github_id_rule_allows,
    read_active_authz_policy_record,
)
from control_plane.http_routes.support import AuthorizationAllows, HttpErrorFactory
from control_plane.privileged_operation_registry import plan_backup_policy_proposal
from control_plane.privileged_operation_service import (
    PrivilegedOperationWriteResult,
    create_typed_privileged_operation_plan,
    require_privileged_operation_store,
    approve_privileged_operation,
    cancel_privileged_operation,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubHumanIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
)


class BackupPolicyDecisionAuthorizationError(PermissionError):
    pass


def require_current_backup_policy_plan(
    *, record_store: object, record: PrivilegedOperationRecord
) -> None:
    fresh = plan_backup_policy_proposal(record_store, record.request)
    if fresh != record.evidence:
        raise ValueError("Backup authority changed; a new proposal is required.")


def propose_backup_policy(
    *,
    record_store: object,
    identity: LocalOperatorIdentity | TerminalAgentIdentity,
    envelope: Any,
    authorization_allows: AuthorizationAllows,
    http_error: HttpErrorFactory,
    trace_id: str,
) -> PrivilegedOperationWriteResult:
    request = envelope.request
    if not isinstance(request, BackupPolicyProposalInput):
        raise ValueError("Backup policy proposal requires typed backup input.")
    policy = request.authority.policy
    allowed = any(
        authorization_allows(
            identity=identity,
            action=action,
            product=policy.product,
            context=policy.context,
            target=AuthorizationTarget(scope="instance", instances=(policy.instance,)),
        )
        for action in ("production_backup_authority.plan", "production_backup_authority.write")
    )
    if not allowed:
        raise http_error(
            status_code=403,
            trace_id=trace_id,
            code="authorization_denied",
            message="Identity cannot plan this lane's backup authority.",
        )
    actor = (
        PrivilegedOperationLocalOperatorActor(
            principal_sha256=local_operator_principal_sha256(
                subject=identity.subject, token_label=identity.token_label
            )
        )
        if isinstance(identity, LocalOperatorIdentity)
        else PrivilegedOperationAgentActor(
            principal_sha256=terminal_agent_principal_sha256(
                subject=identity.subject, token_label=identity.token_label
            )
        )
    )
    return create_typed_privileged_operation_plan(
        record_store=record_store,
        descriptor_id="production-backup-policy",
        actor=actor,
        source_kind="agent_api",
        source_event_id=envelope.source_event_id,
        request=request,
        expires_in_seconds=envelope.expires_in_seconds,
    )


def decide_backup_policy(
    *,
    record_store: object,
    record: PrivilegedOperationRecord,
    identity: GitHubHumanIdentity,
    decision: str,
    source_event_id: str,
    authorization_allows: AuthorizationAllows,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> PrivilegedOperationWriteResult:
    if identity.role != "admin" or not isinstance(record.request, BackupPolicyProposalInput):
        raise BackupPolicyDecisionAuthorizationError(
            "Backup policy decisions require a signed-in administrator."
        )
    policy = record.request.authority.policy
    target = AuthorizationTarget(scope="instance", instances=(policy.instance,))
    if not authorization_allows(
        identity=identity,
        action="production_backup_authority.write",
        product=policy.product,
        context=policy.context,
        target=target,
    ):
        raise BackupPolicyDecisionAuthorizationError("Backup policy decision is unauthorized.")
    actor = PrivilegedOperationActor(
        identity_type="github_human", github_id=identity.github_id, login=identity.login
    )
    if (
        decision == "approve"
        and record.status in {"approved", "executing", "executed", "execution_failed"}
        and record.approval is not None
    ):
        store = require_privileged_operation_store(record_store)
        for event in store.list_privileged_operation_event_records(
            operation_id=record.operation_id
        ):
            if (
                event.action == "approved"
                and event.source_event_id == source_event_id
                and event.actor == actor
            ):
                return PrivilegedOperationWriteResult("replayed", record, event)
    active = read_active_authz_policy_record(record_store)
    try:
        authorization = capture_durable_operation_authorization(
            identity=identity,
            action="production_backup_authority.write",
            product=policy.product,
            context=policy.context,
            instances=(policy.instance,),
            policy_record=active,
            authorized_at=now().astimezone(timezone.utc).isoformat(),
        )
    except DurableOperationAuthorizationCaptureError as error:
        raise BackupPolicyDecisionAuthorizationError(
            "Backup decisions require supported immutable human authority."
        ) from error
    if authorization.grant == "policy_rule" and not managed_github_id_rule_allows(
        policy=active.policy,
        github_id=identity.github_id,
        managed_set_id=authorization.managed_set_id,
        managed_rule_id=authorization.managed_rule_id,
        action=authorization.action,
        product=authorization.product,
        context=authorization.context,
        target=target,
    ):
        raise BackupPolicyDecisionAuthorizationError(
            "Backup decisions require immutable human authority."
        )
    if decision == "decline":
        return cancel_privileged_operation(
            record_store=record_store,
            operation_id=record.operation_id,
            actor_github_id=actor.github_id,
            actor_login=actor.login,
            source_event_id=source_event_id,
            reason="Director declined the proposed backup policy.",
            now=now,
        )
    if record.status == "planned":
        require_current_backup_policy_plan(record_store=record_store, record=record)
    assert isinstance(record.evidence, BackupPolicyHumanEvidence)
    approval = PrivilegedOperationApproval(
        approver=actor,
        descriptor_id=record.descriptor_id,
        descriptor_version=record.descriptor_version,
        request_digest=record.request_digest,
        evidence_digest=record.evidence_digest,
        plan_digest=record.evidence.plan_digest,
        pre_state_digest=privileged_operation_pre_state_digest(record.evidence),
        policy_record_id=active.record_id,
        policy_revision=active.revision,
        policy_sha256=active.policy_sha256,
        policy_source=active.source,
        managed_set_id=authorization.managed_set_id,
        managed_rule_id=authorization.managed_rule_id,
        grant="policy_administrator"
        if authorization.grant == "policy_administrator"
        else "policy_rule",
        expires_at=record.expires_at,
        reason="Director approved the service-pinned backup policy.",
        rollback_class="policy_cas",
    )
    return approve_privileged_operation(
        record_store=record_store,
        operation_id=record.operation_id,
        approval=approval,
        source_event_id=source_event_id,
        now=now,
    )
