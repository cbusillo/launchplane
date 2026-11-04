from pathlib import Path
from tempfile import TemporaryDirectory
import json
import unittest

from sqlalchemy import JSON, MetaData, Table, bindparam, create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.migrations import monitoring_authority_v1 as history


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
            {
                "product": product,
                "payload": {
                    "product": product,
                    "lifecycle_state": state,
                    "display_name": "unrelated private detail",
                    "lanes": [
                        {
                            "context": product,
                            "instance": "prod",
                            "base_url": "https://example.invalid",
                            "health_monitoring": {
                                "monitoring_intent": "public",
                                "checks": [{"name": "health", "kind": "public_http"}],
                            },
                            "odoo_stable_bootstrap": {"confirmation": "unrelated bootstrap detail"},
                        }
                    ],
                },
            },
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
        payloads = [
            json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
            for row in rows
        ]
        self.assertEqual(
            [payload["lifecycle_state"] for payload in payloads], ["active", "retired", "retired"]
        )
        self.assertNotIn("unrelated private detail", str(payloads))
        self.assertNotIn("unrelated bootstrap detail", str(payloads))
        self.assertEqual(
            payloads[0]["lanes"][0]["health_monitoring"]["checks"][0]["name"], "health"
        )
        self.assertEqual(rows[1]["payload"], rows[2]["payload"])

    def test_history_cannot_be_overwritten_or_deleted(self) -> None:
        with self.engine.begin() as connection:
            history.initialize(connection)
        for statement in (
            f"DELETE FROM {history.TABLE_NAME}",
            f"UPDATE {history.TABLE_NAME} SET product='forged'",
        ):
            with self.assertRaises(DBAPIError):
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
