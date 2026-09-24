import json
import io
import unittest
from collections.abc import Mapping
from email.message import Message
from http.client import IncompleteRead
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
from unittest.mock import MagicMock, patch
from urllib.parse import urlencode, urlsplit
from urllib.request import BaseHandler, OpenerDirector, Request, build_opener
from urllib.response import addinfourl

from fastapi import FastAPI
from httpx2 import Response
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.odoo_preview_runtime_plan import OdooPreviewRuntimeTargetEvidence
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import DeploymentEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity, RUNTIME_IDENTITY_ENV_KEY
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.odoo_runtime_reads import _NoRedirects
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.filesystem import FilesystemRecordStore
from tests.http_app_test_support import (
    _preview_read_record,
    _preview_generation_read_record,
    _record_read_policy,
)
from tests.support.auth import _identity, _StubVerifier
from tests.support.http import request


class OdooRuntimeReadTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.temp = TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = FilesystemRecordStore(state_dir=self.root / "state")
        self.preview = _preview_read_record()
        generation = _preview_generation_read_record()
        self.preview_identity = RuntimeIdentity(
            product="example-site",
            context="example-site",
            instance="example-preview-pr-42",
            environment_kind="preview",
            deployment_record_id="mutation-reservation-example",
            artifact_id=generation.artifact_id,
            source_git_ref=generation.anchor_summary.head_sha,
            image_reference="ghcr.io/example/site@sha256:" + "a" * 64,
            preview_id=self.preview.preview_id,
            preview_generation_id=generation.generation_id,
        )
        self.generation = generation.model_copy(update={"runtime_identity": self.preview_identity})
        self.stable_identity = self.preview_identity.model_copy(
            update={
                "instance": "testing",
                "environment_kind": "stable",
                "preview_id": "",
                "preview_generation_id": "",
                "deployment_record_id": "deployment-stable",
            }
        )
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            {
                "product": "example-site",
                "display_name": "Example Site",
                "repository": "every/example-site",
                "driver_id": "odoo",
                "image": {"repository": "ghcr.io/example/site"},
                "runtime_port": 8069,
                "health_path": "/launchplane/health",
                "preview": {
                    "enabled": True,
                    "context": "example-site",
                    "app_name_prefix": "new-prefix-after-deploy",
                },
                "lanes": [
                    {
                        "instance": "testing",
                        "context": "example-site",
                        "base_url": "https://testing.example.invalid",
                    }
                ],
                "updated_at": "2026-09-23T00:00:00Z",
                "source": "test",
            }
        )
        self.store.write_product_profile_record(self.profile)
        self.store.write_preview_record(self.preview)
        self.store.write_preview_generation_record(self.generation)
        self.store.write_environment_inventory(
            EnvironmentInventory(
                context="example-site",
                instance="testing",
                source_git_ref=self.stable_identity.source_git_ref,
                deploy=DeploymentEvidence(
                    status="pass",
                    deploy_mode="dokploy-compose-api",
                    target_name="stable",
                    target_type="compose",
                ),
                updated_at="2026-09-23T00:00:00Z",
                deployment_record_id="deployment-stable",
                runtime_identity=self.stable_identity,
            )
        )
        self.store.write_dokploy_target_record(
            DokployTargetRecord(
                context="example-site",
                instance="testing",
                target_name="stable",
                updated_at="2026-09-23T00:00:00Z",
            )
        )
        self.store.write_dokploy_target_id_record(
            DokployTargetIdRecord(
                context="example-site",
                instance="testing",
                target_id="stable-target",
                updated_at="2026-09-23T00:00:00Z",
            )
        )
        self.base = f"/v1/previews/{self.preview.preview_id}"
        self.filters = {
            "subject": "General inquiry",
            "recipient": "support@example.invalid",
            "created_after": "2026-09-23T20:00:00Z",
        }
        self.mail_rows = [self.mail_row("sent")]
        self.provider_calls: list[dict[str, object]] = []
        self.rpc_calls: list[dict[str, Any]] = []
        self.container_identity = self.preview_identity
        self.container_image = self.preview_identity.image_reference
        self.health_identity = self.preview_identity
        self.domains = [urlsplit(self.preview.canonical_url).hostname, "testing.example.invalid"]
        self.mail_error = False
        self.destroy_on_logs = False
        self.patch = patch(
            "control_plane.odoo_runtime_reads.source.read_dokploy_config",
            return_value=("https://provider.invalid", "provider-secret"),
        )
        self.patch.start()
        self.addCleanup(self.patch.stop)
        self.discovery = patch(
            "control_plane.odoo_runtime_reads.discover_odoo_preview_target",
            return_value=OdooPreviewRuntimeTargetEvidence(
                target_id="preview-target",
                target_name=self.preview_identity.instance,
                context="example-site",
                instance=self.preview_identity.instance,
                environment_kind="preview",
            ),
        )
        self.discover = self.discovery.start()
        self.addCleanup(self.discovery.stop)
        self.provider_patch = patch(
            "control_plane.dokploy.api.dokploy_request", side_effect=self.provider
        )
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)
        self.http_patch = patch(
            "control_plane.odoo_runtime_reads._http_json", side_effect=self.http_json
        )
        self.http_patch.start()
        self.addCleanup(self.http_patch.stop)

    @staticmethod
    def mail_row(state: str) -> dict[str, object]:
        return {
            "id": 9,
            "state": state,
            "failure_type": "mail_smtp" if state == "exception" else False,
            "failure_reason": "SMTP_PASSWORD=smtp-secret login rejected admin-secret"
            if state == "exception"
            else False,
            "message_id": "<test@example.invalid>",
            "email_from": "website@example.invalid",
            "create_date": "2026-09-23 21:00:00",
            "write_date": "2026-09-23 21:00:02",
            "auto_delete": False,
        }

    def provider(self, **kwargs: object) -> object:
        self.provider_calls.append(kwargs)
        path = kwargs["path"]
        query = cast(dict[str, object], kwargs.get("query", {}))
        if path == "/api/compose.one":
            stable = query.get("composeId") == "stable-target"
            return {
                "name": "stable" if stable else self.preview_identity.instance,
                "appName": "selected-compose",
                "serverId": "server",
            }
        if path == "/api/domain.byComposeId":
            return [{"host": domain} for domain in self.domains]
        if path == "/api/docker.getContainersByAppNameMatch":
            return [
                {
                    "containerId": "123456abcdef",
                    "name": "selected-compose-web-1",
                    "state": "running",
                }
            ]
        if path == "/api/docker.getConfig":
            return {
                "State": {"Running": True},
                "Config": {
                    "Image": self.container_image,
                    "Labels": {
                        "com.docker.compose.project": "selected-compose",
                        "com.docker.compose.service": "web",
                    },
                    "Env": [
                        f"{RUNTIME_IDENTITY_ENV_KEY}={self.container_identity.model_dump_json()}",
                        "ODOO_DB_NAME=actual_deployed_database",
                        "ODOO_ADMIN_PASSWORD=admin-secret",
                        "ODOO_ADMIN_LOGIN=configured-admin",
                    ],
                },
            }
        if path == "/api/compose.readLogs":
            if self.destroy_on_logs:
                self.store.write_preview_record(
                    self.preview.model_copy(
                        update={"state": "destroyed", "serving_generation_id": ""}
                    )
                )
            return "old mail\nMAIL SMTP_PASSWORD=mail-secret\nhealth ok"
        raise AssertionError(f"Unexpected provider request: {path!r}")

    def http_json(self, _opener: object, req: Request) -> object:
        if req.data is None:
            return {"runtime_identity": self.health_identity.model_dump()}
        assert isinstance(req.data, bytes)
        body = json.loads(req.data)
        self.rpc_calls.append(body)
        if req.full_url.endswith("/web/session/authenticate"):
            return {"result": {"uid": 7}}
        if req.full_url.endswith("/web/session/destroy"):
            return {"result": None}
        self.assertTrue(req.full_url.endswith("/web/dataset/call_kw"))
        if self.mail_error:
            return {"error": {"message": "admin-secret"}}
        return {"result": self.mail_rows}

    def app(
        self,
        instances: tuple[str, ...] = ("*",),
        actions: tuple[str, ...] = ("target_logs.read", "operations.read"),
    ) -> FastAPI:
        rules = _record_read_policy(
            action="preview.read", context="example-site", schema_version=2
        ).github_actions
        if actions:
            rules += _record_read_policy(
                action=actions[0],
                extra_actions=actions[1:],
                context="example-site",
                instances=instances,
                schema_version=2,
            ).github_actions
        return create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(schema_version=2, github_actions=rules),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )

    async def get(
        self,
        path: str,
        params: Mapping[str, object] | None = None,
        *,
        instances: tuple[str, ...] = ("*",),
        actions: tuple[str, ...] = ("target_logs.read", "operations.read"),
    ) -> Response:
        return await request(
            self.app(instances=instances, actions=actions),
            "GET",
            path + ("?" + urlencode(params) if params else ""),
            headers={"Authorization": "Bearer valid-token"},
        )

    async def test_preview_without_tracked_rows_reads_current_container_and_redacts_bounded_logs(
        self,
    ) -> None:
        response = await self.get(self.base + "/logs", {"lines": 2, "search": "mail"})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["lines"], ["MAIL SMTP_PASSWORD=[redacted]"])
        self.assertEqual(
            response.json()["runtime"]["identity"]["preview_generation_id"],
            self.generation.generation_id,
        )
        self.assertEqual(
            self.discover.call_args.kwargs["compose_name"], self.preview_identity.instance
        )
        self.assertEqual(response.headers["cache-control"], "no-store")

    async def test_testing_grant_cannot_read_preview_logs_or_mail(self) -> None:
        for suffix in ("logs", "outgoing-email"):
            response = await self.get(
                self.base + "/" + suffix,
                self.filters if suffix == "outgoing-email" else {},
                instances=("testing",),
            )
            self.assertEqual(response.status_code, 403, response.text)
        self.assertFalse(self.provider_calls)

    async def test_preview_record_read_alone_does_not_grant_runtime_reads(self) -> None:
        response = await self.get(self.base + "/logs", actions=())
        self.assertEqual(response.status_code, 403, response.text)
        self.assertFalse(self.provider_calls)

    async def test_destroyed_preview_returns_terminal_state_without_provider_read(self) -> None:
        self.store.write_preview_record(
            self.preview.model_copy(update={"state": "destroyed", "serving_generation_id": ""})
        )
        response = await self.get(self.base + "/logs")
        self.assertEqual(response.status_code, 410, response.text)
        self.assertEqual(response.json()["error"]["code"], "preview_destroyed")
        self.assertFalse(self.provider_calls)

    async def test_refreshed_preview_rejects_previous_running_generation(self) -> None:
        new_identity = self.preview_identity.model_copy(
            update={"preview_generation_id": "generation-2"}
        )
        new_generation = self.generation.model_copy(
            update={"generation_id": "generation-2", "runtime_identity": new_identity}
        )
        self.store.write_preview_generation_record(new_generation)
        self.store.write_preview_record(
            self.preview.model_copy(update={"serving_generation_id": "generation-2"})
        )
        response = await self.get(self.base + "/logs")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(
            any(call["path"] == "/api/compose.readLogs" for call in self.provider_calls)
        )

    async def test_refresh_in_progress_is_a_conflict_not_a_provider_outage(self) -> None:
        self.store.write_preview_record(
            self.preview.model_copy(update={"latest_manifest_fingerprint": "new-manifest"})
        )
        response = await self.get(self.base + "/logs")
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "preview_identity_mismatch")
        self.assertFalse(self.provider_calls)

    async def test_image_drift_rejects_before_mail_credentials_are_sent(self) -> None:
        self.container_image = "ghcr.io/example/site:latest"
        response = await self.get(self.base + "/outgoing-email", self.filters)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(self.rpc_calls)

    async def test_destroy_during_read_discards_log_evidence(self) -> None:
        self.destroy_on_logs = True
        response = await self.get(self.base + "/logs")
        self.assertEqual(response.status_code, 410, response.text)
        self.assertNotIn("MAIL", response.text)

    async def test_mail_states_and_failure_reason_are_read_without_sending(self) -> None:
        for state, expected, left in (
            ("sent", "sent", True),
            ("outgoing", "queued", False),
            ("exception", "failed", False),
            ("cancel", "cancelled", False),
            ("received", "unknown", None),
        ):
            with self.subTest(state=state):
                self.mail_rows = [self.mail_row(state)]
                response = await self.get(self.base + "/outgoing-email", self.filters)
                self.assertEqual(response.status_code, 200, response.text)
                email = response.json()["email"]
                self.assertEqual(email["messages"][0]["state"], expected)
                self.assertEqual(email["left_odoo"], left)
                self.assertNotIn("admin-secret", response.text)
                self.assertNotIn("smtp-secret", response.text)
                if state == "exception":
                    self.assertIn("login rejected", email["messages"][0]["failure_reason"])
        reads_ = [call["params"] for call in self.rpc_calls if call["params"].get("model")]
        self.assertTrue(
            all(call["model"] == "mail.mail" and call["method"] == "search_read" for call in reads_)
        )
        self.assertTrue(all("body_html" not in call["kwargs"]["fields"] for call in reads_))

    async def test_empty_or_ambiguous_mail_is_not_delivery_proof(self) -> None:
        for rows, match in (
            ([], "not_found"),
            ([self.mail_row("sent"), self.mail_row("exception")], "ambiguous"),
        ):
            self.mail_rows = rows
            response = await self.get(self.base + "/outgoing-email", self.filters)
            self.assertEqual(response.status_code, 200, response.text)
            self.assertEqual(response.json()["email"]["match"], match)
            self.assertIsNone(response.json()["email"]["left_odoo"])

    async def test_stable_testing_mail_uses_its_own_runtime_and_database(self) -> None:
        self.container_identity = self.stable_identity
        self.health_identity = self.stable_identity
        response = await self.get(
            "/v1/products/example-site/environments/testing/outgoing-email",
            self.filters,
            instances=("testing",),
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.discover.assert_not_called()
        self.assertEqual(response.json()["runtime"]["target_id"], "stable-target")
        self.assertEqual(self.rpc_calls[0]["params"]["db"], "actual_deployed_database")

    async def test_unbound_stable_origin_cannot_receive_credentials_even_if_health_matches(
        self,
    ) -> None:
        self.container_identity = self.stable_identity
        self.health_identity = self.stable_identity
        self.domains = ["different.example.invalid"]
        response = await self.get(
            "/v1/products/example-site/environments/testing/outgoing-email", self.filters
        )
        self.assertEqual(response.status_code, 409, response.text)
        self.assertEqual(response.json()["error"]["code"], "runtime_domain_mismatch")
        self.assertFalse(self.rpc_calls)

    async def test_rejected_mail_read_still_destroys_its_session_without_leaking_error(
        self,
    ) -> None:
        self.mail_error = True
        response = await self.get(self.base + "/outgoing-email", self.filters)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertNotIn("admin-secret", response.text)
        self.assertEqual(self.rpc_calls[-1]["params"], {})

    async def test_wrong_public_runtime_never_receives_credentials(self) -> None:
        self.health_identity = self.stable_identity
        response = await self.get(self.base + "/outgoing-email", self.filters)
        self.assertEqual(response.status_code, 409, response.text)
        self.assertFalse(self.rpc_calls)

    async def test_redirect_is_not_followed_and_never_receives_credentials(self) -> None:
        self.http_patch.stop()
        urls: list[str] = []

        class RedirectResponse(addinfourl):
            msg = "Found"

        class RedirectingOrigin(BaseHandler):
            handler_order = 100

            def https_open(self, req: Request) -> addinfourl:
                urls.append(req.full_url)
                headers = Message()
                headers["Location"] = "https://wrong.example.invalid/launchplane/health"
                return RedirectResponse(io.BytesIO(b""), headers, req.full_url, 302)

        opener = build_opener(_NoRedirects(), RedirectingOrigin())
        with patch("control_plane.odoo_runtime_reads.build_opener", return_value=opener):
            response = await self.get(self.base + "/outgoing-email", self.filters)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(response.json()["error"]["code"], "odoo_redirect")
        self.assertEqual(urls, [self.preview.canonical_url + "/launchplane/health"])

    async def test_partial_or_oversized_http_response_returns_sanitized_unavailable(self) -> None:
        self.http_patch.stop()
        for failure in (
            IncompleteRead(b"admin-secret"),
            ConnectionResetError("admin-secret"),
            None,
        ):
            with self.subTest(failure=type(failure).__name__):
                opener = MagicMock(spec=OpenerDirector)
                reader = opener.open.return_value.__enter__.return_value.read
                reader.side_effect = failure
                reader.return_value = b"x" * 1_000_001
                with patch("control_plane.odoo_runtime_reads.build_opener", return_value=opener):
                    response = await self.get(self.base + "/outgoing-email", self.filters)
                self.assertEqual(response.status_code, 503, response.text)
                self.assertNotIn("admin-secret", response.text)
                self.assertEqual(opener.open.call_count, 1)

    async def test_invalid_query_is_rejected_before_provider_access(self) -> None:
        for update in ({"created_after": "2026-09-23T00:00:00"}, {"subject": " "}, {"limit": 500}):
            response = await self.get(self.base + "/outgoing-email", {**self.filters, **update})
            self.assertEqual(response.status_code, 400, response.text)
        self.assertFalse(self.provider_calls)
