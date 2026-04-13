import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import PromotionRecord

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
