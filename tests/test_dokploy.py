import base64
import hashlib
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
from control_plane.odoo_instance_overrides import ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
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
    def test_application_log_payload_normalization_redacts_likely_secrets(self) -> None:
        lines = control_plane_dokploy.normalize_dokploy_log_payload(
            {
                "logs": [
                    "started",
                    "RESEND_API_KEY=re_123 Bearer abc.def SMTP_PASSWORD=smtp-secret",
                ]
            }
        )

        self.assertEqual(lines[0], "started")
        self.assertIn("RESEND_API_KEY=[redacted]", lines[1])
        self.assertIn("Bearer [redacted]", lines[1])
        self.assertIn("SMTP_PASSWORD=[redacted]", lines[1])
        self.assertNotIn("re_123", lines[1])
        self.assertNotIn("smtp-secret", lines[1])

    def test_redact_dokploy_log_line_redacts_quoted_secret_fields(self) -> None:
        redacted_line = control_plane_dokploy.redact_dokploy_log_line(
            '{"API_KEY":"super-secret","nested":{"SERVICE_TOKEN":"inner-secret"},"note":"safe"}'
        )

        self.assertEqual(
            redacted_line,
            '{"API_KEY":"[redacted]","nested":{"SERVICE_TOKEN":"[redacted]"},"note":"safe"}',
        )

    def test_fetch_application_logs_calls_dokploy_read_logs_endpoint(self) -> None:
        requests: list[dict[str, object]] = []

        with patch(
            "control_plane.dokploy.dokploy_request",
            side_effect=lambda **kwargs: (
                requests.append(kwargs) or {"logs": "one\ntwo\nTHREE_TOKEN=secret"}
            ),
        ):
            lines = control_plane_dokploy.fetch_dokploy_application_logs(
                host="https://dokploy.example.com",
                token="secret-token",
                application_id="app-123",
                line_count=2,
            )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["path"], "/api/application.readLogs")
        self.assertEqual(
            requests[0]["query"], {"applicationId": "app-123", "tail": 2, "since": "all"}
        )
        self.assertEqual(lines, ("two", "THREE_TOKEN=[redacted]"))

    def test_environments_logs_resolves_tracked_application_and_redacts_output(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "sellyouroutboard-testing"
instance = "testing"
target_id = "app-123"
target_type = "application"
target_name = "syo-testing-app"
""",
            )
            store.close()

            with (
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "secret-token"),
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_target_payload",
                    return_value={"appName": "syo-testing-gfbiqh", "serverId": "server-1"},
                ),
                patch(
                    "control_plane.tracked_target_logs.control_plane_dokploy.fetch_dokploy_application_logs",
                    return_value=("started", "SMTP_PASSWORD=[redacted]"),
                ),
            ):
                result = runner.invoke(
                    main,
                    [
                        "environments",
                        "logs",
                        "--database-url",
                        database_url,
                        "--context",
                        "sellyouroutboard-testing",
                        "--instance",
                        "testing",
                        "--lines",
                        "2",
                    ],
                )

        self.assertEqual(result.exit_code, 0, result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["target"]["target_id"], "app-123")
        self.assertEqual(payload["target"]["target_name"], "syo-testing-app")
        self.assertEqual(payload["target"]["app_name"], "syo-testing-gfbiqh")
        self.assertEqual(payload["logs"]["lines"], ["started", "SMTP_PASSWORD=[redacted]"])
        self.assertNotIn("secret-token", result.output)

    def test_environments_logs_rejects_compose_targets(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "opw"
instance = "testing"
target_id = "compose-123"
target_type = "compose"
target_name = "opw-testing"
""",
            )
            store.close()

            result = runner.invoke(
                main,
                [
                    "environments",
                    "logs",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("application targets only", result.output)

    def test_update_application_env_includes_empty_build_fields_when_missing(self) -> None:
        requests: list[dict[str, object]] = []

        with patch(
            "control_plane.dokploy.dokploy_request",
            side_effect=lambda **kwargs: requests.append(kwargs) or {"ok": True},
        ):
            control_plane_dokploy.update_dokploy_target_env(
                host="https://dokploy.example.com",
                token="secret-token",
                target_type="application",
                target_id="app-123",
                target_payload={"createEnvFile": True, "buildArgs": None, "buildSecrets": None},
                env_text="APP_URL=https://example.com",
            )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0]["path"], "/api/application.saveEnvironment")
        payload = requests[0]["payload"]
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["buildArgs"], "")
        self.assertEqual(payload["buildSecrets"], "")

    def test_dokploy_targets_list_and_show_include_shopify_policy_metadata(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "opw"
instance = "testing"
target_id = "compose-123"
target_type = "compose"
project_name = "opw-testing"
target_name = "opw-testing"

[targets.policies.shopify]
protected_store_keys = ["yps-your-part-supplier"]
""",
            )

            list_result = runner.invoke(
                main, ["dokploy-targets", "list", "--database-url", database_url]
            )
            show_result = runner.invoke(
                main,
                [
                    "dokploy-targets",
                    "show",
                    "--database-url",
                    database_url,
                    "--context",
                    "OPW",
                    "--instance",
                    "Testing",
                ],
            )
            store.close()

        self.assertEqual(list_result.exit_code, 0, msg=list_result.output)
        self.assertEqual(show_result.exit_code, 0, msg=show_result.output)
        list_payload = json.loads(list_result.output)
        show_payload = json.loads(show_result.output)
        self.assertEqual(list_payload["count"], 1)
        self.assertEqual(list_payload["records"][0]["target_id"], "compose-123")
        self.assertEqual(
            list_payload["records"][0]["shopify_protected_store_keys"], ["yps-your-part-supplier"]
        )
        self.assertEqual(show_payload["target_id"], "compose-123")
        self.assertEqual(show_payload["shopify_protected_store_keys"], ["yps-your-part-supplier"])

    def test_dokploy_targets_put_shopify_protected_store_key_updates_record(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "opw"
instance = "testing"
target_id = "compose-123"
target_type = "compose"
""",
            )

            result = runner.invoke(
                main,
                [
                    "dokploy-targets",
                    "put-shopify-protected-store-key",
                    "--database-url",
                    database_url,
                    "--context",
                    "OPW",
                    "--instance",
                    "Testing",
                    "--key",
                    " YPS-Your-Part-Supplier ",
                    "--key",
                    "yps-your-part-supplier",
                    "--source-label",
                    "policy:test",
                ],
            )
            stored_record = store.read_dokploy_target_record(
                context_name="opw", instance_name="testing"
            )
            store.close()

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["added_keys"], ["yps-your-part-supplier"])
        self.assertEqual(payload["already_present_keys"], ["yps-your-part-supplier"])
        self.assertEqual(
            payload["record"]["shopify_protected_store_keys"], ["yps-your-part-supplier"]
        )
        self.assertEqual(
            stored_record.policies.shopify.protected_store_keys, ("yps-your-part-supplier",)
        )
        self.assertEqual(stored_record.source_label, "policy:test")

    def test_dokploy_targets_unset_shopify_protected_store_key_reports_missing_keys(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            _seed_dokploy_target_records(
                store=store,
                payload="""
schema_version = 2

[[targets]]
context = "opw"
instance = "testing"
target_id = "compose-123"
target_type = "compose"

[targets.policies.shopify]
protected_store_keys = ["yps-your-part-supplier", "spare-store"]
""",
            )

            result = runner.invoke(
                main,
                [
                    "dokploy-targets",
                    "unset-shopify-protected-store-key",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--key",
                    "yps-your-part-supplier",
                    "--key",
                    "missing-store",
                ],
            )
            stored_record = store.read_dokploy_target_record(
                context_name="opw", instance_name="testing"
            )
            store.close()

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["removed_keys"], ["yps-your-part-supplier"])
        self.assertEqual(payload["missing_keys"], ["missing-store"])
        self.assertEqual(payload["record"]["shopify_protected_store_keys"], ["spare-store"])
        self.assertEqual(stored_record.policies.shopify.protected_store_keys, ("spare-store",))

    def test_dokploy_targets_put_shopify_protected_store_key_requires_existing_record(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            database_url = _sqlite_database_url(
                Path(temporary_directory_name) / "launchplane.sqlite3"
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            store.close()

            result = runner.invoke(
                main,
                [
                    "dokploy-targets",
                    "put-shopify-protected-store-key",
                    "--database-url",
                    database_url,
                    "--context",
                    "opw",
                    "--instance",
                    "testing",
                    "--key",
                    "yps-your-part-supplier",
                ],
            )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Missing DB-backed tracked Dokploy target record", result.output)

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
                    "schema_version = 1\n\n"
                    "[shared_env]\n"
                    'LAUNCHPLANE_PREVIEW_BASE_URL = "https://launchplane.file.example.com"\n'
                ),
                encoding="utf-8",
            )
            (config_directory / "dokploy.toml").write_text(
                (
                    "schema_version = 2\n\n"
                    "[[targets]]\n"
                    'context = "cm"\n'
                    'instance = "prod"\n'
                    'target_id = "compose-file"\n'
                ),
                encoding="utf-8",
            )
            (config_directory / "dokploy-targets.toml").write_text(
                (
                    "schema_version = 1\n\n"
                    "[[targets]]\n"
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

        with (
            patch(
                "control_plane.dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=source_of_truth,
            ),
            patch(
                "control_plane.dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
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
            ),
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
        self.assertEqual(
            payload["live_target"]["custom_git_url"], "git@github.com:cbusillo/odoo-ai.git"
        )
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

        with (
            patch(
                "control_plane.dokploy.read_control_plane_dokploy_source_of_truth",
                return_value=source_of_truth,
            ),
            patch(
                "control_plane.dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.dokploy.fetch_dokploy_target_payload",
                side_effect=fetch_payloads,
            ),
            patch(
                "control_plane.dokploy.update_dokploy_target_source",
                side_effect=lambda **kwargs: captured_source_updates.append(kwargs),
            ),
            patch(
                "control_plane.dokploy.update_dokploy_target_env",
                side_effect=lambda **kwargs: captured_env_updates.append(kwargs),
            ),
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
        self.assertIn(
            "411f6b8e85cac72dc7aa2e2dc5540001043c327d", str(captured_env_updates[0]["env_text"])
        )
        payload = json.loads(result.output)
        self.assertTrue(payload["artifact_runtime_contract"]["artifact_ready"])
        self.assertEqual(
            payload["live_target"]["custom_git_url"], "git@github.com:cbusillo/odoo-devkit.git"
        )
        self.assertEqual(
            payload["sync_preview"]["source_changes"]["custom_git_url"]["tracked"],
            "git@github.com:cbusillo/odoo-devkit.git",
        )

    def test_apply_live_target_dry_run_reports_runtime_key_delta_without_values(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context="sellyouroutboard-testing",
                        instance="prod",
                        env={"CONTACT_EMAIL_MODE": "runtime-private-value"},
                        updated_at="2026-05-01T00:00:00Z",
                        source_label="test",
                    )
                )
                _seed_dokploy_target_records(
                    store=store,
                    payload="""
schema_version = 2

[[targets]]
context = "sellyouroutboard-testing"
instance = "prod"
target_id = "application-syo-prod"
target_type = "application"
target_name = "syo-prod-app"

[targets.env]
TRACKED_ONLY = "tracked-private-value"
""",
                )
                with patch.dict(
                    os.environ,
                    {
                        "LAUNCHPLANE_DATABASE_URL": database_url,
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    },
                    clear=True,
                ):
                    control_plane_secrets.write_secret_value(
                        record_store=store,
                        scope="context_instance",
                        integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                        name="smtp-password",
                        plaintext_value="smtp-secret-value",
                        binding_key="SMTP_PASSWORD",
                        context_name="sellyouroutboard-testing",
                        instance_name="prod",
                        actor="test",
                    )
            finally:
                store.close()

            with (
                patch.dict(
                    os.environ,
                    {
                        "LAUNCHPLANE_DATABASE_URL": database_url,
                        control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    },
                    clear=True,
                ),
                patch(
                    "control_plane.dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.dokploy.fetch_dokploy_target_payload",
                    return_value={
                        "applicationId": "application-syo-prod",
                        "name": "syo-prod-app",
                        "env": "CONTACT_EMAIL_MODE=old-value\nEXISTING=value\n",
                    },
                ),
                patch("control_plane.dokploy.update_dokploy_target_env") as update_env,
            ):
                result = runner.invoke(
                    main,
                    [
                        "environments",
                        "apply-live-target",
                        "--context",
                        "sellyouroutboard-testing",
                        "--instance",
                        "prod",
                        "--dry-run",
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        update_env.assert_not_called()
        self.assertNotIn("runtime-private-value", result.output)
        self.assertNotIn("tracked-private-value", result.output)
        self.assertNotIn("smtp-secret-value", result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "dry-run")
        self.assertEqual(payload["tracked_target"]["target_type"], "application")
        self.assertEqual(payload["runtime_environment"]["desired_key_count"], 3)
        self.assertEqual(payload["runtime_environment"]["different_keys"], ["CONTACT_EMAIL_MODE"])
        self.assertEqual(
            payload["runtime_environment"]["missing_keys"], ["SMTP_PASSWORD", "TRACKED_ONLY"]
        )
        self.assertFalse(payload["apply"]["env_updated"])
        self.assertFalse(payload["deploy"]["triggered"])

    def test_apply_live_target_updates_runtime_env_and_verifies_without_values(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context="sellyouroutboard-testing",
                        instance="prod",
                        env={"CONTACT_EMAIL_MODE": "smtp"},
                        updated_at="2026-05-01T00:00:00Z",
                        source_label="test",
                    )
                )
                _seed_dokploy_target_records(
                    store=store,
                    payload="""
schema_version = 2

[[targets]]
context = "sellyouroutboard-testing"
instance = "prod"
target_id = "application-syo-prod"
target_type = "application"
target_name = "syo-prod-app"
deploy_timeout_seconds = 77
""",
                )
            finally:
                store.close()

            captured_env_updates: list[dict[str, object]] = []

            def fetch_target_payload(**_kwargs: object) -> dict[str, object]:
                env_text = "CONTACT_EMAIL_MODE=old\nEXISTING=value\n"
                if captured_env_updates:
                    env_text = str(captured_env_updates[-1]["env_text"])
                return {
                    "applicationId": "application-syo-prod",
                    "name": "syo-prod-app",
                    "env": env_text,
                }

            with (
                patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True),
                patch(
                    "control_plane.dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.dokploy.fetch_dokploy_target_payload",
                    side_effect=fetch_target_payload,
                ),
                patch(
                    "control_plane.dokploy.update_dokploy_target_env",
                    side_effect=lambda **kwargs: captured_env_updates.append(kwargs),
                ),
            ):
                result = runner.invoke(
                    main,
                    [
                        "environments",
                        "apply-live-target",
                        "--context",
                        "sellyouroutboard-testing",
                        "--instance",
                        "prod",
                        "--apply",
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(len(captured_env_updates), 1)
        env_text = str(captured_env_updates[0]["env_text"])
        self.assertIn("CONTACT_EMAIL_MODE=smtp", env_text)
        self.assertIn("EXISTING=value", env_text)
        self.assertNotIn("CONTACT_EMAIL_MODE=old", result.output)
        self.assertNotIn("CONTACT_EMAIL_MODE=smtp", result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["mode"], "apply")
        self.assertTrue(payload["apply"]["env_updated"])
        self.assertEqual(payload["apply"]["verification"]["status"], "pass")
        self.assertFalse(payload["deploy"]["triggered"])

    def test_apply_live_target_deploy_is_explicit(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                store.write_runtime_environment_record(
                    RuntimeEnvironmentRecord(
                        scope="instance",
                        context="sellyouroutboard-testing",
                        instance="prod",
                        env={"CONTACT_EMAIL_MODE": "smtp"},
                        updated_at="2026-05-01T00:00:00Z",
                        source_label="test",
                    )
                )
                _seed_dokploy_target_records(
                    store=store,
                    payload="""
schema_version = 2

[[targets]]
context = "sellyouroutboard-testing"
instance = "prod"
target_id = "application-syo-prod"
target_type = "application"
target_name = "syo-prod-app"
deploy_timeout_seconds = 77
""",
                )
            finally:
                store.close()

            with (
                patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True),
                patch(
                    "control_plane.dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.dokploy.fetch_dokploy_target_payload",
                    return_value={
                        "applicationId": "application-syo-prod",
                        "name": "syo-prod-app",
                        "env": "CONTACT_EMAIL_MODE=smtp\n",
                    },
                ),
                patch(
                    "control_plane.dokploy.latest_deployment_for_target",
                    return_value={"deploymentId": "before"},
                ),
                patch("control_plane.dokploy.trigger_deployment") as trigger_deployment,
                patch(
                    "control_plane.dokploy.wait_for_target_deployment",
                    return_value="deployment=after status=done",
                ) as wait_for_target_deployment,
            ):
                result = runner.invoke(
                    main,
                    [
                        "environments",
                        "apply-live-target",
                        "--context",
                        "sellyouroutboard-testing",
                        "--instance",
                        "prod",
                        "--apply",
                        "--deploy",
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        trigger_deployment.assert_called_once()
        self.assertEqual(trigger_deployment.call_args.kwargs["target_type"], "application")
        wait_for_target_deployment.assert_called_once()
        self.assertEqual(wait_for_target_deployment.call_args.kwargs["timeout_seconds"], 77)
        payload = json.loads(result.output)
        self.assertFalse(payload["apply"]["env_updated"])
        self.assertTrue(payload["deploy"]["triggered"])
        self.assertEqual(
            payload["deploy"]["result"]["deployment_result"], "deployment=after status=done"
        )

    def test_apply_live_target_rejects_deploy_options_for_dry_run(self) -> None:
        runner = CliRunner()
        result = runner.invoke(
            main,
            [
                "environments",
                "apply-live-target",
                "--context",
                "sellyouroutboard-testing",
                "--instance",
                "prod",
                "--dry-run",
                "--deploy",
            ],
        )

        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Deploy options require --apply", result.output)

    def test_read_control_plane_dokploy_source_of_truth_prefers_postgres_target_ids_without_file_fallback(
        self,
    ) -> None:
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
            [
                (target.context, target.instance, target.target_id)
                for target in source_of_truth.targets
            ],
            [("cm", "testing", "compose-cm-db"), ("opw", "prod", "compose-db")],
        )

    def test_read_control_plane_dokploy_source_of_truth_requires_database_target_ids_without_file_fallback(
        self,
    ) -> None:
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

        self.assertIn(
            "Missing DB-backed Dokploy target-id record for opw/prod", str(raised_error.exception)
        )

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

    def test_read_control_plane_dokploy_source_of_truth_preserves_target_policies(self) -> None:
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

[targets.policies.shopify]
protected_store_keys = ["yps-your-part-supplier"]
""",
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
                source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
                    control_plane_root=control_plane_root
                )

            store.close()

        self.assertEqual(
            source_of_truth.targets[0].policies.shopify.protected_store_keys,
            ("yps-your-part-supplier",),
        )

    def test_read_control_plane_dokploy_source_of_truth_fails_closed_when_target_id_missing(
        self,
    ) -> None:
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

        self.assertIn(
            "Missing DB-backed Dokploy target-id record for opw/prod", str(raised_error.exception)
        )

    def test_read_control_plane_dokploy_source_of_truth_rejects_duplicate_context_instance_targets(
        self,
    ) -> None:
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
            control_plane_dokploy.build_dokploy_source_of_truth_from_records(
                duplicate_records, target_id_records
            )

        self.assertIn(
            "Duplicate Dokploy target definition for opw/prod", str(raised_error.exception)
        )

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
                host, token = control_plane_dokploy.read_dokploy_config(
                    control_plane_root=control_plane_root
                )

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
                host, token = control_plane_dokploy.read_dokploy_config(
                    control_plane_root=control_plane_root
                )

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
                host, token = control_plane_dokploy.read_dokploy_config(
                    control_plane_root=control_plane_root
                )

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

        self.assertIn(
            "Configure Launchplane-managed Dokploy secrets in the shared store",
            str(raised_error.exception),
        )

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

        self.assertIn(
            "Configure Launchplane-managed Dokploy secrets in the shared store",
            str(raised_error.exception),
        )

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

    def test_run_compose_post_deploy_update_applies_explicit_env_file_without_control_plane_secrets(
        self,
    ) -> None:
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
            target_definition = control_plane_dokploy.DokployTargetDefinition(
                context="opw", instance="prod", target_id="compose-123", target_name="opw-prod"
            )
            updated_env_payloads: list[str] = []
            schedule_payloads: list[dict[str, object]] = []
            request_paths: list[str] = []

            with (
                patch(
                    "control_plane.dokploy.fetch_dokploy_target_payload",
                    return_value={
                        "env": "ODOO_DB_NAME=old_db\nODOO_FILESTORE_PATH=/volumes/data/filestore\n",
                        "appName": "opw-prod-app",
                        "serverId": "server-123",
                    },
                ),
                patch(
                    "control_plane.dokploy.update_dokploy_target_env",
                    side_effect=lambda **kwargs: updated_env_payloads.append(
                        str(kwargs["env_text"])
                    ),
                ),
                patch(
                    "control_plane.dokploy.latest_deployment_for_target",
                    return_value={"deploymentId": "deployment-before"},
                ),
                patch(
                    "control_plane.dokploy.wait_for_target_deployment",
                    side_effect=lambda **_kwargs: None,
                ),
                patch(
                    "control_plane.dokploy.find_matching_dokploy_schedule",
                    return_value=None,
                ),
                patch(
                    "control_plane.dokploy.upsert_dokploy_schedule",
                    side_effect=lambda **kwargs: (
                        schedule_payloads.append(kwargs["schedule_payload"])
                        or {"scheduleId": "schedule-123"}
                    ),
                ),
                patch(
                    "control_plane.dokploy.latest_deployment_for_schedule",
                    return_value={"deploymentId": "schedule-before"},
                ),
                patch(
                    "control_plane.dokploy.wait_for_dokploy_schedule_deployment",
                    side_effect=lambda **_kwargs: None,
                ),
                patch(
                    "control_plane.dokploy.dokploy_request",
                    side_effect=lambda **kwargs: (
                        request_paths.append(str(kwargs["path"])) or {"ok": True}
                    ),
                ),
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
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="opw", instance="prod", target_id="compose-123", target_name="opw-prod"
        )

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

    def test_run_compose_odoo_backup_gate_uses_manual_schedule_with_consistency_script(
        self,
    ) -> None:
        target_definition = control_plane_dokploy.DokployTargetDefinition(
            context="cm", instance="prod", target_id="compose-123", target_name="cm-prod"
        )
        schedule_payloads: list[dict[str, object]] = []
        request_paths: list[str] = []

        with (
            patch(
                "control_plane.dokploy.fetch_dokploy_target_payload",
                return_value={"appName": "cm-prod-app", "serverId": "server-123"},
            ),
            patch(
                "control_plane.dokploy.upsert_dokploy_schedule",
                side_effect=lambda **kwargs: (
                    schedule_payloads.append(kwargs["schedule_payload"])
                    or {"scheduleId": "schedule-123"}
                ),
            ),
            patch(
                "control_plane.dokploy.latest_deployment_for_schedule",
                return_value={"deploymentId": "schedule-before"},
            ),
            patch(
                "control_plane.dokploy.wait_for_dokploy_schedule_deployment",
                side_effect=lambda **_kwargs: None,
            ),
            patch(
                "control_plane.dokploy.dokploy_request",
                side_effect=lambda **kwargs: (
                    request_paths.append(str(kwargs["path"])) or {"ok": True}
                ),
            ),
        ):
            control_plane_dokploy.run_compose_odoo_backup_gate(
                host="https://dokploy.example.com",
                token="secret-token",
                target_definition=target_definition,
                backup_record_id="backup-gate-cm-prod-1",
                database_name="cm",
                filestore_path="/volumes/data/filestore",
                backup_root="/volumes/data/backups/launchplane",
            )

        self.assertEqual(len(schedule_payloads), 1)
        self.assertEqual(
            schedule_payloads[0]["name"],
            control_plane_dokploy.DOKPLOY_ODOO_BACKUP_GATE_SCHEDULE_NAME,
        )
        self.assertEqual(schedule_payloads[0]["command"], "control-plane odoo backup gate")
        script = str(schedule_payloads[0]["script"])
        self.assertIn("docker stop", script)
        self.assertIn("pg_dump", script)
        self.assertIn("tar -C", script)
        self.assertIn("manifest.json", script)
        self.assertIn("/api/schedule.runManually", request_paths)

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
            target_definition = control_plane_dokploy.DokployTargetDefinition(
                context="opw", instance="prod", target_id="compose-123", target_name="opw-prod"
            )

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
    def _target_payload(
        *, env_text: str, custom_git_ssh_key_id: str = "ssh-key-123"
    ) -> dict[str, object]:
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

    def test_render_odoo_raw_compose_file_pins_artifact_image_and_services(self) -> None:
        compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123"
        )

        self.assertIn("image: ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123", compose_file)
        self.assertIn("\n  web:", compose_file)
        self.assertIn("\n  database:", compose_file)
        self.assertIn("\n  script-runner:", compose_file)
        self.assertIn("name: ${ODOO_PROJECT_NAME:-odoo}", compose_file)

    def test_sync_dokploy_compose_raw_source_updates_and_verifies_hash(self) -> None:
        compose_file = control_plane_dokploy.render_odoo_raw_compose_file(
            image_reference="ghcr.io/cbusillo/odoo-tenant-cm@sha256:abc123"
        )
        update_payloads: list[dict[str, object]] = []

        def fake_dokploy_request(**kwargs: object) -> dict[str, object]:
            update_payloads.append(dict(kwargs))
            return {"status": "ok"}

        with (
            patch("control_plane.dokploy.dokploy_request", side_effect=fake_dokploy_request),
            patch(
                "control_plane.dokploy.fetch_dokploy_target_payload",
                return_value={
                    "name": "cm-testing",
                    "environmentId": "env-123",
                    "sourceType": "raw",
                    "composePath": "docker-compose.yml",
                    "composeFile": compose_file,
                },
            ),
        ):
            evidence = control_plane_dokploy.sync_dokploy_compose_raw_source(
                host="https://dokploy.example.com",
                token="token-123",
                compose_id="compose-123",
                compose_name="cm-testing",
                target_payload={
                    "name": "cm-testing",
                    "environmentId": "env-123",
                    "sourceType": "git",
                    "composeFile": "",
                    "autoDeploy": False,
                },
                compose_file=compose_file,
            )

        self.assertEqual(len(update_payloads), 1)
        payload = update_payloads[0]["payload"]
        self.assertIsInstance(payload, dict)
        self.assertEqual(payload["sourceType"], "raw")
        self.assertEqual(payload["composePath"], "docker-compose.yml")
        self.assertEqual(payload["composeFile"], compose_file)
        self.assertEqual(evidence["source_type"], "raw")
        self.assertEqual(evidence["compose_path"], "docker-compose.yml")
        self.assertEqual(
            evidence["compose_sha256"], control_plane_dokploy.compose_file_sha256(compose_file)
        )
        self.assertEqual(evidence["changed"], "true")

    def test_service_render_authz_policy_uses_explicit_policy_source(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            policy_dir = control_plane_root / "config"
            policy_dir.mkdir(parents=True)
            policy_file = policy_dir / "launchplane-authz.toml"
            policy_text = """
schema_version = 1

[[github_actions]]
repository = "cbusillo/launchplane"
workflow_refs = ["cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"]
event_names = ["workflow_dispatch"]
products = ["launchplane"]
contexts = ["launchplane"]
actions = ["launchplane_service_deploy.execute"]
""".strip()
            policy_file.write_text(policy_text, encoding="utf-8")

            result = runner.invoke(
                main,
                [
                    "service",
                    "render-authz-policy",
                    "--policy-file",
                    str(policy_file),
                    "--control-plane-root",
                    str(control_plane_root),
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["policy_file"], str(policy_file))
        self.assertEqual(
            payload["policy_b64"],
            base64.b64encode(policy_text.encode("utf-8")).decode("ascii"),
        )
        self.assertEqual(
            payload["policy_sha256"], hashlib.sha256(policy_text.encode("utf-8")).hexdigest()
        )
        self.assertEqual(payload["github_actions_rule_count"], 1)

    def test_service_sync_bootstrap_policy_updates_live_target_env(self) -> None:
        runner = CliRunner()
        captured_env_updates: list[dict[str, object]] = []
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            policy_dir = control_plane_root / "config"
            policy_dir.mkdir(parents=True)
            policy_file = policy_dir / "launchplane-authz.toml"
            policy_text = """
schema_version = 1

[[github_actions]]
repository = "cbusillo/launchplane"
workflow_refs = ["cbusillo/launchplane/.github/workflows/deploy-launchplane.yml@refs/heads/main"]
event_names = ["workflow_dispatch"]
products = ["launchplane"]
contexts = ["launchplane"]
actions = ["launchplane_service_deploy.execute"]
""".strip()
            policy_file.write_text(policy_text, encoding="utf-8")
            policy_b64 = base64.b64encode(policy_text.encode("utf-8")).decode("ascii")

            with (
                patch(
                    "control_plane.dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.com", "token-123"),
                ),
                patch(
                    "control_plane.dokploy.fetch_dokploy_target_payload",
                    return_value=self._target_payload(
                        env_text=(
                            "DOCKER_IMAGE_REFERENCE=ghcr.io/every/launchplane@sha256:old\n"
                            "LAUNCHPLANE_POLICY_B64=dGVzdA==\n"
                            "LAUNCHPLANE_POLICY_TOML=schema_version = 1\n"
                            "LAUNCHPLANE_POLICY_FILE=/etc/launchplane/policy.toml\n"
                        ),
                    ),
                ),
                patch(
                    "control_plane.dokploy.update_dokploy_target_env",
                    side_effect=lambda **kwargs: captured_env_updates.append(kwargs),
                ),
            ):
                result = runner.invoke(
                    main,
                    [
                        "service",
                        "sync-bootstrap-policy",
                        "--target-type",
                        "compose",
                        "--target-id",
                        "compose-123",
                        "--policy-file",
                        str(policy_file),
                        "--control-plane-root",
                        str(control_plane_root),
                        "--apply",
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(len(captured_env_updates), 1)
        self.assertIn(f"LAUNCHPLANE_POLICY_B64={policy_b64}", captured_env_updates[0]["env_text"])
        self.assertNotIn("LAUNCHPLANE_POLICY_TOML=", captured_env_updates[0]["env_text"])
        self.assertNotIn("LAUNCHPLANE_POLICY_FILE=", captured_env_updates[0]["env_text"])
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertTrue(payload["changed"])
        self.assertEqual(payload["desired_policy_file"], str(policy_file))
        self.assertEqual(
            payload["desired_policy_sha256"],
            hashlib.sha256(policy_text.encode("utf-8")).hexdigest(),
        )

    def test_build_dokploy_data_workflow_script_injects_workflow_environment(self) -> None:
        script = control_plane_dokploy._build_dokploy_data_workflow_script(
            compose_app_name="opw-prod",
            database_name="opw_prod",
            filestore_path="/volumes/data/filestore",
            clear_stale_lock=False,
            data_workflow_lock_path="/volumes/data/.data_workflow_in_progress",
            workflow_environment_overrides={
                ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY: "payload-value",
                "EXTRA_WORKFLOW_VALUE": "https://opw-prod.example.com",
            },
            required_workflow_environment_keys=("ODOO_OVERRIDE_SECRET__ADDON__SHOPIFY__API_TOKEN",),
            protected_shopify_store_keys=("yps-your-part-supplier",),
        )

        self.assertIn(
            f"workflow_environment+=(-e {ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY}=payload-value)",
            script,
        )
        self.assertIn(
            "workflow_environment+=(-e EXTRA_WORKFLOW_VALUE=https://opw-prod.example.com)",
            script,
        )
        self.assertIn(
            "required_workflow_environment_keys+=(ODOO_OVERRIDE_SECRET__ADDON__SHOPIFY__API_TOKEN)",
            script,
        )
        self.assertIn("protected_shopify_store_keys+=(yps-your-part-supplier)", script)
        self.assertIn("Missing required Odoo override environment key", script)
        self.assertIn("Protected Shopify store key is not allowed on this Dokploy lane.", script)
        self.assertIn(
            'if [ "${exit_status}" -eq 0 ] && [ "${restart_web_on_success}" = "1" ]', script
        )
        self.assertIn('"${workflow_environment[@]}"', script)

    def test_service_deploy_dokploy_image_rolls_forward_and_verifies_health(self) -> None:
        runner = CliRunner()
        captured_env_updates: list[dict[str, object]] = []
        captured_trigger_calls: list[dict[str, object]] = []

        with (
            patch(
                "control_plane.dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
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
            ),
            patch(
                "control_plane.dokploy.update_dokploy_target_env",
                side_effect=lambda **kwargs: captured_env_updates.append(kwargs),
            ),
            patch(
                "control_plane.dokploy.latest_deployment_for_target",
                return_value={"deploymentId": "deploy-old"},
            ),
            patch(
                "control_plane.dokploy.trigger_deployment",
                side_effect=lambda **kwargs: captured_trigger_calls.append(kwargs),
            ),
            patch(
                "control_plane.dokploy.wait_for_target_deployment",
                return_value="deployment=deploy-new status=done",
            ),
            patch(
                "control_plane.cli._wait_for_ship_healthcheck",
                return_value=None,
            ),
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
        self.assertEqual(
            payload["previous_image_reference"], "ghcr.io/every/launchplane@sha256:old"
        )
        self.assertEqual(payload["deployment_result"], "deployment=deploy-new status=done")
        self.assertEqual(payload["preflight"]["runtime_contract"]["database_host"], "db.internal")
        self.assertTrue(payload["preflight"]["custom_git_ssh_key_configured"])

    def test_service_deploy_dokploy_image_rolls_back_when_health_verification_fails(self) -> None:
        runner = CliRunner()
        captured_env_updates: list[dict[str, object]] = []
        captured_trigger_calls: list[dict[str, object]] = []

        with (
            patch(
                "control_plane.dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
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
            ),
            patch(
                "control_plane.dokploy.update_dokploy_target_env",
                side_effect=lambda **kwargs: captured_env_updates.append(kwargs),
            ),
            patch(
                "control_plane.dokploy.latest_deployment_for_target",
                side_effect=[
                    {"deploymentId": "deploy-old"},
                    {"deploymentId": "deploy-new"},
                ],
            ),
            patch(
                "control_plane.dokploy.trigger_deployment",
                side_effect=lambda **kwargs: captured_trigger_calls.append(kwargs),
            ),
            patch(
                "control_plane.dokploy.wait_for_target_deployment",
                side_effect=[
                    "deployment=deploy-new status=done",
                    "deployment=deploy-rollback status=done",
                ],
            ),
            patch(
                "control_plane.cli._wait_for_ship_healthcheck",
                side_effect=[
                    click.ClickException(
                        "Healthcheck failed for https://launchplane.example.com/v1/health: http 503"
                    ),
                    None,
                ],
            ),
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

        with (
            patch(
                "control_plane.dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.dokploy.fetch_dokploy_target_payload",
                return_value=self._target_payload(
                    env_text="DOCKER_IMAGE_REFERENCE=ghcr.io/example/launchplane@sha256:old\n",
                    custom_git_ssh_key_id="",
                ),
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

    def test_service_deploy_dokploy_image_stops_before_env_change_when_preflight_fails(
        self,
    ) -> None:
        runner = CliRunner()

        with (
            patch(
                "control_plane.dokploy.read_dokploy_config",
                return_value=("https://dokploy.example.com", "token-123"),
            ),
            patch(
                "control_plane.dokploy.fetch_dokploy_target_payload",
                return_value=self._target_payload(
                    env_text=(
                        "LAUNCHPLANE_MASTER_ENCRYPTION_KEY=test-key\n"
                        "DOKPLOY_HOST=https://dokploy.example.com\n"
                        "DOKPLOY_TOKEN=token-123\n"
                    ),
                ),
            ),
            patch(
                "control_plane.dokploy.update_dokploy_target_env",
            ) as update_target_env,
            patch(
                "control_plane.dokploy.trigger_deployment",
            ) as trigger_deployment,
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
                    "ghcr.io/example/launchplane@sha256:new",
                    "--health-url",
                    "https://launchplane.example.com/v1/health",
                ],
            )

        self.assertNotEqual(result.exit_code, 0, msg=result.output)
        update_target_env.assert_not_called()
        trigger_deployment.assert_not_called()
        self.assertIn(
            "Launchplane service target is missing LAUNCHPLANE_DATABASE_URL.", result.output
        )
        self.assertIn(
            "Launchplane service target is missing LAUNCHPLANE_POLICY_* or LAUNCHPLANE_POLICY_FILE.",
            result.output,
        )


if __name__ == "__main__":
    unittest.main()
