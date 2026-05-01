from __future__ import annotations

import json
import os
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from click.testing import CliRunner

from control_plane import dokploy as control_plane_dokploy
from control_plane.cli import main
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.runtime_environment_record import (
    RuntimeEnvironmentDeleteEvent,
    RuntimeEnvironmentRecord,
)
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _seed_runtime_environment_records(
    *,
    database_url: str,
    definition: control_plane_runtime_environments.RuntimeEnvironmentDefinition,
    updated_at: str = "2026-04-22T00:00:00Z",
    source_label: str = "test",
) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        for (
            record
        ) in control_plane_runtime_environments.build_runtime_environment_records_from_definition(
            definition,
            updated_at=updated_at,
            source_label=source_label,
        ):
            store.write_runtime_environment_record(record)
    finally:
        store.close()


def _seed_dokploy_target_records(*, database_url: str, payload: str) -> None:
    source_of_truth = control_plane_dokploy.DokploySourceOfTruth.model_validate(
        tomllib.loads(payload.strip())
    )
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        for target in source_of_truth.targets:
            store.write_dokploy_target_record(
                control_plane_dokploy.build_dokploy_target_record_from_definition(
                    target,
                    updated_at="2026-04-22T00:00:00Z",
                    source_label="test",
                )
            )
            store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context=target.context,
                    instance=target.instance,
                    target_id=target.target_id,
                    updated_at="2026-04-22T00:00:00Z",
                    source_label="test",
                )
            )
    finally:
        store.close()


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_environments_import_command_is_not_available(self) -> None:
        result = CliRunner().invoke(main, ["environments", "import"])

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("No such command", result.output)

    def test_environments_put_writes_db_record_without_echoing_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")

            result = CliRunner().invoke(
                main,
                [
                    "environments",
                    "put",
                    "--database-url",
                    database_url,
                    "--scope",
                    "instance",
                    "--context",
                    "verireel",
                    "--instance",
                    "prod",
                    "--set",
                    "VERIREEL_PROD_PROXMOX_HOST=proxmox.example.com",
                    "--set",
                    "VERIREEL_PROD_CT_ID=101",
                    "--source-label",
                    "operator-cli",
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertNotIn("proxmox.example.com", result.output)
            self.assertNotIn("101", result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["record"]["source_label"], "operator-cli")
            self.assertEqual(
                payload["record"]["env_keys"],
                ["VERIREEL_PROD_CT_ID", "VERIREEL_PROD_PROXMOX_HOST"],
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                resolved_values = (
                    control_plane_runtime_environments.resolve_runtime_environment_values(
                        control_plane_root=control_plane_root,
                        context_name="verireel",
                        instance_name="prod",
                    )
                )

        self.assertEqual(resolved_values["VERIREEL_PROD_PROXMOX_HOST"], "proxmox.example.com")
        self.assertEqual(resolved_values["VERIREEL_PROD_CT_ID"], "101")

    def test_environments_put_merges_existing_record_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={},
                    contexts={
                        "verireel": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={},
                            instances={
                                "prod": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                    env={"VERIREEL_PROD_PROXMOX_HOST": "proxmox.example.com"}
                                )
                            },
                        )
                    },
                ),
            )

            result = CliRunner().invoke(
                main,
                [
                    "environments",
                    "put",
                    "--database-url",
                    database_url,
                    "--scope",
                    "instance",
                    "--context",
                    "verireel",
                    "--instance",
                    "prod",
                    "--set",
                    "VERIREEL_PROD_CT_ID=101",
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertNotIn("proxmox.example.com", result.output)
            payload = json.loads(result.output)
            self.assertEqual(
                payload["record"]["env_keys"],
                ["VERIREEL_PROD_CT_ID", "VERIREEL_PROD_PROXMOX_HOST"],
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                resolved_values = (
                    control_plane_runtime_environments.resolve_runtime_environment_values(
                        control_plane_root=control_plane_root,
                        context_name="verireel",
                        instance_name="prod",
                    )
                )

        self.assertEqual(resolved_values["VERIREEL_PROD_PROXMOX_HOST"], "proxmox.example.com")
        self.assertEqual(resolved_values["VERIREEL_PROD_CT_ID"], "101")

    def test_environments_put_rejects_instance_scope_without_instance(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            result = CliRunner().invoke(
                main,
                [
                    "environments",
                    "put",
                    "--database-url",
                    database_url,
                    "--scope",
                    "instance",
                    "--context",
                    "verireel",
                    "--set",
                    "VERIREEL_PROD_CT_ID=101",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("require --context and --instance", result.output)

    def test_environments_put_rejects_secret_shaped_keys(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            result = CliRunner().invoke(
                main,
                [
                    "environments",
                    "put",
                    "--database-url",
                    database_url,
                    "--scope",
                    "context",
                    "--context",
                    "verireel",
                    "--set",
                    "GITHUB_TOKEN=secret-value",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("must be written with launchplane secrets put", result.output)
        self.assertNotIn("secret-value", result.output)

    def test_environments_unset_removes_keys_without_echoing_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={},
                    contexts={
                        "verireel": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={},
                            instances={
                                "prod": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                    env={
                                        "VERIREEL_PROD_CT_ID": "101",
                                        "VERIREEL_PROD_PROXMOX_HOST": "proxmox.example.com",
                                    }
                                )
                            },
                        )
                    },
                ),
            )

            result = CliRunner().invoke(
                main,
                [
                    "environments",
                    "unset",
                    "--database-url",
                    database_url,
                    "--scope",
                    "instance",
                    "--context",
                    "verireel",
                    "--instance",
                    "prod",
                    "--key",
                    "VERIREEL_PROD_CT_ID",
                    "--key",
                    "MISSING_KEY",
                    "--source-label",
                    "operator-cli",
                ],
            )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertNotIn("proxmox.example.com", result.output)
            self.assertNotIn("101", result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["record"]["source_label"], "operator-cli")
            self.assertEqual(payload["record"]["env_keys"], ["VERIREEL_PROD_PROXMOX_HOST"])
            self.assertEqual(payload["removed_keys"], ["VERIREEL_PROD_CT_ID"])
            self.assertEqual(payload["missing_keys"], ["MISSING_KEY"])

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                resolved_values = (
                    control_plane_runtime_environments.resolve_runtime_environment_values(
                        control_plane_root=control_plane_root,
                        context_name="verireel",
                        instance_name="prod",
                    )
                )

        self.assertNotIn("VERIREEL_PROD_CT_ID", resolved_values)
        self.assertEqual(resolved_values["VERIREEL_PROD_PROXMOX_HOST"], "proxmox.example.com")

    def test_environments_unset_rejects_empty_result_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={"ONLY_KEY": "only-value"},
                    contexts={},
                ),
            )

            result = CliRunner().invoke(
                main,
                [
                    "environments",
                    "unset",
                    "--database-url",
                    database_url,
                    "--scope",
                    "global",
                    "--key",
                    "ONLY_KEY",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Refusing to leave an empty runtime environment record", result.output)
        self.assertNotIn("only-value", result.output)

    def test_environments_delete_record_dry_run_reports_key_only_metadata(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={},
                    contexts={
                        "sellyouroutboard": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={},
                            instances={
                                "prod": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                    env={"TAWK_WIDGET_ID": "widget-123"}
                                )
                            },
                        )
                    },
                ),
                source_label="operator:mistake",
            )

            result = CliRunner().invoke(
                main,
                [
                    "environments",
                    "delete-record",
                    "--database-url",
                    database_url,
                    "--scope",
                    "instance",
                    "--context",
                    "sellyouroutboard",
                    "--instance",
                    "prod",
                    "--actor",
                    "operator@example.com",
                    "--dry-run",
                ],
            )

            store = PostgresRecordStore(database_url=database_url)
            try:
                remaining_records = store.list_runtime_environment_records(
                    scope="instance",
                    context_name="sellyouroutboard",
                    instance_name="prod",
                )
            finally:
                store.close()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("widget-123", result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "dry_run")
        self.assertFalse(payload["deleted"])
        self.assertEqual(payload["record"]["source_label"], "operator:mistake")
        self.assertEqual(payload["record"]["env_keys"], ["TAWK_WIDGET_ID"])
        self.assertEqual(payload["event"]["actor"], "operator@example.com")
        self.assertEqual(payload["event"]["env_value_count"], 1)
        self.assertEqual(len(remaining_records), 1)

    def test_environments_delete_record_apply_deletes_whole_record_and_audits(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={},
                    contexts={
                        "sellyouroutboard": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={},
                            instances={
                                "prod": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                    env={
                                        "TAWK_PROPERTY_ID": "property-123",
                                        "TAWK_WIDGET_ID": "widget-123",
                                    }
                                )
                            },
                        )
                    },
                ),
                source_label="operator:mistake",
            )

            result = CliRunner().invoke(
                main,
                [
                    "environments",
                    "delete-record",
                    "--database-url",
                    database_url,
                    "--scope",
                    "instance",
                    "--context",
                    "sellyouroutboard",
                    "--instance",
                    "prod",
                    "--actor",
                    "operator@example.com",
                    "--apply",
                ],
            )

            store = PostgresRecordStore(database_url=database_url)
            try:
                remaining_records = store.list_runtime_environment_records(
                    scope="instance",
                    context_name="sellyouroutboard",
                    instance_name="prod",
                )
                delete_events = store.list_runtime_environment_delete_events(
                    scope="instance",
                    context_name="sellyouroutboard",
                    instance_name="prod",
                )
            finally:
                store.close()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("widget-123", result.output)
        self.assertNotIn("property-123", result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["deleted"])
        self.assertEqual(payload["record"]["env_keys"], ["TAWK_PROPERTY_ID", "TAWK_WIDGET_ID"])
        self.assertEqual(remaining_records, ())
        self.assertEqual(len(delete_events), 1)
        self.assertEqual(delete_events[0].actor, "operator@example.com")
        self.assertEqual(delete_events[0].env_keys, ("TAWK_PROPERTY_ID", "TAWK_WIDGET_ID"))

    def test_environments_delete_record_apply_refuses_changed_snapshot(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={},
                    contexts={
                        "sellyouroutboard": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={},
                            instances={
                                "prod": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                    env={"TAWK_WIDGET_ID": "widget-123"}
                                )
                            },
                        )
                    },
                ),
                source_label="operator:mistake",
            )

            with patch.object(
                PostgresRecordStore,
                "delete_runtime_environment_record_with_event",
                return_value="changed",
            ):
                result = CliRunner().invoke(
                    main,
                    [
                        "environments",
                        "delete-record",
                        "--database-url",
                        database_url,
                        "--scope",
                        "instance",
                        "--context",
                        "sellyouroutboard",
                        "--instance",
                        "prod",
                        "--apply",
                    ],
                )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("changed before delete could complete", result.output)

    def test_environments_delete_record_appends_audit_events_for_repeated_delete_shape(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            definition = control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                schema_version=1,
                shared_env={},
                contexts={
                    "sellyouroutboard": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                        shared_env={},
                        instances={
                            "prod": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                env={"TAWK_WIDGET_ID": "widget-123"}
                            )
                        },
                    )
                },
            )
            for _ in range(2):
                _seed_runtime_environment_records(
                    database_url=database_url,
                    definition=definition,
                    updated_at="2026-04-22T00:00:00Z",
                    source_label="operator:mistake",
                )
                result = CliRunner().invoke(
                    main,
                    [
                        "environments",
                        "delete-record",
                        "--database-url",
                        database_url,
                        "--scope",
                        "instance",
                        "--context",
                        "sellyouroutboard",
                        "--instance",
                        "prod",
                        "--actor",
                        "operator@example.com",
                        "--apply",
                    ],
                )
                self.assertEqual(result.exit_code, 0, result.output)

            store = PostgresRecordStore(database_url=database_url)
            try:
                delete_events = store.list_runtime_environment_delete_events(
                    scope="instance",
                    context_name="sellyouroutboard",
                    instance_name="prod",
                )
            finally:
                store.close()

        self.assertEqual(len(delete_events), 2)
        self.assertNotEqual(delete_events[0].event_id, delete_events[1].event_id)

    def test_delete_runtime_environment_record_refuses_changed_snapshot(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            expected_record = RuntimeEnvironmentRecord(
                scope="instance",
                context="sellyouroutboard",
                instance="prod",
                env={"TAWK_WIDGET_ID": "widget-123"},
                updated_at="2026-04-22T00:00:00Z",
                source_label="operator:mistake",
            )
            changed_record = RuntimeEnvironmentRecord(
                scope="instance",
                context="sellyouroutboard",
                instance="prod",
                env={"TAWK_PROPERTY_ID": "property-123"},
                updated_at="2026-04-22T00:01:00Z",
                source_label="operator:replacement",
            )
            stale_delete_event = RuntimeEnvironmentDeleteEvent(
                event_id="runtime-env-delete-test",
                recorded_at="2026-04-22T00:02:00Z",
                actor="operator@example.com",
                scope=expected_record.scope,
                context=expected_record.context,
                instance=expected_record.instance,
                source_label=expected_record.source_label,
                env_keys=tuple(sorted(expected_record.env.keys())),
                env_value_count=len(expected_record.env),
                detail="deleted by launchplane environments delete-record",
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_runtime_environment_record(expected_record)
                store.write_runtime_environment_record(changed_record)

                delete_status = store.delete_runtime_environment_record_with_event(
                    event=stale_delete_event,
                    expected_record=expected_record,
                )

                remaining_records = store.list_runtime_environment_records(
                    scope="instance",
                    context_name="sellyouroutboard",
                    instance_name="prod",
                )
                delete_events = store.list_runtime_environment_delete_events()
            finally:
                store.close()

        self.assertEqual(delete_status, "changed")
        self.assertEqual(remaining_records, (changed_record,))
        self.assertEqual(delete_events, ())

    def test_environments_delete_record_refuses_tracked_target_without_allow_flag(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={},
                    contexts={
                        "sellyouroutboard-testing": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={},
                            instances={
                                "prod": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                    env={"TAWK_WIDGET_ID": "widget-123"}
                                )
                            },
                        )
                    },
                ),
            )
            _seed_dokploy_target_records(
                database_url=database_url,
                payload=(
                    "schema_version = 2\n\n"
                    "[[targets]]\n"
                    'context = "sellyouroutboard-testing"\n'
                    'instance = "prod"\n'
                    'target_id = "target-syo-prod"\n'
                    'target_name = "syo-prod-app"\n'
                ),
            )

            result = CliRunner().invoke(
                main,
                [
                    "environments",
                    "delete-record",
                    "--database-url",
                    database_url,
                    "--scope",
                    "instance",
                    "--context",
                    "sellyouroutboard-testing",
                    "--instance",
                    "prod",
                    "--apply",
                ],
            )

            store = PostgresRecordStore(database_url=database_url)
            try:
                remaining_records = store.list_runtime_environment_records(
                    scope="instance",
                    context_name="sellyouroutboard-testing",
                    instance_name="prod",
                )
                delete_events = store.list_runtime_environment_delete_events()
            finally:
                store.close()

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Refusing to delete", result.output)
        self.assertIn("sellyouroutboard-testing/prod", result.output)
        self.assertNotIn("widget-123", result.output)
        self.assertEqual(len(remaining_records), 1)
        self.assertEqual(delete_events, ())

    def test_environments_delete_record_allow_flag_deletes_tracked_target_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={},
                    contexts={
                        "sellyouroutboard-testing": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={},
                            instances={
                                "prod": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                    env={"TAWK_WIDGET_ID": "widget-123"}
                                )
                            },
                        )
                    },
                ),
            )
            _seed_dokploy_target_records(
                database_url=database_url,
                payload=(
                    "schema_version = 2\n\n"
                    "[[targets]]\n"
                    'context = "sellyouroutboard-testing"\n'
                    'instance = "prod"\n'
                    'target_id = "target-syo-prod"\n'
                    'target_name = "syo-prod-app"\n'
                ),
            )

            result = CliRunner().invoke(
                main,
                [
                    "environments",
                    "delete-record",
                    "--database-url",
                    database_url,
                    "--scope",
                    "instance",
                    "--context",
                    "sellyouroutboard-testing",
                    "--instance",
                    "prod",
                    "--allow-tracked-target",
                    "--apply",
                ],
            )

            store = PostgresRecordStore(database_url=database_url)
            try:
                remaining_records = store.list_runtime_environment_records(
                    scope="instance",
                    context_name="sellyouroutboard-testing",
                    instance_name="prod",
                )
                delete_events = store.list_runtime_environment_delete_events()
            finally:
                store.close()

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("widget-123", result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["protected_tracked_targets"][0]["target_name"], "syo-prod-app")
        self.assertFalse(payload["requires_allow_tracked_target"])
        self.assertEqual(remaining_records, ())
        self.assertEqual(len(delete_events), 1)

    def test_environments_delete_record_distinguishes_missing_and_invalid_routes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            missing_result = CliRunner().invoke(
                main,
                [
                    "environments",
                    "delete-record",
                    "--database-url",
                    database_url,
                    "--scope",
                    "global",
                    "--dry-run",
                ],
            )
            invalid_result = CliRunner().invoke(
                main,
                [
                    "environments",
                    "delete-record",
                    "--database-url",
                    database_url,
                    "--scope",
                    "context",
                    "--context",
                    "sellyouroutboard",
                    "--instance",
                    "prod",
                    "--dry-run",
                ],
            )

        self.assertNotEqual(missing_result.exit_code, 0)
        self.assertIn("Missing DB-backed runtime environment record", missing_result.output)
        self.assertNotEqual(invalid_result.exit_code, 0)
        self.assertIn("require --context and do not accept --instance", invalid_result.output)

    def test_environments_relabel_updates_metadata_without_echoing_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={"ODOO_DB_USER": "odoo"},
                    contexts={},
                ),
                source_label="legacy-source",
            )

            result = CliRunner().invoke(
                main,
                [
                    "environments",
                    "relabel",
                    "--database-url",
                    database_url,
                    "--scope",
                    "global",
                    "--source-label",
                    "operator:db-native",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("odoo", result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["record"]["source_label"], "operator:db-native")
        self.assertEqual(payload["record"]["env_keys"], ["ODOO_DB_USER"])

    def test_environments_list_redacts_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={},
                    contexts={
                        "verireel": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={},
                            instances={
                                "prod": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                    env={"VERIREEL_PROD_PROXMOX_HOST": "proxmox.example.com"}
                                )
                            },
                        )
                    },
                ),
            )

            result = CliRunner().invoke(
                main,
                ["environments", "list", "--database-url", database_url],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertNotIn("proxmox.example.com", result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["records"][0]["env_keys"], ["VERIREEL_PROD_PROXMOX_HOST"])

    def test_resolve_runtime_environment_values_merges_shared_context_and_instance_values(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={"ODOO_MASTER_PASSWORD": "shared-master", "ODOO_DB_USER": "odoo"},
                    contexts={
                        "opw": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={"ENV_OVERRIDE_DISABLE_CRON": True},
                            instances={
                                "local": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                    env={
                                        "ODOO_DB_PASSWORD": "local-secret",
                                        "ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL": "https://opw-local.example.com",
                                    }
                                )
                            },
                        )
                    },
                ),
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                resolved_values = (
                    control_plane_runtime_environments.resolve_runtime_environment_values(
                        control_plane_root=control_plane_root,
                        context_name="opw",
                        instance_name="local",
                    )
                )

        self.assertEqual(resolved_values["ODOO_MASTER_PASSWORD"], "shared-master")
        self.assertEqual(resolved_values["ODOO_DB_USER"], "odoo")
        self.assertEqual(resolved_values["ODOO_DB_PASSWORD"], "local-secret")
        self.assertEqual(resolved_values["ENV_OVERRIDE_DISABLE_CRON"], "True")
        self.assertEqual(
            resolved_values["ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL"],
            "https://opw-local.example.com",
        )

    def test_resolve_runtime_environment_values_fails_closed_when_instance_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={},
                    contexts={
                        "opw": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={},
                            instances={
                                "testing": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                    env={"ODOO_DB_PASSWORD": "testing-secret"}
                                )
                            },
                        )
                    },
                ),
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                with self.assertRaisesRegex(Exception, "opw/local"):
                    control_plane_runtime_environments.resolve_runtime_environment_values(
                        control_plane_root=control_plane_root,
                        context_name="opw",
                        instance_name="local",
                    )

    def test_resolve_runtime_environment_values_merges_tracked_target_env_for_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            config_directory = control_plane_root / "config"
            config_directory.mkdir(parents=True, exist_ok=True)
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={"ODOO_MASTER_PASSWORD": "shared-master"},
                    contexts={
                        "cm": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={},
                            instances={
                                "testing": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                    env={"ODOO_DB_PASSWORD": "testing-secret"}
                                )
                            },
                        )
                    },
                ),
            )
            _seed_dokploy_target_records(
                database_url=database_url,
                payload=(
                    "schema_version = 2\n\n"
                    "[[targets]]\n"
                    'context = "cm"\n'
                    'instance = "testing"\n'
                    'target_id = "target-cm-testing"\n\n'
                    "[targets.env]\n"
                    'ODOO_ADDON_REPOSITORIES = "cbusillo/disable_odoo_online@411f6b8e85cac72dc7aa2e2dc5540001043c327d"\n'
                    'ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL = "https://cm-testing.example.com"\n'
                ),
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                resolved_values = (
                    control_plane_runtime_environments.resolve_runtime_environment_values(
                        control_plane_root=control_plane_root,
                        context_name="cm",
                        instance_name="testing",
                    )
                )

        self.assertEqual(resolved_values["ODOO_MASTER_PASSWORD"], "shared-master")
        self.assertEqual(resolved_values["ODOO_DB_PASSWORD"], "testing-secret")
        self.assertEqual(
            resolved_values["ODOO_ADDON_REPOSITORIES"],
            "cbusillo/disable_odoo_online@411f6b8e85cac72dc7aa2e2dc5540001043c327d",
        )
        self.assertEqual(
            resolved_values["ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL"],
            "https://cm-testing.example.com",
        )

    def test_resolve_runtime_context_values_merges_shared_and_context_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={
                        "LAUNCHPLANE_PREVIEW_BASE_URL": "https://launchplane.example",
                        "ODOO_MASTER_PASSWORD": "shared-master",
                    },
                    contexts={
                        "opw": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={"ENV_OVERRIDE_DISABLE_CRON": True},
                            instances={},
                        )
                    },
                ),
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                resolved_values = control_plane_runtime_environments.resolve_runtime_context_values(
                    control_plane_root=control_plane_root,
                    context_name="opw",
                )

        self.assertEqual(
            resolved_values["LAUNCHPLANE_PREVIEW_BASE_URL"], "https://launchplane.example"
        )
        self.assertEqual(resolved_values["ODOO_MASTER_PASSWORD"], "shared-master")
        self.assertEqual(resolved_values["ENV_OVERRIDE_DISABLE_CRON"], "True")

    def test_load_runtime_environment_definition_prefers_postgres_records_without_file_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[shared_env]
