from contextlib import redirect_stdout
from email.message import Message
import io
import http.client
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
import urllib.error
import urllib.request
from unittest.mock import Mock, patch

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
)
from control_plane.runtime_platform_credentials import PLATFORM_CREDENTIAL_KEYS
from tests.support.profiles import _odoo_profile_payload_with_prod_lane


class PreviewOverrideClientTests(unittest.TestCase):
    def test_private_files_refuse_other_checkouts_and_expanded_symlinks(self) -> None:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/set-preview-override.py")
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            checkout = root / "another-checkout"
            checkout.mkdir()
            (checkout / ".git").touch()
            value = root / "value"
            value.touch(mode=0o600)
            link = root / "link"
            link.symlink_to(value)
            with self.assertRaises(client["PreviewOverrideRefused"]):
                client["_private_file"](checkout / "value", root / "this-checkout")
            with (
                patch.object(Path, "expanduser", return_value=link),
                self.assertRaises(client["PreviewOverrideRefused"]),
            ):
                client["_private_file"](Path("~/link"), root / "this-checkout")

    def test_platform_credential_refusal_returns_safe_json_without_transport(self) -> None:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/set-preview-override.py")
        )
        with TemporaryDirectory() as directory:
            root = Path(directory)
            value = root / "value"
            value.write_text("private-fixture-value")
            value.chmod(0o600)
            args = [
                "set-preview-override.py",
                "dry-run",
                "--product",
                "fixture-product",
                "--key",
                sorted(PLATFORM_CREDENTIAL_KEYS)[0],
                "--reason",
                "fixture-reason",
                "--value-file",
                str(value),
                "--evidence-file",
                str(root / "evidence"),
                "--operator-helper",
                str(root / "launchplane-write-action.py"),
            ]
            output = io.StringIO()
            with patch("sys.argv", args), redirect_stdout(output):
                self.assertEqual(client["main"](), 1)
            self.assertEqual(
                json.loads(output.getvalue())["reason"], "runtime_platform_credential_refused"
            )
            self.assertNotIn("private-fixture-value", output.getvalue())

    def _exercise(self, scenario: str = "") -> tuple[str, Mock]:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/set-preview-override.py")
        )
        before = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane()
        ).model_dump(mode="json")
        before["preview"]["override_env"] = {"ODOO_INSTALL_MODULES": "fixture-addon"}
        if scenario == "historical-transport":
            before["preview"]["historical_transport"] = {"data_transport_mode": "legacy"}
        if scenario == "opaque-revision":
            before["future_profile_field"] = {"preserve": "server-owned"}
        after = json.loads(json.dumps(before))
        after["preview"]["override_env"]["ODOO_PROXY_MODE"] = "private-fixture-value"
        etag = '"server-fixture-revision"'
        transport = {
            "resolve_settings": lambda args: {
                "service_url": "https://example.invalid",
                "token": "private-fixture-token",
            },
            "validate_service_url": lambda value: None,
            "emit_http_error_payload": lambda **kwargs: print(
                json.dumps({"status": "denied", "trace_id": "fixture-trace"})
            ),
        }
        network = Mock(
            side_effect=[
                ({"profile": before}, etag),
                ({"profile": before}, etag),
                ({"status": "accepted"}, ""),
                ({"profile": after}, etag),
            ]
        )
        if scenario == "stale":
            changed = json.loads(json.dumps(before))
            changed["display_name"] = "Concurrent change"
            changed_etag = '"concurrent-server-revision"'
            network.side_effect = [
                ({"profile": before}, etag),
                ({"profile": changed}, changed_etag),
            ]
        elif scenario == "unsupported-service":
            network.side_effect = [({"profile": before}, etag), ({"profile": before}, "")]
        elif scenario in {"weak-revision", "multiple-revisions"}:
            revision = 'W/"fixture"' if scenario == "weak-revision" else '"first", "second"'
            network.side_effect = [({"profile": before}, etag), ({"profile": before}, revision)]
        elif scenario in {"rollback-readback", "already-current"}:
            if scenario == "rollback-readback":
                after["future_profile_field"] = "unrelated-service-change"
                after["preview"]["enabled"] = False
            network.side_effect = [
                ({"profile": before}, etag),
                ({"profile": after}, '"after-revision"' if scenario == "already-current" else ""),
            ]
        elif scenario == "lost-apply":
            network.side_effect = [
                ({"profile": before}, etag),
                ({"profile": before}, etag),
                http.client.IncompleteRead(b"", 1),
            ]
        elif scenario == "lost-readback":
            network.side_effect = [
                ({"profile": before}, etag),
                ({"profile": before}, etag),
                ({"status": "accepted"}, ""),
                http.client.IncompleteRead(b"", 1),
            ]
        elif scenario == "denied-readback":
            network.side_effect = [
                ({"profile": before}, etag),
                ({"profile": before}, etag),
                ({"status": "accepted"}, ""),
                urllib.error.HTTPError("https://example.invalid", 403, "denied", Message(), None),
            ]
        elif scenario == "intermediary-timeout":
            network.side_effect = [
                ({"profile": before}, etag),
                ({"profile": before}, etag),
                urllib.error.HTTPError("https://example.invalid", 408, "timeout", Message(), None),
            ]
        elif scenario in {"precondition-refused", "server-error"}:
            code = 412 if scenario == "precondition-refused" else 503
            network.side_effect = [
                ({"profile": before}, etag),
                ({"profile": before}, etag),
                urllib.error.HTTPError("https://example.invalid", code, "fixture", Message(), None),
            ]
        with TemporaryDirectory() as directory:
            root = Path(directory)
            helper = root / "launchplane-write-action.py"
            helper.touch()
            value = root / "value.txt"
            value.write_text("private-fixture-value")
            value.chmod(0o600)
            evidence = root / "review.json"
            args = [
                "set-preview-override.py",
                "dry-run",
                "--product",
                before["product"],
                "--key",
                "ODOO_PROXY_MODE",
                "--value-file",
                str(value),
                "--evidence-file",
                str(evidence),
                "--reason",
                "fixture-reason",
                "--operator-helper",
                str(helper),
            ]
            output = io.StringIO()
            with (
                patch.dict(client["main"].__globals__, {"_request": network}),
                patch("runpy.run_path", return_value=transport),
                redirect_stdout(output),
            ):
                with patch("sys.argv", args):
                    if scenario == "historical-transport":
                        self.assertEqual(client["main"](), 1)
                        self.assertFalse(evidence.exists())
                        self.assertEqual(network.call_count, 1)
                        return output.getvalue(), network
                    self.assertEqual(client["main"](), 0)
                if scenario == "tampered":
                    saved = json.loads(evidence.read_text())
                    saved["after"]["display_name"] = "Unexpected field"
                    evidence.write_text(json.dumps(saved))
                args[1] = "readback" if scenario == "rollback-readback" else "apply"
                args.extend(("--reviewed-dry-run", "--idempotency-key", "fixture-key"))
                with patch("sys.argv", args):
                    self.assertEqual(
                        client["main"](),
                        0
                        if scenario
                        in {"", "opaque-revision", "rollback-readback", "already-current"}
                        else 1,
                    )
            posts = [call for call in network.call_args_list if call.args[2] == "POST"]
            self.assertEqual(
                len(posts),
                0
                if scenario
                in {
                    "stale",
                    "tampered",
                    "unsupported-service",
                    "weak-revision",
                    "multiple-revisions",
                    "rollback-readback",
                    "already-current",
                }
                else 1,
            )
            if posts:
                self.assertEqual(posts[0].kwargs["body"], after)
                self.assertEqual(posts[0].kwargs["revision"], etag)
            self.assertFalse(evidence.stat().st_mode & 0o077)
            self.assertNotIn("private-fixture-value", output.getvalue())
            self.assertNotIn("private-fixture-token", output.getvalue())
            if scenario in {"rollback-readback", "already-current"}:
                self.assertFalse(json.loads(output.getvalue().splitlines()[-1])["applied"])
            return output.getvalue(), network

    def test_reviewed_save_changes_only_one_preview_key_without_echoing_the_value(self) -> None:
        self._exercise()
        self._exercise("opaque-revision")

    def test_readback_survives_service_rollback_and_unrelated_profile_changes(self) -> None:
        self._exercise("rollback-readback")
        self._exercise("already-current")

    def test_historical_transport_is_refused_before_review_is_created(self) -> None:
        output, _ = self._exercise("historical-transport")
        self.assertEqual(json.loads(output)["reason"], "unsupported_preview_transport_history")

    def test_changed_profile_or_tampered_review_sends_no_write(self) -> None:
        for scenario, reason in (
            ("stale", "profile_changed_since_review"),
            ("tampered", "review_changes_other_fields"),
            ("unsupported-service", "conditional_save_unsupported"),
            ("weak-revision", "conditional_save_unsupported"),
            ("multiple-revisions", "conditional_save_unsupported"),
        ):
            with self.subTest(scenario=scenario):
                output, _ = self._exercise(scenario)
                result = json.loads(output.splitlines()[-1])
                self.assertEqual(result["status"], "unverified")
                self.assertEqual(result["reason"], reason)

    def test_uncertain_apply_and_readback_are_not_retried(self) -> None:
        for scenario, status in (
            ("lost-apply", "outcome_unknown"),
            ("lost-readback", "accepted_unverified"),
            ("denied-readback", "accepted_unverified"),
            ("intermediary-timeout", "outcome_unknown"),
            ("server-error", "outcome_unknown"),
        ):
            with self.subTest(scenario=scenario):
                output, _ = self._exercise(scenario)
                self.assertEqual(json.loads(output.splitlines()[-1])["status"], status)

    def test_server_precondition_refusal_is_definite_and_actionable(self) -> None:
        output, _ = self._exercise("precondition-refused")
        result = json.loads(output.splitlines()[-1])
        self.assertEqual(result["status"], "unverified")
        self.assertEqual(result["reason"], "profile_changed_since_review")

    def test_transport_sends_conditional_save_headers_and_reads_revision(self) -> None:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/set-preview-override.py")
        )
        response = Mock()
        response.headers = {"ETag": '"fixture-revision"'}
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        opener = Mock(return_value=response)
        transport = {
            "build_url": lambda base, path: base + path,
            "safe_urlopen": opener,
            "_read_service_response": lambda result: {"status": "accepted"},
        }
        body = {"preview": {"override_env": {"ODOO_PROXY_MODE": "True"}}}
        result, revision = client["_request"](
            transport,
            {"token": "fixture-token", "service_url": "https://example.invalid"},
            "POST",
            "/v1/product-profiles",
            body=body,
            revision='"reviewed-revision"',
            idempotency_key="fixture-idempotency",
        )
        request = opener.call_args.args[0]
        self.assertIsInstance(request, urllib.request.Request)
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(json.loads(request.data), body)
        self.assertEqual(request.get_header("If-match"), '"reviewed-revision"')
        self.assertEqual(request.get_header("Idempotency-key"), "fixture-idempotency")
        self.assertEqual(request.get_header("Authorization"), "Bearer fixture-token")
        self.assertEqual(revision, response.headers["ETag"])
        self.assertEqual(result, {"status": "accepted"})
