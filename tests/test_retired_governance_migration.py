from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from sqlalchemy import BigInteger, Integer, JSON, MetaData, Table, create_engine, inspect, select

from control_plane.storage.migrations.versions import (
    b7e9f1a3c5d8_drop_retired_human_governance as migration,
)
from control_plane.storage.postgres import Base
from control_plane.storage.schema_invariants import EXPECTED_ALEMBIC_HEAD_REVISION
from control_plane.storage.schema_migration import alembic_config

_RETIRED_TABLES = (
    "launchplane_manager_preview_approval_events",
    "launchplane_repository_human_role_policies",
    "launchplane_tenant_technical_human_waiver_events",
)


def assert_retired_governance_drop(test: unittest.TestCase, database_url: str) -> None:
    """Exercise populated upgrade, empty-schema downgrade and re-upgrade on either DB."""
    config = alembic_config(database_url)
    command.upgrade(config, migration.down_revision)
    engine = create_engine(database_url)
    try:
        metadata = MetaData()
        with engine.begin() as connection:
            for table_name in (*_RETIRED_TABLES, "launchplane_artifact_manifests"):
                table = Table(table_name, metadata, autoload_with=connection)
                values: dict[str, object] = {}
                for column in table.columns:
                    if column.nullable:
                        values[column.name] = None
                    elif isinstance(column.type, JSON):
                        values[column.name] = {"retained": "evidence"}
                    elif isinstance(column.type, (Integer, BigInteger)):
                        values[column.name] = 1
                    else:
                        values[column.name] = "sample"
                if "status" in values:
                    values["status"] = "active"
                if "action" in values:
                    values["action"] = "created"
                connection.execute(table.insert().values(**values))
            artifact = metadata.tables["launchplane_artifact_manifests"]
            retained = connection.execute(select(artifact)).all()
        previous_tables = set(inspect(engine).get_table_names())
        command.upgrade(config, EXPECTED_ALEMBIC_HEAD_REVISION)
        test.assertEqual(
            set(inspect(engine).get_table_names()), previous_tables - set(_RETIRED_TABLES)
        )
        with engine.connect() as connection:
            test.assertEqual(connection.execute(select(artifact)).all(), retained)

        command.downgrade(config, migration.down_revision)
        inspector = inspect(engine)
        for table_name in _RETIRED_TABLES:
            test.assertIn(table_name, inspector.get_table_names())
            table = metadata.tables[table_name]
            with engine.connect() as connection:
                test.assertEqual(connection.execute(select(table)).all(), [])
        test.assertTrue(
            next(
                index
                for index in inspector.get_indexes("launchplane_repository_human_role_policies")
                if index["name"] == "launchplane_repo_human_role_active_uidx"
            )["unique"]
        )
        command.upgrade(config, EXPECTED_ALEMBIC_HEAD_REVISION)
        with engine.connect() as connection:
            test.assertEqual(connection.execute(select(artifact)).all(), retained)
    finally:
        engine.dispose()


class RetiredGovernanceMigrationTests(unittest.TestCase):
    def test_populated_upgrade_preserves_other_records_and_downgrade_restores_empty_tables(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            assert_retired_governance_drop(
                self, f"sqlite+pysqlite:///{Path(directory) / 'records.sqlite3'}"
            )

    def test_upgrade_tolerates_previously_removed_retired_tables(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite+pysqlite:///{Path(directory) / 'records.sqlite3'}"
            config = alembic_config(database_url)
            command.upgrade(config, migration.down_revision)
            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    for table_name in _RETIRED_TABLES[:2]:
                        Table(table_name, MetaData(), autoload_with=connection).drop(connection)
                command.upgrade(config, EXPECTED_ALEMBIC_HEAD_REVISION)
                self.assertTrue(set(_RETIRED_TABLES).isdisjoint(inspect(engine).get_table_names()))
            finally:
                engine.dispose()

    def test_fresh_head_matches_current_store_tables(self) -> None:
        with TemporaryDirectory() as directory:
            database_url = f"sqlite+pysqlite:///{Path(directory) / 'records.sqlite3'}"
            command.upgrade(alembic_config(database_url), EXPECTED_ALEMBIC_HEAD_REVISION)
            engine = create_engine(database_url)
            try:
                tables = set(inspect(engine).get_table_names()) - {"alembic_version"}
                self.assertEqual(tables, set(Base.metadata.tables))
                self.assertTrue(set(_RETIRED_TABLES).isdisjoint(tables))
            finally:
                engine.dispose()
