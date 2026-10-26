from contextlib import redirect_stdout
import io
import json
import subprocess
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.dokploy.cutover_restore_probe import RESTORE_PROBE_PROGRAM


class CutoverRestoreProbeTests(unittest.TestCase):
    def setUp(self) -> None:
        self.request = dict(
            record="original-cutover",
            image="postgres@sha256:" + "a" * 64,
            runner="runner",
            dump="/backup/example.dump",
            database="example",
            database_uuid="original-database",
            database_dump_sha256="b" * 64,
            filestore="/backup/example.tar.gz",
            filestore_sha256="c" * 64,
            timeout_seconds=1800,
        )
        self.commands: list[tuple[list[str], dict[str, Any]]] = []
        self.checksum = "d" * 40
        self.container = "e" * 64
        self.clock = 0.0

    def run_probe(self, *, failure: str = "") -> dict[str, Any]:
        def run(argv: list[str], **kwargs: Any) -> subprocess.CompletedProcess[bytes]:
            self.commands.append((argv, kwargs))
            output = b""
            status = 0
            if argv[1] == "ps" and failure == "live":
                output = self.container.encode()
            elif argv[1] == "run":
                output = self.container.encode()
            elif "pg_database_size" in argv[-1]:
                output = b"100000"
            elif "df" in argv:
                output = b"Filesystem 1-blocks Used Available Capacity Mounted\nx 100000000 100 99999900 1% /data"
                if failure == "space":
                    output = (
                        b"Filesystem 1-blocks Used Available Capacity Mounted\nx 100 99 1 99% /data"
                    )
            elif "sha256sum" in argv:
                field = (
                    "database_dump_sha256"
                    if argv[-1] == self.request["dump"]
                    else "filestore_sha256"
                )
                output = (str(self.request[field]) + "  artifact").encode()
            elif argv[-1].startswith("SELECT store_fname"):
                output = (("dd/" + self.checksum + "|" + self.checksum + "\n") * 10000).encode()
            elif argv[-1].startswith("SELECT value"):
                output = str(self.request["database_uuid"]).encode()
            elif "sha1sum" in argv and failure == "attachment":
                status = 1
            elif "pg_restore" in argv:
                self.clock += 30
                if failure == "timeout":
                    raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            elif argv[1] == "rm" and failure == "cleanup":
                raise subprocess.TimeoutExpired(argv, kwargs["timeout"])
            return subprocess.CompletedProcess(argv, status, stdout=output)

        source = Mock(stdout=io.BytesIO(b"archive"))
        source.wait.return_value = 0
        source.poll.return_value = 0
        output = io.StringIO()
        with (
            patch("sys.stdin", io.StringIO(json.dumps(self.request))),
            patch("subprocess.run", side_effect=run),
            patch("subprocess.Popen", return_value=source),
            patch("time.monotonic", side_effect=lambda: self.clock),
            redirect_stdout(output),
        ):
            exec(RESTORE_PROBE_PROGRAM, {})
        return json.loads(output.getvalue())  # type: ignore[no-any-return]

    def test_restore_uses_one_deadline_and_one_attachment_batch_with_owned_cleanup(self) -> None:
        result = self.run_probe()
        self.assertEqual(result["restored_database_uuid"], self.request["database_uuid"])
        checks = [(argv, kwargs) for argv, kwargs in self.commands if "sha1sum" in argv]
        self.assertEqual(len(checks), 1)
        self.assertEqual(len(checks[0][1]["input"].splitlines()), 10000)
        restore = next(kwargs for argv, kwargs in self.commands if "pg_restore" in argv)
        archive = next(kwargs for argv, kwargs in self.commands if "tar" in argv)
        self.assertLess(archive["timeout"], restore["timeout"])
        self.assertLess(restore["timeout"], self.request["timeout_seconds"])
        self.assertTrue(
            all(
                0 < kwargs["timeout"] <= self.request["timeout_seconds"]
                for _, kwargs in self.commands
            )
        )
        self.assertIn(
            ["docker", "rm", "-f", "-v", self.container], [argv for argv, _ in self.commands]
        )

    def test_space_hash_and_timeout_failures_are_definite_only_after_verified_cleanup(self) -> None:
        for failure in ("space", "attachment", "timeout"):
            with self.subTest(failure=failure):
                self.commands.clear()
                result = self.run_probe(failure=failure)
                self.assertEqual(result, {"status": "failed", "terminated": True})
                self.assertIn(
                    ["docker", "rm", "-f", "-v", self.container],
                    [argv for argv, _ in self.commands],
                )
                if failure == "space":
                    self.assertFalse(any("pg_restore" in argv for argv, _ in self.commands))
        with self.assertRaises(subprocess.TimeoutExpired):
            self.run_probe(failure="cleanup")

    def test_live_original_restore_refuses_a_second_copy_without_effects(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "original restore work"):
            self.run_probe(failure="live")
        self.assertFalse(any(argv[1] in {"run", "rm"} for argv, _ in self.commands))


