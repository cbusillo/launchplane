from __future__ import annotations

import hashlib
import json
import re
import secrets as python_secrets
from pathlib import Path, PurePosixPath
from typing import Literal, Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.dokploy import source as dokploy_source
from control_plane.dokploy import post_deploy as dokploy_post_deploy

BACKUP_GATE_SOURCE = "launchplane-odoo-prod-backup-gate"
BACKUP_VERIFICATION_SOURCE = "launchplane-odoo-prod-backup-verification"
RETAINED_VOLUME_BACKUP_IMPORT_SOURCE = "launchplane-odoo-prod-retained-volume-backup-import"
VERIFIABLE_BACKUP_GATE_SOURCES = frozenset(
    {BACKUP_GATE_SOURCE, RETAINED_VOLUME_BACKUP_IMPORT_SOURCE}
)
_BACKUP_PATH_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
BackupVerificationCheckStatus = Literal["not_run", "pass", "fail"]
BackupVerificationFailureCode = Literal[
    "",
    "manifest_unreadable",
    "manifest_missing",
    "manifest_metadata_unreadable",
    "manifest_too_large",
    "manifest_read_error",
    "manifest_decode_error",
    "manifest_identity_mismatch",
    "manifest_path_mismatch",
    "manifest_size_invalid",
    "artifact_missing",
    "artifact_size_mismatch",
    "artifact_hash_mismatch",
    "database_dump_invalid",
    "filestore_archive_invalid",
    "filestore_members_unsafe",
    "filestore_top_level_mismatch",
    "staging_path_unavailable",
    "staging_space_insufficient",
    "manifest_path_verification_error",
    "manifest_read_verification_error",
    "manifest_validation_error",
    "artifact_path_verification_error",
    "artifact_metadata_verification_error",
    "verification_error",
    "provider_verification_failed",
]


class OdooProdBackupGateStore(Protocol):
    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...

    def write_backup_gate_record(self, record: BackupGateRecord) -> None: ...


class OdooProdBackupVerificationStore(Protocol):
    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...

    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord: ...

    def write_backup_gate_record(self, record: BackupGateRecord) -> None: ...


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
        if not self.context:
            raise ValueError("Odoo prod backup gate requires context.")
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


class OdooProdBackupCaptureEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int = Field(default=1, ge=1)
    backup_nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    backup_record_id: str = Field(min_length=1)
    database_name: str = Field(min_length=1)
    database_dump_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filestore_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_dump_size: int = Field(gt=0)
    filestore_archive_size: int = Field(gt=0)


class OdooProdBackupVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str = "prod"
    backup_record_id: str
    timeout_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooProdBackupVerificationRequest":
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.backup_record_id = self.backup_record_id.strip()
        if not self.context:
            raise ValueError("Odoo prod backup verification requires context.")
        if self.instance != "prod":
            raise ValueError("Odoo prod backup verification requires instance 'prod'.")
        if not _BACKUP_PATH_COMPONENT_PATTERN.fullmatch(self.backup_record_id):
            raise ValueError("Odoo prod backup verification requires a path-safe backup_record_id.")
        return self


class OdooProdBackupVerificationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int = Field(default=1, ge=1)
    verification_status: Literal["pass", "fail"]
    manifest_status: BackupVerificationCheckStatus = "not_run"
    sha256_status: BackupVerificationCheckStatus = "not_run"
    pg_restore_status: BackupVerificationCheckStatus = "not_run"
    tar_status: BackupVerificationCheckStatus = "not_run"
    staging_space_status: BackupVerificationCheckStatus = "not_run"
    database_dump_sha256: str = ""
    filestore_archive_sha256: str = ""
    database_dump_size: int = Field(default=0, ge=0)
    filestore_archive_size: int = Field(default=0, ge=0)
    pg_restore_entry_count: int = Field(default=0, ge=0)
    filestore_member_count: int = Field(default=0, ge=0)
    filestore_unpacked_size: int = Field(default=0, ge=0)
    data_volume_free_bytes: int = Field(default=0, ge=0)
    staging_required_bytes: int = Field(default=0, ge=0)
    failure_code: BackupVerificationFailureCode = ""

    @model_validator(mode="after")
    def _validate_evidence(self) -> "OdooProdBackupVerificationEvidence":
        for digest in (self.database_dump_sha256, self.filestore_archive_sha256):
            if digest and not re.fullmatch(r"[0-9a-f]{64}", digest):
                raise ValueError("Odoo backup verification hashes must be SHA-256 values.")
        check_statuses = (
            self.manifest_status,
            self.sha256_status,
            self.pg_restore_status,
            self.tar_status,
            self.staging_space_status,
        )
        if self.verification_status == "pass":
            if check_statuses != ("pass", "pass", "pass", "pass", "pass"):
                raise ValueError("Passing Odoo backup verification requires every check to pass.")
            if self.failure_code:
                raise ValueError("Passing Odoo backup verification cannot include failure_code.")
            if not self.database_dump_sha256 or not self.filestore_archive_sha256:
                raise ValueError("Passing Odoo backup verification requires SHA-256 hashes.")
            if (
                self.database_dump_size < 1
                or self.filestore_archive_size < 1
                or self.pg_restore_entry_count < 1
                or self.filestore_member_count < 1
            ):
                raise ValueError("Passing Odoo backup verification requires non-empty artifacts.")
            if self.data_volume_free_bytes < self.staging_required_bytes:
                raise ValueError("Passing Odoo backup verification requires staging free space.")
        elif not self.failure_code:
            raise ValueError("Failed Odoo backup verification requires failure_code.")
        return self


