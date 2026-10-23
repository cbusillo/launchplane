from contextlib import redirect_stdout
import io
import http.client
import json
from pathlib import Path
import runpy
from tempfile import TemporaryDirectory
import unittest
import urllib.error
from unittest.mock import Mock, patch

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_profile_record_sha256,
)
from tests.support.profiles import _odoo_profile_payload_with_prod_lane


class PreviewOverrideClientTests(unittest.TestCase):
    def _exercise(self, scenario: str = "") -> tuple[str, Mock]:
        client = runpy.run_path(
            str(Path(__file__).resolve().parents[1] / "scripts/set-preview-override.py")
        )
        before = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane()
        ).model_dump(mode="json")
        before["preview"]["override_env"] = {"ODOO_INSTALL_MODULES": "fixture-addon"}
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
            network.side_effect = [({"profile": before}, etag), ({"profile": changed}, etag)]
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
                urllib.error.HTTPError("https://example.invalid", 403, "denied", None, None),
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
            self.assertEqual(len(posts), 0 if scenario in {"stale", "tampered"} else 1)
            if posts:
                self.assertEqual(posts[0].kwargs["body"], after)
                self.assertEqual(posts[0].kwargs["revision"], etag)
            self.assertFalse(evidence.stat().st_mode & 0o077)
            self.assertNotIn("private-fixture-value", output.getvalue())
            self.assertNotIn("private-fixture-token", output.getvalue())
            return output.getvalue(), network

    def test_reviewed_save_changes_only_one_preview_key_without_echoing_the_value(self) -> None:
        self._exercise()

    def test_changed_profile_or_tampered_review_sends_no_write(self) -> None:
        for scenario in ("stale", "tampered"):
            with self.subTest(scenario=scenario):
                output, _ = self._exercise(scenario)
                self.assertEqual(json.loads(output.splitlines()[-1])["status"], "unverified")

    def test_uncertain_apply_and_readback_are_not_retried(self) -> None:
        for scenario, status in (
            ("lost-apply", "outcome_unknown"),
            ("lost-readback", "accepted_unverified"),
            ("denied-readback", "accepted_unverified"),
        ):
            with self.subTest(scenario=scenario):
                output, _ = self._exercise(scenario)
                self.assertEqual(json.loads(output.splitlines()[-1])["status"], status)
