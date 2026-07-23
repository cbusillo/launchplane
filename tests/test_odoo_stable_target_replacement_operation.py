import hashlib
import unittest
from concurrent.futures import ThreadPoolExecutor
from json import JSONDecodeError
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Lock
from unittest.mock import patch

from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
    build_odoo_stable_target_replacement_operation_id,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyRequest,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.durable_operations import (
    durable_operation_authorization_payload,
    durable_operation_cancellation_payload,
)


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


def _lane_reservation_path(
    store: FilesystemRecordStore,
    record: OdooStableTargetReplacementOperationRecord,
) -> Path:
    lane_key = "|".join((record.product, record.context, record.instance))
    digest = hashlib.sha256(lane_key.encode()).hexdigest()[:16]
    reservation_id = (
        f"{record.product}-{record.context}-{record.instance}".replace("/", "-") + f"-{digest}"
    )
    return store._record_path("odoo_stable_target_replacement_lane_reservations", reservation_id)


class OdooStableTargetReplacementOperationRecordTests(unittest.TestCase):
    def test_operation_record_round_trips(self) -> None:
        record = OdooStableTargetReplacementOperationRecord.model_validate(_operation_payload())

        self.assertEqual(record.product, "odoo-tenant-cm")
        self.assertEqual(record.context, "cm")
        self.assertIsInstance(record.request, OdooStableTargetReplacementApplyRequest)
        self.assertEqual(record.request.confirmation, "recreate cm testing")

    def test_schema_v2_operation_requires_matching_authorization_provenance(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires authorization provenance"):
            OdooStableTargetReplacementOperationRecord.model_validate(
                {**_operation_payload(), "schema_version": 2}
            )

        record = OdooStableTargetReplacementOperationRecord.model_validate(
            {
                **_operation_payload(),
                "schema_version": 2,
                "authorization": durable_operation_authorization_payload(
                    action="odoo_target_replacement_apply.execute",
                    managed_rule_id="cm-testing-replacement",
                ),
            }
        )

        assert record.authorization is not None
        self.assertEqual(record.authorization.policy_revision, 41)
        self.assertEqual(record.authorization.instances, ("testing",))

    def test_cancelled_operation_is_terminal_and_requires_cancellation_evidence(self) -> None:
        authorization = durable_operation_authorization_payload(
            action="odoo_target_replacement_apply.execute",
            managed_rule_id="cm-testing-replacement",
        )
        cancellation = durable_operation_cancellation_payload()
        cancellation["caller"] = authorization["caller"]
        record = OdooStableTargetReplacementOperationRecord.model_validate(
            {
                **_operation_payload(),
                "schema_version": 2,
                "authorization": authorization,
                "status": "cancelled",
                "phase": "cancelled",
                "finished_at": "2026-07-23T03:32:00Z",
                "cancellation": cancellation,
            }
        )
        self.assertEqual(record.status, "cancelled")
        assert record.cancellation is not None
        self.assertEqual(record.cancellation.cancelled_at, "2026-07-23T03:32:00Z")

    def test_pending_cancellation_prevents_worker_claim(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            stores = (
                FilesystemRecordStore(state_dir=root / "filesystem"),
                PostgresRecordStore(database_url=f"sqlite:///{root / 'launchplane.sqlite3'}"),
            )
            stores[1].ensure_schema()
            try:
                for store in stores:
                    with self.subTest(store=type(store).__name__):
                        authorization = durable_operation_authorization_payload(
                            action="odoo_target_replacement_apply.execute",
                            managed_rule_id="cm-testing-replacement",
                        )
                        pending_record = OdooStableTargetReplacementOperationRecord.model_validate(
                            {
                                **_operation_payload(),
                                "schema_version": 2,
                                "authorization": authorization,
                            }
                        )
                        store.write_odoo_stable_target_replacement_operation_record(pending_record)
                        cancellation = durable_operation_cancellation_payload()
                        cancellation["caller"] = authorization["caller"]
                        cancelled_record = (
                            OdooStableTargetReplacementOperationRecord.model_validate(
                                {
                                    **pending_record.model_dump(mode="json"),
                                    "status": "cancelled",
                                    "phase": "cancelled",
                                    "updated_at": "2026-07-23T03:32:00Z",
                                    "finished_at": "2026-07-23T03:32:00Z",
                                    "cancellation": cancellation,
                                }
                            )
                        )

                        self.assertTrue(
                            store.cancel_pending_odoo_stable_target_replacement_operation_record(
                                cancelled_record
                            )
                        )
                        self.assertFalse(
                            store.cancel_pending_odoo_stable_target_replacement_operation_record(
                                cancelled_record
                            )
                        )
                        self.assertIsNone(
                            store.claim_next_odoo_stable_target_replacement_operation_record(
                                lease_owner="worker-a",
                                lease_expires_at="2026-07-23T03:40:00Z",
                                claimed_at="2026-07-23T03:35:00Z",
                            )
                        )
            finally:
                stores[1].close()

    def test_operation_id_is_stable_for_same_inputs(self) -> None:
        first = build_odoo_stable_target_replacement_operation_id(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            created_at="2026-05-17T00:00:00Z",
            idempotency_key="replacement-cm-testing",
            idempotency_scope="github-actions|cbusillo/launchplane|apply.yml|subject-a",
        )
        second = build_odoo_stable_target_replacement_operation_id(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            created_at="2026-05-17T00:00:00Z",
            idempotency_key="replacement-cm-testing",
            idempotency_scope="github-actions|cbusillo/launchplane|apply.yml|subject-a",
        )

        self.assertEqual(first, second)
        self.assertTrue(first.startswith("odoo-target-replacement-cm-testing-"))

    def test_operation_id_distinguishes_same_second_idempotency_inputs(self) -> None:
        first = build_odoo_stable_target_replacement_operation_id(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            created_at="2026-05-17T00:00:00Z",
            idempotency_key="replacement-cm-testing-a",
            idempotency_scope="github-actions|cbusillo/launchplane|apply.yml|subject-a",
        )
        second = build_odoo_stable_target_replacement_operation_id(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            created_at="2026-05-17T00:00:00Z",
            idempotency_key="replacement-cm-testing-b",
            idempotency_scope="github-actions|cbusillo/launchplane|apply.yml|subject-a",
        )

        self.assertNotEqual(first, second)

    def test_operation_id_distinguishes_same_key_different_scope(self) -> None:
        first = build_odoo_stable_target_replacement_operation_id(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            created_at="2026-05-17T00:00:00Z",
            idempotency_key="replacement-cm-testing",
            idempotency_scope="github-actions|cbusillo/launchplane|apply.yml|subject-a",
        )
        second = build_odoo_stable_target_replacement_operation_id(
            product="odoo-tenant-cm",
            context="cm",
            instance="testing",
            created_at="2026-05-17T00:00:00Z",
            idempotency_key="replacement-cm-testing",
            idempotency_scope="github-actions|cbusillo/launchplane|apply.yml|subject-b",
        )

        self.assertNotEqual(first, second)

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

    def test_filesystem_store_waits_past_slow_reserved_operation_write(self) -> None:
        class SlowWriteFilesystemRecordStore(FilesystemRecordStore):
            odoo_target_replacement_reservation_settle_timeout_seconds = 2.0

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

            def _wait_for_odoo_stable_target_replacement_reserved_operation(
                self, operation_id: str, deadline: float
            ) -> OdooStableTargetReplacementOperationRecord | None:
                if operation_id == "operation-cm-testing-first":
                    self.first_write_started.wait(timeout=2.0)
                    self.release_first_write.set()
                return super()._wait_for_odoo_stable_target_replacement_reserved_operation(
                    operation_id, deadline
                )

        with TemporaryDirectory() as temporary_directory_name:
            store = SlowWriteFilesystemRecordStore(state_dir=Path(temporary_directory_name))
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

                first_record, first_created = first_future.result(timeout=2.0)
                second_record, second_created = second_future.result(timeout=2.0)

            active_records = store.list_odoo_stable_target_replacement_operation_records(
                product="odoo-tenant-cm",
                context_name="cm",
                instance_name="testing",
                statuses=("pending", "running"),
            )

            self.assertEqual(first_record.operation_id, first.operation_id)
            self.assertTrue(first_created)
            self.assertEqual(second_record.operation_id, first.operation_id)
            self.assertFalse(second_created)
            self.assertEqual(len(active_records), 1)

    def test_filesystem_store_restarts_settle_window_after_owner_id_appears(self) -> None:
        class LateOwnerFilesystemRecordStore(FilesystemRecordStore):
            odoo_target_replacement_reservation_settle_timeout_seconds = 0.08
            odoo_target_replacement_reservation_poll_seconds = 0.001

        with TemporaryDirectory() as temporary_directory_name:
            store = LateOwnerFilesystemRecordStore(state_dir=Path(temporary_directory_name))
            owner = OdooStableTargetReplacementOperationRecord.model_validate(
                _operation_payload("operation-cm-testing-first")
            )
            requester = OdooStableTargetReplacementOperationRecord.model_validate(
                {
                    **_operation_payload("operation-cm-testing-second"),
                    "idempotency_key": "replacement-cm-testing-second",
                    "idempotency_scope": "caller-second",
                }
            )
            reservation_path = _lane_reservation_path(store, owner)
            reservation_path.parent.mkdir(parents=True, exist_ok=True)
            reservation_path.write_text("", encoding="utf-8")

            monotonic_time = 0.0
            owner_id_published = False
            owner_record_published = False

            def monotonic() -> float:
                return monotonic_time

            def sleep(seconds: float) -> None:
                nonlocal monotonic_time, owner_id_published, owner_record_published
                monotonic_time += seconds
                if not owner_id_published and monotonic_time >= 0.06:
                    reservation_path.write_text(owner.operation_id, encoding="utf-8")
                    owner_id_published = True
                if not owner_record_published and monotonic_time >= 0.10:
                    store.write_odoo_stable_target_replacement_operation_record(owner)
                    owner_record_published = True

            with (
                patch("control_plane.storage.filesystem.time.monotonic", monotonic),
                patch("control_plane.storage.filesystem.time.sleep", sleep),
            ):
                requester_record, requester_created = (
                    store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
                        requester
                    )
                )

            active_records = store.list_odoo_stable_target_replacement_operation_records(
                product="odoo-tenant-cm",
                context_name="cm",
                instance_name="testing",
                statuses=("pending", "running"),
            )

            self.assertEqual(requester_record.operation_id, owner.operation_id)
            self.assertFalse(requester_created)
            self.assertEqual(len(active_records), 1)

    def test_filesystem_store_recovers_empty_crashed_lane_reservation(self) -> None:
        class FastRecoveryFilesystemRecordStore(FilesystemRecordStore):
            odoo_target_replacement_reservation_settle_timeout_seconds = 0.01
            odoo_target_replacement_reservation_poll_seconds = 0.001

        with TemporaryDirectory() as temporary_directory_name:
            store: FilesystemRecordStore = FastRecoveryFilesystemRecordStore(
                state_dir=Path(temporary_directory_name)
            )
            record = OdooStableTargetReplacementOperationRecord.model_validate(
                _operation_payload("operation-cm-testing-recovered")
            )
            reservation_path = _lane_reservation_path(store, record)
            reservation_path.parent.mkdir(parents=True, exist_ok=True)
            reservation_path.write_text("", encoding="utf-8")

            created_record, created = (
                store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
                    record
                )
            )

            active_records = store.list_odoo_stable_target_replacement_operation_records(
                product="odoo-tenant-cm",
                context_name="cm",
                instance_name="testing",
                statuses=("pending", "running"),
            )

            self.assertEqual(created_record.operation_id, record.operation_id)
            self.assertTrue(created)
            self.assertEqual(reservation_path.read_text(encoding="utf-8"), record.operation_id)
            self.assertEqual(
                tuple(active_record.operation_id for active_record in active_records),
                (record.operation_id,),
            )

    def test_filesystem_store_recovers_missing_reserved_owner_record(self) -> None:
        class FastRecoveryFilesystemRecordStore(FilesystemRecordStore):
            odoo_target_replacement_reservation_settle_timeout_seconds = 0.01
            odoo_target_replacement_reservation_poll_seconds = 0.001

        with TemporaryDirectory() as temporary_directory_name:
            store: FilesystemRecordStore = FastRecoveryFilesystemRecordStore(
                state_dir=Path(temporary_directory_name)
            )
            record = OdooStableTargetReplacementOperationRecord.model_validate(
                _operation_payload("operation-cm-testing-recovered")
            )
            reservation_path = _lane_reservation_path(store, record)
            reservation_path.parent.mkdir(parents=True, exist_ok=True)
            reservation_path.write_text("operation-cm-testing-missing", encoding="utf-8")

            created_record, created = (
                store.create_odoo_stable_target_replacement_operation_record_if_no_active_lane(
                    record
                )
            )

            active_records = store.list_odoo_stable_target_replacement_operation_records(
                product="odoo-tenant-cm",
                context_name="cm",
                instance_name="testing",
                statuses=("pending", "running"),
            )

            self.assertEqual(created_record.operation_id, record.operation_id)
            self.assertTrue(created)
            self.assertEqual(reservation_path.read_text(encoding="utf-8"), record.operation_id)
            self.assertEqual(
                tuple(active_record.operation_id for active_record in active_records),
                (record.operation_id,),
            )

    def test_filesystem_store_waits_for_reserved_operation_json_to_settle(self) -> None:
        class SettlingReadFilesystemRecordStore(FilesystemRecordStore):
            odoo_target_replacement_reservation_settle_timeout_seconds = 2.0

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

    def test_postgres_store_claims_and_heartbeats_operation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite:///{database_path}")
            store.ensure_schema()
            record = OdooStableTargetReplacementOperationRecord.model_validate(_operation_payload())
            store.write_odoo_stable_target_replacement_operation_record(record)

            claimed = store.claim_next_odoo_stable_target_replacement_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-05-17T00:10:00Z",
                claimed_at="2026-05-17T00:01:00Z",
            )
            second_claim = store.claim_next_odoo_stable_target_replacement_operation_record(
                lease_owner="worker-b",
                lease_expires_at="2026-05-17T00:11:00Z",
                claimed_at="2026-05-17T00:02:00Z",
            )
            wrong_owner_heartbeat = store.heartbeat_odoo_stable_target_replacement_operation_record(
                operation_id=record.operation_id,
                lease_owner="worker-b",
                heartbeat_at="2026-05-17T00:03:00Z",
                lease_expires_at="2026-05-17T00:13:00Z",
            )
            owner_heartbeat = store.heartbeat_odoo_stable_target_replacement_operation_record(
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
            loaded = store.read_odoo_stable_target_replacement_operation_record(record.operation_id)
            self.assertEqual(loaded.heartbeat_at, "2026-05-17T00:04:00Z")
            self.assertEqual(loaded.lease_expires_at, "2026-05-17T00:14:00Z")

    def test_postgres_store_rejects_expired_lease_heartbeat_and_completion(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite:///{database_path}")
            store.ensure_schema()
            record = OdooStableTargetReplacementOperationRecord.model_validate(_operation_payload())
            store.write_odoo_stable_target_replacement_operation_record(record)

            claimed = store.claim_next_odoo_stable_target_replacement_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-05-17T00:02:00Z",
                claimed_at="2026-05-17T00:01:00Z",
            )
            assert claimed is not None
            expired_heartbeat = store.heartbeat_odoo_stable_target_replacement_operation_record(
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
            expired_completion = store.complete_odoo_stable_target_replacement_operation_record(
                record=terminal_record,
                lease_owner="worker-a",
            )

            self.assertFalse(expired_heartbeat)
            self.assertFalse(expired_completion)
            loaded = store.read_odoo_stable_target_replacement_operation_record(record.operation_id)
            self.assertEqual(loaded.status, "running")
            self.assertEqual(loaded.phase, "running")

    def test_postgres_store_recovers_safe_expired_operation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            database_path = Path(temporary_directory_name) / "launchplane.sqlite3"
            store = PostgresRecordStore(database_url=f"sqlite:///{database_path}")
            store.ensure_schema()
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(
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

            affected = store.recover_expired_odoo_stable_target_replacement_operation_records(
                now="2026-05-17T00:03:00Z",
                safe_phases=("created",),
                max_attempts=3,
            )

            self.assertEqual(affected, ("operation-cm-testing",))
            loaded = store.read_odoo_stable_target_replacement_operation_record(
                "operation-cm-testing"
            )
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
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(
                    {
                        **_operation_payload(),
                        "status": "running",
                        "phase": "apply",
                        "started_at": "2026-05-17T00:01:00Z",
                        "lease_owner": "worker-a",
                        "lease_expires_at": "2026-05-17T00:02:00Z",
                        "heartbeat_at": "2026-05-17T00:01:00Z",
                        "attempt": 1,
                    }
                )
            )

            affected = store.recover_expired_odoo_stable_target_replacement_operation_records(
                now="2026-05-17T00:03:00Z",
                safe_phases=("created",),
                max_attempts=3,
            )

            self.assertEqual(affected, ("operation-cm-testing",))
            loaded = store.read_odoo_stable_target_replacement_operation_record(
                "operation-cm-testing"
            )
            self.assertEqual(loaded.status, "fail")
            self.assertEqual(loaded.phase, "failed")
            self.assertIn("unsafe to retry", loaded.error_message)
            self.assertEqual(loaded.lease_owner, "")


if __name__ == "__main__":
    unittest.main()
