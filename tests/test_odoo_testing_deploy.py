import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import click
from pydantic import ValidationError

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
)
from control_plane.contracts.ship_request import ShipRequest
from control_plane.workflows.odoo_testing_deploy import (
    OdooTestingDeployRequest,
    execute_odoo_testing_deploy,
)


def _ship_request() -> ShipRequest:
    return ShipRequest(
        artifact_id="artifact-cm-new",
        context="cm",
        instance="testing",
        source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
        target_name="cm-testing",
        target_type="compose",
        deploy_mode="dokploy-compose-api",
        wait=True,
        timeout_seconds=1800,
        verify_health=True,
        health_timeout_seconds=180,
        destination_health=HealthcheckEvidence(
            urls=("https://cm-testing.example/launchplane/health",),
            timeout_seconds=180,
            status="pending",
        ),
    )


def _ship_request_without_health_url() -> ShipRequest:
    request = _ship_request()
    return request.model_copy(update={"destination_health": HealthcheckEvidence(status="skipped")})


def _profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="odoo-tenant-cm",
        display_name="CM Odoo",
        repository="cbusillo/odoo-tenant-cm",
        driver_id="odoo",
        image=ProductImageProfile(repository="ghcr.io/cbusillo/odoo-tenant-cm"),
        runtime_port=8069,
        health_path="/launchplane/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="cm",
                health_url="https://cm-testing.example/launchplane/health",
            ),
        ),
        updated_at="2026-05-30T20:30:00Z",
        source="test",
    )


def _deployment_record() -> DeploymentRecord:
    return DeploymentRecord(
        record_id="deployment-cm-testing",
        artifact_identity=ArtifactIdentityReference(artifact_id="artifact-cm-new"),
        context="cm",
        instance="testing",
        source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
        wait_for_completion=True,
        deploy=DeploymentEvidence(
            target_name="cm-testing",
            target_type="compose",
            deploy_mode="dokploy-compose-api",
            deployment_id="control-plane-dokploy",
            status="pass",
        ),
        post_deploy_update=PostDeployUpdateEvidence(
            attempted=True,
            status="pass",
            detail="Post-deploy passed.",
        ),
        destination_health=HealthcheckEvidence(
            verified=True,
            urls=("https://cm-testing.example/launchplane/health",),
            timeout_seconds=180,
            status="pass",
        ),
    )


