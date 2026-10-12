from __future__ import annotations

import json
import os
from pathlib import Path
import runpy
import sys
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock, patch

import requests

from control_plane.deploy_failure_alert import (
    health_results,
    replacement_may_have_dispatched,
    send_failure_alert,
    validate_sender,
)


REPO_ROOT = Path(__file__).resolve().parents[1]


class DeployFailureAlertTests(unittest.TestCase):
    def test_pre_effect_refusals_and_drain_skip_but_unknown_attempt_still_alerts(self) -> None:
        with TemporaryDirectory() as directory:
            response = Path(directory) / "response.json"
            self.assertTrue(replacement_may_have_dispatched(response))
            for body in (
                {"result": {"deploy_state": "draining"}},
                {"error": {"code": "self_deploy_fence_conflict"}},
                {"error": {"code": "authorization_denied"}},
                {"error": {"code": "authentication_required"}},
            ):
                response.write_text(json.dumps(body))
                with patch.dict(
                    os.environ, {"SELF_DEPLOY_RESPONSE_FILE": str(response)}, clear=True
                ):
                    with patch("control_plane.deploy_failure_alert.post_discord_webhook") as sender:
                        self.assertFalse(send_failure_alert())
                        sender.assert_not_called()
            response.write_text("null")
            self.assertTrue(replacement_may_have_dispatched(response))

    def test_drain_timeout_alerts_without_claiming_dispatch_or_forwarding_private_blockers(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            response = Path(directory) / "response.json"
            response.write_text(
                json.dumps(
                    {
                        "error": {
                            "code": "self_deploy_drain_timeout",
                            "message": "private-coordinates",
                            "blocking": ["private-key"],
                        }
                    }
                )
            )
            environment = {
                "SELF_DEPLOY_RESPONSE_FILE": str(response),
                "DEPLOYED_IMAGE_REFERENCE": "registry.example/service@sha256:" + "a" * 64,
                "DISCORD_LAUNCHPLANE_WEBHOOK": "https://discord.com/api/webhooks/example/sender",
                "GITHUB_SERVER_URL": "https://forge.example",
                "GITHUB_REPOSITORY": "example/service",
                "GITHUB_RUN_ID": "1234",
                "LAUNCHPLANE_DEPLOY_HEALTH_URLS": "https://health.example",
            }
            self.assertFalse(replacement_may_have_dispatched(response))
            with (
                patch.dict(os.environ, environment, clear=True),
                patch("control_plane.deploy_failure_alert.health_results", return_value=[]),
                patch("control_plane.deploy_failure_alert.post_discord_webhook") as sender,
            ):
                self.assertTrue(send_failure_alert())
            payload = sender.call_args.args[1]
            self.assertIn("drain", payload["embeds"][0]["title"])
            self.assertNotIn("private-", json.dumps(payload))
            self.assertTrue(
                any(
                    "deploy-launchplane.yml" in field["value"]
                    for field in payload["embeds"][0]["fields"]
                )
            )

    def test_health_evidence_never_returns_urls_bodies_or_exception_values(self) -> None:
        unexpected = MagicMock()
        unexpected.status_code = 404
        unexpected.__enter__.return_value = unexpected
        with patch("control_plane.deploy_failure_alert.requests.get", return_value=unexpected):
            results = health_results(["https://private.example/health?token=private"])
        self.assertEqual(results[0]["http_status"], 404)
        unexpected.iter_content.assert_not_called()
        self.assertNotIn("private", json.dumps(results))
        with patch(
            "control_plane.deploy_failure_alert.requests.get",
            side_effect=requests.ConnectionError("secret-url"),
        ):
            failed = health_results(["https://health.example"])
        self.assertEqual(failed[0]["result"], "request_failed")
        self.assertNotIn("secret-url", json.dumps(failed))

    def test_alert_uses_only_existing_sender_and_expected_run_and_recovery_links(self) -> None:
        with TemporaryDirectory() as directory:
            failed_health = Path(directory) / "failed-health.json"
            failed_health.write_text(json.dumps({"http_status": "503", "body": "secret-detail"}))
            environment = {
                "SELF_DEPLOY_RESPONSE_FILE": str(Path(directory) / "missing.json"),
                "DEPLOYED_IMAGE_REFERENCE": "registry.example/service@sha256:" + "a" * 64,
                "PREVIOUS_IMAGE_REFERENCE": "registry.example/service@sha256:" + "b" * 64,
                "DISCORD_LAUNCHPLANE_WEBHOOK": "https://discord.com/api/webhooks/example/sender",
                "GITHUB_SERVER_URL": "https://forge.example",
                "GITHUB_REPOSITORY": "example/service",
                "GITHUB_RUN_ID": "1234",
                "LAUNCHPLANE_DEPLOY_HEALTH_URLS": "https://health.example",
                "FAILING_RUNTIME_STATUS": "404",
                "FAILING_HEALTH_RESULT_FILE": str(failed_health),
            }
            observations = [{"check": 1, "http_status": 503, "result": "unexpected_http_status"}]
            with (
                patch.dict(os.environ, environment, clear=True),
                patch(
                    "control_plane.deploy_failure_alert.health_results", return_value=observations
                ),
                patch("control_plane.deploy_failure_alert.post_discord_webhook") as sender,
            ):
                self.assertTrue(send_failure_alert())
            sender_url, payload = sender.call_args.args
            self.assertEqual(sender_url, environment["DISCORD_LAUNCHPLANE_WEBHOOK"])
            values = [field["value"] for field in payload["embeds"][0]["fields"]]
            self.assertIn(environment["DEPLOYED_IMAGE_REFERENCE"], values)
            self.assertIn(environment["PREVIOUS_IMAGE_REFERENCE"], values)
            evidence = next(json.loads(value) for value in values if value.startswith("{"))
            self.assertEqual(evidence["health"], observations)
            self.assertEqual(evidence["failed_health_result"]["http_status"], "503")
            self.assertNotIn("secret-detail", json.dumps(payload))
            self.assertIn("https://forge.example/example/service/actions/runs/1234", values)
            recovery_link = next(value for value in values if "/actions/workflows/" in value)
            workflow_name = recovery_link.rsplit("/", 1)[1]
            self.assertTrue(((REPO_ROOT / ".github/workflows") / workflow_name).is_file())
            self.assertTrue(recovery_link.startswith("https://forge.example/example/service/"))
            self.assertNotIn(sender_url, json.dumps(payload))
            self.assertEqual(payload["allowed_mentions"]["parse"], [])

    def test_missing_sender_fails_before_replacement_without_printing_a_value(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError):
                validate_sender()

    def test_cli_failure_never_logs_the_sender_or_http_exception_detail(self) -> None:
        script = REPO_ROOT / "scripts/deploy/notify-deploy-failure.py"
        with (
            patch.object(sys, "argv", [str(script)]),
            patch(
                "control_plane.deploy_failure_alert.send_failure_alert",
                side_effect=RuntimeError("private-webhook-token"),
            ),
            self.assertRaises(SystemExit) as failure,
        ):
            runpy.run_path(str(script), run_name="__main__")
        self.assertNotIn("private-webhook-token", str(failure.exception))
