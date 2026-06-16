import unittest
from subprocess import CompletedProcess, TimeoutExpired
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.runtime_key_safety_policy import (
    RuntimeKeySafetyPolicyRecord,
    RuntimeSecretClass,
    RuntimeSecretSafetyRule,
)
from control_plane.contracts.secret_record import SecretBinding
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
    ) -> VeriReelProdBackupGateOperationRecord:
        request = VeriReelProdBackupGateRequest(backup_record_id=backup_record_id)
        payload: dict[str, object] = {
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
        if status in {"pass", "fail"}:
            payload["finished_at"] = "2026-04-25T00:02:00Z"
            if status == "fail":
                payload["error_message"] = error_message
        return VeriReelProdBackupGateOperationRecord.model_validate(payload)

    def _write_prod_worker_secret_binding(
        self,
        store: PostgresRecordStore,
        *,
        secret_class: RuntimeSecretClass = "prod_only",
    ) -> None:
        binding_key = "VERIREEL_PROD_PROXMOX_SSH_PRIVATE_KEY"
        store.write_secret_binding(
            SecretBinding(
                binding_id="secret-verireel-prod-proxmox-ssh-private-key-binding",
                secret_id="secret-verireel-prod-proxmox-ssh-private-key",
                integration="runtime_environment",
                binding_key=binding_key,
                context="verireel",
                instance="prod",
                status="configured",
                created_at="2026-05-05T22:30:00Z",
                updated_at="2026-05-05T22:30:00Z",
            )
        )
        store.write_runtime_key_safety_policy_record(
            RuntimeKeySafetyPolicyRecord(
                record_id="runtime-key-safety-policy-test",
                status="active",
                source="test",
                updated_at="2026-05-05T22:30:00Z",
                rules=(
                    RuntimeSecretSafetyRule(
                        binding_key=binding_key,
                        secret_class=secret_class,
                        allowed_contexts=("verireel",),
                        allowed_instances=("prod",),
                    ),
                ),
            )
        )

    def test_run_delegated_worker_prefers_runtime_environment_values(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = self._sqlite_database_url(root)
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                for record in control_plane_runtime_environments.build_runtime_environment_records_from_definition(
                    control_plane_runtime_environments.RuntimeEnvironmentDefinition(
                        schema_version=1,
                        shared_env={},
                        contexts={
                            "verireel": control_plane_runtime_environments.RuntimeEnvironmentContextDefinition(
                                shared_env={},
                                instances={
                                    "prod": control_plane_runtime_environments.RuntimeEnvironmentInstanceDefinition(
                                        env={
                                            "LAUNCHPLANE_VERIREEL_PROD_BACKUP_GATE_WORKER_COMMAND": "uv run python -m control_plane.workflows.verireel_prod_backup_gate_worker",
                                            "VERIREEL_PROD_PROXMOX_HOST": "proxmox.runtime.example",
                                            "VERIREEL_PROD_PROXMOX_USER": "runtime-user",
                                            "VERIREEL_PROD_PROXMOX_SSH_PRIVATE_KEY": "runtime-private-key",
                                            "VERIREEL_PROD_PROXMOX_SSH_KNOWN_HOSTS": "runtime-known-hosts",
                                            "VERIREEL_PROD_CT_ID": "211",
                                            "VERIREEL_PROD_BACKUP_STORAGE": "pbs-runtime",
                                            "VERIREEL_PROD_GATE_HEALTH_TIMEOUT_MS": "25000",
                                        }
                                    )
                                },
                            )
                        },
                    ),
                    updated_at="2026-04-25T00:00:00Z",
                    source_label="test",
                ):
                    store.write_runtime_environment_record(record)
            finally:
                store.close()

            captured: dict[str, object] = {}

            def _fake_run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
                captured["command"] = command
                captured["env"] = kwargs["env"]
                return CompletedProcess(
                    args=command,
                    returncode=0,
                    stdout=(
                        '{"schema_version":1,"status":"pass","snapshot_name":"ver-predeploy-20260425-001500",'
                        '"started_at":"2026-04-25T00:15:00Z","finished_at":"2026-04-25T00:16:00Z",'
                        '"detail":"Backup completed.","evidence":{"snapshot_name":"ver-predeploy-20260425-001500"}}\n'
                    ),
                    stderr="",
                )

            with (
                patch(
                    "control_plane.workflows.verireel_prod_backup_gate.subprocess.run",
                    side_effect=_fake_run,
                ),
                patch.dict(
                    "os.environ",
                    {
                        "LAUNCHPLANE_DATABASE_URL": database_url,
                        "LAUNCHPLANE_VERIREEL_PROD_BACKUP_GATE_WORKER_COMMAND": "legacy worker",
                        "VERIREEL_PROD_PROXMOX_HOST": "legacy.example",
                        "VERIREEL_PROD_PROXMOX_USER": "legacy-user",
                        "VERIREEL_PROD_CT_ID": "999",
                    },
                    clear=True,
                ),
            ):
                result = _run_delegated_worker(
                    control_plane_root=root,
                    request=VeriReelProdBackupGateWorkerRequest(
                        context="verireel",
                        instance="prod",
                        backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    ),
                )

        self.assertEqual(result.status, "pass")
        self.assertEqual(
            captured["command"],
            [
                "uv",
                "run",
                "python",
                "-m",
                "control_plane.workflows.verireel_prod_backup_gate_worker",
            ],
        )
        worker_env = captured["env"]
        assert isinstance(worker_env, dict)
        self.assertEqual(worker_env["VERIREEL_PROD_PROXMOX_HOST"], "proxmox.runtime.example")
        self.assertEqual(worker_env["VERIREEL_PROD_PROXMOX_USER"], "runtime-user")
        self.assertEqual(worker_env["VERIREEL_PROD_CT_ID"], "211")
        self.assertEqual(worker_env["VERIREEL_PROD_PROXMOX_SSH_PRIVATE_KEY"], "runtime-private-key")
        self.assertEqual(worker_env["VERIREEL_PROD_PROXMOX_SSH_KNOWN_HOSTS"], "runtime-known-hosts")
        self.assertEqual(worker_env["VERIREEL_PROD_BACKUP_STORAGE"], "pbs-runtime")
        self.assertEqual(worker_env["VERIREEL_PROD_GATE_HEALTH_TIMEOUT_MS"], "25000")

    def test_run_delegated_worker_blocks_unsafe_managed_runtime_secret(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            database_url = self._sqlite_database_url(root)
            store = PostgresRecordStore(database_url=database_url)
            store.ensure_schema()
            try:
                self._write_prod_worker_secret_binding(store, secret_class="testing")
            finally:
                store.close()

            with (
                patch.dict("os.environ", {"LAUNCHPLANE_DATABASE_URL": database_url}, clear=True),
                patch("control_plane.workflows.verireel_prod_backup_gate.subprocess.run") as run,
            ):
                with self.assertRaisesRegex(click.ClickException, "key-safety gate failed"):
                    _run_delegated_worker(
                        control_plane_root=root,
                        request=VeriReelProdBackupGateWorkerRequest(
                            context="verireel",
                            instance="prod",
                            backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                        ),
                    )

        run.assert_not_called()

    def test_run_delegated_worker_requires_database_for_runtime_key_safety(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            with (
                patch.dict("os.environ", {}, clear=True),
                patch("control_plane.workflows.verireel_prod_backup_gate.subprocess.run") as run,
            ):
                with self.assertRaisesRegex(click.ClickException, "LAUNCHPLANE_DATABASE_URL"):
                    _run_delegated_worker(
                        control_plane_root=root,
                        request=VeriReelProdBackupGateWorkerRequest(
                            context="verireel",
                            instance="prod",
                            backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                        ),
                    )

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
                now="2026-04-25T00:00:00Z",
            )

            with self.assertRaisesRegex(click.ClickException, "conflicts"):
                enqueue_verireel_prod_backup_gate(
                    record_store=record_store,
                    request=VeriReelProdBackupGateRequest(
                        backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                        timeout_seconds=42,
                    ),
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
                operation: VeriReelProdBackupGateOperationRecord,
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
                        now="2026-04-25T00:01:00Z",
                    )

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
                now="2026-04-25T00:03:00Z",
            )

            self.assertEqual(result.backup_status, "fail")
            self.assertEqual(result.error_message, "lease expired in backup_gate")
            backup_record = record_store.read_backup_gate_record(request.backup_record_id)
            self.assertEqual(backup_record.status, "fail")
            self.assertEqual(
                backup_record.evidence["error_message"], "lease expired in backup_gate"
            )

    def test_verireel_operation_worker_claims_and_executes_pending_operation(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            record_store = self._record_store(root)
            record_store.write_verireel_prod_backup_gate_operation_record(
                self._operation_record()
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
            record_store.write_verireel_prod_backup_gate_operation_record(
                self._operation_record()
            )

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
            record_store.write_verireel_prod_backup_gate_operation_record(
                self._operation_record()
            )

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
            failed_backup_gate = record_store.read_backup_gate_record(
                "backup-gate-side-effect"
            )
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
            record_store.write_verireel_prod_backup_gate_operation_record(
                self._operation_record()
            )
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

    def test_run_delegated_worker_reports_timeout_as_click_exception(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)

            with (
                patch(
                    "control_plane.workflows.verireel_prod_backup_gate._worker_environment",
                    return_value={"LAUNCHPLANE_VERIREEL_PROD_BACKUP_GATE_WORKER_COMMAND": "worker"},
                ),
                patch(
                    "control_plane.workflows.verireel_prod_backup_gate.subprocess.run",
                    side_effect=TimeoutExpired(cmd=["worker"], timeout=12),
                ),
            ):
                with self.assertRaisesRegex(
                    click.ClickException,
                    "VeriReel prod backup gate worker timed out after 12 seconds",
                ):
                    _run_delegated_worker(
                        control_plane_root=root,
                        request=VeriReelProdBackupGateWorkerRequest(
                            context="verireel",
                            instance="prod",
                            backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                            timeout_seconds=12,
                        ),
                    )

    def test_worker_uses_explicit_ssh_material_for_remote_proxmox_commands(self) -> None:
        captured_commands: list[list[str]] = []

        def _fake_run(command: list[str], **kwargs: object) -> CompletedProcess[str]:
            captured_commands.append(command)
            if len(captured_commands) == 1:
                identity_file = Path(command[command.index("-i") + 1])
                known_hosts_option = next(
                    item for item in command if item.startswith("UserKnownHostsFile=")
                )
                known_hosts_file = Path(known_hosts_option.partition("=")[2])
                self.assertTrue(identity_file.exists())
                self.assertTrue(known_hosts_file.exists())
                self.assertEqual(identity_file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(known_hosts_file.stat().st_mode & 0o777, 0o600)
                self.assertEqual(identity_file.read_text(encoding="utf-8"), "test-private-key\n")
                self.assertEqual(
                    known_hosts_file.read_text(encoding="utf-8"),
                    "proxmox.runtime.example ssh-ed25519 test-key\n",
                )
                return CompletedProcess(args=command, returncode=0, stdout="", stderr="")
            return CompletedProcess(args=command, returncode=0, stdout="", stderr="")

        with (
            patch.dict(
                "os.environ",
                {
                    "VERIREEL_PROD_PROXMOX_HOST": "proxmox.runtime.example",
                    "VERIREEL_PROD_PROXMOX_USER": "runtime-user",
                    "VERIREEL_PROD_PROXMOX_SSH_PRIVATE_KEY": "test-private-key",
                    "VERIREEL_PROD_PROXMOX_SSH_KNOWN_HOSTS": "proxmox.runtime.example ssh-ed25519 test-key",
                    "VERIREEL_PROD_CT_ID": "211",
                    "VERIREEL_PROD_BACKUP_MODE": "snapshot",
                    "VERIREEL_TESTING_BASE_URL": "",
                    "VERIREEL_PROD_OPERATOR_BASE_URL": "",
                },
                clear=True,
            ),
            patch(
                "control_plane.workflows.verireel_prod_backup_gate_worker.subprocess.run",
                side_effect=_fake_run,
            ),
        ):
            result = verireel_prod_backup_gate_worker.execute_worker(
                VeriReelProdBackupGateWorkerRequest(
                    context="verireel",
                    instance="prod",
                    backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                )
            )

        self.assertEqual(result.status, "pass")
        self.assertTrue(captured_commands)
        command = captured_commands[0]
        self.assertEqual(command[0], "ssh")
        self.assertIn("runtime-user@proxmox.runtime.example", command)
        remote_command_start = command.index("runtime-user@proxmox.runtime.example") + 1
        self.assertEqual(
            command[remote_command_start : remote_command_start + 3], ["pct", "snapshot", "211"]
        )
        self.assertRegex(
            command[remote_command_start + 3],
            r"^ver-predeploy-\d{8}-\d{6}-[0-9a-f]{6}$",
        )

    def test_worker_requires_explicit_ssh_material_for_remote_proxmox_commands(self) -> None:
        with patch.dict(
            "os.environ",
            {
                "VERIREEL_PROD_PROXMOX_HOST": "proxmox.runtime.example",
                "VERIREEL_PROD_PROXMOX_USER": "runtime-user",
                "VERIREEL_PROD_CT_ID": "211",
                "VERIREEL_PROD_BACKUP_MODE": "snapshot",
            },
            clear=True,
        ):
            with self.assertRaisesRegex(
                click.ClickException, "VERIREEL_PROD_PROXMOX_SSH_PRIVATE_KEY"
            ):
                verireel_prod_backup_gate_worker.execute_worker(
                    VeriReelProdBackupGateWorkerRequest(
                        context="verireel",
                        instance="prod",
                        backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1",
                    )
                )

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
