import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.contracts.artifact_identity import ArtifactIdentityManifest, ArtifactImageReference
from control_plane.contracts.promotion_record import DeploymentEvidence, PromotionRecord
from control_plane.storage.filesystem import FilesystemRecordStore


class FilesystemRecordStoreTests(unittest.TestCase):
    def test_write_and_read_artifact_manifest(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            manifest = ArtifactIdentityManifest(
                artifact_id="artifact-20260410-f45db648",
                odoo_ai_commit="f45db648",
                enterprise_base_digest="sha256:enterprise123",
                image=ArtifactImageReference(
                    repository="ghcr.io/cbusillo/odoo-private",
                    digest="sha256:image456",
                    tags=("sha-f45db648",),
                ),
            )

            written_path = store.write_artifact_manifest(manifest)
            loaded_manifest = store.read_artifact_manifest(manifest.artifact_id)
            self.assertTrue(written_path.exists())
            self.assertEqual(loaded_manifest.artifact_id, manifest.artifact_id)
            self.assertEqual(loaded_manifest.image.digest, "sha256:image456")

    def test_write_and_read_promotion_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = PromotionRecord(
                record_id="promotion-20260410-182231-opw-testing-prod",
                artifact_identity={"artifact_id": "artifact-20260410-f45db648"},
                context="opw",
                from_instance="testing",
                to_instance="prod",
                deploy=DeploymentEvidence(
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                ),
            )

            written_path = store.write_promotion_record(record)
            loaded_record = store.read_promotion_record(record.record_id)
            self.assertTrue(written_path.exists())
            self.assertEqual(loaded_record.record_id, record.record_id)
            self.assertEqual(loaded_record.deploy.target_name, "opw-prod")
