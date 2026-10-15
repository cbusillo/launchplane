"""The supported client retains its review and caller across retries and refusals."""

from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.testing_activation import (
    TestingActivationPlan,
    TestingActivationRequest,
    TestingActivationResult,
)


class ActivationClientTests(unittest.TestCase):
    def test_review_binding_and_replay_preserve_evidence_with_fresh_trace(self) -> None:
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
        result = TestingActivationResult(
            **plan.model_dump(exclude={"applied", "separation_verified"}),
            qualified_at="2026-10-15T00:00:00Z",
        )
        post = Mock(
            side_effect=[
                {
                    "status": "accepted",
                    "trace_id": f"trace-{index}",
                    "result": (plan if index == 0 else result).model_dump(mode="json"),
                }
                for index in range(3)
            ]
        )
        transport: dict[str, Any] = {
            "resolve_settings": lambda args: {
                "service_url": "https://service.invalid",
                "token": "fixture-private-token",
            },
            "validate_service_url": lambda url: None,
            "request_launchplane": post,
            "public_trace_id": lambda trace: trace,
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "launchplane-write-action.py"
            helper.touch()
            request = root / "request.json"
            request.write_text(draft.model_dump_json())
            evidence = root / "review.json"
            for mode in ("dry-run", "apply", "apply"):
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
                if mode == "apply":
                    args.extend(["--reviewed-dry-run", "--idempotency-key", "original-activation"])
                output = io.StringIO()
                with (
                    patch("runpy.run_path", return_value=transport),
                    patch("sys.argv", args),
                    redirect_stdout(output),
                ):
                    self.assertEqual(client["main"](), 0, output.getvalue())
                self.assertNotIn("fixture-private-token", output.getvalue())
            self.assertEqual(evidence.stat().st_mode & 0o077, 0)
            self.assertEqual(
                json.loads(evidence.read_text())["result"], plan.model_dump(mode="json")
            )
            self.assertEqual(
                post.call_args_list[1].kwargs["body"]["reviewed_plan_sha256"], plan.plan_sha256
            )
            request.write_text(
                draft.model_copy(update={"context": "different-lane"}).model_dump_json()
            )
            output = io.StringIO()
            with (
                patch("runpy.run_path", return_value=transport),
                patch("sys.argv", args),
                redirect_stdout(output),
            ):
                self.assertEqual(client["main"](), 1)
            self.assertEqual(post.call_count, 3)
