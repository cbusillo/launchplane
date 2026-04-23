from __future__ import annotations

import json
import os
from pathlib import Path
import shlex
import subprocess
import time
from urllib.error import HTTPError, URLError

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.promotion_record import (
    HealthcheckEvidence,
    PromotionRecord,
    RollbackExecutionEvidence,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.verireel_prod_promotion import (
    DEFAULT_ROLLOUT_INTERVAL_SECONDS,
    DEFAULT_ROLLOUT_TIMEOUT_SECONDS,
    VeriReelRolloutVerificationResult,
    _assert_rollout_pages,
    _fetch_url_text,
    _read_backup_gate_record,
    _resolve_rollout_base_urls,
    _validate_health_payload,
)


class VeriReelProdRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str = "verireel"
    instance: str = "prod"
    promotion_record_id: str
    backup_record_id: str
    snapshot_name: str = ""
    expected_build_revision: str = ""
    expected_build_tag: str = ""
    rollout_timeout_seconds: int = Field(default=DEFAULT_ROLLOUT_TIMEOUT_SECONDS, ge=1)
    rollout_interval_seconds: int = Field(default=DEFAULT_ROLLOUT_INTERVAL_SECONDS, ge=1)
    start_after_rollback: bool = True

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelProdRollbackRequest":
        if self.context != "verireel":
            raise ValueError("VeriReel prod rollback requires context 'verireel'.")
        if self.instance != "prod":
            raise ValueError("VeriReel prod rollback requires instance 'prod'.")
        if not self.promotion_record_id.strip():
            raise ValueError("VeriReel prod rollback requires promotion_record_id.")
        if not self.backup_record_id.strip():
            raise ValueError("VeriReel prod rollback requires backup_record_id.")
        return self


class VeriReelProdRollbackWorkerRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str
    promotion_record_id: str
    backup_record_id: str
    snapshot_name: str
    start_after_rollback: bool = True
    timeout_seconds: int = Field(default=DEFAULT_ROLLOUT_TIMEOUT_SECONDS, ge=1)


class VeriReelProdRollbackWorkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: str
    snapshot_name: str
    started_at: str = ""
    finished_at: str = ""
    detail: str = ""


class VeriReelProdRollbackResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    promotion_record_id: str
    backup_record_id: str
    snapshot_name: str = ""
    rollback_status: str
    rollback_health_status: str = "skipped"
    rollback_started_at: str = ""
    rollback_finished_at: str = ""
    error_message: str = ""


WORKER_COMMAND_ENV_VAR = "LAUNCHPLANE_VERIREEL_PROD_ROLLBACK_WORKER_COMMAND"
WORKER_RUNTIME_ENV_KEYS = (
    WORKER_COMMAND_ENV_VAR,
    "VERIREEL_PROD_PROXMOX_HOST",
    "VERIREEL_PROD_PROXMOX_USER",
    "VERIREEL_PROD_PROXMOX_SSH_PRIVATE_KEY",
    "VERIREEL_PROD_PROXMOX_SSH_KNOWN_HOSTS",
    "VERIREEL_PROD_CT_ID",
    "VERIREEL_PROD_GATE_LOCAL",
)


def _read_promotion_record(
    *,
    record_store: FilesystemRecordStore,
    record_id: str,
) -> PromotionRecord:
    try:
        return record_store.read_promotion_record(record_id)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"VeriReel prod rollback requires stored promotion record '{record_id}'."
        ) from exc


def _resolve_backup_gate_record(
    *,
    record_store: FilesystemRecordStore,
    request: VeriReelProdRollbackRequest,
) -> BackupGateRecord:
    backup_gate_record = _read_backup_gate_record(
        record_store=record_store,
        record_id=request.backup_record_id,
    )
    if backup_gate_record.context != request.context:
        raise click.ClickException(
            "Backup gate record context does not match VeriReel prod rollback request. "
            f"Record={backup_gate_record.context} request={request.context}."
        )
    if backup_gate_record.instance != request.instance:
        raise click.ClickException(
            "Backup gate record instance does not match VeriReel prod rollback destination. "
            f"Record={backup_gate_record.instance} request={request.instance}."
        )
    if backup_gate_record.status != "pass":
        raise click.ClickException(
            f"Backup gate record '{backup_gate_record.record_id}' must have status=pass before VeriReel prod rollback."
        )
    return backup_gate_record


