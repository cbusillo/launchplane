import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.product_onboarding_manifest import ProductOnboardingManifest
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.product_onboarding import apply_product_onboarding_manifest


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _manifest_payload() -> dict[str, object]:
    return {
        "product": "example-site",
        "display_name": "Example Site",
        "repository": "cbusillo/example-site",
        "driver_id": "generic-web",
        "image_repository": "ghcr.io/cbusillo/example-site",
        "runtime_port": 3000,
        "health_path": "/api/health",
        "lanes": [
            {
                "instance": "testing",
                "context": "example-site-testing",
                "base_url": "https://testing.example.invalid",
            },
            {
                "instance": "prod",
                "context": "example-site-prod",
                "base_url": "https://example.invalid",
                "health_url": "https://example.invalid/status",
            },
        ],
        "preview": {
            "enabled": True,
            "context": "example-site-preview",
            "slug_template": "pr-{number}",
        },
        "dokploy_targets": [
            {
                "context": "example-site-testing",
                "instance": "testing",
                "target_id": "app-testing-123",
                "target_type": "application",
                "target_name": "example-site-testing",
                "domains": ["testing.example.invalid"],
            },
            {
                "context": "example-site-prod",
                "instance": "prod",
                "target_type": "application",
                "target_name": "example-site-prod",
                "domains": ["example.invalid"],
                "require_prod_gate": True,
            },
        ],
        "runtime_environments": [
            {
                "scope": "instance",
                "context": "example-site-testing",
                "instance": "testing",
                "env": {"PUBLIC_BASE_URL": "https://testing.example.invalid"},
            }
        ],
        "secret_bindings": [
            {
                "binding_key": "SMTP_PASSWORD",
                "context": "example-site-prod",
                "instance": "prod",
            }
        ],
        "updated_at": "2026-05-03T01:30:00Z",
        "source_label": "test:onboarding",
    }


class ProductOnboardingTests(unittest.TestCase):
    def test_apply_product_onboarding_manifest_writes_canonical_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            store.ensure_schema()
            manifest = ProductOnboardingManifest.model_validate(_manifest_payload())

            first_result = apply_product_onboarding_manifest(
                record_store=store,
                manifest=manifest,
            )
            second_result = apply_product_onboarding_manifest(
                record_store=store,
                manifest=manifest,
            )

            profile = store.read_product_profile_record("example-site")
            targets = store.list_dokploy_target_records()
            target_ids = store.list_dokploy_target_id_records()
            runtime_records = store.list_runtime_environment_records()
            secret_bindings = store.list_secret_bindings()
            store.close()

        self.assertEqual(first_result.product, "example-site")
        self.assertEqual(second_result.product_profile.updated_at, "2026-05-03T01:30:00Z")
        self.assertEqual(profile.driver_id, "generic-web")
        self.assertEqual(profile.lanes[0].health_url, "https://testing.example.invalid/api/health")
        self.assertEqual(profile.lanes[1].health_url, "https://example.invalid/status")
        self.assertEqual(len(targets), 2)
        self.assertEqual(len(target_ids), 1)
        self.assertEqual(len(runtime_records), 1)
        self.assertEqual(len(secret_bindings), 1)
        self.assertEqual(secret_bindings[0].binding_key, "SMTP_PASSWORD")
        self.assertEqual(secret_bindings[0].status, "disabled")

    def test_product_onboarding_manifest_rejects_unowned_target_route(self) -> None:
        payload = _manifest_payload()
        payload["dokploy_targets"] = [
            {
                "context": "other-product-prod",
                "instance": "prod",
                "target_type": "application",
            }
        ]

        with self.assertRaisesRegex(ValueError, "target must match a stable lane"):
            ProductOnboardingManifest.model_validate(payload)

    def test_product_onboarding_cli_applies_manifest_without_secret_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            temporary_directory = Path(temporary_directory_name)
            database_url = _sqlite_database_url(temporary_directory / "db.sqlite3")
            manifest_path = temporary_directory / "product-onboarding.json"
            manifest_path.write_text(json.dumps(_manifest_payload()))

            result = CliRunner().invoke(
                main,
                [
                    "product-onboarding",
                    "apply",
                    "--database-url",
                    database_url,
                    "--manifest-file",
                    str(manifest_path),
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["product"], "example-site")
        self.assertEqual(payload["secret_binding_count"], 1)
        self.assertNotIn("secret_id", payload["secret_bindings"][0])


if __name__ == "__main__":
    unittest.main()
