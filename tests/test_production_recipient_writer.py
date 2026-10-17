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

from control_plane.production_recipient_cutover import WRITER_LIBRARY, WRITER_RECEIPT_LIBRARY
from control_plane.dokploy.post_deploy import extract_odoo_post_deploy_readback_markers
from control_plane.production_recipient_evidence import WRITER_ACTION_MARKER, WRITER_BINDING_MARKER
from control_plane.production_recipient_fence import (
    HOST_FENCE_LIBRARY,
    fenced_program,
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
            scope: dict[str, Any] = {}
            exec(WRITER_LIBRARY, scope)
            scope["coordinate_writer"]({**self.spec, "action": action}, base=self.root / "writers")
        text = output.getvalue()
        evidence = extract_odoo_post_deploy_readback_markers({"logs": text})
        self.assertEqual(evidence[WRITER_ACTION_MARKER], action)
        self.assertEqual(evidence[WRITER_BINDING_MARKER], self.spec["binding"])
        return text

    def test_pause_receipt_resume_and_late_pause_preserve_only_original_writer(self) -> None:
        self.run_program("pause")
        self.assertFalse(self.row["State"]["Running"])
        self.run_program("receipt_pause")
        self.assertFalse(self.row["State"]["Running"])
        self.assertEqual(self.effects, ["stop"])
        self.run_program("resume")
        self.assertTrue(self.row["State"]["Running"])
        self.run_program("receipt_resume")
        self.assertTrue(self.row["State"]["Running"])
        with self.assertRaises(ValueError):
            self.run_program("pause")
        self.assertEqual(self.effects, ["stop", "start"])

    def test_interrupted_start_retries_under_same_pause_and_cannot_override_a_later_pause(
        self,
    ) -> None:
        self.run_program("pause")
        original = self.docker
        failures: list[bool] = []

        def fail_start(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args[1] == "start" and not failures:
                failures.append(True)
                raise TimeoutError("start interrupted")
            return original(args, **kwargs)

        with patch.object(self, "docker", side_effect=fail_start):
            with self.assertRaises(TimeoutError):
                self.run_program("resume")
        self.assertFalse(self.row["State"]["Running"])
        self.run_program("receipt_resume_retryable")
        scope: dict[str, Any] = {}
        exec(WRITER_RECEIPT_LIBRARY, scope)
        with (
            redirect_stdout(io.StringIO()),
            patch("subprocess.run", side_effect=AssertionError("Retained read is read-only")),
        ):
            scope["read_writer_receipt"](
                {**self.spec, "action": "receipt_resume_retryable"}, base=self.root / "writers"
            )
        self.run_program("resume")
        self.assertTrue(self.row["State"]["Running"])
        self.spec["binding"] = "b" * 64
        self.run_program("pause")
        self.spec["binding"] = "a" * 64
        with self.assertRaises(ValueError):
            self.run_program("resume")
        self.assertFalse(self.row["State"]["Running"])
        self.assertEqual(self.effects, ["stop", "start", "stop"])

    def test_originally_stopped_writer_is_not_started_by_rollback(self) -> None:
        self.row["State"]["Running"] = False
        self.run_program("pause")
        self.run_program("resume")
        self.assertFalse(self.row["State"]["Running"])
        self.assertNotIn("start", self.effects)

    def test_new_pause_cycle_has_its_own_witness_and_preserves_old_release(self) -> None:
        original = self.spec["binding"]
        self.run_program("pause")
        self.run_program("resume")
        self.spec["binding"] = "b" * 64
        self.run_program("pause")
        self.assertFalse(self.row["State"]["Running"])
        second = self.spec["binding"]
        self.spec["binding"] = original
        with self.assertRaises(ValueError):
            self.run_program("resume")
        self.assertFalse(self.row["State"]["Running"])
        self.spec["binding"] = second
        self.run_program("resume")
        self.assertTrue(self.row["State"]["Running"])
        self.spec["binding"] = original
        with self.assertRaises(ValueError):
            self.run_program("pause")
        self.assertEqual(self.effects, ["stop", "start", "stop", "start"])

    def test_historical_receipt_survives_container_retirement_without_docker_effect(self) -> None:
        self.run_program("pause")
        scope: dict[str, Any] = {}
        exec(WRITER_RECEIPT_LIBRARY, scope)
        for action, completed in (("receipt_pause", True), ("receipt_resume", False)):
            with self.subTest(action=action):
                output = io.StringIO()
                with (
                    patch(
                        "subprocess.run",
                        side_effect=AssertionError("Historical reads cannot run Docker"),
                    ),
                    redirect_stdout(output),
                ):
                    if completed:
                        scope["read_writer_receipt"](
                            {**self.spec, "action": action}, base=self.root / "writers"
                        )
                    else:
                        with self.assertRaises(ValueError):
                            scope["read_writer_receipt"](
                                {**self.spec, "action": action}, base=self.root / "writers"
                            )
                if completed:
                    evidence = extract_odoo_post_deploy_readback_markers(
                        {"logs": output.getvalue()}
                    )
                    self.assertEqual(evidence[WRITER_BINDING_MARKER], self.spec["binding"])
        self.assertEqual(self.effects, ["stop"])

    def test_container_identity_change_or_missing_terminal_witness_refuses_adoption(self) -> None:
        with self.assertRaises(ValueError):
            self.run_program("receipt_pause")
        original = self.docker

        def fail_readback(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args[1] == "inspect" and self.effects:
                raise OSError("reply lost before host completion witness")
            return original(args, **kwargs)

        with patch.object(self, "docker", side_effect=fail_readback):
            with self.assertRaises(OSError):
                self.run_program("pause")
        with self.assertRaises(ValueError):
            self.run_program("receipt_pause")
        self.row["Image"] = "intervening-image"
        with self.assertRaises(ValueError):
            self.run_program("resume")
        self.assertEqual(self.effects, ["stop"])

    def read_fence(self) -> str:
        scope: dict[str, Any] = {}
        exec(WRITER_RECEIPT_LIBRARY, scope)
        output = io.StringIO()
        with (
            patch("subprocess.run", side_effect=AssertionError("A fence read is read-only")),
            redirect_stdout(output),
        ):
            scope["read_writer_receipt"](
                {**self.spec, "action": "receipt_fenced"}, base=self.root / "writers"
            )
        return extract_odoo_post_deploy_readback_markers({"logs": output.getvalue()})[
            WRITER_BINDING_MARKER
        ]

    def test_unwitnessed_operation_fence_refuses_its_late_dispatch_only(self) -> None:
        with patch.object(self, "docker", side_effect=AssertionError("Fencing never runs Docker")):
            self.run_program("fence")
            self.run_program("fence")
        self.assertEqual(self.read_fence(), self.spec["binding"])
        for late in ("pause", "resume", "receipt_pause"):
            with self.subTest(late=late), self.assertRaises(ValueError):
                self.run_program(late)
        self.assertTrue(self.row["State"]["Running"])
        self.assertEqual(self.effects, [])
        self.spec["binding"] = "b" * 64
        with self.assertRaises(FileNotFoundError):
            self.read_fence()
        self.run_program("pause")
        self.run_program("resume")
        self.assertEqual(self.effects, ["stop", "start"])

    def test_witnessed_operation_is_never_fenced(self) -> None:
        original = self.docker

        def lose_reply(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
            if args[1] == "inspect" and self.effects:
                raise OSError("reply lost after the original stop")
            return original(args, **kwargs)

        with patch.object(self, "docker", side_effect=lose_reply):
            with self.assertRaises(OSError):
                self.run_program("pause")
        with self.assertRaises(ValueError):
            self.run_program("fence")
        with self.assertRaises(FileNotFoundError):
            self.read_fence()
        self.assertFalse(self.row["State"]["Running"])
        self.assertEqual(self.effects, ["stop"])

    def test_active_writer_job_prevents_its_fence(self) -> None:
        original = self.docker
        refused: list[type[BaseException]] = []

        def fence_while_stopping(
            args: list[str], **kwargs: Any
        ) -> subprocess.CompletedProcess[str]:
            if args[1] == "stop":
                scope: dict[str, Any] = {}
                exec(WRITER_LIBRARY, scope)
                try:
                    with redirect_stdout(io.StringIO()):
                        scope["coordinate_writer"](
                            {**self.spec, "action": "fence"}, base=self.root / "writers"
                        )
                except BlockingIOError as error:
                    refused.append(type(error))
            return original(args, **kwargs)

        with patch.object(self, "docker", side_effect=fence_while_stopping):
            self.run_program("pause")
        self.assertEqual(refused, [BlockingIOError])
        with self.assertRaises(ValueError):
            self.run_program("fence")
        self.run_program("receipt_pause")


class RecipientExecutionFenceTests(unittest.TestCase):
    def test_durable_tombstone_rejects_queued_worker_after_idle_lock(self) -> None:
        with TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            spec = {"namespace": "lp-prod-recipient-" + "b" * 32, "binding": "c" * 64}
            effects: list[str] = []
            namespace = {"effects": effects}
            program = fenced_program(
                "effects.append('provider effect')",
                spec["namespace"],
                spec["binding"],
                base=str(root),
            )
            exec(compile(program, "<before>", "exec"), namespace)
            scope: dict[str, Any] = {}
            exec(HOST_FENCE_LIBRARY, scope)
            with scope["recipient_execution"](spec, retire=True, base=root):
                pass
            with self.assertRaises(ValueError):
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
