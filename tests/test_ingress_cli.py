import json
import os
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from control_plane.cli import main


class IngressCliTests(unittest.TestCase):
    def test_route_apply_posts_dry_run_request(self) -> None:
        captured_request: dict[str, object] = {}

        def fake_post(**kwargs: object) -> dict[str, object]:
            captured_request.update(kwargs)
            return {
                "status": "accepted",
                "result": {"status": "planned", "dry_run": True},
            }

        with (
            patch.dict(os.environ, {"LAUNCHPLANE_SERVICE_TOKEN": "service-token"}),
            patch("control_plane.cli._post_launchplane_service_json", side_effect=fake_post),
        ):
            result = CliRunner().invoke(
                main,
                [
                    "ingress",
                    "route-apply",
                    "--service-url",
                    "https://launchplane.example",
                    "--product",
                    "launchplane",
                    "--context",
                    "reon-prod",
                    "--domain",
                    "ingress-canary.example.test",
                    "--forward-host",
                    "192.0.2.10",
                    "--forward-port",
                    "8123",
                    "--certificate-id",
                    "47",
                    "--noindex",
                    "--reason",
                    "test route dry run",
                    "--idempotency-key",
                    "ingress-canary-dry-run",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured_request["path"], "/v1/drivers/ingress/route-apply")
        self.assertEqual(captured_request["bearer_token"], "service-token")
        self.assertEqual(captured_request["session_cookie"], "")
        self.assertEqual(captured_request["idempotency_key"], "ingress-canary-dry-run")
        payload = captured_request["payload"]
        assert isinstance(payload, dict)
        self.assertEqual(payload["product"], "launchplane")
        self.assertEqual(payload["context"], "reon-prod")
        ingress = payload["ingress"]
        assert isinstance(ingress, dict)
        self.assertEqual(ingress["mode"], "dry-run")
        route = ingress["route"]
        assert isinstance(route, dict)
        self.assertEqual(route["domain_names"], ["ingress-canary.example.test"])
        self.assertEqual(route["forward_host"], "192.0.2.10")
        self.assertEqual(route["forward_port"], 8123)
        self.assertEqual(route["certificate_id"], 47)
        self.assertTrue(route["npmplus_noindex"])
        response_payload = json.loads(result.output)
        self.assertEqual(response_payload["result"]["status"], "planned")

    def test_route_apply_uses_session_cookie_and_apply_mode(self) -> None:
        captured_request: dict[str, object] = {}

        def fake_post(**kwargs: object) -> dict[str, object]:
            captured_request.update(kwargs)
            return {
                "status": "accepted",
                "result": {"status": "applied", "dry_run": False},
            }

        with patch("control_plane.cli._post_launchplane_service_json", side_effect=fake_post):
            result = CliRunner().invoke(
                main,
                [
                    "ingress",
                    "route-apply",
                    "--service-url",
                    "https://launchplane.example",
                    "--session-cookie",
                    "launchplane_session=signed",
                    "--product",
                    "launchplane",
                    "--context",
                    "reon-prod",
                    "--domain",
                    "ingress-canary.example.test",
                    "--forward-scheme",
                    "https",
                    "--forward-host",
                    "upstream.example.test",
                    "--certificate-id",
                    "new",
                    "--expected-host-id",
                    "78",
                    "--disable",
                    "--no-http3",
                    "--reason",
                    "apply route",
                    "--idempotency-key",
                    "ingress-canary-apply",
                    "--apply",
                ],
            )

        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(captured_request["bearer_token"], "")
        self.assertEqual(captured_request["session_cookie"], "launchplane_session=signed")
        payload = captured_request["payload"]
        assert isinstance(payload, dict)
        ingress = payload["ingress"]
        assert isinstance(ingress, dict)
        self.assertEqual(ingress["mode"], "apply")
        self.assertEqual(ingress["expected_host_id"], 78)
        route = ingress["route"]
        assert isinstance(route, dict)
        self.assertEqual(route["certificate_id"], "new")
        self.assertFalse(route["enabled"])
        self.assertFalse(route["npmplus_http3_support"])

    def test_route_apply_requires_idempotency_key_in_apply_mode(self) -> None:
        with patch.dict(os.environ, {"LAUNCHPLANE_SERVICE_TOKEN": "service-token"}):
            result = CliRunner().invoke(
                main,
                [
                    "ingress",
                    "route-apply",
                    "--service-url",
                    "https://launchplane.example",
                    "--product",
                    "launchplane",
                    "--context",
                    "reon-prod",
                    "--domain",
                    "ingress-canary.example.test",
                    "--forward-host",
                    "192.0.2.10",
                    "--reason",
                    "apply route",
                    "--apply",
                ],
            )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("--apply requires --idempotency-key", result.output)

    def test_route_apply_requires_auth_source(self) -> None:
        result = CliRunner().invoke(
            main,
            [
                "ingress",
                "route-apply",
                "--service-url",
                "https://launchplane.example",
                "--product",
                "launchplane",
                "--context",
                "reon-prod",
                "--domain",
                "ingress-canary.example.test",
                "--forward-host",
                "192.0.2.10",
                "--reason",
                "test route dry run",
            ],
        )

        self.assertEqual(result.exit_code, 1)
        self.assertIn("LAUNCHPLANE_SERVICE_TOKEN is required", result.output)


if __name__ == "__main__":
    unittest.main()
