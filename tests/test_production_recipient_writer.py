"""Execute fixed writer and execution-fence programs against isolated host fixtures."""

import base64
from contextlib import redirect_stdout
import io
import json
import subprocess
import sys
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import patch

from control_plane.production_recipient_cutover import WRITER_PROGRAM
from control_plane.production_recipient_fence import (
    HOST_FENCE_LIBRARY,
    fenced_program,
    RETIRE_EXECUTION_PROGRAM,
)


class RecipientWriterProgramTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.identity = {
            "deployment_record_id": "original-fixture",
            "context": "fixture",
            "instance": "prod",
        }
        self.spec = {
            "project": "exact-project",
            "service": "sync",
            "binding": "a" * 64,
            "old": True,
            "expected_identity": self.identity,
            "image": "example@sha256:" + "a" * 64,
        }
        self.row: dict[str, Any] = {
            "Image": "immutable-container-image",
            "State": {"Running": True},
            "Config": {
                "Labels": {
                    "com.docker.compose.project": "exact-project",
                    "com.docker.compose.service": "sync",
                },
                "Env": ["LAUNCHPLANE_RUNTIME_IDENTITY_JSON=" + json.dumps(self.identity)],
            },
        }
        self.effects: list[str] = []

    def docker(self, args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if args[1] == "ps":
            output = "exact-writer\n"
        elif args[1] == "inspect":
            output = json.dumps([self.row])
        elif args[1] in {"stop", "start"}:
            self.assertEqual(args[-1], "exact-writer")
            self.effects.append(args[1])
            self.row["State"]["Running"] = args[1] == "start"
            output = "exact-writer\n"
        else:
            self.fail(f"Unexpected Docker operation {args[1:]}")
        return subprocess.CompletedProcess(args, 0, stdout=output, stderr="")

    def run_program(self, action: str) -> str:
        output = io.StringIO()
        with (
            patch.object(
                sys,
                "argv",
                [
                    "writer",
                    base64.b64encode(json.dumps({**self.spec, "action": action}).encode()).decode(),
                ],
            ),
            patch("subprocess.run", side_effect=self.docker),
            redirect_stdout(output),
        ):
            exec(
                compile(
                    WRITER_PROGRAM.replace(
                        "/var/lib/launchplane/recipient-writers", str(self.root / "writers")
                    ),
                    "<writer>",
                    "exec",
                ),
                {},
            )
        return output.getvalue()

    def test_pause_receipt_resume_and_late_pause_preserve_only_original_writer(self) -> None:
        self.run_program("pause")
        self.assertFalse(self.row["State"]["Running"])
        self.assertIn("recipient_writer_action=receipt_pause", self.run_program("receipt_pause"))
        self.assertEqual(self.effects, ["stop"])
        self.run_program("resume")
        self.assertTrue(self.row["State"]["Running"])
        self.assertIn("recipient_writer_action=receipt_resume", self.run_program("receipt_resume"))
        with self.assertRaisesRegex(ValueError, "permanently released"):
            self.run_program("pause")
        self.assertEqual(self.effects, ["stop", "start"])

    def test_originally_stopped_writer_is_not_started_by_rollback(self) -> None:
        self.row["State"]["Running"] = False
        self.run_program("pause")
        self.run_program("resume")
        self.assertFalse(self.row["State"]["Running"])
        self.assertNotIn("start", self.effects)

    def test_container_identity_change_or_missing_terminal_witness_refuses_adoption(self) -> None:
        with self.assertRaisesRegex(ValueError, "not witnessed"):
            self.run_program("receipt_pause")
        original = self.docker

        def fail_readback(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args[1] == "inspect" and self.effects:
                raise OSError("reply lost before host completion witness")
            return original(args, **kwargs)

        with patch.object(self, "docker", side_effect=fail_readback):
            with self.assertRaises(OSError):
                self.run_program("pause")
        with self.assertRaisesRegex(ValueError, "terminal witness"):
            self.run_program("receipt_pause")
        self.row["Image"] = "intervening-image"
        with self.assertRaisesRegex(ValueError, "retained writer changed"):
            self.run_program("resume")
        self.assertEqual(self.effects, ["stop"])


class RecipientExecutionFenceTests(unittest.TestCase):
    def test_durable_tombstone_rejects_queued_worker_after_idle_lock(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            spec = {"namespace": "lp-prod-recipient-" + "b" * 32, "binding": "c" * 64}
            effects: list[str] = []
            namespace = {"effects": effects}
            program = fenced_program(
                "effects.append('provider effect')", spec["namespace"], spec["binding"]
            ).replace("/var/lib/launchplane/recipient-execution", str(root))
            exec(compile(program, "<before>", "exec"), namespace)
            retire = RETIRE_EXECUTION_PROGRAM.replace(
                "/var/lib/launchplane/recipient-execution", str(root)
            )
            with (
                patch.object(
                    sys, "argv", ["retire", base64.b64encode(json.dumps(spec).encode()).decode()]
                ),
                redirect_stdout(io.StringIO()),
            ):
                exec(compile(retire, "<retire>", "exec"), {})
            with self.assertRaisesRegex(ValueError, "permanently retired"):
                exec(compile(program, "<queued>", "exec"), namespace)
            self.assertEqual(effects, ["provider effect"])

    def test_active_worker_prevents_fence_commit(self) -> None:
        scope: dict[str, Any] = {}
        exec(HOST_FENCE_LIBRARY, scope)
        with TemporaryDirectory() as temporary:
            spec = {"namespace": "lp-prod-recipient-" + "d" * 32, "binding": "e" * 64}
            base = Path(temporary).resolve()
            with scope["recipient_execution"](spec, base=base):
                with self.assertRaises(BlockingIOError):
                    with scope["recipient_execution"](spec, retire=True, base=base):
                        self.fail("An active host worker must retain execution.")
            self.assertFalse((base / spec["namespace"] / "retired.json").exists())
