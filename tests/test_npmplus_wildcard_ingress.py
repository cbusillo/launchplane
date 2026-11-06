import io
import click
import json
import os
import traceback
import unittest
from email.message import Message
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import cast
from unittest.mock import patch
from urllib.error import HTTPError, URLError
from urllib.request import Request

from cryptography.fernet import Fernet
from httpx2 import Response
from http.client import IncompleteRead

from control_plane import secrets
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.npmplus import PREVIEW_DNS_BINDING, PREVIEW_DNS_INTEGRATION, NpmplusClient
from control_plane.product_config import _plan_product_config_secret_write
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.storage.product_authority_bundle import ProductAuthorityBundle
from tests.http_app_test_support import _asgi_request, _notification_policy_apply_policy
from tests.support.auth import _identity, _StubVerifier
from tests.support.ingress import _edge_endpoint_record
from tests.support.profiles import product_profile_payload
from tests.support.stores import sqlite_database_url
from tests.test_npmplus import _credentials, _Opener, _Response, _proxy_host_response

_TOKEN = "fictional-preview-zone-token"
_ROUTE = "/v1/drivers/ingress/route-apply"


class _DnsOpener(_Opener):
    def __init__(self, mode: str = "success") -> None:
        super().__init__()
        self.mode = mode
        self.proxy_hosts_payload: list[dict[str, object]] = [
            {"id": 79, "domain_names": ["unrelated.example.test"]}
        ]
        self.created = 0
        self.certificates = [self.certificate_payload.copy()]

    def open(self, request: Request, timeout: int | float) -> _Response:
        if request.get_method() == "GET" and request.selector == "/api/nginx/proxy-hosts/78":
            return _Response(self.host_details)
        if request.get_method() == "POST" and request.selector == "/api/nginx/proxy-hosts":
            self.created += 1
            body = json.loads(cast(bytes, request.data))
            if self.mode in {
                "native-rejection",
                "native-error",
                "native-orphan",
                "transport-inflight",
            }:
                status = 400 if self.mode == "native-rejection" else 500
                if self.mode in {"native-orphan", "transport-inflight"}:
                    self.certificates.append(
                        {
                            **self.certificate_payload,
                            "id": 48,
                            "domain_names": body["domain_names"],
                            "expires_on": None
                            if self.mode == "transport-inflight"
                            else self.certificate_payload["expires_on"],
                        }
                    )
                if self.mode == "transport-inflight":
                    raise TimeoutError(_TOKEN)
                raise HTTPError(
                    request.full_url,
                    status,
                    _TOKEN,
                    Message(),
                    io.BytesIO(json.dumps({"error": {"code": status, "message": _TOKEN}}).encode()),
                )
            if self.mode == "error":
                raise HTTPError(
                    request.full_url, 500, _TOKEN, Message(), io.BytesIO(_TOKEN.encode())
                )
            if self.mode == "partial":
                raise IncompleteRead(_TOKEN.encode())
            if self.mode == "transport":
                raise URLError(_TOKEN)
            if self.mode == "malformed":
                return _Response(_TOKEN.encode())
            if self.mode == "echo":
                return _Response({**_proxy_host_response(body), "forward_host": _TOKEN})
            response = super().open(request, timeout)
            assert isinstance(response.payload, dict)
            response.payload["certificate_id"] = 48
            self.host_details.update(_proxy_host_response(body), certificate_id=48)
            self.certificate_payload = {
                **self.certificate_payload,
                "id": 48,
                "domain_names": body["domain_names"],
            }
            self.certificates.append(self.certificate_payload)
            self.proxy_hosts_payload.append({"id": 78, "domain_names": body["domain_names"]})
            if self.mode == "lost":
                raise TimeoutError(_TOKEN)
            return response
        if request.get_method() == "GET" and request.selector == "/api/nginx/certificates":
            return _Response(self.certificates)
        return super().open(request, timeout)


