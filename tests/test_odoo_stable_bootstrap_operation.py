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

    def test_postgres_store_claims_and_heartbeats_operation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite:///{database_path}")
            store.ensure_schema()
            record = OdooStableBootstrapOperationRecord.model_validate(_operation_payload())
            store.write_odoo_stable_bootstrap_operation_record(record)

            claimed = store.claim_next_odoo_stable_bootstrap_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-05-17T00:10:00Z",
                claimed_at="2026-05-17T00:01:00Z",
            )
            second_claim = store.claim_next_odoo_stable_bootstrap_operation_record(
                lease_owner="worker-b",
                lease_expires_at="2026-05-17T00:11:00Z",
                claimed_at="2026-05-17T00:02:00Z",
            )
            wrong_owner_heartbeat = store.heartbeat_odoo_stable_bootstrap_operation_record(
                operation_id=record.operation_id,
                lease_owner="worker-b",
                heartbeat_at="2026-05-17T00:03:00Z",
                lease_expires_at="2026-05-17T00:13:00Z",
            )
            owner_heartbeat = store.heartbeat_odoo_stable_bootstrap_operation_record(
                operation_id=record.operation_id,
                lease_owner="worker-a",
                heartbeat_at="2026-05-17T00:04:00Z",
                lease_expires_at="2026-05-17T00:14:00Z",
            )

            self.assertIsNotNone(claimed)
            assert claimed is not None
            self.assertEqual(claimed.status, "running")
            self.assertEqual(claimed.phase, "running")
            self.assertEqual(claimed.lease_owner, "worker-a")
            self.assertEqual(claimed.heartbeat_at, "2026-05-17T00:01:00Z")
            self.assertEqual(claimed.attempt, 1)
            self.assertIsNone(second_claim)
            self.assertFalse(wrong_owner_heartbeat)
            self.assertTrue(owner_heartbeat)
            loaded = store.read_odoo_stable_bootstrap_operation_record(record.operation_id)
            self.assertEqual(loaded.heartbeat_at, "2026-05-17T00:04:00Z")
            self.assertEqual(loaded.lease_expires_at, "2026-05-17T00:14:00Z")

    def test_postgres_store_rejects_expired_lease_heartbeat_and_completion(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite:///{database_path}")
            store.ensure_schema()
            record = OdooStableBootstrapOperationRecord.model_validate(_operation_payload())
            store.write_odoo_stable_bootstrap_operation_record(record)

            claimed = store.claim_next_odoo_stable_bootstrap_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-05-17T00:02:00Z",
                claimed_at="2026-05-17T00:01:00Z",
            )
            assert claimed is not None
            expired_heartbeat = store.heartbeat_odoo_stable_bootstrap_operation_record(
                operation_id=record.operation_id,
                lease_owner="worker-a",
                heartbeat_at="2026-05-17T00:03:00Z",
                lease_expires_at="2026-05-17T00:13:00Z",
            )
            terminal_record = claimed.model_copy(
                update={
                    "status": "pass",
                    "phase": "completed",
                    "updated_at": "2026-05-17T00:03:00Z",
                    "finished_at": "2026-05-17T00:03:00Z",
                }
            )
            expired_completion = store.complete_odoo_stable_bootstrap_operation_record(
                record=terminal_record,
                lease_owner="worker-a",
            )

            self.assertFalse(expired_heartbeat)
            self.assertFalse(expired_completion)
            loaded = store.read_odoo_stable_bootstrap_operation_record(record.operation_id)
            self.assertEqual(loaded.status, "running")
            self.assertEqual(loaded.phase, "running")

    def test_postgres_store_recovers_safe_expired_operation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite:///{database_path}")
            store.ensure_schema()
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    {
                        **_operation_payload(),
                        "status": "running",
                        "phase": "created",
                        "started_at": "2026-05-17T00:01:00Z",
                        "lease_owner": "worker-a",
                        "lease_expires_at": "2026-05-17T00:02:00Z",
                        "heartbeat_at": "2026-05-17T00:01:00Z",
                        "attempt": 1,
                    }
                )
            )

            affected = store.recover_expired_odoo_stable_bootstrap_operation_records(
                now="2026-05-17T00:03:00Z",
                safe_phases=("created",),
                max_attempts=3,
            )

            self.assertEqual(affected, ("operation-cm-testing",))
            loaded = store.read_odoo_stable_bootstrap_operation_record("operation-cm-testing")
            self.assertEqual(loaded.status, "pending")
            self.assertEqual(loaded.phase, "created")
            self.assertEqual(loaded.lease_owner, "")
            self.assertEqual(loaded.started_at, "")
            self.assertEqual(loaded.attempt, 1)

    def test_postgres_store_fails_unsafe_expired_operation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite:///{database_path}")
            store.ensure_schema()
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    {
                        **_operation_payload(),
                        "status": "running",
                        "phase": "bootstrap",
                        "started_at": "2026-05-17T00:01:00Z",
                        "lease_owner": "worker-a",
                        "lease_expires_at": "2026-05-17T00:02:00Z",
                        "heartbeat_at": "2026-05-17T00:01:00Z",
                        "attempt": 1,
                    }
                )
            )

            affected = store.recover_expired_odoo_stable_bootstrap_operation_records(
                now="2026-05-17T00:03:00Z",
                safe_phases=("created",),
                max_attempts=3,
            )

            self.assertEqual(affected, ("operation-cm-testing",))
            loaded = store.read_odoo_stable_bootstrap_operation_record("operation-cm-testing")
            self.assertEqual(loaded.status, "fail")
            self.assertEqual(loaded.phase, "failed")
            self.assertIn("unsafe to retry", loaded.error_message)
            self.assertEqual(loaded.lease_owner, "")


if __name__ == "__main__":
    unittest.main()
