import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_profile_record import ProductImageProfile
from control_plane.contracts.product_profile_record import ProductLaneProfile
from control_plane.contracts.product_profile_record import ProductPreviewProfile
from control_plane.contracts.promotion_record import ArtifactIdentityReference
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding
from control_plane.contracts.secret_record import SecretRecord
from control_plane.contracts.secret_record import SecretVersion
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _product_profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="sellyouroutboard",
        display_name="SellYourOutboard",
        repository="cbusillo/sellyouroutboard",
        driver_id="generic-web",
        image=ProductImageProfile(repository="ghcr.io/cbusillo/sellyouroutboard"),
        runtime_port=3000,
        health_path="/api/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="sellyouroutboard-testing",
                base_url="https://testing.sellyouroutboard.com",
                health_url="https://testing.sellyouroutboard.com/api/health",
            ),
            ProductLaneProfile(
                instance="prod",
                context="sellyouroutboard-testing",
                base_url="https://www.sellyouroutboard.com",
                health_url="https://www.sellyouroutboard.com/api/health",
            ),
        ),
        preview=ProductPreviewProfile(
            enabled=True,
            context="sellyouroutboard-testing",
            slug_template="pr-{number}",
        ),
        updated_at="2026-05-01T00:00:00Z",
        source="test",
    )


def _deployment_record() -> DeploymentRecord:
    return DeploymentRecord(
        record_id="deployment-syo-testing-1",
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-syo-1"),
        context="sellyouroutboard-testing",
        instance="testing",
        source_git_ref="abcdef1",
        resolved_target=ResolvedTargetEvidence(
            target_type="application",
            target_id="app-syo-testing",
            target_name="syo-testing-app",
        ),
        deploy=DeploymentEvidence(
            target_name="syo-testing-app",
            target_type="application",
            deploy_mode="dokploy-application-api",
            deployment_id="dokploy-deployment-1",
            status="pass",
            started_at="2026-05-01T00:01:00Z",
            finished_at="2026-05-01T00:02:00Z",
        ),
    )


def _seed_records(database_url: str) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        store.write_product_profile_record(_product_profile())
        store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="sellyouroutboard-testing",
                instance="prod",
                env={"TAWK_PROPERTY_ID": "property-legacy"},
                updated_at="2026-05-01T00:03:00Z",
                source_label="legacy",
            )
        )
        store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="sellyouroutboard",
                instance="prod",
                env={"TAWK_WIDGET_ID": "widget-canonical"},
                updated_at="2026-05-01T00:04:00Z",
                source_label="operator:mistake",
            )
        )
        store.write_dokploy_target_record(
            DokployTargetRecord(
                context="sellyouroutboard-testing",
                instance="prod",
                target_type="application",
                target_name="syo-prod-app",
                domains=("sellyouroutboard.com",),
                updated_at="2026-05-01T00:05:00Z",
                source_label="test",
            )
        )
        store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="sellyouroutboard-testing",
                instance="prod",
                target_id="target-syo-prod",
                updated_at="2026-05-01T00:05:00Z",
                source_label="test",
            )
        )
        store.write_secret_record(
            SecretRecord(
                secret_id="secret-syo-smtp-password",
                scope="context_instance",
                integration="runtime_environment",
                name="smtp-password",
                context="sellyouroutboard-testing",
                instance="prod",
                current_version_id="secret-version-syo-smtp-password",
                created_at="2026-05-01T00:06:00Z",
                updated_at="2026-05-01T00:06:00Z",
                updated_by="test",
            )
        )
        store.write_secret_version(
            SecretVersion(
                version_id="secret-version-syo-smtp-password",
                secret_id="secret-syo-smtp-password",
                created_at="2026-05-01T00:06:00Z",
                ciphertext="encrypted-smtp-password",
            )
        )
        store.write_secret_binding(
            SecretBinding(
                binding_id="secret-binding-syo-smtp-password",
                secret_id="secret-syo-smtp-password",
                integration="runtime_environment",
                binding_key="SMTP_PASSWORD",
                context="sellyouroutboard-testing",
                instance="prod",
                created_at="2026-05-01T00:06:00Z",
                updated_at="2026-05-01T00:06:00Z",
            )
        )
        store.write_deployment_record(_deployment_record())
    finally:
        store.close()


class ProductProfileContextAuditTests(unittest.TestCase):
    def test_audit_context_cutover_reports_redacted_current_authority(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            _seed_records(database_url)

            result = CliRunner().invoke(
                main,
                [
                    "product-profiles",
                    "audit-context-cutover",
                    "--database-url",
                    database_url,
                    "--product",
                    "sellyouroutboard",
                    "--source-context",
                    "sellyouroutboard-testing",
                    "--target-context",
                    "sellyouroutboard",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("property-legacy", result.output)
        self.assertNotIn("widget-canonical", result.output)
        self.assertNotIn("encrypted-smtp-password", result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["profile"]["summary"]["display_name"], "SellYourOutboard")
        self.assertEqual(
            payload["contexts"]["source"]["runtime_environment_records"][0]["env_keys"],
            ["TAWK_PROPERTY_ID"],
        )
        self.assertEqual(
            payload["contexts"]["target"]["runtime_environment_records"][0]["env_keys"],
            ["TAWK_WIDGET_ID"],
        )
        self.assertEqual(
            payload["contexts"]["source"]["managed_secret_records"][0]["binding_key"],
            "SMTP_PASSWORD",
        )
        self.assertEqual(
            payload["contexts"]["source"]["dokploy_targets"][0]["target_id"],
            "target-syo-prod",
        )
        self.assertEqual(
            payload["contexts"]["source"]["append_only_evidence_counts"]["deployments"],
            1,
        )
        self.assertIn(
            "Stable product profile lanes still reference legacy context",
            "\n".join(payload["warnings"]),
        )
        self.assertIn(
            "do not rewrite those records",
            "\n".join(payload["warnings"]),
        )

    def test_audit_context_cutover_rejects_same_source_and_target(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.close()

            result = CliRunner().invoke(
                main,
                [
                    "product-profiles",
                    "audit-context-cutover",
                    "--database-url",
                    database_url,
                    "--product",
                    "sellyouroutboard",
                    "--source-context",
                    "sellyouroutboard",
                    "--target-context",
                    "sellyouroutboard",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Source and target contexts must differ", result.output)


if __name__ == "__main__":
    unittest.main()
