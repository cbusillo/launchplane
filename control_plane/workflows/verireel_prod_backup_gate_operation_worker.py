from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from threading import Event, Thread
from typing import Callable, Protocol

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.verireel_prod_backup_gate import (
    VeriReelProdBackupGateResult,
    VeriReelProdBackupGateWorkerRequest,
)
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VeriReelProdBackupGateOperationRecord,
)
from control_plane.durable_operation_authorization import (
    DurableOperationAuthorizationDeniedError,
    DurableOperationAuthorizationGuard,
    read_active_authz_policy_record,
)
from control_plane.workflows.verireel_prod_backup_gate import (
    _build_backup_gate_record,
    _failed_backup_gate_record,
    _run_delegated_worker,
)

DEFAULT_VERIREEL_BACKUP_GATE_WORKER_LEASE_SECONDS = 300
DEFAULT_VERIREEL_BACKUP_GATE_WORKER_HEARTBEAT_SECONDS = 60
DEFAULT_VERIREEL_BACKUP_GATE_WORKER_POLL_SECONDS = 10
DEFAULT_VERIREEL_BACKUP_GATE_WORKER_ERROR_BACKOFF_SECONDS = 30
DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_ATTEMPTS = 3
DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_CONSECUTIVE_ERRORS = 5
SAFE_BACKUP_GATE_RETRY_PHASES = ("created", "running")


class VeriReelProdBackupGateOperationWorkerStore(Protocol):
    def write_verireel_prod_backup_gate_operation_record(
        self, record: VeriReelProdBackupGateOperationRecord
    ) -> Path | None: ...

    def claim_next_verireel_prod_backup_gate_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> VeriReelProdBackupGateOperationRecord | None: ...

    def heartbeat_verireel_prod_backup_gate_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool: ...

    def mark_verireel_prod_backup_gate_operation_phase(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        phase: str,
        updated_at: str,
    ) -> VeriReelProdBackupGateOperationRecord | None: ...

    def complete_verireel_prod_backup_gate_operation_record(
        self,
        *,
        record: VeriReelProdBackupGateOperationRecord,
        lease_owner: str,
    ) -> bool: ...

    def complete_verireel_prod_backup_gate_operation_with_backup_gate_record(
        self,
        *,
        operation_record: VeriReelProdBackupGateOperationRecord,
        backup_gate_record: BackupGateRecord,
        lease_owner: str,
    ) -> bool: ...

    def recover_expired_verireel_prod_backup_gate_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]: ...

    def list_verireel_prod_backup_gate_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        backup_record_id: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[VeriReelProdBackupGateOperationRecord, ...]: ...

    def write_backup_gate_record(self, record: BackupGateRecord) -> Path | None: ...


@dataclass(frozen=True)
class VeriReelProdBackupGateWorkerResult:
    status: str
    operation_id: str = ""
    recovered_operation_ids: tuple[str, ...] = ()
    terminal_write_committed: bool = False


@dataclass(frozen=True)
class VeriReelProdBackupGateOperationLeaseSummary:
    operation_id: str
    product: str
    context: str
    instance: str
    backup_record_id: str
    status: str
    phase: str
    attempt: int
    lease_owner: str
    lease_expires_at: str
    heartbeat_at: str
    heartbeat_age_seconds: int | None
    lease_expired: bool


@dataclass(frozen=True)
class VeriReelProdBackupGateWorkerStatus:
    status: str
    recorded_at: str
    pending_count: int
    running_count: int
    stalled_count: int
    terminal_count: int
    counts_by_status: dict[str, int]
    operations: tuple[VeriReelProdBackupGateOperationLeaseSummary, ...]


@dataclass(frozen=True)
class VeriReelProdBackupGateWorkerLoopResult:
    status: str
    iterations: int
    worked_count: int
    idle_count: int
    error_count: int


@dataclass(frozen=True)
class VeriReelProdBackupGateReconcileResult:
    reconciled_operation_ids: tuple[str, ...]


