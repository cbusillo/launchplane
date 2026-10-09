import json
import os
from pathlib import Path
import subprocess
import unittest


class ProxmoxBackupBoundaryTests(unittest.TestCase):
    def test_metadata_capture_executes_only_the_bound_lxc_command(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts/proxmox-prod-gate-filter.sh"
        environment = {
            **os.environ,
            "PROD_GATE_ALLOWED_CTID": "101",
            "PROD_GATE_ALLOWED_STORAGE": "pbs-example",
            "PROD_GATE_SNAPSHOT_PREFIX": "example-predeploy",
            "PROD_GATE_GUEST_KIND": "lxc",
            "PROD_GATE_ALLOW_RESTORE": "false",
        }
        base = "vzdump 101 --mode snapshot --storage pbs-example"
        metadata = base + " --pbs-change-detection-mode metadata"
        # Capture exec's argv in a subprocess instead of invoking a real host utility.
        harness = 'exec() { printf "%s\\n" "$@"; exit 0; }; source "$1"'
        for command in (base, metadata):
            with self.subTest(command=command):
                result = subprocess.run(
                    ["bash", "-c", harness, "filter-test", str(script)],
                    env={**environment, "SSH_ORIGINAL_COMMAND": command},
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(
                    result.stdout.splitlines(), ["/usr/bin/vzdump", *command.split()[1:]]
                )
        for kind, command in (
            ("qemu", metadata),
            ("lxc", metadata.replace("101", "102")),
            ("lxc", metadata.replace("pbs-example", "other")),
            ("lxc", metadata.replace("--mode snapshot", "--mode stop")),
            ("lxc", metadata.replace("metadata", "data")),
            ("lxc", metadata.replace("metadata", "legacy")),
            (
                "lxc",
                metadata.replace(
                    "--pbs-change-detection-mode metadata", "--pbs-change-detection-mode=metadata"
                ),
            ),
            ("lxc", metadata.replace("--pbs-change-detection-mode", "--change-detection-mode")),
            ("lxc", metadata + " --crypt-mode none"),
            ("lxc", metadata + " --exclude-path /srv"),
            ("lxc", metadata + " --pbs-change-detection-mode metadata"),
            ("lxc", metadata + "; id"),
            ("lxc", metadata + "\nid"),
            ("lxc", metadata + "\rid"),
        ):
            with self.subTest(kind=kind, command=command):
                denied = subprocess.run(
                    ["bash", "-c", harness, "filter-test", str(script)],
                    env={
                        **environment,
                        "PROD_GATE_GUEST_KIND": kind,
                        "SSH_ORIGINAL_COMMAND": command,
                    },
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(denied.returncode, 126, denied.stderr)
                self.assertEqual(denied.stdout, "")

    def test_binding_is_bounded_and_other_commands_are_rejected(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts/proxmox-prod-gate-filter.sh"
        environment = {
            **os.environ,
            "PROD_GATE_ALLOWED_CTID": "101",
            "PROD_GATE_ALLOWED_STORAGE": "pbs-example",
            "PROD_GATE_SNAPSHOT_PREFIX": "example-predeploy",
            "PROD_GATE_GUEST_KIND": "lxc",
            "PROD_GATE_ALLOW_RESTORE": "false",
            "SSH_ORIGINAL_COMMAND": "launchplane-backup-boundary",
        }
        binding = subprocess.run(
            ["bash", str(script)], env=environment, text=True, capture_output=True
        )
        self.assertEqual(binding.returncode, 0, binding.stderr)
        missing_mode = dict(environment)
        missing_mode.pop("PROD_GATE_ALLOW_RESTORE")
        denied_mode = subprocess.run(
            ["bash", str(script)], env=missing_mode, text=True, capture_output=True
        )
        self.assertEqual(denied_mode.returncode, 126)
        self.assertEqual(
            json.loads(binding.stdout),
            {
                "schema_version": 1,
                "guest_kind": "lxc",
                "guest_id": "101",
                "storage_id": "pbs-example",
                "snapshot_prefix": "example-predeploy",
                "restore_allowed": False,
            },
        )
        for command in (
            "bash",
            "pct listsnapshot 102",
            "pct rollback 101 example-predeploy-20260926-100000-abcdef",
            "pct start 101",
            "qm listsnapshot 101",
            "pvesm status",
            "pvesm status --storage other",
            "pvesm list pbs-example --vmid 102 --content backup",
            "pvesm list pbs-example --vmid 101 --content images",
            "vzdump 101 --mode snapshot --storage other",
            "pct snapshot 101 unrelated-20260926-100000-abcdef",
            "launchplane-backup-boundary; id",
            "launchplane-backup-boundary\nid",
            "launchplane-backup-boundary extra",
        ):
            with self.subTest(command=command):
                denied = subprocess.run(
                    ["bash", str(script)],
                    env={**environment, "SSH_ORIGINAL_COMMAND": command},
                    text=True,
                    capture_output=True,
                )
                self.assertEqual(denied.returncode, 126, denied.stderr)
                self.assertEqual(denied.stdout, "")


if __name__ == "__main__":
    unittest.main()
