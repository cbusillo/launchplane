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

    def test_deploy_executes_ship_and_reports_testing_release_tuple(self) -> None:
        record_store = Mock()

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

    def test_failed_ship_returns_failed_result(self) -> None:
        record_store = Mock()

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
