from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from alembic import command
from sqlalchemy import create_engine, text

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.schema_adoption import (
    LEGACY_BASELINE_REVISION,
    LEGACY_CURRENT_SCHEMA_REVISION,
)

from control_plane.storage.schema_invariants import (
    AUTHZ_COMPATIBILITY_FLOOR_REVISION,
    EXPECTED_ALEMBIC_HEAD_REVISION,
)
from control_plane.storage.schema_migration import (
    _alembic_config,
    migrate_schema,
    schema_migration_action,
)


class SchemaMigrationTests(unittest.TestCase):
    def test_monitoring_intent_migration_backfills_and_downgrades_profile_payloads(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            config = _alembic_config(database_url)
            command.upgrade(config, "b3d5f7a9c1e4")
            engine = create_engine(database_url)
            profile_payload = {
                "product": "example-site",
                "display_name": "Example Site",
                "repository": "example/example-site",
                "driver_id": "generic-web",
                "image": {"repository": "ghcr.io/example/example-site"},
                "runtime_port": 3000,
                "health_path": "/healthz",
                "updated_at": "2026-07-27T16:50:00Z",
                "source": "test:migration",
                "lanes": [
                    {
                        "instance": "public",
                        "context": "example-site",
                        "health_monitoring": {
                            "checks": [{"name": "public-ingress", "kind": "public_http"}]
                        },
                    },
                    {
                        "instance": "private",
                        "context": "example-site",
                        "health_monitoring": {
                            "checks": [
                                {
                                    "name": "private-runtime",
                                    "kind": "private_http",
                                    "private_endpoint_key": "example-private",
                                }
                            ]
                        },
                    },
                    {
                        "instance": "prelaunch",
                        "context": "example-site",
                        "health_monitoring": {"checks": []},
                    },
                ],
            }
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "insert into launchplane_product_profiles "
                            "(product, display_name, repository, driver_id, updated_at, payload) "
                            "values (:product, :display_name, :repository, :driver_id, "
                            ":updated_at, :payload)"
                        ),
                        {
                            "product": "example-site",
                            "display_name": "Example Site",
                            "repository": "example/example-site",
                            "driver_id": "generic-web",
                            "updated_at": "2026-07-27T16:50:00Z",
                            "payload": json.dumps(profile_payload),
                        },
                    )
                command.upgrade(config, EXPECTED_ALEMBIC_HEAD_REVISION)
                with engine.connect() as connection:
                    migrated_payload = connection.execute(
                        text(
                            "select payload from launchplane_product_profiles "
                            "where product = 'example-site'"
                        )
                    ).scalar_one()
                migrated = (
                    json.loads(migrated_payload)
                    if isinstance(migrated_payload, str)
                    else migrated_payload
                )
                self.assertEqual(
                    [lane["health_monitoring"]["monitoring_intent"] for lane in migrated["lanes"]],
                    ["public", "private", "prelaunch"],
                )

                command.downgrade(config, "b3d5f7a9c1e4")
                with engine.connect() as connection:
                    downgraded_payload = connection.execute(
                        text(
                            "select payload from launchplane_product_profiles "
                            "where product = 'example-site'"
                        )
                    ).scalar_one()
                downgraded = (
                    json.loads(downgraded_payload)
                    if isinstance(downgraded_payload, str)
                    else downgraded_payload
                )
                self.assertTrue(
                    all(
                        "monitoring_intent" not in lane["health_monitoring"]
                        for lane in downgraded["lanes"]
                    )
                )
            finally:
                engine.dispose()

    def test_compatibility_floor_upgrades_by_default(self) -> None:
        self.assertEqual(
            schema_migration_action(current_revision=AUTHZ_COMPATIBILITY_FLOOR_REVISION),
            "upgrade",
        )

    def test_supported_adoption_revisions_upgrade_to_compatibility_floor(self) -> None:
        for revision in (LEGACY_BASELINE_REVISION, LEGACY_CURRENT_SCHEMA_REVISION):
            with self.subTest(revision=revision):
                self.assertEqual(
                    schema_migration_action(current_revision=revision),
                    "upgrade",
                )

    def test_migration_stamps_and_upgrades_supported_predecessor_schemas(self) -> None:
        for revision in (LEGACY_BASELINE_REVISION, LEGACY_CURRENT_SCHEMA_REVISION):
            with self.subTest(revision=revision):
                engine = MagicMock()
                engine.url.get_backend_name.return_value = "postgresql"
                connection = MagicMock()
                connection.execution_options.return_value = connection
                engine.connect.return_value.__enter__.return_value = connection
                with (
                    patch(
                        "control_plane.storage.schema_migration._build_engine",
                        return_value=engine,
                    ),
                    patch(
                        "control_plane.storage.schema_migration.schema_stamp_revision_for_engine",
                        return_value=revision,
                    ),
                    patch(
                        "control_plane.storage.schema_migration._current_revision",
                        side_effect=(revision, EXPECTED_ALEMBIC_HEAD_REVISION),
                    ),
                    patch("control_plane.storage.schema_migration.command.stamp") as stamp,
                    patch("control_plane.storage.schema_migration.command.upgrade") as upgrade,
                ):
                    migrated_revision = migrate_schema(
                        database_url="postgresql+psycopg://launchplane:test@postgres/launchplane"
                    )

                self.assertEqual(migrated_revision, EXPECTED_ALEMBIC_HEAD_REVISION)
                self.assertEqual(stamp.call_args.args[1], revision)
                self.assertEqual(
                    upgrade.call_args.args[1],
                    EXPECTED_ALEMBIC_HEAD_REVISION,
                )

    def test_fenced_revision_is_current_by_default(self) -> None:
        self.assertEqual(
            schema_migration_action(current_revision=EXPECTED_ALEMBIC_HEAD_REVISION),
            "current",
        )

    def test_fenced_release_upgrades_from_compatibility_floor(self) -> None:
        self.assertEqual(
            schema_migration_action(
                current_revision=AUTHZ_COMPATIBILITY_FLOOR_REVISION,
                target_revision=EXPECTED_ALEMBIC_HEAD_REVISION,
            ),
            "upgrade",
        )

    def test_unknown_revision_fails_closed(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported Launchplane database revision"):
            schema_migration_action(current_revision="unknown")

    def test_alembic_config_accepts_percent_encoded_database_url(self) -> None:
        database_url = "postgresql+psycopg://launchplane:p%40ssword@postgres/launchplane"

        self.assertEqual(
            _alembic_config(database_url).get_main_option("sqlalchemy.url"),
            database_url,
        )

    def test_odoo_stable_lane_migration_rejects_preexisting_cross_kind_queue(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            config = _alembic_config(database_url)
            command.upgrade(config, "a1c3e5f7b9d2")
            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    common_values = {
                        "product": "odoo-tenant-cm",
                        "context": "cm",
                        "instance": "prod",
                        "status": "pending",
                        "phase": "created",
                        "created_at": "2026-07-26T05:00:00Z",
                        "updated_at": "2026-07-26T05:00:00Z",
                        "lease_owner": "",
                        "lease_expires_at": "",
                        "heartbeat_at": "",
                        "attempt": 0,
                        "payload": "{}",
                    }
                    connection.execute(
                        text(
                            "insert into launchplane_odoo_stable_bootstrap_operations "
                            "(operation_id, product, context, instance, idempotency_key, status, "
                            "phase, created_at, updated_at, lease_owner, lease_expires_at, "
                            "heartbeat_at, attempt, payload) values "
                            "(:operation_id, :product, :context, :instance, :idempotency_key, "
                            ":status, :phase, :created_at, :updated_at, :lease_owner, "
                            ":lease_expires_at, :heartbeat_at, :attempt, :payload)"
                        ),
                        {
                            **common_values,
                            "operation_id": "bootstrap-preexisting",
                            "idempotency_key": "bootstrap-preexisting",
                        },
                    )
                    connection.execute(
                        text(
                            "insert into launchplane_odoo_prod_backup_restore_operations "
                            "(operation_id, product, context, instance, idempotency_key, "
                            "idempotency_scope, status, phase, created_at, updated_at, "
                            "lease_owner, lease_expires_at, heartbeat_at, attempt, payload) values "
                            "(:operation_id, :product, :context, :instance, :idempotency_key, "
                            ":idempotency_scope, :status, :phase, :created_at, :updated_at, "
                            ":lease_owner, :lease_expires_at, :heartbeat_at, :attempt, :payload)"
                        ),
                        {
                            **common_values,
                            "operation_id": "restore-preexisting",
                            "idempotency_key": "restore-preexisting",
                            "idempotency_scope": "operator",
                        },
                    )

                with self.assertRaisesRegex(
                    RuntimeError,
                    "multiple blocking operation kinds",
                ):
                    command.upgrade(config, EXPECTED_ALEMBIC_HEAD_REVISION)
            finally:
                engine.dispose()

    def test_sqlite_fenced_schema_allocates_revisions_for_compatibility_writer(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            command.upgrade(_alembic_config(database_url), EXPECTED_ALEMBIC_HEAD_REVISION)
            policy = LaunchplaneAuthzPolicy()
            first_record = LaunchplaneAuthzPolicyRecord(
                record_id="authz-sqlite-first",
                source="test:sqlite-compat",
                updated_at="2026-07-18T00:00:00Z",
                policy=policy,
            )
            second_record = first_record.model_copy(
                update={
                    "record_id": "authz-sqlite-second",
                    "updated_at": "2026-07-18T00:01:00Z",
                }
            )
            store = PostgresRecordStore(database_url=database_url)
            try:
                with store._engine.begin() as connection:
                    for record in (first_record, second_record):
                        payload = record.model_dump(mode="json", exclude_none=True)
                        payload.pop("revision", None)
                        connection.execute(
                            text(
                                "insert into launchplane_authz_policies "
                                "(record_id, status, source, updated_at, policy_sha256, payload) "
                                "values (:record_id, :status, :source, :updated_at, "
                                ":policy_sha256, :payload)"
                            ),
                            {
                                "record_id": record.record_id,
                                "status": record.status,
                                "source": record.source,
                                "updated_at": record.updated_at,
                                "policy_sha256": record.policy_sha256,
                                "payload": json.dumps(payload, separators=(",", ":")),
                            },
                        )
                active_records = store.list_authz_policy_records(status="active")
                with store._engine.connect() as connection:
                    revisions = tuple(
                        connection.execute(
                            text(
                                "select revision from launchplane_authz_policies order by revision"
                            )
                        ).scalars()
                    )
            finally:
                store.close()

        self.assertEqual(revisions, (1, 2))
        self.assertEqual(
            tuple(record.record_id for record in active_records),
            ("authz-sqlite-second",),
        )


if __name__ == "__main__":
    unittest.main()
