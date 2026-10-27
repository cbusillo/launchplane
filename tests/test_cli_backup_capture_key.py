import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch, MagicMock

from click.testing import CliRunner

from control_plane.cli_backup_capture_key import backup_capture_key


class BackupCaptureKeyCliTests(unittest.TestCase):
    def test_only_public_request_is_sent_and_private_fields_are_refused(self) -> None:
        with TemporaryDirectory() as directory:
            request_file = Path(directory) / "public-request.json"
            payload = {
                "generate_capture_key": True,
                "known_hosts": "public host pin",
                "promotion_action": "example.execute",
                "reason": "Approved enrollment",
            }
            request_file.write_text(json.dumps(payload))
            response = MagicMock()
            response.status_code = 202
            response.json.return_value = {
                "trace_id": "trace",
                "result": {
                    "public_key": "public-only",
                    "fingerprint": "public-fingerprint",
                    "private_key": "must-be-dropped",
                    "ciphertext": "must-be-dropped",
                },
            }
            with patch("control_plane.cli_backup_capture_key.requests.Session") as session:
                session.return_value.__enter__.return_value.request.return_value = response
                runner = CliRunner()
                arguments = [
                    "--service-url",
                    "https://control.example.invalid",
                    "--product",
                    "example",
                    "--mode",
                    "dry-run",
                    "--request-file",
                    str(request_file),
                ]
                result = runner.invoke(
                    backup_capture_key,
                    arguments,
                    env={"LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "fixture-token"},
                )
                self.assertEqual(result.exit_code, 0, result.output)
                self.assertNotIn("must-be-dropped", result.output)
                self.assertNotIn("fixture-token", result.output)
                sent = session.return_value.__enter__.return_value.request.call_args.kwargs
                self.assertFalse(sent["allow_redirects"])
                self.assertEqual(sent["json"]["known_hosts"], payload["known_hosts"])
                response.status_code = 409
                response.json.return_value = {
                    "trace_id": "refusal",
                    "error": {
                        "code": "backup_credential_conflict",
                        "message": "Host pin does not match approved target.",
                    },
                }
                refused = runner.invoke(
                    backup_capture_key,
                    arguments,
                    env={"LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "fixture-token"},
                )
                self.assertNotEqual(refused.exit_code, 0)
                self.assertIn(response.json.return_value["error"]["message"], refused.output)
                self.assertNotIn("fixture-token", refused.output)
                session.reset_mock()
                request_file.write_text(json.dumps({**payload, "value": "private-input"}))
                refused = runner.invoke(
                    backup_capture_key,
                    arguments,
                    env={"LAUNCHPLANE_LOCAL_OPERATOR_TOKEN": "fixture-token"},
                )
                self.assertNotEqual(refused.exit_code, 0)
                self.assertNotIn("private-input", refused.output)
                session.assert_not_called()