ODOO_MASTER_PASSWORD = "shared-master"

[contexts.opw.instances.local.env]
ODOO_DB_PASSWORD = "file-secret"

[contexts.cm.instances.testing.env]
ODOO_DB_PASSWORD = "cm-file-secret"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            for record in control_plane_runtime_environments.build_runtime_environment_records_from_definition(
                control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={"ODOO_MASTER_PASSWORD": "db-master"},
                    contexts={
                        "opw": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={},
                            instances={
                                "local": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                    env={"ODOO_DB_PASSWORD": "db-secret"}
                                )
                            },
                        )
                    },
                ),
                updated_at="2026-04-21T19:00:00Z",
                source_label="import:test",
            ):
                store.write_runtime_environment_record(record)
            store.close()

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                resolved_values = (
                    control_plane_runtime_environments.resolve_runtime_environment_values(
                        control_plane_root=control_plane_root,
                        context_name="opw",
                        instance_name="local",
                    )
                )
                loaded_definition = (
                    control_plane_runtime_environments.load_runtime_environment_definition(
                        control_plane_root=control_plane_root,
                    )
                )

        self.assertEqual(resolved_values["ODOO_MASTER_PASSWORD"], "db-master")
        self.assertEqual(resolved_values["ODOO_DB_PASSWORD"], "db-secret")
        self.assertNotIn("cm", loaded_definition.contexts)

    def test_load_runtime_environment_definition_requires_database_records_without_file_fallback(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[contexts.opw.instances.local.env]
ODOO_DB_PASSWORD = "file-secret"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaisesRegex(
                    Exception, "Missing Launchplane runtime environment authority"
                ):
                    control_plane_runtime_environments.load_runtime_environment_definition(
                        control_plane_root=control_plane_root,
                    )

    def test_environments_resolve_command_emits_json_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={"ODOO_MASTER_PASSWORD": "shared-master"},
                    contexts={
                        "opw": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={},
                            instances={
                                "local": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                    env={"ODOO_DB_PASSWORD": "local-secret"}
                                )
                            },
                        )
                    },
                ),
            )
            command_runner = CliRunner()
            with (
                patch("control_plane.cli._control_plane_root", return_value=control_plane_root),
                patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True),
            ):
                result = command_runner.invoke(
                    main,
                    [
                        "environments",
                        "resolve",
                        "--context",
                        "opw",
                        "--instance",
                        "local",
                        "--json-output",
                    ],
                )

        self.assertEqual(result.exit_code, 0)
        payload = json.loads(result.output)
        self.assertEqual(payload["context"], "opw")
        self.assertEqual(payload["instance"], "local")
        self.assertEqual(payload["environment"]["ODOO_MASTER_PASSWORD"], "shared-master")
        self.assertEqual(payload["environment"]["ODOO_DB_PASSWORD"], "local-secret")

    def test_product_config_apply_dry_run_redacts_and_does_not_write(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            input_file = control_plane_root / "product-config.json"
            input_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                        "runtime_env": {
                            "CONTACT_EMAIL_MODE": "smtp",
                            "SELLYOUROUTBOARD_SITE_URL": "https://www.sellyouroutboard.com",
                        },
                        "secrets": [
                            {
                                "name": "smtp-password",
                                "binding_key": "SMTP_PASSWORD",
                                "value": "smtp-secret-value",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "test-master-key"},
                clear=True,
            ):
                result = CliRunner().invoke(
                    main,
                    [
                        "product-config",
                        "apply",
                        "--database-url",
                        database_url,
                        "--input-file",
                        str(input_file),
                        "--actor",
                        "operator@example.com",
                        "--dry-run",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertNotIn("smtp-secret-value", result.output)
            self.assertNotIn("https://www.sellyouroutboard.com", result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["mode"], "dry-run")
            self.assertEqual(payload["runtime_environment"]["action"], "created")
            self.assertEqual(payload["secrets"][0]["action"], "created")

            store = PostgresRecordStore(database_url=database_url)
            try:
                self.assertEqual(store.list_runtime_environment_records(), ())
                self.assertEqual(store.list_secret_records(), ())
            finally:
                store.close()

    def test_product_config_apply_writes_runtime_env_and_secret_without_echoing_values(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            input_file = control_plane_root / "product-config.json"
            input_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                        "runtime_env": {"CONTACT_EMAIL_MODE": "smtp"},
                        "secrets": [
                            {
                                "name": "smtp-password",
                                "binding_key": "SMTP_PASSWORD",
                                "value": "smtp-secret-value",
                                "description": "SMTP password for owner contact mail.",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    "LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "test-master-key",
                },
                clear=True,
            ):
                result = CliRunner().invoke(
                    main,
                    [
                        "product-config",
                        "apply",
                        "--input-file",
                        str(input_file),
                        "--actor",
                        "operator@example.com",
                        "--source-label",
                        "issue-110-test",
                        "--apply",
                    ],
                )
                resolved_values = (
                    control_plane_runtime_environments.resolve_runtime_environment_values(
                        control_plane_root=control_plane_root,
                        context_name="sellyouroutboard",
                        instance_name="prod",
                    )
                )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertNotIn("smtp-secret-value", result.output)
            self.assertNotIn('smtp"', result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["mode"], "apply")
            self.assertEqual(
                payload["runtime_environment"]["record"]["source_label"], "issue-110-test"
            )
            self.assertEqual(payload["secrets"][0]["action"], "created")
            self.assertEqual(resolved_values["CONTACT_EMAIL_MODE"], "smtp")
            self.assertEqual(resolved_values["SMTP_PASSWORD"], "smtp-secret-value")

            store = PostgresRecordStore(database_url=database_url)
            try:
                secret_records = store.list_secret_records()
                self.assertEqual(len(secret_records), 1)
                audit_events = store.list_secret_audit_events(secret_id=secret_records[0].secret_id)
                self.assertEqual(audit_events[0].actor, "operator@example.com")
                self.assertEqual(audit_events[0].metadata["source"], "issue-110-test")
            finally:
                store.close()

    def test_product_config_apply_rejects_runtime_route_that_differs_from_top_level(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            input_file = control_plane_root / "product-config.json"
            input_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                        "runtime_env": {
                            "context": "other-context",
                            "instance": "prod",
                            "env": {"CONTACT_EMAIL_MODE": "smtp"},
                        },
                    }
                ),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                main,
                [
                    "product-config",
                    "apply",
                    "--database-url",
                    database_url,
                    "--input-file",
                    str(input_file),
                    "--dry-run",
                ],
            )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("runtime_env target must match the top-level target", result.output)

            store = PostgresRecordStore(database_url=database_url)
            try:
                self.assertEqual(store.list_runtime_environment_records(), ())
            finally:
                store.close()

    def test_product_config_apply_rejects_secret_route_that_differs_from_top_level(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            input_file = control_plane_root / "product-config.json"
            input_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                        "runtime_env": {},
                        "secrets": [
                            {
                                "scope": "context_instance",
                                "name": "smtp-password",
                                "binding_key": "SMTP_PASSWORD",
                                "value": "smtp-secret-value",
                                "context": "other-context",
                                "instance": "prod",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "test-master-key"},
                clear=True,
            ):
                result = CliRunner().invoke(
                    main,
                    [
                        "product-config",
                        "apply",
                        "--database-url",
                        database_url,
                        "--input-file",
                        str(input_file),
                        "--apply",
                    ],
                )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("secret #1 target must match the top-level target", result.output)
            self.assertNotIn("smtp-secret-value", result.output)

            store = PostgresRecordStore(database_url=database_url)
            try:
                self.assertEqual(store.list_secret_records(), ())
            finally:
                store.close()

    def test_product_config_apply_rejects_invalid_secret_scope_before_writes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            input_file = control_plane_root / "product-config.json"
            input_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "runtime_env": {},
                        "secrets": [
                            {
                                "scope": "global",
                                "name": "valid-secret",
                                "binding_key": "VALID_SECRET",
                                "value": "first-secret-value",
                            },
                            {
                                "scope": "not-a-scope",
                                "name": "invalid-secret",
                                "binding_key": "INVALID_SECRET",
                                "value": "second-secret-value",
                            },
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": "test-master-key"},
                clear=True,
            ):
                result = CliRunner().invoke(
                    main,
                    [
                        "product-config",
                        "apply",
                        "--database-url",
                        database_url,
                        "--input-file",
                        str(input_file),
                        "--apply",
                    ],
                )

            self.assertNotEqual(result.exit_code, 0)
            self.assertIn("unsupported scope 'not-a-scope'", result.output)
            self.assertNotIn("first-secret-value", result.output)
            self.assertNotIn("second-secret-value", result.output)

            store = PostgresRecordStore(database_url=database_url)
            try:
                self.assertEqual(store.list_secret_records(), ())
                self.assertEqual(store.list_secret_bindings(limit=None), ())
            finally:
                store.close()

    def test_product_config_apply_requires_master_key_for_secrets(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            input_file = control_plane_root / "product-config.json"
            input_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                        "secrets": [
                            {
                                "name": "smtp-password",
                                "binding_key": "SMTP_PASSWORD",
                                "value": "smtp-secret-value",
                            }
                        ],
                    }
                ),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                result = CliRunner().invoke(
                    main,
                    [
                        "product-config",
                        "apply",
                        "--database-url",
                        database_url,
                        "--input-file",
                        str(input_file),
                        "--dry-run",
                    ],
                )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn(
            "Product config secrets require LAUNCHPLANE_MASTER_ENCRYPTION_KEY", result.output
        )
        self.assertNotIn("smtp-secret-value", result.output)

    def test_product_config_apply_rejects_secret_shaped_runtime_env(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            input_file = control_plane_root / "product-config.json"
            input_file.write_text(
                json.dumps(
                    {
                        "schema_version": 1,
                        "product": "sellyouroutboard",
                        "context": "sellyouroutboard",
                        "instance": "prod",
                        "runtime_env": {"SMTP_PASSWORD": "smtp-secret-value"},
                    }
                ),
                encoding="utf-8",
            )

            result = CliRunner().invoke(
                main,
                [
                    "product-config",
                    "apply",
                    "--database-url",
                    database_url,
                    "--input-file",
                    str(input_file),
                    "--dry-run",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("must be written as a managed secret", result.output)
        self.assertNotIn("smtp-secret-value", result.output)


if __name__ == "__main__":
    unittest.main()
