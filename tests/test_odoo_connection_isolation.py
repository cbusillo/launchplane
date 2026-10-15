from __future__ import annotations

import sqlite3
import io
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
from unittest.mock import Mock, patch

from control_plane.odoo_connection_isolation import (
    CONNECTION_ISOLATION_PROGRAM,
    connection_isolation_library,
)
from control_plane.workflows.odoo_post_deploy import OdooPostDeployRequest, execute_odoo_post_deploy


class SqliteCursor:
    """Only adapt PostgreSQL metadata/btrim syntax; execute repair SQL as written."""

    def __init__(self, connection: sqlite3.Connection):
        self.connection = connection
        self.cursor = connection.cursor()

    def execute(self, query: str, params: Any = ()) -> None:
        if query.startswith("SELECT column_name FROM information_schema.columns"):
            self.cursor.execute("SELECT name FROM pragma_table_info(?)", (params[0],))
            return
        query = query.replace("btrim(", "trim(").replace("::text", "").replace("%s", "?")
        self.cursor.execute(query, params)

    def fetchone(self) -> Any:
        return self.cursor.fetchone()

    def fetchall(self) -> Any:
        return self.cursor.fetchall()


def seed(connection: Any) -> None:
    connection.execute("CREATE TABLE ir_config_parameter (key TEXT PRIMARY KEY, value TEXT)")
    connection.execute("CREATE TABLE res_company (stock_move_sms_validation BOOLEAN)")
    connection.execute("INSERT INTO res_company VALUES (true)")
    connection.execute("CREATE TABLE iap_account (account_token TEXT)")
    connection.execute("INSERT INTO iap_account VALUES ('copied-sms-key')")
    connection.execute(
        "CREATE TABLE delivery_carrier (delivery_type TEXT, active BOOLEAN, prod_environment BOOLEAN, "
        "usps_api_key TEXT, usps_api_secret TEXT, usps_eps_account_number TEXT, "
        "usps_access_token TEXT, usps_payment_token TEXT, ups_client_id TEXT, "
        "ups_client_secret TEXT, ups_shipper_number TEXT, ups_access_token TEXT)"
    )
    connection.execute(
        "INSERT INTO delivery_carrier VALUES ('usps_rest', true, false, 'key', 'secret', 'account', 'access', 'payment', NULL, NULL, NULL, NULL)"
    )
    connection.execute(
        "INSERT INTO delivery_carrier VALUES ('ups_rest', false, true, NULL, NULL, NULL, NULL, NULL, 'client', 'secret', 'shipper', 'cached-token')"
    )
    connection.execute(
        "INSERT INTO delivery_carrier VALUES ('fixed', true, false, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL, NULL)"
    )
    connection.execute(
        "INSERT INTO ir_config_parameter VALUES ('shopify.api_token', 'development-store-key')"
    )
    connection.commit()


