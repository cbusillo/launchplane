"""The evidence client binds collection to its inspection and suppresses private output."""

from contextlib import redirect_stdout
import io
import http.client
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from control_plane.disposable_lane_collection import disposable_collection_coverage


class DisposableLaneClientTests(unittest.TestCase):
    def call_client(
        self,
        *,
        collection_mode: str = "collect",
        private_fact: bool = False,
        truncated: bool = False,
        provider_mutation: bool = False,
        binding_sha256: str = "a" * 64,
        usage: str = "unknown",
        reason_code: object = None,
        runtime_facts: bool | None = False,
    ) -> tuple[int, str, Mock]:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/collect-disposable-lane.py")
        )
        facts = {"usage": usage, "data": "unknown", "production_resources": "unknown"}
        inspected = {
            "trace_id": "fixture-trace",
            "result": {"mode": "inspect", "binding_sha256": "a" * 64, **facts},
        }
        collected_result: dict[str, object] = {
            "mode": collection_mode,
            "source_coverage": disposable_collection_coverage(),
            "binding_sha256": binding_sha256,
            "provider_mutation": provider_mutation,
            "odoo_usage": {
                "recent_signins": runtime_facts,
                "authenticated_sessions": runtime_facts,
            },
            **facts,
        }
        if reason_code is not None:
            odoo_usage = collected_result["odoo_usage"]
            assert isinstance(odoo_usage, dict)
            odoo_usage["reason_code"] = reason_code
        collected = {
            "trace_id": "fixture-trace",
            "private_debug": "private-fixture",
            "result": collected_result,
        }
        if private_fact:
            collected_result["usage"] = "private-fixture"
        request = Mock(
            side_effect=[inspected, http.client.IncompleteRead(b"", 1) if truncated else collected]
        )
        transport = {
            "resolve_settings": lambda args: {
                "service_url": "https://service.invalid",
                "token": "fixture-token",
            },
            "validate_service_url": lambda value: None,
            "request_launchplane": request,
            "public_trace_id": lambda value: value,
        }
        with TemporaryDirectory() as directory:
            helper = Path(directory) / "launchplane-write-action.py"
            helper.touch()
            arguments = [
                "collect-disposable-lane.py",
                "collect",
                "--operator-helper",
                str(helper),
                "--product",
                "example",
                "--context",
                "example",
                "--instance",
                "testing",
            ]
            output = io.StringIO()
            with (
                patch("sys.argv", arguments),
                patch("runpy.run_path", return_value=transport),
                redirect_stdout(output),
            ):
                status = client["main"]()
            return status, output.getvalue(), request

    def test_collection_uses_inspected_binding_and_reports_only_safe_facts_and_coverage(
        self,
    ) -> None:
        status, output, request = self.call_client()
        self.assertEqual(status, 0)
        first, second = request.call_args_list
        self.assertEqual(second.kwargs["body"]["expected_binding_sha256"], "a" * 64)
        self.assertEqual(first.kwargs["body"]["mode"], "inspect")
        self.assertEqual(second.kwargs["body"]["mode"], "collect")
        self.assertNotIn("private-fixture", output)
        self.assertNotIn("fixture-token", output)
        self.assertEqual(json.loads(output)["source_coverage"], disposable_collection_coverage())

    def test_unsettled_mode_and_invalid_fact_never_report_collected_evidence(self) -> None:
        for mode, private_fact in (("inspect", False), ("collect", True)):
            with self.subTest(mode=mode, private_fact=private_fact):
                status, output, _ = self.call_client(
                    collection_mode=mode, private_fact=private_fact
                )
                self.assertEqual(status, 1)
                self.assertEqual(json.loads(output)["status"], "unverified")
                self.assertNotIn("private-fixture", output)

    def test_service_collected_non_use_is_reported_by_the_bounded_client(self) -> None:
        status, output, _ = self.call_client(usage="unused")
        self.assertEqual(status, 0)
        self.assertEqual(json.loads(output)["facts"]["usage"], "unused")

    def test_never_served_uses_no_invented_session_or_login_boolean(self) -> None:
        status, output, _ = self.call_client(
            usage="unused", reason_code="never_served", runtime_facts=None
        )
        self.assertEqual(status, 0)
        result = json.loads(output)
        self.assertIsNone(result["odoo_usage"]["recent_signins"])
        self.assertIsNone(result["odoo_usage"]["authenticated_sessions"])
        status, _, _ = self.call_client(usage="unused", reason_code="never_served")
        self.assertEqual(status, 1)

    def test_truncated_or_mismatched_receipt_stays_unverified(self) -> None:
        for truncated, mutation, digest in (
            (True, False, "a" * 64),
            (False, True, "a" * 64),
            (False, False, "b" * 64),
        ):
            with self.subTest(truncated=truncated, mutation=mutation, digest=digest):
                status, output, _ = self.call_client(
                    truncated=truncated, provider_mutation=mutation, binding_sha256=digest
                )
                self.assertEqual(status, 1)
                self.assertEqual(json.loads(output)["status"], "unverified")

    def test_client_reports_bounded_reason_and_marks_older_service_diagnostics_unavailable(
        self,
    ) -> None:
        for reason in (None, "serving_process_absent", "session_store_unavailable"):
            with self.subTest(reason=reason):
                status, output, _ = self.call_client(reason_code=reason)
                self.assertEqual(status, 0)
                self.assertEqual(json.loads(output)["facts"]["usage"], "unknown")
                self.assertEqual(
                    json.loads(output)["odoo_usage"]["reason_code"],
                    reason or "diagnostics_unavailable",
                )
        for invalid_reason in (
            "private-fixture topology",
            ["private-fixture"],
            {"private-fixture": True},
        ):
            with self.subTest(reason=invalid_reason):
                status, output, _ = self.call_client(reason_code=invalid_reason)
                self.assertEqual(status, 1)
                self.assertNotIn("private-fixture", output)

    def test_future_diagnostic_keeps_safe_facts_and_trace_without_echoing_unknown_text(
        self,
    ) -> None:
        for usage in ("unknown", "in_use"):
            with self.subTest(usage=usage):
                status, output, _ = self.call_client(usage=usage, reason_code="future_checkpoint")
                self.assertEqual(status, 0)
                result = json.loads(output)
                self.assertEqual(result["odoo_usage"]["reason_code"], "diagnostics_unrecognized")
                self.assertEqual(result["facts"]["usage"], usage)
                self.assertTrue(result["trace_id"])
                self.assertNotIn("future_checkpoint", output)

    def test_negative_usage_requires_consistent_diagnostic_when_service_supplies_one(self) -> None:
        for usage, reason, expected_status in (
            ("unused", "complete", 0),
            ("unused", None, 0),  # Older services have complete booleans but no diagnostic.
            ("unused", "not_collected", 1),
            ("unused", "future_checkpoint", 1),
            ("unknown", "complete", 1),
        ):
            with self.subTest(usage=usage, reason=reason):
                status, _, _ = self.call_client(usage=usage, reason_code=reason)
                self.assertEqual(status, expected_status)
