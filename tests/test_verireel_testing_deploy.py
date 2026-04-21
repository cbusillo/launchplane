import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.ship_request import ShipRequest
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.verireel_stable_deploy import (
    VeriReelStableDeployRequest,
    execute_verireel_stable_deploy,
)


class VeriReelTestingDeployWorkflowTests(unittest.TestCase):
    def test_execute_writes_passed_deployment_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            request = VeriReelStableDeployRequest(
                artifact_id="ghcr.io/every/verireel-app:sha-abcdef1234567890",
                source_git_ref="abcdef1234567890",
            )
            ship_request = ShipRequest(
                artifact_id=request.artifact_id,
                context="verireel",
                instance="testing",
                source_git_ref=request.source_git_ref,
                target_name="ver-testing-app",
                target_type="application",
                deploy_mode="dokploy-application-api",
                wait=True,
                verify_health=False,
                destination_health=HealthcheckEvidence(status="skipped"),
            )

            with patch(
                "control_plane.workflows.verireel_stable_deploy.generate_deployment_record_id",
                return_value="deployment-verireel-testing-run-12345-attempt-1",
            ), patch(
                "control_plane.workflows.verireel_stable_deploy.utc_now_timestamp",
                side_effect=[
                    "2026-04-20T18:20:00Z",
                    "2026-04-20T18:21:15Z",
                ],
            ), patch(
                "control_plane.workflows.verireel_stable_deploy._resolve_ship_request",
                return_value=(
                    ship_request,
                    ResolvedTargetEvidence(
                        target_type="application",
                        target_id="testing-app-123",
                        target_name="ver-testing-app",
                    ),
                    300,
                ),
            ), patch(
                "control_plane.workflows.verireel_stable_deploy._execute_dokploy_deploy"
            ):
                result = execute_verireel_stable_deploy(
                    control_plane_root=root,
                    record_store=store,
                    request=request,
                )

            self.assertEqual(result.deployment_record_id, "deployment-verireel-testing-run-12345-attempt-1")
            self.assertEqual(result.deploy_status, "pass")
            self.assertEqual(result.target_id, "testing-app-123")
            deployment = store.read_deployment_record("deployment-verireel-testing-run-12345-attempt-1")
            self.assertEqual(deployment.deploy.status, "pass")
            self.assertEqual(deployment.deploy.started_at, "2026-04-20T18:20:00Z")
            self.assertEqual(deployment.deploy.finished_at, "2026-04-20T18:21:15Z")
            self.assertEqual(deployment.destination_health.status, "skipped")

    def test_execute_writes_failed_deployment_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            request = VeriReelStableDeployRequest(
                artifact_id="ghcr.io/every/verireel-app:sha-abcdef1234567890",
                source_git_ref="abcdef1234567890",
            )
            ship_request = ShipRequest(
                artifact_id=request.artifact_id,
                context="verireel",
                instance="testing",
                source_git_ref=request.source_git_ref,
                target_name="ver-testing-app",
                target_type="application",
                deploy_mode="dokploy-application-api",
                wait=True,
                verify_health=False,
                destination_health=HealthcheckEvidence(status="skipped"),
            )

            with patch(
                "control_plane.workflows.verireel_stable_deploy.generate_deployment_record_id",
                return_value="deployment-verireel-testing-run-12345-attempt-1",
            ), patch(
                "control_plane.workflows.verireel_stable_deploy.utc_now_timestamp",
                side_effect=[
                    "2026-04-20T18:20:00Z",
                    "2026-04-20T18:21:15Z",
                ],
            ), patch(
                "control_plane.workflows.verireel_stable_deploy._resolve_ship_request",
                return_value=(
                    ship_request,
                    ResolvedTargetEvidence(
                        target_type="application",
                        target_id="testing-app-123",
                        target_name="ver-testing-app",
                    ),
                    300,
                ),
            ), patch(
                "control_plane.workflows.verireel_stable_deploy._execute_dokploy_deploy",
                side_effect=click.ClickException("deploy failed"),
            ):
                result = execute_verireel_stable_deploy(
                    control_plane_root=root,
                    record_store=store,
                    request=request,
                )

            self.assertEqual(result.deploy_status, "fail")
            self.assertEqual(result.error_message, "deploy failed")
            deployment = store.read_deployment_record("deployment-verireel-testing-run-12345-attempt-1")
            self.assertEqual(deployment.deploy.status, "fail")
            self.assertEqual(deployment.resolved_target.target_id, "testing-app-123")
