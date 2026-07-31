from __future__ import annotations

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from alembic import command
from sqlalchemy import create_engine, inspect, text

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.schema_adoption import (
    LEGACY_BASELINE_REVISION,
    LEGACY_CURRENT_SCHEMA_REVISION,
)

from control_plane.storage.schema_invariants import (
    AUTHZ_COMPATIBILITY_FLOOR_REVISION,
    CRITICAL_POSTGRES_COLUMN_TYPES,
    CRITICAL_PRIMARY_KEYS,
    CRITICAL_SCHEMA_INDEXES,
    EXPECTED_ALEMBIC_HEAD_REVISION,
)
from control_plane.storage.schema_migration import (
    _alembic_config,
    migrate_schema,
    schema_migration_action,
)


class SchemaMigrationTests(unittest.TestCase):
    def test_material_incident_migration_links_evidence_without_duplicate_opening(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            config = _alembic_config(database_url)
            command.upgrade(config, "c6f8a0b2d4e6")
            engine = create_engine(database_url)
            observation_id = "public-ingress-example-site-prod-20260727t120000z"
            incident_id = "public-ingress-incident-example-site-prod"
            observation_payload = {
                "schema_version": 1,
                "record_id": observation_id,
                "product": "example-site",
                "repository": "example/example-site",
                "driver_id": "generic-web",
                "context": "example-site",
                "instance": "prod",
                "check_name": "public-ingress",
                "check_kind": "public_http",
                "purpose": "probe",
                "monitoring_intent": "public",
                "observed_at": "2026-07-27T12:00:00Z",
                "status": "fail",
                "failure_code": "http_error",
                "base_url": "https://example.test",
                "health_url": "https://example.test/healthz",
                "targets": [
                    {
                        "target": "base_url",
                        "url": "https://example.test",
                        "status": "fail",
                        "failure_code": "http_error",
                        "http_status": 503,
                        "summary": "HTTP 503",
                    }
                ],
                "summary": "Public ingress failed.",
            }
            incident_payload = {
                "schema_version": 1,
                "incident_id": incident_id,
                "product": "example-site",
                "repository": "example/example-site",
                "driver_id": "generic-web",
                "context": "example-site",
                "instance": "prod",
                "check_name": "public-ingress",
                "check_kind": "public_http",
                "status": "open",
                "opened_at": "2026-07-27T12:00:00Z",
                "opened_observation_id": observation_id,
                "latest_observation_id": observation_id,
                "latest_observed_at": "2026-07-27T12:00:00Z",
                "failure_code": "http_error",
                "summary": "Public ingress failed.",
            }
            policy_payload = {
                "schema_version": 1,
                "policy_id": "example-policy",
                "product": "example-site",
                "context": "example-site",
                "instance": "prod",
                "status": "enabled",
                "destinations": [
                    {
                        "destination_id": "github-main",
                        "kind": "github_issue",
                        "github_repository": "example/launchplane",
                        "github_label": "public-ingress",
                    }
                ],
                "created_at": "2026-07-27T11:00:00Z",
                "updated_at": "2026-07-27T11:00:00Z",
                "source": "test:migration",
            }
            try:
                with engine.begin() as connection:
                    connection.execute(
                        text(
                            "insert into launchplane_public_ingress_observations "
                            "(record_id, product, context, instance, status, observed_at, payload) "
                            "values (:record_id, :product, :context, :instance, :status, "
                            ":observed_at, :payload)"
                        ),
                        {
                            "record_id": observation_id,
                            "product": "example-site",
                            "context": "example-site",
                            "instance": "prod",
                            "status": "fail",
                            "observed_at": "2026-07-27T12:00:00Z",
                            "payload": json.dumps(observation_payload),
                        },
                    )
                    connection.execute(
                        text(
                            "insert into launchplane_public_ingress_incidents "
                            "(incident_id, product, context, instance, status, opened_at, "
                            "latest_observed_at, payload) values (:incident_id, :product, "
                            ":context, :instance, :status, :opened_at, :latest_observed_at, "
                            ":payload)"
                        ),
                        {
                            "incident_id": incident_id,
                            "product": "example-site",
                            "context": "example-site",
                            "instance": "prod",
                            "status": "open",
                            "opened_at": "2026-07-27T12:00:00Z",
                            "latest_observed_at": "2026-07-27T12:00:00Z",
                            "payload": json.dumps(incident_payload),
                        },
                    )
                    connection.execute(
                        text(
                            "insert into launchplane_public_ingress_notification_policies "
                            "(policy_id, product, context, instance, status, updated_at, payload) "
                            "values (:policy_id, :product, :context, :instance, :status, "
                            ":updated_at, :payload)"
                        ),
                        {
                            "policy_id": "example-policy",
                            "product": "example-site",
                            "context": "example-site",
                            "instance": "prod",
                            "status": "enabled",
                            "updated_at": "2026-07-27T11:00:00Z",
                            "payload": json.dumps(policy_payload),
                        },
                    )
                command.upgrade(config, EXPECTED_ALEMBIC_HEAD_REVISION)
                with engine.connect() as connection:
                    observation_row = connection.execute(
                        text(
                            "select incident_id, check_token, check_kind, payload from "
                            "launchplane_public_ingress_observations where record_id = :record_id"
                        ),
                        {"record_id": observation_id},
                    ).one()
                    incident_row = connection.execute(
                        text(
                            "select state_version, check_token, check_kind, payload from "
                            "launchplane_public_ingress_incidents where incident_id = :incident_id"
                        ),
                        {"incident_id": incident_id},
                    ).one()
                    events = connection.execute(
                        text(
                            "select payload from launchplane_public_ingress_incident_events "
                            "where incident_id = :incident_id"
                        ),
                        {"incident_id": incident_id},
                    ).all()
                    policy_value = connection.execute(
                        text(
                            "select payload from launchplane_public_ingress_notification_policies "
                            "where policy_id = 'example-policy'"
                        )
                    ).scalar_one()
                migrated_observation = (
                    json.loads(observation_row.payload)
                    if isinstance(observation_row.payload, str)
                    else observation_row.payload
                )
                migrated_incident = (
                    json.loads(incident_row.payload)
                    if isinstance(incident_row.payload, str)
                    else incident_row.payload
                )
                migrated_event = (
                    json.loads(events[0].payload)
                    if isinstance(events[0].payload, str)
                    else events[0].payload
                )
                migrated_policy = (
                    json.loads(policy_value) if isinstance(policy_value, str) else policy_value
                )
                self.assertEqual(observation_row.incident_id, incident_id)
                self.assertEqual(observation_row.check_token, "")
                self.assertEqual(observation_row.check_kind, "public_http")
                self.assertEqual(migrated_observation["incident_id"], incident_id)
                self.assertEqual(
                    migrated_observation["incident_event_id"],
                    migrated_event["event_id"],
                )
                self.assertTrue(migrated_observation["material_fingerprint_sha256"])
                self.assertEqual(incident_row.state_version, 1)
                self.assertEqual(incident_row.check_token, "")
                self.assertEqual(incident_row.check_kind, "public_http")
                self.assertEqual(migrated_incident["schema_version"], 2)
                self.assertEqual(migrated_incident["latest_material_event"], "baseline")
                self.assertEqual(len(events), 1)
                self.assertFalse(migrated_event["delivery_eligible"])
                self.assertEqual(migrated_policy["reminder_interval_seconds"], 21600)
            finally:
                engine.dispose()

    def test_material_incident_migration_preserves_terminal_history(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            config = _alembic_config(database_url)
            command.upgrade(config, "c6f8a0b2d4e6")
            engine = create_engine(database_url)

            def incident_payload(
                *,
                incident_id: str,
                check_name: str,
                opened_at: str,
                latest_observed_at: str,
                status: str = "open",
                resolution_reason: str = "",
            ) -> dict[str, object]:
                payload: dict[str, object] = {
                    "schema_version": 1,
                    "incident_id": incident_id,
                    "product": "example-site",
                    "repository": "example/example-site",
                    "driver_id": "generic-web",
                    "context": "example-site",
                    "instance": "prod",
                    "check_name": check_name,
                    "check_kind": "public_http",
                    "status": status,
                    "opened_at": opened_at,
                    "opened_observation_id": f"{incident_id}-opened",
                    "latest_observation_id": f"{incident_id}-latest",
                    "latest_observed_at": latest_observed_at,
                    "failure_code": "http_error",
                    "summary": "Public ingress failed.",
                }
                if status == "resolved":
                    payload.update(
                        resolved_at=latest_observed_at,
                        resolved_observation_id=f"{incident_id}-latest",
                        resolution_reason=resolution_reason or "recovered",
                    )
                return payload

            rows = (
                (
                    "resolved-incident",
                    incident_payload(
                        incident_id="resolved-incident",
                        check_name="resolved-check",
                        opened_at="2026-07-27T10:00:00Z",
                        latest_observed_at="2026-07-27T10:10:00Z",
                        status="resolved",
                        resolution_reason="recovered",
                    ),
                ),
                (
                    "duplicate-canonical",
                    incident_payload(
                        incident_id="duplicate-canonical",
                        check_name="duplicate-check",
                        opened_at="2026-07-27T11:00:00Z",
                        latest_observed_at="2026-07-27T11:05:00Z",
                    ),
                ),
                (
                    "duplicate-later",
                    incident_payload(
                        incident_id="duplicate-later",
                        check_name="duplicate-check",
                        opened_at="2026-07-27T11:01:00Z",
                        latest_observed_at="2026-07-27T11:10:00Z",
                    ),
                ),
            )
            try:
                with engine.begin() as connection:
                    for incident_id, payload in rows:
                        connection.execute(
                            text(
                                "insert into launchplane_public_ingress_incidents "
                                "(incident_id, product, context, instance, status, opened_at, "
                                "latest_observed_at, payload) values (:incident_id, :product, "
                                ":context, :instance, :status, :opened_at, :latest_observed_at, "
                                ":payload)"
                            ),
                            {
                                "incident_id": incident_id,
                                "product": "example-site",
                                "context": "example-site",
                                "instance": "prod",
                                "status": payload["status"],
                                "opened_at": payload["opened_at"],
                                "latest_observed_at": payload["latest_observed_at"],
                                "payload": json.dumps(payload),
                            },
                        )
                command.upgrade(config, EXPECTED_ALEMBIC_HEAD_REVISION)
                with engine.connect() as connection:
                    migrated_rows = connection.execute(
                        text(
                            "select incident_id, status, payload from "
                            "launchplane_public_ingress_incidents where incident_id in "
                            "('resolved-incident', 'duplicate-canonical', 'duplicate-later')"
                        )
                    ).all()
                    event_rows = connection.execute(
                        text(
                            "select incident_id, event, payload from "
                            "launchplane_public_ingress_incident_events where incident_id in "
                            "('resolved-incident', 'duplicate-canonical', 'duplicate-later')"
                        )
                    ).all()
                incidents = {
                    row.incident_id: (
                        json.loads(row.payload) if isinstance(row.payload, str) else row.payload
                    )
                    for row in migrated_rows
                }
                events_by_incident: dict[str, list[dict[str, object]]] = {}
                for row in event_rows:
                    events_by_incident.setdefault(row.incident_id, []).append(
                        json.loads(row.payload) if isinstance(row.payload, str) else row.payload
                    )

                self.assertEqual(
                    incidents["resolved-incident"]["latest_material_event"], "resolved"
                )
                resolved_events = events_by_incident["resolved-incident"]
                self.assertEqual(
                    {event["event"] for event in resolved_events}, {"baseline", "resolved"}
                )
                resolved_event = next(
                    event for event in resolved_events if event["event"] == "resolved"
                )
                self.assertFalse(resolved_event["delivery_eligible"])
                self.assertEqual(
                    resolved_event["suppression_reason"],
                    "migration_reconciliation",
                )

                duplicate_statuses = {row.incident_id: row.status for row in migrated_rows}
                self.assertEqual(duplicate_statuses["duplicate-canonical"], "open")
                self.assertEqual(duplicate_statuses["duplicate-later"], "resolved")
                self.assertEqual(
                    incidents["duplicate-later"]["resolution_reason"],
                    "duplicate_reconciled",
                )
                self.assertEqual(
                    incidents["duplicate-later"]["latest_material_event"],
                    "resolved",
                )
                self.assertEqual(
                    {event["event"] for event in events_by_incident["duplicate-later"]},
                    {"baseline", "resolved"},
                )
            finally:
                engine.dispose()

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

    def test_tenant_repository_classification_migration_upgrades_and_downgrades(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            config = _alembic_config(database_url)

            command.upgrade(config, EXPECTED_ALEMBIC_HEAD_REVISION)
            engine = create_engine(database_url)
            try:
                inspector = inspect(engine)
                table_names = set(inspector.get_table_names())
                columns = {
                    column["name"]
                    for column in inspector.get_columns(
                        "launchplane_tenant_repository_classifications"
                    )
                }
                indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes(
                        "launchplane_tenant_repository_classifications"
                    )
                }
                primary_key = inspector.get_pk_constraint(
                    "launchplane_tenant_repository_classifications"
                )
            finally:
                engine.dispose()

            command.downgrade(config, "e8a0c2d4f6b8")
            engine = create_engine(database_url)
            try:
                downgraded_table_names = set(inspect(engine).get_table_names())
            finally:
                engine.dispose()

        self.assertIn("launchplane_tenant_repository_classifications", table_names)
        self.assertGreaterEqual(
            columns,
            {
                "record_id",
                "repository_id",
                "repository_owner_id",
                "repository",
                "product",
                "context",
                "classification_kind",
                "classification_revision",
                "classified_at",
                "classification_digest",
                "payload",
            },
        )
        self.assertEqual(primary_key["constrained_columns"], ["record_id"])
        self.assertTrue(indexes["launchplane_tenant_repo_class_revision_uidx"]["unique"])
        self.assertEqual(
            indexes["launchplane_tenant_repo_class_revision_uidx"]["column_names"],
            ["repository_id", "classification_revision"],
        )
        self.assertFalse(indexes["launchplane_tenant_repo_class_current_idx"]["unique"])
        self.assertEqual(
            indexes["launchplane_tenant_repo_class_current_idx"]["column_names"],
            ["repository_id", "classification_revision"],
        )
        self.assertNotIn(
            "launchplane_tenant_repository_classifications",
            downgraded_table_names,
        )

    def test_tenant_repository_classification_schema_invariants_are_expected(
        self,
    ) -> None:
        column_types = {
            (column.table_name, column.column_name): column.accepted_type_tokens
            for column in CRITICAL_POSTGRES_COLUMN_TYPES
        }
        indexes = {(index.table_name, index.index_name): index for index in CRITICAL_SCHEMA_INDEXES}
        primary_keys = {
            primary_key.table_name: primary_key.column_names
            for primary_key in CRITICAL_PRIMARY_KEYS
        }

        self.assertEqual(
            column_types[("launchplane_tenant_repository_classifications", "payload")],
            ("jsonb",),
        )
        self.assertEqual(
            primary_keys["launchplane_tenant_repository_classifications"],
            ("record_id",),
        )
        self.assertTrue(
            indexes[
                (
                    "launchplane_tenant_repository_classifications",
                    "launchplane_tenant_repo_class_revision_uidx",
                )
            ].unique
        )
        self.assertEqual(
            indexes[
                (
                    "launchplane_tenant_repository_classifications",
                    "launchplane_tenant_repo_class_current_idx",
                )
            ].column_names,
            ("repository_id", "classification_revision"),
        )


if __name__ == "__main__":
    unittest.main()
