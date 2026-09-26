from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.production_backup_authority import (
    ProductionBackupPolicyRecord,
    ProductionBackupTargetRecord,
    ProxmoxGuestBackupDestinationReference,
    ProxmoxStorageBackupDestinationReference,
)


PRODUCTION_BACKUP_GATE_EXECUTE_ACTION = "production_backup_gate.execute"


class ProductionBackupGateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    schema_version: Literal[1] = 1
    product: str = Field(min_length=1, max_length=128)
    context: str = Field(min_length=1, max_length=128)
    instance: str = Field(min_length=1, max_length=128)
    promotion_action: str = Field(min_length=1, max_length=256)
    backup_record_id: str = Field(min_length=1, max_length=256)
    timeout_seconds: int = Field(default=1800, ge=1, le=7200)

    @field_validator("product", "context", "instance")
    @classmethod
    def _normalize_scope(cls, value: str) -> str:
        return value.lower()


class ProductionBackupGateWorkerRequest(BaseModel):
    """A service-resolved binding; never accepted as caller-supplied topology."""

    model_config = ConfigDict(extra="forbid")

    request: ProductionBackupGateRequest
    policy: ProductionBackupPolicyRecord
    source_target: ProductionBackupTargetRecord
    destination_target: ProductionBackupTargetRecord

    @model_validator(mode="after")
    def _validate_binding(self) -> ProductionBackupGateWorkerRequest:
        request = self.request
        policy = self.policy
        if (policy.product, policy.context, policy.instance, policy.promotion_action) != (
            request.product,
            request.context,
            request.instance,
            request.promotion_action,
        ):
            raise ValueError("Production backup policy does not match the requested scope.")
        if policy.target_ids != (self.source_target.target_id, self.destination_target.target_id):
            raise ValueError("Production backup targets do not match the policy.")
        if any(
            record.status != "active"
            for record in (policy, self.source_target, self.destination_target)
        ):
            raise ValueError("Production backup execution requires active authority records.")
        source = self.source_target.destination
        destination = self.destination_target.destination
        if not isinstance(source, ProxmoxGuestBackupDestinationReference):
            raise ValueError("Production backup source must be a Proxmox guest.")
        if not isinstance(destination, ProxmoxStorageBackupDestinationReference):
            raise ValueError("Production backup destination must be Proxmox storage.")
        if (source.host, source.username) != (destination.host, destination.username):
            raise ValueError("Production backup targets must use the same provider endpoint.")
        return self


class ProductionBackupGateWorkerResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["pass", "fail"]
    started_at: str
    finished_at: str
    evidence: dict[str, str] = Field(default_factory=dict)
    error_code: str = ""
