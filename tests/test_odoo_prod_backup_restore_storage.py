import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.storage.postgres import PostgresRecordStore
from tests.test_odoo_stable_operation_worker import _restore_operation


class OdooProdBackupRestoreStorageTests(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
