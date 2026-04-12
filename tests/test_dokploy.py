import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane import dokploy as control_plane_dokploy


class DokployConfigTests(unittest.TestCase):
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

            with patch.dict(
                os.environ,
                {},
                clear=True,
            ):
                with self.assertRaises(click.ClickException) as raised_error:
                    control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)

        self.assertIn("control-plane .env", str(raised_error.exception))

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


if __name__ == "__main__":
    unittest.main()
