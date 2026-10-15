"""The bounded client preserves reviewed intent and never retries an uncertain apply."""

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

from control_plane.testing_replacement_bootstrap import TestingReplacementBootstrapPlan
from tests.test_odoo_testing_restore import generation


class TestingBootstrapClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = runpy.run_path(
            str(
                Path(__file__).resolve().parents[1]
                / "scripts/prepare-testing-replacement-bootstrap.py"
            )
        )
        self.temporary = TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.helper = self.root / "launchplane-write-action.py"
        self.helper.touch()
        self.evidence = self.root / "review.json"
        self.gen = generation()
        self.plan = TestingReplacementBootstrapPlan(
            product=self.gen.product,
            context=self.gen.context,
            operation_id=self.gen.operation_id,
            domain_host=self.gen.domain_host,
            plan_sha256=self.gen.bootstrap_plan_sha256,
            resources_binding_sha256=self.gen.resources_binding_sha256,
        )
        self.request = Mock(return_value=self.receipt(self.plan))
        self.transport = {
            "resolve_settings": lambda _: {
                "service_url": "https://service.invalid",
                "token": "private-token",
            },
            "validate_service_url": lambda _: None,
            "request_launchplane": self.request,
            "public_trace_id": lambda value: value,
        }

    def receipt(self, plan: TestingReplacementBootstrapPlan) -> dict[str, object]:
        return {
            "status": "accepted",
            "trace_id": "fixture-trace",
            "result": plan.model_dump(mode="json"),
            "private_debug": "private-value",
        }

    def call(self, mode: str, *extra: str) -> tuple[int, dict[str, object]]:
        argv = [
            "client",
            mode,
            "--operator-helper",
            str(self.helper),
            "--product",
            self.gen.product,
            "--context",
            self.gen.context,
            "--operation-id",
            self.gen.operation_id,
            "--reason",
            "fixture copy",
            "--domain-host",
            self.gen.domain_host,
            "--evidence-file",
            str(self.evidence),
            *extra,
        ]
        output = io.StringIO()
        with (
            patch.object(sys, "argv", argv),
            patch("runpy.run_path", return_value=self.transport),
            redirect_stdout(output),
        ):
            status = self.client["main"]()
        self.assertNotIn("private-token", output.getvalue())
        self.assertNotIn("private-value", output.getvalue())
        return status, json.loads(output.getvalue())

    def test_private_review_is_bound_to_apply_and_actual_generation_receipt(self) -> None:
        self.assertEqual(self.call("dry-run")[0], 0)
        self.assertEqual(self.evidence.stat().st_mode & 0o777, 0o600)
        saved = self.evidence.read_bytes()
        self.request.return_value = self.receipt(
            self.plan.model_copy(update={"applied": True, "reconcile_requested": True})
        )
        status, result = self.call(
            "apply", "--reviewed-dry-run", "--idempotency-key", "original-key"
        )
        self.assertEqual(status, 0)
        self.assertFalse(result["runtime_verified"])
        self.assertEqual(self.evidence.read_bytes(), saved)
        self.assertEqual(
            self.request.call_args.kwargs["body"]["reviewed_plan_sha256"], self.plan.plan_sha256
        )
        self.assertEqual(self.request.call_args.kwargs["idempotency_key"], "original-key")

    def test_uncertain_apply_is_not_repeated_and_mismatched_receipt_stays_unverified(self) -> None:
        self.call("dry-run")
        for response, expected in (
            (http.client.IncompleteRead(b"", 1), "outcome_unknown"),
            (
                self.receipt(
                    self.plan.model_copy(
                        update={
                            "applied": True,
                            "reconcile_requested": True,
                            "plan_sha256": "e" * 64,
                        }
                    )
                ),
                "accepted_unverified",
            ),
        ):
            with self.subTest(expected=expected):
                self.request.reset_mock()
                if isinstance(response, Exception):
                    self.request.side_effect = response
                else:
                    self.request.side_effect = None
                    self.request.return_value = response
                status, result = self.call(
                    "apply", "--reviewed-dry-run", "--idempotency-key", "original-key"
                )
                self.assertEqual(status, 1)
                self.assertEqual(result["status"], expected)
                self.assertEqual(self.request.call_count, 1)

    def test_foreign_review_refuses_before_transport(self) -> None:
        self.call("dry-run")
        saved = json.loads(self.evidence.read_text())
        saved["request"]["domain_host"] = "different.example.invalid"
        self.evidence.write_text(json.dumps(saved))
        self.request.reset_mock()
        status, _ = self.call("apply", "--reviewed-dry-run", "--idempotency-key", "original-key")
        self.assertEqual(status, 1)
        self.request.assert_not_called()
