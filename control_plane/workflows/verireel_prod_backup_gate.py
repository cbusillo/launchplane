from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
from typing import Literal, Protocol

import click
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.verireel_prod_backup_gate import (
    DEFAULT_VERIREEL_PROD_BACKUP_GATE_TIMEOUT_SECONDS,
    VeriReelProdBackupGateRequest,
    VeriReelProdBackupGateResult,
    VeriReelProdBackupGateWorkerRequest,
    VeriReelProdBackupGateWorkerResult,
)
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VERIREEL_PROD_BACKUP_GATE_TERMINAL_OPERATION_STATUSES,
    VeriReelProdBackupGateOperationRecord,
    build_verireel_prod_backup_gate_operation_id,
)
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.worker_runtime_key_safety import enforce_worker_runtime_key_safety


DEFAULT_TIMEOUT_SECONDS = DEFAULT_VERIREEL_PROD_BACKUP_GATE_TIMEOUT_SECONDS
WORKER_COMMAND_ENV_VAR = "LAUNCHPLANE_VERIREEL_PROD_BACKUP_GATE_WORKER_COMMAND"
ASYNC_SOURCE = "launchplane-verireel-prod-backup-gate"
WORKER_RUNTIME_ENV_KEYS = (
    WORKER_COMMAND_ENV_VAR,
    "VERIREEL_PROD_PROXMOX_HOST",
    "VERIREEL_PROD_PROXMOX_USER",
    "VERIREEL_PROD_PROXMOX_SSH_PRIVATE_KEY",
    "VERIREEL_PROD_PROXMOX_SSH_KNOWN_HOSTS",
    "VERIREEL_PROD_CT_ID",
    "VERIREEL_PROD_GATE_LOCAL",
    "VERIREEL_PROD_BACKUP_MODE",
    "VERIREEL_PROD_BACKUP_STORAGE",
    "VERIREEL_PROD_SNAPSHOT_PREFIX",
    "VERIREEL_PROD_SNAPSHOT_KEEP",
    "VERIREEL_PROD_GATE_HEALTH_TIMEOUT_MS",
    "VERIREEL_TESTING_BASE_URL",
    "VERIREEL_PROD_OPERATOR_BASE_URL",
)


class VeriReelProdBackupGateStore(Protocol):
    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord: ...

    def write_backup_gate_record(self, record: BackupGateRecord) -> Path | None: ...


class VeriReelProdBackupGateOperationStore(VeriReelProdBackupGateStore, Protocol):
    def create_verireel_prod_backup_gate_operation_record_if_no_active_record(
        self, record: VeriReelProdBackupGateOperationRecord
    ) -> tuple[VeriReelProdBackupGateOperationRecord, bool]: ...

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


def _resolve_worker_runtime_environment(
    *,
    control_plane_root: Path,
    request: VeriReelProdBackupGateWorkerRequest,
) -> dict[str, str]:
    try:
        resolved_values = control_plane_runtime_environments.resolve_runtime_environment_values(
            control_plane_root=control_plane_root,
            context_name=request.context,
            instance_name=request.instance,
        )
    except click.ClickException:
        return {}
    return {
        key: value
        for key, value in resolved_values.items()
        if key in WORKER_RUNTIME_ENV_KEYS and str(value).strip()
    }


def _worker_environment(
    *,
    control_plane_root: Path,
    request: VeriReelProdBackupGateWorkerRequest,
) -> dict[str, str]:
    enforce_worker_runtime_key_safety(
        context_name=request.context,
        instance_name=request.instance,
        allowed_worker_keys=WORKER_RUNTIME_ENV_KEYS,
        operation_name="VeriReel prod backup gate worker",
    )
    environment = {
        key: value for key, value in os.environ.items() if key not in WORKER_RUNTIME_ENV_KEYS
    }
    environment.update(
        _resolve_worker_runtime_environment(
            control_plane_root=control_plane_root,
            request=request,
        )
    )
    return environment


def _worker_command(*, environment: dict[str, str]) -> list[str]:
    raw_value = environment.get(WORKER_COMMAND_ENV_VAR, "").strip()
    if not raw_value:
        raise click.ClickException(
            f"Missing {WORKER_COMMAND_ENV_VAR} for VeriReel prod backup gate execution."
        )
    command = shlex.split(raw_value)
    if not command:
        raise click.ClickException(
            f"{WORKER_COMMAND_ENV_VAR} did not resolve to an executable command."
        )
    return command