class OdooProdBackupVerificationResult(OdooProdBackupVerificationEvidence):
    context: str
    instance: str
    backup_record_id: str
    verification_record_id: str


def _read_target_definition(
    *,
    record_store: OdooProdBackupGateStore | OdooProdBackupVerificationStore,
    context: str,
    instance: str,
) -> dokploy_source.DokployTargetDefinition:
    try:
        target_record = record_store.read_dokploy_target_record(
            context_name=context,
            instance_name=instance,
        )
        target_id_record = record_store.read_dokploy_target_id_record(
            context_name=context,
            instance_name=instance,
        )
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"Odoo prod backup workflow requires DB-backed Dokploy target records for {context}/{instance}."
        ) from exc
    payload = target_record.model_dump(
        mode="json",
        exclude={"schema_version", "updated_at", "source_label"},
    )
    payload["target_id"] = target_id_record.target_id
    target_definition = dokploy_source.DokployTargetDefinition.model_validate(payload)
    if target_definition.target_type != "compose":
        raise click.ClickException(
            "Odoo prod backup gate requires a compose target in Launchplane Dokploy records. "
            f"Configured={target_definition.target_type}."
        )
    return target_definition


def _require_record_store(record_store: object) -> OdooProdBackupGateStore:
    required_methods = (
        "read_dokploy_target_record",
        "read_dokploy_target_id_record",
        "write_backup_gate_record",
    )
    missing_methods = tuple(
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    )
    if missing_methods:
        raise click.ClickException(
            "Odoo prod backup gate requires a DB-backed Launchplane record store. "
            f"Missing methods: {', '.join(missing_methods)}."
        )
    return cast(OdooProdBackupGateStore, record_store)


def _runtime_values(
    *,
    control_plane_root: Path,
    context: str,
    instance: str,
) -> dict[str, str]:
    try:
        return control_plane_runtime_environments.resolve_runtime_environment_values(
            control_plane_root=control_plane_root,
            context_name=context,
            instance_name=instance,
        )
    except click.ClickException as error:
        raise click.ClickException(
            "Odoo prod backup workflow requires DB-backed runtime environment records for "
            f"{context}/{instance}."
        ) from error


def _backup_paths(*, runtime_values: dict[str, str], backup_record_id: str) -> dict[str, str]:
    database_name = runtime_values.get("ODOO_DB_NAME", "").strip()
    if not _BACKUP_PATH_COMPONENT_PATTERN.fullmatch(database_name):
        raise click.ClickException(
            "Odoo prod backup workflow requires a path-safe ODOO_DB_NAME in DB-backed runtime environment records."
        )
    if not _BACKUP_PATH_COMPONENT_PATTERN.fullmatch(backup_record_id):
        raise click.ClickException("Odoo prod backup workflow requires a path-safe record id.")
    backup_root = _normalized_backup_root(
        runtime_values.get("ODOO_BACKUP_ROOT", ""),
    )
    if not backup_root:
        raise click.ClickException(
            "Odoo prod backup gate requires ODOO_BACKUP_ROOT in DB-backed runtime environment records."
        )
    filestore_path = _normalized_runtime_path(
        runtime_values.get("ODOO_FILESTORE_PATH") or "/volumes/data/filestore",
        label="ODOO_FILESTORE_PATH",
    )
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


