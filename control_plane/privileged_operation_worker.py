from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
from typing import Literal, cast

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
    DurableOperationCallerIdentity,
)
from control_plane.contracts.privileged_operation import (
    PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,
    PrivilegedOperationActor,
    PrivilegedOperationConflictError,
    PrivilegedOperationEventRecord,
    PrivilegedOperationExecutionEvidence,
    PrivilegedOperationRecord,
    build_privileged_operation_event_id,
    privileged_operation_pre_state_digest,
    privileged_operation_record_digest,
)
from control_plane.durable_operation_authorization import read_active_authz_policy_record
from control_plane.privileged_operation_registry import read_privileged_operation_descriptor
from control_plane.privileged_operation_service import (
    PrivilegedOperationStore,
    expire_privileged_operation_if_due,
    require_privileged_operation_store,
)
from control_plane.service_auth import AuthorizationTarget, GitHubHumanIdentity


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _result_digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _operation_token(operation_id: str) -> str:
    return hashlib.sha256(f"privileged-operation:{operation_id}".encode("utf-8")).hexdigest()


def _immutable_approver_is_still_authorized(
    *, record: PrivilegedOperationRecord, policy_record: object
) -> bool:
    approval = record.approval
    if approval is None:
        return False
    current = read_active_authz_policy_record(_RecordStoreAdapter(policy_record)).policy
    matching_rules = tuple(
        rule
        for rule in current.github_humans
        if rule.managed_set_id == approval.managed_set_id
        and rule.managed_rule_id == approval.managed_rule_id
    )
    if len(matching_rules) != 1:
        return False
    rule = matching_rules[0]
    if (
        not rule.github_ids
        or approval.approver.github_id not in rule.github_ids
        or rule.logins
        or rule.organizations
        or rule.teams
        or rule.roles
    ):
        return False
    identity = GitHubHumanIdentity(
        login=f"github-id-{approval.approver.github_id}",
        github_id=approval.approver.github_id,
        name="",
        email="",
        organizations=frozenset(),
        teams=frozenset(),
        role="read_only",
    )
    return rule.allows(
        identity=identity,
        action=PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,
        product="launchplane",
        context="launchplane",
        target=AuthorizationTarget(scope="global"),
        schema_version=2,
    )


class _RecordStoreAdapter:
    def __init__(self, policy_record: object) -> None:
        self._policy_record = policy_record

    def list_authz_policy_records(self, *, status: str, limit: int) -> tuple[object, ...]:
        _ = status, limit
        return (self._policy_record,)


def _construct_approver_authorization(
    record: PrivilegedOperationRecord,
) -> DurableOperationAuthorization:
    approval = record.approval
    if approval is None:
        raise ValueError("Privileged operation has no approval evidence.")
    return DurableOperationAuthorization(
        action=PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,
        product="launchplane",
        context="launchplane",
        instances=("global",),
        managed_set_id=approval.managed_set_id,
        managed_rule_id=approval.managed_rule_id,
        policy_record_id=approval.policy_record_id,
        policy_revision=approval.policy_revision,
        policy_schema_version=2,
        policy_sha256=approval.policy_sha256,
        policy_source=approval.policy_source,
        authorized_at=record.updated_at,
        caller=DurableOperationCallerIdentity(
            identity_type="github_human",
            login=approval.approver.login,
            github_id=approval.approver.github_id,
        ),
    )


def _transition(
    *,
    store: PrivilegedOperationStore,
    record: PrivilegedOperationRecord,
    status: Literal["executing", "executed", "execution_failed"],
    sequence: int,
    source_event_id: str,
    reason: str,
    execution: PrivilegedOperationExecutionEvidence | None = None,
    now: Callable[[], datetime],
) -> PrivilegedOperationRecord:
    occurred_at = _timestamp(now())
    terminal = status in {"executed", "execution_failed"}
    proposed = record.model_copy(
        update={
            "status": status,
            "updated_at": occurred_at,
            "terminal_at": occurred_at if terminal else "",
            "terminal_reason": reason if terminal else "",
            "execution": execution,
        }
    )
    event = PrivilegedOperationEventRecord(
        event_id=build_privileged_operation_event_id(
            operation_id=record.operation_id,
            action=status,
            source_kind="system",
            source_event_id=source_event_id,
        ),
        operation_id=record.operation_id,
        sequence=sequence,
        action=status,
        occurred_at=occurred_at,
        source_kind="system",
        source_event_id=source_event_id,
        actor=PrivilegedOperationActor(identity_type="system", login="system"),
        reason=reason,
        resulting_record_digest=privileged_operation_record_digest(proposed),
    )
    try:
        store.transition_privileged_operation(proposed, event)
    except PrivilegedOperationConflictError:
        return store.read_privileged_operation_record(record.operation_id)
    return store.read_privileged_operation_record(record.operation_id)


