import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PromotionRecord,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.verireel_prod_rollback import (
    VeriReelProdRollbackRequest,
    VeriReelProdRollbackWorkerResult,
    execute_verireel_prod_rollback,
)
from control_plane.workflows.verireel_prod_promotion import VeriReelRolloutVerificationResult


class VeriReelProdRollbackWorkflowTests(unittest.TestCase):
    def _record_store(self, root: Path) -> FilesystemRecordStore:
        return FilesystemRecordStore(root / "state")

    def _write_backup_gate(self, record_store: FilesystemRecordStore) -> None:
        record_store.write_backup_gate_record(
            BackupGateRecord(
                record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                context="verireel",
                instance="prod",
                created_at="2026-04-21T18:00:00Z",
                source="verireel-prod-gate",
                required=True,
                status="pass",
                evidence={"snapshot_name": "ver-predeploy-20260421-180000"},
            )
        )

    def _write_promotion(self, record_store: FilesystemRecordStore) -> None:
        record_store.write_promotion_record(
            PromotionRecord(
                record_id="promotion-verireel-testing-to-prod-run-12345-attempt-1",
                artifact_identity=ArtifactIdentityReference(
                    artifact_id="ghcr.io/every/verireel-app:sha-abcdef1234567890"
                ),
                deployment_record_id="deployment-verireel-prod-run-12345-attempt-1",
                backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                context="verireel",
                from_instance="testing",
                to_instance="prod",
                deploy=DeploymentEvidence(
                    target_name="ver-prod-app",
                    target_type="application",
                    deploy_mode="dokploy-application-api",
                    deployment_id="prod-app-123",
                    status="pass",
                    started_at="2026-04-21T18:10:00Z",
                    finished_at="2026-04-21T18:11:00Z",
                ),
            )
        )

    def test_execute_verireel_prod_rollback_records_pass_statuses(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            self._write_backup_gate(record_store)
            self._write_promotion(record_store)

            with patch(
                "control_plane.workflows.verireel_prod_rollback._run_delegated_worker",
                return_value=VeriReelProdRollbackWorkerResult(
                    status="pass",
                    snapshot_name="ver-predeploy-20260421-180000",
                    started_at="2026-04-21T18:20:00Z",
                    finished_at="2026-04-21T18:21:00Z",
                    detail="Rollback completed.",
                ),
            ), patch(
                "control_plane.workflows.verireel_prod_rollback._verify_post_rollback_health",
                return_value=VeriReelRolloutVerificationResult(
                    status="pass",
                    base_url="https://ver-prod.shinycomputers.com",
                    health_urls=("https://ver-prod.shinycomputers.com/api/health",),
                    started_at="2026-04-21T18:21:00Z",
                    finished_at="2026-04-21T18:22:00Z",
                ),
            ):
                result = execute_verireel_prod_rollback(
                    control_plane_root=root,
                    record_store=record_store,
                    request=VeriReelProdRollbackRequest(
                        promotion_record_id="promotion-verireel-testing-to-prod-run-12345-attempt-1",
                        backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    ),
                )

            self.assertEqual(result.rollback_status, "pass")
            self.assertEqual(result.rollback_health_status, "pass")
            updated_record = record_store.read_promotion_record(result.promotion_record_id)
            self.assertEqual(updated_record.rollback.status, "pass")
            self.assertEqual(updated_record.rollback.snapshot_name, "ver-predeploy-20260421-180000")
            self.assertEqual(updated_record.rollback_health.status, "pass")

    def test_execute_verireel_prod_rollback_records_worker_failure(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            self._write_backup_gate(record_store)
            self._write_promotion(record_store)

            with patch(
                "control_plane.workflows.verireel_prod_rollback._run_delegated_worker",
                return_value=VeriReelProdRollbackWorkerResult(
                    status="fail",
                    snapshot_name="ver-predeploy-20260421-180000",
                    started_at="2026-04-21T18:20:00Z",
                    finished_at="2026-04-21T18:20:30Z",
                    detail="pct rollback failed",
                ),
            ):
                result = execute_verireel_prod_rollback(
                    control_plane_root=root,
                    record_store=record_store,
                    request=VeriReelProdRollbackRequest(
                        promotion_record_id="promotion-verireel-testing-to-prod-run-12345-attempt-1",
                        backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    ),
                )

            self.assertEqual(result.rollback_status, "fail")
            self.assertEqual(result.rollback_health_status, "skipped")
            self.assertEqual(result.error_message, "pct rollback failed")
            updated_record = record_store.read_promotion_record(result.promotion_record_id)
            self.assertEqual(updated_record.rollback.status, "fail")
            self.assertEqual(updated_record.rollback_health.status, "skipped")

    def test_execute_verireel_prod_rollback_records_health_failure(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            self._write_backup_gate(record_store)
            self._write_promotion(record_store)

            with patch(
                "control_plane.workflows.verireel_prod_rollback._run_delegated_worker",
                return_value=VeriReelProdRollbackWorkerResult(
                    status="pass",
                    snapshot_name="ver-predeploy-20260421-180000",
                    started_at="2026-04-21T18:20:00Z",
                    finished_at="2026-04-21T18:21:00Z",
                    detail="Rollback completed.",
                ),
            ), patch(
                "control_plane.workflows.verireel_prod_rollback._resolve_rollout_base_urls",
                return_value=("https://ver-prod.shinycomputers.com",),
            ), patch(
                "control_plane.workflows.verireel_prod_rollback._verify_post_rollback_health",
                side_effect=click.ClickException("health still failed after rollback"),
            ):
                result = execute_verireel_prod_rollback(
                    control_plane_root=root,
                    record_store=record_store,
                    request=VeriReelProdRollbackRequest(
                        promotion_record_id="promotion-verireel-testing-to-prod-run-12345-attempt-1",
                        backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    ),
                )

            self.assertEqual(result.rollback_status, "pass")
            self.assertEqual(result.rollback_health_status, "fail")
            self.assertEqual(result.error_message, "health still failed after rollback")
            updated_record = record_store.read_promotion_record(result.promotion_record_id)
            self.assertEqual(updated_record.rollback.status, "pass")
            self.assertEqual(updated_record.rollback_health.status, "fail")


if __name__ == "__main__":
    unittest.main()
