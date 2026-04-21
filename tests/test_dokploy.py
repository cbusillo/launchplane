import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click
from click.testing import CliRunner

from control_plane import dokploy as control_plane_dokploy
from control_plane.cli import main


class DokployConfigTests(unittest.TestCase):
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

    def test_read_control_plane_dokploy_source_of_truth_merges_operator_local_target_ids(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            source_file = control_plane_root / "config" / "dokploy.toml"
            target_ids_file = control_plane_root / "config" / "dokploy-targets.toml"
            source_file.parent.mkdir(parents=True, exist_ok=True)
            source_file.write_text(
                """
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_type = "compose"
""".strip(),
                encoding="utf-8",
            )
            target_ids_file.write_text(
                """
schema_version = 1

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
""".strip(),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
                    control_plane_root=control_plane_root
                )

        self.assertEqual(len(source_of_truth.targets), 1)
        self.assertEqual(source_of_truth.targets[0].target_id, "compose-123")

    def test_read_control_plane_dokploy_source_of_truth_prefers_explicit_source_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            explicit_source_file = control_plane_root / "tmp" / "dokploy.toml"
            explicit_source_file.parent.mkdir(parents=True, exist_ok=True)
            explicit_source_file.write_text(
                """
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
target_type = "compose"
""".strip(),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(explicit_source_file),
                },
                clear=True,
            ):
                source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
                    control_plane_root=control_plane_root
                )

        self.assertEqual(len(source_of_truth.targets), 1)
        self.assertEqual(source_of_truth.targets[0].target_id, "compose-123")

    def test_read_control_plane_dokploy_source_of_truth_fails_closed_when_target_id_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            explicit_source_file = control_plane_root / "tmp" / "dokploy.toml"
            explicit_source_file.parent.mkdir(parents=True, exist_ok=True)
            explicit_source_file.write_text(
                """
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_type = "compose"
""".strip(),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(explicit_source_file),
                },
                clear=True,
            ):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_control_plane_dokploy_source_of_truth(control_plane_root=control_plane_root)

        self.assertIn("requires non-empty target_id", str(raised_error.exception))

    def test_read_control_plane_dokploy_source_of_truth_fails_closed_when_explicit_target_id_catalog_is_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            source_file = control_plane_root / "config" / "dokploy.toml"
            missing_target_ids_file = control_plane_root / "tmp" / "dokploy-targets.toml"
            source_file.parent.mkdir(parents=True, exist_ok=True)
            source_file.write_text(
                """
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_type = "compose"
""".strip(),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    control_plane_dokploy.CONTROL_PLANE_DOKPLOY_TARGET_IDS_FILE_ENV_VAR: str(missing_target_ids_file),
                },
                clear=True,
            ):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_control_plane_dokploy_source_of_truth(control_plane_root=control_plane_root)

        self.assertIn("Dokploy target-id catalog file not found", str(raised_error.exception))

    def test_read_control_plane_dokploy_source_of_truth_rejects_duplicate_context_instance_targets(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            explicit_source_file = control_plane_root / "tmp" / "dokploy.toml"
            explicit_source_file.parent.mkdir(parents=True, exist_ok=True)
            explicit_source_file.write_text(
                """
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-123"
target_type = "compose"

[[targets]]
context = "opw"
instance = "prod"
target_id = "compose-456"
target_type = "compose"
""".strip(),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(explicit_source_file),
                },
                clear=True,
            ):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_control_plane_dokploy_source_of_truth(control_plane_root=control_plane_root)

        self.assertIn("Duplicate Dokploy target definition for opw/prod", str(raised_error.exception))

    def test_read_control_plane_dokploy_source_of_truth_rejects_dev_lane_targets(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            explicit_source_file = control_plane_root / "tmp" / "dokploy.toml"
            explicit_source_file.parent.mkdir(parents=True, exist_ok=True)
            explicit_source_file.write_text(
                """
schema_version = 2

[[targets]]
context = "opw"
instance = "dev"
target_id = "compose-123"
target_type = "compose"
""".strip(),
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    control_plane_dokploy.CONTROL_PLANE_DOKPLOY_SOURCE_FILE_ENV_VAR: str(explicit_source_file),
                },
                clear=True,
            ):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_control_plane_dokploy_source_of_truth(control_plane_root=control_plane_root)

        self.assertIn("stable remote instances prod, testing", str(raised_error.exception))
        self.assertIn("opw/dev", str(raised_error.exception))
        self.assertIn("Harbor preview records", str(raised_error.exception))

    def test_read_control_plane_dokploy_source_of_truth_rejects_unknown_target_id_override_routes(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            source_file = control_plane_root / "config" / "dokploy.toml"
            target_ids_file = control_plane_root / "config" / "dokploy-targets.toml"
            source_file.parent.mkdir(parents=True, exist_ok=True)
            source_file.write_text(
                """
schema_version = 2

[[targets]]
context = "opw"
instance = "prod"
target_type = "compose"
""".strip(),
                encoding="utf-8",
            )
            target_ids_file.write_text(
                """
schema_version = 1

[[targets]]
context = "cm"
instance = "prod"
target_id = "compose-456"
""".strip(),
                encoding="utf-8",
            )

            with patch.dict(os.environ, {}, clear=True):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_control_plane_dokploy_source_of_truth(control_plane_root=control_plane_root)

        self.assertIn("route(s) that are not present in the source-of-truth: cm/prod", str(raised_error.exception))

    def test_read_dokploy_config_prefers_control_plane_env_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            (control_plane_root / ".env").write_text(
                "DOKPLOY_HOST=https://dokploy.control-plane.example\nDOKPLOY_TOKEN=control-plane-token\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {},
                clear=True,
            ):
                host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertEqual(host, "https://dokploy.control-plane.example")
        self.assertEqual(token, "control-plane-token")

    def test_read_dokploy_config_uses_process_environment_over_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            (control_plane_root / ".env").write_text(
                "DOKPLOY_HOST=https://dokploy.control-plane.example\nDOKPLOY_TOKEN=control-plane-token\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "DOKPLOY_HOST": "https://dokploy.process.example",
                    "DOKPLOY_TOKEN": "process-token",
                },
                clear=True,
            ):
                host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertEqual(host, "https://dokploy.process.example")
        self.assertEqual(token, "process-token")

    def test_read_dokploy_config_supports_explicit_control_plane_env_file(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            explicit_env_file = control_plane_root / "tmp" / "control-plane.env"
            explicit_env_file.parent.mkdir(parents=True, exist_ok=True)
            explicit_env_file.write_text(
                "DOKPLOY_HOST=https://dokploy.explicit.example\nDOKPLOY_TOKEN=explicit-token\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    control_plane_dokploy.CONTROL_PLANE_ENV_FILE_ENV_VAR: str(explicit_env_file),
                },
                clear=True,
            ):
                host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertEqual(host, "https://dokploy.explicit.example")
        self.assertEqual(token, "explicit-token")

    def test_read_dokploy_config_uses_external_harbor_config_dir_when_repo_file_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name) / "repo"
            control_plane_root.mkdir(parents=True, exist_ok=True)
            xdg_config_home = Path(temporary_directory_name) / "xdg"
            external_env_file = xdg_config_home / "harbor" / "dokploy.env"
            external_env_file.parent.mkdir(parents=True, exist_ok=True)
            external_env_file.write_text(
                "DOKPLOY_HOST=https://dokploy.external.example\nDOKPLOY_TOKEN=external-token\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": str(xdg_config_home),
                },
                clear=True,
            ):
                host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertEqual(host, "https://dokploy.external.example")
        self.assertEqual(token, "external-token")

    def test_read_control_plane_environment_values_includes_process_overrides(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            (control_plane_root / ".env").write_text(
                "DOKPLOY_SHIP_MODE=compose\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "DOKPLOY_SHIP_MODE": "application",
                },
                clear=True,
            ):
                environment_values = control_plane_dokploy.read_control_plane_environment_values(
                    control_plane_root=control_plane_root
                )

        self.assertEqual(environment_values["DOKPLOY_SHIP_MODE"], "application")

    def test_read_dokploy_config_fails_closed_without_control_plane_secret_source(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            xdg_config_home = Path(temporary_directory_name) / "xdg"

            with patch.dict(
                os.environ,
                {"XDG_CONFIG_HOME": str(xdg_config_home)},
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


class HarborServiceDeployTests(unittest.TestCase):
    @staticmethod
    def _target_payload(*, env_text: str, custom_git_ssh_key_id: str = "ssh-key-123") -> dict[str, object]:
        return {
            "name": "harbor",
            "appName": "compose-harbor",
            "sourceType": "git",
            "customGitUrl": "git@github.com:example/harbor.git",
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
                    "DOCKER_IMAGE_REFERENCE=ghcr.io/every/harbor@sha256:old\n"
                    "HARBOR_DATABASE_URL=postgresql+psycopg://harbor:test@db.internal:5432/harbor\n"
                    "HARBOR_MASTER_ENCRYPTION_KEY=test-key\n"
                    "DOKPLOY_HOST=https://dokploy.example.com\n"
                    "DOKPLOY_TOKEN=token-123\n"
                    "HARBOR_POLICY_B64=dGVzdA==\n"
                    "HARBOR_DOKPLOY_TARGET_IDS_B64=dGVzdA==\n"
                    "HARBOR_RUNTIME_ENVIRONMENTS_B64=dGVzdA==\n"
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
                    "ghcr.io/every/harbor@sha256:new",
                    "--health-url",
                    "https://harbor.example.com/v1/health",
                ],
            )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(len(captured_env_updates), 1)
        self.assertEqual(len(captured_trigger_calls), 1)
        self.assertIn("sha256:new", str(captured_env_updates[0]["env_text"]))
        payload = json.loads(result.output)
        self.assertEqual(payload["status"], "ok")
        self.assertEqual(payload["previous_image_reference"], "ghcr.io/every/harbor@sha256:old")
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
                        "DOCKER_IMAGE_REFERENCE=ghcr.io/every/harbor@sha256:old\n"
                        "HARBOR_DATABASE_URL=postgresql+psycopg://harbor:test@db.internal:5432/harbor\n"
                        "HARBOR_MASTER_ENCRYPTION_KEY=test-key\n"
                        "DOKPLOY_HOST=https://dokploy.example.com\n"
                        "DOKPLOY_TOKEN=token-123\n"
                        "HARBOR_POLICY_B64=dGVzdA==\n"
                        "HARBOR_DOKPLOY_TARGET_IDS_B64=dGVzdA==\n"
                        "HARBOR_RUNTIME_ENVIRONMENTS_B64=dGVzdA==\n"
                    ),
                ),
                self._target_payload(
                    env_text=(
                        "DOCKER_IMAGE_REFERENCE=ghcr.io/every/harbor@sha256:new\n"
                        "HARBOR_DATABASE_URL=postgresql+psycopg://harbor:test@db.internal:5432/harbor\n"
                        "HARBOR_MASTER_ENCRYPTION_KEY=test-key\n"
                        "DOKPLOY_HOST=https://dokploy.example.com\n"
                        "DOKPLOY_TOKEN=token-123\n"
                        "HARBOR_POLICY_B64=dGVzdA==\n"
                        "HARBOR_DOKPLOY_TARGET_IDS_B64=dGVzdA==\n"
                        "HARBOR_RUNTIME_ENVIRONMENTS_B64=dGVzdA==\n"
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
                click.ClickException("Healthcheck failed for https://harbor.example.com/v1/health: http 503"),
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
                    "ghcr.io/every/harbor@sha256:new",
                    "--health-url",
                    "https://harbor.example.com/v1/health",
                ],
            )

        self.assertNotEqual(result.exit_code, 0, msg=result.output)
        self.assertEqual(len(captured_env_updates), 2)
        self.assertEqual(len(captured_trigger_calls), 2)
        self.assertIn("sha256:new", str(captured_env_updates[0]["env_text"]))
        self.assertIn("sha256:old", str(captured_env_updates[1]["env_text"]))
        self.assertIn("Harbor service health verification failed", result.output)
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
                env_text="DOCKER_IMAGE_REFERENCE=ghcr.io/example/harbor@sha256:old\n",
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
            "Harbor service target is missing HARBOR_DATABASE_URL.",
            payload.get("blockers", []),
        )
        self.assertIn("Harbor service Dokploy target preflight failed", result.output)

    def test_service_deploy_dokploy_image_stops_before_env_change_when_preflight_fails(self) -> None:
        runner = CliRunner()

        with patch(
            "control_plane.dokploy.read_dokploy_config",
            return_value=("https://dokploy.example.com", "token-123"),
        ), patch(
            "control_plane.dokploy.fetch_dokploy_target_payload",
            return_value=self._target_payload(
                env_text=(
                    "HARBOR_MASTER_ENCRYPTION_KEY=test-key\n"
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
                    "ghcr.io/example/harbor@sha256:new",
                    "--health-url",
                    "https://harbor.example.com/v1/health",
                ],
            )

        self.assertNotEqual(result.exit_code, 0, msg=result.output)
        update_target_env.assert_not_called()
        trigger_deployment.assert_not_called()
        self.assertIn("Harbor service target is missing HARBOR_DATABASE_URL.", result.output)


if __name__ == "__main__":
    unittest.main()
