from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane import dokploy as control_plane_dokploy
from control_plane.dokploy_target_inspect import (
    DokployTargetInspectRequest,
    inspect_dokploy_target,
    summarize_dokploy_target_payload,
)
from control_plane.storage.postgres import PostgresRecordStore


def _sqlite_database_url(path: Path) -> str:
    return f"sqlite:///{path}"


def _require_mapping(value: object) -> dict[str, object]:
    assert isinstance(value, dict)
    return value


def _fake_target_payload(
    *, host: str, token: str, target_type: str, target_id: str
) -> control_plane_dokploy.JsonObject:
    del host, token, target_type, target_id
    return {
        "id": "compose-cm-prod",
        "name": "cm-prod",
        "serverId": "server-123",
        "environment": {
            "id": "env-prod",
            "name": "prod",
            "project": {"id": "project-odoo", "name": "odoo"},
        },
        "env": {"ODOO_DB_PASSWORD": "secret"},
    }


class DokployTargetInspectTests(unittest.TestCase):
    def test_summarize_payload_redacts_env_values_and_keeps_identity_fields(self) -> None:
        summary = summarize_dokploy_target_payload(
            target_type="compose",
            target_id="compose-123",
            payload={
                "id": "compose-123",
                "name": "odoo-tenant-cm-website-prod",
                "appName": "odoo-tenant-cm-website-prod",
                "serverId": "server-123",
                "sourceType": "raw",
                "composePath": "docker-compose.yml",
                "environment": {
                    "id": "env-prod",
                    "name": "prod",
                    "project": {"id": "project-odoo", "name": "odoo"},
                },
                "env": "ODOO_DB_PASSWORD=secret\nDISABLE_ODOO_ONLINE=true\n",
                "domains": [
                    {"id": "domain-1", "host": "cm-website-prod.shinycomputers.com", "port": 8069}
                ],
            },
        )

        self.assertEqual(summary["id"], "compose-123")
        self.assertEqual(summary["server_id"], "server-123")
        environment = _require_mapping(summary["environment"])
        env = _require_mapping(summary["env"])
        self.assertEqual(environment["id"], "env-prod")
        self.assertEqual(environment["project_id"], "project-odoo")
        self.assertEqual(env["key_count"], 2)
        self.assertEqual(env["keys"], ["DISABLE_ODOO_ONLINE", "ODOO_DB_PASSWORD"])
        self.assertTrue(env["values_redacted"])
        self.assertNotIn("secret", str(summary))

    def test_inspect_tracked_target_includes_records_and_redacted_provider_summary(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = PostgresRecordStore(
                database_url=_sqlite_database_url(Path(temporary_directory_name) / "db.sqlite3")
            )
            try:
                store.ensure_schema()
                store.write_dokploy_target_record(
                    DokployTargetRecord(
                        context="cm_website",
                        instance="prod",
                        target_type="compose",
                        target_name="cm-prod",
                        project_name="odoo",
                        domains=("cm-website-prod.shinycomputers.com",),
                        updated_at="2026-06-14T00:00:00Z",
                        source_label="test",
                    )
                )
                store.write_dokploy_target_id_record(
                    DokployTargetIdRecord(
                        context="cm_website",
                        instance="prod",
                        target_id="compose-cm-prod",
                        updated_at="2026-06-14T00:00:00Z",
                        source_label="test",
                    )
                )
                store.write_provider_target_record(
                    ProviderTargetRecord(
                        context="cm_website",
                        instance="prod",
                        provider_id="dokploy",
                        target_category="compose",
                        target_id="compose-cm-prod",
                        display_name="cm-prod",
                        provider_target_type="compose",
                        provider_evidence={"project_name": "odoo"},
                        updated_at="2026-06-14T00:00:00Z",
                        source_label="test",
                    )
                )

                result = inspect_dokploy_target(
                    record_store=store,
                    host="https://dokploy.example.invalid",
                    token="token",
                    request=DokployTargetInspectRequest(
                        context="cm_website",
                        instance="prod",
                    ),
                    fetch_target_payload=_fake_target_payload,
                )
            finally:
                store.close()

        self.assertEqual(result["target_id"], "compose-cm-prod")
        tracked_target = _require_mapping(result["tracked_target"])
        provider_target_record = _require_mapping(result["provider_target_record"])
        provider = _require_mapping(result["provider"])
        provider_environment = _require_mapping(provider["environment"])
        provider_env = _require_mapping(provider["env"])
        self.assertEqual(tracked_target["target_name"], "cm-prod")
        self.assertEqual(provider_target_record["display_name"], "cm-prod")
        self.assertEqual(provider_environment["id"], "env-prod")
        self.assertEqual(provider_env["keys"], ["ODOO_DB_PASSWORD"])
        self.assertTrue(result["provider_payload_redacted"])
        self.assertNotIn("secret", str(result))


if __name__ == "__main__":
    unittest.main()
