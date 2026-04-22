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
        for record in control_plane_runtime_environments.build_runtime_environment_records_from_definition(
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
                    'schema_version = 2\n\n'
                    '[[targets]]\n'
                    'context = "cm"\n'
                    'instance = "testing"\n'
                    'target_id = "target-cm-testing"\n\n'
                    '[targets.env]\n'
                    'ODOO_ADDON_REPOSITORIES = "cbusillo/disable_odoo_online@411f6b8e85cac72dc7aa2e2dc5540001043c327d"\n'
                    'ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL = "https://cm-testing.example.com"\n'
                ),
            )

            with patch.dict(os.environ, {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True):
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

        self.assertEqual(resolved_values["LAUNCHPLANE_PREVIEW_BASE_URL"], "https://launchplane.example")
        self.assertEqual(resolved_values["ODOO_MASTER_PASSWORD"], "shared-master")
        self.assertEqual(resolved_values["ENV_OVERRIDE_DISABLE_CRON"], "True")

    def test_load_runtime_environment_definition_prefers_postgres_records_without_file_fallback(self) -> None:
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
                loaded_definition = control_plane_runtime_environments.load_runtime_environment_definition(
                    control_plane_root=control_plane_root,
                )

        self.assertEqual(resolved_values["ODOO_MASTER_PASSWORD"], "db-master")
        self.assertEqual(resolved_values["ODOO_DB_PASSWORD"], "db-secret")
        self.assertNotIn("cm", loaded_definition.contexts)

    def test_load_runtime_environment_definition_requires_database_records_without_file_fallback(self) -> None:
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
                with self.assertRaisesRegex(Exception, "Missing Launchplane runtime environment authority"):
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


if __name__ == "__main__":
    unittest.main()
