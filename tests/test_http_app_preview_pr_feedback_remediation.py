import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch

from control_plane.contracts.preview_pr_feedback_remediation import (
    PreviewPrFeedbackObservation,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import (
    _asgi_request,
    _local_operator_bearer_config,
)
from tests.support.auth import _identity, _local_operator_policy, _StubVerifier
from tests.support.profiles import _generic_site_profile_payload


def _payload(*, mode: str) -> dict[str, object]:
    pull_request_url = "https://github.com/cbusillo/verireel/pull/311"
    payload: dict[str, object] = {
        "mode": mode,
        "product": "verireel",
        "context": "verireel-preview",
        "repository": "cbusillo/verireel",
        "pull_request_url": pull_request_url,
        "terminal_status": "cleared",
        "reason": "Clear a stale cleanup warning from a retired workflow identity.",
        "related_issue": "cbusillo/launchplane#2076",
    }
    if mode == "apply":
        payload["confirmation"] = f"remediate preview feedback {pull_request_url} to cleared"
    return payload


class PreviewPrFeedbackRemediationHttpTests(unittest.IsolatedAsyncioTestCase):
    async def test_operator_dry_run_and_apply_record_already_absent(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            profile_payload = _generic_site_profile_payload(product="verireel")
            profile_payload["repository"] = "cbusillo/verireel"
            preview_payload = cast(dict[str, object], profile_payload["preview"])
            preview_payload["context"] = "verireel-preview"
            store.write_product_profile_record(
                LaunchplaneProductProfileRecord.model_validate(profile_payload)
            )
            app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=_local_operator_policy(
                    actions=(
                        "preview_pr_feedback_remediation.plan",
                        "preview_pr_feedback_remediation.apply",
                    ),
                    products=("verireel",),
                    contexts=("verireel-preview",),
                ),
                bearer_identity_config=_local_operator_bearer_config(
                    token_label="local-owner-write"
                ),
                control_plane_root_path=root,
                record_store_factory=lambda: store,
            )
            observation = PreviewPrFeedbackObservation(
                state="absent",
                digest_sha256="a" * 64,
                token_actor_id=101,
                token_actor_login="launchplane-bot",
            )
            headers = {
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": "preview-feedback-remediation-verireel-311",
            }
            with (
                patch("control_plane.http_app.resolve_remediation_token", return_value="token"),
                patch(
                    "control_plane.http_app.observe_managed_preview_pr_feedback",
                    return_value=observation,
                ),
            ):
                missing_key = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/remediation",
                    headers={"Authorization": "Bearer local-operator-token"},
                    payload=_payload(mode="dry-run"),
                )
                unmatched_apply = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/remediation",
                    headers={
                        "Authorization": "Bearer local-operator-token",
                        "Idempotency-Key": "preview-feedback-remediation-unmatched",
                    },
                    payload=_payload(mode="apply"),
                )
                dry_run = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/remediation",
                    headers=headers,
                    payload=_payload(mode="dry-run"),
                )
                apply = await _asgi_request(
                    app,
                    "POST",
                    "/v1/previews/pr-feedback/remediation",
                    headers=headers,
                    payload=_payload(mode="apply"),
                )

            self.assertEqual(missing_key.status_code, 400, missing_key.text)
            self.assertEqual(unmatched_apply.status_code, 409, unmatched_apply.text)
            self.assertEqual(unmatched_apply.json()["error"]["code"], "matching_dry_run_required")
            self.assertEqual(dry_run.status_code, 202, dry_run.text)
            self.assertEqual(apply.status_code, 202, apply.text)
            self.assertEqual(apply.json()["result"]["outcome"], "already_absent")
            self.assertFalse(apply.json()["result"]["mutation_evidence"]["mutated"])
            records = store.list_preview_pr_feedback_remediation_records(
                actor="local-operator:local-owner-agent",
                idempotency_key="preview-feedback-remediation-verireel-311",
            )
            self.assertEqual({record.mode for record in records}, {"apply", "dry-run"})

    async def test_github_actions_identity_cannot_use_operator_route(self) -> None:
        app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=_local_operator_policy(
                actions=("preview_pr_feedback_remediation.plan",),
            ),
            record_store_factory=lambda: FilesystemRecordStore(
                state_dir=Path("/tmp/launchplane-remediation-test")
            ),
        )

        response = await _asgi_request(
            app,
            "POST",
            "/v1/previews/pr-feedback/remediation",
            headers={
                "Authorization": "Bearer valid-token",
                "Idempotency-Key": "denied",
            },
            payload=_payload(mode="dry-run"),
        )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(response.json()["error"]["code"], "authorization_denied")


if __name__ == "__main__":
    unittest.main()