def _normalized_backup_root(raw_value: str) -> str:
    backup_root = _normalized_runtime_path(raw_value, label="ODOO_BACKUP_ROOT")
    if not backup_root:
        return ""
    allowed_root = PurePosixPath(dokploy_post_deploy.DEFAULT_ODOO_BACKUP_ROOT)
    backup_root_path = PurePosixPath(backup_root)
    if backup_root_path != allowed_root and allowed_root not in backup_root_path.parents:
        raise click.ClickException(
            "Odoo prod backup workflow requires ODOO_BACKUP_ROOT to stay within the dedicated Launchplane backup root."
        )
    return backup_root


def _normalized_runtime_path(raw_value: str, *, label: str) -> str:
    normalized_value = raw_value.strip()
    if not normalized_value:
        return ""
    path = PurePosixPath(normalized_value)
    if not path.is_absolute() or ".." in path.parts:
        raise click.ClickException(
            f"Odoo prod backup workflow requires an absolute path-safe {label}."
        )
    return path.as_posix()


def _require_verification_record_store(record_store: object) -> OdooProdBackupVerificationStore:
    required_methods = (
        "read_dokploy_target_record",
        "read_dokploy_target_id_record",
        "read_backup_gate_record",
        "write_backup_gate_record",
    )
    missing_methods = tuple(
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    )
    if missing_methods:
        raise click.ClickException(
            "Odoo prod backup verification requires a DB-backed Launchplane record store. "
            f"Missing methods: {', '.join(missing_methods)}."
        )
    return cast(OdooProdBackupVerificationStore, record_store)


def _require_exact_passing_backup_record(
    *,
    record_store: OdooProdBackupVerificationStore,
    request: OdooProdBackupVerificationRequest,
    expected_paths: dict[str, str],
) -> BackupGateRecord:
    record = record_store.read_backup_gate_record(request.backup_record_id)
    if (
        record.record_id != request.backup_record_id
        or record.context != request.context
        or record.instance != request.instance
        or record.source not in VERIFIABLE_BACKUP_GATE_SOURCES
        or not record.required
        or record.status != "pass"
    ):
        raise click.ClickException(
            "Odoo prod backup verification requires the exact passing Odoo backup-gate record."
        )
    required_evidence_keys = (
        "database_name",
        "filestore_path",
        "backup_root",
        "backup_dir",
        "database_dump_path",
        "filestore_archive_path",
        "manifest_path",
    )
    if any(record.evidence.get(key) != expected_paths[key] for key in required_evidence_keys):
        raise click.ClickException(
            "Odoo prod backup verification requires backup-gate evidence to match current DB-backed path authority."
        )
    return record


def build_odoo_prod_backup_verification_record_id(
    *,
    context: str,
    instance: str,
    backup_record_id: str,
    verification_nonce: str,
    created_at: str,
) -> str:
    digest_input = json.dumps(
        [
            context.strip().lower(),
            instance.strip().lower(),
            backup_record_id.strip(),
            verification_nonce.strip(),
            created_at.strip(),
        ],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]
    normalized_created_at = created_at.strip().replace(":", "").replace("+", "z")
    return f"odoo-backup-verification-{normalized_created_at}-{digest}"


def _write_backup_verification_record(
    *,
    record_store: OdooProdBackupVerificationStore,
    request: OdooProdBackupVerificationRequest,
    backup_record: BackupGateRecord,
    expected_paths: dict[str, str],
    evidence: OdooProdBackupVerificationEvidence,
    verification_nonce: str,
    created_at: str,
) -> BackupGateRecord:
    verification_record = BackupGateRecord(
        record_id=build_odoo_prod_backup_verification_record_id(
            context=request.context,
            instance=request.instance,
            backup_record_id=request.backup_record_id,
            verification_nonce=verification_nonce,
            created_at=created_at,
        ),
        context=request.context,
        instance=request.instance,
        created_at=created_at,
        source=BACKUP_VERIFICATION_SOURCE,
        required=True,
        status=evidence.verification_status,
        evidence={
            "backup_record_id": request.backup_record_id,
            "backup_record_created_at": backup_record.created_at,
            "verification_nonce": verification_nonce,
            **expected_paths,
            **{
                key: str(value).lower() if isinstance(value, bool) else str(value)
                for key, value in evidence.model_dump(mode="json").items()
            },
        },
    )
    record_store.write_backup_gate_record(verification_record)
    return verification_record


