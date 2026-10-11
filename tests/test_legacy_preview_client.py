import unittest
from typing import Any
from unittest.mock import Mock, patch

from control_plane.contracts.product_retirement import canonical_sha256
from control_plane.legacy_preview_client import send_request
from control_plane.legacy_preview_reconciliation import (
    LEGACY_PREVIEW_RECONCILIATION_ROUTE,
    LegacyPreviewReconciliationRequest,
)


class LegacyPreviewClientTests(unittest.TestCase):
    def setUp(self) -> None:
        self.payload = {
            "product": "example-site",
            "preview_id": "legacy-preview",
            "mode": "plan",
            "reason": "fixture",
        }
        self.contract = {
            "contract": {
                "operations": [
                    {
                        "operation_id": "reconcile_legacy_generic_web_preview",
                        "method": "POST",
                        "path": LEGACY_PREVIEW_RECONCILIATION_ROUTE,
                        "modes": ["inspect", "plan", "apply"],
                    }
                ]
            }
        }
        self.result = {
            "product": "example-site",
            "preview_id": "legacy-preview",
            "mode": "plan",
            "apply_eligible": True,
            "provider_absence_verified": True,
            "provider_state": "absent",
            "provider_writes": False,
            "plan_digest": "a" * 64,
        }

    def send(
        self, payload: dict[str, Any], reviewed: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        return send_request(
            payload=payload,
            reviewed=reviewed,
            idempotency_key="fixture-key",
            service_url="https://service.invalid",
            token="private-fixture-token",
            contract=self.contract,
        )

    def saved(self) -> dict[str, Any]:
        model = LegacyPreviewReconciliationRequest.model_validate(self.payload)
        return {
            "status": "accepted",
            "idempotency_key": "saved-plan",
            "request_digest": canonical_sha256(model.model_dump(mode="json")),
            "result": self.result,
        }

    def test_apply_uses_saved_plan_and_never_follows_redirect(self) -> None:
        response = Mock(status_code=202)
        response.json.return_value = {
            "status": "accepted",
            "trace_id": "trace",
            "result": {**self.result, "mode": "apply", "unexpected_secret": "private"},
        }
        with patch(
            "control_plane.legacy_preview_client.requests.post", return_value=response
        ) as post:
            result = self.send({**self.payload, "mode": "apply"}, self.saved())
        sent = post.call_args.kwargs["json"]
        self.assertTrue(sent["reviewed_plan"])
        self.assertEqual(sent["plan_idempotency_key"], "saved-plan")
        self.assertEqual(sent["expected_plan_digest"], self.result["plan_digest"])
        self.assertFalse(post.call_args.kwargs["allow_redirects"])
        self.assertNotIn("unexpected_secret", result["result"])
        self.assertNotIn("private-fixture-token", str(result))

    def test_unreviewed_or_changed_apply_never_sends(self) -> None:
        for saved in (
            None,
            {**self.saved(), "status": "unavailable"},
            {**self.saved(), "request_digest": "changed"},
            {**self.saved(), "result": {**self.result, "apply_eligible": False}},
        ):
            with (
                self.subTest(saved=saved),
                patch("control_plane.legacy_preview_client.requests.post") as post,
            ):
                with self.assertRaises(ValueError):
                    self.send({**self.payload, "mode": "apply"}, saved)
                post.assert_not_called()

    def test_unknown_or_redirect_response_does_not_echo_private_error(self) -> None:
        with patch(
            "control_plane.legacy_preview_client.requests.post", return_value=Mock(status_code=302)
        ) as post:
            result = self.send(self.payload)
        self.assertEqual(result["status"], "unavailable")
        post.return_value.json.assert_not_called()
