from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.odoo_stable_bootstrap import (
    OdooStableBootstrapRequest,
    OdooStableBootstrapResult,
)

OdooStableBootstrapOperationStatus = Literal["pending", "running", "pass", "fail"]
OdooStableBootstrapOperationPhase = Literal[
    "created",
    "running",
    "bootstrap",
    "post_deploy",
    "verification",
    "completed",
    "failed",
]

_TERMINAL_OPERATION_STATUSES: tuple[OdooStableBootstrapOperationStatus, ...] = (
    "pass",
    "fail",
)
ODOO_STABLE_BOOTSTRAP_TERMINAL_OPERATION_STATUSES: frozenset[
    OdooStableBootstrapOperationStatus
] = frozenset(_TERMINAL_OPERATION_STATUSES)


class OdooStableBootstrapOperationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    operation_id: str
    product: str
    context: str
    instance: str
    idempotency_key: str
    request_fingerprint: str
    request: OdooStableBootstrapRequest
    status: OdooStableBootstrapOperationStatus = "pending"
    phase: OdooStableBootstrapOperationPhase = "created"
    deployment_record_id: str = ""
    created_at: str
    updated_at: str
    started_at: str = ""
    finished_at: str = ""
    result: OdooStableBootstrapResult | None = None
    error_message: str = ""
    runner_trace_id: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "OdooStableBootstrapOperationRecord":
        self.operation_id = _normalize_required(
            self.operation_id, "Odoo stable bootstrap operation requires operation_id."
        )
        self.product = _normalize_required(
            self.product, "Odoo stable bootstrap operation requires product."
        )
        self.context = _normalize_required(
            self.context, "Odoo stable bootstrap operation requires context."
        ).lower()
        self.instance = _normalize_required(
            self.instance, "Odoo stable bootstrap operation requires instance."
        ).lower()
        self.idempotency_key = _normalize_required(
            self.idempotency_key,
            "Odoo stable bootstrap operation requires idempotency_key.",
        )
        self.request_fingerprint = _normalize_required(
            self.request_fingerprint,
            "Odoo stable bootstrap operation requires request_fingerprint.",
        )
        self.created_at = _normalize_required(
            self.created_at, "Odoo stable bootstrap operation requires created_at."
        )
        self.updated_at = _normalize_required(
            self.updated_at, "Odoo stable bootstrap operation requires updated_at."
        )
        self.started_at = self.started_at.strip()
        self.finished_at = self.finished_at.strip()
        self.deployment_record_id = self.deployment_record_id.strip()
        self.error_message = self.error_message.strip()
        self.runner_trace_id = self.runner_trace_id.strip()
        if self.product != self.request.product:
            raise ValueError("Odoo stable bootstrap operation product must match request.")
        if self.context != self.request.context:
            raise ValueError("Odoo stable bootstrap operation context must match request.")
        if self.instance != self.request.instance:
            raise ValueError("Odoo stable bootstrap operation instance must match request.")
        if self.status in ODOO_STABLE_BOOTSTRAP_TERMINAL_OPERATION_STATUSES:
            if not self.finished_at:
                raise ValueError("Terminal Odoo stable bootstrap operations require finished_at.")
            if self.status == "pass" and self.error_message:
                raise ValueError("Passing Odoo stable bootstrap operations must not include error_message.")
            if self.status == "fail" and not self.error_message:
                raise ValueError("Failed Odoo stable bootstrap operations require error_message.")
        return self


def build_odoo_stable_bootstrap_operation_id(
    *, product: str, context: str, instance: str, created_at: str
) -> str:
    normalized_product = product.strip().lower().replace("/", "-")
    normalized_context = context.strip().lower()
    normalized_instance = instance.strip().lower()
    normalized_created_at = created_at.strip().replace(":", "").replace("+", "z")
    digest = hashlib.sha256(
        "|".join(
            (
                normalized_product,
                normalized_context,
                normalized_instance,
                normalized_created_at,
            )
        ).encode("utf-8")
    ).hexdigest()[:16]
    return (
        "odoo-stable-bootstrap-"
        f"{normalized_context}-{normalized_instance}-{normalized_created_at}-{digest}"
    )


def odoo_stable_bootstrap_operation_is_terminal(
    record: OdooStableBootstrapOperationRecord,
) -> bool:
    return record.status in ODOO_STABLE_BOOTSTRAP_TERMINAL_OPERATION_STATUSES


def _normalize_required(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized
