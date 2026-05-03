import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast

from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.secret_record import SecretBinding, SecretRecord, SecretVersion
from control_plane.product_context_cutover import (
    LegacyContextCleanupBoundaryError,
    LegacyContextCleanupRequest,
    ProductContextCutoverRequest,
    apply_legacy_context_cleanup,
    apply_product_context_cutover,
    plan_product_context_cutover,
)
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _payload_section(payload: dict[str, object], key: str) -> dict[str, object]:
    return cast("dict[str, object]", payload[key])


def _payload_counts(payload: dict[str, object]) -> dict[str, dict[str, int]]:
    return cast("dict[str, dict[str, int]]", payload["counts"])


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


class _FakeProductContextCutoverStore:
    def __init__(self) -> None:
        self.profile = LaunchplaneProductProfileRecord(
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
                    base_url="https://syo-testing.example",
                    health_url="https://syo-testing.example/api/health",
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
            source="fake-store",
        )
        self.runtime_records = (
            RuntimeEnvironmentRecord(
                scope="instance",
                context="sellyouroutboard-testing",
                instance="prod",
                env={"CONTACT_EMAIL_MODE": "log"},
                updated_at="2026-05-01T19:15:31Z",
                source_label="fake-store",
            ),
        )
        self.secret_records = (
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
            ),
        )
        self.secret_bindings = (
            SecretBinding(
                binding_id="binding-source",
                secret_id="secret-runtime-environment-smtp-password-sellyouroutboard-testing-prod",
                integration="runtime_environment",
                binding_key="SMTP_PASSWORD",
                context="sellyouroutboard-testing",
                instance="prod",
                created_at="2026-05-01T04:00:00Z",
                updated_at="2026-05-01T04:00:00Z",
            ),
        )

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def write_product_profile_record(self, record: LaunchplaneProductProfileRecord) -> None:
        self.profile = record

    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        if driver_id and driver_id != self.profile.driver_id:
            return ()
        return (self.profile,)

    def list_runtime_environment_records(
        self, *, context_name: str = "", instance_name: str = ""
    ) -> tuple[RuntimeEnvironmentRecord, ...]:
        return tuple(
            record
            for record in self.runtime_records
            if (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        )

    def list_dokploy_target_records(self) -> tuple[DokployTargetRecord, ...]:
        return ()

    def list_dokploy_target_id_records(self) -> tuple[DokployTargetIdRecord, ...]:
        return ()

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretRecord, ...]:
        records = tuple(
            record
            for record in self.secret_records
            if (not integration or record.integration == integration)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        )
        return records[:limit] if limit is not None else records

    def find_secret_record(
        self,
        *,
        scope: str,
        integration: str,
        name: str,
        context: str = "",
        instance: str = "",
    ) -> SecretRecord | None:
        for record in self.secret_records:
            if (
                record.scope == scope
                and record.integration == integration
                and record.name == name
                and record.context == context
                and record.instance == instance
            ):
                return record
        return None

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        bindings = tuple(
            binding
            for binding in self.secret_bindings
            if (not integration or binding.integration == integration)
            and (not context_name or binding.context == context_name)
            and (not instance_name or binding.instance == instance_name)
        )
        return bindings[:limit] if limit is not None else bindings

    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]:
        return ()

    def list_release_tuple_records(self) -> tuple[ReleaseTupleRecord, ...]:
        return ()