def execute_approved_privileged_operations_once(
    *, record_store: object, now: Callable[[], datetime] = _utc_now, limit: int = 20
) -> tuple[PrivilegedOperationRecord, ...]:
    """Claim and execute approved operations without exposing an HTTP execute surface."""

    store = require_privileged_operation_store(record_store)
    completed: list[PrivilegedOperationRecord] = []
    for candidate in store.list_privileged_operation_records(status="approved", limit=limit):
        record = expire_privileged_operation_if_due(record_store=store, record=candidate, now=now)
        if record.status != "approved":
            completed.append(record)
            continue
        claimed = _transition(
            store=store,
            record=record,
            status="executing",
            sequence=3,
            source_event_id=f"worker-claim:{record.operation_id}",
            reason="Service worker claimed approved privileged operation.",
            now=now,
        )
        if claimed.status != "executing":
            completed.append(claimed)
            continue
        try:
            approval = claimed.approval
            if approval is None:
                raise ValueError("approval_provenance_missing")
            policy_record = read_active_authz_policy_record(record_store)
            if (
                policy_record.record_id != approval.policy_record_id
                or policy_record.revision != approval.policy_revision
                or policy_record.policy_sha256 != approval.policy_sha256
                or policy_record.source != approval.policy_source
            ):
                raise ValueError("approval_policy_drift")
            if not _immutable_approver_is_still_authorized(
                record=claimed, policy_record=policy_record
            ):
                raise ValueError("approval_managed_rule_drift")
            registration = read_privileged_operation_descriptor(claimed.descriptor_id)
            fresh_evidence = registration.planner(record_store, claimed.request)
            if (
                fresh_evidence.plan_digest != approval.plan_digest
                or privileged_operation_pre_state_digest(fresh_evidence)
                != approval.pre_state_digest
            ):
                raise ValueError("approved_plan_drift")
            _construct_approver_authorization(claimed)
            result = control_plane_secrets.reencrypt_secrets(
                record_store=cast(control_plane_secrets.SecretRotationStore, record_store),
                apply=True,
                expected_plan_digest=approval.plan_digest,
                operation_token=_operation_token(claimed.operation_id),
                actor=f"github-human:{approval.approver.github_id}",
                source_label="privileged-operation-worker",
                reason=claimed.request.reason,
            )
            if result.get("status") != "ok":
                raise ValueError("executor_result_error")
            execution = PrivilegedOperationExecutionEvidence(
                result_status="ok",
                result_digest=_result_digest(
                    {key: value for key, value in result.items() if key != "errors"}
                ),
                configured_secret_count=int(result.get("rotated_count", 0))
                + int(result.get("unchanged_count", 0)),
                rotation_candidate_count=int(result.get("rotated_count", 0)),
                unchanged_count=int(result.get("unchanged_count", 0)),
                unreadable_secret_count=int(result.get("error_count", 0)),
                reconciliation_required=False,
            )
            completed.append(
                _transition(
                    store=store,
                    record=claimed,
                    status="executed",
                    sequence=4,
                    source_event_id=f"worker-result:{claimed.operation_id}",
                    reason="Privileged operation executed by the service worker.",
                    execution=execution,
                    now=now,
                )
            )
        except Exception as error:
            failure_code = str(error).strip() or type(error).__name__
            failure_code = failure_code[:160]
            execution = PrivilegedOperationExecutionEvidence(
                result_status="error",
                result_digest=_result_digest({"failure_code": failure_code}),
                configured_secret_count=0,
                rotation_candidate_count=0,
                unchanged_count=0,
                unreadable_secret_count=0,
                reconciliation_required=True,
                failure_code=failure_code,
            )
            completed.append(
                _transition(
                    store=store,
                    record=claimed,
                    status="execution_failed",
                    sequence=4,
                    source_event_id=f"worker-failure:{claimed.operation_id}",
                    reason="Privileged operation failed; reconciliation is required.",
                    execution=execution,
                    now=now,
                )
            )
    return tuple(completed)
