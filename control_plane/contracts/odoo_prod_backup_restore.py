from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.workflows.odoo_verification import OdooVerificationEvidence


_DOCKER_VOLUME_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
ODOO_PROD_BACKUP_RESTORE_CONFIRMATION = "restore-verified-production-backup"


class OdooProdBackupRestoreRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    instance: Literal["prod"] = "prod"
    backup_record_id: str
    verification_record_id: str
    expected_current_artifact_id: str
    expected_old_db_volume: str
    expected_new_db_volume: str
    expected_data_volume: str
    expected_log_volume: str

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooProdBackupRestoreRequest":
        self.product = _normalize_required(self.product, "Odoo backup restore requires product.")
        self.context = _normalize_required(
            self.context, "Odoo backup restore requires context."
        ).lower()
        self.backup_record_id = _normalize_required(
            self.backup_record_id,
            "Odoo backup restore requires backup_record_id.",
        )
        self.verification_record_id = _normalize_required(
            self.verification_record_id,
            "Odoo backup restore requires verification_record_id.",
        )
        for field_name, record_id in (
            ("backup_record_id", self.backup_record_id),
            ("verification_record_id", self.verification_record_id),
        ):
            if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]*", record_id):
                raise ValueError(f"Odoo backup restore requires a path-safe {field_name}.")
        self.expected_current_artifact_id = _normalize_required(
            self.expected_current_artifact_id,
            "Odoo backup restore requires expected_current_artifact_id.",
        )
        volume_fields = {
            "expected_old_db_volume": self.expected_old_db_volume,
            "expected_new_db_volume": self.expected_new_db_volume,
            "expected_data_volume": self.expected_data_volume,
            "expected_log_volume": self.expected_log_volume,
        }
        normalized_volumes: dict[str, str] = {}
        for field_name, raw_value in volume_fields.items():
            normalized_value = _normalize_required(
                raw_value,
                f"Odoo backup restore requires {field_name}.",
            )
            if not _DOCKER_VOLUME_NAME_PATTERN.fullmatch(normalized_value):
                raise ValueError(
                    f"Odoo backup restore requires a path-safe Docker volume name for {field_name}."
                )
            normalized_volumes[field_name] = normalized_value
        self.expected_old_db_volume = normalized_volumes["expected_old_db_volume"]
        self.expected_new_db_volume = normalized_volumes["expected_new_db_volume"]
        self.expected_data_volume = normalized_volumes["expected_data_volume"]
        self.expected_log_volume = normalized_volumes["expected_log_volume"]
        if self.expected_old_db_volume == self.expected_new_db_volume:
            raise ValueError("Odoo backup restore requires different old and new DB volumes.")
        if (
            len(
                {
                    self.expected_new_db_volume,
                    self.expected_data_volume,
                    self.expected_log_volume,
                }
            )
            != 3
        ):
            raise ValueError(
                "Odoo backup restore requires the new DB, data, and log volumes to be distinct."
            )
        return self


class OdooProdBackupRestoreApplyRequest(OdooProdBackupRestoreRequest):
    plan_fingerprint: str
    confirmation: str
    no_cache: bool = False
    timeout_seconds: int | None = Field(default=None, ge=1)
    health_timeout_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_apply_request(self) -> "OdooProdBackupRestoreApplyRequest":
        self.plan_fingerprint = self.plan_fingerprint.strip().lower()
        self.confirmation = self.confirmation.strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", self.plan_fingerprint):
            raise ValueError("Odoo backup restore apply requires a SHA-256 plan_fingerprint.")
        if self.confirmation != ODOO_PROD_BACKUP_RESTORE_CONFIRMATION:
            raise ValueError(
                "Odoo backup restore apply requires the exact destructive confirmation phrase."
            )
        return self


class OdooProdBackupRestoreStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    status: Literal["ready", "blocked"]
    message: str


class OdooProdBackupRestorePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    plan_status: Literal["ready", "blocked"]
    plan_fingerprint: str = ""
    product: str
    context: str
    instance: Literal["prod"]
    backup_record_id: str
    backup_record_created_at: str = ""
    verification_record_id: str
    verification_record_created_at: str = ""
    verification_nonce: str = ""
    verification_status: str = ""
    manifest_status: str = ""
    sha256_status: str = ""
    pg_restore_status: str = ""
    tar_status: str = ""
    staging_space_status: str = ""
    target_id: str = ""
    target_name: str = ""
    expected_current_artifact_id: str
    expected_source_git_ref: str = ""
    image_reference: str = ""
    database_name: str = ""
    database_dump_path: str = ""
    filestore_archive_path: str = ""
    manifest_path: str = ""
    database_dump_sha256: str = ""
    filestore_archive_sha256: str = ""
    database_dump_size: int = Field(default=0, ge=0)
    filestore_archive_size: int = Field(default=0, ge=0)
    pg_restore_entry_count: int = Field(default=0, ge=0)
    filestore_member_count: int = Field(default=0, ge=0)
    filestore_unpacked_size: int = Field(default=0, ge=0)
    data_volume_free_bytes: int = Field(default=0, ge=0)
    staging_required_bytes: int = Field(default=0, ge=0)
    old_db_volume: str
    new_db_volume: str
    data_volume: str
    log_volume: str
    filestore_path: str = ""
    filestore_staging_path: str = ""
    filestore_quarantine_path: str = ""
    base_url: str = ""
    health_url: str = ""
    expected_domain_hosts: tuple[str, ...] = ()
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    steps: tuple[OdooProdBackupRestoreStep, ...] = ()

    @model_validator(mode="after")
    def _validate_plan(self) -> "OdooProdBackupRestorePlan":
        if self.plan_status == "ready":
            if self.blockers:
                raise ValueError("Ready Odoo backup restore plans cannot include blockers.")
            if not re.fullmatch(r"[0-9a-f]{64}", self.plan_fingerprint):
                raise ValueError("Ready Odoo backup restore plans require a fingerprint.")
            required_strings = (
                self.backup_record_created_at,
                self.verification_record_created_at,
                self.verification_nonce,
                self.target_id,
                self.target_name,
                self.expected_source_git_ref,
                self.image_reference,
                self.database_name,
                self.database_dump_path,
                self.filestore_archive_path,
                self.manifest_path,
                self.database_dump_sha256,
                self.filestore_archive_sha256,
                self.filestore_path,
                self.filestore_staging_path,
                self.filestore_quarantine_path,
                self.base_url,
                self.health_url,
            )
            if any(not value.strip() for value in required_strings):
                raise ValueError(
                    "Ready Odoo backup restore plans require complete authority evidence."
                )
            if not re.fullmatch(r"[0-9a-f]{64}", self.verification_nonce):
                raise ValueError(
                    "Ready Odoo backup restore plans require the exact verification nonce."
                )
            if not self.expected_domain_hosts:
                raise ValueError("Ready Odoo backup restore plans require expected domains.")
            if any(
                status != "pass"
                for status in (
                    self.verification_status,
                    self.manifest_status,
                    self.sha256_status,
                    self.pg_restore_status,
                    self.tar_status,
                    self.staging_space_status,
                )
            ):
                raise ValueError("Ready Odoo backup restore plans require passing verification.")
            if (
                self.staging_required_bytes < 1
                or self.data_volume_free_bytes < self.staging_required_bytes
            ):
                raise ValueError(
                    "Ready Odoo backup restore plans require bounded staging-space evidence."
                )
        elif self.plan_fingerprint:
            raise ValueError("Blocked Odoo backup restore plans cannot expose a fingerprint.")
        return self


class OdooProdBackupRestoreResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: Literal["prod"]
    backup_record_id: str
    verification_record_id: str
    plan_fingerprint: str
    deployment_record_id: str = ""
    release_tuple_id: str = ""
    restore_status: Literal["pass", "fail"]
    database_restore_status: Literal["pass", "fail", "skipped"] = "skipped"
    filestore_stage_status: Literal["pass", "fail", "skipped"] = "skipped"
    web_quiesce_status: Literal["pass", "fail", "skipped"] = "skipped"
    filestore_activation_status: Literal["pass", "fail", "skipped"] = "skipped"
    deployment_status: Literal["pass", "fail", "skipped"] = "skipped"
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
    health_status: Literal["pass", "fail", "skipped"] = "skipped"
    canonical_status: Literal["pass", "fail", "skipped"] = "skipped"
    logo_status: Literal["pass", "fail", "skipped"] = "skipped"
    runtime_identity_status: Literal[
        "match",
        "mismatch",
        "missing",
        "malformed",
        "unverifiable",
        "unchecked",
        "skipped",
    ] = "skipped"
    old_db_volume: str
    new_db_volume: str
    data_volume: str
    log_volume: str
    filestore_quarantine_path: str = ""
    database_dump_sha256: str
    filestore_archive_sha256: str
    verification_evidence: OdooVerificationEvidence = Field(
        default_factory=OdooVerificationEvidence
    )
    phase_evidence: dict[str, dict[str, str]] = Field(default_factory=dict)
    error_message: str = ""


def build_odoo_prod_backup_restore_plan_fingerprint(
    plan: OdooProdBackupRestorePlan,
) -> str:
    authority = {
        "schema_version": plan.schema_version,
        "product": plan.product,
        "context": plan.context,
        "instance": plan.instance,
        "backup_record_id": plan.backup_record_id,
        "backup_record_created_at": plan.backup_record_created_at,
        "verification_record_id": plan.verification_record_id,
        "verification_record_created_at": plan.verification_record_created_at,
        "verification_nonce": plan.verification_nonce,
        "verification_status": plan.verification_status,
        "manifest_status": plan.manifest_status,
        "sha256_status": plan.sha256_status,
        "pg_restore_status": plan.pg_restore_status,
        "tar_status": plan.tar_status,
        "staging_space_status": plan.staging_space_status,
        "target_id": plan.target_id,
        "target_name": plan.target_name,
        "expected_current_artifact_id": plan.expected_current_artifact_id,
        "expected_source_git_ref": plan.expected_source_git_ref,
        "image_reference": plan.image_reference,
        "database_name": plan.database_name,
        "database_dump_path": plan.database_dump_path,
        "filestore_archive_path": plan.filestore_archive_path,
        "manifest_path": plan.manifest_path,
        "database_dump_sha256": plan.database_dump_sha256,
        "filestore_archive_sha256": plan.filestore_archive_sha256,
        "database_dump_size": plan.database_dump_size,
        "filestore_archive_size": plan.filestore_archive_size,
        "pg_restore_entry_count": plan.pg_restore_entry_count,
        "filestore_member_count": plan.filestore_member_count,
        "filestore_unpacked_size": plan.filestore_unpacked_size,
        "data_volume_free_bytes": plan.data_volume_free_bytes,
        "staging_required_bytes": plan.staging_required_bytes,
        "old_db_volume": plan.old_db_volume,
        "new_db_volume": plan.new_db_volume,
        "data_volume": plan.data_volume,
        "log_volume": plan.log_volume,
        "filestore_path": plan.filestore_path,
        "filestore_staging_path": plan.filestore_staging_path,
        "filestore_quarantine_path": plan.filestore_quarantine_path,
        "base_url": plan.base_url,
        "health_url": plan.health_url,
        "expected_domain_hosts": list(plan.expected_domain_hosts),
    }
    encoded = json.dumps(authority, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_required(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized
