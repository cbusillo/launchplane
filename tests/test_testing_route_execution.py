"""Execute real shell/rename/descriptor locks with isolated route directories."""

import hashlib
from collections.abc import Callable
import json
import os
import signal
import subprocess
import time
import unittest

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.dokploy.testing_route import route_document
from control_plane.dokploy.testing_route_execution import ATOMIC_TESTING_ROUTE_WRITER
from tests import test_testing_route_transport as transport_tests


class TestingRouteExecutionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.fixture = transport_tests.TestingRouteTransportTests()
        self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        self.root = self.fixture.root
        self.execution = self.fixture.client.execution_intent(
            self.fixture.plan, self.fixture.plan.new_backend, operation_id="execution-fixture"
        )
        self.document = json.dumps(
            route_document(self.fixture.plan, self.fixture.plan.new_backend),
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
        self.environment = {
            **os.environ,
            "PATH": str(self.fixture.bin) + os.pathsep + os.environ["PATH"],
        }

    def command(
        self, mode: str, *, key: str = "", desired: str = "", filesystem: str = ""
    ) -> list[str]:
        return [
            "sh",
            "-c",
            ATOMIC_TESTING_ROUTE_WRITER,
            "_",
            str(self.root),
            self.fixture.route_file.name,
            "absent",
            desired or self.execution.document_sha256,
            key or self.execution.key(attempt=1),
            filesystem or self.execution.custody.filesystem,
            canonical_json_sha256(self.execution.custody.model_dump(mode="json")),
            mode,
        ]

    def invoke(self, mode: str) -> str:
        result = subprocess.run(
            self.command(mode),
            input=self.document if mode == "publish" else b"",
            env=self.environment,
            capture_output=True,
            timeout=10,
            check=True,
        )
        return result.stdout.decode().strip()

    def test_a_sealed_original_cannot_arrive_after_a_successor(self) -> None:
        self.assertEqual(self.invoke("settle"), "excluded")
        successor = b"fixture successor route"
        self.fixture.route_file.write_bytes(successor)
        self.assertEqual(self.invoke("publish"), "excluded")
        self.assertEqual(self.fixture.route_file.read_bytes(), successor)

    def test_a_key_cannot_be_reused_for_a_different_document(self) -> None:
        self.assertEqual(self.invoke("publish"), "applied")
        before = self.fixture.route_file.read_bytes()
        other = b"fixture conflicting document"
        result = subprocess.run(
            self.command("publish", desired=hashlib.sha256(other).hexdigest()),
            input=other,
            env=self.environment,
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(self.fixture.route_file.read_bytes(), before)

    def test_changed_filesystem_cannot_supply_an_exclusion(self) -> None:
        device, inode = self.execution.custody.filesystem.split(":")
        result = subprocess.run(
            self.command("settle", filesystem=device + ":" + str(int(inode) + 1)),
            env=self.environment,
            capture_output=True,
            timeout=10,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertFalse(list(self.root.glob("*.executions")))

    def wait_for(self, predicate: Callable[[], bool]) -> None:
        deadline = time.monotonic() + 5
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail("The controlled writer did not reach its expected boundary")
            time.sleep(0.01)

    def killed_parent(self, *, after_rename: bool) -> None:
        entered, release = self.root / "entered", self.root / "release"
        gate = (
            "\nif os.environ.get('GATED_WRITER')=='yes':"
            "\n from pathlib import Path"
            "\n Path(os.environ['GATE_ENTERED']).write_text(str(os.getpid()))"
            "\n while not Path(os.environ['GATE_RELEASE']).exists(): time.sleep(.01)\n"
        )
        if after_rename:
            body = (
                "import os,sys,time;fd=os.open(sys.argv[-1],os.O_RDONLY);os.fsync(fd);os.close(fd)"
                "\nif sys.argv[-1]==os.environ.get('WRITER_ROOT') and os.path.exists(os.environ.get('WRITER_ROUTE','')):"
                + "\n ".join(gate.splitlines())
            )
            self.fixture.script("sync", body)
        else:
            self.fixture.script(
                "mv",
                "import os,sys,time"
                "\nif sys.argv[-1]==os.environ.get('WRITER_ROUTE'):"
                + "\n ".join(gate.splitlines())
                + "\nos.replace(sys.argv[-2],sys.argv[-1])\n",
            )
        process = subprocess.Popen(
            self.command("publish"),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
            env={
                **self.environment,
                "GATED_WRITER": "yes",
                "WRITER_ROOT": str(self.root),
                "WRITER_ROUTE": str(self.fixture.route_file),
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
            process.kill()
            process.wait(timeout=5)
            self.assertEqual(self.invoke("settle"), "busy")
            if not after_rename:
                self.assertFalse(self.fixture.route_file.exists())
            release.touch()
            process.communicate(timeout=5)
            self.assertEqual(self.invoke("settle"), "applied")
            self.assertEqual(self.fixture.route_file.read_bytes(), self.document)
            self.assertEqual(self.invoke("publish"), "applied")
        finally:
            release.touch()
            try:
                process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                os.killpg(process.pid, signal.SIGKILL)
                process.communicate(timeout=5)

    def test_killed_shell_keeps_its_child_rename_excluded(self) -> None:
        self.killed_parent(after_rename=False)

    def test_interrupted_completion_is_adopted_after_the_child_finishes(self) -> None:
        self.killed_parent(after_rename=True)
