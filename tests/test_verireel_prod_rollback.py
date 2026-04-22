import unittest
from subprocess import CompletedProcess
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.promotion_record import (
    ArtifactIdentityReference,
    DeploymentEvidence,
    PromotionRecord,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.verireel_prod_rollback import (
    VeriReelProdRollbackRequest,
    VeriReelProdRollbackWorkerRequest,
    VeriReelProdRollbackWorkerResult,
    _run_delegated_worker,
    execute_verireel_prod_rollback,
)
from control_plane.workflows.verireel_prod_promotion import VeriReelRolloutVerificationResult


class VeriReelProdRollbackWorkflowTests(unittest.TestCase):
    def _sqlite_database_url(self, root: Path) -> str:
        return f"sqlite+pysqlite:///{root / 'launchplane.sqlite3'}"

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

    def test_run_delegated_worker_prefers_runtime_environment_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = self._sqlite_database_url(root)
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                for record in control_plane_runtime_environments.build_runtime_environment_records_from_definition(
                    control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                        schema_version=1,
                        shared_env={},
                        contexts={
                            "verireel": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                                shared_env={},
                                instances={
                                    "prod": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                        env={
                                            "LAUNCHPLANE_VERIREEL_PROD_ROLLBACK_WORKER_COMMAND": "uv run python -m control_plane.workflows.verireel_prod_rollback_worker",
                                            "VERIREEL_PROD_PROXMOX_HOST": "proxmox.runtime.example",
                                            "VERIREEL_PROD_PROXMOX_USER": "runtime-user",
                                            "VERIREEL_PROD_CT_ID": "211",
                                        }
                                    )
                                },
                            )
                        },
                    ),
                    updated_at="2026-04-22T00:00:00Z",
                    source_label="test",
                ):
                    store.write_runtime_environment_record(record)
            finally:
                store.close()

            captured: dict[str, object] = {}

            def _fake_run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
                captured["command"] = command
                captured["env"] = kwargs["env"]
                return CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=(
                        '{"schema_version":1,"status":"pass","snapshot_name":"ver-predeploy-20260421-180000",'
                        '"started_at":"2026-04-21T18:20:00Z","finished_at":"2026-04-21T18:21:00Z",'
                        '"detail":"Rollback completed."}\n'
                    ),
                    stderr="",
                )

            with patch(
                "control_plane.workflows.verireel_prod_rollback.subprocess.run",
                side_effect=_fake_run,
            ), patch.dict(
                "os.environ",
                {
                    "LAUNCHPLANE_DATABASE_URL": database_url,
                    "LAUNCHPLANE_VERIREEL_PROD_ROLLBACK_WORKER_COMMAND": "legacy worker",
                    "VERIREEL_PROD_PROXMOX_HOST": "legacy.example",
                    "VERIREEL_PROD_PROXMOX_USER": "legacy-user",
                    "VERIREEL_PROD_CT_ID": "999",
                },
                clear=True,
            ):
                result = _run_delegated_worker(
                    control_plane_root=root,
                    request=VeriReelProdRollbackWorkerRequest(
                        context="verireel",
                        instance="prod",
                        promotion_record_id="promotion-verireel-testing-to-prod-run-12345-attempt-1",
                        backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                        snapshot_name="ver-predeploy-20260421-180000",
                    ),
                )

        self.assertEqual(result.status, "pass")
        self.assertEqual(
            captured["command"],
            ["uv", "run", "python", "-m", "control_plane.workflows.verireel_prod_rollback_worker"],
        )
        worker_env = captured["env"]
        assert isinstance(worker_env, dict)
        self.assertEqual(worker_env["VERIREEL_PROD_PROXMOX_HOST"], "proxmox.runtime.example")
        self.assertEqual(worker_env["VERIREEL_PROD_PROXMOX_USER"], "runtime-user")
        self.assertEqual(worker_env["VERIREEL_PROD_CT_ID"], "211")

    def test_run_delegated_worker_rejects_process_environment_worker_config(self) -> None:
        with TemporaryDirectory() as temporary_directory_name, patch.dict(
            "os.environ",
            {
                "LAUNCHPLANE_VERIREEL_PROD_ROLLBACK_WORKER_COMMAND": "python worker.py",
                "VERIREEL_PROD_PROXMOX_HOST": "legacy.example",
                "VERIREEL_PROD_PROXMOX_USER": "legacy-user",
                "VERIREEL_PROD_CT_ID": "999",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(Exception, "Missing LAUNCHPLANE_VERIREEL_PROD_ROLLBACK_WORKER_COMMAND"):
                _run_delegated_worker(
                    control_plane_root=Path(temporary_directory_name),
                    request=VeriReelProdRollbackWorkerRequest(
                        context="verireel",
                        instance="prod",
                        promotion_record_id="promotion-verireel-testing-to-prod-run-12345-attempt-1",
                        backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                        snapshot_name="ver-predeploy-20260421-180000",
                    ),
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
