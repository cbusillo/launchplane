from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
    DurableOperationCancellation,
)
from control_plane.contracts.verireel_prod_backup_gate import (
    VeriReelProdBackupGateRequest,
    VeriReelProdBackupGateResult,
)

VeriReelProdBackupGateOperationStatus = Literal["pending", "running", "pass", "fail", "cancelled"]
VeriReelProdBackupGateOperationPhase = Literal[
    "created",
    "running",
    "backup_gate",
    "completed",
    "failed",
    "cancelled",
]

_TERMINAL_OPERATION_STATUSES: tuple[VeriReelProdBackupGateOperationStatus, ...] = (
    "pass",
    "fail",
    "cancelled",
)
VERIREEL_PROD_BACKUP_GATE_TERMINAL_OPERATION_STATUSES: frozenset[
    VeriReelProdBackupGateOperationStatus
] = frozenset(_TERMINAL_OPERATION_STATUSES)


class VeriReelProdBackupGateOperationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    operation_id: str
    product: str = "verireel"
    context: str
    instance: str
    backup_record_id: str
    request_fingerprint: str
    request: VeriReelProdBackupGateRequest
    authorization: DurableOperationAuthorization | None = None
    status: VeriReelProdBackupGateOperationStatus = "pending"
    phase: VeriReelProdBackupGateOperationPhase = "created"
    created_at: str
    updated_at: str
    started_at: str = ""
    finished_at: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    heartbeat_at: str = ""
    attempt: int = Field(default=0, ge=0)
    result: VeriReelProdBackupGateResult | None = None
    cancellation: DurableOperationCancellation | None = None
    error_code: str = ""
    error_message: str = ""
    runner_trace_id: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "VeriReelProdBackupGateOperationRecord":
        if self.schema_version not in {1, 2}:
            raise ValueError("Unsupported VeriReel prod backup gate operation schema version.")
        self.operation_id = _normalize_required(
            self.operation_id, "VeriReel prod backup gate operation requires operation_id."
        )
        self.product = _normalize_required(
            self.product, "VeriReel prod backup gate operation requires product."
        ).lower()
        self.context = _normalize_required(
            self.context, "VeriReel prod backup gate operation requires context."
        ).lower()
        self.instance = _normalize_required(
            self.instance, "VeriReel prod backup gate operation requires instance."
        ).lower()
        self.backup_record_id = _normalize_required(
            self.backup_record_id,
            "VeriReel prod backup gate operation requires backup_record_id.",
        )
        self.request_fingerprint = _normalize_required(
            self.request_fingerprint,
            "VeriReel prod backup gate operation requires request_fingerprint.",
        )
        self.created_at = _normalize_required(
            self.created_at, "VeriReel prod backup gate operation requires created_at."
        )
        self.updated_at = _normalize_required(
            self.updated_at, "VeriReel prod backup gate operation requires updated_at."
        )
        self.started_at = self.started_at.strip()
        self.finished_at = self.finished_at.strip()
        self.lease_owner = self.lease_owner.strip()
        self.lease_expires_at = self.lease_expires_at.strip()
        self.heartbeat_at = self.heartbeat_at.strip()
        self.error_code = self.error_code.strip()
        self.error_message = self.error_message.strip()
        self.runner_trace_id = self.runner_trace_id.strip()
        if self.product != "verireel":
            raise ValueError("VeriReel prod backup gate operation product must be verireel.")
        if self.context != self.request.context:
            raise ValueError("VeriReel prod backup gate operation context must match request.")
        if self.instance != self.request.instance:
            raise ValueError("VeriReel prod backup gate operation instance must match request.")
        if self.backup_record_id != self.request.backup_record_id:
            raise ValueError(
                "VeriReel prod backup gate operation backup_record_id must match request."
            )
        if self.schema_version == 2 and self.authorization is None:
            raise ValueError(
                "Schema-v2 VeriReel prod backup gate operation requires authorization provenance."
            )
        if self.authorization is not None:
            if self.schema_version != 2:
                raise ValueError(
                    "VeriReel backup gate authorization provenance requires schema version 2."
                )
            if self.authorization.action != "verireel_prod_backup_gate.execute":
                raise ValueError(
                    "VeriReel backup gate authorization action must match the operation."
                )
            if (
                self.authorization.product != self.product
                or self.authorization.context != self.context
                or self.authorization.instances != (self.instance,)
            ):
                raise ValueError(
                    "VeriReel backup gate authorization target must match the operation."
                )
        if self.status in VERIREEL_PROD_BACKUP_GATE_TERMINAL_OPERATION_STATUSES:
            if not self.finished_at:
                raise ValueError(
                    "Terminal VeriReel prod backup gate operations require finished_at."
                )
            if self.status == "pass" and (self.error_code or self.error_message):
                raise ValueError(
                    "Passing VeriReel prod backup gate operations must not include an error."
                )
            if self.status == "fail" and not self.error_message:
                raise ValueError(
                    "Failed VeriReel prod backup gate operations require error_message."
                )
            if self.status == "cancelled":
                if self.cancellation is None:
                    raise ValueError(
                        "Cancelled VeriReel backup gate operation requires cancellation evidence."
                    )
                if self.result is not None or self.error_code or self.error_message:
                    raise ValueError(
                        "Cancelled VeriReel backup gate operation cannot include result or error."
                    )
        elif self.cancellation is not None:
            raise ValueError(
                "Only cancelled VeriReel backup gate operations can include cancellation evidence."
            )
        return self


def build_verireel_prod_backup_gate_operation_id(
    *, product: str, context: str, instance: str, backup_record_id: str
) -> str:
    normalized_product = product.strip().lower().replace("/", "-")
    normalized_context = context.strip().lower()
    normalized_instance = instance.strip().lower()
    normalized_backup_record_id = backup_record_id.strip()
    digest = hashlib.sha256(
        "|".join(
            (
                normalized_product,
                normalized_context,
                normalized_instance,
                normalized_backup_record_id,
            )
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"verireel-prod-backup-gate-{normalized_context}-{normalized_instance}-{digest}"


def build_cancelled_verireel_prod_backup_gate_record(
    operation: VeriReelProdBackupGateOperationRecord,
) -> BackupGateRecord:
    if operation.status != "cancelled" or operation.cancellation is None:
        raise ValueError("Cancelled VeriReel backup evidence requires a cancelled operation.")
    return BackupGateRecord(
        record_id=operation.backup_record_id,
        context=operation.context,
        instance=operation.instance,
        created_at=operation.cancellation.cancelled_at,
        source="launchplane-verireel-prod-backup-gate-cancellation",
        required=True,
        status="fail",
        evidence={
            "operation_id": operation.operation_id,
            "operation_status": "cancelled",
            "cancelled_at": operation.cancellation.cancelled_at,
            "cancellation_reason": operation.cancellation.reason,
        },
    )


def verireel_prod_backup_gate_operation_is_terminal(
    record: VeriReelProdBackupGateOperationRecord,
) -> bool:
    return record.status in VERIREEL_PROD_BACKUP_GATE_TERMINAL_OPERATION_STATUSES


def _normalize_required(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized
