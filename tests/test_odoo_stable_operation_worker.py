import json
import logging
import unittest
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
from unittest.mock import patch

from click.testing import CliRunner

from control_plane.cli import main
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_bootstrap import OdooStableBootstrapResult
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    OdooStableTargetReplacementApplyResult,
)
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.odoo_stable_operation_worker import (
    OdooStableOperationWorkerLoopResult,
    build_odoo_stable_operation_worker_status,
    reconcile_stale_odoo_stable_operation_records,
    run_odoo_stable_operation_worker_loop,
    run_odoo_stable_operation_worker_once,
)


def _bootstrap_payload(operation_id: str = "operation-cm-testing") -> dict[str, object]:
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


def _replacement_payload(operation_id: str = "operation-cm-testing") -> dict[str, object]:
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


class OdooStableOperationWorkerTests(unittest.TestCase):
    def test_worker_executes_bootstrap_and_writes_terminal_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(_bootstrap_payload())
            )
            result = OdooStableBootstrapResult(
                product="odoo-tenant-cm",
                context="cm",
                instance="testing",
                deployment_record_id="deployment-cm-testing",
                bootstrap_status="pass",
                bootstrap_run_status="pass",
                readiness_status="pass",
                post_deploy_status="pass",
                health_status="pass",
                canonical_status="pass",
                logo_status="pass",
            )

            with patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_bootstrap",
                return_value=result,
            ) as execute_mock:
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            operation = store.read_odoo_stable_bootstrap_operation_record("operation-cm-testing")
            self.assertEqual(worker_result.status, "worked")
            self.assertEqual(worker_result.operation_kind, "odoo_stable_bootstrap")
            self.assertTrue(worker_result.terminal_write_committed)
            self.assertEqual(operation.status, "pass")
            self.assertEqual(operation.phase, "completed")
            self.assertEqual(operation.deployment_record_id, "deployment-cm-testing")
            self.assertEqual(operation.lease_owner, "worker-a")
            execute_mock.assert_called_once()

    def test_worker_executes_target_replacement_and_writes_terminal_result(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(_replacement_payload())
            )
            result = OdooStableTargetReplacementApplyResult(
                product="odoo-tenant-cm",
                context="cm",
                instance="testing",
                strategy="recreate-in-place",
                deployment_record_id="deployment-cm-testing",
                deploy_status="pass",
                post_deploy_status="pass",
                health_status="pass",
                canonical_status="pass",
                logo_status="pass",
                runtime_identity_injected=True,
            )

            with patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_target_replacement_apply",
                return_value=result,
            ) as execute_mock:
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            operation = store.read_odoo_stable_target_replacement_operation_record(
                "operation-cm-testing"
            )
            self.assertEqual(worker_result.status, "worked")
            self.assertEqual(worker_result.operation_kind, "odoo_stable_target_replacement")
            self.assertTrue(worker_result.terminal_write_committed)
            self.assertEqual(operation.status, "pass")
            self.assertEqual(operation.phase, "completed")
            self.assertEqual(operation.deployment_record_id, "deployment-cm-testing")
            self.assertEqual(operation.lease_owner, "worker-a")
            execute_mock.assert_called_once()

    def test_worker_writes_failure_when_execution_raises(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(_bootstrap_payload())
            )

            with (
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_bootstrap",
                    side_effect=RuntimeError("provider unavailable"),
                ),
                self.assertLogs(level=logging.ERROR),
            ):
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            operation = store.read_odoo_stable_bootstrap_operation_record("operation-cm-testing")
            self.assertEqual(worker_result.status, "worked")
            self.assertTrue(worker_result.terminal_write_committed)
            self.assertEqual(operation.status, "fail")
            self.assertEqual(operation.phase, "failed")
            self.assertEqual(operation.error_message, "provider unavailable")

    def test_worker_writes_target_replacement_failure_when_execution_raises(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(_replacement_payload())
            )

            with (
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_target_replacement_apply",
                    side_effect=RuntimeError("provider unavailable"),
                ),
                self.assertLogs(level=logging.ERROR),
            ):
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            operation = store.read_odoo_stable_target_replacement_operation_record(
                "operation-cm-testing"
            )
            self.assertEqual(worker_result.status, "worked")
            self.assertTrue(worker_result.terminal_write_committed)
            self.assertEqual(operation.status, "fail")
            self.assertEqual(operation.phase, "failed")
            self.assertEqual(operation.error_message, "provider unavailable")

    def test_worker_recovers_expired_safe_operation_before_claiming(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    {
                        **_bootstrap_payload(),
                        "status": "running",
                        "phase": "created",
                        "started_at": "2026-05-17T00:01:00Z",
                        "lease_owner": "old-worker",
                        "lease_expires_at": "2000-01-01T00:00:00Z",
                        "heartbeat_at": "2000-01-01T00:00:00Z",
                        "attempt": 1,
                    }
                )
            )
            result = OdooStableBootstrapResult(
                product="odoo-tenant-cm",
                context="cm",
                instance="testing",
                deployment_record_id="deployment-cm-testing",
                bootstrap_status="pass",
                bootstrap_run_status="pass",
                readiness_status="pass",
                post_deploy_status="pass",
                health_status="pass",
                canonical_status="pass",
                logo_status="pass",
            )

            with patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_bootstrap",
                return_value=result,
            ):
                worker_result = run_odoo_stable_operation_worker_once(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                )

            self.assertEqual(worker_result.status, "worked")
            self.assertEqual(worker_result.recovered_operation_ids, ("operation-cm-testing",))
            operation = store.read_odoo_stable_bootstrap_operation_record("operation-cm-testing")
            self.assertEqual(operation.status, "pass")
            self.assertEqual(operation.attempt, 2)

    def test_worker_returns_idle_when_no_operation_exists(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")

            worker_result = run_odoo_stable_operation_worker_once(
                record_store=store,
                control_plane_root_path=root,
                lease_owner="worker-a",
            )

            self.assertEqual(worker_result.status, "idle")
            self.assertEqual(worker_result.operation_kind, "")

    def test_reconcile_recovers_expired_leases_without_claiming_work(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    {
                        **_bootstrap_payload("bootstrap-expired-safe"),
                        "status": "running",
                        "phase": "created",
                        "started_at": "2026-05-17T00:01:00Z",
                        "lease_owner": "old-worker",
                        "lease_expires_at": "2026-05-17T00:02:00Z",
                        "heartbeat_at": "2026-05-17T00:01:00Z",
                        "attempt": 1,
                    }
                )
            )
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    _bootstrap_payload("bootstrap-pending")
                )
            )
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(
                    {
                        **_replacement_payload("replacement-expired-unsafe"),
                        "status": "running",
                        "phase": "apply",
                        "started_at": "2026-05-17T00:01:00Z",
                        "lease_owner": "old-worker",
                        "lease_expires_at": "2026-05-17T00:02:00Z",
                        "heartbeat_at": "2026-05-17T00:01:00Z",
                        "attempt": 1,
                    }
                )
            )

            result = reconcile_stale_odoo_stable_operation_records(
                record_store=store,
                now="2026-05-17T00:03:00Z",
            )

            self.assertEqual(result.reconciled_bootstrap_ids, ("bootstrap-expired-safe",))
            self.assertEqual(
                result.reconciled_replacement_ids,
                ("replacement-expired-unsafe",),
            )
            recovered = store.read_odoo_stable_bootstrap_operation_record("bootstrap-expired-safe")
            pending = store.read_odoo_stable_bootstrap_operation_record("bootstrap-pending")
            failed = store.read_odoo_stable_target_replacement_operation_record(
                "replacement-expired-unsafe"
            )
            self.assertEqual(recovered.status, "pending")
            self.assertEqual(recovered.phase, "created")
            self.assertEqual(recovered.lease_owner, "")
            self.assertEqual(recovered.started_at, "")
            self.assertEqual(pending.status, "pending")
            self.assertEqual(pending.lease_owner, "")
            self.assertEqual(failed.status, "fail")
            self.assertEqual(failed.phase, "failed")
            self.assertIn("unsafe to retry", failed.error_message)
            self.assertEqual(failed.lease_owner, "")

    def test_reconcile_rejects_invalid_max_attempts(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")

            with self.assertRaisesRegex(ValueError, "max_attempts must be positive"):
                reconcile_stale_odoo_stable_operation_records(
                    record_store=store,
                    max_attempts=0,
                )

    def test_worker_status_reports_pending_running_stalled_and_terminal_records(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    _bootstrap_payload("bootstrap-pending")
                )
            )
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    {
                        **_bootstrap_payload("bootstrap-running"),
                        "status": "running",
                        "phase": "running",
                        "lease_owner": "worker-a",
                        "lease_expires_at": "2026-05-17T00:10:00Z",
                        "heartbeat_at": "2026-05-17T00:04:00Z",
                        "attempt": 1,
                    }
                )
            )
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(
                    {
                        **_replacement_payload("replacement-stalled"),
                        "status": "running",
                        "phase": "running",
                        "lease_owner": "worker-b",
                        "lease_expires_at": "2026-05-17T00:03:00Z",
                        "heartbeat_at": "2026-05-17T00:01:00Z",
                        "attempt": 2,
                    }
                )
            )
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(
                    {
                        **_replacement_payload("replacement-blank-lease"),
                        "status": "running",
                        "phase": "running",
                        "lease_owner": "worker-c",
                        "heartbeat_at": "2026-05-17T00:02:00Z",
                        "attempt": 1,
                    }
                )
            )
            store.write_odoo_stable_target_replacement_operation_record(
                OdooStableTargetReplacementOperationRecord.model_validate(
                    {
                        **_replacement_payload("replacement-pass"),
                        "status": "pass",
                        "phase": "completed",
                        "finished_at": "2026-05-17T00:02:00Z",
                    }
                )
            )

            status = build_odoo_stable_operation_worker_status(
                record_store=store,
                now="2026-05-17T00:05:00Z",
            )

            self.assertEqual(status.status, "stalled")
            self.assertEqual(status.pending_count, 1)
            self.assertEqual(status.running_count, 3)
            self.assertEqual(status.stalled_count, 2)
            self.assertEqual(status.terminal_count, 1)
            self.assertEqual(status.counts_by_kind_status["odoo_stable_bootstrap:pending"], 1)
            self.assertEqual(status.counts_by_kind_status["odoo_stable_bootstrap:running"], 1)
            self.assertEqual(
                status.counts_by_kind_status["odoo_stable_target_replacement:running"],
                2,
            )
            self.assertEqual(
                status.counts_by_kind_status["odoo_stable_target_replacement:pass"],
                1,
            )
            stalled_operation = next(
                operation
                for operation in status.operations
                if operation.operation_id == "replacement-stalled"
            )
            self.assertTrue(stalled_operation.lease_expired)
            self.assertEqual(stalled_operation.heartbeat_age_seconds, 240)
            blank_lease_operation = next(
                operation
                for operation in status.operations
                if operation.operation_id == "replacement-blank-lease"
            )
            self.assertTrue(blank_lease_operation.lease_expired)

    def test_worker_loop_processes_until_idle_then_stops(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(_bootstrap_payload())
            )
            result = OdooStableBootstrapResult(
                product="odoo-tenant-cm",
                context="cm",
                instance="testing",
                deployment_record_id="deployment-cm-testing",
                bootstrap_status="pass",
                bootstrap_run_status="pass",
                readiness_status="pass",
                post_deploy_status="pass",
                health_status="pass",
                canonical_status="pass",
                logo_status="pass",
            )
            stop_event = Event()
            iteration_statuses: list[str] = []

            def _record_iteration(worker_result: object) -> None:
                status = getattr(worker_result, "status")
                iteration_statuses.append(status)
                if status == "idle":
                    stop_event.set()

            with patch(
                "control_plane.workflows.odoo_stable_operation_worker.execute_odoo_stable_bootstrap",
                return_value=result,
            ):
                loop_result = run_odoo_stable_operation_worker_loop(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    lease_seconds=300,
                    heartbeat_seconds=60,
                    poll_seconds=1,
                    stop_event=stop_event,
                    iteration_callback=_record_iteration,
                )

            self.assertEqual(loop_result.status, "stopped")
            self.assertEqual(loop_result.iterations, 2)
            self.assertEqual(loop_result.worked_count, 1)
            self.assertEqual(loop_result.idle_count, 1)
            self.assertEqual(iteration_statuses, ["worked", "idle"])

    def test_worker_loop_raises_after_consecutive_errors(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")

            with (
                patch(
                    "control_plane.workflows.odoo_stable_operation_worker.run_odoo_stable_operation_worker_once",
                    side_effect=RuntimeError("boom"),
                ),
                self.assertLogs(level=logging.ERROR),
                self.assertRaises(RuntimeError),
            ):
                run_odoo_stable_operation_worker_loop(
                    record_store=store,
                    control_plane_root_path=root,
                    lease_owner="worker-a",
                    error_backoff_seconds=1,
                    max_consecutive_errors=2,
                    max_iterations=3,
                )

    def test_cli_status_outputs_worker_status_json(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(_bootstrap_payload())
            )

            with patch("control_plane.cli_service._store", return_value=store):
                result = CliRunner().invoke(
                    main,
                    [
                        "service",
                        "odoo-workers",
                        "status",
                        "--database-url",
                        "sqlite+pysqlite:///:memory:",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["pending_count"], 1)
            self.assertEqual(payload["running_count"], 0)
            self.assertEqual(payload["operations"][0]["operation_id"], "operation-cm-testing")
            self.assertNotIn("request", payload["operations"][0])

    def test_cli_reconcile_outputs_reconciled_ids_json(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_odoo_stable_bootstrap_operation_record(
                OdooStableBootstrapOperationRecord.model_validate(
                    {
                        **_bootstrap_payload(),
                        "status": "running",
                        "phase": "created",
                        "started_at": "2026-05-17T00:01:00Z",
                        "lease_owner": "old-worker",
                        "lease_expires_at": "2000-01-01T00:00:00Z",
                        "heartbeat_at": "2000-01-01T00:00:00Z",
                        "attempt": 1,
                    }
                )
            )

            with patch("control_plane.cli_service._store", return_value=store):
                result = CliRunner().invoke(
                    main,
                    [
                        "service",
                        "odoo-workers",
                        "reconcile",
                        "--database-url",
                        "sqlite+pysqlite:///:memory:",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["status"], "ok")
            self.assertEqual(payload["reconciled_count"], 1)
            self.assertEqual(
                payload["reconciled_bootstrap_ids"],
                ["operation-cm-testing"],
            )
            self.assertEqual(payload["reconciled_replacement_ids"], [])

    def test_cli_run_once_outputs_worker_result_json(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")

            with (
                patch("control_plane.cli_service._store", return_value=store),
                patch(
                    "control_plane.cli_service._control_plane_root",
                    return_value=root,
                ),
            ):
                result = CliRunner().invoke(
                    main,
                    [
                        "service",
                        "odoo-workers",
                        "run-once",
                        "--database-url",
                        "sqlite+pysqlite:///:memory:",
                        "--lease-owner",
                        "worker-a",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["status"], "idle")
            self.assertEqual(payload["operation_kind"], "")

    def test_cli_run_delegates_to_worker_loop(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            loop_result = OdooStableOperationWorkerLoopResult(
                status="completed",
                iterations=1,
                worked_count=0,
                idle_count=1,
                error_count=0,
            )

            with (
                patch("control_plane.cli_service._store", return_value=store),
                patch(
                    "control_plane.cli_service._control_plane_root",
                    return_value=root,
                ),
                patch(
                    "control_plane.cli_service.run_odoo_stable_operation_worker_loop",
                    return_value=loop_result,
                ) as loop_mock,
            ):
                result = CliRunner().invoke(
                    main,
                    [
                        "service",
                        "odoo-workers",
                        "run",
                        "--database-url",
                        "sqlite+pysqlite:///:memory:",
                        "--lease-owner",
                        "worker-a",
                        "--poll-seconds",
                        "1",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            payload = json.loads(result.output)
            self.assertEqual(payload["status"], "completed")
            loop_mock.assert_called_once()
            self.assertEqual(loop_mock.call_args.kwargs["record_store"], store)
            self.assertEqual(loop_mock.call_args.kwargs["lease_owner"], "worker-a")
            self.assertEqual(loop_mock.call_args.kwargs["poll_seconds"], 1)


if __name__ == "__main__":
    unittest.main()
