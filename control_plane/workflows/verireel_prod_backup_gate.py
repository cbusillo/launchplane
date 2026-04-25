from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import threading

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.ship import utc_now_timestamp


DEFAULT_TIMEOUT_SECONDS = 1800
WORKER_COMMAND_ENV_VAR = "LAUNCHPLANE_VERIREEL_PROD_BACKUP_GATE_WORKER_COMMAND"
ASYNC_SOURCE = "launchplane-verireel-prod-backup-gate"
_ACTIVE_BACKUP_GATES: set[str] = set()
_ACTIVE_BACKUP_GATES_LOCK = threading.Lock()
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


class VeriReelProdBackupGateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str = "verireel"
    instance: str = "prod"
    backup_record_id: str
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelProdBackupGateRequest":
        if self.context != "verireel":
            raise ValueError("VeriReel prod backup gate requires context 'verireel'.")
        if self.instance != "prod":
            raise ValueError("VeriReel prod backup gate requires instance 'prod'.")
        if not self.backup_record_id.strip():
            raise ValueError("VeriReel prod backup gate requires backup_record_id.")
        return self


class VeriReelProdBackupGateWorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    backup_record_id: str
    timeout_seconds: int = Field(default=DEFAULT_TIMEOUT_SECONDS, ge=1)


class VeriReelProdBackupGateWorkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: str
    snapshot_name: str = ""
    started_at: str = ""
    finished_at: str = ""
    detail: str = ""
    evidence: dict[str, str] = Field(default_factory=dict)


class VeriReelProdBackupGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    backup_record_id: str
    backup_status: str
    backup_started_at: str = ""
    backup_finished_at: str = ""
    snapshot_name: str = ""
    error_message: str = ""


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
            "VeriReel prod backup gate worker timed out after "
            f"{timeout_seconds} seconds."
        ) from exc
    stdout = completed.stdout.strip()
    if not stdout:
        detail = (
            completed.stderr.strip()
            or "VeriReel prod backup gate worker returned no JSON payload."
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
    return BackupGateRecord(
        record_id=request.backup_record_id,
        context=request.context,
        instance=request.instance,
        created_at=worker_result.finished_at or utc_now_timestamp(),
        source=ASYNC_SOURCE,
        required=True,
        status="pass" if worker_result.status == "pass" else "fail",
        evidence=evidence if worker_result.status == "pass" else {},
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
    record_store: FilesystemRecordStore,
    record_id: str,
) -> BackupGateRecord | None:
    try:
        return record_store.read_backup_gate_record(record_id)
    except FileNotFoundError:
        return None


def _run_backup_gate_worker_and_store(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    request: VeriReelProdBackupGateRequest,
) -> None:
    try:
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
            record_store.write_backup_gate_record(
                _failed_backup_gate_record(request=request, error_message=str(exc))
            )
            return
        record_store.write_backup_gate_record(
            _build_backup_gate_record(request=request, worker_result=worker_result)
        )
    finally:
        with _ACTIVE_BACKUP_GATES_LOCK:
            _ACTIVE_BACKUP_GATES.discard(request.backup_record_id)


def _ensure_async_backup_gate_worker(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    request: VeriReelProdBackupGateRequest,
) -> None:
    with _ACTIVE_BACKUP_GATES_LOCK:
        if request.backup_record_id in _ACTIVE_BACKUP_GATES:
            return
        _ACTIVE_BACKUP_GATES.add(request.backup_record_id)
    worker_thread = threading.Thread(
        target=_run_backup_gate_worker_and_store,
        kwargs={
            "control_plane_root": control_plane_root,
            "record_store": record_store,
            "request": request,
        },
        daemon=True,
        name=f"verireel-prod-backup-gate-{request.backup_record_id}",
    )
    worker_thread.start()


def execute_verireel_prod_backup_gate(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    request: VeriReelProdBackupGateRequest,
    run_async: bool = False,
) -> VeriReelProdBackupGateResult:
    if run_async:
        existing_record = _read_existing_backup_gate_record(
            record_store=record_store,
            record_id=request.backup_record_id,
        )
        if existing_record is None:
            existing_record = _pending_backup_gate_record(request=request)
            record_store.write_backup_gate_record(existing_record)
        if existing_record.status == "pending":
            _ensure_async_backup_gate_worker(
                control_plane_root=control_plane_root,
                record_store=record_store,
                request=request,
            )
        return _result_from_backup_gate_record(request=request, record=existing_record)

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
