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

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.testing_replacement_resources import (
    resource_wire_payload,
    TestingInitialPreparationRelease,
    TestingReplacementResourcesPlan,
    TestingReplacementResourcesRequest,
)


class TestingReplacementResourceClientTests(unittest.TestCase):
    def call(
        self,
        *,
        mode: str = "apply",
        lost: bool = False,
        wrong_review: bool = False,
        verified: bool = True,
        initial_generation: bool = False,
        released: bool = False,
        wrong_receipt: bool = False,
    ) -> tuple[int, dict[str, Any], Mock]:
        client = runpy.run_path(
            str(
                Path(__file__).resolve().parents[1]
                / "scripts/prepare-testing-replacement-resources.py"
            )
        )
        draft = TestingReplacementResourcesRequest(
            context="example",
            operation_id="" if initial_generation else "retained",
            reason="private-reason",
            initial_generation=initial_generation,
        )
        plan = TestingReplacementResourcesPlan(
            product="example-odoo",
            context=draft.context,
            instance="testing",
            operation_id="service-initial-preparation"
            if initial_generation
            else draft.operation_id,
            plan_sha256=canonical_json_sha256({"fixture": "review"}),
            retained_target_preserved=True,
            fresh_storage_required=True,
            published_host_ports=False,
            host_ssh_mounts=False,
            restore_started=False,
            applied=False,
        )
        binding = canonical_json_sha256({"fixture": "allocation"})
        if mode == "release" or released:
            result = TestingInitialPreparationRelease(
                product=plan.product, context=plan.context, operation_id=plan.operation_id
            ).model_dump(mode="json")
        elif mode == "readback":
            result = {
                "product": plan.product,
                "context": plan.context,
                "instance": "testing",
                "operation_id": plan.operation_id,
                "resources_verified": verified,
                "retained_target_preserved": True,
                "production_resources": "separation_ready",
                "restore_started": False,
                "resources_binding_sha256": binding,
                "binding_sha256": binding,
                "schedule_id": "fixture-schedule",
            }
        else:
            result = plan.model_copy(update={"applied": mode == "apply"}).model_dump(mode="json")
            if mode == "apply":
                result.update(
                    resources_verified=verified,
                    resources_binding_sha256=binding,
                    schedule_id="fixture-schedule",
                )
        if wrong_receipt:
            result["operation_id"] = "another-preparation"
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
            evidence = Path(directory) / "review.json"
            if mode in {"apply", "release"}:
                evidence.write_text(
                    json.dumps(
                        {
                            "request": {
                                "product": plan.product,
                                **resource_wire_payload(draft),
                                **({"operation_id": "another-operation"} if wrong_review else {}),
                            },
                            "plan": plan.model_dump(mode="json"),
                        }
                    )
                )
            arguments = [
                "prepare-testing-replacement-resources.py",
                mode,
                "--operator-helper",
                str(helper),
                "--product",
                plan.product,
                "--context",
                draft.context,
                "--operation-id",
                draft.operation_id,
                "--reason",
                draft.reason,
            ]
            if initial_generation:
                arguments.extend(("--initial-generation", "--idempotency-key", "original-key"))
            if mode != "readback":
                arguments.extend(("--evidence-file", str(evidence)))
            if mode in {"apply", "release"}:
                arguments.extend(("--reviewed-dry-run", "--idempotency-key", "original-key"))
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

    def test_reviewed_apply_and_separate_readback_are_bound_and_value_free(self) -> None:
        self.assertEqual(self.call(mode="dry-run")[0], 0)
        status, output, post = self.call()
        self.assertEqual(status, 0)
        self.assertFalse(output["restore_started"])
        self.assertEqual(post.call_args.kwargs["idempotency_key"], "original-key")
        self.assertEqual(
            post.call_args.kwargs["body"]["reviewed_plan_sha256"], output["plan_sha256"]
        )
        self.assertEqual(self.call(mode="readback")[0], 0)

    def test_lost_reply_and_false_receipt_are_unverified_without_retry(self) -> None:
        for changes, outcome in (
            ({"lost": True}, "outcome_unknown"),
            ({"verified": False}, "accepted_unverified"),
        ):
            with self.subTest(changes=changes):
                status, output, post = self.call(
                    lost=changes.get("lost", False), verified=changes.get("verified", True)
                )
                self.assertEqual(status, 1)
                self.assertEqual(output["status"], outcome)
                post.assert_called_once()
        self.assertEqual(self.call(mode="readback", verified=False)[0], 1)

    def test_review_for_another_operation_sends_nothing(self) -> None:
        status, output, post = self.call(wrong_review=True)
        self.assertEqual(status, 1)
        self.assertEqual(output["status"], "unverified")
        post.assert_not_called()

    def test_initial_plan_apply_readback_keep_the_original_key_and_service_identity(self) -> None:
        for mode in ("dry-run", "apply", "readback", "release"):
            with self.subTest(mode=mode):
                status, output, post = self.call(mode=mode, initial_generation=True)
                self.assertEqual(status, 0)
                self.assertEqual(output["operation_id"], "service-initial-preparation")
                self.assertTrue(post.call_args.kwargs["body"]["initial_generation"])
                self.assertTrue(post.call_args.kwargs["idempotency_key"])

    def test_replayed_apply_of_a_released_intent_reports_released(self) -> None:
        status, result, post = self.call(initial_generation=True, released=True)
        self.assertEqual(status, 1)
        self.assertEqual(result["status"], "released")
        self.assertFalse(result["resources_verified"])
        post.assert_called_once()

    def test_release_rejects_a_receipt_for_another_reviewed_preparation(self) -> None:
        status, result, post = self.call(
            mode="release", initial_generation=True, wrong_receipt=True
        )
        self.assertEqual(status, 1)
        self.assertEqual(result["status"], "accepted_unverified")
        post.assert_called_once()
