import json
import unittest
from email.message import Message
from io import BytesIO
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from urllib.error import HTTPError

import click

from control_plane.workflows.verireel_app_maintenance import (
    VeriReelAppMaintenanceRequest,
    _request_internal_smoke_maintenance_api,
    _command_for_request,
    _resolve_stable_testing_smoke_maintenance_secret,
    execute_verireel_app_maintenance,
)


def _stable_migration_request() -> VeriReelAppMaintenanceRequest:
    return VeriReelAppMaintenanceRequest(
        action="migrate",
        intent="stable-testing-migration",
    )


def _stable_reset_request() -> VeriReelAppMaintenanceRequest:
    return VeriReelAppMaintenanceRequest(
        action="reset-testing",
        intent="stable-testing-reset",
    )


class VeriReelAppMaintenanceTests(unittest.TestCase):
    def test_builds_allow_listed_prisma_migration_command(self) -> None:
        request = _stable_migration_request()

        schedule_name, command = _command_for_request(request)

        self.assertEqual(schedule_name, "ver-apply-prisma-migrations")
        self.assertEqual(command, "npx prisma migrate deploy --config prisma.config.ts")

    def test_owner_admin_actions_no_longer_build_product_image_script_command(self) -> None:
        request = VeriReelAppMaintenanceRequest(
            context="verireel-testing",
            instance="preview",
            action="grant-sponsored",
            intent="remote-e2e-grant-sponsored",
            email="creator+e2e@example.com",
            preview_slug="pr-42",
        )

        with self.assertRaisesRegex(click.ClickException, "internal smoke maintenance API"):
            _command_for_request(request)

    def test_rejects_preview_request_with_invalid_preview_application_name(self) -> None:
        with self.assertRaises(ValueError):
            VeriReelAppMaintenanceRequest(
                context="verireel-testing",
                instance="preview",
                action="grant-sponsored",
                intent="remote-e2e-grant-sponsored",
                email="creator@example.com",
                application_name="ver-testing-app",
            )

    def test_rejects_action_only_request_without_intent(self) -> None:
        with self.assertRaisesRegex(ValueError, "intent"):
            VeriReelAppMaintenanceRequest.model_validate({"action": "migrate"})

    def test_rejects_migration_for_preview_context(self) -> None:
        with self.assertRaises(ValueError):
            VeriReelAppMaintenanceRequest(
                context="verireel-testing",
                instance="preview",
                action="migrate",
                intent="stable-testing-migration",
                preview_slug="pr-42",
            )

    def test_accepts_preview_slug_for_preview_owner_admin_request(self) -> None:
        request = VeriReelAppMaintenanceRequest(
            context="verireel-testing",
            instance="preview",
            action="grant-sponsored",
            intent="remote-e2e-grant-sponsored",
            email="creator@example.com",
            preview_slug="pr-42",
        )

        self.assertEqual(request.preview_slug, "pr-42")
        self.assertEqual(request.application_name, "")

    def test_accepts_remote_e2e_intent_for_matching_preview_action(self) -> None:
        request = VeriReelAppMaintenanceRequest(
            context="verireel-testing",
            instance="preview",
            action="grant-sponsored",
            intent="remote-e2e-grant-sponsored",
            email="creator@example.com",
            preview_slug="pr-42",
        )

        self.assertEqual(request.intent, "remote-e2e-grant-sponsored")

    def test_accepts_stable_testing_remote_e2e_grant_intent(self) -> None:
        request = VeriReelAppMaintenanceRequest(
            context="verireel",
            instance="testing",
            action="grant-sponsored",
            intent="stable-testing-remote-e2e-grant-sponsored",
            email="creator@example.com",
        )

        self.assertEqual(request.intent, "stable-testing-remote-e2e-grant-sponsored")

    def test_executes_stable_testing_remote_e2e_grant_through_internal_api(self) -> None:
        request = VeriReelAppMaintenanceRequest(
            context="verireel",
            instance="testing",
            action="grant-sponsored",
            intent="stable-testing-remote-e2e-grant-sponsored",
            email="creator+e2e@example.com",
        )

        with TemporaryDirectory() as temporary_directory_name:
            with (
                patch(
                    "control_plane.workflows.verireel_app_maintenance._resolve_stable_testing_application",
                    return_value=("ver-testing-app", "app-123"),
                ) as stable_resolve_mock,
                patch(
                    "control_plane.workflows.verireel_app_maintenance._resolve_stable_testing_base_url",
                    return_value="https://testing.verireel.example",
                ),
                patch(
                    "control_plane.workflows.verireel_app_maintenance._resolve_stable_testing_smoke_maintenance_secret",
                    return_value="managed-secret",
                ),
                patch(
                    "control_plane.workflows.verireel_app_maintenance._request_internal_smoke_maintenance_api"
                ) as api_mock,
            ):
                result = execute_verireel_app_maintenance(
                    control_plane_root=Path(temporary_directory_name),
                    request=request,
                )

        self.assertEqual(result.maintenance_status, "pass")
        self.assertEqual(result.application_id, "app-123")
        self.assertEqual(result.intent, "stable-testing-remote-e2e-grant-sponsored")
        self.assertEqual(result.schedule_name, "ver-internal-smoke-grant-sponsored")
        stable_resolve_mock.assert_called_once()
        api_mock.assert_called_once_with(
            base_url="https://testing.verireel.example",
            secret="managed-secret",
            request=request,
        )

    def test_executes_preview_remote_e2e_delete_through_internal_api(self) -> None:
        request = VeriReelAppMaintenanceRequest(
            context="verireel-testing",
            instance="preview",
            action="delete-user",
            intent="remote-e2e-delete-user",
            email="creator+e2e@example.com",
            preview_slug="pr-42",
        )

        with TemporaryDirectory() as temporary_directory_name:
            with (
                patch(
                    "control_plane.workflows.verireel_app_maintenance.control_plane_dokploy.read_dokploy_config",
                    return_value=("https://dokploy.example.test", "managed-token"),
                ),
                patch(
                    "control_plane.workflows.verireel_app_maintenance._resolve_preview_smoke_maintenance_target",
                    return_value=(
                        "ver-preview-pr-42-app",
                        "app-preview-42",
                        "https://pr-42.verireel.example",
                        "preview-secret",
                    ),
                ) as preview_resolve_mock,
                patch(
                    "control_plane.workflows.verireel_app_maintenance._request_internal_smoke_maintenance_api"
                ) as api_mock,
            ):
                result = execute_verireel_app_maintenance(
                    control_plane_root=Path(temporary_directory_name),
                    request=request,
                )

        self.assertEqual(result.maintenance_status, "pass")
        self.assertEqual(result.application_name, "ver-preview-pr-42-app")
        self.assertEqual(result.application_id, "app-preview-42")
        self.assertEqual(result.schedule_name, "ver-internal-smoke-delete-user")
        preview_resolve_mock.assert_called_once_with(
            host="https://dokploy.example.test",
            token="managed-token",
            application_name="ver-preview-pr-42-app",
        )
        api_mock.assert_called_once_with(
            base_url="https://pr-42.verireel.example",
            secret="preview-secret",
            request=request,
        )

    def test_internal_api_request_posts_json_with_bearer_secret(self) -> None:
        class FakeResponse:
            status = 200

            def __enter__(self) -> "FakeResponse":
                return self

            def __exit__(self, *args: object) -> None:
                return None

            def read(self) -> bytes:
                return b'{"ok":true}'

        request = VeriReelAppMaintenanceRequest(
            action="promote-owner",
            intent="owner-route-promote-owner",
            email="owner-e2e@example.com",
        )

        with patch(
            "control_plane.workflows.verireel_app_maintenance.urlopen",
            return_value=FakeResponse(),
        ) as urlopen_mock:
            _request_internal_smoke_maintenance_api(
                base_url="https://testing.verireel.example/",
                secret="managed-secret",
                request=request,
            )

        http_request = urlopen_mock.call_args.args[0]
        self.assertEqual(
            http_request.full_url,
            "https://testing.verireel.example/api/internal/smoke-maintenance",
        )
        self.assertEqual(http_request.get_method(), "POST")
        self.assertEqual(http_request.get_header("Authorization"), "Bearer managed-secret")
        self.assertEqual(http_request.get_header("Content-type"), "application/json")
        self.assertEqual(
            json.loads(http_request.data.decode("utf-8")),
            {"action": "promote-owner", "email": "owner-e2e@example.com"},
        )

    def test_internal_api_request_redacts_http_error_body(self) -> None:
        request = VeriReelAppMaintenanceRequest(
            action="promote-owner",
            intent="owner-route-promote-owner",
            email="owner-e2e@example.com",
        )
        http_error = HTTPError(
            "https://testing.verireel.example/api/internal/smoke-maintenance",
            403,
            "Forbidden",
            hdrs=Message(),
            fp=BytesIO(b"not allowed"),
        )

        with patch(
            "control_plane.workflows.verireel_app_maintenance.urlopen",
            side_effect=http_error,
        ):
            with self.assertRaisesRegex(click.ClickException, "HTTP 403\\."):
                _request_internal_smoke_maintenance_api(
                    base_url="https://testing.verireel.example",
                    secret="managed-secret",
                    request=request,
                )

    def test_stable_secret_resolution_fails_closed_when_missing(self) -> None:
        with patch(
            "control_plane.workflows.verireel_app_maintenance.control_plane_runtime_environments.resolve_runtime_environment_values",
            return_value={},
        ):
            with self.assertRaisesRegex(click.ClickException, "VERIREEL_SMOKE_MAINTENANCE_SECRET"):
                _resolve_stable_testing_smoke_maintenance_secret(control_plane_root=Path("."))

    def test_rejects_intent_that_does_not_match_action_and_context(self) -> None:
        with self.assertRaises(ValueError):
            VeriReelAppMaintenanceRequest(
                context="verireel",
                instance="testing",
                action="grant-sponsored",
                intent="remote-e2e-grant-sponsored",
                email="creator@example.com",
            )

    def test_builds_testing_reset_command(self) -> None:
        request = _stable_reset_request()

        schedule_name, command = _command_for_request(request)

        self.assertEqual(schedule_name, "ver-testing-reset")
        self.assertEqual(
            command,
            "node prisma/reset-testing-job.mjs && npx prisma migrate deploy --schema prisma/schema.prisma && node prisma/seed.mjs",
        )

    def test_preview_target_reads_secret_from_application_one_payload(self) -> None:
        from control_plane.workflows.verireel_app_maintenance import (
            _resolve_preview_smoke_maintenance_target,
        )

        def fake_dokploy_request(**kwargs: object) -> object:
            path = kwargs["path"]
            if path == "/api/project.all":
                return [
                    {
                        "environments": [
                            {
                                "applications": [
                                    {
                                        "name": "ver-preview-pr-42-app",
                                        "applicationId": "app-preview-42",
                                    }
                                ]
                            }
                        ]
                    }
                ]
            if path == "/api/domain.byApplicationId":
                return [{"host": "pr-42.verireel.example"}]
            if path == "/api/application.one":
                return {
                    "applicationId": "app-preview-42",
                    "env": "VERIREEL_SMOKE_MAINTENANCE_SECRET=preview-secret\n",
                }
            raise AssertionError(f"unexpected path {path!r}")

        with patch(
            "control_plane.workflows.verireel_app_maintenance.control_plane_dokploy.dokploy_request",
            side_effect=fake_dokploy_request,
        ):
            result = _resolve_preview_smoke_maintenance_target(
                host="https://dokploy.example.test",
                token="managed-token",
                application_name="ver-preview-pr-42-app",
            )

        self.assertEqual(
            result,
            (
                "ver-preview-pr-42-app",
                "app-preview-42",
                "https://pr-42.verireel.example",
                "preview-secret",
            ),
        )

    def test_executes_stable_testing_command_through_launchplane_dokploy_config(self) -> None:
        request = _stable_migration_request()

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
        self.assertEqual(result.intent, "stable-testing-migration")
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
        request = _stable_reset_request()

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
        request = _stable_migration_request()

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

    def test_reports_failed_internal_api_without_leaking_response_body(self) -> None:
        request = VeriReelAppMaintenanceRequest(
            context="verireel",
            instance="testing",
            action="promote-owner",
            intent="owner-route-promote-owner",
            email="owner-e2e@example.com",
        )

        with TemporaryDirectory() as temporary_directory_name:
            with (
                patch(
                    "control_plane.workflows.verireel_app_maintenance._resolve_stable_testing_application",
                    return_value=("ver-testing-app", "app-123"),
                ),
                patch(
                    "control_plane.workflows.verireel_app_maintenance._resolve_stable_testing_base_url",
                    return_value="https://testing.verireel.example",
                ),
                patch(
                    "control_plane.workflows.verireel_app_maintenance._resolve_stable_testing_smoke_maintenance_secret",
                    return_value="managed-secret",
                ),
                patch(
                    "control_plane.workflows.verireel_app_maintenance._request_internal_smoke_maintenance_api",
                    side_effect=click.ClickException(
                        "VeriReel smoke maintenance API returned HTTP 500."
                    ),
                ),
            ):
                result = execute_verireel_app_maintenance(
                    control_plane_root=Path(temporary_directory_name),
                    request=request,
                )

        self.assertEqual(result.maintenance_status, "fail")
        self.assertEqual(
            result.error_message,
            "VeriReel smoke maintenance API returned HTTP 500.",
        )
        self.assertEqual(result.schedule_name, "ver-internal-smoke-promote-owner")


if __name__ == "__main__":
    unittest.main()
