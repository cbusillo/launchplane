from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runtime_identity import RuntimeIdentity


_DOCKER_VOLUME_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,254}")
_PATH_COMPONENT_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_SHA256_PATTERN = re.compile(r"[0-9a-f]{64}")
_DOCKER_IMAGE_ID_PATTERN = re.compile(r"sha256:[0-9a-f]{64}")

ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_CONFIRMATION = (
    "import-retained-volumes-as-production-backup"
)
OdooProdRetainedVolumeBackupImportInspectionFailureStage = Literal[
    "provider_control",
    "active_runtime",
    "source_safety",
    "source_database",
    "source_filestore",
    "destination",
    "result",
]
OdooProdRetainedVolumeBackupImportInspectionFailureCode = Literal[
    "provider_target_read_failed",
    "provider_target_identity_mismatch",
    "provider_schedule_runtime_resolution_failed",
    "provider_schedule_upsert_failed",
    "provider_schedule_identity_missing",
    "provider_schedule_baseline_read_failed",
    "provider_schedule_trigger_failed",
    "provider_schedule_wait_failed",
    "provider_deployment_identity_missing",
    "provider_result_log_read_failed",
    "provider_result_invalid",
    "active_volume_missing",
    "source_volume_missing",
    "source_volume_usage_read_failed",
    "source_volume_in_use",
    "active_database_container_unavailable",
    "script_runner_container_unavailable",
    "active_runtime_inspection_failed",
    "active_runtime_identity_mismatch",
    "active_volume_mount_mismatch",
    "active_image_invalid",
    "active_postgres_version_mismatch",
    "source_volume_label_read_failed",
    "source_postgres_metadata_read_failed",
    "source_db_measurement_failed",
    "source_filestore_measurement_failed",
    "active_data_space_read_failed",
    "destination_check_failed",
    "result_emit_failed",
]
ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_FAILURE_STAGE_BY_CODE: dict[str, str] = {
    "provider_target_read_failed": "provider_control",
    "provider_target_identity_mismatch": "provider_control",
    "provider_schedule_runtime_resolution_failed": "provider_control",
    "provider_schedule_upsert_failed": "provider_control",
    "provider_schedule_identity_missing": "provider_control",
    "provider_schedule_baseline_read_failed": "provider_control",
    "provider_schedule_trigger_failed": "provider_control",
    "provider_schedule_wait_failed": "provider_control",
    "provider_deployment_identity_missing": "provider_control",
    "provider_result_log_read_failed": "result",
    "provider_result_invalid": "result",
    "active_volume_missing": "active_runtime",
    "source_volume_missing": "source_safety",
    "source_volume_usage_read_failed": "source_safety",
    "source_volume_in_use": "source_safety",
    "active_database_container_unavailable": "active_runtime",
    "script_runner_container_unavailable": "active_runtime",
    "active_runtime_inspection_failed": "active_runtime",
    "active_runtime_identity_mismatch": "active_runtime",
    "active_volume_mount_mismatch": "active_runtime",
    "active_image_invalid": "active_runtime",
    "active_postgres_version_mismatch": "active_runtime",
    "source_volume_label_read_failed": "source_safety",
    "source_postgres_metadata_read_failed": "source_database",
    "source_db_measurement_failed": "source_database",
    "source_filestore_measurement_failed": "source_filestore",
    "active_data_space_read_failed": "destination",
    "destination_check_failed": "destination",
    "result_emit_failed": "result",
}


class OdooProdRetainedVolumeBackupImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    instance: Literal["prod"] = "prod"
    backup_record_id: str
    expected_current_artifact_id: str
    expected_active_db_volume: str
    expected_active_data_volume: str
    expected_active_log_volume: str
    source_db_volume: str
    source_data_volume: str
    source_database_name: str
    expected_destination_database_name: str
    expected_database_user: str
    staging_clone_volume: str
    expected_source_compose_project: str

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooProdRetainedVolumeBackupImportRequest":
        self.product = _normalize_required(
            self.product,
            "Odoo retained-volume backup import requires product.",
        )
        self.context = _normalize_required(
            self.context,
            "Odoo retained-volume backup import requires context.",
        ).lower()
        self.backup_record_id = _normalize_path_component(
            self.backup_record_id,
            "Odoo retained-volume backup import requires a path-safe backup_record_id.",
        )
        self.expected_current_artifact_id = _normalize_required(
            self.expected_current_artifact_id,
            "Odoo retained-volume backup import requires expected_current_artifact_id.",
        )
        self.source_database_name = _normalize_path_component(
            self.source_database_name,
            "Odoo retained-volume backup import requires a path-safe source_database_name.",
        )
        self.expected_destination_database_name = _normalize_path_component(
            self.expected_destination_database_name,
            "Odoo retained-volume backup import requires a path-safe expected_destination_database_name.",
        )
        self.expected_database_user = _normalize_path_component(
            self.expected_database_user,
            "Odoo retained-volume backup import requires a path-safe expected_database_user.",
        )
        self.expected_source_compose_project = _normalize_path_component(
            self.expected_source_compose_project,
            "Odoo retained-volume backup import requires a path-safe expected_source_compose_project.",
        )
        if self.source_database_name == self.expected_destination_database_name:
            raise ValueError(
                "Odoo retained-volume backup import requires different source and destination database names."
            )

        volume_fields = (
            "expected_active_db_volume",
            "expected_active_data_volume",
            "expected_active_log_volume",
            "source_db_volume",
            "source_data_volume",
            "staging_clone_volume",
        )
        for field_name in volume_fields:
            normalized_value = _normalize_required(
                str(getattr(self, field_name)),
                f"Odoo retained-volume backup import requires {field_name}.",
            )
            if not _DOCKER_VOLUME_NAME_PATTERN.fullmatch(normalized_value):
                raise ValueError(
                    "Odoo retained-volume backup import requires path-safe Docker volume names."
                )
            setattr(self, field_name, normalized_value)

        active_volumes = {
            self.expected_active_db_volume,
            self.expected_active_data_volume,
            self.expected_active_log_volume,
        }
        if len(active_volumes) != 3:
            raise ValueError(
                "Odoo retained-volume backup import requires distinct active DB, data, and log volumes."
            )
        retained_volumes = {self.source_db_volume, self.source_data_volume}
        if len(retained_volumes) != 2:
            raise ValueError(
                "Odoo retained-volume backup import requires distinct source DB and data volumes."
            )
        if retained_volumes & active_volumes:
            raise ValueError(
                "Odoo retained-volume backup import source volumes must not be active target volumes."
            )
        if self.staging_clone_volume in active_volumes | retained_volumes:
            raise ValueError(
                "Odoo retained-volume backup import staging volume must be fresh and distinct."
            )
        return self


class OdooProdRetainedVolumeBackupImportApplyRequest(OdooProdRetainedVolumeBackupImportRequest):
    plan_operation_id: str
    plan_fingerprint: str
    confirmation: str
    timeout_seconds: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_apply_request(self) -> "OdooProdRetainedVolumeBackupImportApplyRequest":
        self.plan_operation_id = _normalize_path_component(
            self.plan_operation_id,
            "Odoo retained-volume backup import apply requires a path-safe plan_operation_id.",
        )
        self.plan_fingerprint = self.plan_fingerprint.strip().lower()
        self.confirmation = self.confirmation.strip().lower()
        if not _SHA256_PATTERN.fullmatch(self.plan_fingerprint):
            raise ValueError(
                "Odoo retained-volume backup import apply requires a SHA-256 plan_fingerprint."
            )
        if self.confirmation != ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_CONFIRMATION:
            raise ValueError(
                "Odoo retained-volume backup import apply requires the exact confirmation phrase."
            )
        return self


class OdooProdRetainedVolumeBackupImportStep(BaseModel):
    model_config = ConfigDict(extra="forbid")

    step_id: str
    status: Literal["ready", "blocked"]
    message: str


class OdooProdRetainedVolumeBackupImportInspectionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int = Field(default=1, ge=1)
    inspection_nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    backup_record_id: str = Field(min_length=1)
    active_db_volume: str = Field(min_length=1)
    active_data_volume: str = Field(min_length=1)
    active_log_volume: str = Field(min_length=1)
    source_db_volume: str = Field(min_length=1)
    source_data_volume: str = Field(min_length=1)
    staging_clone_volume: str = Field(min_length=1)
    source_database_name: str = Field(min_length=1)
    destination_database_name: str = Field(min_length=1)
    database_user: str = Field(min_length=1)
    source_db_project_label: str = Field(min_length=1)
    source_db_role_label: str = Field(min_length=1)
    source_data_project_label: str = Field(min_length=1)
    source_data_role_label: str = Field(min_length=1)
    source_pg_version: str = Field(min_length=1)
    source_pg_control_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    source_pg_control_size: int = Field(gt=0)
    source_pg_system_identifier: str = Field(min_length=1)
    source_pg_cluster_state: str = Field(min_length=1)
    source_pg_checkpoint_location: str = Field(min_length=1)
    source_pg_checkpoint_redo_location: str = Field(min_length=1)
    source_pg_checkpoint_timeline_id: str = Field(min_length=1)
    source_pg_checkpoint_time: str = Field(min_length=1)
    source_db_volume_used_bytes: int = Field(gt=0)
    source_filestore_file_count: int = Field(ge=0)
    source_filestore_size_bytes: int = Field(ge=0)
    active_data_free_bytes: int = Field(ge=0)
    staging_clone_volume_absent: bool
    backup_destination_absent: bool
    postgres_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    script_runner_image_id: str = Field(pattern=r"^sha256:[0-9a-f]{64}$")
    inspection_deployment_id: str = Field(min_length=1)


