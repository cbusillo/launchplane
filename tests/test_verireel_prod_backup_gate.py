import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane import secrets as control_plane_secrets
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.durable_operation_authorization import (
    DurableOperationAuthorization,
)
from control_plane.contracts.verireel_prod_backup_gate import (
    VeriReelProdBackupGateRequest,
    VeriReelProdBackupGateWorkerRequest,
    VeriReelProdBackupGateWorkerResult,
)
from control_plane.contracts.verireel_prod_backup_gate_operation import (
    VeriReelProdBackupGateOperationRecord,
    build_verireel_prod_backup_gate_operation_id,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows import verireel_prod_backup_gate_worker
from control_plane.workflows.verireel_prod_backup_gate import (
    DEFAULT_TIMEOUT_SECONDS,
    _run_delegated_worker,
    enqueue_verireel_prod_backup_gate,
    execute_verireel_prod_backup_gate,
)
from control_plane.workflows.verireel_prod_backup_gate_operation_worker import (
    build_verireel_prod_backup_gate_operation_worker_status,
    reconcile_stale_verireel_prod_backup_gate_operation_records,
    run_verireel_prod_backup_gate_operation_worker_loop,
    run_verireel_prod_backup_gate_operation_worker_once,
)
from tests.support.durable_operations import (
    durable_operation_authorization_payload,
    durable_operation_cancellation_payload,
    durable_operation_policy_record,
)


class VeriReelProdBackupGateWorkflowTests(unittest.TestCase):
    def _sqlite_database_url(self, root: Path) -> str:
        return f"sqlite+pysqlite:///{root / 'launchplane.sqlite3'}"

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
            "control_plane.workflows.verireel_prod_backup_gate_operation_worker.read_active_authz_policy_record",
            return_value=self.authorization_policy_record,
        )
        self.authorization_policy_patcher.start()
        self.addCleanup(self.authorization_policy_patcher.stop)

    def _write_prod_worker_secret_bindings(self, store: PostgresRecordStore) -> None:
        plaintext_values = {
            "VERIREEL_PROD_PROXMOX_SSH_KNOWN_HOSTS": "runtime-known-hosts",
            "VERIREEL_PROD_PROXMOX_SSH_PRIVATE_KEY": "runtime-private-key",
        }
        with patch.dict(
            "os.environ",
            {control_plane_secrets.LAUNCHPLANE_SECRET_MASTER_KEY_ENV_VAR: "test-master-key"},
        ):
            for binding_key, plaintext_value in plaintext_values.items():
                control_plane_secrets.write_secret_value(
                    record_store=store,
                    scope="context_instance",
                    integration=control_plane_secrets.LAUNCHPLANE_WORKER_SECRET_INTEGRATION,
                    name=binding_key,
                    plaintext_value=plaintext_value,
                    binding_key=binding_key,
                    context_name="verireel",
                    instance_name="prod",
                    actor="test",
                )

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

    def test_legacy_dispatch_refuses_without_reading_runtime_or_starting_worker(self) -> None:
        with (
            patch("subprocess.run") as run,
            patch.object(
                control_plane_runtime_environments, "resolve_runtime_environment_values"
            ) as resolve,
        ):
            with self.assertRaisesRegex(click.ClickException, "/v1/production-backup-gates"):
                _run_delegated_worker(
                    control_plane_root=Path("unused"),
                    request=VeriReelProdBackupGateWorkerRequest(
                        context="example", instance="prod", backup_record_id="legacy-backup"
                    ),
                )
        resolve.assert_not_called()
        run.assert_not_called()

    def test_prod_backup_gate_default_timeout_allows_longer_vzdump_backup(self) -> None:
        self.assertEqual(DEFAULT_TIMEOUT_SECONDS, 1800)

        request = VeriReelProdBackupGateRequest(
            backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1"
        )

        self.assertEqual(request.timeout_seconds, 1800)

    def test_enqueue_verireel_prod_backup_gate_records_pending_operation_and_replays_terminal_evidence(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            request = VeriReelProdBackupGateRequest(
                backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1"
            )

            result = enqueue_verireel_prod_backup_gate(
                record_store=record_store,
                request=request,
                authorization=self._operation_authorization(),
                now="2026-04-25T00:00:00Z",
            )

            self.assertEqual(result.backup_status, "pending")
            record = record_store.read_backup_gate_record(result.backup_record_id)
            self.assertEqual(record.status, "pending")
            operations = record_store.list_verireel_prod_backup_gate_operation_records(
                backup_record_id=request.backup_record_id
            )
            self.assertEqual(len(operations), 1)
            self.assertEqual(operations[0].status, "pending")

            replay = enqueue_verireel_prod_backup_gate(
                record_store=record_store,
                request=request,
                authorization=self._operation_authorization(),
                now="2026-04-25T00:01:00Z",
            )
            self.assertEqual(replay.backup_status, "pending")
            self.assertEqual(
                len(
                    record_store.list_verireel_prod_backup_gate_operation_records(
                        backup_record_id=request.backup_record_id
                    )
                ),
                1,
            )

            record_store.write_backup_gate_record(
                BackupGateRecord(
                    record_id=result.backup_record_id,
                    context="verireel",
                    instance="prod",
                    created_at="2026-04-25T00:16:00Z",
                    source="launchplane-verireel-prod-backup-gate",
                    required=True,
                    status="pass",
                    evidence={"snapshot_name": "ver-predeploy-20260425-001500"},
                )
            )

            completed_result = enqueue_verireel_prod_backup_gate(
                record_store=record_store,
                request=request,
                authorization=self._operation_authorization(),
                now="2026-04-25T00:02:00Z",
            )

            self.assertEqual(completed_result.backup_status, "pass")
            self.assertEqual(completed_result.snapshot_name, "ver-predeploy-20260425-001500")

    def test_enqueue_verireel_prod_backup_gate_rejects_conflicting_request(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            request = VeriReelProdBackupGateRequest(
                backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1"
            )
            enqueue_verireel_prod_backup_gate(
                record_store=record_store,
                request=request,
                authorization=self._operation_authorization(),
                now="2026-04-25T00:00:00Z",
            )

            with self.assertRaisesRegex(click.ClickException, "conflicts"):
                enqueue_verireel_prod_backup_gate(
                    record_store=record_store,
                    request=VeriReelProdBackupGateRequest(
                        backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                        timeout_seconds=42,
                    ),
                    authorization=self._operation_authorization(),
                    now="2026-04-25T00:01:00Z",
                )

    def test_enqueue_verireel_prod_backup_gate_rejects_raced_conflicting_operation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            request = VeriReelProdBackupGateRequest(
                backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                timeout_seconds=42,
            )
            existing_operation = self._operation_record(
                operation_id=build_verireel_prod_backup_gate_operation_id(
                    product="verireel",
                    context="verireel",
                    instance="prod",
                    backup_record_id=request.backup_record_id,
                )
            )

            def _return_raced_operation(
                _operation: VeriReelProdBackupGateOperationRecord,
                **_kwargs: object,
            ) -> tuple[VeriReelProdBackupGateOperationRecord, bool]:
                return existing_operation, False

            with patch.object(
                record_store,
                "create_verireel_prod_backup_gate_operation_record_if_no_active_record",
                side_effect=_return_raced_operation,
            ):
                with self.assertRaisesRegex(click.ClickException, "conflicts"):
                    enqueue_verireel_prod_backup_gate(
                        record_store=record_store,
                        request=request,
                        authorization=self._operation_authorization(),
                        now="2026-04-25T00:01:00Z",
                    )
            with self.assertRaises(FileNotFoundError):
                record_store.read_backup_gate_record(request.backup_record_id)

    def test_enqueue_verireel_prod_backup_gate_materializes_failed_terminal_operation(
        self,
    ) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            request = VeriReelProdBackupGateRequest(
                backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1"
            )
            record_store.write_backup_gate_record(
                BackupGateRecord(
                    record_id=request.backup_record_id,
                    context="verireel",
                    instance="prod",
                    created_at="2026-04-25T00:00:00Z",
                    source="launchplane-verireel-prod-backup-gate",
                    required=True,
                    status="pending",
                    evidence={},
                )
            )
            record_store.write_verireel_prod_backup_gate_operation_record(
                self._operation_record(
                    operation_id=build_verireel_prod_backup_gate_operation_id(
                        product="verireel",
                        context="verireel",
                        instance="prod",
                        backup_record_id=request.backup_record_id,
                    ),
                    status="fail",
                    phase="failed",
                    error_message="lease expired in backup_gate",
                )
            )

            result = enqueue_verireel_prod_backup_gate(
                record_store=record_store,
                request=request,
                authorization=self._operation_authorization(),
                now="2026-04-25T00:03:00Z",
            )

            self.assertEqual(result.backup_status, "fail")
            self.assertEqual(result.error_message, "lease expired in backup_gate")
            backup_record = record_store.read_backup_gate_record(request.backup_record_id)
            self.assertEqual(backup_record.status, "fail")
            self.assertEqual(
                backup_record.evidence["error_message"], "lease expired in backup_gate"
            )

    def test_verireel_operation_worker_reauthorizes_before_delegated_effect(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            record_store.write_verireel_prod_backup_gate_operation_record(self._operation_record())
            revoked_policy = durable_operation_policy_record(revision=43)

            with (
                patch(
                    "control_plane.workflows.verireel_prod_backup_gate_operation_worker.read_active_authz_policy_record",
                    side_effect=(self.authorization_policy_record, revoked_policy),
                ),
                patch(
                    "control_plane.workflows.verireel_prod_backup_gate_operation_worker._run_delegated_worker"
                ) as delegated_worker,
            ):
                result = run_verireel_prod_backup_gate_operation_worker_once(
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
            delegated_worker.assert_not_called()

    def test_verireel_operation_worker_fails_closed_for_legacy_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            record_store.write_verireel_prod_backup_gate_operation_record(
                self._operation_record(include_authorization=False)
            )

            with patch(
                "control_plane.workflows.verireel_prod_backup_gate_operation_worker._run_delegated_worker"
            ) as delegated_worker:
                result = run_verireel_prod_backup_gate_operation_worker_once(
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
            self.assertEqual(
                operation.error_code,
                "operation_authorization_provenance_missing",
            )
            delegated_worker.assert_not_called()

    def test_verireel_operation_worker_claims_and_executes_pending_operation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            record_store.write_verireel_prod_backup_gate_operation_record(self._operation_record())

            with patch(
                "control_plane.workflows.verireel_prod_backup_gate_operation_worker._run_delegated_worker",
                return_value=VeriReelProdBackupGateWorkerResult(
                    status="pass",
                    snapshot_name="ver-predeploy-20260425-001500",
                    started_at="2026-04-25T00:15:00Z",
                    finished_at="2026-04-25T00:16:00Z",
                    detail="Backup completed.",
                    evidence={"snapshot_name": "ver-predeploy-20260425-001500"},
                ),
            ):
                result = run_verireel_prod_backup_gate_operation_worker_once(
                    record_store=record_store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            self.assertEqual(result.status, "worked")
            self.assertTrue(result.terminal_write_committed)
            backup_record = record_store.read_backup_gate_record(
                "backup-gate-verireel-prod-run-12345-attempt-1"
            )
            self.assertEqual(backup_record.status, "pass")
            operation = record_store.read_verireel_prod_backup_gate_operation_record(
                "verireel-operation-1"
            )
            self.assertEqual(operation.status, "pass")
            self.assertEqual(operation.phase, "completed")
            self.assertEqual(operation.attempt, 1)

    def test_verireel_operation_worker_writes_fail_record_on_worker_exception(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            record_store.write_verireel_prod_backup_gate_operation_record(self._operation_record())

            with patch(
                "control_plane.workflows.verireel_prod_backup_gate_operation_worker._run_delegated_worker",
                side_effect=click.ClickException("pct snapshot failed"),
            ):
                result = run_verireel_prod_backup_gate_operation_worker_once(
                    record_store=record_store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            self.assertEqual(result.status, "worked")
            backup_record = record_store.read_backup_gate_record(
                "backup-gate-verireel-prod-run-12345-attempt-1"
            )
            self.assertEqual(backup_record.status, "fail")
            self.assertEqual(backup_record.evidence["error_message"], "pct snapshot failed")
            operation = record_store.read_verireel_prod_backup_gate_operation_record(
                "verireel-operation-1"
            )
            self.assertEqual(operation.status, "fail")
            self.assertEqual(operation.phase, "failed")

    def test_verireel_operation_worker_preserves_structured_fail_detail(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            record_store.write_verireel_prod_backup_gate_operation_record(self._operation_record())

            with patch(
                "control_plane.workflows.verireel_prod_backup_gate_operation_worker._run_delegated_worker",
                return_value=VeriReelProdBackupGateWorkerResult(
                    status="fail",
                    started_at="2026-04-25T00:15:00Z",
                    finished_at="2026-04-25T00:16:00Z",
                    detail="pct snapshot failed",
                    evidence={"exit_code": "17"},
                ),
            ):
                result = run_verireel_prod_backup_gate_operation_worker_once(
                    record_store=record_store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            self.assertEqual(result.status, "worked")
            backup_record = record_store.read_backup_gate_record(
                "backup-gate-verireel-prod-run-12345-attempt-1"
            )
            self.assertEqual(backup_record.status, "fail")
            self.assertEqual(backup_record.evidence["error_message"], "pct snapshot failed")
            self.assertEqual(backup_record.evidence["exit_code"], "17")

            replay = enqueue_verireel_prod_backup_gate(
                record_store=record_store,
                request=VeriReelProdBackupGateRequest(
                    backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1"
                ),
                authorization=self._operation_authorization(),
                now="2026-04-25T00:17:00Z",
            )
            self.assertEqual(replay.backup_status, "fail")
            self.assertEqual(replay.error_message, "pct snapshot failed")

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

            result = reconcile_stale_verireel_prod_backup_gate_operation_records(
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

            status = build_verireel_prod_backup_gate_operation_worker_status(
                record_store=record_store,
                now="2026-04-25T00:10:00Z",
            )

            self.assertEqual(status.status, "stalled")
            self.assertEqual(status.running_count, 1)
            self.assertEqual(status.stalled_count, 1)
            loop_result = run_verireel_prod_backup_gate_operation_worker_loop(
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

            with patch(
                "control_plane.workflows.verireel_prod_backup_gate_operation_worker._run_delegated_worker",
                return_value=VeriReelProdBackupGateWorkerResult(
                    status="pass",
                    snapshot_name="ver-predeploy-20260425-001500",
                    started_at="2026-04-25T00:15:00Z",
                    finished_at="2026-04-25T00:16:00Z",
                    detail="Backup completed.",
                    evidence={"snapshot_name": "ver-predeploy-20260425-001500"},
                ),
            ) as delegated_worker:
                second_result = run_verireel_prod_backup_gate_operation_worker_once(
                    record_store=record_store,
                    control_plane_root_path=root,
                    lease_owner="worker-b",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            self.assertEqual(second_result.status, "idle")
            delegated_worker.assert_not_called()
            with self.assertRaises(FileNotFoundError):
                record_store.read_backup_gate_record(
                    "backup-gate-verireel-prod-run-12345-attempt-1"
                )

    def test_legacy_worker_refuses_even_with_backup_disabled(self) -> None:
        with (
            patch.dict("os.environ", {"VERIREEL_PROD_BACKUP_MODE": "none"}, clear=True),
            patch("subprocess.run") as run,
        ):
            with self.assertRaisesRegex(click.ClickException, "typed production backup"):
                verireel_prod_backup_gate_worker.execute_worker(
                    VeriReelProdBackupGateWorkerRequest(
                        context="example", instance="prod", backup_record_id="legacy-backup"
                    )
                )
        run.assert_not_called()

    def test_execute_verireel_prod_backup_gate_records_pass_status(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)

            with patch(
                "control_plane.workflows.verireel_prod_backup_gate._run_delegated_worker",
                return_value=VeriReelProdBackupGateWorkerResult(
                    status="pass",
                    snapshot_name="ver-predeploy-20260425-001500",
                    started_at="2026-04-25T00:15:00Z",
                    finished_at="2026-04-25T00:16:00Z",
                    detail="Backup completed.",
                    evidence={
                        "snapshot_name": "ver-predeploy-20260425-001500",
                        "backup_mode": "snapshot,vzdump",
                    },
                ),
            ):
                result = execute_verireel_prod_backup_gate(
                    control_plane_root=root,
                    record_store=record_store,
                    request=VeriReelProdBackupGateRequest(
                        backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1"
                    ),
                )

            self.assertEqual(result.backup_status, "pass")
            self.assertEqual(result.snapshot_name, "ver-predeploy-20260425-001500")
            record = record_store.read_backup_gate_record(result.backup_record_id)
            self.assertEqual(record.status, "pass")
            self.assertEqual(record.source, "launchplane-verireel-prod-backup-gate")
            self.assertEqual(record.evidence["snapshot_name"], "ver-predeploy-20260425-001500")

    def test_execute_verireel_prod_backup_gate_records_worker_failure(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)

            with patch(
                "control_plane.workflows.verireel_prod_backup_gate._run_delegated_worker",
                return_value=VeriReelProdBackupGateWorkerResult(
                    status="fail",
                    snapshot_name="",
                    started_at="2026-04-25T00:15:00Z",
                    finished_at="2026-04-25T00:15:30Z",
                    detail="pct snapshot failed",
                    evidence={},
                ),
            ):
                result = execute_verireel_prod_backup_gate(
                    control_plane_root=root,
                    record_store=record_store,
                    request=VeriReelProdBackupGateRequest(
                        backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1"
                    ),
                )

            self.assertEqual(result.backup_status, "fail")
            self.assertEqual(result.error_message, "pct snapshot failed")
            record = record_store.read_backup_gate_record(result.backup_record_id)
            self.assertEqual(record.status, "fail")
            self.assertEqual(record.evidence["error_message"], "pct snapshot failed")
