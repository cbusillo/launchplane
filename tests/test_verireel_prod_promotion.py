import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.verireel_prod_promotion import (
    VeriReelProdPromotionRequest,
    VeriReelRolloutVerificationResult,
    execute_verireel_prod_promotion,
)
from control_plane.workflows.verireel_stable_deploy import VeriReelStableDeployResult


class VeriReelProdPromotionWorkflowTests(unittest.TestCase):
    def test_execute_writes_promotion_record_after_passing_backup_gate(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_backup_gate_record(
                BackupGateRecord(
                    record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    context="verireel",
                    instance="prod",
                    created_at="2026-04-21T18:05:00Z",
                    source="verireel-prod-gate",
                    status="pass",
                    evidence={
                        "snapshot_name": "ver-predeploy-20260421T180500Z",
                        "manifest_path": "scratch/prod-gates/ver-predeploy-20260421T180500Z.json",
                    },
                )
            )
            store.write_deployment_record(
                DeploymentRecord(
                    record_id="deployment-verireel-prod-run-12345-attempt-1",
                    artifact_identity={"artifact_id": "ghcr.io/every/verireel-app:sha-abcdef1234567890"},
                    context="verireel",
                    instance="prod",
                    source_git_ref="abcdef1234567890",
                    resolved_target=ResolvedTargetEvidence(
                        target_type="application",
                        target_id="prod-app-123",
                        target_name="ver-prod-app",
                    ),
                    deploy=DeploymentEvidence(
                        target_name="ver-prod-app",
                        target_type="application",
                        deploy_mode="dokploy-application-api",
                        deployment_id="control-plane-dokploy",
                        status="pass",
                        started_at="2026-04-21T18:20:00Z",
                        finished_at="2026-04-21T18:21:15Z",
                    ),
                )
            )
            request = VeriReelProdPromotionRequest(
                artifact_id="ghcr.io/every/verireel-app:sha-abcdef1234567890",
                source_git_ref="abcdef1234567890",
                backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                promotion_record_id="promotion-verireel-testing-to-prod-run-12345-attempt-1",
                expected_build_revision="abcdef1234567890",
                expected_build_tag="sha-abcdef1234567890",
            )

            with patch(
                "control_plane.workflows.verireel_prod_promotion.execute_verireel_stable_deploy",
                return_value=VeriReelStableDeployResult(
                    deployment_record_id="deployment-verireel-prod-run-12345-attempt-1",
                    deploy_status="pass",
                    deploy_started_at="2026-04-21T18:20:00Z",
                    deploy_finished_at="2026-04-21T18:21:15Z",
                    target_name="ver-prod-app",
                    target_type="application",
                    target_id="prod-app-123",
                ),
            ), patch(
                "control_plane.workflows.verireel_prod_promotion._verify_rollout",
                return_value=VeriReelRolloutVerificationResult(
                    status="pass",
                    base_url="https://ver-prod.shinycomputers.com",
                    health_urls=("https://ver-prod.shinycomputers.com/api/health",),
                    started_at="2026-04-21T18:21:16Z",
                    finished_at="2026-04-21T18:21:45Z",
                ),
            ):
                result = execute_verireel_prod_promotion(
                    control_plane_root=root,
                    record_store=store,
                    request=request,
                )

            self.assertEqual(
                result.promotion_record_id,
                "promotion-verireel-testing-to-prod-run-12345-attempt-1",
            )
            self.assertEqual(
                result.deployment_record_id,
                "deployment-verireel-prod-run-12345-attempt-1",
            )
            self.assertEqual(result.deploy_status, "pass")
            self.assertEqual(result.rollout_status, "pass")
            promotion = store.read_promotion_record(
                "promotion-verireel-testing-to-prod-run-12345-attempt-1"
            )
            self.assertEqual(promotion.backup_record_id, "backup-gate-verireel-prod-run-12345-attempt-1")
            self.assertEqual(promotion.deploy.status, "pass")
            self.assertEqual(promotion.deploy.deployment_id, "prod-app-123")
            self.assertEqual(promotion.deploy.started_at, "2026-04-21T18:20:00Z")
            self.assertEqual(promotion.deploy.finished_at, "2026-04-21T18:21:15Z")
            self.assertEqual(promotion.destination_health.status, "pass")
            self.assertEqual(
                promotion.destination_health.urls,
                ("https://ver-prod.shinycomputers.com/api/health",),
            )

    def test_execute_writes_failed_rollout_status_when_rollout_verification_fails(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            store.write_backup_gate_record(
                BackupGateRecord(
                    record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    context="verireel",
                    instance="prod",
                    created_at="2026-04-21T18:05:00Z",
                    source="verireel-prod-gate",
                    status="pass",
                    evidence={"snapshot_name": "ver-predeploy-20260421T180500Z"},
                )
            )
            request = VeriReelProdPromotionRequest(
                artifact_id="ghcr.io/every/verireel-app:sha-abcdef1234567890",
                source_git_ref="abcdef1234567890",
                backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                promotion_record_id="promotion-verireel-testing-to-prod-run-12345-attempt-1",
                expected_build_revision="abcdef1234567890",
                expected_build_tag="sha-abcdef1234567890",
            )

            with patch(
                "control_plane.workflows.verireel_prod_promotion.execute_verireel_stable_deploy",
                return_value=VeriReelStableDeployResult(
                    deployment_record_id="deployment-verireel-prod-run-12345-attempt-1",
                    deploy_status="pass",
                    deploy_started_at="2026-04-21T18:20:00Z",
                    deploy_finished_at="2026-04-21T18:21:15Z",
                    target_name="ver-prod-app",
                    target_type="application",
                    target_id="prod-app-123",
                ),
            ), patch(
                "control_plane.workflows.verireel_prod_promotion._verify_rollout",
                side_effect=click.ClickException("VeriReel prod rollout page verification expected https://ver-prod.shinycomputers.com/ to include \"VeriReel\"."),
            ), patch(
                "control_plane.workflows.verireel_prod_promotion._resolve_rollout_base_urls",
                return_value=("https://ver-prod.shinycomputers.com",),
            ):
                result = execute_verireel_prod_promotion(
                    control_plane_root=root,
                    record_store=store,
                    request=request,
                )

            self.assertEqual(result.deploy_status, "pass")
            self.assertEqual(result.rollout_status, "fail")
            self.assertIn("rollout page verification", result.error_message)
            promotion = store.read_promotion_record(
                "promotion-verireel-testing-to-prod-run-12345-attempt-1"
            )
            self.assertEqual(promotion.backup_gate.status, "pass")
            self.assertEqual(promotion.deploy.status, "pass")
            self.assertEqual(promotion.destination_health.status, "fail")
            self.assertEqual(
                promotion.destination_health.urls,
                ("https://ver-prod.shinycomputers.com/api/health",),
            )

    def test_execute_writes_failed_promotion_record_when_backup_gate_is_missing(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            state_dir = root / "state"
            store = FilesystemRecordStore(state_dir=state_dir)
            request = VeriReelProdPromotionRequest(
                artifact_id="ghcr.io/every/verireel-app:sha-abcdef1234567890",
                source_git_ref="abcdef1234567890",
                backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                promotion_record_id="promotion-verireel-testing-to-prod-run-12345-attempt-1",
            )

            result = execute_verireel_prod_promotion(
                control_plane_root=root,
                record_store=store,
                request=request,
            )

            self.assertEqual(result.deploy_status, "fail")
            self.assertIn("requires stored backup gate record", result.error_message)
            promotion = store.read_promotion_record(
                "promotion-verireel-testing-to-prod-run-12345-attempt-1"
            )
            self.assertEqual(promotion.deploy.status, "fail")
            self.assertEqual(promotion.backup_gate.status, "fail")
            self.assertEqual(promotion.deployment_record_id, "")