def _write_backup_gate_record(
    *,
    record_store: OdooProdBackupGateStore,
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
    record_store: object,
    request: OdooProdBackupGateRequest,
) -> OdooProdBackupGateResult:
    typed_record_store = _require_record_store(record_store)
    target_definition = _read_target_definition(
        record_store=typed_record_store,
        context=request.context,
        instance=request.instance,
    )
    runtime_values = _runtime_values(
        control_plane_root=control_plane_root,
        context=request.context,
        instance=request.instance,
    )
    evidence = _backup_paths(
        runtime_values=runtime_values,
        backup_record_id=request.backup_record_id,
    )
    _write_backup_gate_record(
        record_store=typed_record_store,
        request=request,
        status="pending",
        evidence={},
    )
    backup_nonce = python_secrets.token_hex(32)
    try:
        host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
        raw_capture_evidence = dokploy_post_deploy.run_compose_odoo_backup_gate(
            host=host,
            token=token,
            target_definition=target_definition,
            backup_nonce=backup_nonce,
            backup_record_id=request.backup_record_id,
            database_name=evidence["database_name"],
            filestore_path=evidence["filestore_path"],
            backup_root=evidence["backup_root"],
            timeout_seconds=request.timeout_seconds,
        )
        try:
            capture_evidence = OdooProdBackupCaptureEvidence.model_validate(raw_capture_evidence)
        except ValidationError as error:
            raise click.ClickException(
                "Dokploy Odoo backup gate returned invalid bounded completion evidence."
            ) from error
        if (
            capture_evidence.backup_nonce != backup_nonce
            or capture_evidence.backup_record_id != request.backup_record_id
            or capture_evidence.database_name != evidence["database_name"]
        ):
            raise click.ClickException(
                "Dokploy Odoo backup gate result did not match the exact request."
            )
    except (click.ClickException, OSError) as error:
        _write_backup_gate_record(
            record_store=typed_record_store,
            request=request,
            status="fail",
            evidence={
                **evidence,
                "backup_nonce": backup_nonce,
                "error_message": str(error),
            },
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
    captured_evidence = {key: str(value) for key, value in capture_evidence.model_dump().items()}
    _write_backup_gate_record(
        record_store=typed_record_store,
        request=request,
        status="pass",
        evidence={**evidence, **captured_evidence},
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


def execute_odoo_prod_backup_verification(
    *,
    control_plane_root: Path,
    record_store: object,
    request: OdooProdBackupVerificationRequest,
) -> OdooProdBackupVerificationResult:
    typed_record_store = _require_verification_record_store(record_store)
    target_definition = _read_target_definition(
        record_store=typed_record_store,
        context=request.context,
        instance=request.instance,
    )
    runtime_values = _runtime_values(
        control_plane_root=control_plane_root,
        context=request.context,
        instance=request.instance,
    )
    expected_paths = _backup_paths(
        runtime_values=runtime_values,
        backup_record_id=request.backup_record_id,
    )
    backup_record = _require_exact_passing_backup_record(
        record_store=typed_record_store,
        request=request,
        expected_paths=expected_paths,
    )
    verification_nonce = python_secrets.token_hex(32)
    try:
        host, token = dokploy_source.read_dokploy_config(control_plane_root=control_plane_root)
        raw_evidence = dokploy_post_deploy.run_compose_odoo_backup_verification(
            host=host,
            token=token,
            target_definition=target_definition,
            verification_nonce=verification_nonce,
            backup_record_id=request.backup_record_id,
            database_name=expected_paths["database_name"],
            filestore_path=expected_paths["filestore_path"],
            backup_dir=expected_paths["backup_dir"],
            database_dump_path=expected_paths["database_dump_path"],
            filestore_archive_path=expected_paths["filestore_archive_path"],
            manifest_path=expected_paths["manifest_path"],
            timeout_seconds=request.timeout_seconds,
        )
        if (
            raw_evidence.pop("verification_nonce", None) != verification_nonce
            or raw_evidence.pop("backup_record_id", None) != request.backup_record_id
            or raw_evidence.pop("database_name", None) != expected_paths["database_name"]
        ):
            raise click.ClickException(
                "Dokploy Odoo backup verification result did not match the exact request."
            )
        verification_evidence = OdooProdBackupVerificationEvidence.model_validate(raw_evidence)
    except (click.ClickException, OSError, ValidationError):
        verification_evidence = OdooProdBackupVerificationEvidence(
            verification_status="fail",
            failure_code="provider_verification_failed",
        )
    verification_created_at = utc_now_timestamp()
    verification_record = _write_backup_verification_record(
        record_store=typed_record_store,
        request=request,
        backup_record=backup_record,
        expected_paths=expected_paths,
        evidence=verification_evidence,
        verification_nonce=verification_nonce,
        created_at=verification_created_at,
    )
    return OdooProdBackupVerificationResult(
        context=request.context,
        instance=request.instance,
        backup_record_id=request.backup_record_id,
        verification_record_id=verification_record.record_id,
        **verification_evidence.model_dump(),
    )
