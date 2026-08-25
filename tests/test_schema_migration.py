from __future__ import annotations

import json
import unittest
from collections.abc import Mapping
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import MagicMock, patch

from alembic import command
from sqlalchemy import create_engine, inspect, text

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.product_owner import (
    ProductOwnerActionContext,
    ProductOwnerActorIdentity,
    ProductOwnerRequirement,
    ProductOwnerRequirementRecord,
)
from control_plane.product_owner_service import evaluate_product_owner_authority
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
    _canonical_predicate_expression,
)
from control_plane.storage.schema_migration import (
    alembic_config,
    migrate_schema,
    schema_migration_action,
)


class SchemaMigrationTests(unittest.TestCase):
    def test_postgres_any_array_predicate_matches_equivalent_in_expression(self) -> None:
        observed = _canonical_predicate_expression(
            "status = ANY (ARRAY['pending'::character varying, 'active'::character varying])"
        )
        expected = _canonical_predicate_expression("status IN ('pending', 'active')")

        self.assertEqual(observed, expected)

    def test_material_incident_migration_links_evidence_without_duplicate_opening(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            config = alembic_config(database_url)
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
            config = alembic_config(database_url)
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
                    for row_incident_id, row_payload in rows:
                        connection.execute(
                            text(
                                "insert into launchplane_public_ingress_incidents "
                                "(incident_id, product, context, instance, status, opened_at, "
                                "latest_observed_at, payload) values (:incident_id, :product, "
                                ":context, :instance, :status, :opened_at, :latest_observed_at, "
                                ":payload)"
                            ),
                            {
                                "incident_id": row_incident_id,
                                "product": "example-site",
                                "context": "example-site",
                                "instance": "prod",
                                "status": row_payload["status"],
                                "opened_at": row_payload["opened_at"],
                                "latest_observed_at": row_payload["latest_observed_at"],
                                "payload": json.dumps(row_payload),
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
            config = alembic_config(database_url)
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
            alembic_config(database_url).get_main_option("sqlalchemy.url"),
            database_url,
        )

    def test_odoo_stable_lane_migration_rejects_preexisting_cross_kind_queue(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            config = alembic_config(database_url)
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
            command.upgrade(alembic_config(database_url), EXPECTED_ALEMBIC_HEAD_REVISION)
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
            config = alembic_config(database_url)

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

    def test_repository_human_admission_migration_upgrades_and_downgrades(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            config = alembic_config(database_url)

            command.upgrade(config, EXPECTED_ALEMBIC_HEAD_REVISION)
            engine = create_engine(database_url)
            try:
                inspector = inspect(engine)
                table_names = set(inspector.get_table_names())
                role_columns = {
                    column["name"]
                    for column in inspector.get_columns(
                        "launchplane_repository_human_role_policies"
                    )
                }
                waiver_columns = {
                    column["name"]
                    for column in inspector.get_columns(
                        "launchplane_tenant_technical_human_waiver_events"
                    )
                }
                role_indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("launchplane_repository_human_role_policies")
                }
                waiver_indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes(
                        "launchplane_tenant_technical_human_waiver_events"
                    )
                }
                role_primary_key = inspector.get_pk_constraint(
                    "launchplane_repository_human_role_policies"
                )
                waiver_primary_key = inspector.get_pk_constraint(
                    "launchplane_tenant_technical_human_waiver_events"
                )
                owner_table_names = (
                    "launchplane_product_owner_policies",
                    "launchplane_product_owner_requirements",
                    "launchplane_product_owner_routing",
                )
                owner_columns: dict[str, set[str]] = {
                    table_name: {
                        str(column["name"]) for column in inspector.get_columns(table_name)
                    }
                    for table_name in owner_table_names
                }
                owner_indexes: dict[str, dict[str, Mapping[str, object]]] = {
                    table_name: {
                        index_name: index
                        for index in inspector.get_indexes(table_name)
                        if isinstance(index_name := index.get("name"), str)
                    }
                    for table_name in owner_table_names
                }
                owner_primary_keys: dict[str, Mapping[str, object]] = {
                    table_name: inspector.get_pk_constraint(table_name)
                    for table_name in (
                        "launchplane_product_owner_policies",
                        "launchplane_product_owner_requirements",
                        "launchplane_product_owner_routing",
                    )
                }
            finally:
                engine.dispose()

            command.downgrade(config, "f9c1d3e5a7b9")
            engine = create_engine(database_url)
            try:
                downgraded_table_names = set(inspect(engine).get_table_names())
            finally:
                engine.dispose()

        self.assertIn("launchplane_repository_human_role_policies", table_names)
        self.assertIn("launchplane_tenant_technical_human_waiver_events", table_names)
        self.assertGreaterEqual(table_names, set(owner_table_names))
        self.assertGreaterEqual(
            role_columns,
            {
                "record_id",
                "repository_id",
                "repository_owner_id",
                "repository",
                "product",
                "context",
                "status",
                "role_policy_revision",
                "supersedes_record_id",
                "role_policy_digest",
                "payload",
            },
        )
        self.assertGreaterEqual(
            waiver_columns,
            {
                "event_id",
                "repository_id",
                "repository_owner_id",
                "repository",
                "product",
                "context",
                "waiver_id",
                "binding_sha256",
                "pull_request_number",
                "head_sha",
                "classification_digest",
                "role_policy_record_id",
                "authz_policy_record_id",
                "action",
                "author_github_id",
                "occurred_at",
                "event_digest",
                "payload",
            },
        )
        self.assertEqual(role_primary_key["constrained_columns"], ["record_id"])
        self.assertEqual(waiver_primary_key["constrained_columns"], ["event_id"])
        self.assertNotIn(
            "enforcement_mode",
            owner_columns["launchplane_product_owner_requirements"],
        )
        self.assertTrue(role_indexes["launchplane_repo_human_role_revision_uidx"]["unique"])
        self.assertTrue(role_indexes["launchplane_repo_human_role_active_uidx"]["unique"])
        self.assertEqual(
            role_indexes["launchplane_repo_human_role_revision_uidx"]["column_names"],
            ["repository_id", "product", "context", "role_policy_revision"],
        )
        self.assertEqual(
            role_indexes["launchplane_repo_human_role_active_uidx"]["column_names"],
            ["repository_id", "product", "context"],
        )
        self.assertEqual(
            waiver_indexes["launchplane_tenant_human_waiver_exact_head_idx"]["column_names"],
            ["repository_id", "pull_request_number", "head_sha", "occurred_at", "event_id"],
        )
        self.assertEqual(
            waiver_indexes["launchplane_tenant_human_waiver_binding_idx"]["column_names"],
            ["binding_sha256", "occurred_at", "event_id"],
        )
        for table_name, revision_column, digest_column, active_index in (
            (
                "launchplane_product_owner_policies",
                "policy_revision",
                "policy_digest",
                "launchplane_product_owner_policy_active_uidx",
            ),
            (
                "launchplane_product_owner_requirements",
                "requirement_revision",
                "requirement_digest",
                "launchplane_product_owner_requirement_active_uidx",
            ),
            (
                "launchplane_product_owner_routing",
                "routing_revision",
                "routing_digest",
                "launchplane_product_owner_routing_active_uidx",
            ),
        ):
            self.assertGreaterEqual(
                owner_columns[table_name],
                {
                    "record_id",
                    "product",
                    "system",
                    "status",
                    revision_column,
                    "effective_at",
                    "source",
                    "supersedes_record_id",
                    digest_column,
                    "payload",
                },
            )
            self.assertEqual(
                owner_primary_keys[table_name]["constrained_columns"],
                ["record_id"],
            )
            self.assertTrue(owner_indexes[table_name][active_index]["unique"])
            self.assertEqual(
                owner_indexes[table_name][active_index]["column_names"],
                ["product", "system"],
            )
        self.assertNotIn(
            "launchplane_repository_human_role_policies",
            downgraded_table_names,
        )
        self.assertNotIn(
            "launchplane_tenant_technical_human_waiver_events",
            downgraded_table_names,
        )
        for table_name in owner_table_names:
            self.assertNotIn(table_name, downgraded_table_names)

    def test_owner_authority_migration_archives_legacy_requirements(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            config = alembic_config(database_url)
            command.upgrade(config, "e9b1d3f5a7c0")
            legacy_payloads = []
            previous_record_id: str | None = None
            for revision, status in ((1, "superseded"), (2, "active")):
                record_id = f"legacy-owner-requirement-r{revision}"
                legacy_payloads.append(
                    {
                        "schema_version": 1,
                        "record_id": record_id,
                        "status": status,
                        "product": "example-site",
                        "system": "web",
                        "requirement_revision": revision,
                        "requirements": [
                            {
                                "schema_version": 1,
                                "action": "pull_request.owner_acceptance",
                                "repository_ids": ["101"],
                                "environments": ["preview"],
                                "quorum": 1,
                            }
                        ],
                        "enforcement_mode": "shadow",
                        "effective_at": f"2026-08-{14 + revision:02d}T12:00:00Z",
                        "source": "test",
                        "reason": "Exercise the authority cutover migration.",
                        "supersedes_record_id": previous_record_id,
                        "requirement_digest": chr(96 + revision) * 64,
                    }
                )
                previous_record_id = record_id
            engine = create_engine(database_url)
            try:
                with engine.begin() as connection:
                    for legacy_payload in legacy_payloads:
                        connection.execute(
                            text(
                                """
                            INSERT INTO launchplane_product_owner_requirements (
                                record_id, product, system, status,
                                requirement_revision, enforcement_mode,
                                effective_at, source, supersedes_record_id,
                                requirement_digest, payload
                            ) VALUES (
                                :record_id, :product, :system, :status,
                                :requirement_revision, 'shadow', :effective_at, 'test',
                                :supersedes_record_id,
                                :requirement_digest, :payload
                            )
                            """
                            ),
                            {
                                "record_id": legacy_payload["record_id"],
                                "product": legacy_payload["product"],
                                "system": legacy_payload["system"],
                                "status": legacy_payload["status"],
                                "requirement_revision": legacy_payload["requirement_revision"],
                                "effective_at": legacy_payload["effective_at"],
                                "supersedes_record_id": legacy_payload["supersedes_record_id"],
                                "requirement_digest": legacy_payload["requirement_digest"],
                                "payload": json.dumps(legacy_payload),
                            },
                        )
            finally:
                engine.dispose()

            command.upgrade(config, "f0a2c4e6b8d1")
            engine = create_engine(database_url)
            try:
                with engine.connect() as connection:
                    archived = tuple(
                        connection.execute(
                            text(
                                """
                            SELECT record_id, requirement_digest, payload
                            FROM launchplane_product_owner_requirement_authority_migrations
                            ORDER BY requirement_revision
                            """
                            )
                        )
                        .mappings()
                        .all()
                    )
                    current = (
                        connection.execute(
                            text(
                                """
                            SELECT requirement_revision, requirement_digest, payload
                            FROM launchplane_product_owner_requirements
                            WHERE product = :product AND system = :system
                            """
                            ),
                            {
                                "product": "example-site",
                                "system": "web",
                            },
                        )
                        .mappings()
                        .one()
                    )
            finally:
                engine.dispose()

            migrated_payload = (
                json.loads(current["payload"])
                if isinstance(current["payload"], str)
                else current["payload"]
            )
            migrated_record = ProductOwnerRequirementRecord.model_validate(migrated_payload)
            store = PostgresRecordStore(database_url=database_url)
            successor = ProductOwnerRequirementRecord(
                product=migrated_record.product,
                system=migrated_record.system,
                requirement_revision=2,
                requirements=(
                    ProductOwnerRequirement(
                        action="pull_request.owner_acceptance",
                        repository_ids=("101",),
                        environments=("preview",),
                    ),
                ),
                effective_at="2026-08-16T13:00:00Z",
                source="test:post-migration-write",
                reason="Prove the reset stream accepts the next supported revision.",
                supersedes_record_id=migrated_record.record_id,
            )
            successor_write = store.compare_and_write_product_owner_requirement_record(
                successor,
                expected_current_record_id=migrated_record.record_id,
                expected_current_requirement_digest=migrated_record.requirement_digest,
            )
            active_after_write = store.list_product_owner_requirement_records(
                product="example-site",
                system="web",
                status="active",
            )

        archived_payloads = tuple(
            json.loads(row["payload"]) if isinstance(row["payload"], str) else row["payload"]
            for row in archived
        )
        current_payload = (
            json.loads(current["payload"])
            if isinstance(current["payload"], str)
            else current["payload"]
        )
        self.assertEqual(
            tuple(row["requirement_digest"] for row in archived),
            ("a" * 64, "b" * 64),
        )
        self.assertEqual(archived_payloads, tuple(legacy_payloads))
        current_record = ProductOwnerRequirementRecord.model_validate(current_payload)
        self.assertEqual(current_record.requirement_revision, 1)
        self.assertEqual(current_record.requirements, ())
        self.assertIsNone(current_record.supersedes_record_id)
        self.assertEqual(current_record.source, "migration:owner-authority-cutover")
        self.assertNotIn(current["requirement_digest"], {"a" * 64, "b" * 64})
        evaluation = evaluate_product_owner_authority(
            context=ProductOwnerActionContext(
                product="example-site",
                system="web",
                repository_id="101",
                environment="preview",
                action="pull_request.owner_acceptance",
            ),
            actor=ProductOwnerActorIdentity(
                provider="github",
                provider_subject_id="1001",
            ),
            policies=(),
            requirements=(current_record,),
            routings=(),
        )
        self.assertEqual(evaluation.decision, "not_required")
        self.assertEqual(evaluation.reason_code, "owner_action_not_required")
        self.assertEqual(successor_write, "written")
        self.assertEqual(len(active_after_write), 1)
        self.assertEqual(active_after_write[0].requirement_revision, 2)
        self.assertEqual(active_after_write[0].supersedes_record_id, current_record.record_id)

    def test_owner_acceptance_migration_upgrades_and_downgrades(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            config = alembic_config(database_url)

            command.upgrade(config, "f3a5c7e9b1d4")
            engine = create_engine(database_url)
            try:
                inspector = inspect(engine)
                table_names = set(inspector.get_table_names())
                columns = {
                    column["name"]
                    for column in inspector.get_columns("launchplane_owner_acceptance_events")
                }
                indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("launchplane_owner_acceptance_events")
                }
                primary_key = inspector.get_pk_constraint("launchplane_owner_acceptance_events")
            finally:
                engine.dispose()

            command.downgrade(config, "f4a6c8e0b2d4")
            engine = create_engine(database_url)
            try:
                downgraded_table_names = set(inspect(engine).get_table_names())
            finally:
                engine.dispose()

        self.assertIn("launchplane_owner_acceptance_events", table_names)
        self.assertGreaterEqual(
            columns,
            {
                "event_id",
                "acceptance_id",
                "binding_sha256",
                "repository_id",
                "repository_owner_id",
                "repository",
                "pr_number",
                "head_sha",
                "tree_sha",
                "product",
                "system",
                "owner_action",
                "environment",
                "action",
                "owner_github_id",
                "owner_login",
                "occurred_at",
                "payload",
            },
        )
        self.assertEqual(primary_key["constrained_columns"], ["event_id"])
        self.assertEqual(
            indexes["launchplane_owner_acceptance_events_subject_idx"]["column_names"],
            [
                "repository_id",
                "pr_number",
                "product",
                "system",
                "owner_action",
                "occurred_at",
            ],
        )
        self.assertNotIn("launchplane_owner_acceptance_events", downgraded_table_names)

    def test_owner_acceptance_subject_sequence_migration_backfills_legacy_order(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            config = alembic_config(database_url)
            command.upgrade(config, "a4c6e8f0b2d5")
            engine = create_engine(database_url)
            legacy_rows = [
                {
                    "event_id": "event-b",
                    "occurred_at": "2026-08-07T13:00:00Z",
                    "product": "example-site",
                },
                {
                    "event_id": "event-a",
                    "occurred_at": "2026-08-07T13:00:00Z",
                    "product": "example-site",
                },
                {
                    "event_id": "event-c",
                    "occurred_at": "2026-08-07T11:00:00Z",
                    "product": "example-site",
                },
                {
                    "event_id": "event-other",
                    "occurred_at": "2026-08-07T14:00:00Z",
                    "product": "example-admin",
                },
            ]
            insert_statement = text(
                """
                INSERT INTO launchplane_owner_acceptance_events (
                    event_id, acceptance_id, binding_sha256, repository_id,
                    repository_owner_id, repository, pr_number, head_sha, tree_sha,
                    product, system, owner_action, environment, action,
                    owner_github_id, owner_login, occurred_at, payload
                ) VALUES (
                    :event_id, :event_id, :binding_sha256, '1001', '2001',
                    'example/example-site', 17, :head_sha, :tree_sha, :product,
                    'web', 'pull_request.owner_acceptance', 'pull_request',
                    'accepted', 101, 'owner', :occurred_at, '{}'
                )
                """
            )
            with engine.begin() as connection:
                connection.execute(
                    insert_statement,
                    [
                        row
                        | {
                            "binding_sha256": str(index + 1) * 64,
                            "head_sha": str(index + 1) * 40,
                            "tree_sha": str(index + 5) * 40,
                        }
                        for index, row in enumerate(legacy_rows)
                    ],
                )
            engine.dispose()

            command.upgrade(config, EXPECTED_ALEMBIC_HEAD_REVISION)
            engine = create_engine(database_url)
            try:
                with engine.connect() as connection:
                    sequences = connection.execute(
                        text(
                            """
                            SELECT event_id, product, subject_sequence
                            FROM launchplane_owner_acceptance_events
                            ORDER BY product, subject_sequence
                            """
                        )
                    ).all()
                    counters = connection.execute(
                        text(
                            """
                            SELECT product, last_sequence
                            FROM launchplane_owner_acceptance_subject_sequences
                            ORDER BY product
                            """
                        )
                    ).all()
                inspector = inspect(engine)
                columns = {
                    column["name"]
                    for column in inspector.get_columns("launchplane_owner_acceptance_events")
                }
                indexes = {
                    index["name"]: index
                    for index in inspector.get_indexes("launchplane_owner_acceptance_events")
                }
                sequence_primary_key = inspector.get_pk_constraint(
                    "launchplane_owner_acceptance_subject_sequences"
                )
            finally:
                engine.dispose()

            command.downgrade(config, "a4c6e8f0b2d5")
            engine = create_engine(database_url)
            try:
                downgraded_inspector = inspect(engine)
                downgraded_columns = {
                    column["name"]
                    for column in downgraded_inspector.get_columns(
                        "launchplane_owner_acceptance_events"
                    )
                }
                downgraded_tables = set(downgraded_inspector.get_table_names())
            finally:
                engine.dispose()

        self.assertIn("subject_sequence", columns)
        self.assertEqual(
            sequences,
            [
                ("event-other", "example-admin", 1),
                ("event-c", "example-site", 1),
                ("event-a", "example-site", 2),
                ("event-b", "example-site", 3),
            ],
        )
        self.assertEqual(counters, [("example-admin", 1), ("example-site", 3)])
        self.assertTrue(
            indexes["launchplane_owner_acceptance_events_subject_sequence_uidx"]["unique"]
        )
        self.assertEqual(
            sequence_primary_key["constrained_columns"],
            [
                "repository_id",
                "pr_number",
                "product",
                "system",
                "owner_action",
                "environment",
            ],
        )
        self.assertNotIn("subject_sequence", downgraded_columns)
        self.assertNotIn(
            "launchplane_owner_acceptance_subject_sequences",
            downgraded_tables,
        )

    def test_owner_review_policy_migration_backfills_explicit_defaults(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            database_url = f"sqlite+pysqlite:///{database_path}"
            config = alembic_config(database_url)
            command.upgrade(config, "b5d7f9a1c3e6")
            engine = create_engine(database_url)
            legacy_payload = {
                "schema_version": 1,
                "product": "example-site",
                "system": "web",
                "policy_revision": 1,
                "owners": [],
                "quorum": 1,
                "status": "active",
                "effective_at": "2026-08-07T00:00:00Z",
                "source": "test",
                "reason": "Legacy Owner policy.",
            }
            with engine.begin() as connection:
                connection.execute(
                    text(
                        """
                        INSERT INTO launchplane_product_owner_policies (
                            record_id, product, system, status, policy_revision, quorum,
                            effective_at, source, supersedes_record_id, policy_digest, payload
                        ) VALUES (
                            'owner-policy-legacy', 'example-site', 'web', 'active', 1, 1,
                            '2026-08-07T00:00:00Z', 'test', NULL, :policy_digest, :payload
                        )
                        """
                    ),
                    {
                        "policy_digest": "a" * 64,
                        "payload": json.dumps(legacy_payload, sort_keys=True),
                    },
                )
            engine.dispose()

            command.upgrade(config, EXPECTED_ALEMBIC_HEAD_REVISION)
            engine = create_engine(database_url)
            try:
                with engine.connect() as connection:
                    payload, policy_digest = connection.execute(
                        text(
                            "SELECT payload, policy_digest "
                            "FROM launchplane_product_owner_policies "
                            "WHERE record_id = 'owner-policy-legacy'"
                        )
                    ).one()
            finally:
                engine.dispose()

        decoded = json.loads(payload) if isinstance(payload, str) else payload
        self.assertEqual(
            decoded["review_age"],
            {
                "elevated_max_age_seconds": 604800,
                "routine_max_age_seconds": 2592000,
                "schema_version": 1,
            },
        )
        self.assertEqual(
            decoded["self_review"],
            {
                "exception_reason": "",
                "exception_revision": 0,
                "routine_exception_enabled": False,
                "schema_version": 1,
            },
        )
        self.assertEqual(
            decoded["preview_trust"],
            {
                "minimum_isolation_class": "synthetic_seeded",
                "schema_version": 1,
            },
        )
        self.assertEqual(policy_digest, "a" * 64)

    def test_policy_schema_invariants_are_expected(self) -> None:
        column_types = {
            (column.table_name, column.column_name): column.accepted_type_tokens
            for column in CRITICAL_POSTGRES_COLUMN_TYPES
        }
        indexes = {(index.table_name, index.index_name): index for index in CRITICAL_SCHEMA_INDEXES}
        primary_keys = {
            primary_key.table_name: primary_key.column_names
            for primary_key in CRITICAL_PRIMARY_KEYS
        }

        self.assertEqual(EXPECTED_ALEMBIC_HEAD_REVISION, "f2239a0b1c2d")
        self.assertNotIn(
            ("launchplane_human_sessions", "launchplane_human_sessions_github_id_idx"),
            indexes,
        )
        self.assertEqual(
            column_types[("launchplane_privileged_operations", "payload")],
            ("jsonb",),
        )
        self.assertEqual(
            column_types[("launchplane_privileged_operation_events", "payload")],
            ("jsonb",),
        )
        self.assertEqual(
            primary_keys["launchplane_privileged_operations"],
            ("operation_id",),
        )
        self.assertEqual(
            primary_keys["launchplane_privileged_operation_events"],
            ("event_id",),
        )
        self.assertTrue(
            indexes[
                (
                    "launchplane_privileged_operation_events",
                    "launchplane_privileged_operation_events_operation_sequence_uidx",
                )
            ].unique
        )
        self.assertEqual(
            column_types[("launchplane_authz_denials", "payload")],
            ("jsonb",),
        )
        self.assertEqual(
            indexes[
                (
                    "launchplane_authz_denials",
                    "launchplane_authz_denials_recorded_idx",
                )
            ].column_names,
            ("recorded_at",),
        )
        self.assertEqual(
            indexes[
                (
                    "launchplane_authz_denials",
                    "launchplane_authz_denials_expires_idx",
                )
            ].column_names,
            ("expires_at",),
        )
        self.assertEqual(
            column_types[("launchplane_merge_admissions", "payload")],
            ("jsonb",),
        )
        self.assertEqual(
            column_types[("launchplane_merge_landing_outcomes", "payload")],
            ("jsonb",),
        )
        self.assertTrue(
            indexes[
                (
                    "launchplane_merge_admissions",
                    "launchplane_merge_admissions_attempt_uidx",
                )
            ].unique
        )
        self.assertTrue(
            indexes[
                (
                    "launchplane_merge_landing_outcomes",
                    "launchplane_merge_landing_outcomes_observation_uidx",
                )
            ].unique
        )
        self.assertEqual(primary_keys["launchplane_merge_admissions"], ("admission_id",))
        self.assertEqual(
            primary_keys["launchplane_merge_landing_outcomes"],
            ("outcome_id",),
        )
        self.assertEqual(
            column_types[("launchplane_detached_application_retirements", "payload")],
            ("jsonb",),
        )
        self.assertEqual(
            indexes[
                (
                    "launchplane_detached_application_retirements",
                    "launchplane_detached_app_retirements_plan_idempotency_unique",
                )
            ].predicate_expression,
            "mode='plan'",
        )
        self.assertEqual(
            primary_keys["launchplane_detached_application_retirements"],
            ("record_id",),
        )
        self.assertEqual(
            column_types[("launchplane_repository_human_role_policies", "payload")],
            ("jsonb",),
        )
        self.assertEqual(
            column_types[("launchplane_change_impact_policies", "payload")],
            ("jsonb",),
        )
        self.assertEqual(
            indexes[
                (
                    "launchplane_change_impact_policies",
                    "launchplane_change_impact_policy_active_uidx",
                )
            ].predicate_expression,
            "status='active'",
        )
        self.assertEqual(
            primary_keys["launchplane_change_impact_policies"],
            ("record_id",),
        )
        self.assertEqual(
            column_types[("launchplane_repository_human_role_policies", "role_policy_revision")],
            ("bigint", "int8"),
        )
        self.assertEqual(
            column_types[("launchplane_tenant_technical_human_waiver_events", "payload")],
            ("jsonb",),
        )
        self.assertEqual(
            column_types[
                (
                    "launchplane_tenant_technical_human_waiver_events",
                    "author_github_id",
                )
            ],
            ("bigint", "int8"),
        )
        self.assertEqual(
            column_types[("launchplane_owner_acceptance_events", "payload")],
            ("jsonb",),
        )
        self.assertEqual(
            column_types[("launchplane_owner_acceptance_events", "owner_github_id")],
            ("bigint", "int8"),
        )
        self.assertEqual(
            column_types[("launchplane_owner_acceptance_events", "pr_number")],
            ("bigint", "int8"),
        )
        self.assertEqual(
            column_types[("launchplane_owner_acceptance_events", "subject_sequence")],
            ("bigint", "int8"),
        )
        self.assertEqual(
            indexes[
                (
                    "launchplane_owner_acceptance_events",
                    "launchplane_owner_acceptance_events_subject_idx",
                )
            ].column_names,
            (
                "repository_id",
                "pr_number",
                "product",
                "system",
                "owner_action",
                "environment",
                "subject_sequence",
            ),
        )
        self.assertTrue(
            indexes[
                (
                    "launchplane_owner_acceptance_events",
                    "launchplane_owner_acceptance_events_subject_sequence_uidx",
                )
            ].unique
        )
        self.assertEqual(
            primary_keys["launchplane_owner_acceptance_events"],
            ("event_id",),
        )
        self.assertEqual(
            primary_keys["launchplane_owner_acceptance_subject_sequences"],
            (
                "repository_id",
                "pr_number",
                "product",
                "system",
                "owner_action",
                "environment",
            ),
        )
        self.assertEqual(
            primary_keys["launchplane_repository_human_role_policies"],
            ("record_id",),
        )
        self.assertEqual(
            primary_keys["launchplane_tenant_technical_human_waiver_events"],
            ("event_id",),
        )
        self.assertEqual(
            indexes[
                (
                    "launchplane_repository_human_role_policies",
                    "launchplane_repo_human_role_revision_uidx",
                )
            ].column_names,
            ("repository_id", "product", "context", "role_policy_revision"),
        )
        self.assertEqual(
            indexes[
                (
                    "launchplane_repository_human_role_policies",
                    "launchplane_repo_human_role_active_uidx",
                )
            ].predicate_expression,
            "status='active'",
        )
        self.assertEqual(
            indexes[
                (
                    "launchplane_tenant_technical_human_waiver_events",
                    "launchplane_tenant_human_waiver_exact_head_idx",
                )
            ].column_names,
            ("repository_id", "pull_request_number", "head_sha", "occurred_at", "event_id"),
        )


if __name__ == "__main__":
    unittest.main()