class OdooProdRetainedVolumeBackupImportInspectionFailureEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    inspection_nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    backup_record_id: str = Field(min_length=1)
    failure_stage: OdooProdRetainedVolumeBackupImportInspectionFailureStage
    failure_code: OdooProdRetainedVolumeBackupImportInspectionFailureCode
    inspection_schedule_id: str = Field(
        default="",
        max_length=200,
        pattern=r"^$|^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
    )
    inspection_deployment_id: str = Field(
        default="",
        max_length=200,
        pattern=r"^$|^[A-Za-z0-9][A-Za-z0-9._:-]{0,199}$",
    )

    @model_validator(mode="after")
    def _validate_failure_stage_code_pair(
        self,
    ) -> "OdooProdRetainedVolumeBackupImportInspectionFailureEvidence":
        if (
            ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_FAILURE_STAGE_BY_CODE.get(self.failure_code)
            != self.failure_stage
        ):
            raise ValueError(
                "Odoo retained-volume backup import inspection failure stage/code mismatch."
            )
        if self.failure_stage != "provider_control" and not self.inspection_deployment_id:
            raise ValueError(
                "Odoo retained-volume backup import inspection runtime failure requires deployment evidence."
            )
        return self


class OdooProdRetainedVolumeBackupImportPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    plan_status: Literal["ready", "blocked"]
    plan_fingerprint: str = ""
    product: str
    context: str
    instance: Literal["prod"]
    target_id: str = ""
    target_name: str = ""
    target_compose_project: str = ""
    backup_record_id: str
    expected_current_artifact_id: str
    inventory_updated_at: str = ""
    runtime_identity: RuntimeIdentity | None = None
    active_db_volume: str
    active_data_volume: str
    active_log_volume: str
    source_db_volume: str
    source_data_volume: str
    source_database_name: str
    destination_database_name: str
    database_user: str
    staging_clone_volume: str
    expected_source_compose_project: str
    source_db_project_label: str = ""
    source_db_role_label: str = ""
    source_data_project_label: str = ""
    source_data_role_label: str = ""
    source_pg_version: str = ""
    source_pg_control_sha256: str = ""
    source_pg_control_size: int = Field(default=0, ge=0)
    source_pg_system_identifier: str = ""
    source_pg_cluster_state: str = ""
    source_pg_checkpoint_location: str = ""
    source_pg_checkpoint_redo_location: str = ""
    source_pg_checkpoint_timeline_id: str = ""
    source_pg_checkpoint_time: str = ""
    source_db_volume_used_bytes: int = Field(default=0, ge=0)
    source_filestore_file_count: int = Field(default=0, ge=0)
    source_filestore_size_bytes: int = Field(default=0, ge=0)
    active_data_free_bytes: int = Field(default=0, ge=0)
    active_data_required_bytes: int = Field(default=0, ge=0)
    staging_clone_volume_absent: bool = False
    backup_destination_absent: bool = False
    postgres_image_id: str = ""
    script_runner_image_id: str = ""
    filestore_path: str = ""
    backup_root: str = ""
    backup_dir: str = ""
    database_dump_path: str = ""
    filestore_archive_path: str = ""
    manifest_path: str = ""
    inspection_nonce: str = ""
    inspection_deployment_id: str = ""
    blockers: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    steps: tuple[OdooProdRetainedVolumeBackupImportStep, ...] = ()

    @model_validator(mode="after")
    def _validate_plan(self) -> "OdooProdRetainedVolumeBackupImportPlan":
        if self.plan_status == "blocked":
            if self.plan_fingerprint:
                raise ValueError(
                    "Blocked Odoo retained-volume backup import plans cannot expose a fingerprint."
                )
            return self
        if self.blockers:
            raise ValueError(
                "Ready Odoo retained-volume backup import plans cannot include blockers."
            )
        if not _SHA256_PATTERN.fullmatch(self.plan_fingerprint):
            raise ValueError(
                "Ready Odoo retained-volume backup import plans require a fingerprint."
            )
        required_strings = (
            self.target_id,
            self.target_name,
            self.target_compose_project,
            self.inventory_updated_at,
            self.source_db_project_label,
            self.source_db_role_label,
            self.source_data_project_label,
            self.source_data_role_label,
            self.source_pg_version,
            self.source_pg_control_sha256,
            self.source_pg_system_identifier,
            self.source_pg_cluster_state,
            self.source_pg_checkpoint_location,
            self.source_pg_checkpoint_redo_location,
            self.source_pg_checkpoint_timeline_id,
            self.source_pg_checkpoint_time,
            self.postgres_image_id,
            self.script_runner_image_id,
            self.filestore_path,
            self.backup_root,
            self.backup_dir,
            self.database_dump_path,
            self.filestore_archive_path,
            self.manifest_path,
            self.inspection_nonce,
            self.inspection_deployment_id,
        )
        if self.runtime_identity is None or any(not value.strip() for value in required_strings):
            raise ValueError(
                "Ready Odoo retained-volume backup import plans require complete authority evidence."
            )
        if self.source_pg_version != "17":
            raise ValueError(
                "Ready Odoo retained-volume backup import plans require PostgreSQL 17 source evidence."
            )
        if self.source_pg_cluster_state != "shut down":
            raise ValueError(
                "Ready Odoo retained-volume backup import plans require a cleanly shut down source cluster."
            )
        if not _SHA256_PATTERN.fullmatch(self.source_pg_control_sha256):
            raise ValueError(
                "Ready Odoo retained-volume backup import plans require a pg_control SHA-256 fingerprint."
            )
        if not _SHA256_PATTERN.fullmatch(self.inspection_nonce):
            raise ValueError(
                "Ready Odoo retained-volume backup import plans require a request-bound inspection nonce."
            )
        if not _DOCKER_IMAGE_ID_PATTERN.fullmatch(
            self.postgres_image_id
        ) or not _DOCKER_IMAGE_ID_PATTERN.fullmatch(self.script_runner_image_id):
            raise ValueError(
                "Ready Odoo retained-volume backup import plans require immutable provider image ids."
            )
        if (
            self.source_db_project_label != self.expected_source_compose_project
            or self.source_data_project_label != self.expected_source_compose_project
            or self.source_db_role_label != "odoo_db"
            or self.source_data_role_label != "odoo_data"
        ):
            raise ValueError(
                "Ready Odoo retained-volume backup import plans require exact retained-volume compose labels."
            )
        if not self.staging_clone_volume_absent or not self.backup_destination_absent:
            raise ValueError(
                "Ready Odoo retained-volume backup import plans require absent destinations."
            )
        if (
            self.source_pg_control_size < 1
            or self.source_db_volume_used_bytes < 1
            or self.active_data_required_bytes < 1
            or self.active_data_free_bytes < self.active_data_required_bytes
        ):
            raise ValueError(
                "Ready Odoo retained-volume backup import plans require bounded source and free-space evidence."
            )
        return self


