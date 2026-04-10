import json
from pathlib import Path

from pydantic import BaseModel

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.promotion_record import PromotionRecord


class FilesystemRecordStore:
    def __init__(self, state_dir: Path) -> None:
        self.state_dir = state_dir

    def _record_path(self, record_type: str, record_id: str) -> Path:
        return self.state_dir / record_type / f"{record_id}.json"

    def _write_model(self, record_type: str, record_id: str, model: BaseModel) -> Path:
        record_path = self._record_path(record_type, record_id)
        record_path.parent.mkdir(parents=True, exist_ok=True)
        record_path.write_text(json.dumps(model.model_dump(mode="json"), indent=2, sort_keys=True), encoding="utf-8")
        return record_path

    def _read_model(self, model_type: type[BaseModel], record_type: str, record_id: str) -> BaseModel:
        record_path = self._record_path(record_type, record_id)
        payload = json.loads(record_path.read_text(encoding="utf-8"))
        return model_type.model_validate(payload)

    def _record_dir(self, record_type: str) -> Path:
        return self.state_dir / record_type

    def write_artifact_manifest(self, manifest: ArtifactIdentityManifest) -> Path:
        return self._write_model("artifacts", manifest.artifact_id, manifest)

    def read_artifact_manifest(self, artifact_id: str) -> ArtifactIdentityManifest:
        return ArtifactIdentityManifest.model_validate(
            self._read_model(ArtifactIdentityManifest, "artifacts", artifact_id).model_dump(mode="json")
        )

    def find_artifact_manifests_by_commit(self, odoo_ai_commit: str) -> tuple[ArtifactIdentityManifest, ...]:
        record_dir = self._record_dir("artifacts")
        if not record_dir.exists():
            return ()

        matching_manifests: list[ArtifactIdentityManifest] = []
        for manifest_path in sorted(record_dir.glob("*.json")):
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest = ArtifactIdentityManifest.model_validate(payload)
            if manifest.odoo_ai_commit == odoo_ai_commit:
                matching_manifests.append(manifest)
        return tuple(matching_manifests)

    def write_promotion_record(self, record: PromotionRecord) -> Path:
        return self._write_model("promotions", record.record_id, record)

    def read_promotion_record(self, record_id: str) -> PromotionRecord:
        return PromotionRecord.model_validate(
            self._read_model(PromotionRecord, "promotions", record_id).model_dump(mode="json")
        )
