from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from threading import Event, Thread
from typing import Callable, Protocol, cast

from control_plane.contracts.odoo_stable_bootstrap import OdooStableBootstrapResult
from control_plane.contracts.odoo_prod_backup_restore import OdooProdBackupRestoreResult
from control_plane.contracts.odoo_prod_backup_restore_operation import (
    OdooProdBackupRestoreOperationPhase,
    OdooProdBackupRestoreOperationRecord,
)
from control_plane.contracts.odoo_prod_retained_volume_backup_import import (
    OdooProdRetainedVolumeBackupImportApplyRequest,
    OdooProdRetainedVolumeBackupImportPlan,
    OdooProdRetainedVolumeBackupImportResult,
)
from control_plane.contracts.odoo_prod_retained_volume_backup_import_operation import (
    OdooProdRetainedVolumeBackupImportOperationPhase,
    OdooProdRetainedVolumeBackupImportOperationRecord,
)
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyResult,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.durable_operation_authorization import (
    DurableOperationAuthorizationDeniedError,
    DurableOperationAuthorizationGuard,
    read_active_authz_policy_record,
)
from control_plane.workflows.odoo_stable_bootstrap import (
    OdooStableBootstrapStore,
    execute_odoo_stable_bootstrap,
)
from control_plane.workflows.odoo_prod_backup_restore import (
    OdooProdBackupRestoreStore,
    execute_odoo_prod_backup_restore_apply,
)
from control_plane.workflows.odoo_prod_retained_volume_backup_import import (
    OdooProdRetainedVolumeBackupImportStore,
    build_odoo_prod_retained_volume_backup_import_plan,
    execute_odoo_prod_retained_volume_backup_import_apply,
)
from control_plane.workflows.odoo_stable_target_replacement import (
    OdooStableTargetReplacementStore,
    execute_odoo_stable_target_replacement_apply,
)

DEFAULT_ODOO_STABLE_WORKER_LEASE_SECONDS = 300
DEFAULT_ODOO_STABLE_WORKER_HEARTBEAT_SECONDS = 60
DEFAULT_ODOO_STABLE_WORKER_POLL_SECONDS = 10
DEFAULT_ODOO_STABLE_WORKER_ERROR_BACKOFF_SECONDS = 30
DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS = 3
DEFAULT_ODOO_STABLE_WORKER_MAX_CONSECUTIVE_ERRORS = 5
SAFE_BOOTSTRAP_RETRY_PHASES = ("created",)
SAFE_TARGET_REPLACEMENT_RETRY_PHASES = ("created",)
SAFE_PROD_BACKUP_RESTORE_RETRY_PHASES = ("created", "running", "validated")
SAFE_RETAINED_VOLUME_BACKUP_IMPORT_RETRY_PHASES = ("created", "running", "validated")


class OdooStableOperationWorkerStore(Protocol):
    def list_odoo_stable_bootstrap_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooStableBootstrapOperationRecord, ...]: ...

    def claim_next_odoo_stable_bootstrap_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> OdooStableBootstrapOperationRecord | None: ...

    def heartbeat_odoo_stable_bootstrap_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool: ...

    def complete_odoo_stable_bootstrap_operation_record(
        self,
        *,
        record: OdooStableBootstrapOperationRecord,
        lease_owner: str,
    ) -> bool: ...

    def recover_expired_odoo_stable_bootstrap_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]: ...

    def list_odoo_stable_target_replacement_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        idempotency_scope: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooStableTargetReplacementOperationRecord, ...]: ...

    def claim_next_odoo_stable_target_replacement_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> OdooStableTargetReplacementOperationRecord | None: ...

    def heartbeat_odoo_stable_target_replacement_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool: ...

    def complete_odoo_stable_target_replacement_operation_record(
        self,
        *,
        record: OdooStableTargetReplacementOperationRecord,
        lease_owner: str,
    ) -> bool: ...

    def recover_expired_odoo_stable_target_replacement_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]: ...

    def list_odoo_prod_backup_restore_operation_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        idempotency_scope: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooProdBackupRestoreOperationRecord, ...]: ...

    def read_odoo_prod_backup_restore_operation_record(
        self, operation_id: str
    ) -> OdooProdBackupRestoreOperationRecord: ...

    def claim_next_odoo_prod_backup_restore_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> OdooProdBackupRestoreOperationRecord | None: ...

    def heartbeat_odoo_prod_backup_restore_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool: ...

    def checkpoint_odoo_prod_backup_restore_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        phase: OdooProdBackupRestoreOperationPhase,
        checkpointed_at: str,
        evidence: dict[str, str],
    ) -> OdooProdBackupRestoreOperationRecord | None: ...

    def complete_odoo_prod_backup_restore_operation_record(
        self,
        *,
        record: OdooProdBackupRestoreOperationRecord,
        lease_owner: str,
    ) -> bool: ...

    def recover_expired_odoo_prod_backup_restore_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]: ...

    def list_odoo_prod_retained_volume_backup_import_operation_records(
        self,
        *,
        operation_kind: str = "",
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        idempotency_key: str = "",
        idempotency_scope: str = "",
        statuses: tuple[str, ...] = (),
        limit: int | None = None,
    ) -> tuple[OdooProdRetainedVolumeBackupImportOperationRecord, ...]: ...

    def read_odoo_prod_retained_volume_backup_import_operation_record(
        self, operation_id: str
    ) -> OdooProdRetainedVolumeBackupImportOperationRecord: ...

    def claim_next_odoo_prod_retained_volume_backup_import_operation_record(
        self,
        *,
        lease_owner: str,
        lease_expires_at: str,
        claimed_at: str,
    ) -> OdooProdRetainedVolumeBackupImportOperationRecord | None: ...

    def heartbeat_odoo_prod_retained_volume_backup_import_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        heartbeat_at: str,
        lease_expires_at: str,
    ) -> bool: ...

    def checkpoint_odoo_prod_retained_volume_backup_import_operation_record(
        self,
        *,
        operation_id: str,
        lease_owner: str,
        phase: OdooProdRetainedVolumeBackupImportOperationPhase,
        checkpointed_at: str,
        evidence: dict[str, str],
    ) -> OdooProdRetainedVolumeBackupImportOperationRecord | None: ...

    def complete_odoo_prod_retained_volume_backup_import_operation_record(
        self,
        *,
        record: OdooProdRetainedVolumeBackupImportOperationRecord,
        lease_owner: str,
    ) -> bool: ...

    def recover_expired_odoo_prod_retained_volume_backup_import_operation_records(
        self,
        *,
        now: str,
        safe_phases: tuple[str, ...],
        max_attempts: int,
    ) -> tuple[str, ...]: ...


