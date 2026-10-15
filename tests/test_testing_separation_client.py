"""The staging client preserves reviewed requests and uncertain keys without retrying writes."""

from contextlib import redirect_stdout
from email.message import Message
import io
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch
from urllib.error import HTTPError


class TestingSeparationClientTests(unittest.TestCase):
    def test_reviewed_apply_reports_uncertain_409_and_never_retries(self) -> None:
        self._exercise_apply("mutation_reconciliation_required", unknown=True)

    def test_pre_effect_conflict_stays_a_refusal(self) -> None:
        self._exercise_apply("testing_separation_stale", unknown=False)

    def _exercise_apply(self, code: str, *, unknown: bool) -> None:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/stage-testing-separation.py")
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "launchplane-write-action.py"
            helper.touch()
            evidence = root / "review.json"
            refused = HTTPError(
                "https://service.invalid",
                409,
                "Conflict",
                Message(),
                io.BytesIO(json.dumps({"error": {"code": code}}).encode()),
            )
            call = Mock(
                side_effect=[
                    {
                        "status": "accepted",
                        "trace_id": "fixture-trace",
                        "result": {
                            "separation_binding_sha256": "a" * 64,
                            "deploy": {"requested": False, "triggered": False},
                        },
                    },
                    refused,
                ]
            )
            emit = Mock()
            transport = {
                "resolve_settings": lambda args: {
                    "service_url": "https://service.invalid",
                    "token": "fixture-token",
                },
                "validate_service_url": lambda value: None,
                "public_trace_id": lambda value: value,
                "request_launchplane": call,
                "emit_http_error_payload": emit,
            }
            common = [
                "--operator-helper",
                str(helper),
                "--product",
                "example",
                "--context",
                "example",
                "--reason",
                "Separate testing.",
                "--evidence-file",
                str(evidence),
            ]
            with (
                patch("runpy.run_path", return_value=transport),
                patch("sys.argv", ["client", "dry-run", *common]),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(client["main"](), 0)
            saved = evidence.read_bytes()
            output = io.StringIO()
            with (
                patch("runpy.run_path", return_value=transport),
                patch(
                    "sys.argv",
                    [
                        "client",
                        "apply",
                        *common,
                        "--reviewed-dry-run",
                        "--idempotency-key",
                        "original-key",
                    ],
                ),
                redirect_stdout(output),
            ):
                self.assertEqual(client["main"](), 1)
            self.assertEqual(call.call_count, 2)
            applied = call.call_args.kwargs
            self.assertEqual(applied["idempotency_key"], "original-key")
            self.assertTrue(applied["body"]["separation_only"])
            self.assertFalse(applied["body"]["deploy"])
            self.assertEqual(evidence.read_bytes(), saved)
            if unknown:
                self.assertTrue(json.loads(output.getvalue())["retain_original_key"])
                emit.assert_not_called()
            else:
                emit.assert_called_once()
                self.assertEqual(
                    json.loads(emit.call_args.kwargs["exc"].read())["error"]["code"], code
                )
