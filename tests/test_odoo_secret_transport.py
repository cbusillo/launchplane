import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click
from click.testing import CliRunner

from control_plane.cli import main
from control_plane.cli_odoo import _migrate_odoo_override_secret_transport_record
from control_plane.contracts.odoo_instance_override_record import (
    OdooAddonSettingOverride,
    OdooConfigParameterOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideApplyResult,
    OdooOverrideValue,
)
from control_plane.contracts.odoo_website_settings import (
    OdooWebsiteSettings,
    OdooWebsiteSettingsIntent,
)
from control_plane.contracts.secret_record import SecretBinding
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from control_plane.odoo_instance_overrides import config_parameter_secret_env_key
from control_plane.secrets import expected_secret_binding_id


class OdooSecretTransportTests(unittest.TestCase):
    def make_store(self) -> PostgresRecordStore:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{Path(temporary.name) / 'records.sqlite3'}"
        )
        store.ensure_schema()
        return store

    def setUp(self) -> None:
        self.store = self.make_store()
        self.addCleanup(self.store.close)
        self.database_url = self.store.database_url
        self.binding = SecretBinding(
            binding_id="legacy-token",
            secret_id="synthetic-secret",
            integration="runtime_environment",
            binding_key="ENV_OVERRIDE_SHOPIFY__API_TOKEN",
            context="sample",
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        self.store.write_secret_binding(self.binding)
        self.intent = OdooWebsiteSettingsIntent(
            artifact_id="synthetic-artifact",
            settings=OdooWebsiteSettings(
                website_id=1, public_origin="https://sample.invalid", public_indexing_enabled=True
            ),
        )
        self.record = OdooInstanceOverrideRecord(
            context="sample",
            instance="testing",
            addon_settings=(
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="api_token",
                    value=OdooOverrideValue(
                        source="secret_binding", secret_binding_id=self.binding.binding_id
                    ),
                ),
            ),
            website_settings=self.intent,
            last_apply=OdooOverrideApplyResult(
                attempted=True,
                status="fail",
                applied_at="2026-01-02T00:00:00Z",
                website_settings=self.intent,
                website_settings_verified=False,
            ),
            updated_at="2026-01-02T00:00:00Z",
        )
        self.store.write_odoo_instance_override_record(self.record)

    def migrate(
        self, record: OdooInstanceOverrideRecord | None = None
    ) -> OdooInstanceOverrideRecord:
        updated, _ = _migrate_odoo_override_secret_transport_record(
            record_store=self.store,
            record=record or self.record,
            apply_changes=True,
            source_label="synthetic-migration",
        )
        return updated

    def read(self) -> OdooInstanceOverrideRecord:
        return self.store.read_odoo_instance_override_record(
            context_name="sample", instance_name="testing"
        )

    def assert_unmigrated(self) -> None:
        self.assertEqual(self.store.list_secret_bindings(), (self.binding,))
        self.assertEqual(self.store.list_secret_audit_events(secret_id=self.binding.secret_id), ())

    def test_conflicting_preparation_edit_or_apply_result_survives_and_retry_succeeds(self) -> None:
        commit = self.store.write_odoo_secret_transport_migration
        changes = (
            {"website_settings": self.intent.model_copy(update={"artifact_id": "new-artifact"})},
            {
                "config_parameters": (
                    OdooConfigParameterOverride(
                        key="web.base.url",
                        value=OdooOverrideValue(source="literal", value="https://changed.invalid"),
                    ),
                )
            },
            {
                "last_apply": self.record.last_apply.model_copy(
                    update={"status": "pass", "website_settings_verified": True}
                )
            },
        )
        for change in changes:
            with self.subTest(change=next(iter(change))):
                current = self.read()
                edited = current.model_copy(update=change)

                def conflict(
                    bundle: ProductAuthorityBundle,
                    *,
                    expected_records: tuple[OdooInstanceOverrideRecord, ...],
                ) -> None:
                    peer = PostgresRecordStore(database_url=self.database_url)
                    try:
                        peer.write_odoo_instance_override_record(edited, expected_record=current)
                    finally:
                        peer.close()
                    commit(bundle, expected_records=expected_records)

                with patch.object(
                    self.store, "write_odoo_secret_transport_migration", side_effect=conflict
                ):
                    with self.assertRaisesRegex(click.ClickException, "re-run the migration"):
                        self.migrate(current)
                self.assertEqual(self.read(), edited)
                self.assert_unmigrated()
        edited = self.read()
        updated = self.migrate(edited)
        self.assertEqual(updated.website_settings, edited.website_settings)
        self.assertEqual(updated.last_apply, edited.last_apply)
        self.assertEqual(updated.config_parameters, edited.config_parameters)
        self.assertEqual(self.read(), updated)

    def test_failure_after_override_binding_or_audit_write_rolls_back_and_is_retryable(
        self,
    ) -> None:
        for step in (
            "write_odoo_instance_override",
            "write_secret_binding",
            "write_secret_audit_event",
        ):
            with self.subTest(step=step):

                def fail(actual: str) -> None:
                    if actual == step:
                        raise RuntimeError("injected storage failure")

                with patch.object(
                    self.store, "_after_product_authority_bundle_step", side_effect=fail
                ):
                    with self.assertRaisesRegex(RuntimeError, "injected storage failure"):
                        self.migrate()
                self.assertEqual(self.read(), self.record)
                self.assert_unmigrated()
        updated = self.migrate()
        self.assertEqual(self.read(), updated)
        self.assertEqual(updated.website_settings, self.record.website_settings)
        self.assertEqual(updated.last_apply, self.record.last_apply)
        self.assertNotEqual(updated.addon_settings, self.record.addon_settings)

    def test_concurrent_new_consumer_or_binding_edit_refuses_without_disabling_reference(
        self,
    ) -> None:
        commit = self.store.write_odoo_secret_transport_migration
        other = self.record.model_copy(update={"instance": "other"})

        def new_consumer(
            bundle: ProductAuthorityBundle,
            *,
            expected_records: tuple[OdooInstanceOverrideRecord, ...],
        ) -> None:
            self.store.write_odoo_instance_override_record(other, require_absent=True)
            commit(bundle, expected_records=expected_records)

        with patch.object(
            self.store, "write_odoo_secret_transport_migration", side_effect=new_consumer
        ):
            with self.assertRaisesRegex(click.ClickException, "re-run the migration"):
                self.migrate()
        self.assert_unmigrated()
        edited_binding = self.binding.model_copy(update={"status": "disabled"})

        def edit_binding(
            bundle: ProductAuthorityBundle,
            *,
            expected_records: tuple[OdooInstanceOverrideRecord, ...],
        ) -> None:
            self.store.write_secret_binding(edited_binding)
            commit(bundle, expected_records=expected_records)

        with patch.object(
            self.store, "write_odoo_secret_transport_migration", side_effect=edit_binding
        ):
            with self.assertRaisesRegex(click.ClickException, "consumers changed"):
                self.migrate()
        self.assertEqual(self.read(), self.record)
        self.assertEqual(self.store.list_secret_bindings(), (edited_binding,))

    def test_scoped_migration_keeps_shared_legacy_binding_until_last_consumer_moves(self) -> None:
        other = self.record.model_copy(update={"instance": "other"})
        self.store.write_odoo_instance_override_record(other)
        self.migrate()
        bindings = {binding.binding_id: binding for binding in self.store.list_secret_bindings()}
        self.assertEqual(bindings[self.binding.binding_id].status, "configured")
        self.assertEqual(
            self.store.read_odoo_instance_override_record(
                context_name="sample", instance_name="other"
            ),
            other,
        )
        updated = self.migrate(other)
        bindings = {binding.binding_id: binding for binding in self.store.list_secret_bindings()}
        self.assertEqual(bindings[self.binding.binding_id].status, "disabled")
        self.assertEqual(
            bindings[updated.addon_settings[0].value.secret_binding_id].status, "configured"
        )

    def test_legacy_partial_relabel_repairs_pointer_and_rerun_is_idempotent(self) -> None:
        updated = self.migrate()
        self.store.write_odoo_instance_override_record(self.record)
        repaired = self.migrate()
        self.assertEqual(repaired.addon_settings, updated.addon_settings)
        self.assertEqual(repaired.last_apply, self.record.last_apply)
        events = self.store.list_secret_audit_events(secret_id=self.binding.secret_id)
        self.assertEqual(self.migrate(repaired), repaired)
        self.assertEqual(
            self.store.list_secret_audit_events(secret_id=self.binding.secret_id), events
        )

    def test_dry_run_is_read_only_and_reports_only_metadata(self) -> None:
        result = CliRunner().invoke(
            main,
            ["odoo-overrides", "migrate-secret-transport", "--database-url", self.database_url],
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(self.read(), self.record)
        self.assert_unmigrated()
        self.assertNotIn(self.intent.settings.public_origin, result.output)

    def test_late_missing_binding_leaves_earlier_planned_relabel_untouched(self) -> None:
        record = self.record.model_copy(
            update={
                "addon_settings": (
                    *self.record.addon_settings,
                    OdooAddonSettingOverride(
                        addon="shopify",
                        setting="webhook_secret",
                        value=OdooOverrideValue(
                            source="secret_binding", secret_binding_id="missing-binding"
                        ),
                    ),
                )
            }
        )
        self.store.write_odoo_instance_override_record(record)
        with self.assertRaisesRegex(click.ClickException, "missing secret binding"):
            self.migrate(record)
        self.assertEqual(self.read(), record)
        self.assert_unmigrated()

    def test_config_migration_preserves_binding_safety_metadata_and_rejects_destination_conflict(
        self,
    ) -> None:
        binding = self.binding.model_copy(
            update={"instance": "testing", "declared_secret_class": "testing"}
        )
        self.store.write_secret_binding(binding)
        record = self.record.model_copy(
            update={
                "addon_settings": (),
                "config_parameters": (
                    OdooConfigParameterOverride(
                        key="sample.api_token",
                        value=OdooOverrideValue(
                            source="secret_binding", secret_binding_id=binding.binding_id
                        ),
                    ),
                ),
            }
        )
        self.store.write_odoo_instance_override_record(record)
        desired_key = config_parameter_secret_env_key(record.config_parameters[0].key)
        target = binding.model_copy(
            update={
                "binding_id": expected_secret_binding_id(
                    secret_id=binding.secret_id, binding_key=desired_key
                ),
                "binding_key": desired_key,
            }
        )
        self.store.write_secret_binding(target.model_copy(update={"instance": "other"}))
        with self.assertRaisesRegex(click.ClickException, "destination binding conflicts"):
            self.migrate(record)
        self.assertEqual(self.read(), record)
        self.assertEqual(
            {item.binding_id: item for item in self.store.list_secret_bindings()}[
                binding.binding_id
            ],
            binding,
        )
        self.store.write_secret_binding(target)
        updated = self.migrate(record)
        self.assertEqual(updated.config_parameters[0].value.secret_binding_id, target.binding_id)
        self.assertEqual(
            {item.binding_id: item for item in self.store.list_secret_bindings()}[
                target.binding_id
            ],
            target,
        )


if __name__ == "__main__":
    unittest.main()