def run_verireel_prod_backup_gate_operation_worker_once(
    *,
    record_store: VeriReelProdBackupGateOperationWorkerStore,
    control_plane_root_path: Path,
    lease_owner: str,
    lease_seconds: int = DEFAULT_VERIREEL_BACKUP_GATE_WORKER_LEASE_SECONDS,
    heartbeat_seconds: int = DEFAULT_VERIREEL_BACKUP_GATE_WORKER_HEARTBEAT_SECONDS,
    max_attempts: int = DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_ATTEMPTS,
) -> VeriReelProdBackupGateWorkerResult:
    normalized_lease_owner = lease_owner.strip()
    if not normalized_lease_owner:
        raise ValueError("VeriReel prod backup gate worker requires lease_owner.")
    if lease_seconds < 1:
        raise ValueError("VeriReel prod backup gate worker lease_seconds must be positive.")
    if heartbeat_seconds < 1:
        raise ValueError("VeriReel prod backup gate worker heartbeat_seconds must be positive.")
    if heartbeat_seconds >= lease_seconds:
        raise ValueError(
            "VeriReel prod backup gate worker heartbeat_seconds must be less than lease_seconds."
        )
    if max_attempts < 1:
        raise ValueError("VeriReel prod backup gate worker max_attempts must be positive.")
    now = _utc_now_timestamp()
    recovered_operation_ids = (
        record_store.recover_expired_verireel_prod_backup_gate_operation_records(
            now=now,
            safe_phases=SAFE_BACKUP_GATE_RETRY_PHASES,
            max_attempts=max_attempts,
        )
    )
    claim_started_at = _utc_now_timestamp()
    operation = record_store.claim_next_verireel_prod_backup_gate_operation_record(
        lease_owner=normalized_lease_owner,
        lease_expires_at=_timestamp_after(claim_started_at, seconds=lease_seconds),
        claimed_at=claim_started_at,
    )
    if operation is None:
        return VeriReelProdBackupGateWorkerResult(
            status="idle",
            recovered_operation_ids=recovered_operation_ids,
        )
    terminal_write_committed = _execute_operation(
        record_store=record_store,
        control_plane_root_path=control_plane_root_path,
        operation=operation,
        lease_owner=normalized_lease_owner,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
    )
    return VeriReelProdBackupGateWorkerResult(
        status="worked",
        operation_id=operation.operation_id,
        recovered_operation_ids=recovered_operation_ids,
        terminal_write_committed=terminal_write_committed,
    )


def reconcile_stale_verireel_prod_backup_gate_operation_records(
    *,
    record_store: VeriReelProdBackupGateOperationWorkerStore,
    max_attempts: int = DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_ATTEMPTS,
    now: str | None = None,
) -> VeriReelProdBackupGateReconcileResult:
    if max_attempts < 1:
        raise ValueError("VeriReel prod backup gate worker max_attempts must be positive.")
    reconciled_at = now or _utc_now_timestamp()
    reconciled_operation_ids = (
        record_store.recover_expired_verireel_prod_backup_gate_operation_records(
            now=reconciled_at,
            safe_phases=SAFE_BACKUP_GATE_RETRY_PHASES,
            max_attempts=max_attempts,
        )
    )
    return VeriReelProdBackupGateReconcileResult(reconciled_operation_ids=reconciled_operation_ids)


