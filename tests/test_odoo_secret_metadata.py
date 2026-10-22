from pathlib import Path
from datetime import datetime, timedelta
from tempfile import TemporaryDirectory
import unittest
from typing import cast

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.odoo_instance_override_record import (
    OdooAddonSettingOverride,
    OdooInstanceOverrideRecord,
    OdooOverrideValue,
)
from control_plane.contracts.secret_record import (
    SecretAuditEvent,
    SecretBinding,
    SecretRecord,
    SecretSharingReason,
)
from control_plane.odoo_instance_overrides import addon_setting_secret_env_key
from control_plane.odoo_secret_transport import OdooSecretTransportPlan, plan_odoo_secret_transport
from control_plane.secrets import SecretWriteStore, relabel_secret_binding
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import SecretRecordConflictError


class OdooSecretMetadataTests(unittest.TestCase):
    def fixture(
        self, *, sql: bool = True, completed: bool = True
    ) -> tuple[
        PostgresRecordStore | FilesystemRecordStore,
        SecretBinding,
        SecretBinding,
        SecretRecord,
        OdooInstanceOverrideRecord,
    ]:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        root = Path(temporary.name)
        store: PostgresRecordStore | FilesystemRecordStore
        if sql:
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{root / 'records.db'}")
            self.addCleanup(store.close)
            store.ensure_schema()
        else:
            store = FilesystemRecordStore(root)
        source = SecretBinding(
            binding_id="legacy-source",
            secret_id="synthetic",
            integration="runtime_environment",
            binding_key="ENV_OVERRIDE_SHOPIFY__API_TOKEN",
            context="example",
            instance="testing",
            declared_secret_class="shared_safe",
            value_source="supplied",
            sharing_reason=SecretSharingReason(
                kind="dev_store",
                reason="Synthetic test store",
                evidence="Fixture-only verification",
            ),
            created_at="2026-01-01T00:00:00Z",
            updated_at="2026-01-01T00:00:00Z",
        )
        secret = SecretRecord(
            secret_id=source.secret_id,
            scope="context_instance",
            integration=source.integration,
            name="synthetic",
            context=source.context,
            instance=source.instance,
            current_version_id="synthetic-version",
            created_at=source.created_at,
            updated_at=source.updated_at,
        )
        store.write_secret_record(secret)
        store.write_secret_binding(source)
        result = relabel_secret_binding(
            record_store=cast(SecretWriteStore, store),
            binding_id=source.binding_id,
            binding_key=addon_setting_secret_env_key(
                addon_name="shopify", setting_name="api_token"
            ),
            source_label="historical-fixture",
        )
        destination = next(
            item
            for item in store.list_secret_bindings(limit=None)
            if item.binding_id == result["binding_id"]
        )
        destination = destination.model_copy(
            update={
                "declared_secret_class": None,
                "value_source": None,
                "copied_value_source": None,
                "sharing_reason": None,
            }
        )
        store.write_secret_binding(destination)
        record = OdooInstanceOverrideRecord(
            context=source.context,
            instance=source.instance,
            updated_at=source.updated_at,
            addon_settings=(
                OdooAddonSettingOverride(
                    addon="shopify",
                    setting="api_token",
                    value=OdooOverrideValue(
                        source="secret_binding",
                        secret_binding_id=destination.binding_id
                        if completed
                        else source.binding_id,
                    ),
                ),
            ),
        )
        store.write_odoo_instance_override_record(record)
        return store, source, destination, secret, record

    def plan(
        self,
        store: PostgresRecordStore | FilesystemRecordStore,
        record: OdooInstanceOverrideRecord,
        *,
        events: tuple[SecretAuditEvent, ...] | None = None,
        binding_ids: frozenset[str] = frozenset(),
    ) -> OdooSecretTransportPlan:
        return plan_odoo_secret_transport(
            record=record,
            records=store.list_odoo_instance_override_records(),
            bindings=store.list_secret_bindings(limit=None),
            source_label="metadata-fixture",
            repair_metadata=True,
            repair_binding_ids=binding_ids,
            secret_records=store.list_secret_records(limit=None),
            audit_events=events
            if events is not None
            else store.list_secret_audit_events(secret_id="synthetic"),
        )

    def test_completed_historical_relabel_repairs_sql_and_file_metadata_without_values(
        self,
    ) -> None:
        for sql in (True, False):
            with self.subTest(sql=sql):
                store, source, destination, secret, record = self.fixture(sql=sql)
                self.assertIsNone(destination.declared_secret_class)
                self.assertIsNone(destination.sharing_reason)
                self.assertIsNone(destination.value_source)
                before = store.list_secret_bindings(limit=None)
                plan = self.plan(store, record)
                self.assertEqual(
                    store.read_odoo_instance_override_record(
                        context_name=record.context, instance_name=record.instance
                    ),
                    record,
                )
                self.assertEqual(store.list_secret_bindings(limit=None), before)
                self.assertEqual(plan.bundle.secret_versions, ())
                store.write_product_authority_bundle(plan.bundle)
                repaired = next(
                    item
                    for item in store.list_secret_bindings(limit=None)
                    if item.binding_id == destination.binding_id
                )
                self.assertEqual(repaired.declared_secret_class, source.declared_secret_class)
                self.assertEqual(repaired.sharing_reason, source.sharing_reason)
                self.assertEqual(repaired.value_source, source.value_source)
                self.assertEqual(store.read_secret_record(secret.secret_id), secret)
                audit = next(
                    event
                    for event in store.list_secret_audit_events(secret_id=secret.secret_id)
                    if event.metadata.get("action") == "relabel_metadata_repair"
                )
                self.assertEqual(audit.metadata["binding_id"], destination.binding_id)
                self.assertEqual(
                    store.read_odoo_instance_override_record(
                        context_name=record.context, instance_name=record.instance
                    ),
                    record,
                )
                self.assertEqual(self.plan(store, record).bundle.secret_bindings, ())

    def test_historical_copy_repair_preserves_known_and_unknown_origins(self) -> None:
        for sql in (False, True):
            for origin in ("generated", "supplied", None):
                with self.subTest(sql=sql, origin=origin):
                    store, source, destination, _, record = self.fixture(sql=sql)
                    historical = next(
                        row
                        for row in store.list_secret_bindings(limit=None)
                        if row.binding_id == source.binding_id
                    ).model_copy(update={"value_source": "copy", "copied_value_source": origin})
                    store.write_secret_binding(historical)
                    store.write_product_authority_bundle(self.plan(store, record).bundle)
                    repaired = next(
                        row
                        for row in store.list_secret_bindings(limit=None)
                        if row.binding_id == destination.binding_id
                    )
                    self.assertEqual(repaired.value_source, "copy")
                    self.assertEqual(repaired.copied_value_source, origin)
                    self.assertEqual(self.plan(store, record).bundle.secret_bindings, ())

    def test_partial_relabel_repairs_value_origin_before_atomic_reference_transition(self) -> None:
        store, source, destination, _, record = self.fixture(completed=False)
        assert isinstance(store, PostgresRecordStore)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            plan_odoo_secret_transport(
                record=record,
                records=(record,),
                bindings=store.list_secret_bindings(limit=None),
                source_label="fixture",
            )
        plan = self.plan(store, record)
        store.write_odoo_secret_transport_migration(plan.bundle, expected_records=(record,))
        self.assertEqual(
            plan.record.addon_settings[0].value.secret_binding_id, destination.binding_id
        )
        repaired = next(
            item
            for item in store.list_secret_bindings(limit=None)
            if item.binding_id == destination.binding_id
        )
        self.assertEqual(repaired.value_source, source.value_source)

    def test_ambiguous_stale_or_missing_provenance_refuses_without_writes(self) -> None:
        for failure in ("missing", "ambiguous", "rotated", "edited", "scope"):
            with self.subTest(failure=failure):
                store, source, destination, secret, record = self.fixture()
                later = (
                    datetime.fromisoformat(destination.updated_at) + timedelta(seconds=1)
                ).isoformat()
                events = store.list_secret_audit_events(secret_id=secret.secret_id)
                if failure == "missing":
                    events = ()
                elif failure == "ambiguous":
                    events += (events[0].model_copy(update={"event_id": "duplicate"}),)
                elif failure == "rotated":
                    store.write_secret_record(secret.model_copy(update={"updated_at": later}))
                elif failure == "edited":
                    old = next(
                        item
                        for item in store.list_secret_bindings(limit=None)
                        if item.binding_id == source.binding_id
                    )
                    store.write_secret_binding(old.model_copy(update={"updated_at": later}))
                else:
                    store.write_secret_binding(destination.model_copy(update={"context": "other"}))
                before = store.list_secret_bindings(limit=None)
                with self.assertRaises(ValueError) as refused:
                    self.plan(store, record, events=events)
                self.assertIn(destination.binding_id, str(refused.exception))
                self.assertIn("product-config-dry-run/product-config-apply", str(refused.exception))
                self.assertEqual(store.list_secret_bindings(limit=None), before)

    def test_current_explicit_destination_edits_refuse_without_overwriting(
        self,
    ) -> None:
        store, _, destination, _, record = self.fixture()
        explicit = destination.model_copy(
            update={
                "declared_secret_class": "non_prod",
                "updated_at": (
                    datetime.fromisoformat(destination.updated_at) + timedelta(seconds=1)
                ).isoformat(),
            }
        )
        store.write_secret_binding(explicit)
        before = store.list_secret_bindings(limit=None)
        with self.assertRaises(ValueError):
            self.plan(store, record)
        self.assertEqual(store.list_secret_bindings(limit=None), before)

    def test_binding_selector_isolates_recoverable_history_from_unrelated_legacy_binding(
        self,
    ) -> None:
        store, _, destination, _, record = self.fixture()
        legacy = destination.model_copy(
            update={
                "binding_id": "unrelated-legacy",
                "binding_key": addon_setting_secret_env_key(
                    addon_name="other", setting_name="api_token"
                ),
            }
        )
        store.write_secret_binding(legacy)
        record = record.model_copy(
            update={
                "addon_settings": (
                    *record.addon_settings,
                    OdooAddonSettingOverride(
                        addon="other",
                        setting="api_token",
                        value=OdooOverrideValue(
                            source="secret_binding", secret_binding_id=legacy.binding_id
                        ),
                    ),
                )
            }
        )
        store.write_odoo_instance_override_record(record)
        with self.assertRaisesRegex(ValueError, "no retained relabel provenance"):
            self.plan(store, record)
        plan = self.plan(store, record, binding_ids=frozenset({destination.binding_id}))
        self.assertEqual(
            {binding.binding_id for binding in plan.bundle.secret_bindings},
            {destination.binding_id},
        )
        store.write_product_authority_bundle(plan.bundle)
        self.assertEqual(
            next(
                binding
                for binding in store.list_secret_bindings(limit=None)
                if binding.binding_id == legacy.binding_id
            ),
            legacy,
        )
        with self.assertRaisesRegex(ValueError, "referenced destinations"):
            self.plan(store, record, binding_ids=frozenset({"wrong-destination"}))

    def test_concurrent_rotation_refuses_metadata_commit(self) -> None:
        for sql in (True, False):
            with self.subTest(sql=sql):
                store, _, _, secret, record = self.fixture(sql=sql)
                plan = self.plan(store, record)
                before = store.list_secret_bindings(limit=None)
                store.write_secret_record(
                    secret.model_copy(update={"current_version_id": "new-version"})
                )
                with self.assertRaises(SecretRecordConflictError):
                    store.write_product_authority_bundle(plan.bundle)
                self.assertEqual(store.list_secret_bindings(limit=None), before)

    def test_cli_dry_run_and_apply_acknowledgement(self) -> None:
        store, source, destination, _, _ = self.fixture()
        assert isinstance(store, PostgresRecordStore)
        command = [
            "odoo-overrides",
            "migrate-secret-transport",
            "--database-url",
            store.database_url,
            "--context",
            "example",
            "--instance",
            "testing",
            "--repair-metadata-from-relabel-history",
            "--repair-binding-id",
            f" {destination.binding_id} ",
        ]
        runner = CliRunner()
        dry = runner.invoke(main, command)
        self.assertEqual(dry.exit_code, 0, dry.output)
        self.assertIn("metadata_pending", dry.output)
        refused = runner.invoke(main, [*command, "--apply"])
        self.assertNotEqual(refused.exit_code, 0)
        self.assertEqual(
            next(
                item
                for item in store.list_secret_bindings(limit=None)
                if item.binding_id == destination.binding_id
            ),
            destination,
        )
        applied = runner.invoke(main, [*command, "--apply", "--allow-direct-db-mutation"])
        self.assertEqual(applied.exit_code, 0, applied.output)
        self.assertIn("metadata_repaired", applied.output)
        repaired = next(
            item
            for item in store.list_secret_bindings(limit=None)
            if item.binding_id == destination.binding_id
        )
        self.assertEqual(repaired.value_source, source.value_source)
        self.assertEqual(repaired.declared_secret_class, source.declared_secret_class)
        self.assertEqual(repaired.sharing_reason, source.sharing_reason)

    def test_cli_selector_refuses_missing_lane_and_blank_binding_id(self) -> None:
        store, _, destination, _, _ = self.fixture()
        assert isinstance(store, PostgresRecordStore)
        command = [
            "odoo-overrides",
            "migrate-secret-transport",
            "--database-url",
            store.database_url,
            "--context",
            "missing",
            "--instance",
            "testing",
            "--repair-metadata-from-relabel-history",
            "--repair-binding-id",
        ]
        missing = CliRunner().invoke(main, [*command, destination.binding_id])
        self.assertNotEqual(missing.exit_code, 0)
        self.assertIn("no override record", missing.output)
        blank = CliRunner().invoke(main, [*command, " "])
        self.assertNotEqual(blank.exit_code, 0)
        self.assertIn("must not be blank", blank.output)
