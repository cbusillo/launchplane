import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest, ArtifactImageReference
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_instance_override_record import OdooAddonSettingOverride
from control_plane.contracts.odoo_instance_override_record import OdooConfigParameterOverride
from control_plane.contracts.odoo_instance_override_record import OdooInstanceOverrideRecord
from control_plane.contracts.odoo_instance_override_record import OdooOverrideValue
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.promotion_record import DeploymentEvidence, PromotionRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.storage.filesystem import FilesystemRecordStore


class FilesystemRecordStoreTests(unittest.TestCase):
    def test_write_and_read_product_profile_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = LaunchplaneProductProfileRecord(
                product="sellyouroutboard",
                display_name="SellYourOutboard.com",
                repository="cbusillo/sellyouroutboard",
                driver_id="generic-web",
                image=ProductImageProfile(repository="ghcr.io/cbusillo/sellyouroutboard"),
                runtime_port=3000,
                health_path="/api/health",
                lanes=(ProductLaneProfile(instance="testing", context="sellyouroutboard"),),
                preview=ProductPreviewProfile(
                    enabled=True,
                    context="sellyouroutboard-testing",
                    slug_template="pr-{number}",
                ),
                updated_at="2026-04-30T20:00:00Z",
                source="operator:test",
            )

            written_path = store.write_product_profile_record(record)
            loaded_record = store.read_product_profile_record("sellyouroutboard")
            listed_records = store.list_product_profile_records(driver_id="generic-web")
            self.assertTrue(written_path.exists())

        self.assertEqual(loaded_record.driver_id, "generic-web")
        self.assertEqual(loaded_record.preview.context, "sellyouroutboard-testing")
        self.assertEqual([listed_record.product for listed_record in listed_records], ["sellyouroutboard"])

    def test_write_and_read_artifact_manifest(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            manifest = ArtifactIdentityManifest(
                artifact_id="artifact-20260410-f45db648",
                source_commit="f45db648",
                enterprise_base_digest="sha256:enterprise123",
                addon_selectors=(
                    {
                        "repository": "cbusillo/disable_odoo_online",
                        "selector": "main",
                        "resolved_ref": "f45db648",
                    },
                ),
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
            self.assertEqual(loaded_manifest.addon_selectors[0].selector, "main")

    def test_write_and_read_release_tuple_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = ReleaseTupleRecord(
                tuple_id="opw-testing-artifact-sha256-image456",
                context="opw",
                channel="testing",
                artifact_id="artifact-sha256-image456",
                repo_shas={
                    "tenant-opw": "abc1234",
                    "shared-addons": "def5678",
                },
                image_repository="ghcr.io/cbusillo/odoo-private",
                image_digest="sha256:image456",
                deployment_record_id="deployment-1",
                provenance="ship",
                minted_at="2026-04-10T18:24:00Z",
            )

            written_path = store.write_release_tuple_record(record)
            loaded_record = store.read_release_tuple_record(
                context_name="opw",
                channel_name="testing",
            )

            self.assertTrue(written_path.exists())
            self.assertEqual(loaded_record.tuple_id, record.tuple_id)
            self.assertEqual(loaded_record.repo_shas["shared-addons"], "def5678")

    def test_release_tuples_export_catalog_renders_state_records(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_release_tuple_record(
                ReleaseTupleRecord(
                    tuple_id="opw-testing-artifact-sha256-image456",
                    context="opw",
                    channel="testing",
                    artifact_id="artifact-sha256-image456",
                    repo_shas={"tenant-opw": "abc1234"},
                    deployment_record_id="deployment-1",
                    provenance="ship",
                    minted_at="2026-04-10T18:24:00Z",
                )
            )

            result = runner.invoke(
                main,
                [
                    "release-tuples",
                    "export-catalog",
                    "--state-dir",
                    str(state_dir),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            self.assertIn("[contexts.opw.channels.testing]", result.output)
            self.assertIn('tuple_id = "opw-testing-artifact-sha256-image456"', result.output)
            self.assertIn('tenant-opw = "abc1234"', result.output)

    def test_write_and_read_backup_gate_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = BackupGateRecord(
                record_id="backup-opw-prod-20260410T182231Z",
                context="opw",
                instance="prod",
                created_at="2026-04-10T18:22:31Z",
                source="prod-gate",
                status="pass",
                evidence={
                    "snapshot": "opw-predeploy-20260410-182231",
                    "storage": "pbs",
                },
            )

            written_path = store.write_backup_gate_record(record)
            loaded_record = store.read_backup_gate_record(record.record_id)
            self.assertTrue(written_path.exists())
            self.assertEqual(loaded_record.record_id, record.record_id)
            self.assertEqual(loaded_record.instance, "prod")
            self.assertEqual(loaded_record.evidence["snapshot"], "opw-predeploy-20260410-182231")

    def test_list_backup_gate_records_filters_and_sorts_latest_first(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_backup_gate_record(
                BackupGateRecord(
                    record_id="backup-opw-prod-20260410T182231Z",
                    context="opw",
                    instance="prod",
                    created_at="2026-04-10T18:22:31Z",
                    source="prod-gate",
                    status="pass",
                    evidence={"snapshot": "snap-1"},
                )
            )
            store.write_backup_gate_record(
                BackupGateRecord(
                    record_id="backup-opw-prod-20260411T182231Z",
                    context="opw",
                    instance="prod",
                    created_at="2026-04-11T18:22:31Z",
                    source="prod-gate",
                    status="pass",
                    evidence={"snapshot": "snap-2"},
                )
            )
            store.write_backup_gate_record(
                BackupGateRecord(
                    record_id="backup-opw-staging-20260412T182231Z",
                    context="opw",
                    instance="staging",
                    created_at="2026-04-12T18:22:31Z",
                    source="prod-gate",
                    status="pass",
                    evidence={"snapshot": "snap-3"},
                )
            )

            listed_records = store.list_backup_gate_records(context_name="opw", instance_name="prod", limit=2)

            self.assertEqual([record.record_id for record in listed_records], [
                "backup-opw-prod-20260411T182231Z",
                "backup-opw-prod-20260410T182231Z",
            ])

    def test_write_and_read_promotion_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = PromotionRecord(
                record_id="promotion-20260410-182231-opw-testing-prod",
                artifact_identity={"artifact_id": "artifact-20260410-f45db648"},
                backup_record_id="backup-opw-prod-20260410T182231Z",
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

    def test_list_promotion_records_filters_and_sorts_latest_first(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_promotion_record(
                PromotionRecord(
                    record_id="promotion-20260410T182231Z-opw-testing-to-prod",
                    artifact_identity={"artifact_id": "artifact-1"},
                    backup_record_id="backup-opw-prod-20260410T182231Z",
                    context="opw",
                    from_instance="testing",
                    to_instance="prod",
                    deploy=DeploymentEvidence(
                        target_name="opw-prod",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        status="pass",
                        started_at="2026-04-10T18:22:31Z",
                    ),
                )
            )
            store.write_promotion_record(
                PromotionRecord(
                    record_id="promotion-20260411T182231Z-opw-testing-to-prod",
                    artifact_identity={"artifact_id": "artifact-2"},
                    backup_record_id="backup-opw-prod-20260411T182231Z",
                    context="opw",
                    from_instance="testing",
                    to_instance="prod",
                    deploy=DeploymentEvidence(
                        target_name="opw-prod",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        status="pass",
                        started_at="2026-04-11T18:22:31Z",
                    ),
                )
            )
            store.write_promotion_record(
                PromotionRecord(
                    record_id="promotion-20260412T182231Z-opw-staging-to-prod",
                    artifact_identity={"artifact_id": "artifact-3"},
                    backup_record_id="backup-opw-prod-20260412T182231Z",
                    context="opw",
                    from_instance="staging",
                    to_instance="prod",
                    deploy=DeploymentEvidence(
                        target_name="opw-prod",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        status="pass",
                        started_at="2026-04-12T18:22:31Z",
                    ),
                )
            )
            store.write_promotion_record(
                PromotionRecord(
                    record_id="promotion-20260413T182231Z-opw-testing-to-prod",
                    artifact_identity={"artifact_id": "artifact-4"},
                    backup_record_id="backup-opw-prod-20260413T182231Z",
                    context="opw",
                    from_instance="testing",
                    to_instance="prod",
                    deploy=DeploymentEvidence(target_name="opw-prod", target_type="compose",
                                              deploy_mode="dokploy-compose-api", started_at="2026-04-13T18:22:31Z"),
                )
            )

            listed_records = store.list_promotion_records(
                context_name="opw",
                from_instance_name="testing",
                to_instance_name="prod",
                limit=2,
            )

            self.assertEqual([record.record_id for record in listed_records], [
                "promotion-20260413T182231Z-opw-testing-to-prod",
                "promotion-20260411T182231Z-opw-testing-to-prod",
            ])

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
                resolved_target={
                    "target_type": "compose",
                    "target_id": "compose-123",
                    "target_name": "opw-prod",
                },
                deploy=DeploymentEvidence(
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    deployment_id="delegated-compose-ship",
                    status="pass",
                    started_at="2026-04-10T18:22:31Z",
                    finished_at="2026-04-10T18:24:00Z",
                ),
                post_deploy_update={
                    "attempted": True,
                    "status": "pass",
                    "detail": "Odoo-specific post-deploy update completed through the native control-plane Dokploy schedule workflow.",
                },
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
            self.assertEqual(loaded_record.deploy.deployment_id, "delegated-compose-ship")
            self.assertEqual(loaded_record.post_deploy_update.status, "pass")
            self.assertEqual(loaded_record.destination_health.status, "pass")
            self.assertEqual(loaded_record.resolved_target.target_id, "compose-123")

    def test_list_deployment_records_filters_and_sorts_latest_first(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260410T182231Z-opw-prod",
                    artifact_identity={"artifact_id": "artifact-1"},
                    context="opw",
                    instance="prod",
                    source_git_ref="abc123",
                    deploy=DeploymentEvidence(
                        target_name="opw-prod",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        status="pass",
                        started_at="2026-04-10T18:22:31Z",
                    ),
                )
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260411T182231Z-opw-prod",
                    artifact_identity={"artifact_id": "artifact-2"},
                    context="opw",
                    instance="prod",
                    source_git_ref="def456",
                    deploy=DeploymentEvidence(
                        target_name="opw-prod",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        status="pass",
                        started_at="2026-04-11T18:22:31Z",
                    ),
                )
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260412T182231Z-opw-staging",
                    artifact_identity={"artifact_id": "artifact-3"},
                    context="opw",
                    instance="staging",
                    source_git_ref="ghi789",
                    deploy=DeploymentEvidence(
                        target_name="opw-staging",
                        target_type="compose",
                        deploy_mode="dokploy-compose-api",
                        status="pass",
                        started_at="2026-04-12T18:22:31Z",
                    ),
                )
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-20260413T182231Z-opw-prod",
                    artifact_identity={"artifact_id": "artifact-4"},
                    context="opw",
                    instance="prod",
                    source_git_ref="jkl012",
                    deploy=DeploymentEvidence(target_name="opw-prod", target_type="compose",
                                              deploy_mode="dokploy-compose-api", started_at="2026-04-13T18:22:31Z"),
                )
            )

            listed_records = store.list_deployment_records(context_name="opw", instance_name="prod", limit=2)

            self.assertEqual([record.record_id for record in listed_records], [
                "deployment-20260413T182231Z-opw-prod",
                "deployment-20260411T182231Z-opw-prod",
            ])

    def test_artifacts_ingest_writes_manifest(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "artifact-manifest.json"
            input_file.write_text(
                ArtifactIdentityManifest(
                    artifact_id="artifact-sha256-image456",
                    source_commit="f45db648",
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
                    "ingest",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )

            self.assertEqual(result.exit_code, 0, msg=result.output)
            persisted_manifest = state_dir / "artifacts" / "artifact-sha256-image456.json"
            self.assertTrue(persisted_manifest.exists())

    def test_backup_gates_write_and_show(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            repo_root = Path(temporary_directory_name)
            state_dir = repo_root / "state"
            input_file = repo_root / "backup-gate.json"
            input_file.write_text(
                BackupGateRecord(
                    record_id="backup-opw-prod-20260410T182231Z",
                    context="opw",
                    instance="prod",
                    created_at="2026-04-10T18:22:31Z",
                    source="prod-gate",
                    status="pass",
                    evidence={
                        "snapshot": "opw-predeploy-20260410-182231",
                        "storage": "pbs",
                    },
                ).model_dump_json(indent=2),
                encoding="utf-8",
            )

            write_result = runner.invoke(
                main,
                [
                    "backup-gates",
                    "write",
                    "--state-dir",
                    str(state_dir),
                    "--input-file",
                    str(input_file),
                ],
            )
            show_result = runner.invoke(
                main,
                [
                    "backup-gates",
                    "show",
                    "--state-dir",
                    str(state_dir),
                    "--record-id",
                    "backup-opw-prod-20260410T182231Z",
                ],
            )

            self.assertEqual(write_result.exit_code, 0, msg=write_result.output)
            self.assertEqual(show_result.exit_code, 0, msg=show_result.output)
            self.assertIn('"snapshot": "opw-predeploy-20260410-182231"', show_result.output)

    def test_write_and_read_environment_inventory(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = EnvironmentInventory(
                context="opw",
                instance="prod",
                artifact_identity={"artifact_id": "artifact-20260410-f45db648"},
                source_git_ref="abc123",
                deploy=DeploymentEvidence(
                    target_name="opw-prod",
                    target_type="compose",
                    deploy_mode="dokploy-compose-api",
                    deployment_id="control-plane-dokploy",
                    status="pass",
                    started_at="2026-04-10T18:22:31Z",
                    finished_at="2026-04-10T18:24:00Z",
                ),
                post_deploy_update={
                    "attempted": True,
                    "status": "pass",
                    "detail": "Odoo-specific post-deploy update completed through the native control-plane Dokploy schedule workflow.",
                },
                destination_health={
                    "verified": True,
                    "urls": ["https://prod.example.com/web/health"],
                    "timeout_seconds": 45,
                    "status": "pass",
                },
                updated_at="2026-04-10T18:24:01Z",
                deployment_record_id="deployment-20260410T182231Z-opw-prod",
                promotion_record_id="promotion-20260410T182231Z-opw-testing-to-prod",
                promoted_from_instance="testing",
            )

            written_path = store.write_environment_inventory(record)
            loaded_record = store.read_environment_inventory(context_name="opw", instance_name="prod")
            listed_records = store.list_environment_inventory()

            self.assertTrue(written_path.exists())
            self.assertEqual(loaded_record.context, "opw")
            self.assertEqual(loaded_record.instance, "prod")
            self.assertEqual(loaded_record.promotion_record_id, "promotion-20260410T182231Z-opw-testing-to-prod")
            self.assertEqual(len(listed_records), 1)

    def test_write_and_read_odoo_instance_override_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            state_dir = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=state_dir)
            record = OdooInstanceOverrideRecord(
                context="opw",
                instance="prod",
                config_parameters=(
                    OdooConfigParameterOverride(
                        key="web.base.url",
                        value=OdooOverrideValue(source="literal", value="https://opw-prod.example.com"),
                    ),
                ),
                addon_settings=(
                    OdooAddonSettingOverride(
                        addon="shopify",
                        setting="api_token",
                        value=OdooOverrideValue(
                            source="secret_binding",
                            secret_binding_id="secret-binding-shopify-token",
                        ),
                    ),
                ),
                updated_at="2026-04-21T18:30:00Z",
                source_label="test",
            )

            written_path = store.write_odoo_instance_override_record(record)
            loaded_record = store.read_odoo_instance_override_record(context_name="opw", instance_name="prod")
            listed_records = store.list_odoo_instance_override_records()

            self.assertEqual(written_path.relative_to(state_dir).as_posix(), "odoo_instance_overrides/opw-prod.json")
            self.assertEqual(loaded_record.addon_settings[0].value.secret_binding_id, "secret-binding-shopify-token")
            self.assertEqual([(record.context, record.instance) for record in listed_records], [("opw", "prod")])
