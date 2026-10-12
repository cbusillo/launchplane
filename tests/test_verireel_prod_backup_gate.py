import unittest

from control_plane.contracts.production_backup_failures import LEGACY_BACKUP_EXECUTION_RETIRED_CODE
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch


from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
)
from control_plane.contracts.verireel_prod_backup_gate import (
    VeriReelProdBackupGateRequest,
)
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VeriReelProdBackupGateOperationRecord,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_backup_gate_operation_worker import (
    build_production_backup_gate_operation_worker_status,
    reconcile_stale_production_backup_gate_operation_records,
    run_production_backup_gate_operation_worker_loop,
    run_production_backup_gate_operation_worker_once,
)
from tests.support.durable_operations import (
    durable_operation_authorization_payload,
    durable_operation_cancellation_payload,
    durable_operation_policy_record,
)


class VeriReelProdBackupGateWorkflowTests(unittest.TestCase):
    def _record_store(self, root: Path) -> FilesystemRecordStore:
        return FilesystemRecordStore(root / "state")

    def _operation_record(
        self,
        *,
        operation_id: str = "verireel-operation-1",
        backup_record_id: str = "backup-gate-verireel-prod-run-12345-attempt-1",
        status: str = "pending",
        phase: str = "created",
        attempt: int = 0,
        lease_owner: str = "",
        lease_expires_at: str = "",
        error_message: str = "backup failed",
        include_authorization: bool = True,
    ) -> VeriReelProdBackupGateOperationRecord:
        request = VeriReelProdBackupGateRequest(backup_record_id=backup_record_id)
        payload: dict[str, object] = {
            "schema_version": 2 if include_authorization else 1,
            "operation_id": operation_id,
            "product": "verireel",
            "context": "verireel",
            "instance": "prod",
            "backup_record_id": backup_record_id,
            "request_fingerprint": request.model_dump_json(),
            "request": request.model_dump(mode="json"),
            "status": status,
            "phase": phase,
            "created_at": "2026-04-25T00:00:00Z",
            "updated_at": "2026-04-25T00:00:00Z",
            "attempt": attempt,
            "lease_owner": lease_owner,
            "lease_expires_at": lease_expires_at,
            "heartbeat_at": "2026-04-25T00:01:00Z" if lease_owner else "",
        }
        if include_authorization:
            payload["authorization"] = self._operation_authorization().model_dump(mode="json")
        if status in {"pass", "fail"}:
            payload["finished_at"] = "2026-04-25T00:02:00Z"
            if status == "fail":
                payload["error_message"] = error_message
        return VeriReelProdBackupGateOperationRecord.model_validate(payload)

    def _operation_authorization(self) -> DurableOperationAuthorization:
        payload = durable_operation_authorization_payload(
            action="verireel_prod_backup_gate.execute",
            managed_rule_id="verireel-prod-backup-gate",
        )
        payload["product"] = "verireel"
        payload["context"] = "verireel"
        payload["instances"] = ["prod"]
        return DurableOperationAuthorization.model_validate(payload)

    def setUp(self) -> None:
        authorization = self._operation_authorization().model_dump(mode="json")
        self.authorization_policy_record = durable_operation_policy_record(authorization)
        self.authorization_policy_patcher = patch(
            "control_plane.workflows.production_backup_gate_operation_worker.read_active_authz_policy_record",
            return_value=self.authorization_policy_record,
        )
        self.authorization_policy_patcher.start()
        self.addCleanup(self.authorization_policy_patcher.stop)

    def test_schema_v2_operation_requires_authorization_provenance(self) -> None:
        legacy_record = self._operation_record(include_authorization=False)
        with self.assertRaisesRegex(ValueError, "requires authorization provenance"):
            VeriReelProdBackupGateOperationRecord.model_validate(
                {**legacy_record.model_dump(mode="json"), "schema_version": 2}
            )

        authorization = durable_operation_authorization_payload(
            action="verireel_prod_backup_gate.execute",
            managed_rule_id="verireel-prod-backup-gate",
        )
        authorization["product"] = "verireel"
        authorization["context"] = "verireel"
        authorization["instances"] = ["prod"]
        record = VeriReelProdBackupGateOperationRecord.model_validate(
            {
                **legacy_record.model_dump(mode="json"),
                "schema_version": 2,
                "authorization": authorization,
            }
        )
        self.assertIsNotNone(record.authorization)
        assert record.authorization is not None
        self.assertEqual(record.authorization.managed_rule_id, "verireel-prod-backup-gate")

    def test_authorized_historical_request_fails_without_capture_or_retry(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = self._record_store(root)
            operation = self._operation_record()
            store.write_verireel_prod_backup_gate_operation_record(operation)
            result = run_production_backup_gate_operation_worker_once(
                record_store=store, control_plane_root_path=root, lease_owner="history-worker"
            )
            second = run_production_backup_gate_operation_worker_once(
                record_store=store, control_plane_root_path=root, lease_owner="history-worker"
            )
            self.assertTrue(result.terminal_write_committed)
            self.assertEqual(second.status, "idle")
            persisted = store.read_verireel_prod_backup_gate_operation_record(
                operation.operation_id
            )
            self.assertEqual(persisted.status, "fail")
            self.assertEqual(persisted.error_code, LEGACY_BACKUP_EXECUTION_RETIRED_CODE)
            self.assertEqual(
                store.read_backup_gate_record(operation.backup_record_id).status, "fail"
            )

    def test_pending_operation_can_be_cancelled_with_typed_evidence(self) -> None:
        legacy_record = self._operation_record(include_authorization=False)
        authorization = durable_operation_authorization_payload(
            action="verireel_prod_backup_gate.execute",
            managed_rule_id="verireel-prod-backup-gate",
        )
        authorization["product"] = "verireel"
        authorization["context"] = "verireel"
        authorization["instances"] = ["prod"]
        cancellation = durable_operation_cancellation_payload()
        cancellation["caller"] = authorization["caller"]
        record = VeriReelProdBackupGateOperationRecord.model_validate(
            {
                **legacy_record.model_dump(mode="json"),
                "schema_version": 2,
                "authorization": authorization,
                "status": "cancelled",
                "phase": "cancelled",
                "finished_at": "2026-07-23T03:32:00Z",
                "cancellation": cancellation,
            }
        )
        self.assertEqual(record.status, "cancelled")

    def test_pending_verireel_cancellation_prevents_worker_claim(self) -> None:
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
                        pending_record = self._operation_record()
                        store.write_verireel_prod_backup_gate_operation_record(pending_record)
                        cancellation = durable_operation_cancellation_payload()
                        assert pending_record.authorization is not None
                        cancellation["caller"] = pending_record.authorization.caller.model_dump(
                            mode="json"
                        )
                        cancelled_record = VeriReelProdBackupGateOperationRecord.model_validate(
                            {
                                **pending_record.model_dump(mode="json"),
                                "status": "cancelled",
                                "phase": "cancelled",
                                "updated_at": "2026-07-23T03:32:00Z",
                                "finished_at": "2026-07-23T03:32:00Z",
                                "cancellation": cancellation,
                            }
                        )

                        self.assertTrue(
                            store.cancel_pending_verireel_prod_backup_gate_operation_record(
                                cancelled_record
                            )
                        )
                        self.assertFalse(
                            store.cancel_pending_verireel_prod_backup_gate_operation_record(
                                cancelled_record
                            )
                        )
                        self.assertIsNone(
                            store.claim_next_verireel_prod_backup_gate_operation_record(
                                lease_owner="worker-a",
                                lease_expires_at="2026-07-23T03:40:00Z",
                                claimed_at="2026-07-23T03:35:00Z",
                            )
                        )
                        backup_record = store.read_backup_gate_record(
                            pending_record.backup_record_id
                        )
                        self.assertEqual(backup_record.status, "fail")
                        self.assertEqual(
                            backup_record.evidence["operation_status"],
                            "cancelled",
                        )
            finally:
                stores[1].close()

    def test_verireel_operation_worker_reauthorizes_before_delegated_effect(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            record_store.write_verireel_prod_backup_gate_operation_record(self._operation_record())
            revoked_policy = durable_operation_policy_record(revision=43)
            with patch(
                "control_plane.workflows.production_backup_gate_operation_worker.read_active_authz_policy_record",
                side_effect=(self.authorization_policy_record, revoked_policy),
            ):
                result = run_production_backup_gate_operation_worker_once(
                    record_store=record_store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )
            operation = record_store.read_verireel_prod_backup_gate_operation_record(
                "verireel-operation-1"
            )
            self.assertEqual(result.status, "worked")
            self.assertEqual(operation.status, "fail")
            self.assertEqual(operation.error_code, "operation_authorization_revoked")

    def test_verireel_operation_worker_fails_closed_for_legacy_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            record_store.write_verireel_prod_backup_gate_operation_record(
                self._operation_record(include_authorization=False)
            )
            result = run_production_backup_gate_operation_worker_once(
                record_store=record_store,
                control_plane_root_path=root,
                lease_owner="worker-a",
                lease_seconds=300,
                heartbeat_seconds=60,
            )
            operation = record_store.read_verireel_prod_backup_gate_operation_record(
                "verireel-operation-1"
            )
            self.assertEqual(result.status, "worked")
            self.assertEqual(operation.status, "fail")
            self.assertEqual(operation.error_code, "operation_authorization_provenance_missing")

    def test_verireel_operation_worker_recovers_created_and_fails_backup_gate_phase(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            record_store.write_verireel_prod_backup_gate_operation_record(
                self._operation_record(
                    operation_id="created-operation",
                    backup_record_id="backup-gate-created",
                    status="running",
                    phase="created",
                    attempt=1,
                    lease_owner="old-worker",
                    lease_expires_at="2000-01-01T00:00:00Z",
                )
            )
            record_store.write_verireel_prod_backup_gate_operation_record(
                self._operation_record(
                    operation_id="backup-gate-operation",
                    backup_record_id="backup-gate-side-effect",
                    status="running",
                    phase="backup_gate",
                    attempt=1,
                    lease_owner="old-worker",
                    lease_expires_at="2000-01-01T00:00:00Z",
                )
            )
            record_store.write_verireel_prod_backup_gate_operation_record(
                self._operation_record(
                    operation_id="running-operation",
                    backup_record_id="backup-gate-running-before-side-effect",
                    status="running",
                    phase="running",
                    attempt=1,
                    lease_owner="old-worker",
                    lease_expires_at="2000-01-01T00:00:00Z",
                )
            )

            result = reconcile_stale_production_backup_gate_operation_records(
                record_store=record_store,
                now="2026-04-25T00:10:00Z",
            )

            self.assertEqual(
                set(result.reconciled_operation_ids),
                {"created-operation", "backup-gate-operation", "running-operation"},
            )
            recovered = record_store.read_verireel_prod_backup_gate_operation_record(
                "created-operation"
            )
            self.assertEqual(recovered.status, "pending")
            self.assertEqual(recovered.phase, "created")
            recovered_running = record_store.read_verireel_prod_backup_gate_operation_record(
                "running-operation"
            )
            self.assertEqual(recovered_running.status, "pending")
            self.assertEqual(recovered_running.phase, "running")
            failed = record_store.read_verireel_prod_backup_gate_operation_record(
                "backup-gate-operation"
            )
            self.assertEqual(failed.status, "fail")
            self.assertEqual(failed.phase, "failed")
            self.assertIn("unsafe to retry", failed.error_message)
            failed_backup_gate = record_store.read_backup_gate_record("backup-gate-side-effect")
            self.assertEqual(failed_backup_gate.status, "fail")
            self.assertIn("unsafe to retry", failed_backup_gate.evidence["error_message"])

    def test_verireel_operation_worker_status_and_loop_are_observable(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            record_store.write_verireel_prod_backup_gate_operation_record(
                self._operation_record(
                    status="running",
                    phase="backup_gate",
                    attempt=1,
                    lease_owner="old-worker",
                    lease_expires_at="2000-01-01T00:00:00Z",
                )
            )

            status = build_production_backup_gate_operation_worker_status(
                record_store=record_store,
                now="2026-04-25T00:10:00Z",
            )

            self.assertEqual(status.status, "stalled")
            self.assertEqual(status.running_count, 1)
            self.assertEqual(status.stalled_count, 1)
            loop_result = run_production_backup_gate_operation_worker_loop(
                record_store=record_store,
                control_plane_root_path=root,
                lease_owner="worker-a",
                poll_seconds=1,
                max_iterations=1,
            )
            self.assertEqual(loop_result.status, "completed")
            self.assertEqual(loop_result.iterations, 1)

    def test_verireel_operation_worker_does_not_publish_evidence_after_lease_loss(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            record_store.write_verireel_prod_backup_gate_operation_record(self._operation_record())
            claimed = record_store.claim_next_verireel_prod_backup_gate_operation_record(
                lease_owner="worker-a",
                lease_expires_at="2026-04-25T00:05:00Z",
                claimed_at="2026-04-25T00:01:00Z",
            )
            assert claimed is not None
            record_store.write_verireel_prod_backup_gate_operation_record(
                claimed.model_copy(
                    update={
                        "status": "fail",
                        "phase": "failed",
                        "finished_at": "2026-04-25T00:02:00Z",
                        "lease_owner": "other-worker",
                        "error_message": "superseded lease",
                    }
                )
            )
            second_result = run_production_backup_gate_operation_worker_once(
                record_store=record_store,
                control_plane_root_path=root,
                lease_owner="worker-b",
                lease_seconds=300,
                heartbeat_seconds=60,
            )
            self.assertEqual(second_result.status, "idle")
            with self.assertRaises(FileNotFoundError):
                record_store.read_backup_gate_record(
                    "backup-gate-verireel-prod-run-12345-attempt-1"
                )
