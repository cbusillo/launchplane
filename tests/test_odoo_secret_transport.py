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
from control_plane.contracts.secret_record import (
    SecretBinding,
    SecretRecord,
    SecretSharingReason,
    SecretVersion,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.product_authority_bundle import (
    OdooInstanceOverrideConflictError,
    OdooInstanceOverrideWrite,
    ProductAuthorityBundle,
    SecretCopySourceConflictError,
)
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
                    with self.assertRaises(click.ClickException) as error:
                        self.migrate(current)
                self.assertIsInstance(error.exception.__cause__, OdooInstanceOverrideConflictError)
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
                    with self.assertRaises(RuntimeError):
                        self.migrate()
                self.assertEqual(self.read(), self.record)
                self.assert_unmigrated()
        updated = self.migrate()
        self.assertEqual(self.read(), updated)
        self.assertEqual(updated.website_settings, self.record.website_settings)
        self.assertEqual(updated.last_apply, self.record.last_apply)
        self.assertNotEqual(updated.addon_settings, self.record.addon_settings)

    def test_concurrent_boolean_edit_is_not_equal_to_zero_during_migration(self) -> None:
        record = self.record.model_copy(
            update={
                "config_parameters": (
                    OdooConfigParameterOverride(
                        key="sample.enabled", value=OdooOverrideValue(source="literal", value=0)
                    ),
                )
            }
        )
        self.store.write_odoo_instance_override_record(record)
        edited = record.model_copy(
            update={
                "config_parameters": (
                    OdooConfigParameterOverride(
                        key=record.config_parameters[0].key,
                        value=OdooOverrideValue(source="literal", value=False),
                    ),
                )
            }
        )
        commit = self.store.write_odoo_secret_transport_migration

        def conflict(
            bundle: ProductAuthorityBundle,
            *,
            expected_records: tuple[OdooInstanceOverrideRecord, ...],
        ) -> None:
            self.store.write_odoo_instance_override_record(edited, expected_record=record)
            commit(bundle, expected_records=expected_records)

        with patch.object(
            self.store, "write_odoo_secret_transport_migration", side_effect=conflict
        ):
            with self.assertRaises(click.ClickException):
                self.migrate(record)
        self.assertEqual(self.read().model_dump_json(), edited.model_dump_json())
        self.assert_unmigrated()
        with self.assertRaises(click.ClickException):
            self.migrate(record)
        updated = self.migrate(edited)
        self.assertEqual(
            updated.config_parameters[0].model_dump_json(),
            edited.config_parameters[0].model_dump_json(),
        )

    def test_reviewed_store_writes_preserve_scalar_types_on_conflict(self) -> None:
        record = self.record.model_copy(
            update={
                "config_parameters": (
                    OdooConfigParameterOverride(
                        key="sample.enabled", value=OdooOverrideValue(source="literal", value=0)
                    ),
                )
            }
        )
        edited = record.model_copy(
            update={
                "config_parameters": (
                    OdooConfigParameterOverride(
                        key=record.config_parameters[0].key,
                        value=OdooOverrideValue(source="literal", value=False),
                    ),
                )
            }
        )
        filesystem = FilesystemRecordStore(Path(self.enterContext(TemporaryDirectory())))
        stores: tuple[PostgresRecordStore | FilesystemRecordStore, ...] = (self.store, filesystem)
        for store in stores:
            store.write_odoo_instance_override_record(edited)
            for bundled in (False, True):
                with self.subTest(store=type(store).__name__, bundled=bundled):
                    with self.assertRaises(OdooInstanceOverrideConflictError):
                        if bundled:
                            store.write_product_authority_bundle(
                                ProductAuthorityBundle(
                                    odoo_instance_override_writes=(
                                        OdooInstanceOverrideWrite(
                                            record=record, expected_record=record
                                        ),
                                    ),
                                )
                            )
                        else:
                            store.write_odoo_instance_override_record(
                                record, expected_record=record
                            )
                    persisted = store.read_odoo_instance_override_record(
                        context_name=edited.context, instance_name=edited.instance
                    )
                    self.assertEqual(persisted.model_dump_json(), edited.model_dump_json())

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
            with self.assertRaises(click.ClickException) as error:
                self.migrate()
        self.assertIsInstance(error.exception.__cause__, OdooInstanceOverrideConflictError)
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
            with self.assertRaises(click.ClickException) as error:
                self.migrate()
        self.assertIsInstance(error.exception.__cause__, SecretCopySourceConflictError)
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
        version = SecretVersion(
            version_id="synthetic-version",
            secret_id=self.binding.secret_id,
            ciphertext="synthetic-ciphertext-never-for-output",
            created_at=self.binding.created_at,
        )
        self.store.write_secret_record(
            SecretRecord(
                secret_id=self.binding.secret_id,
                scope="context",
                context=self.binding.context,
                integration=self.binding.integration,
                name="synthetic-token",
                current_version_id=version.version_id,
                created_at=self.binding.created_at,
                updated_at=self.binding.updated_at,
            )
        )
        self.store.write_secret_version(version)
        result = CliRunner().invoke(
            main,
            ["odoo-overrides", "migrate-secret-transport", "--database-url", self.database_url],
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(self.read(), self.record)
        self.assert_unmigrated()
        self.assertNotIn(version.ciphertext, result.output)

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
        with self.assertRaises(click.ClickException):
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
        with self.assertRaises(click.ClickException):
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

    def test_old_relabel_missing_safety_metadata_recovers_atomically(self) -> None:
        binding = self.binding.model_copy(
            update={
                "instance": "testing",
                "declared_secret_class": "shared_safe",
                "sharing_reason": SecretSharingReason(
                    kind="site_shared", reason="synthetic sharing", evidence="synthetic fixture"
                ),
            }
        )
        self.store.write_secret_binding(binding)
        updated = self.migrate()
        target_id = updated.addon_settings[0].value.secret_binding_id
        target = next(
            item for item in self.store.list_secret_bindings() if item.binding_id == target_id
        )
        legacy_target = target.model_copy(
            update={"declared_secret_class": None, "sharing_reason": None}
        )
        self.store.write_secret_binding(legacy_target)
        self.store.write_odoo_instance_override_record(self.record)

        def fail(step: str) -> None:
            if step == "write_secret_binding":
                raise RuntimeError("injected storage failure")

        with patch.object(self.store, "_after_product_authority_bundle_step", side_effect=fail):
            with self.assertRaises(RuntimeError):
                self.migrate()
        self.assertEqual(self.read(), self.record)
        self.assertEqual(
            next(
                item for item in self.store.list_secret_bindings() if item.binding_id == target_id
            ),
            legacy_target,
        )
        repaired = self.migrate()
        repaired_binding = next(
            item for item in self.store.list_secret_bindings() if item.binding_id == target_id
        )
        self.assertEqual(repaired_binding.declared_secret_class, binding.declared_secret_class)
        self.assertEqual(repaired_binding.sharing_reason, binding.sharing_reason)
        self.assertEqual(repaired_binding.created_at, legacy_target.created_at)
        self.assertEqual(repaired.addon_settings, updated.addon_settings)

    def test_unchanged_disabled_binding_is_inspectable_without_reenabling_it(self) -> None:
        updated = self.migrate()
        bindings = self.store.list_secret_bindings()
        for binding in bindings:
            self.store.write_secret_binding(binding.model_copy(update={"status": "disabled"}))
        before = self.store.list_secret_bindings()
        result = CliRunner().invoke(
            main,
            ["odoo-overrides", "migrate-secret-transport", "--database-url", self.database_url],
        )
        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(self.migrate(updated), updated)
        self.assertEqual(self.store.list_secret_bindings(), before)

    def test_audit_records_each_destination_when_a_legacy_binding_fans_out(self) -> None:
        record = self.record.model_copy(
            update={
                "addon_settings": (
                    *self.record.addon_settings,
                    self.record.addon_settings[0].model_copy(update={"setting": "webhook_secret"}),
                )
            }
        )
        self.store.write_odoo_instance_override_record(record)
        updated = self.migrate(record)
        target_ids = {override.value.secret_binding_id for override in updated.addon_settings}
        bindings = {binding.binding_id: binding for binding in self.store.list_secret_bindings()}
        events = self.store.list_secret_audit_events(secret_id=self.binding.secret_id)
        self.assertEqual({event.metadata["new_binding_id"] for event in events}, target_ids)
        for event in events:
            self.assertEqual(event.metadata["old_binding_id"], self.binding.binding_id)
            self.assertEqual(event.metadata["old_binding_key"], self.binding.binding_key)
            self.assertEqual(
                event.metadata["new_binding_key"],
                bindings[event.metadata["new_binding_id"]].binding_key,
            )


if __name__ == "__main__":
    unittest.main()
