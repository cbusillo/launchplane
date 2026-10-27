from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from sqlalchemy import create_engine, inspect
from sqlalchemy.exc import IntegrityError

from control_plane.contracts.preview_execution import odoo_preview_target_key
from control_plane.storage.migrations.versions import (
    b0a2c4e6f8d1_retire_preview_idle_reservations as migration,
    e7c1d5e9a3b6_drop_retired_delivery_tables as held,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.schema_migration import (
    alembic_config,
    migrate_schema,
    schema_migration_target,
    _IDLE_JOIN_BY_SOURCE,
)


def assert_idle_retirement(test: unittest.TestCase, database_url: str, target: str) -> None:
    store = PostgresRecordStore(database_url=database_url)
    test.addCleanup(store.close)
    idle = store.reserve_mutation(
        scope="launchplane:preview-idle:v1",
        route_path="service:preview-idle",
        idempotency_key="sleep",
        request_fingerprint="sleep",
        lease_owner="fixture",
        provider_target_key=odoo_preview_target_key(context="fixture", compose_name="pr-1"),
        reconciliation_key="fixture",
        original_intent={"product": "fixture", "action": "sleep"},
    ).record
    other = store.reserve_mutation(
        scope="fixture:deploy",
        route_path="service:deploy",
        idempotency_key="deploy",
        request_fingerprint="deploy",
        lease_owner="fixture",
        provider_target_key="other-target",
        reconciliation_key="other-target",
        original_intent={"deploy": True},
    ).record
    assert idle is not None and other is not None
    command.upgrade(alembic_config(database_url), target)
    test.assertIsNone(store.read_idempotency_record_by_id(idle.record_id))
    test.assertFalse(store.read_original_mutation_intent(idle))
    test.assertEqual(store.read_idempotency_record_by_id(other.record_id), other)
    test.assertEqual(store.read_original_mutation_intent(other), {"deploy": True})
    with test.assertRaises(IntegrityError):
        store.reserve_mutation(
            scope=idle.scope,
            route_path=idle.route_path,
            idempotency_key="old-scheduler-after-migration",
            request_fingerprint="old-scheduler-after-migration",
            lease_owner="old-scheduler",
            provider_target_key=idle.provider_target_key,
            reconciliation_key=idle.reconciliation_key,
            original_intent={"product": "fixture", "action": "sleep"},
        )
    acquired = store.reserve_mutation(
        scope="fixture:refresh",
        route_path="service:refresh",
        idempotency_key="refresh",
        request_fingerprint="refresh",
        lease_owner="fixture",
        provider_target_key=idle.provider_target_key,
        reconciliation_key=idle.reconciliation_key,
        original_intent={"refresh": True},
    )
    test.assertEqual(acquired.status, "acquired")
    if store.database_dialect_name == "postgresql":
        migrate_schema(database_url=database_url)
    store.verify_schema()


class PreviewIdleMigrationTests(unittest.TestCase):
    def test_cleanup_removes_idle_fences_and_intents_and_preserves_other_operations(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite+pysqlite:///{Path(directory) / 'records.db'}"
            command.upgrade(alembic_config(database_url), migration.down_revision)
            assert_idle_retirement(self, database_url, migration.revision)

    def test_all_existing_branches_clean_idle_history_without_applying_unapproved_branches(
        self,
    ) -> None:
        for source in (migration.down_revision, *_IDLE_JOIN_BY_SOURCE):
            with self.subTest(source=source), TemporaryDirectory() as directory:
                database_url = f"sqlite+pysqlite:///{Path(directory) / 'records.db'}"
                config = alembic_config(database_url)
                command.upgrade(config, source)
                engine = create_engine(database_url)
                self.addCleanup(engine.dispose)
                before = set(inspect(engine).get_table_names()) & set(held.RETIRED_TABLES)
                target = schema_migration_target(
                    current_revision=source, target_revision=migration.revision
                )
                assert_idle_retirement(self, database_url, target)
                after = set(inspect(engine).get_table_names()) & set(held.RETIRED_TABLES)
                self.assertEqual(after, before)
