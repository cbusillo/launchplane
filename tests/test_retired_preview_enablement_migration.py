from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from sqlalchemy import MetaData, Table, create_engine, inspect, text

from control_plane.storage.migrations.versions import (
    e7c1d5e9a3b6_drop_retired_delivery_tables as held,
    e8d2f6a0b4c7_drop_preview_enablement as migration,
    e9e3a7b1c5d8_join_retirement_paths as retirement_join,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.schema_migration import (
    SCHEMA_MIGRATION_TARGET_REVISION,
    alembic_config,
    schema_migration_action,
    schema_migration_target,
)
from tests.test_retired_delivery_migration import history_snapshot, seed_retired_history


def assert_preview_enablement_drop(test: unittest.TestCase, database_url: str) -> None:
    config = alembic_config(database_url)
    command.upgrade(config, migration.down_revision)
    engine = create_engine(database_url)
    try:
        seed_retired_history(engine)
        with engine.begin() as connection:
            table = Table(migration.RETIRED_TABLE, MetaData(), autoload_with=connection)
            connection.execute(
                table.insert().values(
                    record_id="retired-preview",
                    context="example",
                    anchor_repo="example",
                    anchor_pr_number=7,
                    pr_state="open",
                    updated_at="2026-10-19T00:00:00Z",
                    payload={"record_id": "retired-preview"},
                )
            )
        before_tables = set(inspect(engine).get_table_names())
        preserved_tables = tuple(
            sorted(before_tables - {migration.RETIRED_TABLE, "alembic_version"})
        )
        before = history_snapshot(engine, preserved_tables)
        command.upgrade(config, migration.revision)
        test.assertEqual(
            set(inspect(engine).get_table_names()), before_tables - {migration.RETIRED_TABLE}
        )
        test.assertEqual(history_snapshot(engine, preserved_tables), before)
        store = PostgresRecordStore(database_url=database_url)
        try:
            store.verify_schema()
        finally:
            store.close()
        with test.assertRaises(RuntimeError):
            command.downgrade(config, migration.down_revision)
        with engine.connect() as connection:
            test.assertEqual(
                connection.execute(text("SELECT version_num FROM alembic_version")).scalar_one(),
                migration.revision,
            )
        test.assertEqual(history_snapshot(engine, preserved_tables), before)
    finally:
        engine.dispose()


class RetiredPreviewEnablementMigrationTests(unittest.TestCase):
    def test_release_drop_preserves_every_other_table_including_held_history(self) -> None:
        with TemporaryDirectory() as directory:
            assert_preview_enablement_drop(
                self, f"sqlite+pysqlite:///{Path(directory) / 'records.db'}"
            )

    def test_previously_applied_delivery_drop_can_join_without_replaying_it(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite+pysqlite:///{Path(directory) / 'records.db'}"
            config = alembic_config(database_url)
            command.upgrade(config, held.revision)
            self.assertEqual(schema_migration_action(current_revision=held.revision), "upgrade")
            command.upgrade(
                config,
                schema_migration_target(
                    current_revision=held.revision,
                    target_revision=SCHEMA_MIGRATION_TARGET_REVISION,
                ),
            )
            engine = create_engine(database_url)
            try:
                self.assertNotIn(migration.RETIRED_TABLE, inspect(engine).get_table_names())
                with engine.connect() as connection:
                    self.assertEqual(
                        connection.execute(text("SELECT version_num FROM alembic_version"))
                        .scalars()
                        .all(),
                        [retirement_join.revision],
                    )
                self.assertEqual(
                    schema_migration_action(current_revision=retirement_join.revision),
                    "compatible_ahead",
                )
            finally:
                engine.dispose()

    def test_held_drop_requires_an_explicit_target_after_release_drop(self) -> None:
        self.assertEqual(schema_migration_action(current_revision=migration.revision), "current")
        self.assertEqual(
            schema_migration_action(
                current_revision=migration.revision, target_revision=held.revision
            ),
            "upgrade",
        )
        self.assertEqual(
            schema_migration_target(
                current_revision=migration.revision, target_revision=held.revision
            ),
            retirement_join.revision,
        )
