"""The bounded edge client binds review identity and never repeats an uncertain call."""

from contextlib import redirect_stdout
import http.client
import io
import json
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch


class TestingPublicIngressClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/prepare-testing-public-ingress.py")
        )
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.helper = self.root / "launchplane-write-action.py"
        self.helper.touch()
        self.evidence = self.root / "review.json"
        self.receipt: dict[str, Any] = {
            "status": "accepted",
            "trace_id": "fixture-trace",
            "result": {
                "product": "example",
                "context": "example",
                "instance": "testing",
                "operation_id": "original-generation",
                "plan_sha256": "a" * 64,
                "applied": False,
            },
            "private_debug": "fixture-private-value",
        }
        self.request = Mock(return_value=self.receipt)
        self.transport = {
            "resolve_settings": lambda _: {
                "service_url": "https://service.invalid",
                "token": "fixture-private-token",
            },
            "validate_service_url": lambda _: None,
            "request_launchplane": self.request,
            "public_trace_id": lambda value: value,
        }

    def call(self, mode: str) -> tuple[int, dict[str, Any]]:
        output = io.StringIO()
        argv = [
            "client",
            mode,
            "--operator-helper",
            str(self.helper),
            "--product",
            "example",
            "--context",
            "example",
            "--operation-id",
            "original-generation",
            "--reason",
            "fixture edge continuation",
            "--evidence-file",
            str(self.evidence),
        ]
        if mode == "apply":
            argv.extend(("--reviewed-dry-run", "--idempotency-key", "original-edge-key"))
        with (
            patch.object(sys, "argv", argv),
            patch("runpy.run_path", return_value=self.transport),
            redirect_stdout(output),
        ):
            code = self.client["main"]()
        self.assertNotIn("fixture-private", output.getvalue())
        return code, json.loads(output.getvalue())

    def test_review_and_original_key_survive_apply_and_readback(self) -> None:
        self.assertEqual(self.call("dry-run")[0], 0)
        self.assertEqual(self.evidence.stat().st_mode & 0o077, 0)
        original = self.evidence.read_bytes()
        self.receipt["result"]["applied"] = True
        self.assertEqual(self.call("apply")[0], 0)
        self.assertEqual(self.request.call_args.kwargs["idempotency_key"], "original-edge-key")
        self.assertEqual(
            self.request.call_args.kwargs["body"]["reviewed_plan_sha256"],
            self.receipt["result"]["plan_sha256"],
        )
        self.assertEqual(self.call("readback")[0], 0)
        self.assertEqual(self.evidence.read_bytes(), original)

    def test_foreign_review_refuses_before_transport_and_timeout_is_not_retried(self) -> None:
        self.call("dry-run")
        saved = json.loads(self.evidence.read_text())
        saved["request"]["operation_id"] = "foreign-generation"
        self.evidence.write_text(json.dumps(saved))
        self.request.reset_mock()
        self.assertEqual(self.call("apply")[0], 1)
        self.request.assert_not_called()
        saved["request"]["operation_id"] = self.receipt["result"]["operation_id"]
        self.evidence.write_text(json.dumps(saved))
        self.request.side_effect = http.client.IncompleteRead(b"", 1)
        code, result = self.call("apply")
        self.assertEqual(code, 1)
        self.assertEqual(result["status"], "uncertain")
        self.assertEqual(self.request.call_count, 1)