class _Fixture:
    def __init__(
        self, test: unittest.TestCase, *, mode: str = "success", supplied: bool = True
    ) -> None:
        folder = test.enterContext(TemporaryDirectory())
        test.enterContext(
            patch.dict(
                os.environ, {"LAUNCHPLANE_MASTER_ENCRYPTION_KEY": Fernet.generate_key().decode()}
            )
        )
        self.store = PostgresRecordStore(
            database_url=sqlite_database_url(Path(folder) / "records.sqlite")
        )
        self.store.ensure_schema()
        test.addCleanup(self.store.close)
        self.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(product_profile_payload("sample"))
        )
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context="sample-testing",
                env={"LAUNCHPLANE_PREVIEW_BASE_URL": "https://preview.example.test"},
                updated_at="2026-01-01T00:00:00Z",
            )
        )
        edge = _edge_endpoint_record()
        self.store.write_edge_endpoint_record(edge)
        version = ""
        if supplied:
            plan = _plan_product_config_secret_write(
                record_store=self.store,
                scope="context",
                context_name="launchplane",
                integration=PREVIEW_DNS_INTEGRATION,
                name=PREVIEW_DNS_BINDING,
                binding_key=PREVIEW_DNS_BINDING,
                plaintext_value=_TOKEN,
                value_source="supplied",
            )
            self.store.write_product_authority_bundle(
                ProductAuthorityBundle(
                    secret_records=tuple(plan["secret_records"]),
                    secret_versions=tuple(plan["secret_versions"]),
                    secret_bindings=tuple(plan["secret_bindings"]),
                    secret_audit_events=tuple(plan["secret_audit_events"]),
                )
            )
            version = plan["secret_records"][0].current_version_id
        self.opener = _DnsOpener(mode)
        self.client = NpmplusClient(credentials=_credentials(), opener=self.opener)
        policy = _notification_policy_apply_policy(
            action="ingress_route.apply", product="sample", context="sample-testing"
        )
        policy.github_actions[0].actions = ("ingress_route.apply", "ingress_route.plan")
        self.app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=policy,
            record_store_factory=lambda: self.store,
            npmplus_ingress_client_factory=lambda: self.client,
        )
        self.payload: dict[str, object] = {
            "product": "sample",
            "context": "sample-testing",
            "ingress": {
                "mode": "apply",
                "reason": "Prepare preview wildcard",
                "dns_secret_version_id": version,
                "route": {
                    "domain_names": ["*.preview.example.test"],
                    "edge_endpoint_key": edge.endpoint_key,
                    "certificate_id": "new",
                    "dns_challenge": "cloudflare",
                    "npmplus_noindex": True,
                },
            },
        }

    async def post(
        self,
        *,
        mode: str = "apply",
        key: str = "wildcard-original",
        authorization: str = "Bearer valid-token",
    ) -> Response:
        ingress = cast(dict[str, object], self.payload["ingress"])
        return await _asgi_request(
            self.app,
            "POST",
            _ROUTE,
            payload={**self.payload, "ingress": {**ingress, "mode": mode}},
            headers={"Authorization": authorization, "Idempotency-Key": key},
        )

    def use_director(self) -> str:
        from control_plane.service_auth import BearerIdentityConfig, LaunchplaneAuthzPolicy

        self.app = create_launchplane_fastapi_app(
            verifier=_StubVerifier(_identity()),
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: self.store,
            npmplus_ingress_client_factory=lambda: self.client,
            bearer_identity_config=BearerIdentityConfig(
                local_operator_token="director-token",
                local_operator_subject="director",
                local_operator_token_label="fixture",
            ),
        )
        return "Bearer director-token"