@dataclass(frozen=True)
class OdooStableOperationWorkerResult:
    status: str
    operation_kind: str = ""
    operation_id: str = ""
    recovered_operation_ids: tuple[str, ...] = ()
    terminal_write_committed: bool = False


@dataclass(frozen=True)
class OdooStableOperationLeaseSummary:
    operation_kind: str
    operation_id: str
    product: str
    context: str
    instance: str
    status: str
    phase: str
    attempt: int
    lease_owner: str
    lease_expires_at: str
    heartbeat_at: str
    heartbeat_age_seconds: int | None
    lease_expired: bool


@dataclass(frozen=True)
class OdooStableOperationWorkerStatus:
    status: str
    recorded_at: str
    pending_count: int
    running_count: int
    stalled_count: int
    terminal_count: int
    counts_by_kind_status: dict[str, int]
    operations: tuple[OdooStableOperationLeaseSummary, ...]


@dataclass(frozen=True)
class OdooStableOperationWorkerLoopResult:
    status: str
    iterations: int
    worked_count: int
    idle_count: int
    error_count: int


@dataclass(frozen=True)
class OdooStableOperationReconcileResult:
    reconciled_bootstrap_ids: tuple[str, ...]
    reconciled_replacement_ids: tuple[str, ...]
    reconciled_restore_ids: tuple[str, ...]
    reconciled_retained_import_ids: tuple[str, ...]


