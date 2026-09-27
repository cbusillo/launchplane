import hashlib
import hmac
import unittest
from pathlib import Path
from unittest.mock import patch

from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.trusted_maintenance_github_webhook import (
    TrustedMaintenanceGitHubWebhookDependencies,
    TrustedMaintenanceGitHubWebhookResult,
    handle_trusted_maintenance_github_webhook_request,
)
from tests.support.auth import StubVerifier, identity
from tests.support.http import lifespan_client


class RetiredApprovalRoutesTests(unittest.IsolatedAsyncioTestCase):
    async def test_retired_approval_routes_cannot_read_evaluate_or_write(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=object,
        )
        routes = (
            ("GET", "/v1/change-impact/policy"),
            ("POST", "/v1/change-impact/evaluation"),
            ("POST", "/v1/change-impact/policies/apply"),
            ("GET", "/v1/product-owner/policy"),
            ("GET", "/v1/product-owner/requirement"),
            ("GET", "/v1/product-owner/routing"),
            ("GET", "/v1/product-owner/evaluation"),
            ("POST", "/v1/product-owner/policies/apply"),
            ("POST", "/v1/product-owner/requirements/apply"),
            ("POST", "/v1/product-owner/routing/apply"),
            ("POST", "/v1/manager-preview-approval/reconcile"),
        )
        async with lifespan_client(app) as client:
            for method, path in routes:
                with self.subTest(method=method, path=path):
                    response = await client.request(
                        method, path, json={}, headers={"Authorization": "Bearer test"}
                    )
                    self.assertEqual(response.status_code, 404, response.text)


class MaintenanceWebhookTransportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.secret = "test-webhook-secret"
        self.dependencies = TrustedMaintenanceGitHubWebhookDependencies(
            webhook_secret=lambda: self.secret
        )

    def receive(
        self, payload: bytes, *, event: str = "pull_request", signature: str | None = None
    ) -> tuple[int, dict[str, object]]:
        signature = (
            signature
            if signature is not None
            else "sha256=" + hmac.new(self.secret.encode(), payload, hashlib.sha256).hexdigest()
        )
        return handle_trusted_maintenance_github_webhook_request(
            payload,
            event,
            "delivery-1",
            signature,
            object(),
            Path("."),
            "trace-test",
            dependencies=self.dependencies,
        )

    def test_signed_manager_command_is_ignored_without_record_access(self) -> None:
        payload = b'{"action":"created","comment":{"body":"/preview approve aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}}'
        status, body = self.receive(payload, event="issue_comment")
        self.assertEqual(status, 202)
        self.assertEqual(body["result"]["reason"], "unsupported_event")  # type: ignore[index]

    def test_signature_failure_cannot_reach_maintenance_capture(self) -> None:
        with patch(
            "control_plane.trusted_maintenance_github_webhook.handle_trusted_maintenance_github_webhook"
        ) as capture:
            status, _ = self.receive(b"{}", signature="sha256=" + "0" * 64)
        self.assertEqual(status, 401)
        capture.assert_not_called()

    def test_verified_delivery_preserves_digest_and_retryable_capture_failure(self) -> None:
        payload = b'{"action":"opened"}'
        with patch(
            "control_plane.trusted_maintenance_github_webhook.handle_trusted_maintenance_github_webhook",
            return_value=TrustedMaintenanceGitHubWebhookResult(
                status="retryable_error", reason="database_unavailable"
            ),
        ) as capture:
            status, body = self.receive(payload)
        self.assertEqual(status, 503)
        self.assertEqual(body["error"]["code"], "trusted_maintenance_unavailable")  # type: ignore[index]
        self.assertEqual(
            capture.call_args.kwargs["signed_payload_sha256"], hashlib.sha256(payload).hexdigest()
        )
        self.assertEqual(capture.call_args.kwargs["delivery_id"], "delivery-1")
