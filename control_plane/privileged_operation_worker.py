from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import hashlib
import json
from typing import Any, Literal, Protocol, cast

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
    DurableOperationCallerIdentity,
)
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    complete_launchplane_mutation_reservation,
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
from control_plane.durable_operation_authorization import (
    managed_github_id_rule_allows,
    read_active_authz_policy_record,
)
from control_plane.privileged_operation_registry import read_privileged_operation_descriptor
from control_plane.privileged_operation_service import (
    PrivilegedOperationStore,
    expire_privileged_operation_if_due,
    require_privileged_operation_store,
)
from control_plane.service_auth import AuthorizationTarget


PRIVILEGED_OPERATION_EXECUTION_SCOPE = "privileged-operation-execution"
PRIVILEGED_OPERATION_EXECUTION_ROUTE = "service-internal:privileged-operation-worker"
PRIVILEGED_OPERATION_EXECUTION_LEASE_SECONDS = 300


class PrivilegedOperationExecutionStore(PrivilegedOperationStore, Protocol):
    def reserve_mutation(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
        request_fingerprint: str,
        lease_owner: str,
        lease_seconds: int = 300,
        reconciliation_key: str = "",
        provider_target_key: str = "",
    ) -> Any: ...

    def complete_mutation_reservation(
        self,
        *,
        completion: LaunchplaneIdempotencyRecord,
    ) -> Any: ...

    def mark_mutation_reconcile_required(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        reconciliation_key: str,
    ) -> Any: ...

    def lookup_existing_mutation_reservation(
        self,
        *,
        route_path: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> Any: ...

    def prepare_db_only_mutation(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
        request_fingerprint: str,
    ) -> Any: ...

    def adopt_reconciled_mutation(
        self,
        *,
        reservation: LaunchplaneIdempotencyRecord,
        response_status_code: int,
        response_trace_id: str,
        response_payload: dict[str, Any],
    ) -> Any: ...


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _digest(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def privileged_operation_execution_token(operation_id: str) -> str:
    return hashlib.sha256(f"privileged-operation:{operation_id}".encode("utf-8")).hexdigest()


def privileged_operation_execution_fingerprint(record: PrivilegedOperationRecord) -> str:
    if record.approval is None:
        raise ValueError("Privileged operation has no approval evidence.")
    return _digest(
        {
            "operation_id": record.operation_id,
            "descriptor_id": record.descriptor_id,
            "descriptor_version": record.descriptor_version,
            "request_digest": record.request_digest,
            "evidence_digest": record.evidence_digest,
            "approval": record.approval.model_dump(mode="json"),
        }
    )


def privileged_operation_provider_target_key(record: PrivilegedOperationRecord) -> str:
    return f"privileged-operation-target:{record.descriptor_id}:global"


def require_privileged_operation_execution_store(
    record_store: object,
) -> PrivilegedOperationExecutionStore:
    planning_store = require_privileged_operation_store(record_store)
    required_methods = (
        "reserve_mutation",
        "complete_mutation_reservation",
        "mark_mutation_reconcile_required",
        "lookup_existing_mutation_reservation",
        "prepare_db_only_mutation",
        "adopt_reconciled_mutation",
    )
    if any(not callable(getattr(record_store, name, None)) for name in required_methods):
        raise TypeError("Privileged-operation execution requires PostgreSQL mutation storage.")
    return cast(PrivilegedOperationExecutionStore, planning_store)


def _construct_approver_authorization(
    record: PrivilegedOperationRecord,
    policy_record: LaunchplaneAuthzPolicyRecord,
) -> DurableOperationAuthorization:
    approval = record.approval
    if approval is None:
        raise ValueError("approval_provenance_missing")
    return DurableOperationAuthorization(
        action=PRIVILEGED_SECRET_OPERATION_APPROVE_ACTION,
        product="launchplane",
        context="launchplane",
        instances=("global",),
        managed_set_id=approval.managed_set_id,
        managed_rule_id=approval.managed_rule_id,
        policy_record_id=policy_record.record_id,
        policy_revision=policy_record.revision,
        policy_schema_version=2,
        policy_sha256=policy_record.policy_sha256,
        policy_source=policy_record.source,
        authorized_at=record.updated_at,
        caller=DurableOperationCallerIdentity(
            identity_type="github_human",
            login=approval.approver.login,
            github_id=approval.approver.github_id,
            role="read_only",
        ),
    )


def _approver_is_still_authorized(
    *,
    authorization: DurableOperationAuthorization,
    policy_record: LaunchplaneAuthzPolicyRecord,
) -> bool:
    return managed_github_id_rule_allows(
        policy=policy_record.policy,
        github_id=authorization.caller.github_id,
        managed_set_id=authorization.managed_set_id,
        managed_rule_id=authorization.managed_rule_id,
        action=authorization.action,
        product=authorization.product,
        context=authorization.context,
        target=AuthorizationTarget(scope="global"),
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


def _complete_reservation(
    *,
    store: PrivilegedOperationExecutionStore,
    reservation: LaunchplaneIdempotencyRecord,
    operation_id: str,
    response_status_code: int,
    response_payload: dict[str, object],
    now: Callable[[], datetime],
) -> bool:
    completion = complete_launchplane_mutation_reservation(
        reservation,
        response_status_code=response_status_code,
        response_trace_id=f"privileged-operation:{operation_id}",
        completed_at=_timestamp(now()),
        response_payload=response_payload,
    )
    result = store.complete_mutation_reservation(completion=completion)
    return result.status in {"completed", "replayed"}


def _mark_reconciliation_required(
    *,
    store: PrivilegedOperationExecutionStore,
    reservation: LaunchplaneIdempotencyRecord,
    operation_id: str,
) -> None:
    store.mark_mutation_reconcile_required(
        reservation=reservation,
        reconciliation_key=operation_id,
    )


def _execution_evidence(
    *,
    result_status: Literal["ok", "error"],
    payload: dict[str, object],
    reconciliation_required: bool,
    failure_code: str = "",
) -> PrivilegedOperationExecutionEvidence:
    rotated_count = _result_count(payload, "rotated_count")
    unchanged_count = _result_count(payload, "unchanged_count")
    return PrivilegedOperationExecutionEvidence(
        result_status=result_status,
        result_digest=_digest({key: value for key, value in payload.items() if key != "errors"}),
        configured_secret_count=rotated_count + unchanged_count,
        rotation_candidate_count=rotated_count,
        unchanged_count=unchanged_count,
        unreadable_secret_count=_result_count(payload, "error_count"),
        reconciliation_required=reconciliation_required,
        failure_code=failure_code,
    )


def _result_count(payload: dict[str, object], field_name: str) -> int:
    value = payload.get(field_name, 0)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("executor_result_error")
    return value


def _failure_code(error: Exception) -> str:
    known_codes = {
        "approval_provenance_missing",
        "approval_managed_rule_drift",
        "approved_plan_drift",
        "execution_recovery_failed",
        "executor_result_error",
        "mutation_reservation_completion_failed",
    }
    normalized = str(error).strip()
    return normalized if normalized in known_codes else "privileged_operation_execution_error"


def reconcile_stale_privileged_operations(
    *,
    record_store: object,
    now: Callable[[], datetime] = _utc_now,
    limit: int = 100,
) -> tuple[PrivilegedOperationRecord, ...]:
    store = require_privileged_operation_execution_store(record_store)
    reconciled: list[PrivilegedOperationRecord] = []
    for record in store.list_privileged_operation_records(status="executing", limit=limit):
        fingerprint = privileged_operation_execution_fingerprint(record)
        lookup = store.lookup_existing_mutation_reservation(
            route_path=PRIVILEGED_OPERATION_EXECUTION_ROUTE,
            idempotency_key=record.operation_id,
            request_fingerprint=fingerprint,
        )
        if lookup.status != "found" or lookup.record is None:
            continue
        preflight = store.prepare_db_only_mutation(
            scope=PRIVILEGED_OPERATION_EXECUTION_SCOPE,
            route_path=PRIVILEGED_OPERATION_EXECUTION_ROUTE,
            idempotency_key=record.operation_id,
            request_fingerprint=fingerprint,
        )
        if preflight.status != "reconcile_required":
            continue
        reservation = preflight.record
        if reservation is None:
            continue
        try:
            approval = record.approval
            if approval is None:
                raise ValueError("approval_provenance_missing")
            policy_record = read_active_authz_policy_record(record_store)
            authorization = _construct_approver_authorization(record, policy_record)
            if not _approver_is_still_authorized(
                authorization=authorization,
                policy_record=policy_record,
            ):
                raise ValueError("approval_managed_rule_drift")
            result = control_plane_secrets.reencrypt_secrets(
                record_store=cast(control_plane_secrets.SecretRotationStore, record_store),
                apply=True,
                expected_plan_digest=approval.plan_digest,
                operation_token=privileged_operation_execution_token(record.operation_id),
                actor=f"github-human:{approval.approver.github_id}",
                source_label="privileged-operation-worker-recovery",
                reason=record.request.reason,
            )
            if result.get("status") != "ok":
                raise ValueError("execution_recovery_failed")
            execution = _execution_evidence(
                result_status="ok",
                payload={
                    **result,
                    "authorization_policy_sha256": authorization.policy_sha256,
                },
                reconciliation_required=False,
            )
            executed = _transition(
                store=store,
                record=record,
                status="executed",
                sequence=4,
                source_event_id=f"worker-reconcile:{record.operation_id}",
                reason="Privileged operation execution was recovered by deterministic token.",
                execution=execution,
                now=now,
            )
            if executed.status != "executed":
                reconciled.append(executed)
                continue
            adoption = store.adopt_reconciled_mutation(
                reservation=reservation,
                response_status_code=200,
                response_trace_id=f"privileged-operation:{record.operation_id}",
                response_payload={
                    "status": executed.status,
                    "operation_id": executed.operation_id,
                    "result_digest": execution.result_digest,
                },
            )
            reconciled.append(executed)
            if adoption.status not in {"adopted", "replayed"}:
                continue
        except Exception as error:
            failure_code = _failure_code(error)
            execution = _execution_evidence(
                result_status="error",
                payload={"failure_code": failure_code},
                reconciliation_required=True,
                failure_code=failure_code,
            )
            reconciled.append(
                _transition(
                    store=store,
                    record=record,
                    status="execution_failed",
                    sequence=4,
                    source_event_id=f"worker-reconcile-failure:{record.operation_id}",
                    reason="Privileged operation recovery failed; reconciliation is required.",
                    execution=execution,
                    now=now,
                )
            )
    return tuple(reconciled)


def execute_approved_privileged_operations_once(
    *,
    record_store: object,
    lease_owner: str = "privileged-operation-worker",
    now: Callable[[], datetime] = _utc_now,
    limit: int = 20,
) -> tuple[PrivilegedOperationRecord, ...]:
    """Claim and execute approved operations without exposing an HTTP execute surface."""

    store = require_privileged_operation_execution_store(record_store)
    completed = list(reconcile_stale_privileged_operations(record_store=store, now=now))
    for candidate in store.list_privileged_operation_records(status="approved", limit=limit):
        record = expire_privileged_operation_if_due(record_store=store, record=candidate, now=now)
        if record.status != "approved":
            completed.append(record)
            continue
        fingerprint = privileged_operation_execution_fingerprint(record)
        reservation_result = store.reserve_mutation(
            scope=PRIVILEGED_OPERATION_EXECUTION_SCOPE,
            route_path=PRIVILEGED_OPERATION_EXECUTION_ROUTE,
            idempotency_key=record.operation_id,
            request_fingerprint=fingerprint,
            lease_owner=lease_owner,
            lease_seconds=PRIVILEGED_OPERATION_EXECUTION_LEASE_SECONDS,
            reconciliation_key=record.operation_id,
            provider_target_key=privileged_operation_provider_target_key(record),
        )
        if reservation_result.status == "replayed":
            completed.append(store.read_privileged_operation_record(record.operation_id))
            continue
        if reservation_result.status != "acquired":
            continue
        reservation = reservation_result.record
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
            _complete_reservation(
                store=store,
                reservation=reservation,
                operation_id=record.operation_id,
                response_status_code=409,
                response_payload={"status": claimed.status},
                now=now,
            )
            completed.append(claimed)
            continue
        effect_completed = False
        try:
            approval = claimed.approval
            if approval is None:
                raise ValueError("approval_provenance_missing")
            policy_record = read_active_authz_policy_record(record_store)
            authorization = _construct_approver_authorization(claimed, policy_record)
            if not _approver_is_still_authorized(
                authorization=authorization,
                policy_record=policy_record,
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
            result = control_plane_secrets.reencrypt_secrets(
                record_store=cast(control_plane_secrets.SecretRotationStore, record_store),
                apply=True,
                expected_plan_digest=approval.plan_digest,
                operation_token=privileged_operation_execution_token(claimed.operation_id),
                actor=f"github-human:{approval.approver.github_id}",
                source_label="privileged-operation-worker",
                reason=claimed.request.reason,
            )
            if result.get("status") != "ok":
                raise ValueError("executor_result_error")
            effect_completed = True
            execution = _execution_evidence(
                result_status="ok",
                payload={
                    **result,
                    "authorization_policy_sha256": authorization.policy_sha256,
                },
                reconciliation_required=False,
            )
            executed = _transition(
                store=store,
                record=claimed,
                status="executed",
                sequence=4,
                source_event_id=f"worker-result:{claimed.operation_id}",
                reason="Privileged operation executed by the service worker.",
                execution=execution,
                now=now,
            )
            if executed.status != "executed":
                _mark_reconciliation_required(
                    store=store,
                    reservation=reservation,
                    operation_id=claimed.operation_id,
                )
                completed.append(executed)
                continue
            if not _complete_reservation(
                store=store,
                reservation=reservation,
                operation_id=claimed.operation_id,
                response_status_code=200,
                response_payload={
                    "status": executed.status,
                    "operation_id": executed.operation_id,
                    "result_digest": execution.result_digest,
                },
                now=now,
            ):
                _mark_reconciliation_required(
                    store=store,
                    reservation=reservation,
                    operation_id=claimed.operation_id,
                )
            completed.append(executed)
        except Exception as error:
            failure_code = _failure_code(error)
            reconciliation_required = effect_completed
            if reconciliation_required:
                _mark_reconciliation_required(
                    store=store,
                    reservation=reservation,
                    operation_id=claimed.operation_id,
                )
            else:
                _complete_reservation(
                    store=store,
                    reservation=reservation,
                    operation_id=claimed.operation_id,
                    response_status_code=409,
                    response_payload={"status": "execution_failed", "failure_code": failure_code},
                    now=now,
                )
            execution = _execution_evidence(
                result_status="error",
                payload={"failure_code": failure_code},
                reconciliation_required=reconciliation_required,
                failure_code=failure_code,
            )
            completed.append(
                _transition(
                    store=store,
                    record=claimed,
                    status="execution_failed",
                    sequence=4,
                    source_event_id=f"worker-failure:{claimed.operation_id}",
                    reason=(
                        "Privileged operation failed; reconciliation is required."
                        if reconciliation_required
                        else "Privileged operation failed before a provider effect."
                    ),
                    execution=execution,
                    now=now,
                )
            )
    return tuple(completed)
