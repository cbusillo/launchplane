"""Execute real shell/rename/descriptor locks with isolated route directories."""

import hashlib
from collections.abc import Callable
import os
import signal
import subprocess
import time
import unittest

from control_plane.dokploy.testing_route_execution import (
    ALLOCATE_TESTING_ROUTE_LOCK,
    FENCED_TESTING_ROUTE_WRITER,
    TestingRouteFence,
)
from tests import test_testing_route_transport as transport_tests


class FencedTestingRouteExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = transport_tests.TestingRouteTransportTests()
        self.fixture.setUp(initialize_fence=False)
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root / "dynamic"
        self.root.mkdir()
        self.name = self.fixture.route_file.name
        self.route = self.root / self.name
        self.receipt = self.root / ("." + self.name + ".receipt")
        self.lock = self.root / ("." + self.name + ".lock")
        self.document = b"fixture route document"
        self.generation = hashlib.sha256(b"fixture original initializer").hexdigest()
        self.filesystem = self.identity(self.root.stat())
        self.environment = {
            **os.environ,
            "PATH": str(self.fixture.bin) + os.pathsep + os.environ["PATH"],
        }
        self.fixture.script(
            "stat",
            "import os,sys\n"
            "p=sys.argv[-1]\n"
            "s=os.fstat(int(p.rsplit('/',1)[1])) if p.startswith('/proc/self/fd/') else os.stat(p)\n"
            "print(str(s.st_dev)+':'+str(s.st_ino))",
        )
        allocated = subprocess.run(
            [
                "sh",
                "-c",
                ALLOCATE_TESTING_ROUTE_LOCK,
                "_",
                str(self.root),
                self.name,
                self.filesystem,
                self.fixture.client.read()["execution_custody"]["process_epoch"],
            ],
            capture_output=True,
            env=self.environment,
            timeout=10,
            check=True,
        )
        self.allocated_lock_identity = allocated.stdout.decode().strip()

    @staticmethod
    def identity(value: os.stat_result) -> str:
        return str(value.st_dev) + ":" + str(value.st_ino)

    def fence(self) -> TestingRouteFence:
        fields = ("generation", "lock_identity", "revision", "key", "desired", "original", "state")
        return TestingRouteFence.model_validate(
            dict(zip(fields, self.receipt.read_text().splitlines(), strict=True))
        )

    def command(
        self,
        mode: str,
        *,
        fence: TestingRouteFence | None = None,
        key: str = "original",
        expected: str = "absent",
        document: bytes | None = None,
    ) -> list[str]:
        return [
            "sh",
            "-c",
            FENCED_TESTING_ROUTE_WRITER,
            "_",
            str(self.root),
            self.name,
            self.filesystem,
            mode,
            self.generation if mode == "initialize" or fence is None else fence.generation,
            str(fence.revision if fence else 0),
            hashlib.sha256(key.encode()).hexdigest(),
            expected,
            hashlib.sha256(document if document is not None else self.document).hexdigest(),
            fence.generation if fence else "absent",
            fence.lock_identity if fence else self.allocated_lock_identity,
            self.fixture.client.read()["execution_custody"]["process_epoch"],
        ]

    def invoke(self, mode: str, **kwargs: object) -> subprocess.CompletedProcess[bytes]:
        from typing import Any, cast

        return subprocess.run(
            self.command(mode, **cast(Any, kwargs)),
            input=cast(bytes, kwargs.get("document", self.document)) if mode == "publish" else b"",
            env=self.environment,
            capture_output=True,
            timeout=10,
        )

    def initialize(self) -> TestingRouteFence:
        result = self.invoke("initialize")
        self.assertEqual(result.returncode, 0, result.stderr)
        fence = self.fence()
        self.assertEqual(fence.generation, self.generation)
        self.assertEqual(fence.lock_identity, self.identity(self.lock.stat()))
        return fence

    def test_late_writes_and_settlement_cannot_replace_a_successor(self) -> None:
        original = self.initialize()
        self.assertEqual(self.invoke("settle", fence=original).stdout.strip(), b"excluded")
        successor = b"fixture successor"
        result = self.invoke("publish", fence=self.fence(), key="successor", document=successor)
        self.assertEqual(result.stdout.strip(), b"applied", result.stderr)
        saved = self.fence()
        for mode in ("settle", "publish"):
            result = self.invoke(mode, fence=original)
            self.assertEqual(result.stdout.strip(), b"superseded", result.stderr)
            self.assertEqual(self.fence(), saved)
            self.assertEqual(self.route.read_bytes(), successor)

    def test_lost_execution_files_do_not_bootstrap_an_old_writer(self) -> None:
        original = self.initialize()
        self.receipt.unlink()
        self.lock.unlink()
        for mode in ("inspect", "settle", "publish"):
            result = self.invoke(mode, fence=original)
            self.assertNotEqual(result.returncode, 0)
            self.assertFalse(list(self.root.iterdir()))

    def test_a_new_generation_excludes_the_old_argv_on_the_same_root(self) -> None:
        original = self.initialize()
        old_command = self.command("publish", fence=original)
        self.generation = hashlib.sha256(b"fixture successor initializer").hexdigest()
        result = self.invoke("initialize", fence=original)
        self.assertEqual(result.returncode, 0, result.stderr)
        successor = self.fence()
        self.assertEqual(successor.generation, self.generation)
        self.assertEqual(successor.lock_identity, original.lock_identity)
        result = subprocess.run(
            old_command,
            input=self.document,
            env=self.environment,
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.fence(), successor)
        self.assertFalse(self.route.exists())

    def test_old_runtime_cannot_write_when_its_original_directory_returns(self) -> None:
        self.assertEqual(self.invoke("initialize").returncode, 0)
        old_command = self.command("publish", fence=self.fence())
        detached = self.root.with_name("detached-original")
        self.root.rename(detached)
        self.root.mkdir()
        self.fixture.process_start.write_text("200")
        self.root.rmdir()
        detached.rename(self.root)
        result = subprocess.run(
            old_command,
            input=self.document,
            env=self.environment,
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(self.route.exists())

    def test_successive_attempts_do_not_grow_host_artifacts(self) -> None:
        self.initialize()
        footprint: set[str] | None = None
        expected = "absent"
        for index in range(5):
            document = f"fixture document {index}".encode()
            result = self.invoke(
                "publish", fence=self.fence(), key=str(index), document=document, expected=expected
            )
            self.assertEqual(result.stdout.strip(), b"applied", result.stderr)
            expected = hashlib.sha256(document).hexdigest()
            observed = {path.name for path in self.root.iterdir()}
            if footprint is None:
                footprint = observed
            self.assertEqual(observed, footprint)
        self.assertEqual(self.route.read_bytes(), document)

    def test_fresh_initializer_cleans_only_abandoned_owned_staging_under_lock(self) -> None:
        owned = self.root / ("." + self.name + ".pending.abandoned")
        owned.write_bytes(b"unfinished receipt")
        other = self.root / ".another-route.pending.keep"
        other.write_bytes(b"other route")
        self.initialize()
        self.assertFalse(owned.exists())
        self.assertEqual(other.read_bytes(), b"other route")

    def wait_for(self, predicate: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 5
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("The controlled writer did not reach its expected boundary")
            time.sleep(0.01)

    def test_a_writer_pins_its_directory_before_the_identity_check(self) -> None:
        original = self.initialize()
        entered = self.fixture.root / "entered"
        release = self.fixture.root / "release"
        self.fixture.script(
            "stat",
            "import os,sys,time;from pathlib import Path\n"
            "p=sys.argv[-1]\n"
            "s=os.fstat(int(p.rsplit('/',1)[1])) if p.startswith('/proc/self/fd/') else os.stat(p)\n"
            "if p=='.' and os.environ.get('GATED_WRITER')=='yes':\n"
            " Path(os.environ['GATE_ENTERED']).touch()\n"
            " while not Path(os.environ['GATE_RELEASE']).exists(): time.sleep(.01)\n"
            "print(str(s.st_dev)+':'+str(s.st_ino))",
        )
        process = subprocess.Popen(
            self.command("publish", fence=original),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={
                **self.environment,
                "GATED_WRITER": "yes",
                "GATE_ENTERED": str(entered),
                "GATE_RELEASE": str(release),
            },
        )
        try:
            assert process.stdin is not None
            process.stdin.write(self.document)
            process.stdin.close()
            process.stdin = None
            self.wait_for(entered.exists)
            retired = self.fixture.root / "retired"
            self.root.rename(retired)
            self.root.mkdir()
            release.touch()
            stdout, stderr = process.communicate(timeout=10)
            self.assertEqual(process.returncode, 0, stderr)
            self.assertEqual(stdout.strip(), b"applied")
            self.assertFalse(list(self.root.iterdir()))
            self.assertEqual((retired / self.name).read_bytes(), self.document)
        finally:
            release.touch()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)

    def killed_parent(self, *, after_rename: bool) -> None:
        original = self.initialize()
        entered = self.fixture.root / "entered"
        release = self.fixture.root / "release"
        gate = (
            "\n Path(os.environ['GATE_ENTERED']).touch()"
            "\n while not Path(os.environ['GATE_RELEASE']).exists(): time.sleep(.01)\n"
        )
        if after_rename:
            self.fixture.script(
                "sync",
                "import os,sys,time;from pathlib import Path\n"
                "fd=os.open(sys.argv[-1],os.O_RDONLY);os.fsync(fd);os.close(fd)\n"
                "if os.environ.get('GATED_WRITER')=='yes' and sys.argv[-1]=='.' and Path(os.environ['WRITER_ROUTE']).exists():"
                + gate,
            )
        else:
            self.fixture.script(
                "mv",
                "import os,sys,time;from pathlib import Path\n"
                "if os.environ.get('GATED_WRITER')=='yes' and sys.argv[-1]==os.environ['WRITER_NAME']:"
                + gate
                + "\nos.replace(sys.argv[-2],sys.argv[-1])\n",
            )
        process = subprocess.Popen(
            self.command("publish", fence=original),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={
                **self.environment,
                "GATED_WRITER": "yes",
                "GATE_ENTERED": str(entered),
                "GATE_RELEASE": str(release),
                "WRITER_NAME": self.name,
                "WRITER_ROUTE": str(self.route),
            },
        )
        try:
            assert process.stdin is not None
            process.stdin.write(self.document)
            process.stdin.close()
            process.stdin = None
            self.wait_for(entered.exists)
            process.kill()
            process.wait(timeout=5)
            self.assertEqual(self.invoke("settle", fence=original).stdout.strip(), b"busy")
            if not after_rename:
                self.assertFalse(self.route.exists())
            release.touch()
            process.communicate(timeout=5)
            self.assertEqual(self.invoke("settle", fence=original).stdout.strip(), b"applied")
            self.assertEqual(self.route.read_bytes(), self.document)
        finally:
            release.touch()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)

    def test_a_surviving_rename_keeps_the_original_lock(self) -> None:
        self.killed_parent(after_rename=False)

    def test_a_surviving_sync_keeps_the_original_lock(self) -> None:
        self.killed_parent(after_rename=True)

    def test_a_nonce_witness_does_not_exclude_a_surviving_initializer_child(self) -> None:
        entered = self.fixture.root / "entered"
        release = self.fixture.root / "release"
        self.fixture.script(
            "sync",
            "import os,sys,time;from pathlib import Path\n"
            "fd=os.open(sys.argv[-1],os.O_RDONLY);os.fsync(fd);os.close(fd)\n"
            "if os.environ.get('GATED_WRITER')=='yes' and sys.argv[-1]=='.' and Path(os.environ['WRITER_RECEIPT']).exists():\n"
            " Path(os.environ['GATE_ENTERED']).touch()\n"
            " while not Path(os.environ['GATE_RELEASE']).exists(): time.sleep(.01)\n",
        )
        process = subprocess.Popen(
            self.command("initialize"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={
                **self.environment,
                "GATED_WRITER": "yes",
                "GATE_ENTERED": str(entered),
                "GATE_RELEASE": str(release),
                "WRITER_RECEIPT": str(self.receipt),
            },
        )
        try:
            self.wait_for(entered.exists)
            witness = self.fence()
            process.kill()
            process.wait(timeout=5)
            self.assertEqual(self.invoke("inspect", fence=witness).stdout.strip(), b"busy")
            release.touch()
            process.communicate(timeout=5)
            result = self.invoke("inspect", fence=witness)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout.decode(), self.receipt.read_text())
            self.assertEqual(self.fence(), witness)
            self.assertFalse(self.route.exists())
        finally:
            release.touch()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)
