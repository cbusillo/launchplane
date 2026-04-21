from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from click.testing import CliRunner

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane import secrets as control_plane_secrets
from control_plane.cli import main
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


class LaunchplaneSecretsTests(unittest.TestCase):
    def test_read_dokploy_config_prefers_managed_secret_overlay(self) -> None:
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
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                },
                clear=True,
            ):
                control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="global",
                    integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
                    name="host",
                    plaintext_value="https://dokploy.db.example",
                    binding_key="DOKPLOY_HOST",
                    actor="test",
                )
                control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="global",
                    integration=control_plane_secrets.DOKPLOY_SECRET_INTEGRATION,
                    name="token",
                    plaintext_value="db-token",
                    binding_key="DOKPLOY_TOKEN",
                    actor="test",
                )
                host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
                self.assertEqual(host, "https://dokploy.db.example")
                self.assertEqual(token, "db-token")
            store.close()

    def test_resolve_runtime_environment_values_prefers_managed_secret_overlay(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            environments_file = control_plane_root / "config" / "runtime-environments.toml"
            environments_file.parent.mkdir(parents=True, exist_ok=True)
            environments_file.write_text(
                """
schema_version = 1

[shared_env]
ODOO_MASTER_PASSWORD = "file-shared-master"

[contexts.opw.shared_env]
GITHUB_WEBHOOK_SECRET = "file-context-secret"

[contexts.opw.instances.testing.env]
ODOO_DB_PASSWORD = "file-instance-secret"
LAUNCHPLANE_PREVIEW_BASE_URL = "https://preview.example.com"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch.dict(
                os.environ,
                {
                    control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key",
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                },
                clear=True,
            ):
                control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="global",
                    integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                    name="ODOO_MASTER_PASSWORD",
                    plaintext_value="db-shared-master",
                    binding_key="ODOO_MASTER_PASSWORD",
                    actor="test",
                )
                control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="context",
                    integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                    name="GITHUB_WEBHOOK_SECRET",
                    plaintext_value="db-context-secret",
                    binding_key="GITHUB_WEBHOOK_SECRET",
                    context_name="opw",
                    actor="test",
                )
                control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="context_instance",
                    integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                    name="ODOO_DB_PASSWORD",
                    plaintext_value="db-instance-secret",
                    binding_key="ODOO_DB_PASSWORD",
                    context_name="opw",
                    instance_name="testing",
                    actor="test",
                )
                resolved_values = control_plane_runtime_environments.resolve_runtime_environment_values(
                    control_plane_root=control_plane_root,
                    context_name="opw",
                    instance_name="testing",
                )
                self.assertEqual(resolved_values["ODOO_MASTER_PASSWORD"], "db-shared-master")
                self.assertEqual(resolved_values["GITHUB_WEBHOOK_SECRET"], "db-context-secret")
                self.assertEqual(resolved_values["ODOO_DB_PASSWORD"], "db-instance-secret")
                self.assertEqual(resolved_values["LAUNCHPLANE_PREVIEW_BASE_URL"], "https://preview.example.com")
            store.close()

    def test_import_bootstrap_secrets_pulls_existing_dokploy_and_runtime_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            (control_plane_root / ".env").write_text(
                "DOKPLOY_HOST=https://dokploy.bootstrap.example\nDOKPLOY_TOKEN=bootstrap-token\n",
                encoding="utf-8",
            )
            runtime_environments_file = control_plane_root / "config" / "runtime-environments.toml"
            runtime_environments_file.parent.mkdir(parents=True, exist_ok=True)
            runtime_environments_file.write_text(
                """
schema_version = 1

[shared_env]
ODOO_MASTER_PASSWORD = "shared-master"

[contexts.opw.shared_env]
GITHUB_WEBHOOK_SECRET = "webhook-secret"
LAUNCHPLANE_PREVIEW_BASE_URL = "https://preview.example.com"

[contexts.opw.instances.testing.env]
ODOO_DB_PASSWORD = "instance-password"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                summary = control_plane_secrets.import_bootstrap_secrets(
                    record_store=store,
                    control_plane_root=control_plane_root,
                    actor="bootstrap-test",
                )
                statuses = control_plane_secrets.list_secret_statuses(store, context_name="opw", instance_name="testing")
                self.assertEqual(summary["dokploy"]["imported"], 2)
                self.assertEqual(summary["runtime_environment"]["imported"], 3)
                self.assertEqual(
                    {status["binding"]["binding_key"] for status in statuses if status["binding"] is not None},
                    {
                        "DOKPLOY_HOST",
                        "DOKPLOY_TOKEN",
                        "ODOO_MASTER_PASSWORD",
                        "GITHUB_WEBHOOK_SECRET",
                        "ODOO_DB_PASSWORD",
                    },
                )
            store.close()

    def test_secrets_cli_import_bootstrap_reports_summary(self) -> None:
        runner = CliRunner()
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            (control_plane_root / ".env").write_text(
                "DOKPLOY_HOST=https://dokploy.bootstrap.example\nDOKPLOY_TOKEN=bootstrap-token\n",
                encoding="utf-8",
            )
            runtime_environments_file = control_plane_root / "config" / "runtime-environments.toml"
            runtime_environments_file.parent.mkdir(parents=True, exist_ok=True)
            runtime_environments_file.write_text(
                """
schema_version = 1

[contexts.opw.shared_env]
GITHUB_WEBHOOK_SECRET = "webhook-secret"
""".strip()
                + "\n",
                encoding="utf-8",
            )
            with patch.dict(
                os.environ,
                {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
                clear=True,
            ):
                result = runner.invoke(
                    main,
                    [
                        "secrets",
                        "import-bootstrap",
                        "--database-url",
                        database_url,
                        "--control-plane-root",
                        str(control_plane_root),
                    ],
                )

        self.assertEqual(result.exit_code, 0, msg=result.output)
        self.assertIn('"dokploy": {', result.output)
        self.assertIn('"runtime_environment": {', result.output)


if __name__ == "__main__":
    unittest.main()
