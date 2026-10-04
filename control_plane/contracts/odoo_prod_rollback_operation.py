from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
    DurableOperationCancellation,
)
from control_plane.contracts.odoo_prod_promotion_operation import (
    validate_release_operation_state,
)


ODOO_PROD_ROLLBACK_ACTION = "odoo_prod_rollback.execute"


class OdooProdRollbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    instance: str = "prod"
    # Accepted from the site rollback workflows until they are deleted; the
    # default target is the previous passing prod deployment, not a channel.
    source_channel: Literal["testing"] = "testing"
    promotion_record_id: str = ""
    artifact_id: str = ""
    reason: str = ""
    wait: bool = True
    timeout_seconds: int | None = Field(default=None, ge=1)
    verify_health: bool = True
    health_timeout_seconds: int | None = Field(default=None, ge=1)
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "OdooProdRollbackRequest":
        self.context = self.context.strip().lower()
        self.instance = self.instance.strip().lower()
        self.promotion_record_id = self.promotion_record_id.strip()
        self.artifact_id = self.artifact_id.strip()
        self.reason = self.reason.strip()
        if not self.context:
            raise ValueError("Odoo prod rollback requires context.")
        if self.instance != "prod":
            raise ValueError("Odoo prod rollback requires instance 'prod'.")
        if self.verify_health and not self.wait:
            raise ValueError("Odoo prod rollback health verification requires wait=true.")
        return self


class OdooProdRollbackResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    source_channel: str
    artifact_id: str
    promotion_record_id: str
    deployment_record_id: str = ""
    release_tuple_id: str = ""
    rollback_status: Literal["pass", "fail"]
    rollback_health_status: Literal["pass", "fail", "skipped"] = "skipped"
    rollback_started_at: str = ""
    rollback_finished_at: str = ""
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
    error_message: str = ""


class OdooProdRollbackTarget(BaseModel):
    """The artifact a rollback redeploys, resolved once before any effect.

    ``deployment_record_id`` names the previous passing prod deployment the
    default target came from; it is empty for an explicitly chosen artifact.
    """

    model_config = ConfigDict(extra="forbid")

    artifact_id: str
    deployment_record_id: str = ""

    @model_validator(mode="after")
    def _validate_target(self) -> "OdooProdRollbackTarget":
        self.artifact_id = self.artifact_id.strip()
        self.deployment_record_id = self.deployment_record_id.strip()
        if not self.artifact_id:
            raise ValueError("Odoo prod rollback target requires artifact_id.")
        return self


OdooProdRollbackOperationStatus = Literal[
    "pending",
    "running",
    "reconciliation_required",
    "pass",
    "fail",
    "cancelled",
]
OdooProdRollbackOperationPhase = Literal[
    "created",
    "running",
    "validated",
    "rollback_started",
    "completed",
    "failed",
    "cancelled",
]
ODOO_PROD_ROLLBACK_OPERATION_PHASE_SEQUENCE: tuple[OdooProdRollbackOperationPhase, ...] = (
    "created",
    "running",
    "validated",
    "rollback_started",
    "completed",
    "failed",
    "cancelled",
)
# Before the first provider effect nothing has changed prod, so an expired lease
# in these phases may run again. Once the redeploy started it may have deployed.
ODOO_PROD_ROLLBACK_SAFE_RETRY_PHASES: tuple[OdooProdRollbackOperationPhase, ...] = (
    "created",
    "running",
    "validated",
)


class OdooProdRollbackCheckpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    phase: OdooProdRollbackOperationPhase
    recorded_at: str
    evidence: dict[str, str] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _validate_checkpoint(self) -> "OdooProdRollbackCheckpoint":
        self.recorded_at = self.recorded_at.strip()
        if not self.recorded_at:
            raise ValueError("Odoo prod rollback checkpoint requires recorded_at.")
        return self


class OdooProdRollbackOperationRecord(BaseModel):
    """A queued Odoo prod rollback to a target fixed at enqueue."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    operation_id: str
    product: str
    context: str
    instance: Literal["prod"]
    idempotency_key: str
    idempotency_scope: str
    request_fingerprint: str
    request: OdooProdRollbackRequest
    target: OdooProdRollbackTarget
    authorization: DurableOperationAuthorization
    status: OdooProdRollbackOperationStatus = "pending"
    phase: OdooProdRollbackOperationPhase = "created"
    checkpoints: tuple[OdooProdRollbackCheckpoint, ...] = ()
    created_at: str
    updated_at: str
    started_at: str = ""
    finished_at: str = ""
    lease_owner: str = ""
    lease_expires_at: str = ""
    heartbeat_at: str = ""
    attempt: int = Field(default=0, ge=0)
    result: OdooProdRollbackResult | None = None
    cancellation: DurableOperationCancellation | None = None
    error_code: str = ""
    error_message: str = ""
    runner_trace_id: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "OdooProdRollbackOperationRecord":
        for field_name in (
            "operation_id",
            "product",
            "context",
            "idempotency_key",
            "idempotency_scope",
            "request_fingerprint",
            "created_at",
            "updated_at",
        ):
            normalized_value = str(getattr(self, field_name)).strip()
            if not normalized_value:
                raise ValueError(f"Odoo prod rollback operation requires {field_name}.")
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
        if self.context != self.request.context or self.instance != self.request.instance:
            raise ValueError("Odoo prod rollback operation target must match request.")
        if not self.request.wait:
            raise ValueError("Odoo prod rollback operations wait for the redeploy to finish.")
        if self.request.artifact_id and self.request.artifact_id != self.target.artifact_id:
            raise ValueError("Odoo prod rollback target must be the requested artifact.")
        if (
            self.authorization.action != ODOO_PROD_ROLLBACK_ACTION
            or self.authorization.product != self.product
            or self.authorization.context != self.context
            or self.authorization.instances != (self.instance,)
        ):
            raise ValueError("Odoo prod rollback authorization must match the operation.")
        if self.authorization.grant not in {"policy_administrator", "client_release_acceptance"}:
            raise ValueError(
                "Only the admin or a Client's release may queue an Odoo prod rollback."
            )
        validate_release_operation_state(self, label="Odoo prod rollback")
        return self


def odoo_prod_rollback_request_fingerprint(
    *, product: str, request: OdooProdRollbackRequest
) -> str:
    return hashlib.sha256(
        json.dumps(
            {"product": product.strip(), "request": request.model_dump(mode="json")},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()


def build_odoo_prod_rollback_operation_id(
    *,
    product: str,
    context: str,
    idempotency_key: str,
    idempotency_scope: str,
) -> str:
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
    return "odoo-prod-rollback-" + hashlib.sha256(digest_input.encode("utf-8")).hexdigest()[:32]
