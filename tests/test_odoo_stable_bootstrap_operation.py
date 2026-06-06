import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from tempfile import TemporaryDirectory

from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
    build_odoo_stable_bootstrap_operation_id,
)
from control_plane.contracts.odoo_stable_bootstrap import OdooStableBootstrapRequest
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore


def _operation_payload(operation_id: str = "operation-cm-testing") -> dict[str, object]:
    return {
        "operation_id": operation_id,
        "product": "odoo-tenant-cm",
        "context": "cm",
        "instance": "testing",
        "idempotency_key": "bootstrap-cm-testing",
        "request_fingerprint": "fingerprint-123",
        "request": {
            "schema_version": 1,
            "product": "odoo-tenant-cm",
            "context": "cm",
            "instance": "testing",
            "confirmation": "bootstrap cm testing",
        },
        "status": "pending",
        "phase": "created",
        "created_at": "2026-05-17T00:00:00Z",
        "updated_at": "2026-05-17T00:00:00Z",
    }


class OdooStableBootstrapOperationRecordTests(unittest.TestCase):
    def test_operation_record_round_trips(self) -> None:
        record = OdooStableBootstrapOperationRecord.model_validate(_operation_payload())

        self.assertEqual(record.product, "odoo-tenant-cm")
        self.assertIsInstance(record.request, OdooStableBootstrapRequest)
        self.assertEqual(record.request.confirmation, "bootstrap cm testing")

    def test_operation_id_is_stable_for_same_inputs(self) -> None:
        first = build_odoo_stable_bootstrap_operation_id(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            created_at="2026-05-17T00:00:00Z",
            idempotency_key="bootstrap-cm-testing",
        )
        second = build_odoo_stable_bootstrap_operation_id(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            created_at="2026-05-17T00:00:00Z",
            idempotency_key="bootstrap-cm-testing",
        )

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("odoo-stable-bootstrap-cm-testing-"))

    def test_operation_id_distinguishes_same_second_idempotency_keys(self) -> None:
        first = build_odoo_stable_bootstrap_operation_id(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            created_at="2026-05-17T00:00:00Z",
            idempotency_key="bootstrap-cm-testing-a",
        )
        second = build_odoo_stable_bootstrap_operation_id(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            created_at="2026-05-17T00:00:00Z",
            idempotency_key="bootstrap-cm-testing-b",
        )

        self.assertNotEqual(first, second)

    def test_filesystem_store_filters_operations(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            active = OdooStableBootstrapOperationRecord.model_validate(_operation_payload())
            finished = OdooStableBootstrapOperationRecord.model_validate(
                {
                    **_operation_payload("operation-cm-testing-done"),
                    "idempotency_key": "bootstrap-cm-testing-done",
                    "status": "pass",
                    "phase": "completed",
                    "finished_at": "2026-05-17T00:05:00Z",
                    "updated_at": "2026-05-17T00:05:00Z",
                }
            )

            store.write_odoo_stable_bootstrap_operation_record(active)
            store.write_odoo_stable_bootstrap_operation_record(finished)

            loaded = store.read_odoo_stable_bootstrap_operation_record(active.operation_id)
            active_records = store.list_odoo_stable_bootstrap_operation_records(
                product="odoo-tenant-cm",
                context_name="cm",
                instance_name="testing",
                statuses=("pending", "running"),
            )

            self.assertEqual(loaded.operation_id, active.operation_id)
            self.assertEqual(
                tuple(record.operation_id for record in active_records), (active.operation_id,)
            )

    def test_filesystem_store_reserves_active_lane_atomically(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            store = FilesystemRecordStore(state_dir=Path(temporary_directory_name))
            records = [
                OdooStableBootstrapOperationRecord.model_validate(
                    {
                        **_operation_payload(f"operation-cm-testing-{index}"),
                        "idempotency_key": f"bootstrap-cm-testing-{index}",
                    }
                )
                for index in range(2)
            ]

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = tuple(
                    executor.map(
                        store.create_odoo_stable_bootstrap_operation_record_if_no_active_lane,
                        records,
                    )
                )

            created_results = tuple(created for _, created in results)
            returned_ids = {record.operation_id for record, _ in results}
            active_records = store.list_odoo_stable_bootstrap_operation_records(
                product="odoo-tenant-cm",
                context_name="cm",
                instance_name="testing",
                statuses=("pending", "running"),
            )

            self.assertEqual(created_results.count(True), 1)
            self.assertEqual(created_results.count(False), 1)
            self.assertEqual(len(returned_ids), 1)
            self.assertEqual(len(active_records), 1)

    def test_postgres_store_round_trips_operation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite:///{database_path}")
            store.ensure_schema()
            record = OdooStableBootstrapOperationRecord.model_validate(_operation_payload())

            store.write_odoo_stable_bootstrap_operation_record(record)

            loaded = store.read_odoo_stable_bootstrap_operation_record(record.operation_id)
            active_records = store.list_odoo_stable_bootstrap_operation_records(
                idempotency_key="bootstrap-cm-testing",
                statuses=("pending",),
            )

            self.assertEqual(loaded.operation_id, record.operation_id)
            self.assertEqual(
                tuple(item.operation_id for item in active_records), (record.operation_id,)
            )

    def test_postgres_store_reserves_active_lane(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite:///{database_path}")
            store.ensure_schema()
            first = OdooStableBootstrapOperationRecord.model_validate(
                _operation_payload("operation-cm-testing-a")
            )
            second = OdooStableBootstrapOperationRecord.model_validate(
                {
                    **_operation_payload("operation-cm-testing-b"),
                    "idempotency_key": "bootstrap-cm-testing-b",
                }
            )

            created_first = store.create_odoo_stable_bootstrap_operation_record_if_no_active_lane(
                first
            )
            created_second = store.create_odoo_stable_bootstrap_operation_record_if_no_active_lane(
                second
            )

            self.assertTrue(created_first[1])
            self.assertFalse(created_second[1])
            self.assertEqual(created_first[0].operation_id, created_second[0].operation_id)


if __name__ == "__main__":
    unittest.main()
