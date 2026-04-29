import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.idempotency_record import build_launchplane_idempotency_record_id
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.preview_enablement_record import PreviewEnablementRecord
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.preview_lifecycle_cleanup_record import PreviewLifecycleCleanupRecord
from control_plane.contracts.preview_lifecycle_plan_record import PreviewLifecyclePlanRecord
from control_plane.contracts.preview_pr_feedback_record import PreviewPrFeedbackRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord

RecordModel = TypeVar("RecordModel", bound=BaseModel)


class FilesystemRecordStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir

    def _record_path(self, record_type: str, record_id: str) -> Path:
        return self.state_dir / record_type / f"{record_id}.json"

    def _write_model(self, record_type: str, record_id: str, model: BaseModel) -> Path:
        record_path = self._record_path(record_type, record_id)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(
            json.dumps(model.model_dump(mode="json", exclude_none=True), indent=2, sort_keys=True),
            encoding="utf-8",
        )
        return record_path

    def _read_model(self, model_type: type[BaseModel], record_type: str, record_id: str) -> BaseModel:
        record_path = self._record_path(record_type, record_id)
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        return model_type.model_validate(payload)

    def _record_dir(self, record_type: str) -> Path:
        return self.state_dir / record_type

    def _list_models(self, model_type: type[RecordModel], record_type: str) -> tuple[RecordModel, ...]:
        record_dir = self._record_dir(record_type)
        if not record_dir.exists():
            return ()

        records: list[RecordModel] = []
        for record_path in sorted(record_dir.glob("*.json")):
            payload = json.loads(record_path.read_text(encoding="utf-8"))
            records.append(model_type.model_validate(payload))
        return tuple(records)

    @staticmethod
    def _record_sort_timestamp(finished_at: str, started_at: str) -> tuple[str, str]:
        return finished_at or started_at, started_at

    def write_artifact_manifest(self, manifest: ArtifactIdentityManifest) -> Path:
        return self._write_model("artifacts", manifest.artifact_id, manifest)

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest:
        return ArtifactIdentityManifest.model_validate(
            self._read_model(ArtifactIdentityManifest, "artifacts", artifact_id).model_dump(mode="json")
        )

    def list_artifact_manifests(self) -> tuple[ArtifactIdentityManifest, ...]:
        records = list(self._list_models(ArtifactIdentityManifest, "artifacts"))
        records.sort(key=lambda record: record.artifact_id, reverse=True)
        return tuple(records)

    def write_release_tuple_record(self, record: ReleaseTupleRecord) -> Path:
        return self._write_model("release_tuples", f"{record.context}-{record.channel}", record)

    def write_authz_policy_record(self, record: LaunchplaneAuthzPolicyRecord) -> Path:
        return self._write_model("launchplane_authz_policies", record.record_id, record)

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]:
        records = [
            record
            for record in self._list_models(
                LaunchplaneAuthzPolicyRecord, "launchplane_authz_policies"
            )
            if not status or record.status == status
        ]
        records.sort(key=lambda record: (record.updated_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def read_release_tuple_record(self, *, context_name: str, channel_name: str) -> ReleaseTupleRecord:
        return ReleaseTupleRecord.model_validate(
            self._read_model(
                ReleaseTupleRecord,
                "release_tuples",
                f"{context_name}-{channel_name}",
            ).model_dump(mode="json")
        )

    def list_release_tuple_records(self) -> tuple[ReleaseTupleRecord, ...]:
        records = list(self._list_models(ReleaseTupleRecord, "release_tuples"))
        records.sort(key=lambda record: (record.context, record.channel))
        return tuple(records)

    def write_idempotency_record(self, record: LaunchplaneIdempotencyRecord) -> Path:
        return self._write_model("idempotency", record.record_id, record)

    def read_idempotency_record(
        self,
        *,
        scope: str,
        route_path: str,
        idempotency_key: str,
    ) -> LaunchplaneIdempotencyRecord | None:
        record_id = build_launchplane_idempotency_record_id(
            scope=scope,
            route_path=route_path,
            request_token=idempotency_key,
        )
        record_path = self._record_path("idempotency", record_id)
        if not record_path.exists():
            return None
        return LaunchplaneIdempotencyRecord.model_validate(
            self._read_model(LaunchplaneIdempotencyRecord, "idempotency", record_id).model_dump(mode="json")
        )

    def write_backup_gate_record(self, record: BackupGateRecord) -> Path:
        return self._write_model("backup_gates", record.record_id, record)

    def read_backup_gate_record(self, record_id: str) -> BackupGateRecord:
        return BackupGateRecord.model_validate(
            self._read_model(BackupGateRecord, "backup_gates", record_id).model_dump(mode="json")
        )

    def list_backup_gate_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[BackupGateRecord, ...]:
        records = [
            record
            for record in self._list_models(BackupGateRecord, "backup_gates")
            if (not context_name or record.context == context_name) and (not instance_name or record.instance == instance_name)
        ]
        records.sort(key=lambda record: (record.created_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_promotion_record(self, record: PromotionRecord) -> Path:
        return self._write_model("promotions", record.record_id, record)

    def read_promotion_record(self, record_id: str) -> PromotionRecord:
        return PromotionRecord.model_validate(
            self._read_model(PromotionRecord, "promotions", record_id).model_dump(mode="json")
        )

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PromotionRecord, ...]:
        records = [
            record
            for record in self._list_models(PromotionRecord, "promotions")
            if (not context_name or record.context == context_name)
            and (not from_instance_name or record.from_instance == from_instance_name)
            and (not to_instance_name or record.to_instance == to_instance_name)
        ]
        records.sort(
            key=lambda record: (*self._record_sort_timestamp(record.deploy.finished_at, record.deploy.started_at), record.record_id),
            reverse=True,
        )
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_deployment_record(self, record: DeploymentRecord) -> Path:
        return self._write_model("deployments", record.record_id, record)

    def read_deployment_record(self, record_id: str) -> DeploymentRecord:
        return DeploymentRecord.model_validate(
            self._read_model(DeploymentRecord, "deployments", record_id).model_dump(mode="json")
        )

    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[DeploymentRecord, ...]:
        records = [
            record
            for record in self._list_models(DeploymentRecord, "deployments")
            if (not context_name or record.context == context_name) and (not instance_name or record.instance == instance_name)
        ]
        records.sort(
            key=lambda record: (*self._record_sort_timestamp(record.deploy.finished_at, record.deploy.started_at), record.record_id),
            reverse=True,
        )
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_environment_inventory(self, record: EnvironmentInventory) -> Path:
        return self._write_model("inventory", f"{record.context}-{record.instance}", record)

    def read_environment_inventory(self, *, context_name: str, instance_name: str) -> EnvironmentInventory:
        record_id = f"{context_name}-{instance_name}"
        return EnvironmentInventory.model_validate(
            self._read_model(EnvironmentInventory, "inventory", record_id).model_dump(mode="json")
        )

    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]:
        return self._list_models(EnvironmentInventory, "inventory")

    def write_odoo_instance_override_record(self, record: OdooInstanceOverrideRecord) -> Path:
        return self._write_model("odoo_instance_overrides", f"{record.context}-{record.instance}", record)

    def read_odoo_instance_override_record(self, *, context_name: str, instance_name: str) -> OdooInstanceOverrideRecord:
        record_id = f"{context_name}-{instance_name}"
        return OdooInstanceOverrideRecord.model_validate(
            self._read_model(OdooInstanceOverrideRecord, "odoo_instance_overrides", record_id).model_dump(mode="json")
        )

    def list_odoo_instance_override_records(self) -> tuple[OdooInstanceOverrideRecord, ...]:
        records = list(self._list_models(OdooInstanceOverrideRecord, "odoo_instance_overrides"))
        records.sort(key=lambda record: (record.context, record.instance))
        return tuple(records)

    def write_preview_record(self, record: PreviewRecord) -> Path:
        return self._write_model("launchplane_previews", record.preview_id, record)

    def read_preview_record(self, preview_id: str) -> PreviewRecord:
        return PreviewRecord.model_validate(
            self._read_model(PreviewRecord, "launchplane_previews", preview_id).model_dump(mode="json")
        )

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]:
        records = [
            record
            for record in self._list_models(PreviewRecord, "launchplane_previews")
            if (not context_name or record.context == context_name)
            and (not anchor_repo or record.anchor_repo == anchor_repo)
            and (anchor_pr_number is None or record.anchor_pr_number == anchor_pr_number)
        ]
        records.sort(key=lambda record: (record.updated_at, record.preview_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_enablement_record(self, record: PreviewEnablementRecord) -> Path:
        return self._write_model("launchplane_preview_enablement", record.record_id, record)

    def read_preview_enablement_record(self, record_id: str) -> PreviewEnablementRecord:
        return PreviewEnablementRecord.model_validate(
            self._read_model(PreviewEnablementRecord, "launchplane_preview_enablement", record_id).model_dump(
                mode="json"
            )
        )

    def list_preview_enablement_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        pr_state: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewEnablementRecord, ...]:
        records = [
            record
            for record in self._list_models(PreviewEnablementRecord, "launchplane_preview_enablement")
            if (not context_name or record.context == context_name)
            and (not anchor_repo or record.anchor_repo == anchor_repo)
            and (not pr_state or record.pr_state == pr_state)
        ]
        records.sort(key=lambda record: (record.updated_at, record.record_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_generation_record(self, record: PreviewGenerationRecord) -> Path:
        return self._write_model("launchplane_preview_generations", record.generation_id, record)

    def read_preview_generation_record(self, generation_id: str) -> PreviewGenerationRecord:
        return PreviewGenerationRecord.model_validate(
            self._read_model(
                PreviewGenerationRecord,
                "launchplane_preview_generations",
                generation_id,
            ).model_dump(mode="json")
        )

    def list_preview_generation_records(
        self,
        *,
        preview_id: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewGenerationRecord, ...]:
        records = [
            record
            for record in self._list_models(PreviewGenerationRecord, "launchplane_preview_generations")
            if not preview_id or record.preview_id == preview_id
        ]
        records.sort(key=lambda record: (record.sequence, record.generation_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_inventory_scan_record(self, record: PreviewInventoryScanRecord) -> Path:
        return self._write_model("launchplane_preview_inventory_scans", record.scan_id, record)

    def list_preview_inventory_scan_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewInventoryScanRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PreviewInventoryScanRecord, "launchplane_preview_inventory_scans"
            )
            if not context_name or record.context == context_name
        ]
        records.sort(key=lambda record: (record.scanned_at, record.scan_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_desired_state_record(self, record: PreviewDesiredStateRecord) -> Path:
        return self._write_model("launchplane_preview_desired_states", record.desired_state_id, record)

    def list_preview_desired_state_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewDesiredStateRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PreviewDesiredStateRecord, "launchplane_preview_desired_states"
            )
            if not context_name or record.context == context_name
        ]
        records.sort(key=lambda record: (record.discovered_at, record.desired_state_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_lifecycle_plan_record(self, record: PreviewLifecyclePlanRecord) -> Path:
        return self._write_model("launchplane_preview_lifecycle_plans", record.plan_id, record)

    def list_preview_lifecycle_plan_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewLifecyclePlanRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PreviewLifecyclePlanRecord, "launchplane_preview_lifecycle_plans"
            )
            if not context_name or record.context == context_name
        ]
        records.sort(key=lambda record: (record.planned_at, record.plan_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_lifecycle_cleanup_record(
        self, record: PreviewLifecycleCleanupRecord
    ) -> Path:
        return self._write_model("launchplane_preview_lifecycle_cleanups", record.cleanup_id, record)

    def list_preview_lifecycle_cleanup_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewLifecycleCleanupRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PreviewLifecycleCleanupRecord, "launchplane_preview_lifecycle_cleanups"
            )
            if not context_name or record.context == context_name
        ]
        records.sort(key=lambda record: (record.requested_at, record.cleanup_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)

    def write_preview_pr_feedback_record(self, record: PreviewPrFeedbackRecord) -> Path:
        return self._write_model("launchplane_preview_pr_feedback", record.feedback_id, record)

    def list_preview_pr_feedback_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackRecord, ...]:
        records = [
            record
            for record in self._list_models(
                PreviewPrFeedbackRecord, "launchplane_preview_pr_feedback"
            )
            if not context_name or record.context == context_name
        ]
        records.sort(key=lambda record: (record.requested_at, record.feedback_id), reverse=True)
        if limit is not None:
            records = records[:limit]
        return tuple(records)
