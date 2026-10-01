from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
    DurableOperationCancellation,
)


ODOO_PROD_PROMOTION_RUN_ACTION = "odoo_prod_promotion_run.execute"


class OdooProdPromotionRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    from_instance: str = "testing"
    to_instance: str = "prod"
    product: str = ""
    request_id: str
    infrastructure_backup_record_id: str = ""
    backup_timeout_seconds: int | None = Field(default=None, ge=1)
    promotion_timeout_seconds: int | None = Field(default=None, ge=1)
    health_timeout_seconds: int | None = Field(default=None, ge=1)
    wait: bool = True
    verify_health: bool = True
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooProdPromotionRunRequest":
        self.context = self.context.strip().lower()
        self.from_instance = self.from_instance.strip().lower()
        self.to_instance = self.to_instance.strip().lower()
        self.product = self.product.strip()
        self.request_id = self.request_id.strip()
        self.infrastructure_backup_record_id = self.infrastructure_backup_record_id.strip()
        if not self.context:
            raise ValueError("Odoo prod promotion run requires context.")
        if self.from_instance != "testing" or self.to_instance != "prod":
            raise ValueError("Odoo prod promotion run requires testing -> prod.")
        if not self.request_id:
            raise ValueError("Odoo prod promotion run requires request_id.")
        if self.verify_health and not self.wait:
            raise ValueError("Odoo prod promotion run health verification requires wait=true.")
        return self


class OdooProdPromotionRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    from_instance: str
    to_instance: str
    request_id: str
    run_status: Literal["pass", "fail", "blocked"]
    input_status: Literal["ready", "blocked"]
    backup_status: Literal["pass", "fail", "skipped"] = "skipped"
    promotion_status: Literal["pass", "fail", "skipped"] = "skipped"
    deployment_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    post_deploy_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    destination_health_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    artifact_id: str = ""
    source_git_ref: str = ""
    backup_record_id: str = ""
    infrastructure_backup_record_id: str = ""
    promotion_record_id: str = ""
    deployment_record_id: str = ""
    release_tuple_id: str = ""
    image_repository: str = ""
    image_digest: str = ""
    error_message: str = ""


OdooProdPromotionOperationStatus = Literal[
    "pending",
    "running",
    "reconciliation_required",
    "pass",
    "fail",
    "cancelled",
]
OdooProdPromotionOperationPhase = Literal[
    "created",
    "running",
    "validated",
    "logical_backup_started",
    "logical_backup_completed",
    "promotion_started",
    "completed",
    "failed",
    "cancelled",
]
ODOO_PROD_PROMOTION_OPERATION_PHASE_SEQUENCE: tuple[OdooProdPromotionOperationPhase, ...] = (
    "created",
    "running",
    "validated",
    "logical_backup_started",
    "logical_backup_completed",
    "promotion_started",
    "completed",
    "failed",
    "cancelled",
)
# Before the logical backup starts nothing has touched the provider, so an expired
# lease in these phases may run again. Any later phase may have deployed.
ODOO_PROD_PROMOTION_SAFE_RETRY_PHASES: tuple[OdooProdPromotionOperationPhase, ...] = (
    "created",
    "running",
    "validated",
)
ODOO_PROD_PROMOTION_TERMINAL_OPERATION_STATUSES = frozenset({"pass", "fail", "cancelled"})


class OdooProdPromotionCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: OdooProdPromotionOperationPhase
    recorded_at: str
    evidence: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_checkpoint(self) -> "OdooProdPromotionCheckpoint":
        self.recorded_at = self.recorded_at.strip()
        if not self.recorded_at:
            raise ValueError("Odoo prod promotion checkpoint requires recorded_at.")
        return self