def _run_delegated_worker(
    *,
    control_plane_root: Path,
    request: VeriReelProdBackupGateWorkerRequest,
) -> VeriReelProdBackupGateWorkerResult:
    worker_environment = _worker_environment(
        control_plane_root=control_plane_root,
        request=request,
    )
    timeout_seconds = max(request.timeout_seconds, 1)
    try:
        completed = subprocess.run(
            _worker_command(environment=worker_environment),
            input=request.model_dump_json(),
            text=True,
            capture_output=True,
            timeout=timeout_seconds,
            check=False,
            env=worker_environment,
        )
    except subprocess.TimeoutExpired as exc:
        raise click.ClickException(
            f"VeriReel prod backup gate worker timed out after {timeout_seconds} seconds."
        ) from exc
    stdout = completed.stdout.strip()
    if not stdout:
        detail = (
            completed.stderr.strip() or "VeriReel prod backup gate worker returned no JSON payload."
        )
        raise click.ClickException(detail)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f"VeriReel prod backup gate worker returned invalid JSON: {stdout}"
        ) from exc
    try:
        result = VeriReelProdBackupGateWorkerResult.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            f"VeriReel prod backup gate worker returned invalid result payload: {payload}"
        ) from exc
    if completed.returncode != 0 and result.status != "pass":
        return result
    if completed.returncode != 0:
        detail = result.detail or completed.stderr.strip() or stdout
        raise click.ClickException(detail)
    return result


def _build_backup_gate_record(
    *,
    request: VeriReelProdBackupGateRequest,
    worker_result: VeriReelProdBackupGateWorkerResult,
) -> BackupGateRecord:
    evidence = dict(worker_result.evidence)
    if worker_result.snapshot_name and "snapshot_name" not in evidence:
        evidence["snapshot_name"] = worker_result.snapshot_name
    if worker_result.status == "fail" and worker_result.detail:
        evidence.setdefault("error_message", worker_result.detail)
    return BackupGateRecord(
        record_id=request.backup_record_id,
        context=request.context,
        instance=request.instance,
        created_at=worker_result.finished_at or utc_now_timestamp(),
        source=ASYNC_SOURCE,
        required=True,
        status="pass" if worker_result.status == "pass" else "fail",
        evidence=evidence,
    )


def _pending_backup_gate_record(*, request: VeriReelProdBackupGateRequest) -> BackupGateRecord:
    return BackupGateRecord(
        record_id=request.backup_record_id,
        context=request.context,
        instance=request.instance,
        created_at=utc_now_timestamp(),
        source=ASYNC_SOURCE,
        required=True,
        status="pending",
        evidence={},
    )


def _failed_backup_gate_record(
    *,
    request: VeriReelProdBackupGateRequest,
    error_message: str,
) -> BackupGateRecord:
    return BackupGateRecord(
        record_id=request.backup_record_id,
        context=request.context,
        instance=request.instance,
        created_at=utc_now_timestamp(),
        source=ASYNC_SOURCE,
        required=True,
        status="fail",
        evidence={"error_message": error_message} if error_message else {},
    )


def _result_from_backup_gate_record(
    *,
    request: VeriReelProdBackupGateRequest,
    record: BackupGateRecord,
) -> VeriReelProdBackupGateResult:
    evidence = dict(record.evidence)
    snapshot_name = evidence.get("snapshot_name", "")
    error_message = ""
    if record.status == "fail":
        error_message = evidence.get("error_message", "VeriReel prod backup gate failed.")
    return VeriReelProdBackupGateResult(
        backup_record_id=request.backup_record_id,
        backup_status=record.status,
        backup_started_at=evidence.get("started_at", ""),
        backup_finished_at=record.created_at if record.status in {"pass", "fail"} else "",
        snapshot_name=snapshot_name,
        error_message=error_message,
    )


def _read_existing_backup_gate_record(
    *,
    record_store: VeriReelProdBackupGateStore,
    record_id: str,
) -> BackupGateRecord | None:
    try:
        return record_store.read_backup_gate_record(record_id)
    except FileNotFoundError:
        return None


def _request_fingerprint(request: VeriReelProdBackupGateRequest) -> str:
    return request.model_dump_json()


def _build_operation_record(
    *,
    request: VeriReelProdBackupGateRequest,
    created_at: str,
) -> VeriReelProdBackupGateOperationRecord:
    return VeriReelProdBackupGateOperationRecord(
        operation_id=build_verireel_prod_backup_gate_operation_id(
            product="verireel",
            context=request.context,
            instance=request.instance,
            backup_record_id=request.backup_record_id,
        ),
        product="verireel",
        context=request.context,
        instance=request.instance,
        backup_record_id=request.backup_record_id,
        request_fingerprint=_request_fingerprint(request),
        request=request,
        status="pending",
        phase="created",
        created_at=created_at,
        updated_at=created_at,
    )


def enqueue_verireel_prod_backup_gate(
    *,
    record_store: VeriReelProdBackupGateOperationStore,
    request: VeriReelProdBackupGateRequest,
    now: str | None = None,
) -> VeriReelProdBackupGateResult:
    existing_record = _read_existing_backup_gate_record(
        record_store=record_store,
        record_id=request.backup_record_id,
    )
    if existing_record is not None and existing_record.status in {"pass", "fail"}:
        return _result_from_backup_gate_record(request=request, record=existing_record)

    recorded_at = now or utc_now_timestamp()
    if existing_record is None:
        existing_record = _pending_backup_gate_record(request=request)
        record_store.write_backup_gate_record(existing_record)

    operation = _build_operation_record(request=request, created_at=recorded_at)
    operation_records = record_store.list_verireel_prod_backup_gate_operation_records(
        backup_record_id=request.backup_record_id,
        limit=1,
    )
    if operation_records and operation_records[0].request_fingerprint != operation.request_fingerprint:
        raise click.ClickException(
            "VeriReel prod backup gate request conflicts with an existing operation."
        )
    if (
        operation_records
        and operation_records[0].status
        in VERIREEL_PROD_BACKUP_GATE_TERMINAL_OPERATION_STATUSES
    ):
        terminal_operation = operation_records[0]
        if terminal_operation.result is not None:
            record_store.write_backup_gate_record(
                _backup_gate_record_from_terminal_operation(terminal_operation)
            )
            return terminal_operation.result
        if terminal_operation.status == "fail":
            failed_record = _failed_backup_gate_record(
                request=terminal_operation.request,
                error_message=terminal_operation.error_message,
            )
            record_store.write_backup_gate_record(failed_record)
            return _result_from_backup_gate_record(
                request=terminal_operation.request,
                record=failed_record,
            )
    persisted_operation, _created = (
        record_store.create_verireel_prod_backup_gate_operation_record_if_no_active_record(
            operation
        )
    )
    if persisted_operation.request_fingerprint != operation.request_fingerprint:
        raise click.ClickException(
            "VeriReel prod backup gate request conflicts with an existing operation."
        )
    return _result_from_backup_gate_record(request=request, record=existing_record)


def _backup_gate_record_from_terminal_operation(
    operation: VeriReelProdBackupGateOperationRecord,
) -> BackupGateRecord:
    if operation.result is None:
        raise ValueError("Terminal VeriReel backup gate operation result is required.")
    status: Literal["pass", "fail"]
    if operation.result.backup_status == "pass":
        status = "pass"
    elif operation.result.backup_status == "fail":
        status = "fail"
    else:
        raise ValueError("Terminal VeriReel backup gate operation result must pass or fail.")
    evidence: dict[str, str] = {}
    if operation.result.snapshot_name:
        evidence["snapshot_name"] = operation.result.snapshot_name
    if operation.result.error_message:
        evidence["error_message"] = operation.result.error_message
    return BackupGateRecord(
        record_id=operation.backup_record_id,
        context=operation.context,
        instance=operation.instance,
        created_at=operation.finished_at or operation.updated_at,
        source=ASYNC_SOURCE,
        required=True,
        status=status,
        evidence=evidence,
    )


def execute_verireel_prod_backup_gate(
    *,
    control_plane_root: Path,
    record_store: VeriReelProdBackupGateStore,
    request: VeriReelProdBackupGateRequest,
) -> VeriReelProdBackupGateResult:
    try:
        worker_result = _run_delegated_worker(
            control_plane_root=control_plane_root,
            request=VeriReelProdBackupGateWorkerRequest(
                context=request.context,
                instance=request.instance,
                backup_record_id=request.backup_record_id,
                timeout_seconds=request.timeout_seconds,
            ),
        )
    except click.ClickException as exc:
        finished_at = utc_now_timestamp()
        record_store.write_backup_gate_record(
            BackupGateRecord(
                record_id=request.backup_record_id,
                context=request.context,
                instance=request.instance,
                created_at=finished_at,
                source=ASYNC_SOURCE,
                required=True,
                status="fail",
                evidence={"error_message": str(exc)},
            )
        )
        return VeriReelProdBackupGateResult(
            backup_record_id=request.backup_record_id,
            backup_status="fail",
            backup_started_at="",
            backup_finished_at=finished_at,
            error_message=str(exc),
        )

    record_store.write_backup_gate_record(
        _build_backup_gate_record(
            request=request,
            worker_result=worker_result,
        )
    )
    return VeriReelProdBackupGateResult(
        backup_record_id=request.backup_record_id,
        backup_status=worker_result.status,
        backup_started_at=worker_result.started_at,
        backup_finished_at=worker_result.finished_at,
        snapshot_name=worker_result.snapshot_name,
        error_message="" if worker_result.status == "pass" else worker_result.detail,
    )
