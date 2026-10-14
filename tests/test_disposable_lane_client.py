"""The evidence client binds collection to its inspection and suppresses private output."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from control_plane.disposable_lane_collection import disposable_collection_coverage


class DisposableLaneClientTests(unittest.TestCase):
    def call_client(
        self, *, collection_mode: str = "collect", private_fact: bool = False
    ) -> tuple[int, str, Mock]:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/collect-disposable-lane.py")
        )
        facts = {"usage": "unknown", "data": "unknown", "production_resources": "unknown"}
        inspected = {
            "trace_id": "fixture-trace",
            "result": {"mode": "inspect", "binding_sha256": "a" * 64, **facts},
        }
        collected_result: dict[str, object] = {
            "mode": collection_mode,
            "source_coverage": disposable_collection_coverage(),
            **facts,
        }
        collected = {
            "trace_id": "fixture-trace",
            "private_debug": "private-fixture",
            "result": collected_result,
        }
        if private_fact:
            collected_result["usage"] = "private-fixture"
        request = Mock(side_effect=[inspected, collected])
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
