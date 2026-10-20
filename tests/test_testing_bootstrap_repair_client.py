"""The bounded client keeps repair reviews separate from bootstrap custody."""

from contextlib import redirect_stdout
import http.client
import io
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.testing_bootstrap_route_repair import TestingBootstrapRouteRepairPlan
from control_plane.testing_replacement_bootstrap import (
    TestingReplacementBootstrapRequest,
    bootstrap_wire_payload,
)


class TestingBootstrapRepairClientTests(unittest.TestCase):
    def call(
        self, mode: str, *, lost: bool = False, wrong_review: bool = False
    ) -> tuple[int, dict[str, Any], Mock]:
        client = runpy.run_path(
            str(
                Path(__file__).resolve().parents[1]
                / "scripts/prepare-testing-replacement-bootstrap.py"
            )
        )
        draft = TestingReplacementBootstrapRequest(
            context="example",
            operation_id="original-preparation",
            domain_host="fresh.example.invalid",
            certificate_type="letsencrypt",
            reason="original reason",
        )
        plan = TestingBootstrapRouteRepairPlan(
            product="example-odoo",
            context=draft.context,
            operation_id=draft.operation_id,
            generation_binding_sha256="a" * 64,
            plan_sha256="b" * 64,
            certificate_type=draft.certificate_type,
        )
        result = plan.model_copy(
            update={
                "applied": not mode.endswith("dry-run"),
                "route_verified": not mode.endswith("dry-run"),
            }
        ).model_dump(mode="json")
        post = Mock(
            return_value={"status": "accepted", "trace_id": "fixture-trace", "result": result}
        )
        if lost:
            post.side_effect = http.client.IncompleteRead(b"", 1)
        transport = {
            "resolve_settings": lambda args: {
                "service_url": "https://service.invalid",
                "token": "fixture-secret",
            },
            "validate_service_url": lambda value: None,
            "request_launchplane": post,
            "public_trace_id": lambda value: value,
        }
        with TemporaryDirectory() as directory:
            helper = Path(directory) / "launchplane-write-action.py"
            helper.touch()
            evidence = Path(directory) / "repair.json"
            if mode.endswith("apply"):
                evidence.write_text(
                    json.dumps(
                        {
                            "request": {"product": plan.product, **bootstrap_wire_payload(draft)},
                            "plan": {
                                **plan.model_dump(mode="json"),
                                **({"reconcile_requested": False} if wrong_review else {}),
                            },
                        }
                    )
                )
            args = [
                "client",
                mode,
                "--operator-helper",
                str(helper),
                "--product",
                plan.product,
                "--context",
                draft.context,
                "--operation-id",
                draft.operation_id,
                "--domain-host",
                draft.domain_host,
                "--certificate-type",
                draft.certificate_type,
                "--reason",
                draft.reason,
            ]
            if not mode.endswith("readback"):
                args.extend(("--evidence-file", str(evidence)))
            if mode.endswith("apply"):
                args.extend(("--reviewed-dry-run", "--idempotency-key", "separate-repair-key"))
            output = io.StringIO()
            with (
                patch("sys.argv", args),
                patch("runpy.run_path", return_value=transport),
                redirect_stdout(output),
            ):
                status = client["main"]()
            text = output.getvalue()
            self.assertNotIn("fixture-secret", text)
            self.assertNotIn(draft.reason, text)
            if mode.endswith("dry-run"):
                self.assertEqual(
                    json.loads(evidence.read_text())["plan"], plan.model_dump(mode="json")
                )
                self.assertFalse(evidence.stat().st_mode & 0o077)
            return status, json.loads(text), post

    def test_repair_review_apply_and_readback_use_distinct_modes_and_key(self) -> None:
        for mode in ("route-repair-dry-run", "route-repair-apply", "route-repair-readback"):
            with self.subTest(mode=mode):
                status, receipt, post = self.call(mode)
                self.assertEqual(status, 0)
                self.assertFalse(receipt["runtime_verified"])
                self.assertFalse(receipt["bootstrap_prepared"])
                self.assertEqual(post.call_args.kwargs["body"]["mode"], mode)
                if mode.endswith("apply"):
                    self.assertEqual(
                        post.call_args.kwargs["idempotency_key"], "separate-repair-key"
                    )
                    self.assertEqual(
                        post.call_args.kwargs["body"]["reviewed_plan_sha256"],
                        receipt["plan_sha256"],
                    )

    def test_bootstrap_shaped_review_refuses_and_lost_reply_is_never_repeated(self) -> None:
        status, receipt, post = self.call("route-repair-apply", wrong_review=True)
        self.assertEqual(status, 1)
        post.assert_not_called()
        status, receipt, post = self.call("route-repair-apply", lost=True)
        self.assertEqual(status, 1)
        self.assertEqual(receipt["status"], "outcome_unknown")
        post.assert_called_once()
