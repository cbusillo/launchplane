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
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


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
LAUNCHPLANE_PREVIEW_BASE_URL = "https://launchplane.example"
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

        self.assertEqual(resolved_values["LAUNCHPLANE_PREVIEW_BASE_URL"], "https://launchplane.example")
        self.assertEqual(resolved_values["ODOO_MASTER_PASSWORD"], "shared-master")
        self.assertEqual(resolved_values["ENV_OVERRIDE_DISABLE_CRON"], "True")

    def test_resolve_runtime_environment_values_uses_external_launchplane_config_dir_when_repo_file_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name) / "repo"
            control_plane_root.mkdir(parents=True, exist_ok=True)
            xdg_config_home = Path(temporary_directory_name) / "xdg"
            environments_file = xdg_config_home / "launchplane" / "runtime-environments.toml"
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

    def test_load_runtime_environment_definition_merges_postgres_records_over_file_definition(self) -> None:
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
                resolved_values = control_plane_runtime_environments.resolve_runtime_environment_values(
                    control_plane_root=control_plane_root,
                    context_name="opw",
                    instance_name="local",
                )
                cm_values = control_plane_runtime_environments.resolve_runtime_environment_values(
                    control_plane_root=control_plane_root,
                    context_name="cm",
                    instance_name="testing",
                )

        self.assertEqual(resolved_values["ODOO_MASTER_PASSWORD"], "db-master")
        self.assertEqual(resolved_values["ODOO_DB_PASSWORD"], "db-secret")
        self.assertEqual(cm_values["ODOO_DB_PASSWORD"], "cm-file-secret")

    def test_load_runtime_environment_definition_explicit_file_override_wins_over_postgres(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            custom_file = control_plane_root / "custom-runtime-environments.toml"
            custom_file.write_text(
                """
schema_version = 1

[shared_env]
ODOO_MASTER_PASSWORD = "file-master"

[contexts.opw.instances.local.env]
ODOO_DB_PASSWORD = "file-secret"
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

            with patch.dict(
                os.environ,
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    control_plane_runtime_environments.RUNTIME_ENVIRONMENTS_FILE_ENV_VAR: str(custom_file),
                },
                clear=True,
            ):
                resolved_values = control_plane_runtime_environments.resolve_runtime_environment_values(
                    control_plane_root=control_plane_root,
                    context_name="opw",
                    instance_name="local",
                )

        self.assertEqual(resolved_values["ODOO_MASTER_PASSWORD"], "file-master")
        self.assertEqual(resolved_values["ODOO_DB_PASSWORD"], "file-secret")

    def test_storage_import_runtime_environments_writes_records_to_postgres(self) -> None:
        runner = CliRunner()
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

[contexts.opw.shared_env]
ENV_OVERRIDE_DISABLE_CRON = true

[contexts.opw.instances.local.env]
ODOO_DB_PASSWORD = "local-secret"
""".strip()
                + "\n",
                encoding="utf-8",
            )

            result = runner.invoke(
                main,
                [
                    "storage",
                    "import-runtime-environments",
                    "--database-url",
                    database_url,
                    "--control-plane-root",
                    str(control_plane_root),
                ],
            )

            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            listed_records = store.list_runtime_environment_records()
            store.close()

        self.assertEqual(result.exit_code, 0, msg=result.output)
        payload = json.loads(result.output)
        self.assertEqual(payload["count"], 3)
        self.assertEqual(
            [(record.scope, record.context, record.instance) for record in listed_records],
            [("context", "opw", ""), ("global", "", ""), ("instance", "opw", "local")],
        )

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
