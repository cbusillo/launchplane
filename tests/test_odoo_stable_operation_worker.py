import logging
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

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


if __name__ == "__main__":
    unittest.main()
