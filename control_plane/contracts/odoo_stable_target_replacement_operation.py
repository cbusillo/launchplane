from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
    DurableOperationCancellation,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
    OdooStableTargetReplacementApplyResult,
)

OdooStableTargetReplacementOperationStatus = Literal[
    "pending", "running", "pass", "fail", "cancelled"
]
OdooStableTargetReplacementOperationPhase = Literal[
    "created",
    "running",
    "plan",
    "apply",
    "post_deploy",
    "verification",
    "completed",
    "failed",
    "cancelled",
]

_TERMINAL_OPERATION_STATUSES: tuple[OdooStableTargetReplacementOperationStatus, ...] = (
    "pass",
    "fail",
    "cancelled",
)
ODOO_STABLE_TARGET_REPLACEMENT_TERMINAL_OPERATION_STATUSES: frozenset[
    OdooStableTargetReplacementOperationStatus
] = frozenset(_TERMINAL_OPERATION_STATUSES)


class OdooStableTargetReplacementOperationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    operation_id: str
    product: str
    context: str
    instance: str
    idempotency_key: str
    idempotency_scope: str = ""
    request_fingerprint: str
    request: OdooStableTargetReplacementApplyRequest
    authorization: DurableOperationAuthorization | None = None
    status: OdooStableTargetReplacementOperationStatus = "pending"
    phase: OdooStableTargetReplacementOperationPhase = "created"
    deployment_record_id: str = ""
    created_at: str
    updated_at: str
    started_at: str = ""
    finished_at: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    heartbeat_at: str = ""
    attempt: int = Field(default=0, ge=0)
    result: OdooStableTargetReplacementApplyResult | None = None
    cancellation: DurableOperationCancellation | None = None
    error_code: str = ""
    error_message: str = ""
    runner_trace_id: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "OdooStableTargetReplacementOperationRecord":
        if self.schema_version not in {1, 2}:
            raise ValueError("Unsupported Odoo stable target replacement operation schema version.")
        self.operation_id = _normalize_required(
            self.operation_id, "Odoo stable target replacement operation requires operation_id."
        )
        self.product = _normalize_required(
            self.product, "Odoo stable target replacement operation requires product."
        )
        self.context = _normalize_required(
            self.context, "Odoo stable target replacement operation requires context."
        ).lower()
        self.instance = _normalize_required(
            self.instance, "Odoo stable target replacement operation requires instance."
        ).lower()
        self.idempotency_key = _normalize_required(
            self.idempotency_key,
            "Odoo stable target replacement operation requires idempotency_key.",
        )
        self.idempotency_scope = self.idempotency_scope.strip()
        self.request_fingerprint = _normalize_required(
            self.request_fingerprint,
            "Odoo stable target replacement operation requires request_fingerprint.",
        )
        self.created_at = _normalize_required(
            self.created_at, "Odoo stable target replacement operation requires created_at."
        )
        self.updated_at = _normalize_required(
            self.updated_at, "Odoo stable target replacement operation requires updated_at."
        )
        self.started_at = self.started_at.strip()
        self.finished_at = self.finished_at.strip()
        self.lease_owner = self.lease_owner.strip()
        self.lease_expires_at = self.lease_expires_at.strip()
        self.heartbeat_at = self.heartbeat_at.strip()
        self.deployment_record_id = self.deployment_record_id.strip()
        self.error_code = self.error_code.strip()
        self.error_message = self.error_message.strip()
        self.runner_trace_id = self.runner_trace_id.strip()
        if self.product != self.request.product:
            raise ValueError("Odoo stable target replacement operation product must match request.")
        if self.instance != self.request.instance:
            raise ValueError(
                "Odoo stable target replacement operation instance must match request."
            )
        if self.schema_version == 2 and self.authorization is None:
            raise ValueError(
                "Schema-v2 Odoo stable target replacement operation requires authorization provenance."
            )
        if self.authorization is not None:
            if self.schema_version != 2:
                raise ValueError(
                    "Odoo target replacement authorization provenance requires schema version 2."
                )
            if self.authorization.action != "odoo_target_replacement_apply.execute":
                raise ValueError(
                    "Odoo target replacement authorization action must match the operation."
                )
            if (
                self.authorization.product != self.product
                or self.authorization.context != self.context
                or self.authorization.instances != (self.instance,)
            ):
                raise ValueError(
                    "Odoo target replacement authorization target must match the operation."
                )
        if self.status in ODOO_STABLE_TARGET_REPLACEMENT_TERMINAL_OPERATION_STATUSES:
            if not self.finished_at:
                raise ValueError(
                    "Terminal Odoo stable target replacement operations require finished_at."
                )
            if self.status == "pass" and (self.error_code or self.error_message):
                raise ValueError(
                    "Passing Odoo stable target replacement operations must not include an error."
                )
            if self.status == "fail" and not self.error_message:
                raise ValueError(
                    "Failed Odoo stable target replacement operations require error_message."
                )
            if self.status == "cancelled":
                if self.cancellation is None:
                    raise ValueError(
                        "Cancelled Odoo target replacement operation requires cancellation evidence."
                    )
                if self.result is not None or self.error_code or self.error_message:
                    raise ValueError(
                        "Cancelled Odoo target replacement operation cannot include result or error."
                    )
        elif self.cancellation is not None:
            raise ValueError(
                "Only cancelled Odoo target replacement operations can include cancellation evidence."
            )
        return self


def build_odoo_stable_target_replacement_operation_id(
    *,
    product: str,
    context: str,
    instance: str,
    created_at: str,
    idempotency_key: str = "",
    idempotency_scope: str = "",
) -> str:
    normalized_product = product.strip().lower().replace("/", "-")
    normalized_context = context.strip().lower()
    normalized_instance = instance.strip().lower()
    normalized_created_at = created_at.strip().replace(":", "").replace("+", "z")
    normalized_idempotency_key = idempotency_key.strip()
    normalized_idempotency_scope = idempotency_scope.strip()
    digest_input = json.dumps(
        [
            normalized_product,
            normalized_context,
            normalized_instance,
            normalized_created_at,
            normalized_idempotency_key,
            normalized_idempotency_scope,
        ],
        separators=(",", ":"),
    )
    digest = hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:16]
    return (
        "odoo-target-replacement-"
        f"{normalized_context}-{normalized_instance}-{normalized_created_at}-{digest}"
    )


def odoo_stable_target_replacement_operation_is_terminal(
    record: OdooStableTargetReplacementOperationRecord,
) -> bool:
    return record.status in ODOO_STABLE_TARGET_REPLACEMENT_TERMINAL_OPERATION_STATUSES


def _normalize_required(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized
