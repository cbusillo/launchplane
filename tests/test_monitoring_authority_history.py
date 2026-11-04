from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from alembic import command
from sqlalchemy import JSON, MetaData, Table, bindparam, create_engine, inspect, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.migrations import monitoring_authority_v1 as history
from control_plane.storage.migrations.versions import (
    c8d2e4f6a9b1_add_integration_attestations as preceding,
    e2b6c8d0a4f7_monitoring_authority_history as migration,
    e7c1d5e9a3b6_drop_retired_delivery_tables as held,
)
from control_plane.storage.schema_migration import alembic_config, schema_migration_action


class MonitoringAuthorityCaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = self.enterContext(TemporaryDirectory())
        self.url = f"sqlite+pysqlite:///{Path(temporary) / 'history.db'}"
        self.store = PostgresRecordStore(database_url=self.url)
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.engine = create_engine(self.url)
        self.addCleanup(self.engine.dispose)

    @staticmethod
    def write_profile(connection: Connection, product: str, state: str) -> None:
        connection.execute(
            text("""INSERT INTO launchplane_product_profiles
                (product, display_name, repository, driver_id, updated_at, payload)
                VALUES (:product, :product, 'example/site', 'generic-web', '2000-01-01',
                        :payload)
                ON CONFLICT(product) DO UPDATE SET payload = excluded.payload""").bindparams(
                bindparam("payload", type_=JSON)
            ),
            {"product": product, "payload": {"product": product, "lifecycle_state": state}},
        )

    def test_epoch_seeds_inactive_profiles_without_backdating_and_installs_capture_once(
        self,
    ) -> None:
        with self.engine.begin() as connection:
            self.write_profile(connection, "active-site", "active")
            self.write_profile(connection, "retired-site", "retired")
            history.initialize(connection)
            before = (
                connection.execute(text(f"SELECT * FROM {history.TABLE_NAME}")).mappings().all()
            )
            history.initialize(connection)
            after = connection.execute(text(f"SELECT * FROM {history.TABLE_NAME}")).mappings().all()
        self.assertEqual(before, after)
        self.assertEqual({row["product"] for row in before}, {"", "active-site", "retired-site"})
        self.assertEqual({row["operation"] for row in before}, {"epoch", "seed"})
        self.assertTrue(all(row["recorded_at"] > "2000-01-01" for row in before))

    def test_raw_writes_retain_revisions_deletion_and_rollback_atomically(self) -> None:
        with self.engine.begin() as connection:
            history.initialize(connection)
            self.write_profile(connection, "site", "active")
            self.write_profile(connection, "site", "active")
            self.write_profile(connection, "site", "retired")
            connection.execute(
                text("DELETE FROM launchplane_product_profiles WHERE product='site'")
            )
        with self.engine.connect() as connection:
            transaction = connection.begin()
            self.write_profile(connection, "rolled-back", "active")
            transaction.rollback()
            rows = (
                connection.execute(
                    text(
                        f"SELECT * FROM {history.TABLE_NAME} WHERE product != '' ORDER BY sequence"
                    )
                )
                .mappings()
                .all()
            )
        self.assertEqual([row["operation"] for row in rows], ["insert", "update", "delete"])
        self.assertEqual({row["product"] for row in rows}, {"site"})
        self.assertIn("active", str(rows[0]["payload"]))
        self.assertIn("retired", str(rows[1]["payload"]))
        self.assertEqual(rows[1]["payload"], rows[2]["payload"])

    def test_history_cannot_be_overwritten_or_deleted(self) -> None:
        with self.engine.begin() as connection:
            history.initialize(connection)
        for statement in (
            f"DELETE FROM {history.TABLE_NAME}",
            f"UPDATE {history.TABLE_NAME} SET product='forged'",
        ):
            with self.assertRaisesRegex(DBAPIError, "append-only"):
                with self.engine.begin() as connection:
                    connection.execute(text(statement))

    def test_route_and_private_endpoint_changes_share_the_coverage_epoch(self) -> None:
        with self.engine.begin() as connection:
            history.initialize(connection)
            for table, values in (
                (
                    "launchplane_private_health_endpoints",
                    {
                        "endpoint_key": "private-health",
                        "product": "site",
                        "context": "site",
                        "instance": "prod",
                        "url": "http://private.invalid/health",
                        "status": "active",
                        "updated_at": "2000-01-01",
                        "payload": {"endpoint_key": "private-health"},
                    },
                ),
                (
                    "launchplane_route_bindings",
                    {
                        **dict.fromkeys(
                            (
                                "provider_id",
                                "target_category",
                                "ingress_provider",
                                "ingress_endpoint_key",
                                "termination_kind",
                                "tls_owner",
                                "primary_domain",
                                "status",
                                "freshness_status",
                                "updated_at",
                            ),
                            "",
                        ),
                        "product": "site",
                        "context": "site",
                        "instance": "prod",
                        "payload": {},
                    },
                ),
            ):
                source = Table(table, MetaData(), autoload_with=connection)
                connection.execute(source.insert().values(**values))
                connection.execute(source.delete())
            rows = (
                connection.execute(
                    text(
                        f"SELECT * FROM {history.TABLE_NAME} WHERE operation != 'epoch' ORDER BY sequence"
                    )
                )
                .mappings()
                .all()
            )
        self.assertEqual(len(rows), 4)
        self.assertEqual(
            {row["source"] for row in rows}, set(history.SOURCES) - {"launchplane_product_profiles"}
        )
        self.assertEqual({row["context"] for row in rows}, {"site"})


class MonitoringHistoryMigrationTests(unittest.TestCase):
    def test_default_compatibility_target_preserves_history_and_held_tables(self) -> None:
        with TemporaryDirectory() as temporary:
            url = f"sqlite+pysqlite:///{Path(temporary) / 'migration.db'}"
            config = alembic_config(url)
            command.upgrade(config, preceding.revision)
            engine = create_engine(url)
            self.addCleanup(engine.dispose)
            with engine.begin() as connection:
                MonitoringAuthorityCaptureTests.write_profile(connection, "existing", "retired")
            held_before = set(held.RETIRED_TABLES) & set(inspect(engine).get_table_names())
            self.assertTrue(held_before)
            command.upgrade(config, migration.revision)
            self.assertEqual(
                schema_migration_action(current_revision=migration.revision), "compatible_ahead"
            )
            self.assertTrue(held_before <= set(inspect(engine).get_table_names()))
            with engine.connect() as connection:
                operations = (
                    connection.execute(
                        text(f"SELECT operation FROM {history.TABLE_NAME} ORDER BY sequence")
                    )
                    .scalars()
                    .all()
                )
            self.assertEqual(operations, ["epoch", "seed"])
