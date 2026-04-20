from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from click.testing import CliRunner

from control_plane.cli import main
from control_plane import runtime_environments as control_plane_runtime_environments


class RuntimeEnvironmentTests(unittest.TestCase):
    def test_resolve_runtime_environment_values_merges_shared_context_and_instance_values(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[shared_env]
ODOO_MASTER_PASSWORD = "shared-master"
ODOO_DB_USER = "odoo"

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true

[contexts.opw.instances.local.env]
ODOO_DB_PASSWORD = "local-secret"
ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL = "https://opw-local.example.com"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            resolved_values = control_plane_runtime_environments.resolve_runtime_environment_values(
                control_plane_root=control_plane_root,
                context_name="opw",
                instance_name="local",
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
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[contexts.opw.instances.testing.env]
ODOO_DB_PASSWORD = "testing-secret"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with self.assertRaisesRegex(Exception, "opw/local"):
                control_plane_runtime_environments.resolve_runtime_environment_values(
                    control_plane_root=control_plane_root,
                    context_name="opw",
                    instance_name="local",
                )

    def test_resolve_runtime_environment_values_merges_tracked_target_env_for_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            config_directory = control_plane_root / "config"
            config_directory.mkdir(parents=True, exist_ok=True)
            (config_directory / "runtime-environments.toml").write_text(
                (
                    'schema_version = 1\n\n'
                    '[shared_env]\n'
                    'ODOO_MASTER_PASSWORD = "shared-master"\n\n'
                    '[contexts.cm.instances.testing.env]\n'
                    'ODOO_DB_PASSWORD = "testing-secret"\n'
                ),
                encoding="utf-8",
            )
            (config_directory / "dokploy.toml").write_text(
                (
                    'schema_version = 2\n\n'
                    '[[targets]]\n'
                    'context = "cm"\n'
                    'instance = "testing"\n'
                    'target_id = "target-cm-testing"\n\n'
                    '[targets.env]\n'
                    'ODOO_ADDON_REPOSITORIES = "cbusillo/disable_odoo_online@411f6b8e85cac72dc7aa2e2dc5540001043c327d"\n'
                    'ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL = "https://cm-testing.example.com"\n'
                ),
                encoding="utf-8",
            )

            resolved_values = control_plane_runtime_environments.resolve_runtime_environment_values(
                control_plane_root=control_plane_root,
                context_name="cm",
                instance_name="testing",
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
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[shared_env]
HARBOR_PREVIEW_BASE_URL = "https://harbor.example"
ODOO_MASTER_PASSWORD = "shared-master"

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true
""".strip()
                + "\n",
                encoding="utf-8",
            )

            resolved_values = control_plane_runtime_environments.resolve_runtime_context_values(
                control_plane_root=control_plane_root,
                context_name="opw",
            )

        self.assertEqual(resolved_values["HARBOR_PREVIEW_BASE_URL"], "https://harbor.example")
        self.assertEqual(resolved_values["ODOO_MASTER_PASSWORD"], "shared-master")
        self.assertEqual(resolved_values["ENV_OVERRIDE_DISABLE_CRON"], "True")

    def test_resolve_runtime_environment_values_uses_external_harbor_config_dir_when_repo_file_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name) / "repo"
            control_plane_root.mkdir(parents=True, exist_ok=True)
            xdg_config_home = Path(temporary_directory_name) / "xdg"
            environments_file = xdg_config_home / "harbor" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[shared_env]
ODOO_MASTER_PASSWORD = "shared-master"

[contexts.opw.instances.local.env]
ODOO_DB_PASSWORD = "local-secret"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            with patch.dict(
                os.environ,
                {
                    "XDG_CONFIG_HOME": str(xdg_config_home),
                },
                clear=True,
            ):
                resolved_values = control_plane_runtime_environments.resolve_runtime_environment_values(
                    control_plane_root=control_plane_root,
                    context_name="opw",
                    instance_name="local",
                )

        self.assertEqual(resolved_values["ODOO_MASTER_PASSWORD"], "shared-master")
        self.assertEqual(resolved_values["ODOO_DB_PASSWORD"], "local-secret")

    def test_environments_resolve_command_emits_json_payload(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[shared_env]
ODOO_MASTER_PASSWORD = "shared-master"

[contexts.opw.instances.local.env]
ODOO_DB_PASSWORD = "local-secret"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            command_runner = CliRunner()
            with (
                patch("control_plane.cli._control_plane_root", return_value=control_plane_root),
                patch.dict(
                    os.environ,
                    {},
                    clear=True,
                ),
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


if __name__ == "__main__":
    unittest.main()