def _resolve_snapshot_name(
    *,
    request: VeriReelProdRollbackRequest,
    backup_gate_record: BackupGateRecord,
) -> str:
    explicit_name = request.snapshot_name.strip()
    if explicit_name:
        return explicit_name
    snapshot_name = str(backup_gate_record.evidence.get("snapshot_name") or "").strip()
    if snapshot_name:
        return snapshot_name
    raise click.ClickException(
        f"Backup gate record '{backup_gate_record.record_id}' does not include snapshot_name evidence required for VeriReel prod rollback."
    )


def _verify_post_rollback_health(
    *,
    control_plane_root: Path,
    request: VeriReelProdRollbackRequest,
) -> VeriReelRolloutVerificationResult:
    started_at = utc_now_timestamp()
    base_urls = _resolve_rollout_base_urls(
        control_plane_root=control_plane_root,
        request=request,
    )
    health_urls = tuple(f"{base_url.rstrip('/')}/api/health" for base_url in base_urls)
    last_error = "health endpoint not checked yet"
    deadline = time.monotonic() + request.rollout_timeout_seconds
    while time.monotonic() <= deadline:
        for base_url, health_url in zip(base_urls, health_urls, strict=False):
            try:
                status_code, response_text = _fetch_url_text(
                    health_url,
                    accept="application/json,text/html",
                )
                if status_code < 200 or status_code >= 300:
                    last_error = f"received {status_code} from {health_url}"
                    continue
                payload = json.loads(response_text)
                validation_error = _validate_health_payload(
                    payload,
                    health_url=health_url,
                    expected_build_revision=request.expected_build_revision,
                    expected_build_tag=request.expected_build_tag,
                )
                if validation_error is not None:
                    last_error = validation_error
                    continue
                _assert_rollout_pages(base_url)
                return VeriReelRolloutVerificationResult(
                    status="pass",
                    base_url=base_url,
                    health_urls=(health_url,),
                    started_at=started_at,
                    finished_at=utc_now_timestamp(),
                )
            except (HTTPError, URLError, TimeoutError, json.JSONDecodeError, ValueError) as exc:
                last_error = str(exc)
            except click.ClickException as exc:
                raise click.ClickException(str(exc)) from exc
        remaining_seconds = deadline - time.monotonic()
        if remaining_seconds <= 0:
            break
        time.sleep(min(request.rollout_interval_seconds, remaining_seconds))
    raise click.ClickException(
        f"VeriReel prod rollback health verification timed out: {last_error}"
    )


def _build_health_evidence(
    *,
    request: VeriReelProdRollbackRequest,
    health_result: VeriReelRolloutVerificationResult | None,
) -> HealthcheckEvidence:
    if health_result is None:
        return HealthcheckEvidence(status="skipped")
    if not health_result.health_urls:
        return HealthcheckEvidence(status=health_result.status)
    return HealthcheckEvidence(
        verified=True,
        urls=health_result.health_urls,
        timeout_seconds=request.rollout_timeout_seconds,
        status=health_result.status,
    )


def _write_promotion_rollback_state(
    *,
    record_store: FilesystemRecordStore,
    promotion_record: PromotionRecord,
    request: VeriReelProdRollbackRequest,
    snapshot_name: str,
    rollback_status: str,
    rollback_health_status: str,
    rollback_started_at: str,
    rollback_finished_at: str,
    detail: str,
    health_result: VeriReelRolloutVerificationResult | None,
) -> None:
    updated_record = promotion_record.model_copy(
        update={
            "rollback": RollbackExecutionEvidence(
                attempted=True,
                status=rollback_status,
                detail=detail,
                snapshot_name=snapshot_name,
                started_at=rollback_started_at,
                finished_at=rollback_finished_at,
            ),
            "rollback_health": _build_health_evidence(
                request=request,
                health_result=(
                    health_result if rollback_health_status in {"pass", "fail"} else None
                ),
            ),
        },
        deep=True,
    )
    record_store.write_promotion_record(updated_record)