def run_odoo_stable_operation_worker_once(
    *,
    record_store: OdooStableOperationWorkerStore,
    control_plane_root_path: Path,
    lease_owner: str,
    lease_seconds: int = DEFAULT_ODOO_STABLE_WORKER_LEASE_SECONDS,
    heartbeat_seconds: int = DEFAULT_ODOO_STABLE_WORKER_HEARTBEAT_SECONDS,
    max_attempts: int = DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS,
) -> OdooStableOperationWorkerResult:
    normalized_lease_owner = lease_owner.strip()
    if not normalized_lease_owner:
        raise ValueError("Odoo stable operation worker requires lease_owner.")
    if lease_seconds < 1:
        raise ValueError("Odoo stable operation worker lease_seconds must be positive.")
    if heartbeat_seconds < 1:
        raise ValueError("Odoo stable operation worker heartbeat_seconds must be positive.")
    if heartbeat_seconds >= lease_seconds:
        raise ValueError(
            "Odoo stable operation worker heartbeat_seconds must be less than lease_seconds."
        )
    if max_attempts < 1:
        raise ValueError("Odoo stable operation worker max_attempts must be positive.")
    now = _utc_now_timestamp()
    recovered_bootstrap_ids = record_store.recover_expired_odoo_stable_bootstrap_operation_records(
        now=now,
        safe_phases=SAFE_BOOTSTRAP_RETRY_PHASES,
        max_attempts=max_attempts,
    )
    recovered_replacement_ids = (
        record_store.recover_expired_odoo_stable_target_replacement_operation_records(
            now=now,
            safe_phases=SAFE_TARGET_REPLACEMENT_RETRY_PHASES,
            max_attempts=max_attempts,
        )
    )
    recovered_restore_ids = record_store.recover_expired_odoo_prod_backup_restore_operation_records(
        now=now,
        safe_phases=SAFE_PROD_BACKUP_RESTORE_RETRY_PHASES,
        max_attempts=max_attempts,
    )
    recovered_retained_import_ids = (
        record_store.recover_expired_odoo_prod_retained_volume_backup_import_operation_records(
            now=now,
            safe_phases=SAFE_RETAINED_VOLUME_BACKUP_IMPORT_RETRY_PHASES,
            max_attempts=max_attempts,
        )
    )
    recovered_operation_ids = (
        recovered_bootstrap_ids
        + recovered_restore_ids
        + recovered_retained_import_ids
        + recovered_replacement_ids
    )
    claim_started_at = _utc_now_timestamp()
    claim_expires_at = _timestamp_after(claim_started_at, seconds=lease_seconds)
    bootstrap_operation = record_store.claim_next_odoo_stable_bootstrap_operation_record(
        lease_owner=normalized_lease_owner,
        lease_expires_at=claim_expires_at,
        claimed_at=claim_started_at,
    )
    if bootstrap_operation is not None:
        terminal_write_committed = _execute_bootstrap_operation(
            record_store=record_store,
            control_plane_root_path=control_plane_root_path,
            operation=bootstrap_operation,
            lease_owner=normalized_lease_owner,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
        )
        return OdooStableOperationWorkerResult(
            status="worked",
            operation_kind="odoo_stable_bootstrap",
            operation_id=bootstrap_operation.operation_id,
            recovered_operation_ids=recovered_operation_ids,
            terminal_write_committed=terminal_write_committed,
        )
    restore_operation = record_store.claim_next_odoo_prod_backup_restore_operation_record(
        lease_owner=normalized_lease_owner,
        lease_expires_at=claim_expires_at,
        claimed_at=claim_started_at,
    )
    if restore_operation is not None:
        terminal_write_committed = _execute_prod_backup_restore_operation(
            record_store=record_store,
            control_plane_root_path=control_plane_root_path,
            operation=restore_operation,
            lease_owner=normalized_lease_owner,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
        )
        return OdooStableOperationWorkerResult(
            status="worked",
            operation_kind="odoo_prod_backup_restore",
            operation_id=restore_operation.operation_id,
            recovered_operation_ids=recovered_operation_ids,
            terminal_write_committed=terminal_write_committed,
        )
    retained_import_operation = (
        record_store.claim_next_odoo_prod_retained_volume_backup_import_operation_record(
            lease_owner=normalized_lease_owner,
            lease_expires_at=claim_expires_at,
            claimed_at=claim_started_at,
        )
    )
    if retained_import_operation is not None:
        terminal_write_committed = _execute_retained_volume_backup_import_operation(
            record_store=record_store,
            control_plane_root_path=control_plane_root_path,
            operation=retained_import_operation,
            lease_owner=normalized_lease_owner,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
        )
        return OdooStableOperationWorkerResult(
            status="worked",
            operation_kind=(
                "odoo_prod_retained_volume_backup_import_"
                f"{retained_import_operation.operation_kind}"
            ),
            operation_id=retained_import_operation.operation_id,
            recovered_operation_ids=recovered_operation_ids,
            terminal_write_committed=terminal_write_committed,
        )
    replacement_operation = record_store.claim_next_odoo_stable_target_replacement_operation_record(
        lease_owner=normalized_lease_owner,
        lease_expires_at=claim_expires_at,
        claimed_at=claim_started_at,
    )
    if replacement_operation is not None:
        terminal_write_committed = _execute_target_replacement_operation(
            record_store=record_store,
            control_plane_root_path=control_plane_root_path,
            operation=replacement_operation,
            lease_owner=normalized_lease_owner,
            lease_seconds=lease_seconds,
            heartbeat_seconds=heartbeat_seconds,
        )
        return OdooStableOperationWorkerResult(
            status="worked",
            operation_kind="odoo_stable_target_replacement",
            operation_id=replacement_operation.operation_id,
            recovered_operation_ids=recovered_operation_ids,
            terminal_write_committed=terminal_write_committed,
        )
    return OdooStableOperationWorkerResult(
        status="idle",
        recovered_operation_ids=recovered_operation_ids,
    )


def reconcile_stale_odoo_stable_operation_records(
    *,
    record_store: OdooStableOperationWorkerStore,
    max_attempts: int = DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS,
    now: str | None = None,
) -> OdooStableOperationReconcileResult:
    if max_attempts < 1:
        raise ValueError("Odoo stable operation worker max_attempts must be positive.")
    reconciled_at = now or _utc_now_timestamp()
    reconciled_bootstrap_ids = record_store.recover_expired_odoo_stable_bootstrap_operation_records(
        now=reconciled_at,
        safe_phases=SAFE_BOOTSTRAP_RETRY_PHASES,
        max_attempts=max_attempts,
    )
    reconciled_replacement_ids = (
        record_store.recover_expired_odoo_stable_target_replacement_operation_records(
            now=reconciled_at,
            safe_phases=SAFE_TARGET_REPLACEMENT_RETRY_PHASES,
            max_attempts=max_attempts,
        )
    )
    reconciled_restore_ids = (
        record_store.recover_expired_odoo_prod_backup_restore_operation_records(
            now=reconciled_at,
            safe_phases=SAFE_PROD_BACKUP_RESTORE_RETRY_PHASES,
            max_attempts=max_attempts,
        )
    )
    reconciled_retained_import_ids = (
        record_store.recover_expired_odoo_prod_retained_volume_backup_import_operation_records(
            now=reconciled_at,
            safe_phases=SAFE_RETAINED_VOLUME_BACKUP_IMPORT_RETRY_PHASES,
            max_attempts=max_attempts,
        )
    )
    return OdooStableOperationReconcileResult(
        reconciled_bootstrap_ids=reconciled_bootstrap_ids,
        reconciled_replacement_ids=reconciled_replacement_ids,
        reconciled_restore_ids=reconciled_restore_ids,
        reconciled_retained_import_ids=reconciled_retained_import_ids,
    )


