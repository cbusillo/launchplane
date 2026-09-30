import unittest
from unittest.mock import patch

from control_plane import dokploy as control_plane_dokploy
from control_plane.dokploy import post_deploy as dokploy_post_deploy


def _run_restore(log_lines: tuple[str, ...]) -> dict[str, str]:
    target_definition = control_plane_dokploy.DokployTargetDefinition(
        context="opw", instance="testing", target_id="compose-123", target_name="opw-testing"
    )
    with (
        patch(
            "control_plane.dokploy.api.fetch_dokploy_target_payload",
            return_value={
                "env": (
                    "ODOO_DB_NAME=opw_testing\n"
                    "ODOO_UPSTREAM_HOST=source.example.com\n"
                    "ODOO_UPSTREAM_USER=root\n"
                    "ODOO_UPSTREAM_DB_NAME=upstream\n"
                    "ODOO_UPSTREAM_DB_USER=odoo\n"
                    "ODOO_UPSTREAM_FILESTORE_PATH=/volumes/data/filestore/upstream\n"
                ),
                "appName": "opw-testing-app",
                "serverId": "server-123",
            },
        ),
        patch("control_plane.dokploy.api.find_matching_dokploy_schedule", return_value=None),
        patch(
            "control_plane.dokploy.api.upsert_dokploy_schedule",
            return_value={"scheduleId": "schedule-123"},
        ),
        patch(
            "control_plane.dokploy.api.latest_deployment_for_schedule",
            return_value={"deploymentId": "schedule-before"},
        ),
        patch(
            "control_plane.dokploy.api.wait_for_dokploy_schedule_deployment",
            return_value="deployment=schedule-after status=done",
        ),
        patch(
            "control_plane.dokploy.api.fetch_dokploy_deployment_logs",
            return_value=log_lines,
        ),
        patch(
            "control_plane.dokploy.api.dokploy_request",
            side_effect=lambda **_kwargs: {"ok": True},
        ),
    ):
        return control_plane_dokploy.run_compose_post_deploy_update(
            host="https://dokploy.example.com",
            token="secret-token",
            target_definition=target_definition,
            env_file=None,
            run_destructive_restore=True,
        )


class RestoreReadbackTests(unittest.TestCase):
    def test_restore_passes_only_with_the_completion_marker(self) -> None:
        evidence = _run_restore(
            (
                "odoo_restore_completed=true",
                "odoo_module_update_image_match=true",
                "odoo_module_update_modules_configured=true",
                "odoo_module_update_completed=true",
                "integration_readback_ok=true",
            )
        )

        self.assertEqual(evidence["odoo_restore_completed"], "true")

    def test_restore_without_the_completion_marker_is_failed(self) -> None:
        with self.assertRaisesRegex(
            dokploy_post_deploy.OdooPostDeployReadbackFailure, "did not prove the restore"
        ):
            _run_restore(("odoo_module_update_completed=true",))

    def test_restore_that_logged_a_failure_is_failed(self) -> None:
        with self.assertRaisesRegex(
            dokploy_post_deploy.OdooPostDeployReadbackFailure, "logged a restore failure"
        ):
            _run_restore(
                (
                    "odoo_restore_failure_logged=true",
                    "odoo_restore_completed=true",
                )
            )


if __name__ == "__main__":
    unittest.main()
