from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane import secrets as control_plane_secrets
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _seed_runtime_environment_records(
    *,
    database_url: str,
    definition: control_plane_runtime_environments.RuntimeEnvironmentDefinition,
) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        for record in control_plane_runtime_environments.build_runtime_environment_records_from_definition(
            definition,
            updated_at="2026-04-22T00:00:00Z",
            source_label="test",
        ):
            store.write_runtime_environment_record(record)
    finally:
        store.close()


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
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={"ODOO_MASTER_PASSWORD": "file-shared-master"},
                    contexts={
                        "opw": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={"GITHUB_WEBHOOK_SECRET": "file-context-secret"},
                            instances={
                                "testing": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                    env={
                                        "ODOO_DB_PASSWORD": "file-instance-secret",
                                        "LAUNCHPLANE_PREVIEW_BASE_URL": "https://preview.example.com",
                                    }
                                )
                            },
                        )
                    },
                ),
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


if __name__ == "__main__":
    unittest.main()