def _resolve_worker_runtime_environment(
    *,
    control_plane_root: Path,
    request: VeriReelProdRollbackWorkerRequest,
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
    request: VeriReelProdRollbackWorkerRequest,
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
            f"Missing {WORKER_COMMAND_ENV_VAR} for VeriReel prod rollback execution."
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
    request: VeriReelProdRollbackWorkerRequest,
) -> VeriReelProdRollbackWorkerResult:
    worker_environment = _worker_environment(
        control_plane_root=control_plane_root,
        request=request,
    )
    completed = subprocess.run(
        _worker_command(environment=worker_environment),
        input=request.model_dump_json(),
        text=True,
        capture_output=True,
        timeout=max(request.timeout_seconds, 1),
        check=False,
        env=worker_environment,
    )
    stdout = completed.stdout.strip()
    if not stdout:
        detail = (
            completed.stderr.strip() or "VeriReel prod rollback worker returned no JSON payload."
        )
        raise click.ClickException(detail)
    try:
        payload = json.loads(stdout)
    except json.JSONDecodeError as exc:
        raise click.ClickException(
            f"VeriReel prod rollback worker returned invalid JSON: {stdout}"
        ) from exc
    try:
        result = VeriReelProdRollbackWorkerResult.model_validate(payload)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(
            f"VeriReel prod rollback worker returned invalid result payload: {payload}"
        ) from exc
    if completed.returncode != 0 and result.status != "pass":
        return result
    if completed.returncode != 0:
        detail = result.detail or completed.stderr.strip() or stdout
        raise click.ClickException(detail)
    return result


