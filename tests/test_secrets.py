from __future__ import annotations

import os
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.secret_record import SecretAuditEvent
from control_plane.contracts.secret_record import SecretBinding
from control_plane.contracts.secret_record import SecretRecord
from control_plane.contracts.secret_record import SecretVersion
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(database_path: Path) -> str:
    return f"sqlite+pysqlite:///{database_path}"


def _seed_runtime_environment_records(
    *,
    database_url: str,
    definition: runtime_environments.RuntimeEnvironmentDefinition,
) -> None:
    store = PostgresRecordStore(database_url=database_url)
    store.ensure_schema()
    try:
        records = runtime_environments.build_runtime_environment_records_from_definition(
            definition,
            updated_at="2026-04-22T00:00:00Z",
            source_label="test",
        )
        for record in records:
            store.write_runtime_environment_record(record)
    finally:
        store.close()


class _FakeSecretReadStore:
    def __init__(self) -> None:
        self.records = (
            SecretRecord(
                secret_id="secret-runtime-smtp-password-opw-testing",
                scope="context_instance",
                integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                name="smtp-password",
                context="opw",
                instance="testing",
                description="SMTP password",
                current_version_id="secret-version-current",
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:01:00Z",
                updated_by="operator",
            ),
        )
        self.versions = (
            SecretVersion(
                version_id="secret-version-current",
                secret_id="secret-runtime-smtp-password-opw-testing",
                created_at="2026-05-01T00:01:00Z",
                created_by="operator",
                ciphertext="redacted-ciphertext",
            ),
            SecretVersion(
                version_id="secret-version-previous",
                secret_id="secret-runtime-smtp-password-opw-testing",
                created_at="2026-05-01T00:00:30Z",
                created_by="operator",
                ciphertext="previous-redacted-ciphertext",
            ),
        )
        self.bindings = (
            SecretBinding(
                binding_id="secret-runtime-smtp-password-opw-testing-binding-smtp-password",
                secret_id="secret-runtime-smtp-password-opw-testing",
                integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
                binding_key="SMTP_PASSWORD",
                context="opw",
                instance="testing",
                created_at="2026-05-01T00:00:00Z",
                updated_at="2026-05-01T00:01:00Z",
            ),
        )
        self.audit_events = (
            SecretAuditEvent(
                event_id="secret-runtime-smtp-password-opw-testing-event-rotated",
                secret_id="secret-runtime-smtp-password-opw-testing",
                event_type="rotated",
                recorded_at="2026-05-01T00:01:00Z",
                actor="operator",
                detail="Rotated from fake store.",
                metadata={"source": "fake-store"},
            ),
        )

    def read_secret_record(self, secret_id: str) -> SecretRecord:
        for record in self.records:
            if record.secret_id == secret_id:
                return record
        raise FileNotFoundError(secret_id)

    def list_secret_records(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretRecord, ...]:
        records = tuple(
            record
            for record in self.records
            if (not integration or record.integration == integration)
            and (not context_name or record.context == context_name)
            and (not instance_name or record.instance == instance_name)
        )
        return records[:limit] if limit is not None else records

    def read_secret_version(self, version_id: str) -> SecretVersion:
        for version in self.versions:
            if version.version_id == version_id:
                return version
        raise FileNotFoundError(version_id)

    def list_secret_versions(self, *, secret_id: str) -> tuple[SecretVersion, ...]:
        return tuple(version for version in self.versions if version.secret_id == secret_id)

    def list_secret_bindings(
        self,
        *,
        integration: str = "",
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[SecretBinding, ...]:
        bindings = tuple(
            binding
            for binding in self.bindings
            if (not integration or binding.integration == integration)
            and (not context_name or binding.context == context_name)
            and (not instance_name or binding.instance == instance_name)
        )
        return bindings[:limit] if limit is not None else bindings

    def list_secret_audit_events(self, *, secret_id: str) -> tuple[SecretAuditEvent, ...]:
        return tuple(event for event in self.audit_events if event.secret_id == secret_id)


class LaunchplaneSecretsTests(unittest.TestCase):
    def test_secret_statuses_use_structural_read_store_boundary(self) -> None:
        store = _FakeSecretReadStore()

        statuses = control_plane_secrets.list_secret_statuses(
            store,
            integration=control_plane_secrets.RUNTIME_ENVIRONMENT_SECRET_INTEGRATION,
            context_name="opw",
            instance_name="testing",
        )

        self.assertEqual(len(statuses), 1)
        status = statuses[0]
        self.assertEqual(status["secret_id"], "secret-runtime-smtp-password-opw-testing")
        self.assertEqual(status["version_count"], 2)
        self.assertEqual(status["current_version_created_at"], "2026-05-01T00:01:00Z")
        self.assertEqual(
            status["binding"],
            {
                "binding_id": "secret-runtime-smtp-password-opw-testing-binding-smtp-password",
                "binding_type": "env",
                "binding_key": "SMTP_PASSWORD",
                "status": "configured",
                "context": "opw",
                "instance": "testing",
                "updated_at": "2026-05-01T00:01:00Z",
            },
        )
        audit_events = cast("list[dict[str, object]]", status["recent_audit_events"])
        self.assertEqual(audit_events[0]["event_type"], "rotated")

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
                host, token = control_plane_dokploy.read_dokploy_config(
                    control_plane_root=control_plane_root
                )
                self.assertEqual(host, "https://dokploy.db.example")
                self.assertEqual(token, "db-token")
            store.close()

    def test_resolve_runtime_environment_values_prefers_managed_secret_overlay(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            control_plane_root = Path(temporary_directory_name)
            database_url = _sqlite_database_url(control_plane_root / "launchplane.sqlite3")
            _seed_runtime_environment_records(
                database_url=database_url,
                definition=runtime_environments.RuntimeEnvironmentDefinition(
                    schema_version=1,
                    shared_env={"ODOO_MASTER_PASSWORD": "file-shared-master"},
                    contexts={
                        "opw": runtime_environments.RuntimeEnvironmentContextDefinition(
                            shared_env={"GITHUB_WEBHOOK_SECRET": "file-context-secret"},
                            instances={
                                "testing": runtime_environments.RuntimeEnvironmentInstanceDefinition(
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
                resolved_values = runtime_environments.resolve_runtime_environment_values(
                    control_plane_root=control_plane_root,
                    context_name="opw",
                    instance_name="testing",
                )
                self.assertEqual(resolved_values["ODOO_MASTER_PASSWORD"], "db-shared-master")
                self.assertEqual(resolved_values["GITHUB_WEBHOOK_SECRET"], "db-context-secret")
                self.assertEqual(resolved_values["ODOO_DB_PASSWORD"], "db-instance-secret")
                self.assertEqual(
                    resolved_values["LAUNCHPLANE_PREVIEW_BASE_URL"], "https://preview.example.com"
                )
            store.close()


if __name__ == "__main__":
    unittest.main()