class OdooProdRetainedVolumeBackupImportCompletionEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    schema_version: int = Field(default=1, ge=1)
    import_nonce: str = Field(pattern=r"^[0-9a-f]{64}$")
    operation_id: str = Field(min_length=1)
    plan_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    backup_record_id: str = Field(min_length=1)
    source_db_volume: str = Field(min_length=1)
    source_data_volume: str = Field(min_length=1)
    staging_clone_volume: str = Field(min_length=1)
    source_database_name: str = Field(min_length=1)
    destination_database_name: str = Field(min_length=1)
    database_user: str = Field(min_length=1)
    source_pg_control_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    clone_pg_control_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_dump_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    filestore_archive_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    database_dump_size: int = Field(gt=0)
    filestore_archive_size: int = Field(gt=0)
    source_filestore_file_count: int = Field(ge=0)
    source_filestore_size_bytes: int = Field(ge=0)

    @model_validator(mode="after")
    def _validate_completion(self) -> "OdooProdRetainedVolumeBackupImportCompletionEvidence":
        if self.clone_pg_control_sha256 != self.source_pg_control_sha256:
            raise ValueError(
                "Odoo retained-volume backup import clone pg_control fingerprint must match the source."
            )
        return self


class OdooProdRetainedVolumeBackupImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: Literal["prod"]
    backup_record_id: str
    plan_fingerprint: str
    import_status: Literal["pass", "fail"]
    backup_status: Literal["pass", "fail", "skipped"] = "skipped"
    source_db_volume: str
    source_data_volume: str
    staging_clone_volume: str
    source_database_name: str
    destination_database_name: str
    database_user: str
    schedule_deployment_id: str = ""
    database_dump_sha256: str = ""
    filestore_archive_sha256: str = ""
    phase_evidence: dict[str, dict[str, str]] = Field(default_factory=dict)
    error_message: str = ""


def build_odoo_prod_retained_volume_backup_import_plan_fingerprint(
    plan: OdooProdRetainedVolumeBackupImportPlan,
) -> str:
    authority = plan.model_dump(
        mode="json",
        exclude={
            "plan_status",
            "plan_fingerprint",
            "inspection_nonce",
            "inspection_deployment_id",
            "blockers",
            "warnings",
            "steps",
        },
    )
    encoded = json.dumps(authority, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _normalize_required(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized


def _normalize_path_component(value: str, message: str) -> str:
    normalized = _normalize_required(value, message)
    if not _PATH_COMPONENT_PATTERN.fullmatch(normalized):
        raise ValueError(message)
    return normalized
