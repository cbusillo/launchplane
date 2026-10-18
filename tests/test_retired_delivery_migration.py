from __future__ import annotations

import json
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest

from alembic import command
from sqlalchemy import (
    BigInteger,
    Integer,
    JSON,
    MetaData,
    Table,
    create_engine,
    inspect,
    select,
    text,
)
from sqlalchemy.engine import Engine

from control_plane.storage.migrations.versions import (
    e7c1d5e9a3b6_drop_retired_delivery_tables as migration,
)
from control_plane.storage.postgres import Base, PostgresRecordStore
from control_plane.storage.schema_migration import alembic_config


def seed_retired_history(engine: Engine) -> None:
    """Seed old rows through their migrated schemas, without retired producers."""
    with engine.begin() as connection:
        for name in (*migration.RETIRED_TABLES, "launchplane_artifact_manifests"):
            table = Table(name, MetaData(), autoload_with=connection)
            values: dict[str, Any] = {}
            for column in table.columns:
                if column.nullable:
                    values[column.name] = None
                elif isinstance(column.type, JSON):
                    values[column.name] = {"retained": "old evidence", "unicode": "révision"}
                elif isinstance(column.type, (Integer, BigInteger)):
                    values[column.name] = 1
                else:
                    values[column.name] = "sample"
            if name == "launchplane_authorization_bootstrap":
                values.update(record_id="authorization-recovery-bootstrap", status="complete")
            elif name == "launchplane_authorization_recovery_keys":
                values["status"] = "revoked"
            elif name == "launchplane_authorization_recovery_audits":
                values["status"] = "completed"
            elif name == "launchplane_authorization_recovery_challenges":
                values["operation"] = "initial_bootstrap"
            elif name == "launchplane_provider_delivery_inspections":
                values.update(
                    inspection_phase="active",
                    custody_phase="reserved",
                    dispatch_deadline=46,
                    publication_deadline=61,
                )
            if isinstance(values.get("payload"), dict):
                # PostgreSQL's historical identity triggers validate the columns
                # against the raw payload, including nested landing identity.
                values["payload"].update({k: v for k, v in values.items() if k != "payload"})
                values["payload"]["entry"] = {"pull_request_number": 1}
            connection.execute(table.insert().values(**values))


def history_snapshot(engine: Engine, names: tuple[str, ...]) -> str:
    """Canonical comparison of all columns/rows for backup-restoration proof."""
    result: dict[str, list[dict[str, Any]]] = {}
    with engine.connect() as connection:
        for name in sorted(names):
            table = Table(name, MetaData(), autoload_with=connection)
            rows = connection.execute(select(table).order_by(*table.primary_key.columns)).mappings()
            result[name] = [dict(row) for row in rows]
    return json.dumps(result, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def assert_retired_delivery_drop(test: unittest.TestCase, database_url: str) -> None:
    config = alembic_config(database_url)
    command.upgrade(config, migration.down_revision)
    engine = create_engine(database_url)
    try:
        seed_retired_history(engine)
        before_tables = set(inspect(engine).get_table_names())
        preserved_names = ("launchplane_artifact_manifests",)
        preserved = history_snapshot(engine, preserved_names)
        command.upgrade(config, migration.revision)
        test.assertEqual(
            set(inspect(engine).get_table_names()), before_tables - set(migration.RETIRED_TABLES)
        )
        test.assertEqual(history_snapshot(engine, preserved_names), preserved)
        store = PostgresRecordStore(database_url=database_url)
        try:
            store.verify_schema()
            test.assertEqual(store.expire_ordinary_agent_deliveries(), 0)
        finally:
            store.close()
        with test.assertRaises(RuntimeError):
            command.downgrade(config, migration.down_revision)
        with engine.connect() as connection:
            test.assertEqual(
                connection.execute(text("select version_num from alembic_version")).scalar_one(),
                migration.revision,
            )
        test.assertTrue(set(migration.RETIRED_TABLES).isdisjoint(inspect(engine).get_table_names()))
        test.assertEqual(history_snapshot(engine, preserved_names), preserved)
    finally:
        engine.dispose()


class RetiredDeliveryMigrationTests(unittest.TestCase):
    def test_populated_drop_preserves_current_store_and_refuses_lossy_downgrade(self) -> None:
        with TemporaryDirectory() as directory:
            assert_retired_delivery_drop(
                self, f"sqlite+pysqlite:///{Path(directory) / 'records.db'}"
            )

    def test_fresh_head_has_only_supported_tables_and_no_retired_models(self) -> None:
        with TemporaryDirectory() as directory:
            url = f"sqlite+pysqlite:///{Path(directory) / 'records.db'}"
            command.upgrade(alembic_config(url), migration.revision)
            engine = create_engine(url)
            try:
                tables = set(inspect(engine).get_table_names())
                self.assertGreaterEqual(tables, set(Base.metadata.tables))
                self.assertTrue(set(migration.RETIRED_TABLES).isdisjoint(tables))
            finally:
                engine.dispose()
