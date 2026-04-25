import unittest
from subprocess import CompletedProcess, TimeoutExpired
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

import click

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows import verireel_prod_backup_gate_worker
from control_plane.workflows.verireel_prod_backup_gate import (
    DEFAULT_TIMEOUT_SECONDS,
    VeriReelProdBackupGateRequest,
    VeriReelProdBackupGateWorkerRequest,
    VeriReelProdBackupGateWorkerResult,
    _run_delegated_worker,
    execute_verireel_prod_backup_gate,
)


class VeriReelProdBackupGateWorkflowTests(unittest.TestCase):
    def _sqlite_database_url(self, root: Path) -> str:
        return f"sqlite+pysqlite:///{root / 'launchplane.sqlite3'}"

    def _record_store(self, root: Path) -> FilesystemRecordStore:
        return FilesystemRecordStore(root / "state")

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
            ["uv", "run", "python", "-m", "control_plane.workflows.verireel_prod_backup_gate_worker"],
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

    def test_prod_backup_gate_default_timeout_allows_longer_vzdump_backup(self) -> None:
        self.assertEqual(DEFAULT_TIMEOUT_SECONDS, 900)

        request = VeriReelProdBackupGateRequest(
            backup_record_id="backup-gate-verireel-prod-run-12345-attempt-1"
        )

        self.assertEqual(request.timeout_seconds, 900)

    def test_run_delegated_worker_reports_timeout_as_click_exception(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)

            with (
                patch(
                    "control_plane.workflows.verireel_prod_backup_gate._worker_environment",
                    return_value={
                        "LAUNCHPLANE_VERIREEL_PROD_BACKUP_GATE_WORKER_COMMAND": "worker"
                    },
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
        self.assertEqual(command[remote_command_start:remote_command_start + 3], ["pct", "snapshot", "211"])
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
            self.assertEqual(record.evidence, {})
