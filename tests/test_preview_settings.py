from tempfile import TemporaryDirectory
import unittest

from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.product_config_service import resolve_product_config_profile
from control_plane.runtime_environments import resolve_preview_runtime_environment_from_store
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import _generic_site_profile_payload
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord


class PreviewSettingsTests(unittest.TestCase):
    def test_preview_overrides_template_without_changing_stable_instances(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{directory}/records.db")
            store.ensure_schema()
            self.addCleanup(store.close)
            for instance, values in (
                ("testing", {"COLOR": "testing", "TEMPLATE_ONLY": "kept"}),
                ("prod", {"COLOR": "prod"}),
                ("preview", {"COLOR": "preview", "PREVIEW_ONLY": "isolated"}),
            ):
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context="site",
                        instance=instance,
                        env={key: value for key, value in values.items()},
                        updated_at="2026-10-26T00:00:00Z",
                    )
                )
            before = store.list_runtime_environment_records()
            preview = resolve_preview_runtime_environment_from_store(
                record_store=store,
                context_name="site",
                template_values={"COLOR": "template", "TEMPLATE_ONLY": "kept"},
            )
            self.assertEqual(
                preview.values,
                {
                    "COLOR": "preview",
                    "TEMPLATE_ONLY": "kept",
                    "PREVIEW_ONLY": "isolated",
                },
            )
            self.assertEqual(store.list_runtime_environment_records(), before)

    def test_preview_config_target_is_owned_without_adding_a_stable_lane(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{directory}/records.db")
            store.ensure_schema()
            self.addCleanup(store.close)
            store.write_product_profile_record(profile)
            resolved = resolve_product_config_profile(
                store,
                product=profile.product,
                context=profile.preview.context,
                instance="preview",
            )
            self.assertEqual(resolved, profile)
            self.assertNotIn("preview", [lane.instance for lane in resolved.lanes])

    def test_migration_preserves_values_and_stable_records_then_retires_profile_layer(self) -> None:
        import sqlalchemy as sa
        from control_plane.contracts.product_environment_read_model import (
            build_product_environment_config_status,
            ProductConfigWritePrerequisites,
        )
        from control_plane.storage.preview_setting_migration import migrate_preview_settings
        from control_plane.runtime_environments import resolve_site_runtime_environment_from_store

        profile = LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
        profile.preview.override_env = {"COLOR": "preview", "EXTRA": "kept"}
        with TemporaryDirectory() as directory:
            database_url = f"sqlite+pysqlite:///{directory}/records.db"
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            self.addCleanup(store.close)
            store.write_product_profile_record(profile)
            for instance in ("testing", "prod"):
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context=profile.preview.context,
                        instance=instance,
                        env={"COLOR": instance},
                        updated_at="2026-10-26T00:00:00Z",
                    )
                )
            before = store.list_runtime_environment_records()
            engine = sa.create_engine(database_url)
            self.addCleanup(engine.dispose)
            with engine.begin() as connection:
                migrate_preview_settings(connection, recorded_at="2026-10-26T01:00:00Z")
                migrate_preview_settings(connection, recorded_at="2026-10-26T01:00:00Z")
            migrated = store.read_product_profile_record(profile.product)
            self.assertEqual(migrated.preview.override_env, {})
            for record in before:
                self.assertIn(record, store.list_runtime_environment_records())
                values = resolve_site_runtime_environment_from_store(
                    record_store=store,
                    context_name=record.context,
                    instance_name=record.instance,
                    target_environment={},
                ).values
                self.assertEqual(values, {"COLOR": record.instance})
            config = build_product_environment_config_status(
                record_store=store,
                product=profile.product,
                environment="preview",
                action_allowed=lambda *_args: True,
                write_prerequisites=ProductConfigWritePrerequisites(storage_ready=True),
            )
            self.assertEqual(
                {item.key: item.status for item in config.runtime_settings},
                {
                    "COLOR": "configured",
                    "EXTRA": "configured",
                },
            )
            self.assertTrue(config.write_availability.runtime_settings.apply.enabled)
