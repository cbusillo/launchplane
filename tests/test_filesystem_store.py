import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest, ArtifactImageReference
from control_plane.contracts.deployment_record import DeploymentRecord
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

    def test_write_and_read_deployment_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = DeploymentRecord(
                record_id="deployment-20260410T182231Z-opw-prod",
                artifact_identity={"artifact_id": "artifact-20260410-f45db648"},
                context="opw",
                instance="prod",
                source_git_ref="abc123",
                deploy=DeploymentEvidence(
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    deployment_id="delegated-odoo-ai-ship",
                    status="pass",
                    started_at="2026-04-10T18:22:31Z",
                    finished_at="2026-04-10T18:24:00Z",
                ),
                destination_health={
                    "verified": True,
                    "urls": ["https://prod.example.com/web/health"],
                    "timeout_seconds": 45,
                    "status": "pass",
                },
            )

            written_path = store.write_deployment_record(record)
            loaded_record = store.read_deployment_record(record.record_id)
            self.assertTrue(written_path.exists())
            self.assertEqual(loaded_record.record_id, record.record_id)
            self.assertEqual(loaded_record.deploy.deployment_id, "delegated-odoo-ai-ship")
            self.assertEqual(loaded_record.destination_health.status, "pass")

    def test_artifacts_ingest_odoo_ai_writes_manifest(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "artifact-manifest.json"
            input_file.write_text(
                ArtifactIdentityManifest(
                    artifact_id="artifact-sha256-image456",
                    odoo_ai_commit="f45db648",
                    enterprise_base_digest="sha256:enterprise123",
                    addon_sources=({"repository": "cbusillo/disable_odoo_online", "ref": "main"},),
                    image=ArtifactImageReference(
                        repository="ghcr.io/cbusillo/odoo-private",
                        digest="sha256:image456",
                        tags=("sha-f45db648",),
                    ),
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "artifacts",
                    "ingest-odoo-ai",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            persisted_manifest = state_dir / "artifacts" / "artifact-sha256-image456.json"
            self.assertTrue(persisted_manifest.exists())

    def test_find_artifact_manifests_by_commit_returns_matches(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_artifact_manifest(
                ArtifactIdentityManifest(
                    artifact_id="artifact-sha256-image456",
                    odoo_ai_commit="abc123",
                    enterprise_base_digest="sha256:enterprise123",
                    image=ArtifactImageReference(
                        repository="ghcr.io/cbusillo/odoo-private",
                        digest="sha256:image456",
                    ),
                )
            )
            store.write_artifact_manifest(
                ArtifactIdentityManifest(
                    artifact_id="artifact-sha256-image789",
                    odoo_ai_commit="def456",
                    enterprise_base_digest="sha256:enterprise123",
                    image=ArtifactImageReference(
                        repository="ghcr.io/cbusillo/odoo-private",
                        digest="sha256:image789",
                    ),
                )
            )

            matching_manifests = store.find_artifact_manifests_by_commit("abc123")

            self.assertEqual(len(matching_manifests), 1)
            self.assertEqual(matching_manifests[0].artifact_id, "artifact-sha256-image456")
