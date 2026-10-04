from __future__ import annotations

import hashlib
import hmac
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from control_plane.service_bootstrap import create_launchplane_service_application
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.support.auth import _StubVerifier, _identity
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.stores import _sqlite_database_url
from tests.support.http import request
from control_plane.trusted_maintenance_github_webhook import (
    TrustedMaintenanceGitHubWebhookResult,
    TRUSTED_MAINTENANCE_WEBHOOK_ROUTE,
)


class EveryCodeWebhookRetirementTests(unittest.IsolatedAsyncioTestCase):
    async def test_signed_label_delivery_cannot_create_work(self) -> None:
        secret = "retired-webhook-test-secret"
        body = json.dumps(
            {
                "action": "labeled",
                "label": {"name": "every-code"},
                "repository": {"full_name": "example/product"},
                "issue": {"number": 1, "title": "Client request"},
            }
        ).encode()
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {"LAUNCHPLANE_EVERY_CODE_GITHUB_WEBHOOK_SECRET": secret}),
        ):
            root = Path(directory)
            store = PostgresRecordStore(database_url=_sqlite_database_url(root / "records.sqlite3"))
            self.addCleanup(store.close)
            store.ensure_schema()
            app = create_launchplane_service_application(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                bootstrap_authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=root,
                service_record_store=store,
            )
            response = await request(
                app,
                "POST",
                "/v1/every-code/github-webhook",
                raw_body=body,
                headers={
                    "Content-Type": "application/json",
                    "X-GitHub-Event": "issues",
                    "X-GitHub-Delivery": "retired-delivery",
                    "X-Hub-Signature-256": f"sha256={signature}",
                },
            )
            self.assertEqual(response.status_code, 404)
            self.assertEqual(store.list_every_code_work_request_records(), ())

    async def test_service_keeps_signed_maintenance_ingress(self) -> None:
        secret = "maintenance-webhook-test-secret"
        body = b'{"action":"opened"}'
        signature = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        with (
            TemporaryDirectory() as directory,
            patch.dict("os.environ", {"LAUNCHPLANE_MANAGER_PREVIEW_GITHUB_WEBHOOK_SECRET": secret}),
        ):
            root = Path(directory)
            store = PostgresRecordStore(database_url=_sqlite_database_url(root / "records.sqlite3"))
            self.addCleanup(store.close)
            store.ensure_schema()
            app = create_launchplane_service_application(
                state_dir=root / "state",
                verifier=_StubVerifier(_identity()),
                bootstrap_authz_policy=LaunchplaneAuthzPolicy(),
                control_plane_root_path=root,
                service_record_store=store,
            )
            with patch(
                "control_plane.trusted_maintenance_github_webhook.handle_trusted_maintenance_github_webhook",
                return_value=TrustedMaintenanceGitHubWebhookResult(status="captured"),
            ) as capture:
                for supplied_signature, expected_status in (("0" * 64, 401), (signature, 202)):
                    response = await request(
                        app,
                        "POST",
                        TRUSTED_MAINTENANCE_WEBHOOK_ROUTE,
                        raw_body=body,
                        headers={
                            "Content-Type": "application/json",
                            "X-GitHub-Event": "pull_request",
                            "X-GitHub-Delivery": "maintenance-delivery",
                            "X-Hub-Signature-256": f"sha256={supplied_signature}",
                        },
                    )
                    self.assertEqual(response.status_code, expected_status)
                    if expected_status == 401:
                        capture.assert_not_called()
                capture.assert_called_once()
                self.assertEqual(
                    capture.call_args.kwargs["signed_payload_sha256"],
                    hashlib.sha256(body).hexdigest(),
                )
