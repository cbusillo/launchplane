"""Replace disposable testing without declaring the old provider effects safe."""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import cast

from control_plane.contracts.disposable_lane import (
    DisposableLaneSafety,
    DisposableTestingReplacementPreparation,
    DisposableTestingSupersession,
)
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord
from control_plane.disposable_lanes import (
    DisposableLaneStore,
    DisposableLaneSafetyUnknown,
    require_disposable_lane,
)


@dataclass
class ReplacementBindings:
    profile: LaunchplaneProductProfileRecord
    target: DokployTargetRecord
    target_id: DokployTargetIdRecord
    all_target_ids: tuple[DokployTargetIdRecord, ...]
    runtime_records: tuple[RuntimeEnvironmentRecord, ...]
    secret_records: tuple[SecretRecord, ...]
    secret_bindings: tuple[SecretBinding, ...]
    safety: DisposableLaneSafety
    preparation: DisposableTestingReplacementPreparation

    def read_disposable_lane_safety(
        self, product: str, context: str, instance: str
    ) -> DisposableLaneSafety | None:
        return (
            self.safety
            if (product, context, instance)
            == (self.safety.product, self.safety.context, self.safety.instance)
            else None
        )

    def list_runtime_environment_records(
        self, *, scope: str = "", context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        return self.runtime_records

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretRecord, ...]:
        return self.secret_records

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        return self.secret_bindings

    def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]:
        return self.all_target_ids

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord:
        if (context_name, instance_name) != (self.target.context, self.target.instance):
            raise FileNotFoundError(context_name)
        return self.target

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord:
        if (context_name, instance_name) != (self.target_id.context, self.target_id.instance):
            raise FileNotFoundError(context_name)
        return self.target_id


def disposable_testing_supersession(
    operation: OdooStableTargetReplacementOperationRecord,
    bindings: ReplacementBindings,
) -> OdooStableTargetReplacementOperationRecord:
    from control_plane.testing_replacement_recovery import service_testing_operation

    if not service_testing_operation(operation) or operation.status != "reconciliation_required":
        raise DisposableLaneSafetyUnknown(
            "Only an excluded service-owned testing replacement can be superseded."
        )
    require_disposable_lane(
        cast(DisposableLaneStore, bindings),
        product=operation.product,
        context=operation.context,
        instance=operation.instance,
    )
    evidence = bindings.safety
    preparation = bindings.preparation
    if (
        preparation.operation_id,
        preparation.product,
        preparation.context,
        preparation.instance,
    ) != (operation.operation_id, operation.product, operation.context, operation.instance):
        raise DisposableLaneSafetyUnknown(
            "Captured preparation belongs to another operation or lane."
        )
    if preparation.retained_target_id != evidence.replaced_target_id:
        raise DisposableLaneSafetyUnknown(
            "Rebuild requires its service-captured retained target identity."
        )
    if evidence.replaces_operation_id != operation.operation_id or not evidence.replaced_target_id:
        raise DisposableLaneSafetyUnknown(
            "Replacement evidence does not identify the retained operation and target."
        )
    if (
        operation.recovery_journal is not None
        and operation.recovery_journal.target_id != evidence.replaced_target_id
    ):
        raise DisposableLaneSafetyUnknown(
            "Replacement evidence identifies a different retained target."
        )
    supersession = DisposableTestingSupersession(
        binding_sha256=evidence.binding_sha256,
        retained_target_id=evidence.replaced_target_id,
        replacement_target_id=bindings.target_id.target_id,
        evidence_reference=evidence.isolation_evidence,
        recorded_at=datetime.now(timezone.utc),
    )
    stamp = supersession.recorded_at.isoformat()
    return OdooStableTargetReplacementOperationRecord.model_validate(
        {
            **operation.model_dump(mode="json"),
            "status": "superseded",
            "phase": "superseded",
            "updated_at": stamp,
            "finished_at": stamp,
            "lease_owner": "",
            "lease_expires_at": "",
            "heartbeat_at": "",
            "disposable_supersession": supersession.model_dump(mode="json"),
        }
    )
