import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane.workflows.verireel_app_maintenance import (
    VeriReelAppMaintenanceRequest,
    _command_for_request,
    execute_verireel_app_maintenance,
)


class VeriReelAppMaintenanceTests(unittest.TestCase):
    def test_builds_allow_listed_prisma_migration_command(self) -> None:
        request = VeriReelAppMaintenanceRequest(action="migrate")

        schedule_name, command = _command_for_request(request)

        self.assertEqual(schedule_name, "ver-apply-prisma-migrations")
        self.assertEqual(command, "npx prisma migrate deploy --config prisma.config.ts")

    def test_builds_shell_quoted_remote_owner_command(self) -> None:
        request = VeriReelAppMaintenanceRequest(
            action="grant-sponsored",
            email="creator+e2e@example.com",
        )

        schedule_name, command = _command_for_request(request)

        self.assertEqual(schedule_name, "ver-remote-e2e-grant-sponsored")
        self.assertEqual(
            command,
            "node scripts/ops/remote-owner-admin.mjs --action grant-sponsored --email creator+e2e@example.com",
        )

    def test_rejects_preview_request_with_invalid_preview_application_name(self) -> None:
        with self.assertRaises(ValueError):
            VeriReelAppMaintenanceRequest(
                context="verireel-testing",
                instance="preview",
                action="grant-sponsored",
                email="creator@example.com",
                application_name="ver-testing-app",
            )

    def test_rejects_migration_for_preview_context(self) -> None:
        with self.assertRaises(ValueError):
            VeriReelAppMaintenanceRequest(
                context="verireel-testing",
                instance="preview",
                action="migrate",
                preview_slug="pr-42",
            )

    def test_accepts_preview_slug_for_preview_owner_admin_request(self) -> None:
        request = VeriReelAppMaintenanceRequest(
            context="verireel-testing",
            instance="preview",
            action="grant-sponsored",
            email="creator@example.com",
            preview_slug="pr-42",
        )

        self.assertEqual(request.preview_slug, "pr-42")
        self.assertEqual(request.application_name, "")

    def test_builds_testing_reset_command(self) -> None:
        request = VeriReelAppMaintenanceRequest(action="reset-testing")

        schedule_name, command = _command_for_request(request)

        self.assertEqual(schedule_name, "ver-testing-reset")
        self.assertEqual(
            command,
            "node prisma/reset-testing-job.mjs && npx prisma migrate deploy --schema prisma/schema.prisma && node prisma/seed.mjs",
        )

    def test_executes_stable_testing_command_through_launchplane_dokploy_config(self) -> None:
        request = VeriReelAppMaintenanceRequest(action="migrate")

        with TemporaryDirectory() as temporary_directory_name:
            with (
                patch(
                    "control_plane.workflows.verireel_app_maintenance.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.test", "managed-token"),
                ),
                patch(
                    "control_plane.workflows.verireel_app_maintenance._resolve_stable_testing_application",
                    return_value=("ver-testing-app", "app-123"),
                ) as resolve_mock,
                patch(
                    "control_plane.workflows.verireel_app_maintenance._run_application_command_with_retries"
                ) as run_mock,
                patch(
                    "control_plane.workflows.verireel_app_maintenance._trigger_application_deploy"
                ) as deploy_mock,
            ):
                result = execute_verireel_app_maintenance(
                    control_plane_root=Path(temporary_directory_name),
                    request=request,
                )

        self.assertEqual(result.maintenance_status, "pass")
        self.assertEqual(result.application_id, "app-123")
        resolve_mock.assert_called_once()
        run_mock.assert_called_once_with(
            host="https://dokploy.example.test",
            token="managed-token",
            application_id="app-123",
            schedule_name="ver-apply-prisma-migrations",
            command="npx prisma migrate deploy --config prisma.config.ts",
            timeout_seconds=300,
        )
        deploy_mock.assert_not_called()

    def test_redeploys_after_testing_reset(self) -> None:
        request = VeriReelAppMaintenanceRequest(action="reset-testing")

        with TemporaryDirectory() as temporary_directory_name:
            with (
                patch(
                    "control_plane.workflows.verireel_app_maintenance.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.test", "managed-token"),
                ),
                patch(
                    "control_plane.workflows.verireel_app_maintenance._resolve_stable_testing_application",
                    return_value=("ver-testing-app", "app-123"),
                ),
                patch(
                    "control_plane.workflows.verireel_app_maintenance._run_application_command_with_retries"
                ) as run_mock,
                patch(
                    "control_plane.workflows.verireel_app_maintenance._trigger_application_deploy"
                ) as deploy_mock,
            ):
                result = execute_verireel_app_maintenance(
                    control_plane_root=Path(temporary_directory_name),
                    request=request,
                )

        self.assertEqual(result.maintenance_status, "pass")
        run_mock.assert_called_once()
        deploy_mock.assert_called_once_with(
            host="https://dokploy.example.test",
            token="managed-token",
            application_id="app-123",
        )

    def test_reports_failed_command_without_throwing(self) -> None:
        request = VeriReelAppMaintenanceRequest(action="migrate")

        with TemporaryDirectory() as temporary_directory_name:
            with (
                patch(
                    "control_plane.workflows.verireel_app_maintenance.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.test", "managed-token"),
                ),
                patch(
                    "control_plane.workflows.verireel_app_maintenance._resolve_stable_testing_application",
                    return_value=("ver-testing-app", "app-123"),
                ),
                patch(
                    "control_plane.workflows.verireel_app_maintenance._run_application_command_with_retries",
                    side_effect=click.ClickException("schedule failed"),
                ),
            ):
                result = execute_verireel_app_maintenance(
                    control_plane_root=Path(temporary_directory_name),
                    request=request,
                )

        self.assertEqual(result.maintenance_status, "fail")
        self.assertIn("schedule failed", result.error_message)


if __name__ == "__main__":
    unittest.main()
