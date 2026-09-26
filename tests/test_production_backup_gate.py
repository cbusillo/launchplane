from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.contracts.durable_operation_authorization import DurableOperationAuthorization
from control_plane.contracts.production_backup_gate import ProductionBackupGateWorkerResult
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_backup_gate import enqueue_production_backup_gate
from control_plane.workflows.verireel_prod_backup_gate_operation_worker import (
    run_verireel_prod_backup_gate_operation_worker_once,
)
from tests.support.durable_operations import (
    durable_operation_authorization_payload,
    durable_operation_policy_record,
)
from tests.test_production_backup_provider import _binding
from tests.test_production_backup_authority import _source_target


class ProductionBackupGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.store = PostgresRecordStore(
            database_url=f"sqlite+pysqlite:///{self.root / 'state.sqlite3'}"
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.binding = _binding()
        self.store.write_production_backup_target_record(self.binding.source_target)
        self.store.write_production_backup_target_record(self.binding.destination_target)
        self.store.write_production_backup_policy_record(self.binding.policy)
        payload = durable_operation_authorization_payload(
            action="production_backup_gate.execute",
            managed_rule_id="example-prod-backup",
            product="example-product",
            context="example-product",
            instances=("prod",),
        )
        self.authorization = DurableOperationAuthorization.model_validate(payload)
        self.store.seed_authz_policy_if_absent(durable_operation_policy_record(payload))

    def test_replay_and_conflicting_request_do_not_duplicate_work(self) -> None:
        first = enqueue_production_backup_gate(
            record_store=self.store,
            request=self.binding.request,
            authorization=self.authorization,
            operation_key="caller|key-1",
        )
        replay = enqueue_production_backup_gate(
            record_store=self.store,
            request=self.binding.request,
            authorization=self.authorization,
            operation_key="caller|key-1",
        )
        self.assertEqual(first, replay)
        with self.assertRaisesRegex(ValueError, "conflicts"):
            enqueue_production_backup_gate(
                record_store=self.store,
                request=self.binding.request.model_copy(
                    update={"backup_record_id": "another-backup"}
                ),
                authorization=self.authorization,
                operation_key="caller|key-1",
            )
        with self.assertRaisesRegex(ValueError, "conflicts"):
            enqueue_production_backup_gate(
                record_store=self.store,
                request=self.binding.request,
                authorization=self.authorization,
                operation_key="caller|another-key",
            )
        self.assertEqual(len(self.store.list_verireel_prod_backup_gate_operation_records()), 1)

    def test_shared_operation_persists_complete_provider_evidence(self) -> None:
        operation = enqueue_production_backup_gate(
            record_store=self.store,
            request=self.binding.request,
            authorization=self.authorization,
            operation_key="caller|capture",
        )
        evidence = {
            "snapshot_name": "example-20260926-100000-abcdef",
            "independent_backup_id": "ct/101/2026-09-26T10:00:00Z",
        }
        with (
            patch(
                "control_plane.workflows.production_backup_gate.enforce_worker_runtime_key_safety"
            ),
            patch(
                "control_plane.workflows.production_backup_gate.runtime_environments.resolve_runtime_environment_values",
                return_value={
                    "PRODUCTION_BACKUP_SSH_PRIVATE_KEY": "private",
                    "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS": "hosts",
                },
            ),
            patch(
                "control_plane.workflows.production_backup_gate.execute_production_backup_provider",
                return_value=ProductionBackupGateWorkerResult(
                    status="pass",
                    started_at="2026-09-26T10:00:00Z",
                    finished_at="2026-09-26T10:01:00Z",
                    evidence=evidence,
                ),
            ),
            patch(
                "control_plane.workflows.verireel_prod_backup_gate_operation_worker._run_delegated_worker"
            ) as legacy,
        ):
            result = run_verireel_prod_backup_gate_operation_worker_once(
                record_store=self.store,
                control_plane_root_path=self.root,
                lease_owner="test-worker",
            )
        self.assertTrue(result.terminal_write_committed)
        legacy.assert_not_called()
        persisted = self.store.read_verireel_prod_backup_gate_operation_record(
            operation.operation_id
        )
        self.assertEqual(persisted.product, "example-product")
        self.assertEqual(persisted.status, "pass")
        self.assertIsNotNone(persisted.result)
        assert persisted.result is not None
        self.assertEqual(persisted.result.evidence, evidence)
        backup = self.store.read_backup_gate_record(operation.backup_record_id)
        self.assertEqual(backup.source, "launchplane-production-backup-gate")
        self.assertEqual(backup.evidence, evidence)

    def test_retired_binding_blocks_worker_before_provider(self) -> None:
        operation = enqueue_production_backup_gate(
            record_store=self.store,
            request=self.binding.request,
            authorization=self.authorization,
            operation_key="caller|capture",
        )
        self.store.write_production_backup_target_record(
            _source_target(
                revision=2,
                status="retired",
                supersedes_record_id=self.binding.source_target.record_id,
            )
        )
        with patch(
            "control_plane.workflows.production_backup_gate.execute_production_backup_provider"
        ) as provider:
            result = run_verireel_prod_backup_gate_operation_worker_once(
                record_store=self.store,
                control_plane_root_path=self.root,
                lease_owner="test-worker",
            )
        self.assertTrue(result.terminal_write_committed)
        provider.assert_not_called()
        persisted = self.store.read_verireel_prod_backup_gate_operation_record(
            operation.operation_id
        )
        self.assertEqual(persisted.status, "fail")
        self.assertEqual(
            self.store.read_backup_gate_record(operation.backup_record_id).status, "fail"
        )


if __name__ == "__main__":
    unittest.main()
