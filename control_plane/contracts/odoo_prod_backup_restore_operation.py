from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
    DurableOperationCancellation,
)
from control_plane.contracts.odoo_prod_backup_restore import (
    OdooProdBackupRestoreApplyRequest,
    OdooProdBackupRestorePlan,
    OdooProdBackupRestoreResult,
)


ODOO_PROD_BACKUP_RESTORE_REPLAY_REQUIRED_RESULT_STATUSES = {
    "database_restore_status": "pass",
    "filestore_stage_status": "pass",
    "web_quiesce_status": "pass",
    "filestore_activation_status": "pass",
    "deployment_status": "pass",
    "post_deploy_status": "pass",
}
ODOO_PROD_BACKUP_RESTORE_REPLAY_READINESS_FAILURE_STATUSES = (
    "health_status",
    "canonical_status",
    "logo_status",
)
ODOO_PROD_BACKUP_RESTORE_REPLAY_RUNTIME_IDENTITY_FAILURE_STATUSES = frozenset(
    {"mismatch", "missing", "malformed", "unverifiable"}
)
ODOO_PROD_BACKUP_RESTORE_REPLAY_REQUIRED_CHECKPOINT_PHASES = (
    "validated",
    "database_restored",
    "filestore_staged",
    "web_quiesced",
    "filestore_activated",
    "target_env_updated",
    "deployed",
    "post_deploy_completed",
    "verification_started",
)
ODOO_PROD_BACKUP_RESTORE_REPLAY_REQUIRED_PHASE_EVIDENCE = frozenset(
    {
        "database_restore",
        "filestore_stage",
        "web_quiesce",
        "filestore_activate",
        "target_env_update",
        "deployment",
        "post_deploy",
    }
)


OdooProdBackupRestoreOperationStatus = Literal[
    "pending",
    "running",
    "reconciliation_required",
    "pass",
    "fail",
    "cancelled",
]
OdooProdBackupRestoreOperationPhase = Literal[
    "created",
    "running",
    "validated",
    "database_restore_started",
    "database_restored",
    "filestore_stage_started",
    "filestore_staged",
    "web_quiesce_started",
    "web_quiesced",
    "filestore_activate_started",
    "filestore_activated",
    "target_env_update_started",
    "target_env_updated",
    "deployment_started",
    "deployed",
    "post_deploy_started",
    "post_deploy_completed",
    "verification_started",
    "completed",
    "failed",
    "cancelled",
]

ODOO_PROD_BACKUP_RESTORE_OPERATION_PHASE_SEQUENCE: tuple[
    OdooProdBackupRestoreOperationPhase, ...
] = (
    "created",
    "running",
    "validated",
    "database_restore_started",
    "database_restored",
    "filestore_stage_started",
    "filestore_staged",
    "web_quiesce_started",
    "web_quiesced",
    "filestore_activate_started",
    "filestore_activated",
    "target_env_update_started",
    "target_env_updated",
    "deployment_started",
    "deployed",
    "post_deploy_started",
    "post_deploy_completed",
    "verification_started",
    "completed",
    "failed",
    "cancelled",
)

ODOO_PROD_BACKUP_RESTORE_TERMINAL_OPERATION_STATUSES = frozenset({"pass", "fail", "cancelled"})


class OdooProdBackupRestoreCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: OdooProdBackupRestoreOperationPhase
    recorded_at: str
    evidence: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_checkpoint(self) -> "OdooProdBackupRestoreCheckpoint":
        self.recorded_at = self.recorded_at.strip()
        if not self.recorded_at:
            raise ValueError("Odoo backup restore checkpoint requires recorded_at.")
        return self


class OdooProdBackupRestoreOperationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    operation_id: str
    product: str
    context: str
    instance: Literal["prod"]
    idempotency_key: str
    idempotency_scope: str
    request_fingerprint: str
    request: OdooProdBackupRestoreApplyRequest
    plan: OdooProdBackupRestorePlan
    authorization: DurableOperationAuthorization
    status: OdooProdBackupRestoreOperationStatus = "pending"
    phase: OdooProdBackupRestoreOperationPhase = "created"
    checkpoints: tuple[OdooProdBackupRestoreCheckpoint, ...] = ()
    deployment_record_id: str = ""
    created_at: str
    updated_at: str
    started_at: str = ""
    finished_at: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    heartbeat_at: str = ""
    attempt: int = Field(default=0, ge=0)
    result: OdooProdBackupRestoreResult | None = None
    cancellation: DurableOperationCancellation | None = None
    error_code: str = ""
    error_message: str = ""
    runner_trace_id: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "OdooProdBackupRestoreOperationRecord":
        required_values = {
            "operation_id": self.operation_id,
            "product": self.product,
            "context": self.context,
            "idempotency_key": self.idempotency_key,
            "idempotency_scope": self.idempotency_scope,
            "request_fingerprint": self.request_fingerprint,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }
        for field_name, raw_value in required_values.items():
            normalized_value = raw_value.strip()
            if not normalized_value:
                raise ValueError(f"Odoo backup restore operation requires {field_name}.")
            setattr(self, field_name, normalized_value)
        self.context = self.context.lower()
        self.started_at = self.started_at.strip()
        self.finished_at = self.finished_at.strip()
        self.lease_owner = self.lease_owner.strip()
        self.lease_expires_at = self.lease_expires_at.strip()
        self.heartbeat_at = self.heartbeat_at.strip()
        self.deployment_record_id = self.deployment_record_id.strip()
        self.error_code = self.error_code.strip()
        self.error_message = self.error_message.strip()
        self.runner_trace_id = self.runner_trace_id.strip()
        if (
            self.product != self.request.product
            or self.context != self.request.context
            or self.instance != self.request.instance
        ):
            raise ValueError("Odoo backup restore operation target must match request.")
        if (
            self.product != self.plan.product
            or self.context != self.plan.context
            or self.instance != self.plan.instance
        ):
            raise ValueError("Odoo backup restore operation target must match plan.")
        if self.plan.plan_status != "ready":
            raise ValueError("Odoo backup restore operation requires a ready plan.")
        if self.plan.plan_fingerprint != self.request.plan_fingerprint:
            raise ValueError("Odoo backup restore operation plan fingerprint must match request.")
        if self.authorization.action != "odoo_prod_backup_restore_apply.execute":
            raise ValueError("Odoo backup restore authorization action must match operation.")
        if (
            self.authorization.product != self.product
            or self.authorization.context != self.context
            or self.authorization.instances != (self.instance,)
        ):
            raise ValueError("Odoo backup restore authorization target must match operation.")
        if (
            self.checkpoints
            and self.phase not in {"completed", "failed", "cancelled"}
            and self.checkpoints[-1].phase != self.phase
        ):
            raise ValueError(
                "Odoo backup restore operation phase must match its latest checkpoint."
            )
        if self.status in ODOO_PROD_BACKUP_RESTORE_TERMINAL_OPERATION_STATUSES:
            if not self.finished_at:
                raise ValueError("Terminal Odoo backup restore operations require finished_at.")
            if self.status == "pass" and (self.error_code or self.error_message):
                raise ValueError("Passing Odoo backup restore operations cannot include errors.")
            if self.status == "fail" and not self.error_message:
                raise ValueError("Failed Odoo backup restore operations require error_message.")
            if self.status == "cancelled":
                if self.cancellation is None:
                    raise ValueError("Cancelled Odoo backup restore operations require evidence.")
                if self.result is not None or self.error_code or self.error_message:
                    raise ValueError(
                        "Cancelled Odoo backup restore operations cannot include result or error."
                    )
        elif self.status == "reconciliation_required":
            if self.finished_at or self.lease_owner or self.lease_expires_at or self.heartbeat_at:
                raise ValueError(
                    "Reconciliation-required Odoo backup restore operations cannot retain "
                    "terminal or lease state."
                )
            if self.result is not None or not self.error_code or not self.error_message:
                raise ValueError(
                    "Reconciliation-required Odoo backup restore operations require an error and "
                    "cannot include a result."
                )
        elif self.cancellation is not None:
            raise ValueError(
                "Only cancelled Odoo backup restore operations can include cancellation."
            )
        return self


def odoo_prod_backup_restore_failure_is_replay_eligible(
    operation: OdooProdBackupRestoreOperationRecord,
) -> bool:
    """Return whether a failed restore can replay final verification only."""
    result = operation.result
    if (
        operation.status != "fail"
        or operation.phase != "failed"
        or operation.attempt != 1
        or result is None
        or result.restore_status != "fail"
        or not result.deployment_record_id.strip()
        or operation.deployment_record_id != result.deployment_record_id
        or result.plan_fingerprint != operation.plan.plan_fingerprint
        or result.backup_record_id != operation.plan.backup_record_id
        or result.verification_record_id != operation.plan.verification_record_id
        or result.old_db_volume != operation.plan.old_db_volume
        or result.new_db_volume != operation.plan.new_db_volume
        or result.data_volume != operation.plan.data_volume
        or result.log_volume != operation.plan.log_volume
        or result.database_dump_sha256 != operation.plan.database_dump_sha256
        or result.filestore_archive_sha256 != operation.plan.filestore_archive_sha256
        or not _odoo_prod_backup_restore_replay_evidence_is_complete(operation)
    ):
        return False
    for (
        status_field,
        expected_status,
    ) in ODOO_PROD_BACKUP_RESTORE_REPLAY_REQUIRED_RESULT_STATUSES.items():
        if getattr(result, status_field) != expected_status:
            return False
    if any(
        getattr(result, status_field) == "fail"
        for status_field in ODOO_PROD_BACKUP_RESTORE_REPLAY_READINESS_FAILURE_STATUSES
    ):
        return True
    if (
        result.runtime_identity_status
        in ODOO_PROD_BACKUP_RESTORE_REPLAY_RUNTIME_IDENTITY_FAILURE_STATUSES
    ):
        return True
    return False


def _odoo_prod_backup_restore_replay_evidence_is_complete(
    operation: OdooProdBackupRestoreOperationRecord,
) -> bool:
    result = operation.result
    if result is None or not operation.checkpoints:
        return False
    checkpoint_phases = tuple(checkpoint.phase for checkpoint in operation.checkpoints)
    if checkpoint_phases[-1] != "verification_started":
        return False
    required_index = 0
    for checkpoint_phase in checkpoint_phases:
        if (
            required_index < len(ODOO_PROD_BACKUP_RESTORE_REPLAY_REQUIRED_CHECKPOINT_PHASES)
            and checkpoint_phase
            == ODOO_PROD_BACKUP_RESTORE_REPLAY_REQUIRED_CHECKPOINT_PHASES[required_index]
        ):
            required_index += 1
    if required_index != len(ODOO_PROD_BACKUP_RESTORE_REPLAY_REQUIRED_CHECKPOINT_PHASES):
        return False
    if not ODOO_PROD_BACKUP_RESTORE_REPLAY_REQUIRED_PHASE_EVIDENCE.issubset(result.phase_evidence):
        return False
    if any(
        not result.phase_evidence[evidence_name]
        for evidence_name in ODOO_PROD_BACKUP_RESTORE_REPLAY_REQUIRED_PHASE_EVIDENCE
    ):
        return False
    target_env_evidence = result.phase_evidence["target_env_update"]
    if target_env_evidence != {
        "old_db_volume": operation.plan.old_db_volume,
        "new_db_volume": operation.plan.new_db_volume,
        "data_volume": operation.plan.data_volume,
        "log_volume": operation.plan.log_volume,
    }:
        return False
    if result.phase_evidence["post_deploy"].get("status") != "pass":
        return False
    return bool(result.phase_evidence["deployment"].get("provider_deployment", "").strip())


def odoo_prod_backup_restore_operation_is_verification_replay_claim(
    operation: OdooProdBackupRestoreOperationRecord,
) -> bool:
    if operation.status != "running" or operation.attempt != 2:
        return False
    terminal_shape = operation.model_copy(
        update={"status": "fail", "phase": "failed", "attempt": 1}
    )
    return odoo_prod_backup_restore_failure_is_replay_eligible(terminal_shape)


def requeue_odoo_prod_backup_restore_verification_replay(
    *,
    operation: OdooProdBackupRestoreOperationRecord,
    queued_at: str,
    authorization: DurableOperationAuthorization,
) -> OdooProdBackupRestoreOperationRecord:
    normalized_queued_at = queued_at.strip()
    if not normalized_queued_at:
        raise ValueError("Odoo backup restore verification replay requires queued_at.")
    if not odoo_prod_backup_restore_failure_is_replay_eligible(operation):
        raise ValueError("Odoo backup restore failure is not eligible for verification replay.")
    return operation.model_copy(
        update={
            "status": "pending",
            "phase": "verification_started",
            "updated_at": normalized_queued_at,
            "finished_at": "",
            "lease_owner": "",
            "lease_expires_at": "",
            "heartbeat_at": "",
            "authorization": authorization,
            "error_code": "",
            "error_message": "",
        }
    )


def build_odoo_prod_backup_restore_operation_id(
    *,
    product: str,
    context: str,
    created_at: str,
    idempotency_key: str,
    idempotency_scope: str,
) -> str:
    digest_input = json.dumps(
        [
            product.strip().lower(),
            context.strip().lower(),
            "prod",
            created_at.strip(),
            idempotency_key.strip(),
            idempotency_scope.strip(),
        ],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]
    normalized_created_at = created_at.strip().replace(":", "").replace("+", "z")
    return f"odoo-prod-backup-restore-{normalized_created_at}-{digest}"
