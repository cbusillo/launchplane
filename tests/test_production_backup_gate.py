from concurrent.futures import ThreadPoolExecutor, TimeoutError as FutureTimeoutError
from datetime import datetime, timedelta, timezone
import os
import json
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
import unittest
from unittest.mock import patch

from sqlalchemy import create_engine, text
from sqlalchemy.exc import SQLAlchemyError
import click

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.durable_operation_authorization import DurableOperationAuthorization
from control_plane.contracts.production_backup_gate import ProductionBackupGateWorkerResult
from control_plane.contracts.verireel_prod_backup_gate import VeriReelProdBackupGateRequest
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.production_backup_gate import (
    enqueue_production_backup_gate,
    execute_shared_production_backup,
)
from control_plane.workflows.production_backup_provider import ProductionBackupProviderError
from control_plane.workflows.verireel_prod_backup_gate import enqueue_verireel_prod_backup_gate
from control_plane.workflows.verireel_prod_backup_gate_operation_worker import (
    run_verireel_prod_backup_gate_operation_worker_once,
)
from tests.support.durable_operations import (
    durable_operation_authorization_payload,
    durable_operation_policy_record,
)
from tests.test_production_backup_provider import BackupHost, _binding, setup_memory_files
from tests.test_production_backup_authority import _source_target
from tests.test_postgres_integration import _store_for_fresh_head_database


