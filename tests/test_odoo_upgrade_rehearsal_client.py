"""The operator client binds its exact saved review and never replays unknown effects."""

from contextlib import redirect_stdout
from email.message import Message
import http.client
import io
import json
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch
import urllib.error

from control_plane.contracts.odoo_upgrade_rehearsal import (
    OdooUpgradeRehearsalRequest,
    rehearsal_identity,
)


class RehearsalClientTests(unittest.TestCase):
    def call(
        self,
        mode: str,
        *,
        lost: bool = False,
        changed: bool = False,
        failure: tuple[int, str] | None = None,
    ) -> tuple[int, dict[str, Any], Mock]:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/odoo-upgrade-rehearsal.py")
        )
        draft = OdooUpgradeRehearsalRequest.model_validate(
            {
                "source_context": "fixture",
                "source_odoo_version": "1.0",
                "target_artifact_id": "target-artifact",
                "reason": "fixture rehearsal",
                "storage": {
                    "database_bytes": 1024,
                    "filestore_bytes": 1024,
                    "staging_bytes": 4096,
                    "reserve_bytes": 1024,
                },
            }
        )
        identifier = rehearsal_identity("fixture-product", "fixture-principal", "fixture-key")
        result = {
            "product": "fixture-product",
            "rehearsal_id": identifier,
            "state": "planned" if mode == "dry-run" else "prepared",
            "applied": mode == "apply",
            "action": draft.action,
            "plan_sha256": "a" * 64,
            "public_route": False,
            "business_host_frozen": False,
            "resources_preserved": True,
        }
        post = Mock(
            return_value={"status": "accepted", "trace_id": "fixture-trace", "result": result}
        )
        if lost:
            post.side_effect = http.client.IncompleteRead(b"", 1)
        if failure is not None:
            status, code = failure
            post.side_effect = urllib.error.HTTPError(
                "https://service.invalid",
                status,
                "Fixture refusal",
                Message(),
                io.BytesIO(
                    json.dumps({"trace_id": "fixture-trace", "error": {"code": code}}).encode()
                ),
            )
            self.addCleanup(post.side_effect.close)
        transport = {
            "resolve_settings": lambda args: {
                "service_url": "https://service.invalid",
                "token": "private-fixture-token",
            },
            "validate_service_url": lambda url: None,
            "request_launchplane": post,
            "public_trace_id": lambda value: value,
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "launchplane-write-action.py"
            helper.touch()
            payload = root / "intent.json"
            payload.write_text(draft.model_dump_json())
            evidence = root / "review.json"
            if mode == "apply":
                request = draft.model_dump(mode="json", exclude={"mode", "reviewed_plan_sha256"})
                evidence.write_text(
                    json.dumps(
                        {
                            "intent": {
                                "product": "fixture-product",
                                "request": request,
                                "key": "fixture-key",
                            },
                            "plan": {**result, "applied": False},
                        }
                    )
                )
                if changed:
                    payload.write_text(
                        draft.model_copy(
                            update={"target_artifact_id": "another-artifact"}
                        ).model_dump_json()
                    )
            args = [
                "client",
                mode,
                "--product",
                "fixture-product",
                "--payload-file",
                str(payload),
                "--operator-helper",
                str(helper),
                "--evidence-file",
                str(evidence),
                "--idempotency-key",
                "fixture-key",
            ]
            if mode == "apply":
                args += ["--reviewed-dry-run"]
            output = io.StringIO()
            with (
                patch.object(sys, "argv", args),
                patch("runpy.run_path", return_value=transport),
                redirect_stdout(output),
            ):
                code = client["main"]()
            self.assertNotIn("private-fixture-token", output.getvalue())
            if mode == "dry-run" and not lost and failure is None:
                self.assertEqual(
                    json.loads(evidence.read_text())["plan"]["rehearsal_id"], identifier
                )
            return code, json.loads(output.getvalue()), post

    def test_dry_run_saves_its_exact_private_review_without_mutating(self) -> None:
        code, result, post = self.call("dry-run")
        self.assertEqual(code, 0)
        self.assertFalse(post.call_args.kwargs["body"]["mode"] == "apply")
        self.assertEqual(result["status"], "verified")

    def test_changed_artifact_refuses_before_sending_an_apply(self) -> None:
        code, result, post = self.call("apply", changed=True)
        self.assertNotEqual(code, 0)
        post.assert_not_called()

    def test_lost_apply_response_keeps_the_original_key_for_reconciliation(self) -> None:
        code, result, post = self.call("apply", lost=True)
        self.assertNotEqual(code, 0)
        self.assertTrue(result["original_key_required"])
        self.assertEqual(result["status"], "reconciliation_required")
        post.assert_called_once()

    def test_only_terminal_precheck_conflicts_allow_a_new_operation_key(self) -> None:
        for status, code, terminal in (
            (409, "rehearsal_storage_capacity_insufficient", True),
            (409, "rehearsal_precheck_refused", True),
            (409, "rehearsal_state_unverified", False),
            (403, "rehearsal_precheck_refused", False),
        ):
            with self.subTest(status=status, code=code):
                result_code, result, post = self.call("apply", failure=(status, code))
                self.assertNotEqual(result_code, 0)
                self.assertEqual(result["original_key_required"], not terminal)
                self.assertEqual(
                    result["status"], "unavailable" if terminal else "reconciliation_required"
                )
                post.assert_called_once()
