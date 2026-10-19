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
    def preflight(
        self,
        *,
        evidence: str = "",
        changes: dict[str, Any] | None = None,
        extra_args: tuple[str, ...] = (),
    ) -> tuple[int, dict[str, Any], Mock, Mock]:
        from tests.test_odoo_rehearsal_preflight import observation

        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/odoo-upgrade-rehearsal.py")
        )
        read = Mock(
            return_value={
                "status": "accepted",
                "trace_id": "fixture-preflight-trace",
                "result": {
                    **observation(),
                    "product": "fixture-product",
                    "source_context": "fixture-context",
                    "reader_server_id": "fixture-reader",
                    "source_binding_sha256": "a" * 64,
                    **(changes or {}),
                },
            }
        )
        write = Mock(side_effect=AssertionError("Preflight must use only the GET transport"))
        transport = {
            "resolve_settings": lambda args: {
                "service_url": "https://service.invalid",
                "token": "private-fixture-token",
            },
            "validate_service_url": lambda url: None,
            "request_launchplane_read": read,
            "request_launchplane": write,
            "public_trace_id": lambda value: value,
        }
        with TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "launchplane-write-action.py"
            helper.touch()
            args = [
                "client",
                "preflight",
                "--product",
                "fixture-product",
                "--source-context",
                "fixture-context",
                "--operator-helper",
                str(helper),
            ]
            destination = root / "preflight.json"
            if evidence:
                args.extend(("--evidence-file", str(destination)))
                if evidence == "existing":
                    destination.write_text("preserved evidence")
                if evidence == "raced":

                    def concurrent_evidence(**kwargs: Any) -> dict[str, Any]:
                        destination.write_text("preserved concurrent evidence")
                        return dict(read.return_value)

                    read.side_effect = concurrent_evidence
            args.extend(extra_args)
            output = io.StringIO()
            with (
                patch.object(sys, "argv", args),
                patch("runpy.run_path", return_value=transport),
                redirect_stdout(output),
            ):
                code = client["main"]()
            result = json.loads(output.getvalue())
            self.assertNotIn("private-fixture-token", output.getvalue())
            if evidence == "new" and code == 0:
                self.assertEqual(json.loads(destination.read_text()), result)
                self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
            elif evidence == "raced":
                self.assertEqual(destination.read_text(), "preserved concurrent evidence")
            else:
                self.assertFalse(destination.exists())
            self.assertEqual(
                set(root.iterdir()), {helper, destination} if destination.exists() else {helper}
            )
        return code, result, read, write

    def test_preflight_uses_only_the_read_transport_with_optional_private_evidence(self) -> None:
        from control_plane.contracts.odoo_upgrade_rehearsal import (
            ODOO_UPGRADE_REHEARSAL_PREFLIGHT_ROUTE,
        )

        for evidence in ("", "new"):
            with self.subTest(evidence=evidence):
                code, result, read, write = self.preflight(evidence=evidence)
                self.assertEqual(code, 0)
                self.assertEqual(result["status"], "verified")
                self.assertEqual(result["trace_id"], "fixture-preflight-trace")
                self.assertEqual(
                    read.call_args.kwargs["query"], {"source_context": "fixture-context"}
                )
                self.assertEqual(
                    read.call_args.kwargs["path"],
                    ODOO_UPGRADE_REHEARSAL_PREFLIGHT_ROUTE.format(product="fixture-product"),
                )
                self.assertNotIn("idempotency_key", read.call_args.kwargs)
                self.assertNotIn("body", read.call_args.kwargs)
                write.assert_not_called()

    def test_preflight_refuses_foreign_or_incomplete_observations_before_saving_evidence(
        self,
    ) -> None:
        cases: tuple[dict[str, Any], ...] = (
            {"product": "another-product"},
            {"source_context": "another-context"},
            {"backing_groups": []},
        )
        for changes in cases:
            with self.subTest(changes=changes):
                code, result, read, write = self.preflight(evidence="new", changes=changes)
                self.assertNotEqual(code, 0)
                self.assertEqual(result["status"], "unavailable")
                self.assertFalse(result["original_key_required"])
                read.assert_called_once()
                write.assert_not_called()

    def test_preflight_rejects_existing_evidence_and_rehearsal_keys(self) -> None:
        for kwargs in (
            {"evidence": "existing"},
            {"extra_args": ("--idempotency-key", "forbidden-key")},
            {"extra_args": ("--payload-file", "/nonexistent/intent.json")},
        ):
            with self.subTest(kwargs=kwargs), self.assertRaises(SystemExit):
                self.preflight(**kwargs)

    def test_preflight_preserves_evidence_created_while_the_read_is_in_progress(self) -> None:
        code, result, read, write = self.preflight(evidence="raced")
        self.assertNotEqual(code, 0)
        self.assertEqual(result["status"], "unavailable")
        read.assert_called_once()
        write.assert_not_called()

    def call(
        self,
        mode: str,
        *,
        lost: bool = False,
        changed: bool = False,
        failure: tuple[int, dict[str, Any]] | None = None,
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
            status, failure_payload = failure
            post.side_effect = urllib.error.HTTPError(
                "https://service.invalid",
                status,
                "Fixture refusal",
                Message(),
                io.BytesIO(json.dumps(failure_payload).encode()),
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
        for mode in ("apply", "readback"):
            with self.subTest(mode=mode):
                code, result, post = self.call(mode, lost=True)
                self.assertNotEqual(code, 0)
                self.assertTrue(result["original_key_required"])
                post.assert_called_once()