class NativeRestoreLifetimeTests(unittest.TestCase):
    def test_coordinator_death_cannot_retain_restore_volume(self) -> None:
        import os
        from pathlib import Path
        import sys
        from tempfile import TemporaryDirectory
        import time

        image = os.environ.get("LAUNCHPLANE_TEST_RESTORE_IMAGE")
        if not image:
            self.skipTest("select an already-local immutable PostgreSQL restore image")
        from control_plane.dokploy.cutover_restore_probe import RESTORE_PREFLIGHT_PROGRAM

        preflight = subprocess.run(
            [
                "docker",
                "run",
                "--rm",
                "--pull",
                "never",
                "--network",
                "none",
                "--entrypoint",
                "pg_dump",
                image,
                "--version",
            ],
            capture_output=True,
            text=True,
            check=True,
            timeout=30,
        )
        major = int(preflight.stdout.split()[2].split(".")[0])
        subprocess.run(
            [sys.executable, "-c", RESTORE_PREFLIGHT_PROGRAM],
            input=json.dumps({"image": image, "dump_major": major}),
            text=True,
            capture_output=True,
            check=True,
            timeout=40,
        )
        with TemporaryDirectory() as temporary:
            marker = Path(temporary) / "allocated.json"
            spec = {
                "image": image,
                "record": "native-lifetime",
                "runner": "inert-source",
                "database": "example",
                "timeout_seconds": 20,
                "dump": "inert.dump",
                "database_dump_sha256": "a" * 64,
            }
            harness = """
import json,subprocess,sys,time
from pathlib import Path
from control_plane.dokploy.cutover_restore_probe import RESTORE_PROBE_PROGRAM
original=subprocess.run
marker=Path(sys.argv[1])
def run(argv,**kwargs):
    if argv[1:4]==['exec','-e','DATABASE_NAME=example']:
        return subprocess.CompletedProcess(argv,0,stdout=b'1024')
    if 'sha256sum' in argv:
        value=json.loads(original(['docker','inspect',container],capture_output=True,check=True).stdout)[0]
        marker.write_text(json.dumps({'container':container,'volumes':[m['Name'] for m in value['Mounts'] if m['Type']=='volume']}))
        time.sleep(120)
    return original(argv,**kwargs)
subprocess.run=run
exec(RESTORE_PROBE_PROGRAM,globals())
"""
            child = subprocess.Popen(
                [sys.executable, "-c", harness, str(marker)],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            self.addCleanup(lambda: child.kill() if child.poll() is None else None)
            assert child.stdin is not None
            child.stdin.write(json.dumps(spec))
            child.stdin.close()
            deadline = time.monotonic() + 35
            while not marker.exists() and child.poll() is None and time.monotonic() < deadline:
                time.sleep(0.1)
            self.assertTrue(
                marker.exists(),
                child.stderr.read()
                if child.poll() is not None and child.stderr
                else "restore did not reach native allocation",
            )
            allocation = json.loads(marker.read_text())
            child.kill()
            child.wait(timeout=5)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                current = subprocess.run(
                    [
                        "docker",
                        "ps",
                        "-a",
                        "--no-trunc",
                        "--filter",
                        "id=" + allocation["container"],
                        "--format",
                        "{{.ID}}",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                if not current.stdout.strip():
                    break
                time.sleep(0.25)
            self.assertFalse(current.stdout.strip())
            for volume in allocation["volumes"]:
                observed = subprocess.run(
                    [
                        "docker",
                        "volume",
                        "ls",
                        "--filter",
                        "name=^" + volume + "$",
                        "--format",
                        "{{.Name}}",
                    ],
                    capture_output=True,
                    text=True,
                    check=True,
                    timeout=5,
                )
                self.assertFalse(observed.stdout.strip())
            if child.stdout:
                child.stdout.close()
            if child.stderr:
                child.stderr.close()