class OdooProdPromotionOperationRecord(BaseModel):
    """A queued Odoo testing-to-prod promotion that the stable-lane worker runs."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    operation_id: str
    product: str
    context: str
    instance: Literal["prod"]
    idempotency_key: str
    idempotency_scope: str
    request_fingerprint: str
    request: OdooProdPromotionRunRequest
    authorization: DurableOperationAuthorization
    status: OdooProdPromotionOperationStatus = "pending"
    phase: OdooProdPromotionOperationPhase = "created"
    checkpoints: tuple[OdooProdPromotionCheckpoint, ...] = ()
    created_at: str
    updated_at: str
    started_at: str = ""
    finished_at: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    heartbeat_at: str = ""
    attempt: int = Field(default=0, ge=0)
    result: OdooProdPromotionRunResult | None = None
    cancellation: DurableOperationCancellation | None = None
    error_code: str = ""
    error_message: str = ""
    runner_trace_id: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "OdooProdPromotionOperationRecord":
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
                raise ValueError(f"Odoo prod promotion operation requires {field_name}.")
            setattr(self, field_name, normalized_value)
        self.context = self.context.lower()
        for field_name in (
            "started_at",
            "finished_at",
            "lease_owner",
            "lease_expires_at",
            "heartbeat_at",
            "error_code",
            "error_message",
            "runner_trace_id",
        ):
            setattr(self, field_name, str(getattr(self, field_name)).strip())
        if (
            self.product != self.request.product
            or self.context != self.request.context
            or self.instance != self.request.to_instance
        ):
            raise ValueError("Odoo prod promotion operation target must match request.")
        if not self.request.wait:
            raise ValueError("Odoo prod promotion operations wait for the deploy to finish.")
        if self.authorization.action != ODOO_PROD_PROMOTION_RUN_ACTION:
            raise ValueError("Odoo prod promotion authorization action must match operation.")
        if (
            self.authorization.product != self.product
            or self.authorization.context != self.context
            or self.authorization.instances != (self.instance,)
        ):
            raise ValueError("Odoo prod promotion authorization target must match operation.")
        if self.authorization.grant != "policy_administrator":
            raise ValueError("Only the policy administrator may queue an Odoo prod promotion.")
        validate_release_operation_state(self, label="Odoo prod promotion")
        return self


def validate_release_operation_state(record: object, *, label: str) -> None:
    """Shared lifecycle rules for queued Odoo release operations."""

    status = str(getattr(record, "status"))
    phase = str(getattr(record, "phase"))
    checkpoints = tuple(getattr(record, "checkpoints"))
    finished_at = str(getattr(record, "finished_at")).strip()
    holds_lease = any(
        str(getattr(record, name)).strip()
        for name in ("lease_owner", "lease_expires_at", "heartbeat_at")
    )
    result = getattr(record, "result")
    cancellation = getattr(record, "cancellation")
    error_code = str(getattr(record, "error_code")).strip()
    error_message = str(getattr(record, "error_message")).strip()
    if (
        checkpoints
        and phase not in {"completed", "failed", "cancelled"}
        and getattr(checkpoints[-1], "phase") != phase
    ):
        raise ValueError(f"{label} operation phase must match its latest checkpoint.")
    if status in {"pass", "fail", "cancelled"}:
        if not finished_at:
            raise ValueError(f"Terminal {label} operations require finished_at.")
        if status == "pass" and (error_code or error_message):
            raise ValueError(f"Passing {label} operations cannot include errors.")
        if status == "fail" and not error_message:
            raise ValueError(f"Failed {label} operations require error_message.")
        if status == "cancelled":
            if cancellation is None:
                raise ValueError(f"Cancelled {label} operations require evidence.")
            if result is not None or error_code or error_message:
                raise ValueError(f"Cancelled {label} operations cannot include result or error.")
    elif status == "reconciliation_required":
        if finished_at or holds_lease:
            raise ValueError(
                f"Reconciliation-required {label} operations cannot retain terminal or lease state."
            )
        if result is not None or not error_code or not error_message:
            raise ValueError(
                f"Reconciliation-required {label} operations require an error and no result."
            )
    elif cancellation is not None:
        raise ValueError(f"Only cancelled {label} operations can include cancellation.")


def odoo_prod_promotion_request_fingerprint(request: OdooProdPromotionRunRequest) -> str:
    return hashlib.sha256(
        json.dumps(request.model_dump(mode="json"), sort_keys=True, separators=(",", ":")).encode(
            "utf-8"
        )
    ).hexdigest()


def build_odoo_prod_promotion_operation_id(
    *,
    product: str,
    context: str,
    idempotency_key: str,
    idempotency_scope: str,
) -> str:
    """One operation per caller, key, and lane, so a retried enqueue finds its operation."""

    digest_input = json.dumps(
        [
            product.strip().lower(),
            context.strip().lower(),
            "prod",
            idempotency_key.strip(),
            idempotency_scope.strip(),
        ],
        separators=(",", ":"),
    )
    return "odoo-prod-promotion-" + hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:32]
