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
