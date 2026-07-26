from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
    DurableOperationCancellation,
)
from control_plane.contracts.odoo_prod_retained_volume_backup_import import (
    OdooProdRetainedVolumeBackupImportApplyRequest,
    OdooProdRetainedVolumeBackupImportPlan,
    OdooProdRetainedVolumeBackupImportRequest,
    OdooProdRetainedVolumeBackupImportResult,
)


OdooProdRetainedVolumeBackupImportOperationStatus = Literal[
    "pending", "running", "pass", "fail", "cancelled"
]
OdooProdRetainedVolumeBackupImportOperationKind = Literal["plan", "apply"]
OdooProdRetainedVolumeBackupImportOperationPhase = Literal[
    "created",
    "running",
    "inspection_started",
    "inspected",
    "planned",
    "validated",
    "backup_record_pending",
    "provider_import_started",
    "provider_import_completed",
    "backup_record_written",
    "completed",
    "failed",
    "cancelled",
]

ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_OPERATION_PHASE_SEQUENCE: tuple[
    OdooProdRetainedVolumeBackupImportOperationPhase, ...
] = (
    "created",
    "running",
    "inspection_started",
    "inspected",
    "planned",
    "validated",
    "backup_record_pending",
    "provider_import_started",
    "provider_import_completed",
    "backup_record_written",
    "completed",
    "failed",
    "cancelled",
)
ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_TERMINAL_STATUSES = frozenset({"pass", "fail", "cancelled"})


class OdooProdRetainedVolumeBackupImportCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: OdooProdRetainedVolumeBackupImportOperationPhase
    recorded_at: str
    evidence: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_checkpoint(self) -> "OdooProdRetainedVolumeBackupImportCheckpoint":
        self.recorded_at = self.recorded_at.strip()
        if not self.recorded_at:
            raise ValueError("Odoo retained-volume backup import checkpoint requires recorded_at.")
        return self


class OdooProdRetainedVolumeBackupImportOperationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=2, ge=2, le=2)
    operation_id: str
    operation_kind: OdooProdRetainedVolumeBackupImportOperationKind
    product: str
    context: str
    instance: Literal["prod"]
    idempotency_key: str
    idempotency_scope: str
    request_fingerprint: str
    request: (
        OdooProdRetainedVolumeBackupImportRequest | OdooProdRetainedVolumeBackupImportApplyRequest
    )
    plan: OdooProdRetainedVolumeBackupImportPlan | None = None
    authorization: DurableOperationAuthorization
    status: OdooProdRetainedVolumeBackupImportOperationStatus = "pending"
    phase: OdooProdRetainedVolumeBackupImportOperationPhase = "created"
    checkpoints: tuple[OdooProdRetainedVolumeBackupImportCheckpoint, ...] = ()
    created_at: str
    updated_at: str
    started_at: str = ""
    finished_at: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    heartbeat_at: str = ""
    attempt: int = Field(default=0, ge=0)
    result: (
        OdooProdRetainedVolumeBackupImportPlan | OdooProdRetainedVolumeBackupImportResult | None
    ) = None
    cancellation: DurableOperationCancellation | None = None
    error_code: str = ""
    error_message: str = ""
    runner_trace_id: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "OdooProdRetainedVolumeBackupImportOperationRecord":
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
                raise ValueError(
                    f"Odoo retained-volume backup import operation requires {field_name}."
                )
            setattr(self, field_name, normalized_value)
        self.context = self.context.lower()
        self.started_at = self.started_at.strip()
        self.finished_at = self.finished_at.strip()
        self.lease_owner = self.lease_owner.strip()
        self.lease_expires_at = self.lease_expires_at.strip()
        self.heartbeat_at = self.heartbeat_at.strip()
        self.error_code = self.error_code.strip()
        self.error_message = self.error_message.strip()
        self.runner_trace_id = self.runner_trace_id.strip()
        if (
            self.product != self.request.product
            or self.context != self.request.context
            or self.instance != self.request.instance
        ):
            raise ValueError(
                "Odoo retained-volume backup import operation target must match request."
            )
        expected_authorization_action = (
            "odoo_prod_retained_volume_backup_import_plan.execute"
            if self.operation_kind == "plan"
            else "odoo_prod_retained_volume_backup_import_apply.execute"
        )
        if self.authorization.action != expected_authorization_action:
            raise ValueError(
                "Odoo retained-volume backup import authorization action must match operation."
            )
        if (
            self.authorization.product != self.product
            or self.authorization.context != self.context
            or self.authorization.instances != (self.instance,)
        ):
            raise ValueError(
                "Odoo retained-volume backup import authorization target must match operation."
            )
        if self.operation_kind == "plan":
            if isinstance(self.request, OdooProdRetainedVolumeBackupImportApplyRequest):
                raise ValueError(
                    "Odoo retained-volume backup import plan operations require a plan request."
                )
            if self.plan is not None:
                raise ValueError(
                    "Odoo retained-volume backup import plan operations cannot carry an apply plan."
                )
            if self.result is not None and not isinstance(
                self.result, OdooProdRetainedVolumeBackupImportPlan
            ):
                raise ValueError(
                    "Odoo retained-volume backup import plan operation result must be a plan."
                )
        else:
            if not isinstance(self.request, OdooProdRetainedVolumeBackupImportApplyRequest):
                raise ValueError(
                    "Odoo retained-volume backup import apply operations require an apply request."
                )
            if self.plan is None or self.plan.plan_status != "ready":
                raise ValueError(
                    "Odoo retained-volume backup import apply operation requires a ready plan."
                )
            if (
                self.product != self.plan.product
                or self.context != self.plan.context
                or self.instance != self.plan.instance
            ):
                raise ValueError(
                    "Odoo retained-volume backup import operation target must match plan."
                )
            if self.plan.plan_fingerprint != self.request.plan_fingerprint:
                raise ValueError(
                    "Odoo retained-volume backup import operation plan fingerprint must match request."
                )
            if self.result is not None and not isinstance(
                self.result, OdooProdRetainedVolumeBackupImportResult
            ):
                raise ValueError(
                    "Odoo retained-volume backup import apply operation result must be an import result."
                )
        if (
            self.checkpoints
            and self.phase not in {"completed", "failed", "cancelled"}
            and self.checkpoints[-1].phase != self.phase
        ):
            raise ValueError(
                "Odoo retained-volume backup import phase must match its latest checkpoint."
            )
        if self.status in ODOO_PROD_RETAINED_VOLUME_BACKUP_IMPORT_TERMINAL_STATUSES:
            if not self.finished_at:
                raise ValueError(
                    "Terminal Odoo retained-volume backup import operations require finished_at."
                )
            if self.status == "pass" and (self.error_code or self.error_message):
                raise ValueError(
                    "Passing Odoo retained-volume backup import operations cannot include errors."
                )
            if self.status == "fail" and not self.error_message:
                raise ValueError(
                    "Failed Odoo retained-volume backup import operations require error_message."
                )
            if self.status == "cancelled":
                if self.cancellation is None:
                    raise ValueError(
                        "Cancelled Odoo retained-volume backup import operations require evidence."
                    )
                if self.result is not None or self.error_code or self.error_message:
                    raise ValueError(
                        "Cancelled Odoo retained-volume backup import operations cannot include result or error."
                    )
        elif self.cancellation is not None:
            raise ValueError(
                "Only cancelled Odoo retained-volume backup import operations can include cancellation."
            )
        return self


def build_odoo_prod_retained_volume_backup_import_operation_id(
    *,
    operation_kind: OdooProdRetainedVolumeBackupImportOperationKind,
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
            operation_kind,
            created_at.strip(),
            idempotency_key.strip(),
            idempotency_scope.strip(),
        ],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]
    normalized_created_at = created_at.strip().replace(":", "").replace("+", "z")
    return (
        f"odoo-prod-retained-volume-backup-import-{operation_kind}-{normalized_created_at}-{digest}"
    )
