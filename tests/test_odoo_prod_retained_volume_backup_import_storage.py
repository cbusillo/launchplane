import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.test_odoo_prod_retained_volume_backup_import import _operation


class OdooProdRetainedVolumeBackupImportStorageTests(unittest.TestCase):
    def test_filesystem_store_round_trips_plan_and_apply_kinds(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name) / "state")
            plan_operation = _operation(
                operation_kind="plan",
                operation_id="retained-plan-operation-1",
            )
            store.write_odoo_prod_retained_volume_backup_import_operation_record(plan_operation)

            stored = store.read_odoo_prod_retained_volume_backup_import_operation_record(
                plan_operation.operation_id
            )
            plan_records = store.list_odoo_prod_retained_volume_backup_import_operation_records(
                operation_kind="plan"
            )
            apply_records = store.list_odoo_prod_retained_volume_backup_import_operation_records(
                operation_kind="apply"
            )

        self.assertEqual(stored, plan_operation)
        self.assertEqual(plan_records, (plan_operation,))
        self.assertEqual(apply_records, ())

    def test_postgres_store_single_flight_checkpoint_and_kind_filter(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            store.ensure_schema()
            operation = _operation(
                operation_kind="plan",
                operation_id="retained-plan-operation-1",
            )

            persisted, created = (
                store.create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane(
                    operation
                )
            )
            duplicate, duplicate_created = (
                store.create_odoo_prod_retained_volume_backup_import_operation_record_if_no_active_lane(
                    _operation(
                        operation_kind="apply",
                        operation_id="retained-apply-operation-2",
                    )
                )
            )
            claimed = store.claim_next_odoo_prod_retained_volume_backup_import_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-07-26T00:10:00Z",
                claimed_at="2026-07-26T00:01:00Z",
            )
            assert claimed is not None
            checkpointed = (
                store.checkpoint_odoo_prod_retained_volume_backup_import_operation_record(
                    operation_id=claimed.operation_id,
                    lease_owner="worker-a",
                    phase="inspection_started",
                    checkpointed_at="2026-07-26T00:02:00Z",
                    evidence={"provider_effect": "schedule_trigger"},
                )
            )
            plan_records = store.list_odoo_prod_retained_volume_backup_import_operation_records(
                operation_kind="plan"
            )

        self.assertTrue(created)
        self.assertEqual(persisted.operation_id, operation.operation_id)
        self.assertFalse(duplicate_created)
        self.assertEqual(duplicate.operation_id, operation.operation_id)
        self.assertIsNotNone(checkpointed)
        assert checkpointed is not None
        self.assertEqual(checkpointed.phase, "inspection_started")
        self.assertEqual(
            checkpointed.checkpoints[-1].evidence["provider_effect"],
            "schedule_trigger",
        )
        self.assertEqual(len(plan_records), 1)

    def test_postgres_store_fails_expired_import_after_provider_effect(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(
                database_url=f"sqlite+pysqlite:///{database_path.as_posix()}"
            )
            store.ensure_schema()
            operation = _operation(
                operation_kind="apply",
                operation_id="retained-apply-operation-1",
            )
            store.write_odoo_prod_retained_volume_backup_import_operation_record(operation)
            claimed = store.claim_next_odoo_prod_retained_volume_backup_import_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-07-26T00:02:00Z",
                claimed_at="2026-07-26T00:01:00Z",
            )
            assert claimed is not None
            checkpointed = (
                store.checkpoint_odoo_prod_retained_volume_backup_import_operation_record(
                    operation_id=claimed.operation_id,
                    lease_owner="worker-a",
                    phase="provider_import_started",
                    checkpointed_at="2026-07-26T00:01:30Z",
                    evidence={"provider_effect": "schedule_trigger"},
                )
            )
            assert checkpointed is not None

            recovered_ids = (
                store.recover_expired_odoo_prod_retained_volume_backup_import_operation_records(
                    now="2026-07-26T00:03:00Z",
                    safe_phases=("created", "running", "validated"),
                    max_attempts=3,
                )
            )
            stored = store.read_odoo_prod_retained_volume_backup_import_operation_record(
                operation.operation_id
            )

        self.assertEqual(recovered_ids, (operation.operation_id,))
        self.assertEqual(stored.status, "fail")
        self.assertIn("unsafe to retry automatically", stored.error_message)


if __name__ == "__main__":
    unittest.main()
