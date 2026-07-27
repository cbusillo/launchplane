import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.contracts.odoo_prod_backup_restore import OdooProdBackupRestoreResult
from control_plane.contracts.odoo_prod_backup_restore_operation import (
    OdooProdBackupRestoreCheckpoint,
    OdooProdBackupRestoreOperationRecord,
    odoo_prod_backup_restore_failure_is_replay_eligible,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.durable_operation_authorization import DurableOperationAuthorization
from tests.test_odoo_stable_operation_worker import _restore_operation


def _verification_failed_restore_operation() -> OdooProdBackupRestoreOperationRecord:
    operation = _restore_operation()
    result = OdooProdBackupRestoreResult(
        product=operation.product,
        context=operation.context,
        instance=operation.instance,
        backup_record_id=operation.plan.backup_record_id,
        verification_record_id=operation.plan.verification_record_id,
        plan_fingerprint=operation.plan.plan_fingerprint,
        deployment_record_id="deployment-cm-prod-restore",
        restore_status="fail",
        database_restore_status="pass",
        filestore_stage_status="pass",
        web_quiesce_status="pass",
        filestore_activation_status="pass",
        deployment_status="pass",
        post_deploy_status="pass",
        health_status="pass",
        canonical_status="fail",
        logo_status="pass",
        runtime_identity_status="skipped",
        old_db_volume=operation.plan.old_db_volume,
        new_db_volume=operation.plan.new_db_volume,
        data_volume=operation.plan.data_volume,
        log_volume=operation.plan.log_volume,
        filestore_quarantine_path=operation.plan.filestore_quarantine_path,
        database_dump_sha256=operation.plan.database_dump_sha256,
        filestore_archive_sha256=operation.plan.filestore_archive_sha256,
        phase_evidence={
            "database_restore": {"status": "pass"},
            "filestore_stage": {"status": "pass"},
            "web_quiesce": {"status": "pass"},
            "filestore_activate": {"status": "pass"},
            "target_env_update": {
                "old_db_volume": operation.plan.old_db_volume,
                "new_db_volume": operation.plan.new_db_volume,
                "data_volume": operation.plan.data_volume,
                "log_volume": operation.plan.log_volume,
            },
            "deployment": {"provider_deployment": "provider-deployment-1"},
            "post_deploy": {"status": "pass"},
        },
        error_message="Odoo canonical verification failed.",
    )
    return operation.model_copy(
        update={
            "status": "fail",
            "phase": "failed",
            "checkpoints": tuple(
                OdooProdBackupRestoreCheckpoint.model_validate(
                    {
                        "phase": phase,
                        "recorded_at": f"2026-07-25T00:{index:02d}:00Z",
                        "evidence": {"status": "pass"},
                    }
                )
                for index, phase in enumerate(
                    (
                        "validated",
                        "database_restored",
                        "filestore_staged",
                        "web_quiesced",
                        "filestore_activated",
                        "target_env_updated",
                        "deployed",
                        "post_deploy_completed",
                        "verification_started",
                    ),
                    start=1,
                )
            ),
            "deployment_record_id": result.deployment_record_id,
            "updated_at": "2026-07-25T00:30:00Z",
            "started_at": "2026-07-25T00:01:00Z",
            "finished_at": "2026-07-25T00:30:00Z",
            "attempt": 1,
            "result": result,
            "error_message": result.error_message,
        }
    )


def _restore_authorization() -> DurableOperationAuthorization:
    return _verification_failed_restore_operation().authorization


class OdooProdBackupRestoreStorageTests(unittest.TestCase):
    def test_replay_eligibility_accepts_runtime_identity_failure(self) -> None:
        operation = _verification_failed_restore_operation()
        assert operation.result is not None
        runtime_failure = operation.result.model_copy(
            update={
                "canonical_status": "pass",
                "runtime_identity_status": "mismatch",
            }
        )

        self.assertTrue(
            odoo_prod_backup_restore_failure_is_replay_eligible(
                operation.model_copy(update={"result": runtime_failure})
            )
        )

    def test_replay_eligibility_rejects_deployment_record_mismatch(self) -> None:
        operation = _verification_failed_restore_operation()
        assert operation.result is not None
        mismatched = operation.result.model_copy(update={"deployment_record_id": "other"})

        self.assertFalse(
            odoo_prod_backup_restore_failure_is_replay_eligible(
                operation.model_copy(update={"result": mismatched})
            )
        )

    def test_replay_eligibility_rejects_incomplete_checkpoint_evidence(self) -> None:
        operation = _verification_failed_restore_operation()

        self.assertFalse(
            odoo_prod_backup_restore_failure_is_replay_eligible(
                operation.model_copy(update={"checkpoints": operation.checkpoints[-2:]})
            )
        )

    def test_postgres_store_single_flight_and_durable_checkpoint(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            store.ensure_schema()
            operation = _restore_operation()

            persisted, created = (
                store.create_odoo_prod_backup_restore_operation_record_if_no_active_lane(operation)
            )
            duplicate, duplicate_created = (
                store.create_odoo_prod_backup_restore_operation_record_if_no_active_lane(
                    _restore_operation("operation-cm-prod-restore-second")
                )
            )
            claimed = store.claim_next_odoo_prod_backup_restore_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-07-25T00:10:00Z",
                claimed_at="2026-07-25T00:01:00Z",
            )
            assert claimed is not None
            checkpointed = store.checkpoint_odoo_prod_backup_restore_operation_record(
                operation_id=claimed.operation_id,
                lease_owner="worker-a",
                phase="database_restore_started",
                checkpointed_at="2026-07-25T00:02:00Z",
                evidence={"provider_effect": "database_restore_schedule_trigger"},
            )

            self.assertTrue(created)
            self.assertEqual(persisted.operation_id, operation.operation_id)
            self.assertFalse(duplicate_created)
            self.assertEqual(duplicate.operation_id, operation.operation_id)
            self.assertIsNotNone(checkpointed)
            assert checkpointed is not None
            self.assertEqual(checkpointed.phase, "database_restore_started")
            self.assertEqual(
                checkpointed.checkpoints[-1].evidence["provider_effect"],
                "database_restore_schedule_trigger",
            )

    def test_postgres_store_fails_expired_restore_after_provider_effect(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            store.ensure_schema()
            operation = _restore_operation()
            store.write_odoo_prod_backup_restore_operation_record(operation)
            claimed = store.claim_next_odoo_prod_backup_restore_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-07-25T00:02:00Z",
                claimed_at="2026-07-25T00:01:00Z",
            )
            assert claimed is not None
            checkpointed = store.checkpoint_odoo_prod_backup_restore_operation_record(
                operation_id=claimed.operation_id,
                lease_owner="worker-a",
                phase="database_restore_started",
                checkpointed_at="2026-07-25T00:01:30Z",
                evidence={"provider_effect": "database_restore_schedule_trigger"},
            )
            assert checkpointed is not None

            recovered_ids = store.recover_expired_odoo_prod_backup_restore_operation_records(
                now="2026-07-25T00:03:00Z",
                safe_phases=("created", "running", "validated"),
                max_attempts=3,
            )
            stored = store.read_odoo_prod_backup_restore_operation_record(operation.operation_id)

            self.assertEqual(recovered_ids, (operation.operation_id,))
            self.assertEqual(stored.status, "reconciliation_required")
            self.assertEqual(stored.phase, "database_restore_started")
            self.assertEqual(stored.error_code, "operation_reconciliation_required")
            self.assertIn("operator reconciliation", stored.error_message)

    def test_filesystem_store_requeues_terminal_verification_failure_atomically(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            operation = _verification_failed_restore_operation()
            store.write_odoo_prod_backup_restore_operation_record(operation)

            requeued = store.requeue_terminal_failed_odoo_prod_backup_restore_operation_record(
                operation_id=operation.operation_id,
                queued_at="2026-07-25T00:31:00Z",
                authorization=_restore_authorization(),
            )
            second_requeue = (
                store.requeue_terminal_failed_odoo_prod_backup_restore_operation_record(
                    operation_id=operation.operation_id,
                    queued_at="2026-07-25T00:32:00Z",
                    authorization=_restore_authorization(),
                )
            )
            claimed = store.claim_next_odoo_prod_backup_restore_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-07-25T00:40:00Z",
                claimed_at="2026-07-25T00:33:00Z",
            )
            assert claimed is not None
            replay_checkpoint = store.checkpoint_odoo_prod_backup_restore_operation_record(
                operation_id=claimed.operation_id,
                lease_owner="worker-a",
                phase="post_deploy_started",
                checkpointed_at="2026-07-25T00:34:00Z",
                evidence={"provider_effect": "post_deploy_schedule_trigger"},
            )
            recovered_ids = store.recover_expired_odoo_prod_backup_restore_operation_records(
                now="2026-07-25T00:41:00Z",
                safe_phases=("created", "running", "validated"),
                max_attempts=3,
            )
            reconciled = store.read_odoo_prod_backup_restore_operation_record(
                operation.operation_id
            )

            self.assertIsNotNone(requeued)
            assert requeued is not None
            self.assertEqual(requeued.status, "pending")
            self.assertEqual(requeued.phase, "verification_started")
            self.assertEqual(requeued.attempt, 1)
            self.assertIsNone(second_requeue)
            self.assertEqual(claimed.status, "running")
            self.assertEqual(claimed.phase, "verification_started")
            self.assertEqual(claimed.attempt, 2)
            self.assertIsNotNone(replay_checkpoint)
            assert replay_checkpoint is not None
            self.assertEqual(replay_checkpoint.phase, "post_deploy_started")
            self.assertEqual(
                len(replay_checkpoint.checkpoints),
                len(operation.checkpoints) + 1,
            )
            self.assertEqual(recovered_ids, (operation.operation_id,))
            self.assertEqual(reconciled.status, "reconciliation_required")
            self.assertEqual(reconciled.phase, "post_deploy_started")
            self.assertIsNone(reconciled.result)
            self.assertEqual(reconciled.deployment_record_id, operation.deployment_record_id)
            self.assertEqual(reconciled.checkpoints, replay_checkpoint.checkpoints)

    def test_postgres_store_requeues_terminal_verification_failure(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            store.ensure_schema()
            operation = _verification_failed_restore_operation()
            store.write_odoo_prod_backup_restore_operation_record(operation)

            requeued = store.requeue_terminal_failed_odoo_prod_backup_restore_operation_record(
                operation_id=operation.operation_id,
                queued_at="2026-07-25T00:31:00Z",
                authorization=_restore_authorization(),
            )
            claimed = store.claim_next_odoo_prod_backup_restore_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-07-25T00:40:00Z",
                claimed_at="2026-07-25T00:33:00Z",
            )
            assert claimed is not None
            replay_checkpoint = store.checkpoint_odoo_prod_backup_restore_operation_record(
                operation_id=claimed.operation_id,
                lease_owner="worker-a",
                phase="post_deploy_started",
                checkpointed_at="2026-07-25T00:34:00Z",
                evidence={"provider_effect": "post_deploy_schedule_trigger"},
            )
            recovered_ids = store.recover_expired_odoo_prod_backup_restore_operation_records(
                now="2026-07-25T00:41:00Z",
                safe_phases=("created", "running", "validated"),
                max_attempts=3,
            )
            reconciled = store.read_odoo_prod_backup_restore_operation_record(
                operation.operation_id
            )

            self.assertIsNotNone(requeued)
            assert requeued is not None
            self.assertEqual(requeued.status, "pending")
            self.assertEqual(requeued.phase, "verification_started")
            self.assertEqual(claimed.attempt, 2)
            self.assertEqual(claimed.phase, "verification_started")
            self.assertIsNotNone(replay_checkpoint)
            self.assertEqual(recovered_ids, (operation.operation_id,))
            self.assertEqual(reconciled.status, "reconciliation_required")
            self.assertEqual(reconciled.phase, "post_deploy_started")
            self.assertIsNone(reconciled.result)
            self.assertEqual(reconciled.deployment_record_id, operation.deployment_record_id)

    def test_requeue_rejects_missing_destructive_phase_evidence(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            operation = _verification_failed_restore_operation()
            assert operation.result is not None
            ineligible_result = operation.result.model_copy(
                update={"filestore_stage_status": "skipped"}
            )
            store.write_odoo_prod_backup_restore_operation_record(
                operation.model_copy(update={"result": ineligible_result})
            )

            with self.assertRaisesRegex(ValueError, "not eligible"):
                store.requeue_terminal_failed_odoo_prod_backup_restore_operation_record(
                    operation_id=operation.operation_id,
                    queued_at="2026-07-25T00:31:00Z",
                    authorization=_restore_authorization(),
                )


if __name__ == "__main__":
    unittest.main()
