from __future__ import annotations

from pathlib import Path
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.ship import utc_now_timestamp

SUPPORTED_ODOO_CONTEXTS = {"cm", "opw"}
BACKUP_GATE_SOURCE = "launchplane-odoo-prod-backup-gate"


class OdooProdBackupGateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str = "prod"
    backup_record_id: str
    timeout_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooProdBackupGateRequest":
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.backup_record_id = self.backup_record_id.strip()
        if self.context not in SUPPORTED_ODOO_CONTEXTS:
            supported = ", ".join(sorted(SUPPORTED_ODOO_CONTEXTS))
            raise ValueError(
                f"Odoo prod backup gate supports contexts {supported}; got {self.context!r}."
            )
        if self.instance != "prod":
            raise ValueError("Odoo prod backup gate requires instance 'prod'.")
        if not self.backup_record_id:
            raise ValueError("Odoo prod backup gate requires backup_record_id.")
        return self


class OdooProdBackupGateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    backup_record_id: str
    backup_status: Literal["pass", "fail"]
    backup_root: str = ""
    database_dump_path: str = ""
    filestore_archive_path: str = ""
    manifest_path: str = ""
    error_message: str = ""


def _read_target_definition(
    *,
    record_store: FilesystemRecordStore,
    request: OdooProdBackupGateRequest,
) -> control_plane_dokploy.DokployTargetDefinition:
    try:
        target_record = record_store.read_dokploy_target_record(
            context_name=request.context,
            instance_name=request.instance,
        )
        target_id_record = record_store.read_dokploy_target_id_record(
            context_name=request.context,
            instance_name=request.instance,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Odoo prod backup gate requires DB-backed Dokploy target records for {request.context}/{request.instance}."
        ) from exc
    payload = target_record.model_dump(
        mode="json",
        exclude={"schema_version", "updated_at", "source_label"},
    )
    payload["target_id"] = target_id_record.target_id
    target_definition = control_plane_dokploy.DokployTargetDefinition.model_validate(payload)
    if target_definition.target_type != "compose":
        raise click.ClickException(
            "Odoo prod backup gate requires a compose target in Launchplane Dokploy records. "
            f"Configured={target_definition.target_type}."
        )
    return target_definition


def _runtime_values(
    *,
    control_plane_root: Path,
    request: OdooProdBackupGateRequest,
) -> dict[str, str]:
    try:
        return control_plane_runtime_environments.resolve_runtime_environment_values(
            control_plane_root=control_plane_root,
            context_name=request.context,
            instance_name=request.instance,
        )
    except click.ClickException as error:
        raise click.ClickException(
            "Odoo prod backup gate requires DB-backed runtime environment records for "
            f"{request.context}/{request.instance}."
        ) from error


def _backup_paths(*, runtime_values: dict[str, str], backup_record_id: str) -> dict[str, str]:
    database_name = runtime_values.get("ODOO_DB_NAME", "").strip()
    if not database_name:
        raise click.ClickException(
            "Odoo prod backup gate requires ODOO_DB_NAME in DB-backed runtime environment records."
        )
    backup_root = (runtime_values.get("ODOO_BACKUP_ROOT") or "").strip()
    if not backup_root:
        raise click.ClickException(
            "Odoo prod backup gate requires ODOO_BACKUP_ROOT in DB-backed runtime environment records."
        )
    filestore_path = (
        runtime_values.get("ODOO_FILESTORE_PATH") or "/volumes/data/filestore"
    ).strip()
    backup_dir = f"{backup_root}/{database_name}/{backup_record_id}"
    return {
        "database_name": database_name,
        "filestore_path": filestore_path,
        "backup_root": backup_root,
        "backup_dir": backup_dir,
        "database_dump_path": f"{backup_dir}/{database_name}.dump",
        "filestore_archive_path": f"{backup_dir}/{database_name}-filestore.tar.gz",
        "manifest_path": f"{backup_dir}/manifest.json",
    }


def _write_backup_gate_record(
    *,
    record_store: FilesystemRecordStore,
    request: OdooProdBackupGateRequest,
    status: Literal["pending", "pass", "fail"],
    evidence: dict[str, str] | None = None,
) -> BackupGateRecord:
    record = BackupGateRecord(
        record_id=request.backup_record_id,
        context=request.context,
        instance=request.instance,
        created_at=utc_now_timestamp(),
        source=BACKUP_GATE_SOURCE,
        required=True,
        status=status,
        evidence=evidence or {},
    )
    record_store.write_backup_gate_record(record)
    return record


def execute_odoo_prod_backup_gate(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    request: OdooProdBackupGateRequest,
) -> OdooProdBackupGateResult:
    target_definition = _read_target_definition(record_store=record_store, request=request)
    runtime_values = _runtime_values(control_plane_root=control_plane_root, request=request)
    evidence = _backup_paths(
        runtime_values=runtime_values,
        backup_record_id=request.backup_record_id,
    )
    _write_backup_gate_record(
        record_store=record_store,
        request=request,
        status="pending",
        evidence={},
    )
    try:
        host, token = control_plane_dokploy.read_dokploy_config(
            control_plane_root=control_plane_root
        )
        control_plane_dokploy.run_compose_odoo_backup_gate(
            host=host,
            token=token,
            target_definition=target_definition,
            backup_record_id=request.backup_record_id,
            database_name=evidence["database_name"],
            filestore_path=evidence["filestore_path"],
            backup_root=evidence["backup_root"],
            timeout_seconds=request.timeout_seconds,
        )
    except (click.ClickException, OSError) as error:
        _write_backup_gate_record(
            record_store=record_store,
            request=request,
            status="fail",
            evidence={**evidence, "error_message": str(error)},
        )
        return OdooProdBackupGateResult(
            context=request.context,
            instance=request.instance,
            backup_record_id=request.backup_record_id,
            backup_status="fail",
            backup_root=evidence.get("backup_root", ""),
            database_dump_path=evidence.get("database_dump_path", ""),
            filestore_archive_path=evidence.get("filestore_archive_path", ""),
            manifest_path=evidence.get("manifest_path", ""),
            error_message=str(error),
        )
    _write_backup_gate_record(
        record_store=record_store,
        request=request,
        status="pass",
        evidence=evidence,
    )
    return OdooProdBackupGateResult(
        context=request.context,
        instance=request.instance,
        backup_record_id=request.backup_record_id,
        backup_status="pass",
        backup_root=evidence["backup_root"],
        database_dump_path=evidence["database_dump_path"],
        filestore_archive_path=evidence["filestore_archive_path"],
        manifest_path=evidence["manifest_path"],
    )