class OdooTestingDeployWorkflowTests(unittest.TestCase):
    def test_request_rejects_non_testing_instance(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires instance 'testing'"):
            OdooTestingDeployRequest(
                context="cm",
                instance="prod",
                artifact_id="artifact-cm-new",
                source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
            )

    def test_request_rejects_async_deploy_without_tuple_minting(self) -> None:
        with self.assertRaisesRegex(ValidationError, "requires wait=true"):
            OdooTestingDeployRequest(
                context="cm",
                artifact_id="artifact-cm-new",
                source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
                wait=False,
                verify_health=False,
            )

    def test_deploy_executes_ship_and_reports_testing_release_tuple(self) -> None:
        record_store = Mock()
        record_store.read_product_profile_record.return_value = _profile()

        with (
            patch("control_plane.cli._resolve_native_ship_request", return_value=_ship_request()),
            patch("control_plane.cli._read_artifact_manifest"),
            patch("control_plane.cli._execute_ship", return_value=(None, _deployment_record())) as ship,
        ):
            result = execute_odoo_testing_deploy(
                control_plane_root=Path("/control-plane"),
                state_dir=Path("/state"),
                database_url="postgresql://launchplane.example/db",
                record_store=record_store,
                request=OdooTestingDeployRequest(
                    context="cm",
                    artifact_id="artifact-cm-new",
                    source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
                ),
            )

        self.assertEqual(result.deployment_status, "pass")
        self.assertEqual(result.post_deploy_status, "pass")
        self.assertEqual(result.destination_health_status, "pass")
        self.assertEqual(result.deployment_record_id, "deployment-cm-testing")
        self.assertEqual(result.release_tuple_id, "cm-testing-artifact-cm-new")
        ship.assert_called_once()
        self.assertTrue(ship.call_args.kwargs["mint_release_tuple"])
        record_store.read_product_profile_record.assert_not_called()

    def test_deploy_does_not_require_profile_when_target_health_url_resolves(self) -> None:
        record_store = Mock()
        record_store.read_product_profile_record.side_effect = FileNotFoundError(
            "odoo-tenant-cm"
        )

        with (
            patch("control_plane.cli._resolve_native_ship_request", return_value=_ship_request()),
            patch("control_plane.cli._read_artifact_manifest"),
            patch("control_plane.cli._execute_ship", return_value=(None, _deployment_record())),
        ):
            result = execute_odoo_testing_deploy(
                control_plane_root=Path("/control-plane"),
                state_dir=Path("/state"),
                database_url="postgresql://launchplane.example/db",
                record_store=record_store,
                request=OdooTestingDeployRequest(
                    context="cm",
                    artifact_id="artifact-cm-new",
                    source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
                ),
            )

        self.assertEqual(result.deployment_status, "pass")
        self.assertEqual(result.release_tuple_id, "cm-testing-artifact-cm-new")
        record_store.read_product_profile_record.assert_not_called()

    def test_deploy_uses_profile_health_url_when_target_has_no_health_url(self) -> None:
        record_store = Mock()
        record_store.read_product_profile_record.return_value = _profile()
        no_health_error = click.ClickException(
            "Healthcheck verification requested but no target domain/URL was resolved. "
            "Define domains in the tracked Dokploy target record or disable with --no-verify-health."
        )

        with (
            patch(
                "control_plane.cli._resolve_native_ship_request",
                side_effect=(no_health_error, _ship_request_without_health_url()),
            ) as resolve_ship,
            patch("control_plane.cli._read_artifact_manifest"),
            patch("control_plane.cli._execute_ship", return_value=(None, _deployment_record())) as ship,
        ):
            execute_odoo_testing_deploy(
                control_plane_root=Path("/control-plane"),
                state_dir=Path("/state"),
                database_url="postgresql://launchplane.example/db",
                record_store=record_store,
                request=OdooTestingDeployRequest(
                    context="cm",
                    product="odoo-tenant-cm",
                    artifact_id="artifact-cm-new",
                    source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
                ),
            )

        self.assertEqual(resolve_ship.call_count, 2)
        record_store.read_product_profile_record.assert_called_once_with("odoo-tenant-cm")
        self.assertTrue(resolve_ship.call_args_list[0].kwargs["verify_health"])
        self.assertFalse(resolve_ship.call_args_list[1].kwargs["verify_health"])
        normalized_request = ship.call_args.kwargs["request"]
        self.assertTrue(normalized_request.verify_health)
        self.assertEqual(
            normalized_request.destination_health.urls,
            ("https://cm-testing.example/launchplane/health",),
        )

    def test_deploy_preserves_target_health_url_when_resolved(self) -> None:
        record_store = Mock()
        record_store.read_product_profile_record.return_value = _profile()

        with (
            patch("control_plane.cli._resolve_native_ship_request", return_value=_ship_request()) as resolve_ship,
            patch("control_plane.cli._read_artifact_manifest"),
            patch("control_plane.cli._execute_ship", return_value=(None, _deployment_record())) as ship,
        ):
            execute_odoo_testing_deploy(
                control_plane_root=Path("/control-plane"),
                state_dir=Path("/state"),
                database_url="postgresql://launchplane.example/db",
                record_store=record_store,
                request=OdooTestingDeployRequest(
                    context="cm",
                    product="odoo-tenant-cm",
                    artifact_id="artifact-cm-new",
                    source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
                ),
            )

        resolve_ship.assert_called_once()
        self.assertTrue(resolve_ship.call_args.kwargs["verify_health"])
        normalized_request = ship.call_args.kwargs["request"]
        self.assertTrue(normalized_request.verify_health)
        self.assertEqual(
            normalized_request.destination_health.urls,
            ("https://cm-testing.example/launchplane/health",),
        )

    def test_failed_ship_returns_failed_result(self) -> None:
        record_store = Mock()
        record_store.read_product_profile_record.return_value = _profile()

        with (
            patch("control_plane.cli._resolve_native_ship_request", return_value=_ship_request()),
            patch("control_plane.cli._read_artifact_manifest"),
            patch("control_plane.cli._execute_ship", side_effect=click.ClickException("deploy failed")),
        ):
            result = execute_odoo_testing_deploy(
                control_plane_root=Path("/control-plane"),
                state_dir=Path("/state"),
                database_url="postgresql://launchplane.example/db",
                record_store=record_store,
                request=OdooTestingDeployRequest(
                    context="cm",
                    artifact_id="artifact-cm-new",
                    source_git_ref="848bf1b69ff3adbe9b255c61c7b8f5ca04efbcbb",
                ),
            )

        self.assertEqual(result.deployment_status, "fail")
        self.assertIn("deploy failed", result.error_message)


if __name__ == "__main__":
    unittest.main()