def run_verireel_prod_backup_gate_operation_worker_loop(
    *,
    record_store: VeriReelProdBackupGateOperationWorkerStore,
    control_plane_root_path: Path,
    lease_owner: str,
    lease_seconds: int = DEFAULT_VERIREEL_BACKUP_GATE_WORKER_LEASE_SECONDS,
    heartbeat_seconds: int = DEFAULT_VERIREEL_BACKUP_GATE_WORKER_HEARTBEAT_SECONDS,
    max_attempts: int = DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_ATTEMPTS,
    poll_seconds: int = DEFAULT_VERIREEL_BACKUP_GATE_WORKER_POLL_SECONDS,
    error_backoff_seconds: int = DEFAULT_VERIREEL_BACKUP_GATE_WORKER_ERROR_BACKOFF_SECONDS,
    max_consecutive_errors: int = DEFAULT_VERIREEL_BACKUP_GATE_WORKER_MAX_CONSECUTIVE_ERRORS,
    stop_event: Event | None = None,
    max_iterations: int | None = None,
    iteration_callback: Callable[[VeriReelProdBackupGateWorkerResult], None] | None = None,
) -> VeriReelProdBackupGateWorkerLoopResult:
    if poll_seconds < 1:
        raise ValueError("VeriReel prod backup gate worker poll_seconds must be positive.")
    if error_backoff_seconds < 1:
        raise ValueError("VeriReel prod backup gate worker error_backoff_seconds must be positive.")
    if max_consecutive_errors < 1:
        raise ValueError(
            "VeriReel prod backup gate worker max_consecutive_errors must be positive."
        )
    if max_iterations is not None and max_iterations < 1:
        raise ValueError("VeriReel prod backup gate worker max_iterations must be positive.")
    worker_stop_event = stop_event or Event()
    iterations = 0
    worked_count = 0
    idle_count = 0
    error_count = 0
    consecutive_errors = 0
    while not worker_stop_event.is_set():
        if max_iterations is not None and iterations >= max_iterations:
            break
        try:
            result = run_verireel_prod_backup_gate_operation_worker_once(
                record_store=record_store,
                control_plane_root_path=control_plane_root_path,
                lease_owner=lease_owner,
                lease_seconds=lease_seconds,
                heartbeat_seconds=heartbeat_seconds,
                max_attempts=max_attempts,
            )
        except Exception:
            error_count += 1
            consecutive_errors += 1
            logging.exception("VeriReel prod backup gate worker iteration failed.")
            if consecutive_errors >= max_consecutive_errors:
                raise
            worker_stop_event.wait(timeout=float(error_backoff_seconds))
            continue
        iterations += 1
        consecutive_errors = 0
        if iteration_callback is not None:
            iteration_callback(result)
        if result.status == "worked":
            worked_count += 1
            continue
        idle_count += 1
        worker_stop_event.wait(timeout=float(poll_seconds))
    return VeriReelProdBackupGateWorkerLoopResult(
        status="stopped" if worker_stop_event.is_set() else "completed",
        iterations=iterations,
        worked_count=worked_count,
        idle_count=idle_count,
        error_count=error_count,
    )


def build_verireel_prod_backup_gate_operation_worker_status(
    *,
    record_store: VeriReelProdBackupGateOperationWorkerStore,
    now: str | None = None,
    recent_terminal_limit: int = 10,
) -> VeriReelProdBackupGateWorkerStatus:
    if recent_terminal_limit < 0:
        raise ValueError(
            "VeriReel prod backup gate worker recent_terminal_limit cannot be negative."
        )
    recorded_at = now or _utc_now_timestamp()
    active_records = record_store.list_verireel_prod_backup_gate_operation_records(
        statuses=("pending", "running")
    )
    terminal_records = record_store.list_verireel_prod_backup_gate_operation_records(
        statuses=("pass", "fail"),
        limit=recent_terminal_limit,
    )
    summaries = tuple(
        _lease_summary(record=record, recorded_at=recorded_at) for record in active_records
    )
    counts_by_status: dict[str, int] = {}
    for record in active_records + terminal_records:
        counts_by_status[record.status] = counts_by_status.get(record.status, 0) + 1
    stalled_count = sum(1 for summary in summaries if summary.lease_expired)
    pending_count = sum(1 for summary in summaries if summary.status == "pending")
    running_count = sum(1 for summary in summaries if summary.status == "running")
    return VeriReelProdBackupGateWorkerStatus(
        status="stalled" if stalled_count else "ok",
        recorded_at=recorded_at,
        pending_count=pending_count,
        running_count=running_count,
        stalled_count=stalled_count,
        terminal_count=len(terminal_records),
        counts_by_status=counts_by_status,
        operations=tuple(
            sorted(summaries, key=lambda summary: (summary.status, summary.operation_id))
        ),
    )


