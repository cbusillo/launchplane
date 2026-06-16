from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import logging
from pathlib import Path
from threading import Event, Thread
from typing import Protocol, cast

from control_plane.contracts.odoo_stable_bootstrap import OdooStableBootstrapResult
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyResult,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.workflows.odoo_stable_bootstrap import (
    OdooStableBootstrapStore,
    execute_odoo_stable_bootstrap,
)
from control_plane.workflows.odoo_stable_target_replacement import (
    OdooStableTargetReplacementStore,
    execute_odoo_stable_target_replacement_apply,
)

DEFAULT_ODOO_STABLE_WORKER_LEASE_SECONDS = 300
DEFAULT_ODOO_STABLE_WORKER_HEARTBEAT_SECONDS = 60
DEFAULT_ODOO_STABLE_WORKER_MAX_ATTEMPTS = 3
SAFE_BOOTSTRAP_RETRY_PHASES = ("created",)
SAFE_TARGET_REPLACEMENT_RETRY_PHASES = ("created",)


class OdooStableOperationWorkerStore(Protocol):
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


@dataclass(frozen=True)
class OdooStableOperationWorkerResult:
    status: str
    operation_kind: str = ""
    operation_id: str = ""
    recovered_operation_ids: tuple[str, ...] = ()
    terminal_write_committed: bool = False


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
    recovered_operation_ids = recovered_bootstrap_ids + recovered_replacement_ids
    claim_expires_at = _timestamp_after(now, seconds=lease_seconds)
    bootstrap_operation = record_store.claim_next_odoo_stable_bootstrap_operation_record(
        lease_owner=normalized_lease_owner,
        lease_expires_at=claim_expires_at,
        claimed_at=now,
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
    replacement_operation = record_store.claim_next_odoo_stable_target_replacement_operation_record(
        lease_owner=normalized_lease_owner,
        lease_expires_at=claim_expires_at,
        claimed_at=now,
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
    try:
        result = execute_odoo_stable_bootstrap(
            control_plane_root=control_plane_root_path,
            record_store=cast(OdooStableBootstrapStore, record_store),
            request=operation.request,
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
    try:
        result = execute_odoo_stable_target_replacement_apply(
            control_plane_root=control_plane_root_path,
            record_store=cast(OdooStableTargetReplacementStore, record_store),
            request=operation.request,
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
            "error_message": ""
            if passed
            else (result.error_message or "Odoo stable target replacement failed."),
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