class ProductContextCutoverTests(unittest.TestCase):
    def test_dry_run_uses_structural_store_boundary(self) -> None:
        payload = plan_product_context_cutover(
            record_store=_FakeProductContextCutoverStore(),
            request=ProductContextCutoverRequest(
                product="sellyouroutboard",
                source_context="sellyouroutboard-testing",
                target_context="sellyouroutboard",
                display_name="SellYourOutboard",
            ),
        )

        self.assertEqual(payload["mode"], "dry-run")
        profile_payload = _payload_section(payload, "profile")
        counts = _payload_counts(payload)
        self.assertEqual(profile_payload["display_name"], "SellYourOutboard")
        self.assertEqual(profile_payload["preview_context"], "sellyouroutboard")
        self.assertEqual(
            counts["runtime_environment_records"],
            {"created": 1, "skipped": 0},
        )
        self.assertEqual(counts["managed_secret_records"], {"created": 1, "skipped": 0})

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
        profile_payload = _payload_section(payload, "profile")
        counts = _payload_counts(payload)
        self.assertEqual(profile_payload["display_name"], "SellYourOutboard")
        self.assertEqual(profile_payload["preview_context"], "sellyouroutboard")
        self.assertEqual(
            counts["runtime_environment_records"],
            {"created": 2, "skipped": 0},
        )
        self.assertEqual(counts["managed_secret_records"], {"created": 1, "skipped": 0})
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
        self.assertEqual(profile.historical_contexts, ("sellyouroutboard-testing",))
        self.assertEqual(profile.preview.context, "sellyouroutboard")
        self.assertEqual(len(target_runtime_records), 2)
        self.assertEqual(target.target_name, "syo-prod-app")
        self.assertEqual(target_id.target_id, "target-prod-123")
        self.assertEqual(secret.context, "sellyouroutboard")
        self.assertEqual(version.ciphertext, "encrypted-value")
        self.assertEqual([binding.binding_key for binding in bindings], ["SMTP_PASSWORD"])
        self.assertEqual(
            _payload_counts(repeated_payload)["runtime_environment_records"],
            {"created": 0, "skipped": 2},
        )
        self.assertEqual(
            _payload_counts(repeated_payload)["managed_secret_records"],
            {"created": 0, "skipped": 1},
        )
        self.assertEqual(_payload_section(repeated_payload, "profile")["action"], "unchanged")

    def test_apply_preserves_distinct_preview_context_in_history(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "test.sqlite3")
            )
            try:
                store.ensure_schema()
                _seed_syo_source_records(store)
                profile = store.read_product_profile_record("sellyouroutboard")
                store.write_product_profile_record(
                    profile.model_copy(
                        update={
                            "preview": profile.preview.model_copy(
                                update={"context": "sellyouroutboard-preview"}
                            )
                        }
                    )
                )

                apply_product_context_cutover(
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
                updated_profile = store.read_product_profile_record("sellyouroutboard")
            finally:
                store.close()

        self.assertEqual(
            updated_profile.historical_contexts,
            ("sellyouroutboard-testing", "sellyouroutboard-preview"),
        )
        self.assertEqual(updated_profile.preview.context, "sellyouroutboard")

    def test_legacy_cleanup_deletes_lookup_records_and_disables_source_secrets(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "test.sqlite3")
            )
            try:
                store.ensure_schema()
                _seed_syo_source_records(store)
                apply_product_context_cutover(
                    record_store=store,
                    request=ProductContextCutoverRequest(
                        product="sellyouroutboard",
                        source_context="sellyouroutboard-testing",
                        target_context="sellyouroutboard",
                        mode="apply",
                        display_name="SellYourOutboard",
                    ),
                )

                dry_run = apply_legacy_context_cleanup(
                    record_store=store,
                    request=LegacyContextCleanupRequest(
                        product="sellyouroutboard",
                        source_context="sellyouroutboard-testing",
                        target_context="sellyouroutboard",
                    ),
                )
                payload = apply_legacy_context_cleanup(
                    record_store=store,
                    request=LegacyContextCleanupRequest(
                        product="sellyouroutboard",
                        source_context="sellyouroutboard-testing",
                        target_context="sellyouroutboard",
                        mode="apply",
                        actor="test-operator",
                    ),
                )

                source_runtime_records = store.list_runtime_environment_records(
                    context_name="sellyouroutboard-testing"
                )
                target_runtime_records = store.list_runtime_environment_records(
                    context_name="sellyouroutboard"
                )
                source_secrets = store.list_secret_records(
                    context_name="sellyouroutboard-testing",
                    limit=None,
                )
                source_bindings = store.list_secret_bindings(
                    integration="runtime_environment",
                    context_name="sellyouroutboard-testing",
                    instance_name="prod",
                    limit=None,
                )
                target_secret = store.find_secret_record(
                    scope="context_instance",
                    integration="runtime_environment",
                    name="SMTP_PASSWORD",
                    context="sellyouroutboard",
                    instance="prod",
                )
                source_target_records = tuple(
                    record
                    for record in store.list_dokploy_target_records()
                    if record.context == "sellyouroutboard-testing"
                )
                source_target_ids = tuple(
                    record
                    for record in store.list_dokploy_target_id_records()
                    if record.context == "sellyouroutboard-testing"
                )
                target = store.read_dokploy_target_record(
                    context_name="sellyouroutboard",
                    instance_name="prod",
                )
                delete_events = store.list_runtime_environment_delete_events(
                    context_name="sellyouroutboard-testing"
                )
                audit_events = store.list_secret_audit_events(
                    secret_id="secret-runtime-environment-smtp-password-sellyouroutboard-testing-prod"
                )
            finally:
                store.close()

        self.assertEqual(dry_run["mode"], "dry-run")
        self.assertFalse(dry_run["blocked"])
        self.assertEqual(_payload_counts(dry_run)["runtime_environment_records"], {"deleted": 2})
        self.assertEqual(_payload_counts(dry_run)["managed_secret_records"], {"disabled": 1})
        self.assertEqual(payload["mode"], "apply")
        self.assertTrue(payload["applied"])
        self.assertEqual(source_runtime_records, ())
        self.assertEqual(len(target_runtime_records), 2)
        self.assertEqual([secret.status for secret in source_secrets], ["disabled"])
        self.assertEqual([binding.status for binding in source_bindings], ["disabled"])
        self.assertIsNotNone(target_secret)
        assert target_secret is not None
        self.assertEqual(target_secret.status, "configured")
        self.assertEqual(source_target_records, ())
        self.assertEqual(source_target_ids, ())
        self.assertEqual(target.target_name, "syo-prod-app")
        self.assertEqual(len(delete_events), 2)
        self.assertEqual([event.event_type for event in audit_events], ["disabled"])
        self.assertNotIn("encrypted-value", str(payload))

    def test_legacy_cleanup_rejects_source_context_still_owned_by_product(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "test.sqlite3")
            )
            try:
                store.ensure_schema()
                _seed_syo_source_records(store)

                with self.assertRaises(LegacyContextCleanupBoundaryError):
                    apply_legacy_context_cleanup(
                        record_store=store,
                        request=LegacyContextCleanupRequest(
                            product="sellyouroutboard",
                            source_context="sellyouroutboard-testing",
                            target_context="sellyouroutboard",
                        ),
                    )
            finally:
                store.close()


if __name__ == "__main__":
    unittest.main()