class ProductionBackupGateTests(unittest.TestCase):
    def setUp(self) -> None:
        setup_memory_files(self)
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
        self.store.write_production_backup_target_record(
            _source_target(
                revision=2,
                status="retired",
                supersedes_record_id=self.binding.source_target.record_id,
            )
        )
        self.assertEqual(
            enqueue_production_backup_gate(
                record_store=self.store,
                request=self.binding.request,
                authorization=self.authorization,
                operation_key="caller|key-1",
            ),
            first,
        )

    def test_worker_rechecks_revoked_authority_after_snapshot(self) -> None:
        operation = enqueue_production_backup_gate(
            record_store=self.store,
            request=self.binding.request,
            authorization=self.authorization,
            operation_key="caller|revocation",
        )
        current_policy = durable_operation_policy_record(self.authorization.model_dump(mode="json"))

        def revoke() -> None:
            nonlocal current_policy
            current_policy = durable_operation_policy_record(revision=43)

        host = BackupHost(after_snapshot=revoke)
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
                "control_plane.workflows.verireel_prod_backup_gate_operation_worker.read_active_authz_policy_record",
                side_effect=lambda _store: current_policy,
            ),
            patch(
                "control_plane.workflows.production_backup_provider.subprocess.run",
                side_effect=host.run,
            ),
        ):
            result = run_verireel_prod_backup_gate_operation_worker_once(
                record_store=self.store,
                control_plane_root_path=self.root,
                lease_owner="test-worker",
            )
        self.assertTrue(result.terminal_write_committed)
        persisted = self.store.read_verireel_prod_backup_gate_operation_record(
            operation.operation_id
        )
        self.assertEqual(persisted.status, "fail")
        self.assertEqual(persisted.error_code, "operation_authorization_revoked")
        self.assertEqual(persisted.progress_evidence["snapshot_name"], host.snapshot)
        self.assertEqual(
            self.store.read_backup_gate_record(operation.backup_record_id).evidence[
                "snapshot_name"
            ],
            host.snapshot,
        )
        self.assertTrue(all(command[0] != "vzdump" for command in host.commands))

    def test_expired_partial_capture_keeps_evidence_and_cannot_reuse_record_id(self) -> None:
        operation = enqueue_production_backup_gate(
            record_store=self.store,
            request=self.binding.request,
            authorization=self.authorization,
            operation_key="caller|partial",
        )
        self.store.claim_next_verireel_prod_backup_gate_operation_record(
            lease_owner="lost-worker",
            lease_expires_at="2026-09-26T01:00:00Z",
            claimed_at="2026-09-26T00:00:00Z",
        )
        self.store.mark_verireel_prod_backup_gate_operation_phase(
            operation_id=operation.operation_id,
            lease_owner="lost-worker",
            phase="backup_gate",
            updated_at="2026-09-26T00:01:00Z",
            progress_evidence={
                "snapshot_name": "recorded-snapshot",
                "provider_stage": "independent_backup",
            },
        )
        self.store.recover_expired_verireel_prod_backup_gate_operation_records(
            now="2026-09-26T02:00:00Z",
            safe_phases=("created", "running"),
            max_attempts=3,
        )
        recovered = self.store.read_verireel_prod_backup_gate_operation_record(
            operation.operation_id
        )
        self.assertEqual(recovered.status, "fail")
        self.assertEqual(recovered.error_code, "backup_effect_outcome_unknown")
        evidence = self.store.read_backup_gate_record(operation.backup_record_id)
        self.assertEqual(evidence.source, "launchplane-production-backup-gate")
        self.assertEqual(evidence.evidence["snapshot_name"], "recorded-snapshot")
        with self.assertRaisesRegex(ValueError, "conflicts"):
            enqueue_production_backup_gate(
                record_store=self.store,
                request=self.binding.request,
                authorization=self.authorization,
                operation_key="caller|duplicate",
            )
        self.assertEqual(self.store.read_backup_gate_record(operation.backup_record_id), evidence)
        with self.assertRaisesRegex(click.ClickException, "another backup scope or provider"):
            enqueue_verireel_prod_backup_gate(
                record_store=self.store,
                request=VeriReelProdBackupGateRequest(backup_record_id=operation.backup_record_id),
                authorization=self.authorization,
            )
        self.assertEqual(self.store.read_backup_gate_record(operation.backup_record_id), evidence)

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


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_POSTGRES_URL"), "Real PostgreSQL test URL is required"
)
class ProductionBackupGatePostgresTests(unittest.TestCase):
    def test_source_lock_checkpoint_detects_connection_loss(self) -> None:
        with _store_for_fresh_head_database() as store:
            engine = create_engine(store.database_url)
            try:
                with store.production_backup_source_lock("connection-loss-test") as check_lock:
                    assert check_lock is not None
                    check_lock()
                    with engine.begin() as connection:
                        holders = connection.scalars(
                            text(
                                "select distinct pid from pg_locks where locktype = 'advisory' "
                                "and database = (select oid from pg_database where datname = current_database())"
                            )
                        ).all()
                        self.assertEqual(len(holders), 1)
                        self.assertTrue(
                            connection.scalar(
                                text("select pg_terminate_backend(:pid)"), {"pid": holders[0]}
                            )
                        )
                    with self.assertRaises(SQLAlchemyError):
                        check_lock()
            finally:
                engine.dispose()

    def test_same_source_is_fenced_before_host_effects_and_other_sources_are_independent(
        self,
    ) -> None:
        binding = _binding()
        source = binding.source_target.destination
        source_key = json.dumps([source.host.lower(), "lxc", "101"])
        with _store_for_fresh_head_database() as store:
            with store.production_backup_source_lock(source_key) as acquired:
                self.assertTrue(acquired)
                with patch(
                    "control_plane.workflows.production_backup_gate.enforce_worker_runtime_key_safety"
                ) as runtime:
                    with self.assertRaisesRegex(
                        ProductionBackupProviderError, "backup_source_busy"
                    ):
                        execute_shared_production_backup(
                            record_store=store,
                            binding=binding,
                            control_plane_root=Path("."),
                            checkpoint=lambda _phase: None,
                            record_progress=lambda _evidence: None,
                        )
                runtime.assert_not_called()
                with store.production_backup_source_lock(
                    json.dumps([source.host.lower(), "lxc", "102"])
                ) as other:
                    self.assertTrue(other)
            with store.production_backup_source_lock(source_key) as acquired_after_completion:
                self.assertTrue(acquired_after_completion)

    def test_competing_record_id_cannot_overwrite_capture_finishing_during_enqueue(self) -> None:
        with _store_for_fresh_head_database() as store:
            binding = _binding()
            store.write_production_backup_target_record(binding.source_target)
            store.write_production_backup_target_record(binding.destination_target)
            store.write_production_backup_policy_record(binding.policy)
            authorization = DurableOperationAuthorization.model_validate(
                durable_operation_authorization_payload(
                    action="production_backup_gate.execute",
                    managed_rule_id="example-prod-backup",
                    product="example-product",
                    context="example-product",
                    instances=("prod",),
                )
            )
            operation = enqueue_production_backup_gate(
                record_store=store,
                request=binding.request,
                authorization=authorization,
                operation_key="caller|original",
            )
            now = datetime.now(timezone.utc)
            claimed = store.claim_next_verireel_prod_backup_gate_operation_record(
                lease_owner="original-worker",
                claimed_at=now.isoformat(),
                lease_expires_at=(now + timedelta(minutes=5)).isoformat(),
            )
            assert claimed is not None
            original_evidence = BackupGateRecord(
                record_id=operation.backup_record_id,
                context=operation.context,
                instance=operation.instance,
                created_at=now.isoformat(),
                source="launchplane-production-backup-gate",
                required=True,
                status="fail",
                evidence={"snapshot_name": "original-snapshot"},
            )
            started = Event()

            def competing_enqueue() -> None:
                started.set()
                enqueue_production_backup_gate(
                    record_store=store,
                    request=binding.request,
                    authorization=authorization,
                    operation_key="caller|competing",
                )

            engine = create_engine(store.database_url)
            try:
                with ThreadPoolExecutor(max_workers=1) as workers:
                    with engine.begin() as connection:
                        connection.execute(
                            text("select pg_advisory_xact_lock(hashtextextended(:lock_name, 0))"),
                            {
                                "lock_name": f"launchplane:backup-record:{operation.backup_record_id}"
                            },
                        )
                        future = workers.submit(competing_enqueue)
                        self.assertTrue(started.wait(timeout=5))
                        with self.assertRaises(FutureTimeoutError):
                            future.result(timeout=0.2)
                        self.assertTrue(
                            store.complete_verireel_prod_backup_gate_operation_with_backup_gate_record(
                                operation_record=claimed.model_copy(
                                    update={
                                        "status": "fail",
                                        "phase": "failed",
                                        "finished_at": now.isoformat(),
                                        "error_message": "Partial capture retained",
                                    }
                                ),
                                backup_gate_record=original_evidence,
                                lease_owner="original-worker",
                            )
                        )
                    with self.assertRaisesRegex(ValueError, "conflicts"):
                        future.result(timeout=5)
            finally:
                engine.dispose()
            self.assertEqual(len(store.list_verireel_prod_backup_gate_operation_records()), 1)
            self.assertEqual(
                store.read_backup_gate_record(operation.backup_record_id), original_evidence
            )


if __name__ == "__main__":
    unittest.main()
