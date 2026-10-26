from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from sqlalchemy import create_engine, inspect

from control_plane.contracts.preview_execution import odoo_preview_target_key
from control_plane.storage.migrations.versions import (
    b0a2c4e6f8d1_retire_preview_idle_reservations as migration,
    b1b3d5f7a9e2_join_idle_retirement as retirement,
    b2c4e6a8f0d3_join_idle_hostnames as hostnames,
    f87c72d690ab_join_idle_compatibility as compatibility,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.schema_migration import alembic_config, schema_migration_target


class PreviewIdleMigrationTests(unittest.TestCase):
    def test_cleanup_removes_idle_fences_and_intents_and_preserves_other_operations(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite+pysqlite:///{Path(directory) / 'records.db'}"
            config = alembic_config(database_url)
            command.upgrade(config, migration.down_revision)
            store = PostgresRecordStore(database_url=database_url)
            self.addCleanup(store.close)
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
            command.upgrade(config, migration.revision)
            self.assertIsNone(store.read_idempotency_record_by_id(idle.record_id))
            self.assertFalse(store.read_original_mutation_intent(idle))
            self.assertEqual(store.read_idempotency_record_by_id(other.record_id), other)
            self.assertEqual(store.read_original_mutation_intent(other), {"deploy": True})
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
            self.assertEqual(acquired.status, "acquired")

    def test_all_existing_branches_clean_idle_history_without_applying_unapproved_branches(
        self,
    ) -> None:
        for source, target in (
            (migration.down_revision, migration.revision),
            (retirement.down_revision[0], retirement.revision),
            (hostnames.down_revision[0], hostnames.revision),
            (compatibility.down_revision[0], compatibility.revision),
        ):
            with self.subTest(source=source), TemporaryDirectory() as directory:
                database_url = f"sqlite+pysqlite:///{Path(directory) / 'records.db'}"
                config = alembic_config(database_url)
                command.upgrade(config, source)
                engine = create_engine(database_url)
                self.addCleanup(engine.dispose)
                before = set(inspect(engine).get_table_names())
                effective = schema_migration_target(
                    current_revision=source, target_revision=migration.revision
                )
                self.assertEqual(effective, target)
                command.upgrade(config, effective)
                self.assertEqual(set(inspect(engine).get_table_names()), before)
                store = PostgresRecordStore(database_url=database_url)
                try:
                    store.verify_schema()
                finally:
                    store.close()
