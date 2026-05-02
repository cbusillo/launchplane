import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.product_context_cutover import (
    ProductContextCutoverRequest,
    apply_product_context_cutover,
)
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _seed_syo_source_records(store: PostgresRecordStore) -> None:
    store.write_product_profile_record(
        LaunchplaneProductProfileRecord(
            product="sellyouroutboard",
            display_name="SellYourOutboard.com",
            repository="cbusillo/sellyouroutboard",
            driver_id="generic-web",
            image=ProductImageProfile(repository="ghcr.io/cbusillo/sellyouroutboard"),
            runtime_port=3000,
            health_path="/api/health",
            lanes=(
                ProductLaneProfile(
                    instance="testing",
                    context="sellyouroutboard-testing",
                    base_url="https://syo-testing.shinycomputers.com",
                    health_url="https://syo-testing.shinycomputers.com/api/health",
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
            updated_at="2026-05-01T04:29:07Z",
            source="test",
        )
    )
    store.write_runtime_environment_record(
        RuntimeEnvironmentRecord(
            scope="context",
            context="sellyouroutboard-testing",
            env={"LAUNCHPLANE_PREVIEW_BASE_URL": "https://preview.example"},
            updated_at="2026-05-01T03:46:14Z",
            source_label="test",
        )
    )
    store.write_runtime_environment_record(
        RuntimeEnvironmentRecord(
            scope="instance",
            context="sellyouroutboard-testing",
            instance="prod",
            env={"TAWK_PROPERTY_ID": "property", "CONTACT_EMAIL_MODE": "log"},
            updated_at="2026-05-01T19:15:31Z",
            source_label="test",
        )
    )
    store.write_dokploy_target_record(
        DokployTargetRecord(
            context="sellyouroutboard-testing",
            instance="prod",
            target_type="application",
            project_name="sellyouroutboard",
            target_name="syo-prod-app",
            domains=("https://www.sellyouroutboard.com", "https://sellyouroutboard.com"),
            healthcheck_path="/api/health",
            updated_at="2026-05-01T04:29:07Z",
            source_label="test",
        )
    )
    store.write_dokploy_target_id_record(
        DokployTargetIdRecord(
            context="sellyouroutboard-testing",
            instance="prod",
            target_id="target-prod-123",
            updated_at="2026-05-01T04:29:07Z",
            source_label="test",
        )
    )
    store.write_secret_version(
        SecretVersion(
            version_id="secret-version-source",
            secret_id="secret-runtime-environment-smtp-password-sellyouroutboard-testing-prod",
            created_at="2026-05-01T04:00:00Z",
            ciphertext="encrypted-value",
        )
    )
    store.write_secret_record(
        SecretRecord(
            secret_id="secret-runtime-environment-smtp-password-sellyouroutboard-testing-prod",
            scope="context_instance",
            integration="runtime_environment",
            name="SMTP_PASSWORD",
            context="sellyouroutboard-testing",
            instance="prod",
            current_version_id="secret-version-source",
            created_at="2026-05-01T04:00:00Z",
            updated_at="2026-05-01T04:00:00Z",
        )
    )
    store.write_secret_binding(
        SecretBinding(
            binding_id="binding-source",
            secret_id="secret-runtime-environment-smtp-password-sellyouroutboard-testing-prod",
            integration="runtime_environment",
            binding_key="SMTP_PASSWORD",
            context="sellyouroutboard-testing",
            instance="prod",
            created_at="2026-05-01T04:00:00Z",
            updated_at="2026-05-01T04:00:00Z",
        )
    )


class ProductContextCutoverTests(unittest.TestCase):
    def test_dry_run_reports_redacted_plan_without_writing_target_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "test.sqlite3")
            )
            try:
                store.ensure_schema()
                _seed_syo_source_records(store)

                payload = apply_product_context_cutover(
                    record_store=store,
                    request=ProductContextCutoverRequest(
                        product="sellyouroutboard",
                        source_context="sellyouroutboard-testing",
                        target_context="sellyouroutboard",
                        display_name="SellYourOutboard",
                    ),
                )
                target_runtime_records = store.list_runtime_environment_records(
                    context_name="sellyouroutboard"
                )
            finally:
                store.close()

        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["profile"]["display_name"], "SellYourOutboard")
        self.assertEqual(payload["profile"]["preview_context"], "sellyouroutboard")
        self.assertEqual(
            payload["counts"]["runtime_environment_records"],
            {"created": 2, "skipped": 0},
        )
        self.assertEqual(payload["counts"]["managed_secret_records"], {"created": 1, "skipped": 0})
        self.assertNotIn("encrypted-value", str(payload))
        self.assertEqual(target_runtime_records, ())

    def test_apply_copies_current_authority_records_and_updates_profile(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "test.sqlite3")
            )
            try:
                store.ensure_schema()
                _seed_syo_source_records(store)

                payload = apply_product_context_cutover(
                    record_store=store,
                    request=ProductContextCutoverRequest(
                        product="sellyouroutboard",
                        source_context="sellyouroutboard-testing",
                        target_context="sellyouroutboard",
                        mode="apply",
                        display_name="SellYourOutboard",
                        source_label="test:cutover",
                    ),
                )

                profile = store.read_product_profile_record("sellyouroutboard")
                target_runtime_records = store.list_runtime_environment_records(
                    context_name="sellyouroutboard"
                )
                target = store.read_dokploy_target_record(
                    context_name="sellyouroutboard",
                    instance_name="prod",
                )
                target_id = store.read_dokploy_target_id_record(
                    context_name="sellyouroutboard",
                    instance_name="prod",
                )
                secret = store.find_secret_record(
                    scope="context_instance",
                    integration="runtime_environment",
                    name="SMTP_PASSWORD",
                    context="sellyouroutboard",
                    instance="prod",
                )
                assert secret is not None
                version = store.read_secret_version(secret.current_version_id)
                bindings = store.list_secret_bindings(
                    integration="runtime_environment",
                    context_name="sellyouroutboard",
                    instance_name="prod",
                    limit=None,
                )
                repeated_payload = apply_product_context_cutover(
                    record_store=store,
                    request=ProductContextCutoverRequest(
                        product="sellyouroutboard",
                        source_context="sellyouroutboard-testing",
                        target_context="sellyouroutboard",
                        mode="apply",
                        display_name="SellYourOutboard",
                        source_label="test:cutover",
                    ),
                )
            finally:
                store.close()

        self.assertEqual(payload["mode"], "apply")
        self.assertEqual(profile.display_name, "SellYourOutboard")
        self.assertEqual({lane.context for lane in profile.lanes}, {"sellyouroutboard"})
        self.assertEqual(profile.preview.context, "sellyouroutboard")
        self.assertEqual(len(target_runtime_records), 2)
        self.assertEqual(target.target_name, "syo-prod-app")
        self.assertEqual(target_id.target_id, "target-prod-123")
        self.assertEqual(secret.context, "sellyouroutboard")
        self.assertEqual(version.ciphertext, "encrypted-value")
        self.assertEqual([binding.binding_key for binding in bindings], ["SMTP_PASSWORD"])
        self.assertEqual(
            repeated_payload["counts"]["runtime_environment_records"],
            {"created": 0, "skipped": 2},
        )
        self.assertEqual(
            repeated_payload["counts"]["managed_secret_records"],
            {"created": 0, "skipped": 1},
        )


if __name__ == "__main__":
    unittest.main()
