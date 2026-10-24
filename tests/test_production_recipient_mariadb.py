"""Opt-in real MariaDB cold capture/restore proof on disposable local storage."""

from contextlib import redirect_stdout
import io
import json
import os
from pathlib import Path
import subprocess
import time
from tempfile import TemporaryDirectory
from typing import Any
import unittest
import uuid

from control_plane.production_recipient_host import RECOVERY_LIBRARY
from control_plane.production_recipient_resources import RESOURCE_LIBRARY


@unittest.skipUnless(
    os.environ.get("LAUNCHPLANE_TEST_MARIADB_IMAGE"), "explicit local MariaDB image required"
)
class RealMariaDBRecoveryTests(unittest.TestCase):
    def setUp(self) -> None:
        self.image = os.environ["LAUNCHPLANE_TEST_MARIADB_IMAGE"]
        temporary = TemporaryDirectory(dir=os.environ.get("LAUNCHPLANE_TEST_MARIADB_DIRECTORY"))
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.paths = [self.root / name for name in ("database", "config")]
        for path in self.paths:
            path.mkdir()
        self.namespace: dict[str, Any] = {}
        exec(
            compile(RESOURCE_LIBRARY + RECOVERY_LIBRARY, "<real-recovery>", "exec"), self.namespace
        )
        self.namespace["main"] = lambda: self.paths
        self.spec = {
            "recovery_binding": uuid.uuid4().hex + uuid.uuid4().hex,
            "database_image": self.image,
        }

    def run_capture(self) -> str:
        output = io.StringIO()
        with redirect_stdout(output):
            self.namespace["recover"](self.paths, self.spec, base=self.root / "captures")
        return output.getvalue()

    def docker(self, *args: str) -> str:
        return subprocess.run(
            ["docker", *args], capture_output=True, text=True, check=True, timeout=120
        ).stdout.strip()

    def test_empty_destination_is_restored_and_initialized_only_in_isolated_copy(self) -> None:
        self.assertIn("recipient_recovery_ok=true", self.run_capture())
        self.assertEqual(list(self.paths[0].iterdir()), [])
        self.assertEqual(list(self.paths[1].iterdir()), [])

    def seed_database(
        self, engine: str = "InnoDB", *, keep_running: bool = False, tmpfs: bool = False
    ) -> str:
        name = "lp-recovery-fixture-" + uuid.uuid4().hex
        identifier = self.docker(
            "run",
            "-d",
            "--name",
            name,
            "--network",
            "none",
            "--env",
            "MARIADB_ALLOW_EMPTY_ROOT_PASSWORD=1",
            *(["--tmpfs", "/tmp"] if tmpfs else []),
            "--health-cmd",
            "healthcheck.sh --connect --innodb_initialized",
            "--health-interval",
            "1s",
            "--health-timeout",
            "3s",
            "--health-retries",
            "60",
            "--mount",
            f"type=bind,src={self.paths[0]},dst=/var/lib/mysql",
            self.image,
        )
        self.addCleanup(self.docker, "rm", "-f", identifier)
        deadline = time.monotonic() + 90
        while (
            json.loads(self.docker("inspect", identifier))[0]["State"]["Health"]["Status"]
            != "healthy"
        ):
            if time.monotonic() >= deadline:
                self.fail(
                    "Disposable MariaDB fixture did not become healthy: "
                    + self.docker("logs", identifier)
                )
            time.sleep(0.25)
        self.docker(
            "exec",
            identifier,
            "mariadb",
            "--protocol=socket",
            "--user=root",
            f"--execute=CREATE DATABASE fixture; CREATE TABLE fixture.items (note VARCHAR(80)) ENGINE={engine}; INSERT INTO fixture.items VALUES ('restored-business-row');",
        )
        if not keep_running:
            self.docker("stop", "--time", "30", identifier)
        (self.paths[1] / "settings").write_text("retained fixture settings")
        return identifier

    def test_populated_cold_database_and_config_survive_real_recovery(self) -> None:
        self.seed_database()
        before = [self.namespace["manifest"](path) for path in self.paths]
        actual = self.namespace["docker"]

        def check(*args: str) -> str:
            result = str(actual(*args))
            if args[0] == "exec" and "mariadb-check" in args:
                rows = self.docker(
                    "exec",
                    args[1],
                    "mariadb",
                    "--protocol=socket",
                    "--user=root",
                    "--batch",
                    "--skip-column-names",
                    "--execute=SELECT note FROM fixture.items",
                )
                self.assertEqual(rows, "restored-business-row")
                inspected = json.loads(self.docker("inspect", args[1]))[0]
                self.assertEqual(inspected["HostConfig"]["NetworkMode"], "none")
            return result

        self.namespace["docker"] = check
        self.assertIn("recipient_recovery_ok=true", self.run_capture())
        self.assertEqual(before, [self.namespace["manifest"](path) for path in self.paths])

    def test_corrupt_aria_table_is_refused_even_when_check_exits_zero(self) -> None:
        self.seed_database("Aria")
        (self.paths[0] / "fixture" / "items.MAI").write_bytes(b"corrupt index")
        before = [self.namespace["manifest"](path) for path in self.paths]
        with self.assertRaises(ValueError):
            self.run_capture()
        self.assertEqual(before, [self.namespace["manifest"](path) for path in self.paths])
        outcome = json.loads(
            (self.root / "captures" / self.spec["recovery_binding"] / "outcome.json").read_text()
        )
        self.assertEqual(outcome["state"], "failed_safe")

    def test_partial_mysql_initialization_cannot_qualify_as_populated_recovery(self) -> None:
        (self.paths[0] / "mysql").mkdir()
        before = [self.namespace["manifest"](path) for path in self.paths]
        with self.assertRaises((ValueError, RuntimeError)):
            self.run_capture()
        self.assertEqual(before, [self.namespace["manifest"](path) for path in self.paths])
        outcome = json.loads(
            (self.root / "captures" / self.spec["recovery_binding"] / "outcome.json").read_text()
        )
        self.assertEqual(outcome["state"], "failed_safe")
