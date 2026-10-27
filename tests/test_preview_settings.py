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
            store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="context",
                    context="site",
                    env={"COLOR": "context", "CONTEXT_ONLY": "isolated"},
                    updated_at="2026-10-26T00:00:00Z",
                )
            )
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
        from control_plane.contracts.product_profile_record import ProductRuntimeConfigRequirement

        profile.expected_config.runtime_environment_keys = (
            ProductRuntimeConfigRequirement(key="TEMPLATE_ONLY", context=profile.preview.context),
        )
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

    def test_encoded_preview_credentials_are_refused_before_record_writes(self) -> None:
        import base64
        import json
        from control_plane.product_config import apply_product_config_bundle, ProductConfigError

        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{directory}/records.db")
            store.ensure_schema()
            self.addCleanup(store.close)
            for content in (
                {
                    "config_parameters": [
                        {
                            "key": "connector.password",
                            "value": {"source": "literal", "value": "private-fixture"},
                        }
                    ]
                },
                {
                    "addon_settings": [
                        {
                            "addon": "connector",
                            "setting": "api_token",
                            "value": {"source": "literal", "value": "private-fixture"},
                        }
                    ]
                },
            ):
                encoded = base64.b64encode(
                    json.dumps({"context": "site", "instance": "preview", **content}).encode()
                ).decode()
                with self.assertRaises(ProductConfigError) as refused:
                    apply_product_config_bundle(
                        record_store=store,
                        payload={
                            "product": "site",
                            "context": "site",
                            "instance": "preview",
                            "runtime_env": {
                                "env": {"ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64": encoded}
                            },
                        },
                        mode="apply",
                        actor="fixture",
                        source_label="test",
                    )
                self.assertNotIn("private-fixture", str(refused.exception))
            self.assertFalse(store.list_runtime_environment_records())

    def test_migration_conflict_preserves_both_sources(self) -> None:
        import sqlalchemy as sa
        from control_plane.storage.preview_setting_migration import migrate_preview_settings

        with TemporaryDirectory() as directory:
            database_url = f"sqlite+pysqlite:///{directory}/records.db"
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            self.addCleanup(store.close)
            profile = LaunchplaneProductProfileRecord.model_validate(
                _generic_site_profile_payload()
            )
            profile.preview.override_env = {"COLOR": "legacy"}
            store.write_product_profile_record(profile)
            existing = RuntimeEnvironmentRecord(
                scope="instance",
                context=profile.preview.context,
                instance="preview",
                env={"COLOR": "managed"},
                updated_at="2026-10-26T00:00:00Z",
            )
            store.write_runtime_environment_record(existing)
            engine = sa.create_engine(database_url)
            self.addCleanup(engine.dispose)
            with self.assertRaises(ValueError), engine.begin() as connection:
                migrate_preview_settings(connection, recorded_at="2026-10-26T01:00:00Z")
            self.assertEqual(store.read_product_profile_record(profile.product), profile)
            self.assertEqual(store.list_runtime_environment_records(), (existing,))

    def test_preview_boundary_rejects_keyword_credentials_and_nonaddon_payloads(self) -> None:
        import base64
        import json
        from control_plane.preview_settings import validate_preview_scope
        from control_plane.contracts.odoo_post_deploy_payload import (
            ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,
        )

        payload = base64.b64encode(
            json.dumps(
                {
                    "context": "site",
                    "instance": "preview",
                    "config_parameters": [
                        {
                            "key": "shopify.shop_url_key",
                            "value": {"source": "literal", "value": "live-shop"},
                        }
                    ],
                }
            ).encode()
        ).decode()
        for values in (
            {"CONNECTION": "host=x password=private"},
            {ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY: payload},
        ):
            with self.assertRaises(ValueError):
                validate_preview_scope(values, context="site")
        with self.assertRaises(ValueError):
            validate_preview_scope(
                {}, context="site", secret_keys=frozenset({ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY})
            )
        validate_preview_scope({"SMTP_HOST": "mail-sink.invalid"}, context="site")

    def test_historical_invalid_preview_record_returns_a_sanitized_render_refusal(self) -> None:
        import click

        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{directory}/records.db")
            store.ensure_schema()
            self.addCleanup(store.close)
            store.write_runtime_environment_record(
                RuntimeEnvironmentRecord(
                    scope="instance",
                    context="site",
                    instance="preview",
                    env={"CONNECTION": "host=x password=private-fixture"},
                    updated_at="2026-10-26T00:00:00Z",
                )
            )
            with self.assertRaises(click.ClickException) as refusal:
                resolve_preview_runtime_environment_from_store(
                    record_store=store, context_name="site", template_values={}
                )
            self.assertNotIn("private-fixture", str(refusal.exception))