def _execute_operation(
    *,
    record_store: VeriReelProdBackupGateOperationWorkerStore,
    control_plane_root_path: Path,
    operation: VeriReelProdBackupGateOperationRecord,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
) -> bool:
    stop_event = Event()
    heartbeat_lost_event = Event()
    heartbeat_thread = _start_heartbeat(
        record_store=record_store,
        operation_id=operation.operation_id,
        lease_owner=lease_owner,
        lease_seconds=lease_seconds,
        heartbeat_seconds=heartbeat_seconds,
        stop_event=stop_event,
        heartbeat_lost_event=heartbeat_lost_event,
    )
    authorization_guard = DurableOperationAuthorizationGuard(
        authorization=operation.authorization,
        policy_record_reader=lambda: read_active_authz_policy_record(record_store),
    )
    try:
        authorization_guard.authorize_execution()
        running_operation = record_store.mark_verireel_prod_backup_gate_operation_phase(
            operation_id=operation.operation_id,
            lease_owner=lease_owner,
            phase="backup_gate",
            updated_at=_utc_now_timestamp(),
        )
        if running_operation is None:
            logging.error(
                "VeriReel prod backup gate operation %s lost its lease before backup execution.",
                operation.operation_id,
            )
            return False
        authorization_guard.checkpoint_provider_effect("backup_gate")
        worker_result = _run_delegated_worker(
            control_plane_root=control_plane_root_path,
            request=VeriReelProdBackupGateWorkerRequest(
                context=running_operation.context,
                instance=running_operation.instance,
                backup_record_id=running_operation.backup_record_id,
                timeout_seconds=running_operation.request.timeout_seconds,
            ),
        )
        backup_gate_record = _build_backup_gate_record(
            request=running_operation.request,
            worker_result=worker_result,
        )
        result = VeriReelProdBackupGateResult(
            backup_record_id=running_operation.backup_record_id,
            backup_status=backup_gate_record.status,
            backup_started_at=worker_result.started_at,
            backup_finished_at=worker_result.finished_at or backup_gate_record.created_at,
            snapshot_name=worker_result.snapshot_name,
            error_message="" if backup_gate_record.status == "pass" else worker_result.detail,
        )
        terminal_operation = _terminal_operation(
            operation=running_operation,
            result=result,
            lease_owner=lease_owner,
        )
    except DurableOperationAuthorizationDeniedError as error:
        logging.warning(
            "VeriReel prod backup gate operation %s was denied before provider mutation: %s",
            operation.operation_id,
            error,
        )
        backup_gate_record = _failed_backup_gate_record(
            request=operation.request,
            error_message=str(error),
        )
        finished_at = _utc_now_timestamp()
        terminal_operation = operation.model_copy(
            update={
                "status": "fail",
                "phase": "failed",
                "updated_at": finished_at,
                "finished_at": finished_at,
                "lease_owner": lease_owner,
                "error_code": error.code,
                "error_message": str(error),
            }
        )
    except Exception as error:
        logging.exception(
            "VeriReel prod backup gate operation %s failed before producing a result.",
            operation.operation_id,
        )
        backup_gate_record = _failed_backup_gate_record(
            request=operation.request, error_message=str(error)
        )
        terminal_operation = operation.model_copy(
            update={
                "status": "fail",
                "phase": "failed",
                "updated_at": _utc_now_timestamp(),
                "finished_at": _utc_now_timestamp(),
                "lease_owner": lease_owner,
                "error_message": str(error),
            }
        )
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=max(float(heartbeat_seconds), 1.0))
    if heartbeat_lost_event.is_set():
        logging.error(
            "VeriReel prod backup gate operation %s lost its lease before terminal write.",
            operation.operation_id,
        )
    return record_store.complete_verireel_prod_backup_gate_operation_with_backup_gate_record(
        operation_record=terminal_operation,
        backup_gate_record=backup_gate_record,
        lease_owner=lease_owner,
    )


