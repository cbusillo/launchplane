import unittest
from concurrent.futures import ThreadPoolExecutor
from json import JSONDecodeError
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock

from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
    build_odoo_stable_target_replacement_operation_id,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore


def _operation_payload(operation_id: str = "operation-cm-testing") -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "product": "odoo-tenant-cm",
        "context": "cm",
        "instance": "testing",
        "idempotency_key": "replacement-cm-testing",
        "idempotency_scope": "github-actions|cbusillo/launchplane|apply.yml|subject-a",
        "request_fingerprint": "fingerprint-123",
        "request": {
            "schema_version": 1,
            "product": "odoo-tenant-cm",
            "instance": "testing",
            "strategy": "recreate-in-place",
            "confirmation": "recreate cm testing",
            "data_source_mode": "empty",
            "allow_empty_data": True,
        },
        "status": "pending",
        "phase": "created",
        "created_at": "2026-05-17T00:00:00Z",
        "updated_at": "2026-05-17T00:00:00Z",
    }


class OdooStableTargetReplacementOperationRecordTests(unittest.TestCase):
    def test_operation_record_round_trips(self) -> None:
        record = OdooStableTargetReplacementOperationRecord.model_validate(_operation_payload())

        self.assertEqual(record.product, "odoo-tenant-cm")
        self.assertEqual(record.context, "cm")
        self.assertIsInstance(record.request, OdooStableTargetReplacementApplyRequest)
        self.assertEqual(record.request.confirmation, "recreate cm testing")

    def test_operation_id_is_stable_for_same_inputs(self) -> None:
        first = build_odoo_stable_target_replacement_operation_id(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            created_at="2026-05-17T00:00:00Z",
        )
        second = build_odoo_stable_target_replacement_operation_id(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            created_at="2026-05-17T00:00:00Z",
        )

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("odoo-target-replacement-cm-testing-"))

    def test_filesystem_store_filters_operations(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            active = OdooStableTargetReplacementOperationRecord.model_validate(_operation_payload())
            finished = OdooStableTargetReplacementOperationRecord.model_validate(
                {
                    **_operation_payload("operation-cm-testing-done"),
                    "idempotency_key": "replacement-cm-testing-done",
                    "status": "pass",
                    "phase": "completed",
                    "finished_at": "2026-05-17T00:05:00Z",
                    "updated_at": "2026-05-17T00:05:00Z",
                }
            )

            store.write_odoo_stable_target_replacement_operation_record(active)
            store.write_odoo_stable_target_replacement_operation_record(finished)

            loaded = store.read_odoo_stable_target_replacement_operation_record(active.operation_id)
            active_records = store.list_odoo_stable_target_replacement_operation_records(
                product="odoo-tenant-cm",
                context_name="cm",
                instance_name="testing",
                statuses=("pending", "running"),
            )

            self.assertEqual(loaded.operation_id, active.operation_id)
            self.assertEqual(
                tuple(record.operation_id for record in active_records),
                (active.operation_id,),
            )

    def test_filesystem_store_filters_idempotency_by_scope(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            first = OdooStableTargetReplacementOperationRecord.model_validate(
                {
                    **_operation_payload("operation-scope-a"),
                    "idempotency_scope": "caller-a",
                    "status": "pass",
                    "phase": "completed",
                    "finished_at": "2026-05-17T00:05:00Z",
                    "updated_at": "2026-05-17T00:05:00Z",
                }
            )
            second = OdooStableTargetReplacementOperationRecord.model_validate(
                {
                    **_operation_payload("operation-scope-b"),
                    "idempotency_scope": "caller-b",
                    "status": "pass",
                    "phase": "completed",
                    "finished_at": "2026-05-17T00:06:00Z",
                    "updated_at": "2026-05-17T00:06:00Z",
                }
            )

            store.write_odoo_stable_target_replacement_operation_record(first)
            store.write_odoo_stable_target_replacement_operation_record(second)

            scoped_records = store.list_odoo_stable_target_replacement_operation_records(
                idempotency_key="replacement-cm-testing",
                idempotency_scope="caller-a",
            )

            self.assertEqual(
                tuple(record.operation_id for record in scoped_records),
                ("operation-scope-a",),
            )

    def test_filesystem_store_reserves_active_lane_atomically(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            records = [
                OdooStableTargetReplacementOperationRecord.model_validate(
                    {
                        **_operation_payload(f"operation-cm-testing-{index}"),
                        "idempotency_key": f"replacement-cm-testing-{index}",
                        "idempotency_scope": f"caller-{index}",
                    }
                )
                for index in range(2)
            ]

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(
                    executor.map(
                        store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane,
                        records,
                    )
                )

            created_results = tuple(created for _, created in results)
            returned_ids = {record.operation_id for record, _ in results}
            active_records = store.list_odoo_stable_target_replacement_operation_records(
                product="odoo-tenant-cm",
                context_name="cm",
                instance_name="testing",
                statuses=("pending", "running"),
            )

            self.assertEqual(created_results.count(True), 1)
            self.assertEqual(created_results.count(False), 1)
            self.assertEqual(len(returned_ids), 1)
            self.assertEqual(len(active_records), 1)

    def test_filesystem_store_waits_for_empty_lane_reservation_owner(self) -> None:
        class DelayedWriteFilesystemRecordStore(FilesystemRecordStore):
            def __init__(self, state_dir: Path) -> None:
                super().__init__(state_dir)
                self.first_write_started = Event()
                self.release_first_write = Event()
                self._write_lock = Lock()
                self._write_count = 0

            def write_odoo_stable_target_replacement_operation_record(
                self, record: OdooStableTargetReplacementOperationRecord
            ) -> Path:
                with self._write_lock:
                    is_first_write = self._write_count == 0
                    self._write_count += 1
                if is_first_write:
                    self.first_write_started.set()
                    self.release_first_write.wait(timeout=2.0)
                return super().write_odoo_stable_target_replacement_operation_record(record)

        with TemporaryDirectory() as temporary_directory_name:
            store = DelayedWriteFilesystemRecordStore(state_dir=Path(temporary_directory_name))
            first = OdooStableTargetReplacementOperationRecord.model_validate(
                _operation_payload("operation-cm-testing-first")
            )
            second = OdooStableTargetReplacementOperationRecord.model_validate(
                {
                    **_operation_payload("operation-cm-testing-second"),
                    "idempotency_key": "replacement-cm-testing-second",
                    "idempotency_scope": "caller-second",
                }
            )

            with ThreadPoolExecutor(max_workers=2) as executor:
                first_future = executor.submit(
                    store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane,
                    first,
                )
                self.assertTrue(store.first_write_started.wait(timeout=2.0))
                second_future = executor.submit(
                    store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane,
                    second,
                )
                store.release_first_write.set()

                first_record, first_created = first_future.result(timeout=2.0)
                second_record, second_created = second_future.result(timeout=2.0)

            self.assertEqual(first_record.operation_id, first.operation_id)
            self.assertTrue(first_created)
            self.assertEqual(second_record.operation_id, first.operation_id)
            self.assertFalse(second_created)

    def test_filesystem_store_waits_for_reserved_operation_json_to_settle(self) -> None:
        class SettlingReadFilesystemRecordStore(FilesystemRecordStore):
            def __init__(self, state_dir: Path) -> None:
                super().__init__(state_dir)
                self._operation_reads: dict[str, int] = {}

            def read_odoo_stable_target_replacement_operation_record(
                self, operation_id: str
            ) -> OdooStableTargetReplacementOperationRecord:
                read_count = self._operation_reads.get(operation_id, 0)
                self._operation_reads[operation_id] = read_count + 1
                if operation_id == "operation-cm-testing-first" and read_count == 0:
                    raise JSONDecodeError("Expecting value", "", 0)
                return super().read_odoo_stable_target_replacement_operation_record(operation_id)

        with TemporaryDirectory() as temporary_directory_name:
            store = SettlingReadFilesystemRecordStore(state_dir=Path(temporary_directory_name))
            first = OdooStableTargetReplacementOperationRecord.model_validate(
                _operation_payload("operation-cm-testing-first")
            )
            second = OdooStableTargetReplacementOperationRecord.model_validate(
                {
                    **_operation_payload("operation-cm-testing-second"),
                    "idempotency_key": "replacement-cm-testing-second",
                    "idempotency_scope": "caller-second",
                }
            )

            first_record, first_created = (
                store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
                    first
                )
            )
            second_record, second_created = (
                store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
                    second
                )
            )

            self.assertEqual(first_record.operation_id, first.operation_id)
            self.assertTrue(first_created)
            self.assertEqual(second_record.operation_id, first.operation_id)
            self.assertFalse(second_created)

    def test_postgres_store_round_trips_operation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite:///{database_path}")
            store.ensure_schema()
            record = OdooStableTargetReplacementOperationRecord.model_validate(_operation_payload())

            store.write_odoo_stable_target_replacement_operation_record(record)

            loaded = store.read_odoo_stable_target_replacement_operation_record(record.operation_id)
            active_records = store.list_odoo_stable_target_replacement_operation_records(
                idempotency_key="replacement-cm-testing",
                idempotency_scope="github-actions|cbusillo/launchplane|apply.yml|subject-a",
                statuses=("pending",),
            )

            self.assertEqual(loaded.operation_id, record.operation_id)
            self.assertEqual(
                tuple(item.operation_id for item in active_records),
                (record.operation_id,),
            )

    def test_postgres_store_reserves_active_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite:///{database_path}")
            store.ensure_schema()
            first = OdooStableTargetReplacementOperationRecord.model_validate(
                _operation_payload("operation-cm-testing-a")
            )
            second = OdooStableTargetReplacementOperationRecord.model_validate(
                {
                    **_operation_payload("operation-cm-testing-b"),
                    "idempotency_key": "replacement-cm-testing-b",
                    "idempotency_scope": "caller-b",
                }
            )

            created_first = (
                store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
                    first
                )
            )
            created_second = (
                store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
                    second
                )
            )

            self.assertTrue(created_first[1])
            self.assertFalse(created_second[1])
            self.assertEqual(created_first[0].operation_id, created_second[0].operation_id)


if __name__ == "__main__":
    unittest.main()