class WildcardIngressTests(unittest.IsolatedAsyncioTestCase):
    async def test_director_can_plan_its_preview_namespace_without_write_authority(self) -> None:
        fixture = _Fixture(self, supplied=False)
        authorization = fixture.use_director()
        with patch.object(secrets, "resolve_context_secret_value_from_store") as resolver:
            plan = await fixture.post(mode="dry-run", authorization=authorization)
            self.assertEqual(plan.status_code, 202, plan.text)
            self.assertFalse(plan.json()["records"]["dns_secret_ready"])
            ingress = cast(dict[str, object], fixture.payload["ingress"])
            route = cast(dict[str, object], ingress["route"])
            with patch.dict(route, {"dns_challenge": ""}):
                refused = await fixture.post(authorization=authorization)
                self.assertEqual(refused.status_code, 403, refused.text)
            with patch.dict(fixture.payload, {"product": "unknown-product"}):
                missing = await fixture.post(mode="dry-run", authorization=authorization)
                self.assertEqual(missing.status_code, 400, missing.text)
            route["domain_names"] = ["*.foreign.example.test"]
            foreign = await fixture.post(mode="dry-run", authorization=authorization)
            self.assertEqual(foreign.status_code, 400, foreign.text)
            resolver.assert_not_called()
        self.assertEqual(fixture.opener.created, 0)

    async def test_director_create_is_closed_to_unsafe_fields_and_foreign_scopes(self) -> None:
        from tests.support.auth import scoped_agent_transport

        fixture = _Fixture(self)
        authorization = fixture.use_director()
        ingress = cast(dict[str, object], fixture.payload["ingress"])
        route = cast(dict[str, object], ingress["route"])
        route.pop("npmplus_noindex")
        with patch.object(secrets, "resolve_context_secret_value_from_store") as resolver:
            for field, value in (
                ("advanced_config", "include /data/custom_nginx/shared.conf;"),
                (
                    "locations",
                    [
                        {
                            "path": "/extra",
                            "forward_scheme": "https",
                            "forward_host": "other.example.test",
                            "forward_port": 443,
                        }
                    ],
                ),
                ("npmplus_auth_request", "authentik"),
                ("forward_scheme", "http"),
                ("npmplus_noindex", True),
            ):
                with self.subTest(field=field), patch.dict(route, {field: value}):
                    refused = await fixture.post(authorization=authorization, key="unsafe-" + field)
                    self.assertEqual(refused.status_code, 403, refused.text)
            with scoped_agent_transport():
                refused = await fixture.post(authorization=authorization, key="client")
                self.assertEqual(refused.status_code, 403, refused.text)
            with patch.dict(route, {"domain_names": ["*.foreign.example.test"]}):
                refused = await fixture.post(authorization=authorization, key="foreign")
                self.assertEqual(refused.status_code, 400, refused.text)
            resolver.assert_not_called()
        created = await fixture.post(authorization=authorization, key="director-original")
        self.assertEqual(created.status_code, 202, created.text)
        self.assertEqual(fixture.opener.created, 1)
        self.assertNotIn(_TOKEN, created.text)

    async def test_recorded_lane_hosts_and_nested_preview_namespaces_block_issuance(self) -> None:
        for source in ("lane", "public_hosts", "parent_preview", "child_preview"):
            with self.subTest(source=source):
                fixture = _Fixture(self)
                profile = LaunchplaneProductProfileRecord.model_validate(
                    product_profile_payload("other")
                )
                if source == "lane":
                    profile.lanes[0].base_url = "https://app.preview.example.test"
                fixture.store.write_product_profile_record(profile)
                if source == "public_hosts":
                    fixture.store.write_dokploy_target_record(
                        DokployTargetRecord(
                            context=profile.lanes[0].context,
                            instance=profile.lanes[0].instance,
                            public_hosts=("app.preview.example.test",),
                            updated_at=profile.updated_at,
                        )
                    )
                if source in {"parent_preview", "child_preview"}:
                    fixture.store.write_runtime_environment_record(
                        RuntimeEnvironmentRecord(
                            scope="context",
                            context=profile.preview.context,
                            env={
                                "LAUNCHPLANE_PREVIEW_BASE_URL": "https://example.test"
                                if source == "parent_preview"
                                else "https://child.preview.example.test"
                            },
                            updated_at=profile.updated_at,
                        )
                    )
                with patch.object(secrets, "resolve_context_secret_value_from_store") as resolver:
                    refused = await fixture.post(mode="dry-run")
                    self.assertEqual(refused.status_code, 400, refused.text)
                    resolver.assert_not_called()
                self.assertEqual(fixture.opener.created, 0)

    async def test_plan_absence_and_invalid_scope_never_resolve_a_value(self) -> None:
        fixture = _Fixture(self, supplied=False)
        with patch.object(secrets, "resolve_context_secret_value_from_store") as resolver:
            plan = await fixture.post(mode="dry-run")
            self.assertEqual(plan.status_code, 202, plan.text)
            self.assertFalse(plan.json()["records"]["dns_secret_ready"])
            refused = await fixture.post()
            self.assertEqual(refused.status_code, 400, refused.text)
            ingress = cast(dict[str, object], fixture.payload["ingress"])
            route = cast(dict[str, object], ingress["route"])
            route["domain_names"] = ["*.foreign.example.test"]
            bad_plan = await fixture.post(mode="dry-run")
            self.assertEqual(bad_plan.status_code, 400, bad_plan.text)
            resolver.assert_not_called()
        self.assertEqual(fixture.opener.created, 0)

    async def test_denied_authority_and_stale_version_do_not_resolve_credentials(self) -> None:
        from control_plane.service_auth import LaunchplaneAuthzPolicy

        fixture = _Fixture(self)
        ingress = cast(dict[str, object], fixture.payload["ingress"])
        ingress["dns_secret_version_id"] = "stale-version"
        with patch.object(secrets, "resolve_context_secret_value_from_store") as resolver:
            stale = await fixture.post()
            self.assertEqual(stale.status_code, 400, stale.text)
            fixture.app = create_launchplane_fastapi_app(
                verifier=_StubVerifier(_identity()),
                authz_policy=LaunchplaneAuthzPolicy(),
                record_store_factory=lambda: fixture.store,
                npmplus_ingress_client_factory=lambda: fixture.client,
            )
            denied = await fixture.post()
            self.assertEqual(denied.status_code, 403, denied.text)
            resolver.assert_not_called()
        self.assertEqual(fixture.opener.created, 0)

    async def test_local_token_validation_and_pre_effect_interruption_do_not_strand_a_target(
        self,
    ) -> None:
        from control_plane.npmplus_wildcard_ingress import NpmplusWildcardIngressOperation

        fixture = _Fixture(self)
        with patch.object(
            secrets, "resolve_context_secret_value_from_store", return_value="Bearer " + _TOKEN
        ):
            malformed = await fixture.post()
        self.assertEqual(malformed.status_code, 400, malformed.text)
        self.assertEqual(fixture.opener.created, 0)
        with patch.object(
            NpmplusWildcardIngressOperation,
            "plan",
            side_effect=RuntimeError("Interrupted before effect"),
        ):
            with self.assertRaises(RuntimeError):
                await fixture.post()
        recovered = await fixture.post()
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(fixture.opener.created, 1)

    async def test_duplicate_preview_namespace_is_refused_before_secret_resolution(self) -> None:
        fixture = _Fixture(self)
        fixture.store.write_product_profile_record(
            LaunchplaneProductProfileRecord.model_validate(product_profile_payload("other"))
        )
        fixture.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="context",
                context="other-testing",
                env={"LAUNCHPLANE_PREVIEW_BASE_URL": "https://preview.example.test"},
                updated_at="2026-01-01T00:00:00Z",
            )
        )
        with patch.object(secrets, "resolve_context_secret_value_from_store") as resolver:
            refused = await fixture.post()
            self.assertEqual(refused.status_code, 400, refused.text)
            resolver.assert_not_called()
        self.assertEqual(fixture.opener.created, 0)

    async def test_success_replays_receipt_and_keeps_dns_token_inside_create(
        self,
    ) -> None:
        fixture = _Fixture(self)
        with patch.object(
            fixture.store,
            "read_secret_version",
            side_effect=AssertionError("A plan decrypted a credential"),
        ):
            plan = await fixture.post(mode="dry-run")
        self.assertEqual(plan.status_code, 202, plan.text)
        response = await fixture.post()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(
            response.json()["result"]["proxy_host"]["certificate_id"],
            fixture.opener.certificate_payload["id"],
        )
        for method, url, body in fixture.opener.requests:
            if method != "POST":
                continue
            if url.endswith("/api/nginx/proxy-hosts"):
                self.assertIn(_TOKEN, json.dumps(body))
            else:
                self.assertNotIn(_TOKEN, json.dumps(body))
        replay = await fixture.post()
        self.assertEqual(replay.status_code, 202, replay.text)
        self.assertTrue(replay.json()["replayed"])
        self.assertEqual(fixture.opener.created, 1)
        evidence = (
            response.text + replay.text + repr(fixture.store.list_ingress_route_audit_records())
        )
        self.assertNotIn(_TOKEN, evidence)

    async def test_settled_provider_rejection_recovers_only_after_complete_absence_and_converges_audit(
        self,
    ) -> None:
        for mode in ("native-rejection", "native-error"):
            with self.subTest(mode=mode):
                fixture = _Fixture(self, mode=mode)
                rejected = await fixture.post()
                self.assertEqual(rejected.status_code, 409, rejected.text)
                fixture.opener.mode = "success"
                recovered = await fixture.post()
                self.assertEqual(recovered.status_code, 202, recovered.text)
                self.assertEqual(fixture.opener.created, 2)
                audits = [
                    entry
                    for entry in fixture.store.list_ingress_route_audit_records()
                    if entry.idempotency_key == "wildcard-original"
                ]
                self.assertEqual(len(audits), 1)
                self.assertEqual(audits[0].status, "applied")
                self.assertNotIn(_TOKEN, rejected.text + recovered.text + repr(audits))

    async def test_terminal_absence_can_release_stale_version_for_a_fresh_reviewed_key(
        self,
    ) -> None:
        fixture = _Fixture(self, mode="native-rejection")
        self.assertEqual((await fixture.post()).status_code, 409)
        plan = _plan_product_config_secret_write(
            record_store=fixture.store,
            scope="context",
            context_name="launchplane",
            integration=PREVIEW_DNS_INTEGRATION,
            name=PREVIEW_DNS_BINDING,
            binding_key=PREVIEW_DNS_BINDING,
            plaintext_value=_TOKEN + "-rotated",
            value_source="supplied",
        )
        fixture.store.write_product_authority_bundle(
            ProductAuthorityBundle(
                secret_records=tuple(plan["secret_records"]),
                secret_versions=tuple(plan["secret_versions"]),
                secret_bindings=tuple(plan["secret_bindings"]),
                secret_audit_events=tuple(plan["secret_audit_events"]),
            )
        )
        fixture.opener.mode = "success"
        with patch.object(
            secrets,
            "resolve_context_secret_value_from_store",
            side_effect=AssertionError("Stale version decrypted"),
        ):
            stale = await fixture.post()
        self.assertEqual(stale.status_code, 400, stale.text)
        self.assertEqual(fixture.opener.created, 1)
        audits = [
            entry
            for entry in fixture.store.list_ingress_route_audit_records()
            if entry.idempotency_key == "wildcard-original"
        ]
        self.assertEqual(len(audits), 1)
        self.assertEqual(audits[0].status, "unchanged")
        ingress = cast(dict[str, object], fixture.payload["ingress"])
        ingress["dns_secret_version_id"] = plan["secret_records"][0].current_version_id
        recovered = await fixture.post(key="fresh-reviewed-key")
        self.assertEqual(recovered.status_code, 202, recovered.text)
        self.assertEqual(fixture.opener.created, 2)

    async def test_orphan_or_inflight_certificate_prevents_new_issuance(self) -> None:
        for mode in ("native-orphan",):
            with self.subTest(mode=mode):
                fixture = _Fixture(self, mode=mode)
                rejected = await fixture.post()
                self.assertEqual(rejected.status_code, 409, rejected.text)
                fixture.opener.mode = "success"
                with patch.object(
                    secrets,
                    "resolve_context_secret_value_from_store",
                    side_effect=AssertionError("Observation decrypted a credential"),
                ):
                    still_unknown = await fixture.post()
                self.assertEqual(still_unknown.status_code, 409, still_unknown.text)
                self.assertEqual(fixture.opener.created, 1)
                self.assertEqual(len(fixture.opener.certificates), 2)

    async def test_dispatched_failures_never_blind_retry_and_lost_success_is_adopted(self) -> None:
        for mode in ("error", "transport", "partial", "malformed", "echo", "lost"):
            with self.subTest(mode=mode):
                fixture = _Fixture(self, mode=mode)
                with self.assertLogs("control_plane", level="WARNING") as logs:
                    failed = await fixture.post()
                self.assertEqual(failed.status_code, 409, failed.text)
                self.assertEqual(failed.json()["error"]["code"], "mutation_reconciliation_required")
                with patch.object(
                    secrets,
                    "resolve_context_secret_value_from_store",
                    side_effect=AssertionError("Reconciliation resolved a value"),
                ):
                    retried = await fixture.post()
                self.assertEqual(retried.status_code, 202 if mode == "lost" else 409, retried.text)
                different_key = await fixture.post(key="replacement-key")
                self.assertEqual(
                    different_key.status_code, 400 if mode == "lost" else 409, different_key.text
                )
                self.assertEqual(fixture.opener.created, 1)
                if mode == "lost":
                    audits = [
                        entry
                        for entry in fixture.store.list_ingress_route_audit_records()
                        if entry.idempotency_key == "wildcard-original"
                    ]
                    self.assertEqual(len(audits), 1)
                    self.assertEqual(audits[0].status, "applied")
                evidence = (
                    failed.text
                    + retried.text
                    + different_key.text
                    + repr(logs.output)
                    + repr(fixture.store.list_ingress_route_audit_records())
                )
                self.assertNotIn(_TOKEN, evidence)

    def test_provider_errors_with_fictional_credentials_do_not_leak_in_tracebacks(self) -> None:
        from control_plane.npmplus import NpmplusProxyHostPayload

        client = NpmplusClient(credentials=_credentials(), opener=_DnsOpener("echo"))
        try:
            client.create_dns_proxy_host(
                NpmplusProxyHostPayload(
                    domain_names=("*.preview.example.test",),
                    forward_host="192.0.2.40",
                    forward_scheme="https",
                    certificate_id="new",
                ),
                token=_TOKEN,
            )
        except (ValueError, click.ClickException):
            self.assertNotIn(_TOKEN, traceback.format_exc())
        else:
            self.fail("Unverified provider response succeeded")
