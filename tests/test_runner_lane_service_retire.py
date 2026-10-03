from __future__ import annotations

import json
from pathlib import Path
import re
import shutil
import subprocess
import sys
from tempfile import TemporaryDirectory
import unittest


HELPER = Path(__file__).resolve().parents[1] / "scripts/runner-lane-service-retire.sh"

# Every external command except grep is a fixture; no real systemctl can be invoked.
FAKE_COMMAND = """import json
import os
from pathlib import Path
import sys

command = Path(sys.argv[0]).name
args = sys.argv[1:]
if command == "stat":
    print(os.environ["TARGET_UID"] if args[1] == "%u" else os.environ["TARGET_MODE"])
elif command == "realpath":
    if args[-1] == os.environ["REGISTRATION_ROOT"]:
        print(os.environ.get("CANONICAL_ROOT", args[-1]))
    else:
        print(os.environ.get("CANONICAL_PATH", args[-1]))
elif command == "systemctl":
    with Path(os.environ["COMMAND_LOG"]).open("a") as stream:
        stream.write(json.dumps(args) + "\\n")
    if args[0] == "cat":
        sys.exit(int(os.environ.get("CAT_STATUS", "0")))
    if args[0] == "show":
        if "--property=User" in args:
            print(os.environ["UNIT_USER"])
        else:
            print(os.environ["UNIT_EXEC_START"])
    if args[0] in {"is-active", "is-enabled"}:
        status_key = "ACTIVE_STATUS" if args[0] == "is-active" else "ENABLED_STATUS"
        sys.exit(int(os.environ.get(status_key, "1")))
else:
    raise AssertionError(command)
"""


class RunnerLaneServiceRetireTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.root = Path(self.temporary_directory.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "commands.jsonl"
        self.targets = self.root / "targets"
        self.registration = self.root / "runners"
        self.registration.mkdir()
        self.arguments = ["example/repository", "lane-1", str(self.registration), "runner"]
        self.targets.write_text("\t".join(self.arguments) + "\n")
        for name in ("stat", "realpath", "systemctl"):
            executable = self.bin / name
            executable.write_text(f"#!{sys.executable}\n{FAKE_COMMAND}")
            executable.chmod(0o700)
        grep = shutil.which("grep")
        bash = shutil.which("bash")
        if grep is None or bash is None:
            self.fail("Retirement helper tests require bash and grep")
        self.bash = bash
        (self.bin / "grep").symlink_to(grep)
        # Relocate only the fixed host wiring in a private copy. All guard and
        # mutation logic is executed unchanged; production has no test override.
        script = HELPER.read_text()
        for pattern, replacement in (
            (r"^PATH=.*$", f"PATH={self.bin}"),
            (r"^allowed_targets_file=.*$", f"allowed_targets_file={self.targets}"),
        ):
            script, count = re.subn(pattern, replacement, script, flags=re.MULTILINE)
            if count != 1:
                raise AssertionError("Cannot safely relocate retirement helper fixture")
        self.script = self.root / "retire.sh"
        self.script.write_text(script)
        self.env = {
            "SUDO_USER": "runner",
            "TARGET_UID": "0",
            "TARGET_MODE": "600",
            "UNIT_USER": "runner",
            "UNIT_EXEC_START": (
                f"{{ path={self.registration}/lane-1/bin/runsvc.sh ; "
                f"argv[]={self.registration}/lane-1/bin/runsvc.sh ; ignore_errors=no ; }}"
            ),
            "COMMAND_LOG": str(self.log),
            "REGISTRATION_ROOT": str(self.registration),
        }

    def run_helper(self, changes: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.bash, str(self.script), *self.arguments],
            env={**self.env, **(changes or {})},
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )

    def commands(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]

    def assert_refused_without_mutation(self, changes: dict[str, str] | None = None) -> None:
        self.log.unlink(missing_ok=True)
        result = self.run_helper(changes)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("retirement failed", result.stderr)
        self.assertFalse(any(command[0] in {"stop", "disable"} for command in self.commands()))

    def test_retires_only_the_recorded_unit_and_reads_back_stopped_disabled_state(self) -> None:
        result = self.run_helper()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            [command for command in self.commands() if command[0] in {"stop", "disable"}],
            [
                ["stop", "launchplane-runner@lane-1.service"],
                ["disable", "launchplane-runner@lane-1.service"],
            ],
        )
        self.assertEqual(
            [command[0] for command in self.commands()][-2:], ["is-active", "is-enabled"]
        )

    def test_refuses_wrong_sudo_user_owner_permissions_and_noncanonical_paths(self) -> None:
        for changes in (
            {"SUDO_USER": "other"},
            {"SUDO_USER": ""},
            {"TARGET_UID": "1000"},
            {"TARGET_MODE": "644"},
            {"CANONICAL_PATH": "/elsewhere"},
            {"CANONICAL_ROOT": "/elsewhere"},
        ):
            with self.subTest(changes=changes):
                self.assert_refused_without_mutation(changes)

    def test_refuses_missing_symlinked_or_unrecorded_targets(self) -> None:
        self.targets.unlink()
        self.assert_refused_without_mutation()
        backing = self.root / "backing"
        backing.write_text("\t".join(self.arguments) + "\n")
        self.targets.symlink_to(backing)
        self.assert_refused_without_mutation()
        self.targets.unlink()
        for index in range(len(self.arguments)):
            with self.subTest(field=index):
                record = self.arguments.copy()
                record[index] += "-other"
                self.targets.write_text("\t".join(record) + "\n")
                self.assert_refused_without_mutation()

    def test_refuses_wrong_service_user_missing_unit_and_unrelated_exec_start(self) -> None:
        for changes in (
            {"UNIT_USER": "other"},
            {"CAT_STATUS": "1"},
            {"UNIT_EXEC_START": "/elsewhere/lane-1/bin/runsvc.sh"},
            {
                "UNIT_EXEC_START": (
                    f"{{ path={self.registration}/lane-10/bin/runsvc.sh ; argv[]=runner ; }}"
                )
            },
            {
                "UNIT_EXEC_START": (
                    f"{{ path=/usr/bin/bash ; argv[]=bash {self.registration}/lane-1 ; }}"
                )
            },
            {
                "UNIT_EXEC_START": (
                    f"{{ path={self.registration}/lane-1/bin/runsvc.sh-other ; argv[]=runner ; }}"
                )
            },
        ):
            with self.subTest(changes=changes):
                self.assert_refused_without_mutation(changes)

    def test_reports_failed_readback_instead_of_success(self) -> None:
        for status_key in ("ACTIVE_STATUS", "ENABLED_STATUS"):
            with self.subTest(status_key=status_key):
                result = self.run_helper({status_key: "0"})
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("service retired", result.stdout)