def run_odoo_stable_operation_worker_loop(
    *,
    record_store: OdooStableOperationWorkerStore,
    control_plane_root_path: Path,
    lease_owner: str,
    lease_seconds: int = DEFAULT_ODOO_STABLE_WORKER_LEASE_SECONDS,
    heartbeat_seconds: int = DEFAULT_ODOO_STABLE_WORKER_HEARTBEAT_SECONDS,
    max_attempts: int = DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS,
    poll_seconds: int = DEFAULT_ODOO_STABLE_WORKER_POLL_SECONDS,
    error_backoff_seconds: int = DEFAULT_ODOO_STABLE_WORKER_ERROR_BACKOFF_SECONDS,
    max_consecutive_errors: int = DEFAULT_ODOO_STABLE_WORKER_MAX_CONSECUTIVE_ERRORS,
    stop_event: Event | None = None,
    max_iterations: int | None = None,
    iteration_callback: Callable[[OdooStableOperationWorkerResult], None] | None = None,
) -> OdooStableOperationWorkerLoopResult:
    if poll_seconds < 1:
        raise ValueError("Odoo stable operation worker poll_seconds must be positive.")
    if error_backoff_seconds < 1:
        raise ValueError("Odoo stable operation worker error_backoff_seconds must be positive.")
    if max_consecutive_errors < 1:
        raise ValueError("Odoo stable operation worker max_consecutive_errors must be positive.")
    if max_iterations is not None and max_iterations < 1:
        raise ValueError("Odoo stable operation worker max_iterations must be positive.")
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
            result = run_odoo_stable_operation_worker_once(
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
            logging.exception("Odoo stable operation worker iteration failed.")
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
    return OdooStableOperationWorkerLoopResult(
        status="stopped" if worker_stop_event.is_set() else "completed",
        iterations=iterations,
        worked_count=worked_count,
        idle_count=idle_count,
        error_count=error_count,
    )


def build_odoo_stable_operation_worker_status(
    *,
    record_store: OdooStableOperationWorkerStore,
    now: str | None = None,
    recent_terminal_limit: int = 10,
) -> OdooStableOperationWorkerStatus:
    if recent_terminal_limit < 0:
        raise ValueError("Odoo stable operation worker recent_terminal_limit cannot be negative.")
    recorded_at = now or _utc_now_timestamp()
    active_bootstrap_records = record_store.list_odoo_stable_bootstrap_operation_records(
        statuses=("pending", "running")
    )
    active_replacement_records = record_store.list_odoo_stable_target_replacement_operation_records(
        statuses=("pending", "running")
    )
    active_restore_records = record_store.list_odoo_prod_backup_restore_operation_records(
        statuses=("pending", "running")
    )
    active_retained_import_records = (
        record_store.list_odoo_prod_retained_volume_backup_import_operation_records(
            statuses=("pending", "running")
        )
    )
    terminal_bootstrap_records = record_store.list_odoo_stable_bootstrap_operation_records(
        statuses=("pass", "fail"),
        limit=recent_terminal_limit,
    )
    terminal_replacement_records = (
        record_store.list_odoo_stable_target_replacement_operation_records(
            statuses=("pass", "fail"),
            limit=recent_terminal_limit,
        )
    )
    terminal_restore_records = record_store.list_odoo_prod_backup_restore_operation_records(
        statuses=("pass", "fail"),
        limit=recent_terminal_limit,
    )
    terminal_retained_import_records = (
        record_store.list_odoo_prod_retained_volume_backup_import_operation_records(
            statuses=("pass", "fail"),
            limit=recent_terminal_limit,
        )
    )
    summaries = (
        tuple(
            _lease_summary(
                operation_kind="odoo_stable_bootstrap",
                record=record,
                recorded_at=recorded_at,
            )
            for record in active_bootstrap_records
        )
        + tuple(
            _lease_summary(
                operation_kind="odoo_prod_backup_restore",
                record=record,
                recorded_at=recorded_at,
            )
            for record in active_restore_records
        )
        + tuple(
            _lease_summary(
                operation_kind=(f"odoo_prod_retained_volume_backup_import_{record.operation_kind}"),
                record=record,
                recorded_at=recorded_at,
            )
            for record in active_retained_import_records
        )
        + tuple(
            _lease_summary(
                operation_kind="odoo_stable_target_replacement",
                record=record,
                recorded_at=recorded_at,
            )
            for record in active_replacement_records
        )
    )
    counts_by_kind_status: dict[str, int] = {}
    for kind, records in (
        ("odoo_stable_bootstrap", active_bootstrap_records + terminal_bootstrap_records),
        ("odoo_prod_backup_restore", active_restore_records + terminal_restore_records),
        (
            "odoo_prod_retained_volume_backup_import",
            active_retained_import_records + terminal_retained_import_records,
        ),
        (
            "odoo_stable_target_replacement",
            active_replacement_records + terminal_replacement_records,
        ),
    ):
        for record in records:
            key = f"{kind}:{record.status}"
            counts_by_kind_status[key] = counts_by_kind_status.get(key, 0) + 1
    stalled_count = sum(1 for summary in summaries if summary.lease_expired)
    pending_count = sum(1 for summary in summaries if summary.status == "pending")
    running_count = sum(1 for summary in summaries if summary.status == "running")
    terminal_count = (
        len(terminal_bootstrap_records)
        + len(terminal_restore_records)
        + len(terminal_retained_import_records)
        + len(terminal_replacement_records)
    )
    return OdooStableOperationWorkerStatus(
        status="stalled" if stalled_count else "ok",
        recorded_at=recorded_at,
        pending_count=pending_count,
        running_count=running_count,
        stalled_count=stalled_count,
        terminal_count=terminal_count,
        counts_by_kind_status=counts_by_kind_status,
        operations=tuple(
            sorted(summaries, key=lambda summary: (summary.status, summary.operation_id))
        ),
    )


def _execute_bootstrap_operation(
    *,
    record_store: OdooStableOperationWorkerStore,
    control_plane_root_path: Path,
    operation: OdooStableBootstrapOperationRecord,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
) -> bool:
    stop_event = Event()
    heartbeat_lost_event = Event()
    heartbeat_thread = _start_bootstrap_heartbeat(
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
        result = execute_odoo_stable_bootstrap(
            control_plane_root=control_plane_root_path,
            record_store=cast(OdooStableBootstrapStore, record_store),
            request=operation.request,
            provider_effect_checkpoint=authorization_guard.checkpoint_provider_effect,
        )
        if authorization_guard.denial_error is not None:
            raise authorization_guard.denial_error
    except DurableOperationAuthorizationDeniedError as error:
        logging.warning(
            "Odoo stable bootstrap operation %s was denied before provider mutation: %s",
            operation.operation_id,
            error,
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
            "Odoo stable bootstrap operation %s failed before producing a result.",
            operation.operation_id,
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
    else:
        terminal_operation = _bootstrap_terminal_operation(
            operation=operation,
            result=result,
            lease_owner=lease_owner,
        )
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=max(float(heartbeat_seconds), 1.0))
    if heartbeat_lost_event.is_set():
        logging.error(
            "Odoo stable bootstrap operation %s lost its lease before terminal write.",
            operation.operation_id,
        )
    return record_store.complete_odoo_stable_bootstrap_operation_record(
        record=terminal_operation,
        lease_owner=lease_owner,
    )


def _lease_summary(
    *,
    operation_kind: str,
    record: (
        OdooStableBootstrapOperationRecord
        | OdooProdBackupRestoreOperationRecord
        | OdooProdRetainedVolumeBackupImportOperationRecord
        | OdooStableTargetReplacementOperationRecord
    ),
    recorded_at: str,
) -> OdooStableOperationLeaseSummary:
    lease_expired = record.status == "running" and (
        not record.lease_expires_at or record.lease_expires_at <= recorded_at
    )
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
    return OdooStableOperationLeaseSummary(
        operation_kind=operation_kind,
        operation_id=record.operation_id,
        product=record.product,
        context=record.context,
        instance=record.instance,
        status=record.status,
        phase=record.phase,
        attempt=record.attempt,
        lease_owner=record.lease_owner,
        lease_expires_at=record.lease_expires_at,
        heartbeat_at=record.heartbeat_at,
        heartbeat_age_seconds=heartbeat_age_seconds,
        lease_expired=lease_expired,
    )


def _execute_prod_backup_restore_operation(
    *,
    record_store: OdooStableOperationWorkerStore,
    control_plane_root_path: Path,
    operation: OdooProdBackupRestoreOperationRecord,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
) -> bool:
    stop_event = Event()
    heartbeat_lost_event = Event()
    heartbeat_thread = _start_prod_backup_restore_heartbeat(
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

    def latest_operation() -> OdooProdBackupRestoreOperationRecord:
        return record_store.read_odoo_prod_backup_restore_operation_record(operation.operation_id)

    def checkpoint_phase(
        phase: OdooProdBackupRestoreOperationPhase,
        evidence: dict[str, str],
    ) -> None:
        if heartbeat_lost_event.is_set():
            raise RuntimeError("Odoo production backup restore lost its lease before checkpoint.")
        checkpointed = record_store.checkpoint_odoo_prod_backup_restore_operation_record(
            operation_id=operation.operation_id,
            lease_owner=lease_owner,
            phase=phase,
            checkpointed_at=_utc_now_timestamp(),
            evidence=evidence,
        )
        if checkpointed is None:
            heartbeat_lost_event.set()
            raise RuntimeError(
                "Odoo production backup restore could not persist its durable checkpoint."
            )

    def checkpoint_provider_effect(
        phase: OdooProdBackupRestoreOperationPhase,
        effect_name: str,
    ) -> None:
        if heartbeat_lost_event.is_set():
            raise RuntimeError(
                "Odoo production backup restore lost its lease before provider mutation."
            )
        authorization_guard.checkpoint_provider_effect(effect_name)
        checkpoint_phase(phase, {"provider_effect": effect_name})

    try:
        authorization_guard.authorize_execution()
        result = execute_odoo_prod_backup_restore_apply(
            control_plane_root=control_plane_root_path,
            record_store=cast(OdooProdBackupRestoreStore, record_store),
            operation_id=operation.operation_id,
            request=operation.request,
            phase_checkpoint=checkpoint_phase,
            provider_effect_checkpoint=checkpoint_provider_effect,
        )
        if authorization_guard.denial_error is not None:
            raise authorization_guard.denial_error
    except DurableOperationAuthorizationDeniedError as error:
        logging.warning(
            "Odoo production backup restore operation %s was denied before a provider mutation: %s",
            operation.operation_id,
            error,
        )
        current_operation = latest_operation()
        finished_at = _utc_now_timestamp()
        terminal_operation = current_operation.model_copy(
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
            "Odoo production backup restore operation %s failed before producing a result.",
            operation.operation_id,
        )
        current_operation = latest_operation()
        finished_at = _utc_now_timestamp()
        terminal_operation = current_operation.model_copy(
            update={
                "status": "fail",
                "phase": "failed",
                "updated_at": finished_at,
                "finished_at": finished_at,
                "lease_owner": lease_owner,
                "error_message": str(error),
            }
        )
    else:
        terminal_operation = _prod_backup_restore_terminal_operation(
            operation=latest_operation(),
            result=result,
            lease_owner=lease_owner,
        )
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=max(float(heartbeat_seconds), 1.0))
    if heartbeat_lost_event.is_set():
        logging.error(
            "Odoo production backup restore operation %s lost its lease before terminal write.",
            operation.operation_id,
        )
    return record_store.complete_odoo_prod_backup_restore_operation_record(
        record=terminal_operation,
        lease_owner=lease_owner,
    )


def _execute_retained_volume_backup_import_operation(
    *,
    record_store: OdooStableOperationWorkerStore,
    control_plane_root_path: Path,
    operation: OdooProdRetainedVolumeBackupImportOperationRecord,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
) -> bool:
    stop_event = Event()
    heartbeat_lost_event = Event()
    heartbeat_thread = _start_retained_volume_backup_import_heartbeat(
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

    def latest_operation() -> OdooProdRetainedVolumeBackupImportOperationRecord:
        return record_store.read_odoo_prod_retained_volume_backup_import_operation_record(
            operation.operation_id
        )

    def checkpoint_phase(
        phase: OdooProdRetainedVolumeBackupImportOperationPhase,
        evidence: dict[str, str],
    ) -> None:
        if heartbeat_lost_event.is_set():
            raise RuntimeError(
                "Odoo retained-volume backup import lost its lease before checkpoint."
            )
        checkpointed = (
            record_store.checkpoint_odoo_prod_retained_volume_backup_import_operation_record(
                operation_id=operation.operation_id,
                lease_owner=lease_owner,
                phase=phase,
                checkpointed_at=_utc_now_timestamp(),
                evidence=evidence,
            )
        )
        if checkpointed is None:
            heartbeat_lost_event.set()
            raise RuntimeError(
                "Odoo retained-volume backup import could not persist its durable checkpoint."
            )

    def checkpoint_provider_effect(
        phase: OdooProdRetainedVolumeBackupImportOperationPhase,
        effect_name: str,
    ) -> None:
        if heartbeat_lost_event.is_set():
            raise RuntimeError(
                "Odoo retained-volume backup import lost its lease before provider mutation."
            )
        authorization_guard.authorize_execution()
        checkpoint_phase(phase, {"provider_effect": effect_name})

    try:
        authorization_guard.authorize_execution()
        if operation.operation_kind == "plan":
            if isinstance(operation.request, OdooProdRetainedVolumeBackupImportApplyRequest):
                raise TypeError("Retained-volume plan operation carried an apply request.")
            result: (
                OdooProdRetainedVolumeBackupImportPlan | OdooProdRetainedVolumeBackupImportResult
            ) = build_odoo_prod_retained_volume_backup_import_plan(
                control_plane_root=control_plane_root_path,
                record_store=cast(OdooProdRetainedVolumeBackupImportStore, record_store),
                operation_id=operation.operation_id,
                request=operation.request,
                phase_checkpoint=checkpoint_phase,
                provider_effect_checkpoint=checkpoint_provider_effect,
            )
        else:
            if (
                not isinstance(operation.request, OdooProdRetainedVolumeBackupImportApplyRequest)
                or operation.plan is None
            ):
                raise TypeError("Retained-volume apply operation lacks its reviewed plan.")
            result = execute_odoo_prod_retained_volume_backup_import_apply(
                control_plane_root=control_plane_root_path,
                record_store=cast(OdooProdRetainedVolumeBackupImportStore, record_store),
                operation_id=operation.operation_id,
                reviewed_plan=operation.plan,
                request=operation.request,
                phase_checkpoint=checkpoint_phase,
                provider_effect_checkpoint=checkpoint_provider_effect,
            )
        if authorization_guard.denial_error is not None:
            raise authorization_guard.denial_error
    except DurableOperationAuthorizationDeniedError as error:
        logging.warning(
            "Odoo retained-volume backup import operation %s was denied before a provider mutation: %s",
            operation.operation_id,
            error,
        )
        current_operation = latest_operation()
        finished_at = _utc_now_timestamp()
        terminal_operation = current_operation.model_copy(
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
            "Odoo retained-volume backup import operation %s failed before producing a result.",
            operation.operation_id,
        )
        current_operation = latest_operation()
        finished_at = _utc_now_timestamp()
        terminal_operation = current_operation.model_copy(
            update={
                "status": "fail",
                "phase": "failed",
                "updated_at": finished_at,
                "finished_at": finished_at,
                "lease_owner": lease_owner,
                "error_message": str(error),
            }
        )
    else:
        terminal_operation = _retained_volume_backup_import_terminal_operation(
            operation=latest_operation(),
            result=result,
            lease_owner=lease_owner,
        )
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=max(float(heartbeat_seconds), 1.0))
    if heartbeat_lost_event.is_set():
        logging.error(
            "Odoo retained-volume backup import operation %s lost its lease before terminal write.",
            operation.operation_id,
        )
    return record_store.complete_odoo_prod_retained_volume_backup_import_operation_record(
        record=terminal_operation,
        lease_owner=lease_owner,
    )


def _execute_target_replacement_operation(
    *,
    record_store: OdooStableOperationWorkerStore,
    control_plane_root_path: Path,
    operation: OdooStableTargetReplacementOperationRecord,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
) -> bool:
    stop_event = Event()
    heartbeat_lost_event = Event()
    heartbeat_thread = _start_target_replacement_heartbeat(
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
        result = execute_odoo_stable_target_replacement_apply(
            control_plane_root=control_plane_root_path,
            record_store=cast(OdooStableTargetReplacementStore, record_store),
            request=operation.request,
            provider_effect_checkpoint=authorization_guard.checkpoint_provider_effect,
        )
        if authorization_guard.denial_error is not None:
            raise authorization_guard.denial_error
    except DurableOperationAuthorizationDeniedError as error:
        logging.warning(
            "Odoo stable target replacement operation %s was denied before provider mutation: %s",
            operation.operation_id,
            error,
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
            "Odoo stable target replacement operation %s failed before producing a result.",
            operation.operation_id,
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
    else:
        terminal_operation = _target_replacement_terminal_operation(
            operation=operation,
            result=result,
            lease_owner=lease_owner,
        )
    finally:
        stop_event.set()
        heartbeat_thread.join(timeout=max(float(heartbeat_seconds), 1.0))
    if heartbeat_lost_event.is_set():
        logging.error(
            "Odoo stable target replacement operation %s lost its lease before terminal write.",
            operation.operation_id,
        )
    return record_store.complete_odoo_stable_target_replacement_operation_record(
        record=terminal_operation,
        lease_owner=lease_owner,
    )


def _bootstrap_terminal_operation(
    *,
    operation: OdooStableBootstrapOperationRecord,
    result: OdooStableBootstrapResult,
    lease_owner: str,
) -> OdooStableBootstrapOperationRecord:
    finished_at = _utc_now_timestamp()
    passed = _bootstrap_result_passed(result)
    return operation.model_copy(
        update={
            "status": "pass" if passed else "fail",
            "phase": "completed" if passed else "failed",
            "deployment_record_id": result.deployment_record_id,
            "updated_at": finished_at,
            "finished_at": finished_at,
            "lease_owner": lease_owner,
            "result": result,
            "error_code": "",
            "error_message": ""
            if passed
            else (result.error_message or "Odoo stable bootstrap failed."),
        }
    )


def _target_replacement_terminal_operation(
    *,
    operation: OdooStableTargetReplacementOperationRecord,
    result: OdooStableTargetReplacementApplyResult,
    lease_owner: str,
) -> OdooStableTargetReplacementOperationRecord:
    finished_at = _utc_now_timestamp()
    passed = _target_replacement_result_passed(result)
    return operation.model_copy(
        update={
            "status": "pass" if passed else "fail",
            "phase": "completed" if passed else "failed",
            "deployment_record_id": result.deployment_record_id,
            "updated_at": finished_at,
            "finished_at": finished_at,
            "lease_owner": lease_owner,
            "result": result,
            "error_code": "",
            "error_message": ""
            if passed
            else (result.error_message or "Odoo stable target replacement failed."),
        }
    )


def _prod_backup_restore_terminal_operation(
    *,
    operation: OdooProdBackupRestoreOperationRecord,
    result: OdooProdBackupRestoreResult,
    lease_owner: str,
) -> OdooProdBackupRestoreOperationRecord:
    finished_at = _utc_now_timestamp()
    passed = result.restore_status == "pass"
    return operation.model_copy(
        update={
            "status": "pass" if passed else "fail",
            "phase": "completed" if passed else "failed",
            "deployment_record_id": result.deployment_record_id,
            "updated_at": finished_at,
            "finished_at": finished_at,
            "lease_owner": lease_owner,
            "result": result,
            "error_code": "",
            "error_message": ""
            if passed
            else (result.error_message or "Odoo production backup restore failed."),
        }
    )


def _retained_volume_backup_import_terminal_operation(
    *,
    operation: OdooProdRetainedVolumeBackupImportOperationRecord,
    result: OdooProdRetainedVolumeBackupImportPlan | OdooProdRetainedVolumeBackupImportResult,
    lease_owner: str,
) -> OdooProdRetainedVolumeBackupImportOperationRecord:
    finished_at = _utc_now_timestamp()
    if isinstance(result, OdooProdRetainedVolumeBackupImportPlan):
        passed = result.plan_status == "ready"
        error_message = (
            ""
            if passed
            else (
                "; ".join(result.blockers) or "Odoo retained-volume backup import plan was blocked."
            )
        )
    else:
        passed = result.import_status == "pass"
        error_message = (
            "" if passed else (result.error_message or "Odoo retained-volume backup import failed.")
        )
    return operation.model_copy(
        update={
            "status": "pass" if passed else "fail",
            "phase": "completed" if passed else "failed",
            "updated_at": finished_at,
            "finished_at": finished_at,
            "lease_owner": lease_owner,
            "result": result,
            "error_code": "",
            "error_message": error_message,
        }
    )


def _start_bootstrap_heartbeat(
    *,
    record_store: OdooStableOperationWorkerStore,
    operation_id: str,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
    stop_event: Event,
    heartbeat_lost_event: Event,
) -> Thread:
    worker = Thread(
        target=_bootstrap_heartbeat_loop,
        kwargs={
            "record_store": record_store,
            "operation_id": operation_id,
            "lease_owner": lease_owner,
            "lease_seconds": lease_seconds,
            "heartbeat_seconds": heartbeat_seconds,
            "stop_event": stop_event,
            "heartbeat_lost_event": heartbeat_lost_event,
        },
        name=f"odoo-stable-bootstrap-heartbeat-{operation_id}",
        daemon=True,
    )
    worker.start()
    return worker


def _start_target_replacement_heartbeat(
    *,
    record_store: OdooStableOperationWorkerStore,
    operation_id: str,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
    stop_event: Event,
    heartbeat_lost_event: Event,
) -> Thread:
    worker = Thread(
        target=_target_replacement_heartbeat_loop,
        kwargs={
            "record_store": record_store,
            "operation_id": operation_id,
            "lease_owner": lease_owner,
            "lease_seconds": lease_seconds,
            "heartbeat_seconds": heartbeat_seconds,
            "stop_event": stop_event,
            "heartbeat_lost_event": heartbeat_lost_event,
        },
        name=f"odoo-target-replacement-heartbeat-{operation_id}",
        daemon=True,
    )
    worker.start()
    return worker


def _start_prod_backup_restore_heartbeat(
    *,
    record_store: OdooStableOperationWorkerStore,
    operation_id: str,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
    stop_event: Event,
    heartbeat_lost_event: Event,
) -> Thread:
    worker = Thread(
        target=_prod_backup_restore_heartbeat_loop,
        kwargs={
            "record_store": record_store,
            "operation_id": operation_id,
            "lease_owner": lease_owner,
            "lease_seconds": lease_seconds,
            "heartbeat_seconds": heartbeat_seconds,
            "stop_event": stop_event,
            "heartbeat_lost_event": heartbeat_lost_event,
        },
        name=f"odoo-prod-backup-restore-heartbeat-{operation_id}",
        daemon=True,
    )
    worker.start()
    return worker


def _start_retained_volume_backup_import_heartbeat(
    *,
    record_store: OdooStableOperationWorkerStore,
    operation_id: str,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
    stop_event: Event,
    heartbeat_lost_event: Event,
) -> Thread:
    worker = Thread(
        target=_retained_volume_backup_import_heartbeat_loop,
        kwargs={
            "record_store": record_store,
            "operation_id": operation_id,
            "lease_owner": lease_owner,
            "lease_seconds": lease_seconds,
            "heartbeat_seconds": heartbeat_seconds,
            "stop_event": stop_event,
            "heartbeat_lost_event": heartbeat_lost_event,
        },
        name=f"odoo-retained-volume-backup-import-heartbeat-{operation_id}",
        daemon=True,
    )
    worker.start()
    return worker


def _bootstrap_heartbeat_loop(
    *,
    record_store: OdooStableOperationWorkerStore,
    operation_id: str,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
    stop_event: Event,
    heartbeat_lost_event: Event,
) -> None:
    while not stop_event.wait(timeout=float(heartbeat_seconds)):
        heartbeat_at = _utc_now_timestamp()
        renewed = record_store.heartbeat_odoo_stable_bootstrap_operation_record(
            operation_id=operation_id,
            lease_owner=lease_owner,
            heartbeat_at=heartbeat_at,
            lease_expires_at=_timestamp_after(heartbeat_at, seconds=lease_seconds),
        )
        if not renewed:
            heartbeat_lost_event.set()
            return


def _target_replacement_heartbeat_loop(
    *,
    record_store: OdooStableOperationWorkerStore,
    operation_id: str,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
    stop_event: Event,
    heartbeat_lost_event: Event,
) -> None:
    while not stop_event.wait(timeout=float(heartbeat_seconds)):
        heartbeat_at = _utc_now_timestamp()
        renewed = record_store.heartbeat_odoo_stable_target_replacement_operation_record(
            operation_id=operation_id,
            lease_owner=lease_owner,
            heartbeat_at=heartbeat_at,
            lease_expires_at=_timestamp_after(heartbeat_at, seconds=lease_seconds),
        )
        if not renewed:
            heartbeat_lost_event.set()
            return


def _prod_backup_restore_heartbeat_loop(
    *,
    record_store: OdooStableOperationWorkerStore,
    operation_id: str,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
    stop_event: Event,
    heartbeat_lost_event: Event,
) -> None:
    while not stop_event.wait(timeout=float(heartbeat_seconds)):
        heartbeat_at = _utc_now_timestamp()
        renewed = record_store.heartbeat_odoo_prod_backup_restore_operation_record(
            operation_id=operation_id,
            lease_owner=lease_owner,
            heartbeat_at=heartbeat_at,
            lease_expires_at=_timestamp_after(heartbeat_at, seconds=lease_seconds),
        )
        if not renewed:
            heartbeat_lost_event.set()
            return


def _retained_volume_backup_import_heartbeat_loop(
    *,
    record_store: OdooStableOperationWorkerStore,
    operation_id: str,
    lease_owner: str,
    lease_seconds: int,
    heartbeat_seconds: int,
    stop_event: Event,
    heartbeat_lost_event: Event,
) -> None:
    while not stop_event.wait(timeout=float(heartbeat_seconds)):
        heartbeat_at = _utc_now_timestamp()
        renewed = record_store.heartbeat_odoo_prod_retained_volume_backup_import_operation_record(
            operation_id=operation_id,
            lease_owner=lease_owner,
            heartbeat_at=heartbeat_at,
            lease_expires_at=_timestamp_after(heartbeat_at, seconds=lease_seconds),
        )
        if not renewed:
            heartbeat_lost_event.set()
            return


def _bootstrap_result_passed(result: OdooStableBootstrapResult) -> bool:
    return (
        result.bootstrap_status == "pass"
        and result.post_deploy_status == "pass"
        and result.health_status != "fail"
        and result.canonical_status != "fail"
        and result.logo_status != "fail"
    )


def _target_replacement_result_passed(result: OdooStableTargetReplacementApplyResult) -> bool:
    return (
        result.deploy_status == "pass"
        and result.post_deploy_status == "pass"
        and result.health_status != "fail"
        and result.canonical_status != "fail"
        and result.logo_status != "fail"
    )


def _utc_now_timestamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _timestamp_after(value: str, *, seconds: int) -> str:
    return (
        (
            _parse_timestamp(value).astimezone(timezone.utc).replace(microsecond=0)
            + timedelta(seconds=seconds)
        )
        .isoformat()
        .replace("+00:00", "Z")
    )
