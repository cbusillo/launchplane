import json
from pathlib import Path
import subprocess
from typing import Callable
import unittest
from unittest.mock import patch

from control_plane.contracts.production_backup_gate import (
    ProductionBackupGateRequest,
    ProductionBackupGateWorkerRequest,
)
from control_plane.workflows.production_backup_provider import (
    ProductionBackupProviderError,
    execute_production_backup_provider,
)
from tests.test_production_backup_authority import _destination_target, _policy, _source_target


def _binding() -> ProductionBackupGateWorkerRequest:
    return ProductionBackupGateWorkerRequest(
        request=ProductionBackupGateRequest(
            product="example-product",
            context="example-product",
            instance="prod",
            promotion_action="verireel_prod_promotion.execute",
            backup_record_id="backup-example",
        ),
        policy=_policy(),
        source_target=_source_target(),
        destination_target=_destination_target(),
    )


class BackupHost:
    def __init__(self, *, after_snapshot: Callable[[], None] | None = None) -> None:
        self.commands: list[list[str]] = []
        self.snapshot = ""
        self.old_snapshots: list[str] = []
        self.after_snapshot = after_snapshot
        self.fail_retention = False

    def run(self, command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
        args = command[command.index("--") + 2 :]
        self.commands.append(args)
        if args == ["launchplane-backup-boundary"]:
            output = json.dumps(
                {
                    "schema_version": 1,
                    "guest_kind": "lxc",
                    "guest_id": "101",
                    "storage_id": "pbs-production",
                    "snapshot_prefix": "example-predeploy",
                    "restore_allowed": False,
                }
            )
        elif args == ["pvesm", "status", "--storage", "pbs-production"]:
            output = "pbs-production pbs active 100 10 90 10%"
        elif args[:3] == ["pct", "snapshot", "101"]:
            self.snapshot = args[3]
            if self.after_snapshot is not None:
                self.after_snapshot()
            output = ""
        elif args == ["pct", "listsnapshot", "101"]:
            output = "\n".join(
                f"`-> {name} 2026-09-26" for name in [self.snapshot, *self.old_snapshots]
            )
        elif args == ["vzdump", "101", "--mode", "snapshot", "--storage", "pbs-production"]:
            output = "INFO: creating Proxmox Backup Server archive 'ct/101/2026-09-26T10:00:00Z'"
        elif args == ["pvesm", "list", "pbs-production", "--vmid", "101", "--content", "backup"]:
            output = "pbs-production:backup/ct/101/2026-09-26T10:00:00Z pbs backup 1000 101"
        elif args[:3] == ["pct", "delsnapshot", "101"]:
            if self.fail_retention:
                return subprocess.CompletedProcess(command, 1, stdout="", stderr="private-error")
            self.old_snapshots.remove(args[3])
            output = ""
        else:
            raise AssertionError(f"Unexpected provider command: {args}")
        return subprocess.CompletedProcess(command, 0, stdout=output, stderr="SSH warning banner")


class ProductionBackupProviderTests(unittest.TestCase):
    def test_capture_rejects_restore_capability_and_invalid_snapshot_prefixes(self) -> None:
        boundary = {
            "schema_version": 1,
            "guest_kind": "lxc",
            "guest_id": "101",
            "storage_id": "pbs-production",
            "snapshot_prefix": "example-predeploy",
            "restore_allowed": True,
        }
        with patch(
            "control_plane.workflows.production_backup_provider.subprocess.run",
            return_value=subprocess.CompletedProcess([], 0, stdout=json.dumps(boundary), stderr=""),
        ) as run:
            result = execute_production_backup_provider(
                _binding(), ssh_private_key="key", ssh_known_hosts="hosts"
            )
        self.assertEqual(result.error_code, "backup_host_binding_mismatch")
        self.assertEqual(run.call_count, 1)
        for prefix in ("9example", "example.dot", "a" * 18):
            with self.subTest(prefix=prefix):
                binding = _binding()
                binding.policy.fast_snapshot.snapshot_prefix = prefix
                with patch(
                    "control_plane.workflows.production_backup_provider.subprocess.run"
                ) as run:
                    result = execute_production_backup_provider(
                        binding, ssh_private_key="key", ssh_known_hosts="hosts"
                    )
                self.assertEqual(result.error_code, "snapshot_prefix_invalid")
                run.assert_not_called()

    def test_retention_failure_keeps_verified_capture(self) -> None:
        host = BackupHost()
        host.old_snapshots = [f"example-predeploy-2026090{i}-100000-abcdef" for i in range(1, 7)]
        host.fail_retention = True
        with patch(
            "control_plane.workflows.production_backup_provider.subprocess.run",
            side_effect=host.run,
        ):
            result = execute_production_backup_provider(
                _binding(), ssh_private_key="private-material", ssh_known_hosts="host-material"
            )
        self.assertEqual(result.status, "pass")
        self.assertEqual(result.evidence["capture_status"], "verified")
        self.assertEqual(result.evidence["retention_status"], "fail")
        self.assertEqual(
            result.evidence["retention_error_code"], "snapshot_retention_command_failed"
        )
        self.assertNotIn("private-", result.model_dump_json())
        self.assertNotIn("pbs-production", result.model_dump_json())

    def test_revocation_after_snapshot_preserves_partial_evidence_and_stops_backup(self) -> None:
        host = BackupHost()
        progress: list[dict[str, str]] = []

        def checkpoint(phase: str) -> None:
            if phase == "independent_backup":
                raise ProductionBackupProviderError("operation_authorization_revoked")

        with patch(
            "control_plane.workflows.production_backup_provider.subprocess.run",
            side_effect=host.run,
        ):
            result = execute_production_backup_provider(
                _binding(),
                ssh_private_key="private-material",
                ssh_known_hosts="host-material",
                checkpoint=checkpoint,
                record_progress=progress.append,
            )
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.error_code, "operation_authorization_revoked")
        self.assertEqual(result.evidence["snapshot_name"], host.snapshot)
        self.assertIn("snapshot_finished_at", progress[-1])
        self.assertTrue(all(command[0] != "vzdump" for command in host.commands))

    def test_capture_binds_both_operations_and_removes_ssh_material(self) -> None:
        commands: list[list[str]] = []
        identity_paths: list[Path] = []
        snapshot = ""

        def run(command: list[str], **_kwargs: object) -> subprocess.CompletedProcess[str]:
            nonlocal snapshot
            identity_path = Path(command[command.index("-i") + 1])
            identity_paths.append(identity_path)
            self.assertEqual(identity_path.stat().st_mode & 0o777, 0o600)
            args = command[command.index("--") + 2 :]
            commands.append(args)
            if args == ["launchplane-backup-boundary"]:
                output = json.dumps(
                    {
                        "schema_version": 1,
                        "guest_kind": "lxc",
                        "guest_id": "101",
                        "storage_id": "pbs-production",
                        "snapshot_prefix": "example-predeploy",
                        "restore_allowed": False,
                    }
                )
            elif args == ["pvesm", "status", "--storage", "pbs-production"]:
                output = "Name Type Status Total Used Available %\npbs-production pbs active 100 10 90 10%"
            elif args[:2] == ["pct", "snapshot"]:
                self.assertEqual(args[2], "101")
                snapshot = args[3]
                output = ""
            elif args == ["pct", "listsnapshot", "101"]:
                output = f"`-> {snapshot} 2026-09-26\n `-> current You are here!"
            elif args == ["vzdump", "101", "--mode", "snapshot", "--storage", "pbs-production"]:
                output = (
                    "INFO: creating Proxmox Backup Server archive 'ct/101/2026-09-26T10:00:00Z'"
                )
            elif args == [
                "pvesm",
                "list",
                "pbs-production",
                "--vmid",
                "101",
                "--content",
                "backup",
            ]:
                output = "Volid Format Type Size VMID\npbs-production:backup/ct/101/2026-09-26T10:00:00Z pbs backup 1000 101"
            else:
                raise AssertionError(f"Unexpected provider operation: {args}")
            return subprocess.CompletedProcess(command, 0, stdout=output, stderr="")

        with patch(
            "control_plane.workflows.production_backup_provider.subprocess.run", side_effect=run
        ):
            result = execute_production_backup_provider(
                _binding(), ssh_private_key="private-material", ssh_known_hosts="host-material"
            )
        self.assertEqual(result.status, "pass", result.error_code)
        self.assertEqual(result.evidence["snapshot_name"], snapshot)
        self.assertEqual(result.evidence["independent_backup_id"], "ct/101/2026-09-26T10:00:00Z")
        self.assertEqual(result.evidence["policy_digest"], _policy().policy_digest)
        self.assertEqual(result.evidence["source_target_record_id"], _source_target().record_id)
        self.assertEqual(
            result.evidence["destination_target_record_id"], _destination_target().record_id
        )
        self.assertTrue(all(not path.exists() for path in identity_paths))
        self.assertNotIn("private-material", result.model_dump_json())
        self.assertNotIn("host-material", result.model_dump_json())
        self.assertTrue(commands)

    def test_host_binding_drift_stops_before_any_capture(self) -> None:
        with patch(
            "control_plane.workflows.production_backup_provider.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [],
                0,
                stdout=json.dumps(
                    {
                        "schema_version": 1,
                        "guest_kind": "lxc",
                        "guest_id": "101",
                        "storage_id": "renamed-destination",
                        "snapshot_prefix": "example-predeploy",
                        "restore_allowed": False,
                    }
                ),
                stderr="",
            ),
        ) as run:
            result = execute_production_backup_provider(
                _binding(), ssh_private_key="private-material", ssh_known_hosts="host-material"
            )
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.error_code, "backup_host_binding_mismatch")
        self.assertEqual(run.call_count, 1)
        self.assertNotIn("snapshot_name", result.evidence)

    def test_provider_failure_does_not_expose_output(self) -> None:
        with patch(
            "control_plane.workflows.production_backup_provider.subprocess.run",
            return_value=subprocess.CompletedProcess(
                [], 126, stdout="private-output", stderr="private-error"
            ),
        ):
            result = execute_production_backup_provider(
                _binding(), ssh_private_key="private-material", ssh_known_hosts="host-material"
            )
        self.assertEqual(result.status, "fail")
        self.assertEqual(result.error_code, "preflight_command_failed")
        self.assertNotIn("private-", result.model_dump_json())


if __name__ == "__main__":
    unittest.main()
