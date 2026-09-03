from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.production_backup_authority import (
    ProductionBackupPolicyRecord,
    ProductionBackupTargetRecord,
    ProductionFastSnapshotPolicy,
    ProductionIndependentBackupPolicy,
    ProxmoxGuestBackupDestinationReference,
    ProxmoxStorageBackupDestinationReference,
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
    existing_targets = tuple(
        record
        for target_id in (request.source_target_id, request.destination_target_id)
        for record in record_store.list_production_backup_target_records(target_id=target_id)
    )
    existing_policies = record_store.list_production_backup_policy_records(
        product=request.product,
        context_name=request.context,
        instance_name=request.instance,
        promotion_action=request.promotion_action,
    )
    if existing_targets or existing_policies:
        raise ValueError(
            "legacy production backup migration requires absent typed target and policy streams"
        )
    runtime_records = record_store.list_runtime_environment_records(
        scope="instance",
        context_name=request.context,
        instance_name=request.instance,
    )
    exact_records = tuple(
        record
        for record in runtime_records
        if record.scope == "instance"
        and record.context == request.context
        and record.instance == request.instance
    )
    if len(exact_records) != 1:
        raise ValueError(
            "legacy production backup migration requires exactly one exact instance runtime-environment record"
        )
    runtime_record = exact_records[0]
    if runtime_record.updated_at != request.runtime_environment_updated_at:
        raise ValueError("legacy production backup migration runtime-environment revision changed")
    env = runtime_record.env
    host = _required_runtime_value(env, "VERIREEL_PROD_PROXMOX_HOST")
    username = _required_runtime_value(env, "VERIREEL_PROD_PROXMOX_USER")
    guest_id = _required_runtime_value(env, "VERIREEL_PROD_CT_ID")
    storage_id = _required_runtime_value(env, "VERIREEL_PROD_BACKUP_STORAGE")
    snapshot_prefix = _required_runtime_value(env, "VERIREEL_PROD_SNAPSHOT_PREFIX")
    retention_count = _non_negative_runtime_int(env, "VERIREEL_PROD_SNAPSHOT_KEEP")
    backup_modes = _legacy_backup_modes(_required_runtime_value(env, "VERIREEL_PROD_BACKUP_MODE"))
    if backup_modes != {"snapshot", "vzdump"}:
        raise ValueError(
            "legacy production backup migration requires both snapshot and independent backup modes"
        )
    targets = (
        ProductionBackupTargetRecord(
            target_id=request.source_target_id,
            target_revision=1,
            destination=ProxmoxGuestBackupDestinationReference(
                host=host,
                username=username,
                guest_kind="lxc",
                guest_id=guest_id,
            ),
            effective_at=request.effective_at,
            review_after=request.review_after,
            source=request.source,
            reason=request.reason,
        ),
        ProductionBackupTargetRecord(
            target_id=request.destination_target_id,
            target_revision=1,
            destination=ProxmoxStorageBackupDestinationReference(
                host=host,
                username=username,
                storage_id=storage_id,
            ),
            effective_at=request.effective_at,
            review_after=request.review_after,
            source=request.source,
            reason=request.reason,
        ),
    )
    policy = ProductionBackupPolicyRecord(
        product=request.product,
        context=request.context,
        instance=request.instance,
        promotion_action=request.promotion_action,
        policy_revision=1,
        fast_snapshot=ProductionFastSnapshotPolicy(
            source_target_id=request.source_target_id,
            snapshot_prefix=snapshot_prefix,
            retention_count=retention_count,
            max_evidence_age_seconds=request.snapshot_max_evidence_age_seconds,
        ),
        independent_backup=ProductionIndependentBackupPolicy(
            source_target_id=request.source_target_id,
            destination_target_id=request.destination_target_id,
            max_evidence_age_seconds=request.independent_backup_max_evidence_age_seconds,
        ),
        effective_at=request.effective_at,
        review_after=request.review_after,
        source=request.source,
        reason=request.reason,
    )
    return ProductionBackupAuthorityWriteEnvelope(
        mode=request.mode,
        targets=targets,
        policy=policy,
        reviewed_authority_digest=request.reviewed_authority_digest,
    )


def _required_runtime_value(env: Mapping[str, object], key: str) -> str:
    value = str(env.get(key, "")).strip()
    if not value:
        raise ValueError(f"legacy production backup migration requires runtime key {key}")
    return value


def _non_negative_runtime_int(env: Mapping[str, object], key: str) -> int:
    value = _required_runtime_value(env, key)
    if value.startswith("-") or not value.lstrip("+").isdigit():
        raise ValueError(f"legacy production backup migration requires non-negative {key}")
    return int(value)


def _legacy_backup_modes(raw_value: str) -> set[str]:
    tokens = {
        token.strip().lower() for token in raw_value.replace(":", ",").split(",") if token.strip()
    }
    if "both" in tokens:
        tokens.remove("both")
        tokens.update({"snapshot", "vzdump"})
    if "none" in tokens or tokens.difference({"snapshot", "vzdump"}):
        raise ValueError("legacy production backup migration has unsupported backup mode")
    return tokens
