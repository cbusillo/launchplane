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

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.product_testing_restore_policy import (
    TestingRestorePolicyRequest,
    build_testing_restore_policy_plan,
)
from tests.support.profiles import _odoo_profile_payload_with_prod_lane


class TestingRestorePolicyClientTests(unittest.TestCase):
    def call_client(
        self, *, mode: str = "apply", fail: str = "", wrong_review: bool = False
    ) -> tuple[int, dict[str, Any], Mock]:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/prepare-testing-restore-policy.py")
        )
        before = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-site")
        )
        draft = TestingRestorePolicyRequest(
            context=before.lanes[0].context, reason="private-reason"
        )
        plan, after = build_testing_restore_policy_plan(before, draft)
        response = {
            "status": "accepted",
            "trace_id": "fixture-trace",
            "result": plan.model_copy(update={"applied": mode == "apply"}).model_dump(mode="json"),
        }
        post = Mock(return_value=response)
        if fail == "apply":
            post.side_effect = http.client.IncompleteRead(b"", 1)
        read = Mock(return_value={"profile": after.model_dump(mode="json")})
        if fail == "readback":
            read.side_effect = http.client.IncompleteRead(b"", 1)
        transport = {
            "resolve_settings": lambda args: {
                "service_url": "https://service.invalid",
                "token": "fixture-secret",
            },
            "validate_service_url": lambda value: None,
            "request_launchplane": post,
            "request_launchplane_read": read,
            "_product_read_path": lambda operation, **kwargs: "/fixture-profile",
            "public_trace_id": lambda value: value,
        }
        with TemporaryDirectory() as directory:
            helper = Path(directory) / "launchplane-write-action.py"
            helper.touch()
            evidence = Path(directory) / "review.json"
            if mode == "apply":
                evidence.write_text(
                    json.dumps(
                        {
                            "request": {
                                "product": before.product,
                                **draft.model_dump(mode="json"),
                                **({"context": "wrong-lane"} if wrong_review else {}),
                            },
                            "plan": plan.model_dump(mode="json"),
                        }
                    )
                )
            arguments = [
                "prepare-testing-restore-policy.py",
                mode,
                "--operator-helper",
                str(helper),
                "--product",
                before.product,
                "--context",
                draft.context,
                "--reason",
                draft.reason,
                "--evidence-file",
                str(evidence),
            ]
            if mode == "apply":
                arguments.extend(("--reviewed-dry-run", "--idempotency-key", "stable-fixture-key"))
            output = io.StringIO()
            with (
                patch("sys.argv", arguments),
                patch("runpy.run_path", return_value=transport),
                redirect_stdout(output),
            ):
                status = client["main"]()
            text = output.getvalue()
            self.assertNotIn("fixture-secret", text)
            self.assertNotIn(draft.reason, text)
            if mode == "dry-run":
                self.assertEqual(
                    json.loads(evidence.read_text())["plan"], plan.model_dump(mode="json")
                )
                self.assertFalse(evidence.stat().st_mode & 0o077)
            return status, json.loads(text), post

    def test_private_review_and_same_key_apply_require_exact_policy_readback(self) -> None:
        self.assertEqual(self.call_client(mode="dry-run")[0], 0)
        status, output, post = self.call_client()
        self.assertEqual(status, 0)
        self.assertTrue(output["read_back_matches"])
        self.assertFalse(output["restore_started"])
        post.assert_called_once()
        self.assertEqual(post.call_args.kwargs["idempotency_key"], "stable-fixture-key")
        self.assertEqual(
            post.call_args.kwargs["body"]["reviewed_plan_sha256"], output["plan_sha256"]
        )

    def test_lost_apply_and_lost_readback_keep_distinct_outcomes_without_retries(self) -> None:
        for fail, expected in (("apply", "outcome_unknown"), ("readback", "accepted_unverified")):
            with self.subTest(fail=fail):
                status, output, post = self.call_client(fail=fail)
                self.assertEqual(status, 1)
                self.assertEqual(output["status"], expected)
                post.assert_called_once()

    def test_review_for_another_lane_sends_no_apply(self) -> None:
        status, output, post = self.call_client(wrong_review=True)
        self.assertEqual(status, 1)
        self.assertEqual(output["status"], "unverified")
        post.assert_not_called()
