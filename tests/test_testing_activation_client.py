"""The supported client retains its review and caller across retries and refusals."""

from contextlib import redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from typing import Any
import unittest
import urllib.error
from email.message import Message
from unittest.mock import Mock, patch

from control_plane.contracts.testing_activation import (
    TestingActivationPlan,
    TestingActivationRequest,
    TestingActivationResult,
    TestingActivationRefusedResult,
)
from control_plane.testing_activation_inspection import TestingActivationInspection


class ActivationClientTests(unittest.TestCase):
    def test_inspection_reads_the_original_key_and_saves_only_diagnostic_evidence(self) -> None:
        self.exercise_review_and_replay(refused=False, inspection=True)

    def test_review_binding_and_replay_preserve_evidence_with_fresh_trace(self) -> None:
        self.exercise_review_and_replay(refused=False)

    def test_refused_apply_readback_and_receipt_preserve_the_negative_result(self) -> None:
        self.exercise_review_and_replay(refused=True)

    def test_explicit_refusal_binds_the_review_and_original_key_without_exposing_values(
        self,
    ) -> None:
        self.exercise_review_and_replay(refused=True, submit_mode="refuse")

    def test_provider_backoff_survives_supported_caller_refusal(self) -> None:
        self.exercise_review_and_replay(refused=False, external_refusal=True)

    def test_invalid_trace_keeps_provider_backoff_and_original_key(self) -> None:
        self.exercise_review_and_replay(refused=False, external_refusal=True, invalid_trace=True)

    def test_original_activation_key_survives_read_transport_and_http_failures(self) -> None:
        for mode in ("receipt", "readback", "inspect"):
            for failure in (
                urllib.error.URLError("private-read-fixture"),
                ValueError("private-response-fixture"),
                urllib.error.HTTPError(
                    "https://service.invalid", 403, "refused", Message(), io.BytesIO(b"{}")
                ),
            ):
                with self.subTest(mode=mode, failure=type(failure)):
                    self.exercise_review_and_replay(
                        refused=False, read_error=failure, read_mode=mode
                    )

    def exercise_review_and_replay(
        self,
        *,
        refused: bool,
        external_refusal: bool = False,
        invalid_trace: bool = False,
        read_error: Exception | None = None,
        read_mode: str = "receipt",
        inspection: bool = False,
        submit_mode: str = "apply",
    ) -> None:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/activate-generic-web-testing.py")
        )
        draft = TestingActivationRequest(
            context="example",
            preparation_key="original-storage",
            interlock_record_sha256="a" * 64,
            source_commit="b" * 40,
            volume_keys={"database": "DB_VOLUME", "config": "CONFIG_VOLUME"},
            port_keys=("DB_PORT", "HTTP_PORT"),
            reason="Isolate testing.",
        )
        plan = TestingActivationPlan(
            product="example-site",
            context="example",
            plan_sha256="c" * 64,
            source_commit=draft.source_commit,
            resource_namespace="fixture-new-recipient",
            image_reference="example@sha256:" + "a" * 64,
            host_ports=(19212, 19213),
        )
        result: (
            TestingActivationRefusedResult | TestingActivationResult | TestingActivationInspection
        ) = (
            TestingActivationRefusedResult(
                **plan.model_dump(), reason="candidate_deployment_failed"
            )
            if refused
            else TestingActivationResult(
                **plan.model_dump(exclude={"applied", "separation_verified"}),
                qualified_at="2026-10-15T00:00:00Z",
            )
        )
        modes: tuple[str, ...] = (
            ("dry-run", submit_mode, submit_mode, "readback", "receipt")
            if refused
            else ("dry-run", "apply", "apply")
        )
        if inspection:
            result = TestingActivationInspection(
                product=plan.product,
                context=plan.context,
                state="completed",
                plan_sha256=plan.plan_sha256,
                activation_phase="refused",
                predecessor_recorded=True,
                external_capability_count=1,
                programs=(),
                inspected_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
            )
            modes = ("dry-run", "inspect")
        post = Mock(
            side_effect=[
                {
                    "status": "accepted",
                    "trace_id": f"trace-{index}",
                    "result": (plan if index == 0 else result).model_dump(mode="json"),
                }
                for index in range(len(modes))
            ]
        )
        if external_refusal:
            from control_plane.discord_identity import DiscordIdentityResult
            from control_plane.contracts.testing_external_resources import (
                DiscordBotResourceObservation,
                DiscordRateLimitEvidence,
            )
            from control_plane.contracts.testing_activation import TestingActivationRefusalCode

            observed = DiscordIdentityResult(
                observation=DiscordBotResourceObservation(
                    started_at="2026-10-23T00:00:00Z",
                    finished_at="2026-10-23T00:00:01Z",
                    http_status=429,
                    rate_limits=(
                        DiscordRateLimitEvidence(
                            phase="guilds", retry_after=120, global_limit=True
                        ),
                    ),
                )
            )
            payload = {
                "error": {"code": TestingActivationRefusalCode.EXTERNAL_CAPABILITY_UNVERIFIED},
                "trace_id": "trace-refusal",
                "records": {"discord_identity": observed.model_dump_json()},
            }
            post.side_effect = [
                post.side_effect.__next__(),
                urllib.error.HTTPError(
                    "https://service.invalid",
                    409,
                    "refused",
                    Message(),
                    io.BytesIO(json.dumps(payload).encode()),
                ),
            ]
            modes = ("dry-run", "apply")
        if read_error is not None:
            post.side_effect = [post.side_effect.__next__(), read_error]
            modes = ("dry-run", read_mode)
        transport: dict[str, Any] = {
            "resolve_settings": lambda args: {
                "service_url": "https://service.invalid",
                "token": "fixture-private-token",
            },
            "validate_service_url": lambda url: None,
            "request_launchplane": post,
            "public_trace_id": lambda trace: trace,
            "emit_http_error_payload": lambda **kwargs: print(
                json.dumps({"status": "refused", "request": kwargs["request"]})
            ),
        }
        if invalid_trace:
            transport["public_trace_id"] = Mock(
                side_effect=["trace-0", ValueError("private-trace-fixture")]
            )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "launchplane-write-action.py"
            helper.touch()
            request = root / "request.json"
            request.write_text(draft.model_dump_json())
            evidence = root / "review.json"
            apply_args = []
            for mode in modes:
                args = [
                    "client",
                    mode,
                    "--operator-helper",
                    str(helper),
                    "--product",
                    "example-site",
                    "--request-file",
                    str(request),
                    "--evidence-file",
                    str(evidence),
                ]
                if mode == submit_mode:
                    args.extend(["--reviewed-dry-run", "--idempotency-key", "original-activation"])
                    apply_args = args
                elif mode in {"receipt", "inspect"} or (
                    read_error is not None and mode == "readback"
                ):
                    args.extend(["--idempotency-key", "original-activation"])
                output = io.StringIO()
                with (
                    patch("runpy.run_path", return_value=transport),
                    patch("sys.argv", args),
                    redirect_stdout(output),
                ):
                    expected_refusal = external_refusal and mode == submit_mode
                    expected_error = read_error is not None and mode == read_mode
                    self.assertEqual(
                        client["main"](), int(expected_refusal or expected_error), output.getvalue()
                    )
                self.assertNotIn("fixture-private-token", output.getvalue())
                if expected_error:
                    returned = json.loads(output.getvalue())
                    self.assertTrue(returned.get("request", returned)["retain_original_key"])
                    if not isinstance(read_error, urllib.error.HTTPError):
                        self.assertEqual(returned["status"], "unverified")
                    self.assertEqual(
                        json.loads(evidence.read_text())["result"], plan.model_dump(mode="json")
                    )
                    if mode == "readback":
                        self.assertNotIn("idempotency_key", post.call_args.kwargs)
                    self.assertEqual(post.call_count, 2)
                    return
                if expected_refusal:
                    returned = json.loads(output.getvalue())
                    self.assertEqual(
                        returned["external_identity"], observed.model_dump(mode="json")
                    )
                    self.assertTrue(returned["retain_original_key"])
                    if invalid_trace:
                        self.assertEqual(returned["trace_id"], "")
                        self.assertNotIn("private-trace-fixture", output.getvalue())
                    return
                if mode != "dry-run":
                    self.assertEqual(
                        json.loads(output.getvalue())["result"], result.model_dump(mode="json")
                    )
            self.assertEqual(evidence.stat().st_mode & 0o077, 0)
            self.assertEqual(
                json.loads(evidence.read_text())["result"], plan.model_dump(mode="json")
            )
            if inspection:
                self.assertEqual(post.call_args.kwargs["idempotency_key"], "original-activation")
                self.assertEqual(post.call_args.kwargs["body"]["mode"], "inspect")
                self.assertEqual(
                    json.loads(evidence.with_name(evidence.name + ".inspect.json").read_text())[
                        "result"
                    ],
                    result.model_dump(mode="json"),
                )
                post.side_effect = None
                post.return_value = {
                    "status": "accepted",
                    "trace_id": "fresh-trace",
                    "result": result.model_dump(mode="json"),
                }
                with (
                    patch("runpy.run_path", return_value=transport),
                    patch("sys.argv", args),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(client["main"](), 1)
                self.assertEqual(post.call_count, len(modes))
                return
            if refused:
                repeated = list(apply_args)
                repeated[1] = "readback"
                post.side_effect = None
                post.return_value = {
                    "status": "accepted",
                    "trace_id": "fresh-trace",
                    "result": result.model_dump(mode="json"),
                }
                with (
                    patch("runpy.run_path", return_value=transport),
                    patch("sys.argv", repeated),
                    redirect_stdout(io.StringIO()),
                ):
                    self.assertEqual(client["main"](), 1)
                self.assertEqual(post.call_count, len(modes))
            self.assertEqual(
                post.call_args_list[1].kwargs["body"]["reviewed_plan_sha256"], plan.plan_sha256
            )
            request.write_text(
                draft.model_copy(update={"context": "different-lane"}).model_dump_json()
            )
            output = io.StringIO()
            with (
                patch("runpy.run_path", return_value=transport),
                patch("sys.argv", apply_args),
                redirect_stdout(output),
            ):
                self.assertEqual(client["main"](), 1)
            self.assertEqual(post.call_count, len(modes))