def _terminal_operation(
    *,
    operation: VeriReelProdBackupGateOperationRecord,
    result: VeriReelProdBackupGateResult,
    lease_owner: str,
) -> VeriReelProdBackupGateOperationRecord:
    finished_at = _utc_now_timestamp()
    passed = result.backup_status == "pass"
    return operation.model_copy(
        update={
            "status": "pass" if passed else "fail",
            "phase": "completed" if passed else "failed",
            "updated_at": finished_at,
            "finished_at": finished_at,
            "lease_owner": lease_owner,
            "result": result,
            "error_code": "",
            "error_message": "" if passed else (result.error_message or "Backup gate failed."),
        }
    )


def _start_heartbeat(
    *,
    record_store: VeriReelProdBackupGateOperationWorkerStore,
    operation_id: str,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
    stop_event: Event,
    heartbeat_lost_event: Event,
) -> Thread:
    worker = Thread(
        target=_heartbeat_loop,
        kwargs={
            "record_store": record_store,
            "operation_id": operation_id,
            "lease_owner": lease_owner,
            "lease_seconds": lease_seconds,
            "heartbeat_seconds": heartbeat_seconds,
            "stop_event": stop_event,
            "heartbeat_lost_event": heartbeat_lost_event,
        },
        name=f"verireel-prod-backup-gate-heartbeat-{operation_id}",
        daemon=True,
    )
    worker.start()
    return worker


def _heartbeat_loop(
    *,
    record_store: VeriReelProdBackupGateOperationWorkerStore,
    operation_id: str,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
    stop_event: Event,
    heartbeat_lost_event: Event,
) -> None:
    while not stop_event.wait(timeout=float(heartbeat_seconds)):
        heartbeat_at = _utc_now_timestamp()
        renewed = record_store.heartbeat_verireel_prod_backup_gate_operation_record(
            operation_id=operation_id,
            lease_owner=lease_owner,
            heartbeat_at=heartbeat_at,
            lease_expires_at=_timestamp_after(heartbeat_at, seconds=lease_seconds),
        )
        if not renewed:
            heartbeat_lost_event.set()
            return


def _lease_summary(
    *, record: VeriReelProdBackupGateOperationRecord, recorded_at: str
) -> VeriReelProdBackupGateOperationLeaseSummary:
    heartbeat_age_seconds: int | None = None
    if record.heartbeat_at:
        heartbeat_age_seconds = max(
            int(
                (
                    _parse_timestamp(recorded_at) - _parse_timestamp(record.heartbeat_at)
                ).total_seconds()
            ),
            0,
        )
    lease_expired = bool(
        record.status == "running"
        and record.lease_expires_at
        and record.lease_expires_at < recorded_at
    )
    return VeriReelProdBackupGateOperationLeaseSummary(
        operation_id=record.operation_id,
        product=record.product,
        context=record.context,
        instance=record.instance,
        backup_record_id=record.backup_record_id,
        status=record.status,
        phase=record.phase,
        attempt=record.attempt,
        lease_owner=record.lease_owner,
        lease_expires_at=record.lease_expires_at,
        heartbeat_at=record.heartbeat_at,
        heartbeat_age_seconds=heartbeat_age_seconds,
        lease_expired=lease_expired,
    )


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _timestamp_after(timestamp: str, *, seconds: int) -> str:
    return (
        (_parse_timestamp(timestamp) + timedelta(seconds=seconds))
        .isoformat()
        .replace("+00:00", "Z")
    )


def _parse_timestamp(timestamp: str) -> datetime:
    normalized = timestamp.strip()
    if normalized.endswith("Z"):
        normalized = f"{normalized[:-1]}+00:00"
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)