def execute_verireel_prod_rollback(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    request: VeriReelProdRollbackRequest,
) -> VeriReelProdRollbackResult:
    try:
        promotion_record = _read_promotion_record(
            record_store=record_store,
            record_id=request.promotion_record_id,
        )
        backup_gate_record = _resolve_backup_gate_record(
            record_store=record_store,
            request=request,
        )
        snapshot_name = _resolve_snapshot_name(
            request=request,
            backup_gate_record=backup_gate_record,
        )
    except click.ClickException as exc:
        return VeriReelProdRollbackResult(
            promotion_record_id=request.promotion_record_id,
            backup_record_id=request.backup_record_id,
            rollback_status="fail",
            rollback_health_status="skipped",
            error_message=str(exc),
        )

    if (
        promotion_record.context != request.context
        or promotion_record.to_instance != request.instance
    ):
        error_message = (
            "Promotion record does not match VeriReel prod rollback request. "
            f"Record={promotion_record.context}/{promotion_record.to_instance} "
            f"request={request.context}/{request.instance}."
        )
        _write_promotion_rollback_state(
            record_store=record_store,
            promotion_record=promotion_record,
            request=request,
            snapshot_name=snapshot_name,
            rollback_status="fail",
            rollback_health_status="skipped",
            rollback_started_at="",
            rollback_finished_at="",
            detail=error_message,
            health_result=None,
        )
        return VeriReelProdRollbackResult(
            promotion_record_id=request.promotion_record_id,
            backup_record_id=request.backup_record_id,
            snapshot_name=snapshot_name,
            rollback_status="fail",
            rollback_health_status="skipped",
            error_message=error_message,
        )

    if (
        promotion_record.backup_record_id
        and promotion_record.backup_record_id != request.backup_record_id
    ):
        error_message = (
            "Promotion record backup_record_id does not match VeriReel prod rollback request. "
            f"Record={promotion_record.backup_record_id} request={request.backup_record_id}."
        )
        _write_promotion_rollback_state(
            record_store=record_store,
            promotion_record=promotion_record,
            request=request,
            snapshot_name=snapshot_name,
            rollback_status="fail",
            rollback_health_status="skipped",
            rollback_started_at="",
            rollback_finished_at="",
            detail=error_message,
            health_result=None,
        )
        return VeriReelProdRollbackResult(
            promotion_record_id=request.promotion_record_id,
            backup_record_id=request.backup_record_id,
            snapshot_name=snapshot_name,
            rollback_status="fail",
            rollback_health_status="skipped",
            error_message=error_message,
        )

    try:
        worker_result = _run_delegated_worker(
            control_plane_root=control_plane_root,
            request=VeriReelProdRollbackWorkerRequest(
                context=request.context,
                instance=request.instance,
                promotion_record_id=request.promotion_record_id,
                backup_record_id=request.backup_record_id,
                snapshot_name=snapshot_name,
                start_after_rollback=request.start_after_rollback,
                timeout_seconds=request.rollout_timeout_seconds,
            ),
        )
    except click.ClickException as exc:
        error_message = str(exc)
        _write_promotion_rollback_state(
            record_store=record_store,
            promotion_record=promotion_record,
            request=request,
            snapshot_name=snapshot_name,
            rollback_status="fail",
            rollback_health_status="skipped",
            rollback_started_at="",
            rollback_finished_at="",
            detail=error_message,
            health_result=None,
        )
        return VeriReelProdRollbackResult(
            promotion_record_id=request.promotion_record_id,
            backup_record_id=request.backup_record_id,
            snapshot_name=snapshot_name,
            rollback_status="fail",
            rollback_health_status="skipped",
            error_message=error_message,
        )

    if worker_result.status != "pass":
        detail = worker_result.detail or "VeriReel prod rollback worker reported failure."
        _write_promotion_rollback_state(
            record_store=record_store,
            promotion_record=promotion_record,
            request=request,
            snapshot_name=snapshot_name,
            rollback_status="fail",
            rollback_health_status="skipped",
            rollback_started_at=worker_result.started_at,
            rollback_finished_at=worker_result.finished_at,
            detail=detail,
            health_result=None,
        )
        return VeriReelProdRollbackResult(
            promotion_record_id=request.promotion_record_id,
            backup_record_id=request.backup_record_id,
            snapshot_name=snapshot_name,
            rollback_status="fail",
            rollback_health_status="skipped",
            rollback_started_at=worker_result.started_at,
            rollback_finished_at=worker_result.finished_at,
            error_message=detail,
        )

    try:
        health_result = _verify_post_rollback_health(
            control_plane_root=control_plane_root,
            request=request,
        )
    except click.ClickException as exc:
        error_message = str(exc)
        failed_health_result = VeriReelRolloutVerificationResult(status="fail")
        try:
            base_urls = _resolve_rollout_base_urls(
                control_plane_root=control_plane_root,
                request=request,
            )
            failed_health_result = VeriReelRolloutVerificationResult(
                status="fail",
                base_url=base_urls[0],
                health_urls=(f"{base_urls[0].rstrip('/')}/api/health",),
            )
        except click.ClickException:
            pass
        _write_promotion_rollback_state(
            record_store=record_store,
            promotion_record=promotion_record,
            request=request,
            snapshot_name=snapshot_name,
            rollback_status="pass",
            rollback_health_status="fail",
            rollback_started_at=worker_result.started_at,
            rollback_finished_at=worker_result.finished_at,
            detail=worker_result.detail,
            health_result=failed_health_result,
        )
        return VeriReelProdRollbackResult(
            promotion_record_id=request.promotion_record_id,
            backup_record_id=request.backup_record_id,
            snapshot_name=snapshot_name,
            rollback_status="pass",
            rollback_health_status="fail",
            rollback_started_at=worker_result.started_at,
            rollback_finished_at=worker_result.finished_at,
            error_message=error_message,
        )

    _write_promotion_rollback_state(
        record_store=record_store,
        promotion_record=promotion_record,
        request=request,
        snapshot_name=snapshot_name,
        rollback_status="pass",
        rollback_health_status=health_result.status,
        rollback_started_at=worker_result.started_at,
        rollback_finished_at=worker_result.finished_at,
        detail=worker_result.detail,
        health_result=health_result,
    )
    return VeriReelProdRollbackResult(
        promotion_record_id=request.promotion_record_id,
        backup_record_id=request.backup_record_id,
        snapshot_name=snapshot_name,
        rollback_status="pass",
        rollback_health_status=health_result.status,
        rollback_started_at=worker_result.started_at,
        rollback_finished_at=worker_result.finished_at,
        error_message="",
    )
