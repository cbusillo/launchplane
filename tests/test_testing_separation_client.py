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
    def test_independent_resources_preserve_review_and_save_a_private_replayable_receipt(
        self,
    ) -> None:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/stage-testing-separation.py")
        )
        from control_plane.testing_separation_resources import (
            SeparationResourcesPlan,
            SeparationResourcesResult,
        )
        from control_plane.workflows.ship import utc_now_timestamp

        plan = SeparationResourcesPlan(
            product="example", context="example", plan_sha256="a" * 64
        ).model_dump(mode="json")
        with TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "launchplane-write-action.py"
            helper.touch()
            evidence = root / "prepare.json"
            completed = SeparationResourcesResult.model_validate(
                {
                    **plan,
                    "applied": True,
                    "resources_verified": True,
                    "volumes": {"database": "private-volume"},
                    "resources_binding_sha256": "b" * 64,
                    "schedule_id": "fixture-schedule",
                    "qualified_at": utc_now_timestamp(),
                }
            ).model_dump(mode="json")
            call = Mock(
                side_effect=[
                    {"status": "accepted", "trace_id": "fixture", "result": plan},
                    {"status": "accepted", "trace_id": "fixture", "result": completed},
                    {"status": "accepted", "trace_id": "fixture", "result": completed},
                    {"status": "accepted", "trace_id": "fixture", "result": completed},
                ]
            )
            transport = {
                "resolve_settings": lambda args: {
                    "service_url": "https://service.invalid",
                    "token": "fixture",
                },
                "validate_service_url": lambda value: None,
                "public_trace_id": lambda value: value,
                "request_launchplane": call,
            }
            common = [
                "--operator-helper",
                str(helper),
                "--product",
                "example",
                "--context",
                "example",
                "--reason",
                "Prepare empty resources.",
                "--evidence-file",
                str(evidence),
            ]
            for mode in (
                "resources-dry-run",
                "resources-apply",
                "resources-apply",
                "resources-read",
            ):
                args = ["client", mode, *common]
                if mode.endswith("apply"):
                    args += ["--reviewed-dry-run", "--idempotency-key", "original-resource-key"]
                elif mode == "resources-read":
                    args += ["--idempotency-key", "original-resource-key"]
                output = io.StringIO()
                with (
                    patch("runpy.run_path", return_value=transport),
                    patch("sys.argv", args),
                    redirect_stdout(output),
                ):
                    self.assertEqual(client["main"](), 0, output.getvalue())
                self.assertNotIn("private-volume", output.getvalue())
            receipt = evidence.with_name(evidence.name + ".receipt.json")
            self.assertEqual(json.loads(receipt.read_text())["result"], completed)
            self.assertEqual(receipt.stat().st_mode & 0o777, 0o600)
            self.assertEqual(json.loads(evidence.read_text())["result"], plan)
            self.assertEqual(
                call.call_args_list[2].kwargs["body"]["reviewed_plan_sha256"], plan["plan_sha256"]
            )

    def test_reviewed_apply_reports_uncertain_409_and_never_retries(self) -> None:
        self._exercise_apply("mutation_reconciliation_required", unknown=True)

    def test_in_progress_write_retains_the_original_key(self) -> None:
        self._exercise_apply("mutation_in_progress", unknown=True)

    def test_pre_effect_conflict_stays_a_refusal(self) -> None:
        self._exercise_apply("testing_separation_stale", unknown=False)

    def _exercise_apply(self, code: str, *, unknown: bool, status_code: int = 409) -> None:
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
                status_code,
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
            reported = []

            def emit_error(**kwargs: object) -> None:
                error = kwargs["exc"]
                assert isinstance(error, HTTPError)
                reported.append(json.loads(error.read())["error"]["code"])

            emit = Mock(side_effect=emit_error)
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
                self.assertEqual(reported, [code])

    def test_staging_hold_read_uses_the_supported_transport_and_keeps_evidence(self) -> None:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/stage-testing-separation.py")
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "launchplane-write-action.py"
            helper.touch()
            evidence = root / "review.json"
            evidence.write_text("existing evidence")
            read = Mock(
                return_value={
                    "trace_id": "fixture-trace",
                    "private_debug": "private-fixture",
                    "result": {
                        "status": "ok",
                        "product": "example",
                        "context": "example",
                        "instance": "testing",
                        "record_sha256": "a" * 64,
                        "hold": {"purpose": "isolation_staging", "reason": "private-fixture"},
                    },
                }
            )
            write = Mock()
            transport = {
                "resolve_settings": lambda args: {
                    "service_url": "https://service.invalid",
                    "token": "fixture-token",
                },
                "validate_service_url": lambda value: None,
                "public_trace_id": lambda value: value,
                "request_launchplane_read": read,
                "request_launchplane": write,
            }
            output = io.StringIO()
            args = [
                "client",
                "hold-read",
                "--operator-helper",
                str(helper),
                "--product",
                "example",
                "--context",
                "example",
            ]
            with (
                patch("runpy.run_path", return_value=transport),
                patch("sys.argv", args),
                redirect_stdout(output),
            ):
                self.assertEqual(client["main"](), 0)
            self.assertEqual(json.loads(output.getvalue())["purpose"], "isolation_staging")
            self.assertNotIn("private-fixture", output.getvalue())
            self.assertEqual(evidence.read_text(), "existing evidence")
            read.assert_called_once()
            write.assert_not_called()

    def test_reviewed_staging_lift_sends_its_purpose_without_deployment(self) -> None:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/stage-testing-separation.py")
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "launchplane-write-action.py"
            helper.touch()
            evidence = root / "lift.json"
            call = Mock(
                side_effect=[
                    {
                        "status": "accepted",
                        "trace_id": "fixture-trace",
                        "result": {"plan_sha256": "a" * 64, "reconcile_requested": False},
                    },
                    {
                        "status": "accepted",
                        "trace_id": "fixture-trace",
                        "result": {"read_back_matches": True, "reconcile_requested": False},
                    },
                ]
            )
            transport = {
                "resolve_settings": lambda args: {
                    "service_url": "https://service.invalid",
                    "token": "fixture-token",
                },
                "validate_service_url": lambda value: None,
                "public_trace_id": lambda value: value,
                "request_launchplane": call,
            }
            common = [
                "--operator-helper",
                str(helper),
                "--product",
                "example",
                "--context",
                "example",
                "--reason",
                "Qualified interlock release.",
                "--evidence-file",
                str(evidence),
            ]
            with (
                patch("runpy.run_path", return_value=transport),
                patch("sys.argv", ["client", "lift-dry-run", *common]),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(client["main"](), 0)
            with (
                patch("runpy.run_path", return_value=transport),
                patch(
                    "sys.argv",
                    [
                        "client",
                        "lift-apply",
                        *common,
                        "--reviewed-dry-run",
                        "--idempotency-key",
                        "original-lift",
                    ],
                ),
                redirect_stdout(io.StringIO()),
            ):
                self.assertEqual(client["main"](), 0)
            sent = call.call_args.kwargs
            self.assertFalse(sent["body"]["hold"])
            self.assertEqual(sent["body"]["purpose"], "isolation_staging")
            self.assertEqual(sent["idempotency_key"], "original-lift")

    def test_server_failure_after_submission_preserves_the_original_key(self) -> None:
        self._exercise_apply("completion_failed", unknown=True, status_code=503)
