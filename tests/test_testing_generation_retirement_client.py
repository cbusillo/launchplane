"""The supported client binds review, preserves evidence and never retries uncertainty."""

from contextlib import redirect_stdout
import http.client
import io
import json
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.testing_generation_retirement import TestingGenerationRetirementPlan


class TestingGenerationRetirementClientTests(unittest.TestCase):
    def test_review_apply_readback_and_unknown_outcome(self) -> None:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/retire-testing-generation.py")
        )
        with TemporaryDirectory() as directory:
            helper = Path(directory) / "launchplane-write-action.py"
            helper.touch()
            evidence = Path(directory) / "review.json"
            plan = TestingGenerationRetirementPlan(
                product="example-odoo",
                context="example",
                operation_id="held-operation",
                plan_sha256=canonical_json_sha256({"fixture": "review"}),
                generation_binding_sha256=canonical_json_sha256({"fixture": "generation"}),
            )

            def receipt(applied: bool) -> dict[str, object]:
                return {
                    "status": "accepted",
                    "trace_id": "fixture-trace",
                    "result": plan.model_copy(update={"applied": applied}).model_dump(mode="json"),
                    "private_debug": "not-for-output",
                }

            request = Mock(return_value=receipt(False))
            transport = {
                "resolve_settings": lambda _: {
                    "service_url": "https://service.invalid",
                    "token": "private-token",
                },
                "validate_service_url": lambda _: None,
                "request_launchplane": request,
                "public_trace_id": lambda value: value,
            }

            def call(mode: str, *extra: str) -> tuple[int, dict[str, object]]:
                argv = [
                    "client",
                    mode,
                    "--operator-helper",
                    str(helper),
                    "--product",
                    plan.product,
                    "--context",
                    plan.context,
                    "--operation-id",
                    plan.operation_id,
                    "--reason",
                    "Retain original history",
                    "--evidence-file",
                    str(evidence),
                    *extra,
                ]
                output = io.StringIO()
                with (
                    patch.object(sys, "argv", argv),
                    patch("runpy.run_path", return_value=transport),
                    redirect_stdout(output),
                ):
                    status = client["main"]()
                self.assertNotIn("private-token", output.getvalue())
                self.assertNotIn("not-for-output", output.getvalue())
                return status, json.loads(output.getvalue())

            self.assertEqual(call("dry-run")[0], 0)
            saved = evidence.read_bytes()
            self.assertEqual(evidence.stat().st_mode & 0o777, 0o600)
            request.reset_mock()
            request.return_value = receipt(True)
            self.assertEqual(
                call("apply", "--reviewed-dry-run", "--idempotency-key", "original-key")[0], 0
            )
            self.assertEqual(request.call_count, 2)
            self.assertEqual(request.call_args_list[0].kwargs["idempotency_key"], "original-key")
            self.assertEqual(
                request.call_args_list[0].kwargs["body"]["reviewed_plan_sha256"], plan.plan_sha256
            )
            self.assertEqual(request.call_args_list[1].kwargs["body"]["mode"], "readback")
            self.assertEqual(evidence.read_bytes(), saved)
            request.reset_mock()
            request.side_effect = http.client.IncompleteRead(b"", 1)
            status, outcome = call(
                "apply", "--reviewed-dry-run", "--idempotency-key", "original-key"
            )
            self.assertEqual(status, 1)
            self.assertEqual(outcome["status"], "outcome_unknown")
            self.assertEqual(request.call_count, 1)
            self.assertEqual(evidence.read_bytes(), saved)
