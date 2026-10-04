from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.production_backup_authority import (
    ProductionBackupPolicyRecord,
    ProductionBackupTargetRecord,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.production_backup_authority import (
    ProductionBackupAuthorityWriteEnvelope,
    ProductionBackupAuthorityWriteMode,
)


class LegacyProductionBackupMigrationStore(Protocol):
    def list_runtime_environment_records(
        self,
        *,
        scope: str = "",
        context_name: str = "",
        instance_name: str = "",
    ) -> tuple[RuntimeEnvironmentRecord, ...]: ...

    def list_production_backup_target_records(
        self,
        *,
        target_id: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ProductionBackupTargetRecord, ...]: ...

    def list_production_backup_policy_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        promotion_action: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[ProductionBackupPolicyRecord, ...]: ...


class LegacyProductionBackupMigrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: ProductionBackupAuthorityWriteMode
    product: str
    context: str
    instance: str
    promotion_action: str
    source_target_id: str
    destination_target_id: str
    runtime_environment_updated_at: str
    effective_at: str
    review_after: str
    snapshot_max_evidence_age_seconds: int = Field(ge=1, le=86_400)
    independent_backup_max_evidence_age_seconds: int = Field(ge=1, le=604_800)
    source: str
    reason: str
    reviewed_authority_digest: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> LegacyProductionBackupMigrationRequest:
        for field_name in (
            "product",
            "context",
            "instance",
            "promotion_action",
            "source_target_id",
            "destination_target_id",
            "runtime_environment_updated_at",
            "effective_at",
            "review_after",
            "source",
            "reason",
        ):
            value = str(getattr(self, field_name)).strip()
            if not value:
                raise ValueError(f"legacy production backup migration requires {field_name}")
            setattr(self, field_name, value)
        for field_name in (
            "product",
            "context",
            "instance",
            "source_target_id",
            "destination_target_id",
        ):
            setattr(self, field_name, str(getattr(self, field_name)).lower())
        self.reviewed_authority_digest = self.reviewed_authority_digest.strip().lower()
        if self.mode == "apply" and not self.reviewed_authority_digest:
            raise ValueError(
                "legacy production backup migration apply requires reviewed_authority_digest"
            )
        return self


def build_legacy_production_backup_authority_envelope(
    *,
    record_store: LegacyProductionBackupMigrationStore,
    request: LegacyProductionBackupMigrationRequest,
) -> ProductionBackupAuthorityWriteEnvelope:
    raise ValueError(
        "Legacy runtime-environment backup migration is unavailable; submit reviewed typed "
        "targets and policy through /v1/production-backup-authority/apply."
    )
