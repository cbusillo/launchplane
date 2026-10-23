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
    product_profile_record_sha256,
)
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
                "GITHUB_TOKEN",
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
        after = json.loads(json.dumps(before))
        after["preview"]["override_env"]["ODOO_PROXY_MODE"] = "private-fixture-value"
        etag = f'"{product_profile_record_sha256(LaunchplaneProductProfileRecord.model_validate(before))}"'
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
            changed_etag = f'"{product_profile_record_sha256(LaunchplaneProductProfileRecord.model_validate(changed))}"'
            network.side_effect = [
                ({"profile": before}, etag),
                ({"profile": changed}, changed_etag),
            ]
        elif scenario == "unsupported-service":
            network.side_effect = [({"profile": before}, etag), ({"profile": before}, "")]
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
                args[1] = "apply"
                args.extend(("--reviewed-dry-run", "--idempotency-key", "fixture-key"))
                with patch("sys.argv", args):
                    self.assertEqual(client["main"](), 1 if scenario else 0)
            posts = [call for call in network.call_args_list if call.args[2] == "POST"]
            self.assertEqual(
                len(posts), 0 if scenario in {"stale", "tampered", "unsupported-service"} else 1
            )
            if posts:
                self.assertEqual(posts[0].kwargs["body"], after)
                self.assertEqual(posts[0].kwargs["revision"], etag)
            self.assertFalse(evidence.stat().st_mode & 0o077)
            self.assertNotIn("private-fixture-value", output.getvalue())
            self.assertNotIn("private-fixture-token", output.getvalue())
            return output.getvalue(), network

    def test_reviewed_save_changes_only_one_preview_key_without_echoing_the_value(self) -> None:
        self._exercise()

    def test_historical_transport_is_refused_before_review_is_created(self) -> None:
        output, _ = self._exercise("historical-transport")
        self.assertEqual(json.loads(output)["reason"], "unsupported_preview_transport_history")

    def test_changed_profile_or_tampered_review_sends_no_write(self) -> None:
        for scenario, reason in (
            ("stale", "profile_changed_since_review"),
            ("tampered", "review_changes_other_fields"),
            ("unsupported-service", "conditional_save_unsupported"),
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
        ):
            with self.subTest(scenario=scenario):
                output, _ = self._exercise(scenario)
                self.assertEqual(json.loads(output.splitlines()[-1])["status"], status)

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