class ConnectionIsolationTests(unittest.TestCase):
    def test_remote_command_exit_retains_findings_through_native_schedule_helper(self) -> None:
        from control_plane.dokploy import api, post_deploy
        from control_plane.dokploy.source import DokployTargetDefinition

        target = DokployTargetDefinition(
            context="example", instance="testing", target_type="compose", target_id="fixture"
        )
        with (
            patch.object(api, "upsert_dokploy_schedule", return_value={"scheduleId": "fixture"}),
            patch.object(
                api,
                "latest_deployment_for_schedule",
                side_effect=[None, {"deploymentId": "failed-fixture", "status": "error"}],
            ),
            patch.object(
                api,
                "dokploy_request",
                side_effect=api.DokployRequestFailed(
                    method="POST",
                    path="/api/schedule.runManually",
                    detail="remote exit",
                    remote_command_failed=True,
                ),
            ),
            patch.object(api, "fetch_dokploy_deployment_logs", return_value=()),
            patch.object(
                post_deploy,
                "_read_odoo_post_deploy_log_markers",
                return_value={
                    "integration_readback_refused": "carrier/usps_api_secret",
                    "integration_readback_ok": "false",
                },
            ),
        ):
            with self.assertRaises(post_deploy.OdooPostDeployReadbackFailure) as raised:
                post_deploy.run_manual_data_schedule(
                    host="fixture",
                    token="fixture",
                    target_definition=target,
                    schedule_lookup_id="fixture",
                    schedule_type="server",
                    schedule_name="fixture",
                    schedule_app_name="fixture",
                    schedule_payload={},
                    resolved_schedule_execution_timeout_seconds=30,
                )
        self.assertIn("carrier/usps_api_secret", str(raised.exception))

    def test_failed_schedule_returns_value_free_findings(self) -> None:
        from control_plane.dokploy import api, post_deploy
        from control_plane.dokploy.source import DokployTargetDefinition

        target = DokployTargetDefinition(
            context="example", instance="testing", target_type="compose", target_id="fixture"
        )
        with (
            patch.object(
                api, "upsert_dokploy_schedule", return_value={"scheduleId": "schedule-fixture"}
            ),
            patch.object(api, "latest_deployment_for_schedule", return_value=None),
            patch.object(api, "fetch_dokploy_deployment_logs", return_value=()),
            patch.object(
                api,
                "trigger_dokploy_schedule_and_wait",
                side_effect=api.DokployDeploymentFailed(
                    deployment_id="failed-fixture",
                    deployment_status="error",
                    message_prefix="failure",
                ),
            ),
            patch.object(
                post_deploy,
                "_read_odoo_post_deploy_log_markers",
                return_value={
                    "integration_readback_ok": "false",
                    "integration_readback_refused": "printnode/printnode.api_key",
                    "password": "must-not-leak",
                },
            ),
        ):
            with self.assertRaises(post_deploy.OdooPostDeployReadbackFailure) as raised:
                post_deploy.run_manual_data_schedule(
                    host="fixture",
                    token="fixture",
                    target_definition=target,
                    schedule_lookup_id="fixture",
                    schedule_type="server",
                    schedule_name="fixture",
                    schedule_app_name="fixture",
                    schedule_payload={},
                    resolved_schedule_execution_timeout_seconds=30,
                )
        self.assertIn("printnode/printnode.api_key", str(raised.exception))
        self.assertNotIn("password", raised.exception.evidence)

    def test_malformed_or_non_dry_run_receipt_is_rejected_before_transport(self) -> None:
        import json
        import runpy

        script = runpy.run_path(
            "scripts/repair-odoo-copy-connections.py", run_name="repair_client_test"
        )
        with TemporaryDirectory() as directory:
            evidence = Path(directory) / "receipt.json"
            for contents in ("{invalid", json.dumps({"mode": "apply"})):
                evidence.write_text(contents)
                argv = [
                    "repair",
                    "apply",
                    "--product",
                    "example",
                    "--context",
                    "example",
                    "--instance",
                    "testing",
                    "--reason",
                    "repair copy",
                    "--operator-helper",
                    "absent",
                    "--reviewed-dry-run",
                    "--idempotency-key",
                    "fixture",
                    "--evidence-file",
                    str(evidence),
                ]
                with (
                    self.subTest(contents=contents),
                    patch("sys.argv", argv),
                    patch("sys.stderr", new_callable=io.StringIO),
                    self.assertRaises(SystemExit) as raised,
                ):
                    script["main"]()
                self.assertEqual(raised.exception.code, 2)

    def test_changed_plan_refuses_before_schedule_or_database_mutation(self) -> None:
        from control_plane.dokploy.connection_isolation import run_connection_isolation
        from control_plane.dokploy.source import DokployTargetDefinition
        import click

        target = DokployTargetDefinition(
            context="example", instance="testing", target_type="compose", target_id="fixture"
        )
        with (
            patch(
                "control_plane.dokploy.connection_isolation.api.fetch_dokploy_target_payload",
                return_value={"env": "ODOO_DB_NAME=copy", "appName": "fixture"},
            ),
            patch(
                "control_plane.dokploy.connection_isolation.post_deploy.run_manual_data_schedule"
            ) as schedule,
        ):
            with self.assertRaises(click.ClickException):
                run_connection_isolation(
                    host="fixture",
                    token="fixture",
                    target_definition=target,
                    mode="apply",
                    reviewed_plan_sha256="outdated",
                )
        schedule.assert_not_called()

    def test_repairs_copy_and_preserves_fixed_delivery_and_shopify(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        seed(connection)
        namespace: dict[str, Any] = {}
        exec(connection_isolation_library(), namespace)
        repair = namespace["connection_isolation"]
        cursor = SqliteCursor(connection)
        before = connection.total_changes
        self.assertTrue(repair(cursor, apply=False))
        self.assertEqual(connection.total_changes, before)
        self.assertEqual(repair(cursor, apply=True), [])
        self.assertEqual(repair(cursor, apply=False), [])
        self.assertEqual(
            connection.execute(
                "SELECT active FROM delivery_carrier WHERE delivery_type='fixed'"
            ).fetchone(),
            (1,),
        )
        self.assertEqual(
            connection.execute(
                "SELECT value FROM ir_config_parameter WHERE key='shopify.api_token'"
            ).fetchone(),
            ("development-store-key",),
        )
        for values in connection.execute(
            "SELECT usps_api_key, usps_api_secret, usps_eps_account_number, "
            "usps_access_token, usps_payment_token, ups_client_id, ups_client_secret, "
            "ups_shipper_number, ups_access_token FROM delivery_carrier"
        ):
            self.assertTrue(all(value is None for value in values), values)
        self.assertEqual(
            connection.execute("SELECT stock_move_sms_validation FROM res_company").fetchone(), (0,)
        )
        # A recreated account with an enabled token is caught independently,
        # even though the global SMS endpoints remain inert.
        connection.execute("INSERT INTO iap_account VALUES ('new-key')")
        self.assertIn("iap/iap_account", repair(cursor, apply=False))
        self.assertEqual(repair(cursor, apply=True), [])

    def test_production_program_never_connects(self) -> None:
        transport = Mock()
        for instance in ("prod", "production", " PROD "):
            with (
                self.subTest(instance=instance),
                patch.dict("sys.modules", {"psycopg2": transport}),
                patch("sys.argv", ["script", "db", instance, "apply"]),
            ):
                with self.assertRaises(SystemExit):
                    exec(CONNECTION_ISOLATION_PROGRAM, {})
        transport.connect.assert_not_called()

    def test_sms_endpoint_rejects_synchronous_transport_before_network(self) -> None:
        import requests
        from control_plane.odoo_connection_isolation import CONNECTION_ISOLATION_PARAMETERS

        # Exercise requests' actual adapter selection, the transport used by
        # Odoo IAP, with a fake notification and a recreated account token.
        for key in ("sms.endpoint", "iap.endpoint"):
            with (
                patch("socket.create_connection") as network,
                self.assertRaises(requests.exceptions.InvalidSchema),
            ):
                requests.post(
                    CONNECTION_ISOLATION_PARAMETERS[key] + "/api/sms/3/send",
                    json={"account_token": "new-key", "messages": []},
                )
            network.assert_not_called()

    def test_explicit_repair_refuses_production_and_unreviewed_apply(self) -> None:
        for payload in (
            {"instance": "prod", "connection_isolation": "dry-run"},
            {"instance": "production", "connection_isolation": "apply", "reviewed_dry_run": True},
            {"instance": "testing", "connection_isolation": "apply"},
        ):
            with self.subTest(payload=payload), self.assertRaises(ValueError):
                OdooPostDeployRequest(context="example", **payload)

    def test_selected_repair_skips_accounts_addons_and_override_writes(self) -> None:
        from control_plane.dokploy.source import DokployTargetDefinition

        target = DokployTargetDefinition(
            context="example", instance="testing", target_type="compose", target_id="fixture"
        )
        store = Mock()
        with (
            TemporaryDirectory() as directory,
            patch(
                "control_plane.workflows.odoo_post_deploy._resolve_compose_target_definition",
                return_value=target,
            ),
            patch(
                "control_plane.workflows.odoo_post_deploy.dokploy_source.read_dokploy_config",
                return_value=("fixture", "fixture"),
            ),
            patch(
                "control_plane.workflows.odoo_post_deploy.run_connection_isolation",
                return_value={"connection_isolation_ok": "true"},
            ),
            patch(
                "control_plane.workflows.odoo_post_deploy.dokploy_post_deploy.run_compose_post_deploy_update"
            ) as maintenance,
        ):
            result = execute_odoo_post_deploy(
                control_plane_root=Path(directory),
                record_store=store,
                request=OdooPostDeployRequest(
                    context="example",
                    instance="testing",
                    connection_isolation="apply",
                    reviewed_dry_run=True,
                    reviewed_plan_sha256="a" * 64,
                    reason="Repair testing-copy connections",
                ),
            )
        self.assertEqual(result.post_deploy_status, "pass")
        maintenance.assert_not_called()
        self.assertEqual(store.mock_calls, [])


if __name__ == "__main__":
    unittest.main()
