import json
import os
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click
from click.testing import CliRunner
from pydantic import ValidationError

from control_plane import dokploy as control_plane_dokploy
from control_plane import secrets as control_plane_secrets
from control_plane.cli import main
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.release_tuple_record import ReleaseTupleRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _write_dokploy_managed_secrets(*, store: PostgresRecordStore, host: str, token: str) -> None:
    control_plane_secrets.write_secret_value(
        record_store=store,
        scope="global",
        integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
        name="host",
        plaintext_value=host,
        binding_key="DOKPLOY_HOST",
        actor="test",
    )
    control_plane_secrets.write_secret_value(
        record_store=store,
        scope="global",
        integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
        name="token",
        plaintext_value=token,
        binding_key="DOKPLOY_TOKEN",
        actor="test",
    )


def _seed_dokploy_target_records(
    *,
    store: PostgresRecordStore,
    payload: str,
    updated_at: str = "2026-04-22T00:00:00Z",
) -> None:
    source_of_truth = control_plane_dokploy.DokploySourceOfTruth.model_validate(
        tomllib.loads(payload.strip())
    )
    for target in source_of_truth.targets:
        store.write_dokploy_target_record(
            control_plane_dokploy.build_dokploy_target_record_from_definition(
                target,
                updated_at=updated_at,
                source_label="test",
            )
        )
        store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context=target.context,
                instance=target.instance,
                target_id=target.target_id,
                updated_at=updated_at,
                source_label="test",
            )
        )


class DokployConfigTests(unittest.TestCase):
    def test_service_inspect_config_boundary_reports_db_only_authority(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(
                    store=store,
                    host="https://dokploy.db.example",
                    token="db-token",
                )
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="global",
                        context="",
                        instance="",
                        env={"LAUNCHPLANE_PREVIEW_BASE_URL": "https://launchplane.example.com"},
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    )
                )
                store.write_dokploy_target_id_record(
                    DokployTargetIdRecord(
                        context="cm",
                        instance="prod",
                        target_id="compose-123",
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    )
                )
                result = runner.invoke(
                    main,
                    [
                        "service",
                        "inspect-config-boundary",
                        "--control-plane-root",
                        str(control_plane_root),
                    ],
                )

            store.close()

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertTrue(payload["db"]["inspectable"])
        self.assertEqual(payload["authority"]["dokploy_credentials"], "db_only")
        self.assertEqual(payload["authority"]["runtime_environments"], "db_only")
        self.assertEqual(payload["authority"]["dokploy_target_ids"], "db_only")
        self.assertEqual(payload["authority"]["stable_targets"], "missing")
        self.assertEqual(payload["authority"]["release_tuples_catalog"], "missing")
        self.assertEqual(payload["transition_inputs"]["selector_env_keys_present"], [])
        self.assertEqual(payload["transition_inputs"]["payload_env_keys_present"], [])

    def test_service_inspect_config_boundary_reports_mixed_authority(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            config_directory = control_plane_root / "config"
            config_directory.mkdir(parents=True, exist_ok=True)
            (control_plane_root / ".env").write_text(
                "DOKPLOY_HOST=https://dokploy.file.example\nDOKPLOY_TOKEN=file-token\n",
                encoding="utf-8",
            )
            (config_directory / "runtime-environments.toml").write_text(
                (
                    'schema_version = 1\n\n'
                    '[shared_env]\n'
                    'LAUNCHPLANE_PREVIEW_BASE_URL = "https://launchplane.file.example.com"\n'
                ),
                encoding="utf-8",
            )
            (config_directory / "dokploy.toml").write_text(
                (
                    'schema_version = 2\n\n'
                    '[[targets]]\n'
                    'context = "cm"\n'
                    'instance = "prod"\n'
                    'target_id = "compose-file"\n'
                ),
                encoding="utf-8",
            )
            (config_directory / "dokploy-targets.toml").write_text(
                (
                    'schema_version = 1\n\n'
                    '[[targets]]\n'
                    'context = "cm"\n'
                    'instance = "prod"\n'
                    'target_id = "compose-override"\n'
                ),
                encoding="utf-8",
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(
                    store=store,
                    host="https://dokploy.db.example",
                    token="db-token",
                )
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="global",
                        context="",
                        instance="",
                        env={"LAUNCHPLANE_PREVIEW_BASE_URL": "https://launchplane.db.example.com"},
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    )
                )
                store.write_dokploy_target_id_record(
                    DokployTargetIdRecord(
                        context="cm",
                        instance="prod",
                        target_id="compose-123",
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    )
                )
                store.write_dokploy_target_record(
                    control_plane_dokploy.build_dokploy_target_record_from_definition(
                        control_plane_dokploy.DokployTargetDefinition(
                            context="cm",
                            instance="prod",
                            target_id="compose-123",
                            target_type="compose",
                        ),
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    )
                )
                store.write_release_tuple_record(
                    ReleaseTupleRecord(
                        tuple_id="cm-testing-2026-04-22",
                        context="cm",
                        channel="testing",
                        artifact_id="artifact-testing",
                        repo_shas={"tenant-cm": "3333333333333333333333333333333333333333"},
                        provenance="ship",
                        minted_at="2026-04-22T00:00:00Z",
                    )
                )

                result = runner.invoke(
                    main,
                    [
                        "service",
                        "inspect-config-boundary",
                        "--control-plane-root",
                        str(control_plane_root),
                    ],
                )

            store.close()

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["authority"]["dokploy_credentials"], "db_only")
        self.assertEqual(payload["authority"]["runtime_environments"], "db_only")
        self.assertEqual(payload["authority"]["dokploy_target_ids"], "db_only")
        self.assertEqual(payload["authority"]["stable_targets"], "db_only")
        self.assertEqual(payload["authority"]["release_tuples_catalog"], "db_only")
        self.assertTrue(payload["legacy_paths"]["repo_env_file"]["exists"])
        self.assertTrue(payload["legacy_paths"]["repo_runtime_environments_file"]["exists"])
        self.assertTrue(payload["legacy_paths"]["repo_dokploy_source_file"]["exists"])
        self.assertTrue(payload["legacy_paths"]["repo_dokploy_target_ids_file"]["exists"])

    def test_environments_show_live_target_reports_legacy_runtime_contract_blockers(self) -> None:
        runner = CliRunner()
        source_of_truth = control_plane_dokploy.DokploySourceOfTruth.model_validate(
            {
                "schema_version": 2,
                "targets": [
                    {
                        "context": "opw",
                        "instance": "testing",
                        "target_id": "compose-123",
                        "target_type": "compose",
                        "target_name": "opw-testing",
                    }
                ],
            }
        )

        with patch(
            "control_plane.dokploy.read_control_plane_dokploy_source_of_truth",
            return_value=source_of_truth,
        ), patch(
            "control_plane.dokploy.read_dokploy_config",
            return_value=("https://dokploy.example.com", "token-123"),
        ), patch(
            "control_plane.dokploy.fetch_dokploy_target_payload",
            return_value={
                "name": "opw-testing",
                "appName": "compose-opw-testing",
                "sourceType": "git",
                "customGitUrl": "git@github.com:cbusillo/odoo-ai.git",
                "customGitBranch": "opw-testing",
                "composePath": "./docker-compose.yml",
                "env": (
                    "ODOO_BASE_RUNTIME_IMAGE=ghcr.io/cbusillo/odoo-enterprise-docker:19.0-runtime\n"
                    "ODOO_ADDON_REPOSITORIES=cbusillo/disable_odoo_online@main,OCA/OpenUpgrade@89e649728027a8ab656b3aa4be18f4bd364db417"
                ),
            },
        ):
            result = runner.invoke(
                main,
                [
                    "environments",
                    "show-live-target",
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["tracked_target"]["target_id"], "compose-123")
        self.assertEqual(payload["live_target"]["custom_git_url"], "git@github.com:cbusillo/odoo-ai.git")
        self.assertFalse(payload["artifact_runtime_contract"]["artifact_ready"])
        self.assertIn(
            "git@github.com:cbusillo/odoo-ai.git",
            payload["artifact_runtime_contract"]["legacy_monorepo_sources"],
        )
        self.assertIn(
            "cbusillo/disable_odoo_online@main",
            payload["artifact_runtime_contract"]["mutable_addon_refs"],
        )

    def test_environments_sync_live_target_applies_tracked_source_and_env_contract(self) -> None:
        runner = CliRunner()
        source_of_truth = control_plane_dokploy.DokploySourceOfTruth.model_validate(
            {
                "schema_version": 2,
                "targets": [
                    {
                        "context": "opw",
                        "instance": "testing",
                        "target_id": "compose-123",
                        "target_type": "compose",
                        "target_name": "opw-testing",
                        "source_type": "git",
                        "custom_git_url": "git@github.com:cbusillo/odoo-devkit.git",
                        "custom_git_branch": "main",
                        "compose_path": "./docker-compose.yml",
                        "env": {
                            "ODOO_ADDON_REPOSITORIES": "cbusillo/disable_odoo_online@411f6b8e85cac72dc7aa2e2dc5540001043c327d"
                        },
                    }
                ],
            }
        )
        fetch_payloads = [
            {
                "name": "opw-testing",
                "appName": "compose-opw-testing",
                "sourceType": "git",
                "customGitUrl": "git@github.com:cbusillo/odoo-ai.git",
                "customGitBranch": "opw-testing",
                "customGitSSHKeyId": "ssh-key-123",
                "composePath": "./docker-compose.yml",
                "environmentId": "env-123",
                "triggerType": "push",
                "enableSubmodules": False,
                "watchPaths": [],
                "env": "ODOO_ADDON_REPOSITORIES=cbusillo/disable_odoo_online@main\n",
            },
            {
                "name": "opw-testing",
                "appName": "compose-opw-testing",
                "sourceType": "git",
                "customGitUrl": "git@github.com:cbusillo/odoo-devkit.git",
                "customGitBranch": "main",
                "customGitSSHKeyId": "ssh-key-123",
                "composePath": "./docker-compose.yml",
                "environmentId": "env-123",
                "triggerType": "push",
                "enableSubmodules": False,
                "watchPaths": [],
                "env": "ODOO_ADDON_REPOSITORIES=cbusillo/disable_odoo_online@main\n",
            },
            {
                "name": "opw-testing",
                "appName": "compose-opw-testing",
                "sourceType": "git",
                "customGitUrl": "git@github.com:cbusillo/odoo-devkit.git",
                "customGitBranch": "main",
                "customGitSSHKeyId": "ssh-key-123",
                "composePath": "./docker-compose.yml",
                "environmentId": "env-123",
                "triggerType": "push",
                "enableSubmodules": False,
                "watchPaths": [],
                "env": "ODOO_ADDON_REPOSITORIES=cbusillo/disable_odoo_online@411f6b8e85cac72dc7aa2e2dc5540001043c327d\n",
            },
        ]
        captured_source_updates: list[dict[str, object]] = []
        captured_env_updates: list[dict[str, object]] = []

        with patch(
            "control_plane.dokploy.read_control_plane_dokploy_source_of_truth",
            return_value=source_of_truth,
        ), patch(
            "control_plane.dokploy.read_dokploy_config",
            return_value=("https://dokploy.example.com", "token-123"),
        ), patch(
            "control_plane.dokploy.fetch_dokploy_target_payload",
            side_effect=fetch_payloads,
        ), patch(
            "control_plane.dokploy.update_dokploy_target_source",
            side_effect=lambda **kwargs: captured_source_updates.append(kwargs),
        ), patch(
            "control_plane.dokploy.update_dokploy_target_env",
            side_effect=lambda **kwargs: captured_env_updates.append(kwargs),
        ):
            result = runner.invoke(
                main,
                [
                    "environments",
                    "sync-live-target",
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--apply",
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(len(captured_source_updates), 1)
        self.assertEqual(len(captured_env_updates), 1)
        self.assertIn("411f6b8e85cac72dc7aa2e2dc5540001043c327d", str(captured_env_updates[0]["env_text"]))
        payload = json.loads(result.output)
        self.assertTrue(payload["artifact_runtime_contract"]["artifact_ready"])
        self.assertEqual(payload["live_target"]["custom_git_url"], "git@github.com:cbusillo/odoo-devkit.git")
        self.assertEqual(payload["sync_preview"]["source_changes"]["custom_git_url"]["tracked"], "git@github.com:cbusillo/odoo-devkit.git")

    def test_read_control_plane_dokploy_source_of_truth_prefers_postgres_target_ids_without_file_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-file"
target_type = "compose"

[[targets]]
context = "cm"
instance = "testing"
target_id = "compose-cm-file"
target_type = "compose"
""",
            )
            store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context="opw",
                    instance="prod",
                    target_id="compose-db",
                    updated_at="2026-04-21T19:00:00Z",
                    source_label="import:test",
                )
            )
            store.write_dokploy_target_id_record(
                DokployTargetIdRecord(
                    context="cm",
                    instance="testing",
                    target_id="compose-cm-db",
                    updated_at="2026-04-21T19:00:00Z",
                    source_label="import:test",
                )
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
                    control_plane_root=control_plane_root
                )

            store.close()

        self.assertEqual(
            [(target.context, target.instance, target.target_id) for target in source_of_truth.targets],
            [("cm", "testing", "compose-cm-db"), ("opw", "prod", "compose-db")],
        )

    def test_read_control_plane_dokploy_source_of_truth_requires_database_target_ids_without_file_fallback(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.write_dokploy_target_record(
                control_plane_dokploy.build_dokploy_target_record_from_definition(
                    control_plane_dokploy.DokployTargetDefinition(
                        context="opw",
                        instance="prod",
                        target_id="compose-placeholder",
                        target_type="compose",
                    ),
                    updated_at="2026-04-22T00:00:00Z",
                    source_label="test",
                )
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
                        control_plane_root=control_plane_root
                    )

            store.close()

        self.assertIn("Missing DB-backed Dokploy target-id record for opw/prod", str(raised_error.exception))

    def test_read_control_plane_dokploy_source_of_truth_reads_database_target_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
target_type = "compose"
""",
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
                    control_plane_root=control_plane_root
                )

            store.close()

        self.assertEqual(len(source_of_truth.targets), 1)
        self.assertEqual(source_of_truth.targets[0].target_id, "compose-123")

    def test_read_control_plane_dokploy_source_of_truth_fails_closed_when_target_id_missing(self) -> None:
        with self.assertRaises(click.ClickException) as raised_error:
            control_plane_dokploy.build_dokploy_source_of_truth_from_records(
                (
                    control_plane_dokploy.build_dokploy_target_record_from_definition(
                        control_plane_dokploy.DokployTargetDefinition(
                            context="opw",
                            instance="prod",
                            target_id="compose-placeholder",
                            target_type="compose",
                        ),
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    ),
                ),
                (),
            )

        self.assertIn("Missing DB-backed Dokploy target-id record for opw/prod", str(raised_error.exception))

    def test_read_control_plane_dokploy_source_of_truth_rejects_duplicate_context_instance_targets(self) -> None:
        duplicate_records = (
            control_plane_dokploy.build_dokploy_target_record_from_definition(
                control_plane_dokploy.DokployTargetDefinition(
                    context="opw",
                    instance="prod",
                    target_id="compose-123",
                    target_type="compose",
                ),
                updated_at="2026-04-22T00:00:00Z",
                source_label="test",
            ),
            control_plane_dokploy.build_dokploy_target_record_from_definition(
                control_plane_dokploy.DokployTargetDefinition(
                    context="opw",
                    instance="prod",
                    target_id="compose-456",
                    target_type="compose",
                ),
                updated_at="2026-04-22T00:00:00Z",
                source_label="test",
            ),
        )
        target_id_records = (
            DokployTargetIdRecord(
                context="opw",
                instance="prod",
                target_id="compose-123",
                updated_at="2026-04-22T00:00:00Z",
                source_label="test",
            ),
        )

        with self.assertRaises(ValidationError) as raised_error:
            control_plane_dokploy.build_dokploy_source_of_truth_from_records(duplicate_records, target_id_records)

        self.assertIn("Duplicate Dokploy target definition for opw/prod", str(raised_error.exception))

    def test_read_control_plane_dokploy_source_of_truth_rejects_dev_lane_targets(self) -> None:
        with self.assertRaises(ValidationError) as raised_error:
            control_plane_dokploy.build_dokploy_source_of_truth_from_records(
                (
                    control_plane_dokploy.build_dokploy_target_record_from_definition(
                        control_plane_dokploy.DokployTargetDefinition(
                            context="opw",
                            instance="dev",
                            target_id="compose-123",
                            target_type="compose",
                        ),
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    ),
                ),
                (
                    DokployTargetIdRecord(
                        context="opw",
                        instance="dev",
                        target_id="compose-123",
                        updated_at="2026-04-22T00:00:00Z",
                        source_label="test",
                    ),
                ),
            )

        self.assertIn("stable remote instances prod, testing", str(raised_error.exception))
        self.assertIn("opw/dev", str(raised_error.exception))
        self.assertIn("Launchplane preview records", str(raised_error.exception))

    def test_read_dokploy_config_reads_managed_secrets(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(
                    store=store,
                    host="https://dokploy.db.example",
                    token="db-token",
                )
                host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

            store.close()

        self.assertEqual(host, "https://dokploy.db.example")
        self.assertEqual(token, "db-token")

    def test_read_dokploy_config_ignores_repo_env_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            (control_plane_root / ".env").write_text(
                "DOKPLOY_HOST=https://dokploy.control-plane.example\nDOKPLOY_TOKEN=control-plane-token\n",
                encoding="utf-8",
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(
                    store=store,
                    host="https://dokploy.db.example",
                    token="db-token",
                )
                host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

            store.close()

        self.assertEqual(host, "https://dokploy.db.example")
        self.assertEqual(token, "db-token")

    def test_read_dokploy_config_ignores_process_environment_bootstrap(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    "DOKPLOY_HOST": "https://dokploy.process.example",
                    "DOKPLOY_TOKEN": "process-token",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(
                    store=store,
                    host="https://dokploy.db.example",
                    token="db-token",
                )
                host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

            store.close()

        self.assertEqual(host, "https://dokploy.db.example")
        self.assertEqual(token, "db-token")

    def test_read_dokploy_config_fails_closed_with_only_repo_env_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            (control_plane_root / ".env").write_text(
                "DOKPLOY_HOST=https://dokploy.file.example\nDOKPLOY_TOKEN=file-token\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {},
                clear=True,
            ):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertIn("Configure Launchplane-managed Dokploy secrets in the shared store", str(raised_error.exception))

    def test_read_dokploy_config_fails_closed_with_only_process_environment_bootstrap(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)

            with patch.dict(
                os.environ,
                {
                    "DOKPLOY_HOST": "https://dokploy.process.example",
                    "DOKPLOY_TOKEN": "process-token",
                },
                clear=True,
            ):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertIn("Configure Launchplane-managed Dokploy secrets in the shared store", str(raised_error.exception))

    def test_read_control_plane_environment_values_reads_managed_secrets_only(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            (control_plane_root / ".env").write_text(
                "DOKPLOY_HOST=https://dokploy.file.example\nDOKPLOY_TOKEN=file-token\n",
                encoding="utf-8",
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    "DOKPLOY_HOST": "https://dokploy.process.example",
                    "DOKPLOY_TOKEN": "process-token",
                },
                clear=True,
            ):
                _write_dokploy_managed_secrets(
                    store=store,
                    host="https://dokploy.db.example",
                    token="db-token",
                )
                environment_values = control_plane_dokploy.read_control_plane_environment_values(
                    control_plane_root=control_plane_root
                )

            store.close()

        self.assertEqual(environment_values["DOKPLOY_HOST"], "https://dokploy.db.example")
        self.assertEqual(environment_values["DOKPLOY_TOKEN"], "db-token")

    def test_read_dokploy_config_fails_closed_without_control_plane_secret_source(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)

            with patch.dict(
                os.environ,
                {},
                clear=True,
            ):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertIn("DOKPLOY_HOST or DOKPLOY_TOKEN", str(raised_error.exception))

    def test_run_compose_post_deploy_update_applies_explicit_env_file_without_control_plane_secrets(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            env_file = Path(temporary_directory_name) / "post-deploy.env"
            env_file.write_text(
                "\n".join(
                    (
                        "ODOO_DB_NAME=opw_prod",
                        "ODOO_FILESTORE_PATH=/volumes/data/custom-filestore",
                        "DOKPLOY_TOKEN=should-not-sync",
                    )
                ),
                encoding="utf-8",
            )
            target_definition = control_plane_dokploy.DokployTargetDefinition(context="opw", instance="prod",
                                                                              target_id="compose-123",
                                                                              target_name="opw-prod")
            updated_env_payloads: list[str] = []
            schedule_payloads: list[dict[str, object]] = []
            request_paths: list[str] = []

            with patch(
                "control_plane.dokploy.fetch_dokploy_target_payload",
                return_value={
                    "env": "ODOO_DB_NAME=old_db\nODOO_FILESTORE_PATH=/volumes/data/filestore\n",
                    "appName": "opw-prod-app",
                    "serverId": "server-123",
                },
            ), patch(
                "control_plane.dokploy.update_dokploy_target_env",
                side_effect=lambda **kwargs: updated_env_payloads.append(str(kwargs["env_text"])),
            ), patch(
                "control_plane.dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "deployment-before"},
            ), patch(
                "control_plane.dokploy.wait_for_target_deployment",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.dokploy.find_matching_dokploy_schedule",
                return_value=None,
            ), patch(
                "control_plane.dokploy.upsert_dokploy_schedule",
                side_effect=lambda **kwargs: schedule_payloads.append(kwargs["schedule_payload"]) or {"scheduleId": "schedule-123"},
            ), patch(
                "control_plane.dokploy.latest_deployment_for_schedule",
                return_value={"deploymentId": "schedule-before"},
            ), patch(
                "control_plane.dokploy.wait_for_dokploy_schedule_deployment",
                side_effect=lambda **_kwargs: None,
            ), patch(
                "control_plane.dokploy.dokploy_request",
                side_effect=lambda **kwargs: request_paths.append(str(kwargs["path"])) or {"ok": True},
            ):
                control_plane_dokploy.run_compose_post_deploy_update(
                    host="https://dokploy.example.com",
                    token="secret-token",
                    target_definition=target_definition,
                    env_file=env_file,
                )

        self.assertEqual(len(updated_env_payloads), 1)
        self.assertIn("ODOO_DB_NAME=opw_prod", updated_env_payloads[0])
        self.assertIn("ODOO_FILESTORE_PATH=/volumes/data/custom-filestore", updated_env_payloads[0])
        self.assertNotIn("DOKPLOY_TOKEN=should-not-sync", updated_env_payloads[0])
        self.assertEqual(len(schedule_payloads), 1)
        self.assertEqual(schedule_payloads[0]["command"], "control-plane post-deploy update")
        self.assertIn("/api/compose.deploy", request_paths)
        self.assertIn("/api/schedule.runManually", request_paths)

    def test_run_compose_post_deploy_update_requires_database_name(self) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(context="opw", instance="prod",
                                                                          target_id="compose-123",
                                                                          target_name="opw-prod")

        with patch(
            "control_plane.dokploy.fetch_dokploy_target_payload",
            return_value={
                "env": "ODOO_FILESTORE_PATH=/volumes/data/filestore\n",
                "appName": "opw-prod-app",
                "serverId": "server-123",
            },
        ):
            with self.assertRaises(click.ClickException) as raised_error:
                control_plane_dokploy.run_compose_post_deploy_update(
                    host="https://dokploy.example.com",
                    token="secret-token",
                    target_definition=target_definition,
                    env_file=None,
                )

        self.assertIn("ODOO_DB_NAME", str(raised_error.exception))

    def test_run_compose_post_deploy_update_rejects_unsupported_env_overlay_keys(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            env_file = Path(temporary_directory_name) / "post-deploy.env"
            env_file.write_text(
                "\n".join(
                    (
                        "ODOO_DB_NAME=opw_prod",
                        "UNRELATED_RUNTIME_KEY=not-allowed",
                    )
                ),
                encoding="utf-8",
            )
            target_definition = control_plane_dokploy.DokployTargetDefinition(context="opw", instance="prod",
                                                                              target_id="compose-123",
                                                                              target_name="opw-prod")

            with patch(
                "control_plane.dokploy.fetch_dokploy_target_payload",
                return_value={
                    "env": "ODOO_DB_NAME=old_db\n",
                    "appName": "opw-prod-app",
                    "serverId": "server-123",
                },
            ):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.run_compose_post_deploy_update(
                        host="https://dokploy.example.com",
                        token="secret-token",
                        target_definition=target_definition,
                        env_file=env_file,
                    )

        self.assertIn("only supports", str(raised_error.exception))
        self.assertIn("UNRELATED_RUNTIME_KEY", str(raised_error.exception))


class LaunchplaneServiceDeployTests(unittest.TestCase):
    @staticmethod
    def _target_payload(*, env_text: str, custom_git_ssh_key_id: str = "ssh-key-123") -> dict[str, object]:
        return {
            "name": "launchplane",
            "appName": "compose-launchplane",
            "sourceType": "git",
            "customGitUrl": "git@github.com:example/launchplane.git",
            "customGitBranch": "main",
            "customGitSSHKeyId": custom_git_ssh_key_id,
            "composePath": "./docker-compose.yml",
            "composeStatus": "done",
            "env": env_text,
        }

    def test_render_dokploy_env_text_with_overrides_updates_and_removes_keys(self) -> None:
        rendered = control_plane_dokploy.render_dokploy_env_text_with_overrides(
            "KEEP=1\nREMOVE=old\n",
            updates={"ADD": "2"},
            removals=("REMOVE",),
        )

        self.assertEqual(rendered, "KEEP=1\nADD=2")

    def test_build_dokploy_data_workflow_script_injects_workflow_environment(self) -> None:
        script = control_plane_dokploy._build_dokploy_data_workflow_script(
            compose_app_name="opw-prod",
            database_name="opw_prod",
            filestore_path="/volumes/data/filestore",
            clear_stale_lock=False,
            data_workflow_lock_path="/volumes/data/.data_workflow_in_progress",
            workflow_environment_overrides={
                "ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL": "https://opw-prod.example.com",
            },
        )

        self.assertIn(
            "workflow_environment+=(-e ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL=https://opw-prod.example.com)",
            script,
        )
        self.assertIn('"${workflow_environment[@]}"', script)

    def test_service_deploy_dokploy_image_rolls_forward_and_verifies_health(self) -> None:
        runner = CliRunner()
        captured_env_updates: list[dict[str, object]] = []
        captured_trigger_calls: list[dict[str, object]] = []

        with patch(
            "control_plane.dokploy.read_dokploy_config",
            return_value=("https://dokploy.example.com", "token-123"),
        ), patch(
            "control_plane.dokploy.fetch_dokploy_target_payload",
            return_value=self._target_payload(
                env_text=(
                    "DOCKER_IMAGE_REFERENCE=ghcr.io/every/launchplane@sha256:old\n"
                    "LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://launchplane:test@db.internal:5432/launchplane\n"
                    "LAUNCHPLANE_MASTER_ENCRYPTION_KEY=test-key\n"
                    "DOKPLOY_HOST=https://dokploy.example.com\n"
                    "DOKPLOY_TOKEN=token-123\n"
                    "LAUNCHPLANE_POLICY_B64=dGVzdA==\n"
                ),
            ),
        ), patch(
            "control_plane.dokploy.update_dokploy_target_env",
            side_effect=lambda **kwargs: captured_env_updates.append(kwargs),
        ), patch(
            "control_plane.dokploy.latest_deployment_for_target",
            return_value={"deploymentId": "deploy-old"},
        ), patch(
            "control_plane.dokploy.trigger_deployment",
            side_effect=lambda **kwargs: captured_trigger_calls.append(kwargs),
        ), patch(
            "control_plane.dokploy.wait_for_target_deployment",
            return_value="deployment=deploy-new status=done",
        ), patch(
            "control_plane.cli._wait_for_ship_healthcheck",
            return_value=None,
        ):
            result = runner.invoke(
                main,
                [
                    "service",
                    "deploy-dokploy-image",
                    "--target-type",
                    "compose",
                    "--target-id",
                    "compose-123",
                    "--image-reference",
                    "ghcr.io/every/launchplane@sha256:new",
                    "--health-url",
                    "https://launchplane.example.com/v1/health",
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(len(captured_env_updates), 1)
        self.assertEqual(len(captured_trigger_calls), 1)
        self.assertIn("sha256:new", str(captured_env_updates[0]["env_text"]))
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["previous_image_reference"], "ghcr.io/every/launchplane@sha256:old")
        self.assertEqual(payload["deployment_result"], "deployment=deploy-new status=done")
        self.assertEqual(payload["preflight"]["runtime_contract"]["database_host"], "db.internal")
        self.assertTrue(payload["preflight"]["custom_git_ssh_key_configured"])

    def test_service_deploy_dokploy_image_rolls_back_when_health_verification_fails(self) -> None:
        runner = CliRunner()
        captured_env_updates: list[dict[str, object]] = []
        captured_trigger_calls: list[dict[str, object]] = []

        with patch(
            "control_plane.dokploy.read_dokploy_config",
            return_value=("https://dokploy.example.com", "token-123"),
        ), patch(
            "control_plane.dokploy.fetch_dokploy_target_payload",
            side_effect=[
                self._target_payload(
                    env_text=(
                        "DOCKER_IMAGE_REFERENCE=ghcr.io/every/launchplane@sha256:old\n"
                        "LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://launchplane:test@db.internal:5432/launchplane\n"
                        "LAUNCHPLANE_MASTER_ENCRYPTION_KEY=test-key\n"
                        "DOKPLOY_HOST=https://dokploy.example.com\n"
                        "DOKPLOY_TOKEN=token-123\n"
                        "LAUNCHPLANE_POLICY_B64=dGVzdA==\n"
                    ),
                ),
                self._target_payload(
                    env_text=(
                        "DOCKER_IMAGE_REFERENCE=ghcr.io/every/launchplane@sha256:new\n"
                        "LAUNCHPLANE_DATABASE_URL=postgresql+psycopg://launchplane:test@db.internal:5432/launchplane\n"
                        "LAUNCHPLANE_MASTER_ENCRYPTION_KEY=test-key\n"
                        "DOKPLOY_HOST=https://dokploy.example.com\n"
                        "DOKPLOY_TOKEN=token-123\n"
                        "LAUNCHPLANE_POLICY_B64=dGVzdA==\n"
                    ),
                ),
            ],
        ), patch(
            "control_plane.dokploy.update_dokploy_target_env",
            side_effect=lambda **kwargs: captured_env_updates.append(kwargs),
        ), patch(
            "control_plane.dokploy.latest_deployment_for_target",
            side_effect=[
                {"deploymentId": "deploy-old"},
                {"deploymentId": "deploy-new"},
            ],
        ), patch(
            "control_plane.dokploy.trigger_deployment",
            side_effect=lambda **kwargs: captured_trigger_calls.append(kwargs),
        ), patch(
            "control_plane.dokploy.wait_for_target_deployment",
            side_effect=[
                "deployment=deploy-new status=done",
                "deployment=deploy-rollback status=done",
            ],
        ), patch(
            "control_plane.cli._wait_for_ship_healthcheck",
            side_effect=[
                click.ClickException("Healthcheck failed for https://launchplane.example.com/v1/health: http 503"),
                None,
            ],
        ):
            result = runner.invoke(
                main,
                [
                    "service",
                    "deploy-dokploy-image",
                    "--target-type",
                    "compose",
                    "--target-id",
                    "compose-123",
                    "--image-reference",
                    "ghcr.io/every/launchplane@sha256:new",
                    "--health-url",
                    "https://launchplane.example.com/v1/health",
                ],
            )

        self.assertNotEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(len(captured_env_updates), 2)
        self.assertEqual(len(captured_trigger_calls), 2)
        self.assertIn("sha256:new", str(captured_env_updates[0]["env_text"]))
        self.assertIn("sha256:old", str(captured_env_updates[1]["env_text"]))
        self.assertIn("Launchplane service health verification failed", result.output)
        payload_text = result.output.split("Error:", 1)[0].strip()
        payload = json.loads(payload_text) if payload_text else {}
        self.assertEqual(payload.get("status"), "failed")
        self.assertEqual(payload.get("rollback", {}).get("status"), "ok")

    def test_service_inspect_dokploy_target_fails_closed_on_missing_runtime_contract(self) -> None:
        runner = CliRunner()

        with patch(
            "control_plane.dokploy.read_dokploy_config",
            return_value=("https://dokploy.example.com", "token-123"),
        ), patch(
            "control_plane.dokploy.fetch_dokploy_target_payload",
            return_value=self._target_payload(
                env_text="DOCKER_IMAGE_REFERENCE=ghcr.io/example/launchplane@sha256:old\n",
                custom_git_ssh_key_id="",
            ),
        ):
            result = runner.invoke(
                main,
                [
                    "service",
                    "inspect-dokploy-target",
                    "--target-type",
                    "compose",
                    "--target-id",
                    "compose-123",
                ],
            )

        self.assertNotEqual(result.exit_code, 0, msg=result.output)
        payload_text = result.output.split("Error:", 1)[0].strip()
        payload = json.loads(payload_text) if payload_text else {}
        self.assertIn(
            "Dokploy target uses an SSH git remote but has no customGitSSHKeyId configured.",
            payload.get("blockers", []),
        )
        self.assertIn(
            "Launchplane service target is missing LAUNCHPLANE_DATABASE_URL.",
            payload.get("blockers", []),
        )
        self.assertIn(
            "Launchplane service target is missing LAUNCHPLANE_POLICY_* or LAUNCHPLANE_POLICY_FILE. Startup fails closed without an explicit policy input.",
            payload.get("blockers", []),
        )
        self.assertIn("Launchplane service Dokploy target preflight failed", result.output)

    def test_service_deploy_dokploy_image_stops_before_env_change_when_preflight_fails(self) -> None:
        runner = CliRunner()

        with patch(
            "control_plane.dokploy.read_dokploy_config",
            return_value=("https://dokploy.example.com", "token-123"),
        ), patch(
            "control_plane.dokploy.fetch_dokploy_target_payload",
            return_value=self._target_payload(
                env_text=(
                    "LAUNCHPLANE_MASTER_ENCRYPTION_KEY=test-key\n"
                    "DOKPLOY_HOST=https://dokploy.example.com\n"
                    "DOKPLOY_TOKEN=token-123\n"
                ),
            ),
        ), patch(
            "control_plane.dokploy.update_dokploy_target_env",
        ) as update_target_env, patch(
            "control_plane.dokploy.trigger_deployment",
        ) as trigger_deployment:
            result = runner.invoke(
                main,
                [
                    "service",
                    "deploy-dokploy-image",
                    "--target-type",
                    "compose",
                    "--target-id",
                    "compose-123",
                    "--image-reference",
                    "ghcr.io/example/launchplane@sha256:new",
                    "--health-url",
                    "https://launchplane.example.com/v1/health",
                ],
            )

        self.assertNotEqual(result.exit_code, 0, msg=result.output)
        update_target_env.assert_not_called()
        trigger_deployment.assert_not_called()
        self.assertIn("Launchplane service target is missing LAUNCHPLANE_DATABASE_URL.", result.output)
        self.assertIn(
            "Launchplane service target is missing LAUNCHPLANE_POLICY_* or LAUNCHPLANE_POLICY_FILE.",
            result.output,
        )


if __name__ == "__main__":
    unittest.main()
