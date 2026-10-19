"""Exercise testing hostname creation against real records and fake providers."""

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any, cast
import unittest
from unittest.mock import Mock, patch
from sqlalchemy.exc import SQLAlchemyError

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord, PublicHostEffect
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.npmplus import (
    NpmplusAccessList,
    NpmplusCertificate,
    NpmplusClient,
    NpmplusProxyHost,
)
from control_plane.provider_operations import (
    ProviderOperationLease,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderMutationOutcome,
    run_durable_provider_operation,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_private_ingress import (
    ROUTE,
    TestingPrivateIngressAdapter,
    TestingPrivateIngressRequest,
)
from control_plane.testing_event_deploy_recovery import testing_event_execution_lock_name
from control_plane.route_binding_reconcile import (
    RouteBindingReconcileRequest,
    plan_route_binding_reconcile,
)
from control_plane.workflows.generic_web_deploy_provider import (
    dokploy_generic_web_provider_target_key,
)
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport, identity, StubVerifier
from tests.support.http import request
from tests.support.ingress import _FakeNpmplusIngressClient, _npmplus_proxy_host
from tests.support.profiles import _generic_site_profile_payload


class EdgeClient(_FakeNpmplusIngressClient):
    def __init__(self) -> None:
        super().__init__(())
        self.certificate = NpmplusCertificate(
            id=47,
            domain_names=("*.example.test",),
            expires_on=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        )
        self.other_domains: tuple[str, ...] = ()

    def get_certificate(self, certificate_id: int) -> NpmplusCertificate:
        if certificate_id != self.certificate.id:
            raise ValueError("Unknown fixture certificate")
        return self.certificate

    def get_access_list(self, access_list_id: int) -> NpmplusAccessList:
        if access_list_id != 1:
            raise ValueError("Unknown fixture access list")
        return NpmplusAccessList(id=1, name="Private testing", satisfy_any=False, pass_auth=False)

    def get_proxy_host(self, host_id: int, *, public_only: bool = False) -> NpmplusProxyHost:
        host = next(host for host in self.proxy_hosts if host.id == host_id)
        # Disabled NPMplus hosts are not loaded. Exercise the real public-read
        # parser instead of silently ignoring the public_only safety argument.
        reader = Mock(spec=NpmplusClient)
        reader._request.return_value = {
            **host.model_dump(mode="json"),
            "npmplus_nginx_online": host.enabled,
            "npmplus_mtls_certificate_id": 0,
            "npmplus_mtls_verify_client_optional": False,
            "npmplus_mtls_bypass_auth_request": False,
            "npmplus_auth_request_upstream": "",
            "npmplus_location_config": "",
            "npmplus_directory": "",
        }
        return NpmplusClient.get_proxy_host(reader, host_id, public_only=public_only)

    def list_other_host_domains(self) -> tuple[str, ...]:
        return self.other_domains


class PrivateIngressFixture(unittest.IsolatedAsyncioTestCase):
    database_url: str = ""

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(
            database_url=self.database_url or f"sqlite+pysqlite:///{self.root / 'state.db'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        profile = LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
        self.context = profile.lanes[0].context
        self.profile = profile.model_copy(
            update={
                "public_website": "none",
                "health_path": "/health",
                "production_use": "live",
                "lanes": (
                    ProductLaneProfile(context=self.context, instance="testing"),
                    ProductLaneProfile(context=self.context, instance="prod"),
                ),
            }
        )
        self.store.write_product_profile_record(self.profile)
        self.target = DokployTargetRecord(
            context=self.context,
            instance="testing",
            target_type="application",
            target_name="testing-app",
            updated_at=self.profile.updated_at,
        )
        self.target_id = DokployTargetIdRecord(
            context=self.context,
            instance="testing",
            target_id="testing-app-id",
            updated_at=self.profile.updated_at,
        )
        self.provider = ProviderTargetRecord.from_dokploy_records(
            target_record=self.target,
            target_id_record=self.target_id,
        )
        self.store.write_dokploy_target_record(self.target)
        self.store.write_dokploy_target_id_record(self.target_id)
        self.store.write_provider_target_record(self.provider)
        self.endpoint = PrivateHealthEndpointRecord(
            endpoint_key="testing-bridge",
            product=self.profile.product,
            context=self.context,
            instance="testing",
            url="https://bridge-testing.example.test/health",
            updated_at=self.profile.updated_at,
        )
        self.store.write_private_health_endpoint_record(self.endpoint)
        self.edge = EdgeEndpointRecord(
            endpoint_key="nonprod-http",
            server_name="nonprod-server",
            upstream_host="10.20.30.40",
            upstream_scheme="http",
            upstream_port=80,
            updated_at=self.profile.updated_at,
        )
        self.store.write_edge_endpoint_record(self.edge)
        self.client = EdgeClient()
        self.body = TestingPrivateIngressRequest(
            context=self.context,
            endpoint_key=self.endpoint.endpoint_key,
            edge_endpoint_key=self.edge.endpoint_key,
            access_list_id=1,
            certificate_id=47,
            reason="Qualify one isolated testing hostname.",
        )
        self.domains: list[dict[str, Any]] = []
        self.dokploy_calls: list[str] = []
        self.enterContext(
            patch(
                "control_plane.testing_private_ingress.source.read_dokploy_config",
                return_value=("https://provider.example.invalid", "fixture-token"),
            )
        )
        self.actual = {"applicationId": self.target_id.target_id, "serverId": "nonprod-id"}
        self.enterContext(
            patch(
                "control_plane.testing_private_ingress.api.fetch_dokploy_target_payload",
                side_effect=lambda **_: self.actual,
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_private_ingress.api.fetch_dokploy_application_domains",
                side_effect=lambda **_: tuple(self.domains),
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_private_ingress.api.dokploy_request",
                side_effect=self.dokploy_request,
            )
        )
        self.lease = Mock(spec=ProviderOperationLease)
        self.app = create_launchplane_fastapi_app(
            verifier=_RejectingVerifier(),
            authz_policy=LaunchplaneAuthzPolicy(),
            bearer_identity_config=_local_operator_bearer_config(),
            record_store_factory=lambda: self.store,
            control_plane_root_path=self.root,
        )
        self.factory = self.enterContext(
            patch(
                "control_plane.http_routes.testing_private_ingress.build_npmplus_ingress_client_from_env",
                return_value=self.client,
            )
        )

    def dokploy_request(self, **kwargs: Any) -> dict[str, Any]:
        path = kwargs["path"]
        if path == "/api/server.one":
            return {
                "serverId": "nonprod-id",
                "name": self.edge.server_name,
                "ipAddress": self.edge.upstream_host,
            }
        if path == "/api/domain.create":
            self.dokploy_calls.append(path)
            domain = {"domainId": "testing-domain", **kwargs["payload"]}
            self.domains.append(domain)
            return domain
        raise AssertionError(f"Unexpected provider mutation {path}")

    def adapter(self, **changes: Any) -> TestingPrivateIngressAdapter:
        return TestingPrivateIngressAdapter(
            self.root,
            self.store,
            self.profile.product,
            TestingPrivateIngressRequest.model_validate({**self.body.model_dump(), **changes}),
            "fixture-trace",
            cast(NpmplusClient, self.client),
        )

    def create(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        self.adapter(mode="apply", reviewed_plan_sha256=digest).apply("original-key", self.lease)

    async def post(self, **changes: Any) -> Any:
        return await request(
            self.app,
            "POST",
            ROUTE.format(product=self.profile.product),
            payload={**self.body.model_dump(), **changes},
            headers={
                "Authorization": "Bearer local-operator-token",
                "Idempotency-Key": "original-key",
            },
        )


class PrivateTestingIngressTests(PrivateIngressFixture):
    def test_historical_cleanup_receipt_requires_exact_successful_ownership(self) -> None:
        self.create()
        receipt = self.store.list_ingress_route_audit_records()[0]
        invalid: tuple[dict[str, Any], ...] = (
            {"record_id": "foreign-receipt"},
            {"product": "other-product"},
            {"context": "other-context"},
            {"provider": "other-provider"},
            {"mode": "dry-run", "dry_run": True},
            {"status": "planned"},
            {"status": "pending"},
            {"provider_host_id": None},
            {"provider_host_id": self.client.proxy_hosts[0].id + 1},
            {"requested_domains": ("other.example.test",)},
            {"edge_endpoint_key": "other-edge"},
            {"provider_route_sha256": "0" * 64},
        )
        for changes in invalid:
            with (
                self.subTest(changes=changes),
                patch.object(
                    self.store,
                    "list_ingress_route_audit_records",
                    return_value=[receipt.model_copy(update=changes)],
                ),
                self.assertRaises(ValueError),
            ):
                self.adapter(operation="disable").plan()

    def test_original_key_recovers_when_domain_create_did_not_land(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        adapter = self.adapter(reviewed_plan_sha256=digest)
        provider_call = self.dokploy_request

        def reject_domain(**kwargs: Any) -> Any:
            if kwargs["path"] == "/api/domain.create":
                raise ValueError("Synthetic domain rejection")
            return provider_call(**kwargs)

        with patch(
            "control_plane.testing_private_ingress.api.dokploy_request", side_effect=reject_domain
        ):
            with self.assertRaises(ProviderMutationUnknownError):
                adapter.apply("original-key", self.lease)
        self.assertEqual(self.domains, [])
        self.assertEqual(self.client.proxy_hosts, [])
        observation = adapter.observe(
            "original-key", "create_private_testing_application_domain", "key"
        )
        self.assertEqual(observation.outcome, "absent")
        self.assertTrue(observation.retry_safe)
        adapter.apply("original-key", self.lease)
        self.assertTrue(adapter.readback()["applied"])

    async def test_generic_private_noop_cannot_authorize_managed_route_binding(self) -> None:
        self.create()
        verified = identity()
        policy = LaunchplaneAuthzPolicy.model_validate(
            {
                "schema_version": 2,
                "github_actions": [
                    {
                        "repository": verified.repository,
                        "workflow_refs": [verified.workflow_ref],
                        "event_names": [verified.event_name],
                        "products": [self.profile.product],
                        "contexts": [self.context],
                        "instances": ["testing"],
                        "actions": ["ingress_route.apply"],
                    }
                ],
            }
        )
        app = create_launchplane_fastapi_app(
            verifier=StubVerifier(verified),
            authz_policy=policy,
            record_store_factory=lambda: self.store,
            npmplus_ingress_client_factory=lambda: self.client,
        )
        actual = self.client.proxy_hosts[0]
        original = self.store.list_ingress_route_audit_records(
            product=self.profile.product, context_name=self.context
        )[0]
        recorded_at = (
            datetime.fromisoformat(original.recorded_at) + timedelta(seconds=1)
        ).isoformat()
        calls = list(self.client.calls)
        with patch("control_plane.http_app.utc_now_timestamp", return_value=recorded_at):
            response = await request(
                app,
                "POST",
                "/v1/drivers/ingress/route-apply",
                headers={"Authorization": "Bearer valid-token", "Idempotency-Key": "private-noop"},
                payload={
                    "product": self.profile.product,
                    "context": self.context,
                    "instance": "testing",
                    "ingress": {
                        "mode": "apply",
                        "expected_host_id": actual.id,
                        "require_exact_expected_host_domains": True,
                        "allow_create": False,
                        "route": {
                            **actual.model_dump(mode="json", exclude={"id", "meta"}),
                            "forward_host": "",
                            "edge_endpoint_key": self.edge.endpoint_key,
                        },
                        "reason": "Record exact existing route without provider mutation.",
                    },
                },
            )
        self.assertEqual(response.status_code, 202, response.text)
        self.assertEqual(response.json()["result"]["status"], "unchanged")
        self.assertTrue(all(call == "list" for call in self.client.calls[len(calls) :]))
        audits = self.store.list_ingress_route_audit_records(
            product=self.profile.product, context_name=self.context
        )
        self.assertEqual(len(audits), 2)
        plan = plan_route_binding_reconcile(
            record_store=self.store,
            request=RouteBindingReconcileRequest(
                product=self.profile.product,
                context=self.context,
                instance="testing",
                evaluated_at=recorded_at,
            ),
        )
        self.assertEqual(plan.status, "blocked")
        self.assertEqual(
            plan.findings[0].code, "private_testing_endpoint_not_route_binding_evidence"
        )

    def test_create_and_disable_only_the_owned_hostname(self) -> None:
        original_profile = self.store.read_product_profile_record(self.profile.product)
        self.create()
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])
        self.assertEqual(len(self.client.proxy_hosts), 1)
        actual = self.client.proxy_hosts[0]
        self.assertEqual(actual.domain_names, ("bridge-testing.example.test",))
        self.assertEqual(actual.access_list_id, self.body.access_list_id)
        self.assertEqual(actual.certificate_id, self.body.certificate_id)
        recorded = self.store.read_dokploy_target_record(
            context_name=self.context, instance_name="testing"
        )
        self.assertEqual(recorded.domains, actual.domain_names)
        self.assertEqual(recorded.public_hosts, ())
        self.assertFalse(self.adapter().plan()["deployment_performed"])
        disable = self.adapter(operation="disable")
        digest = disable.plan()["plan_sha256"]
        result = self.adapter(operation="disable", mode="apply", reviewed_plan_sha256=digest).apply(
            "disable-key", self.lease
        )
        self.assertIsInstance(result.response_payload["result"], dict)
        self.assertTrue(cast(dict[str, object], result.response_payload["result"])["applied"])
        self.assertFalse(self.client.proxy_hosts[0].enabled)
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])
        self.assertEqual(
            self.store.read_product_profile_record(self.profile.product), original_profile
        )
        self.create()
        self.assertTrue(self.client.proxy_hosts[0].enabled)

    def test_disable_survives_application_replacement_and_unavailable_old_edge(self) -> None:
        self.create()
        digest = self.adapter(operation="disable").plan()["plan_sha256"]
        self.adapter(operation="disable", reviewed_plan_sha256=digest).apply(
            "first-disable-key", self.lease
        )
        self.client.enable_proxy_host(self.client.proxy_hosts[0].id)
        with (
            patch.object(self.store, "read_dokploy_target_record", side_effect=FileNotFoundError),
            patch.object(self.store, "read_provider_target_record", side_effect=FileNotFoundError),
            patch.object(self.store, "read_edge_endpoint_record", side_effect=FileNotFoundError),
            patch.object(self.client, "get_certificate", side_effect=ValueError("expired")),
            patch(
                "control_plane.testing_private_ingress.api.fetch_dokploy_target_payload",
                side_effect=ValueError("old application removed"),
            ),
        ):
            digest = self.adapter(operation="disable").plan()["plan_sha256"]
            adapter = self.adapter(operation="disable", reviewed_plan_sha256=digest)
            adapter.apply("disable-key", self.lease)
            self.assertTrue(adapter.readback()["applied"])
        self.assertFalse(self.client.proxy_hosts[0].enabled)
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])

    def test_active_event_deploy_excludes_private_application_mutation(self) -> None:
        lock_name = testing_event_execution_lock_name(
            dokploy_generic_web_provider_target_key(self.provider)
        )
        digest = self.adapter().plan()["plan_sha256"]
        with self.store.testing_replacement_execution_lock(lock_name) as check:
            self.assertIsNotNone(check)
            with self.assertRaises(ProviderMutationRejectedError):
                self.adapter(reviewed_plan_sha256=digest).apply("original-key", self.lease)
        self.assertEqual(self.dokploy_calls, [])
        self.assertFalse(any(call.startswith("create") for call in self.client.calls))

    def test_plan_does_not_change_either_provider(self) -> None:
        self.assertFalse(self.adapter().plan()["applied"])
        self.assertEqual(self.dokploy_calls, [])
        self.assertFalse([call for call in self.client.calls if call != "list"])

    def test_refuses_foreign_or_disabled_endpoint_and_public_product(self) -> None:
        for update in (
            {"instance": "prod"},
            {"product": "another-product"},
            {"status": "disabled"},
        ):
            with self.subTest(update=update):
                self.store.write_private_health_endpoint_record(
                    self.endpoint.model_copy(update=update)
                )
                with self.assertRaises(ValueError):
                    self.adapter().plan()
        self.store.write_private_health_endpoint_record(self.endpoint)
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"public_website": "required"})
        )
        with self.assertRaises(ValueError):
            self.adapter().plan()
        self.assertEqual(self.dokploy_calls, [])

    def test_refuses_application_shared_with_another_lane(self) -> None:
        sibling = self.provider.model_copy(update={"instance": "prod"})
        with patch.object(
            self.store, "list_provider_target_records", return_value=(self.provider, sibling)
        ):
            with self.assertRaises(ValueError):
                self.adapter().plan()
        self.assertEqual(self.dokploy_calls, [])

    def test_refuses_foreign_target_hostname_claims(self) -> None:
        hostname = "bridge-testing.example.test"
        for update in (
            {"domains": (hostname,)},
            {"public_hosts": (hostname,)},
            {
                "pending_public_host_effects": (
                    PublicHostEffect(
                        host=hostname,
                        provider_host="https://provider.example.invalid",
                        target_id="another-application",
                        runtime_port=self.profile.runtime_port,
                    ),
                )
            },
        ):
            with self.subTest(update=update):
                other = self.target.model_copy(update={"instance": "prod", **update})
                with patch.object(
                    self.store, "list_dokploy_target_records", return_value=(self.target, other)
                ):
                    with self.assertRaises(ValueError):
                        self.adapter().plan()
        self.assertEqual(self.dokploy_calls, [])

    def test_refuses_unresolved_own_application_hostname_effects(self) -> None:
        pending = PublicHostEffect(
            host="bridge-testing.example.test",
            provider_host="https://provider.example.invalid",
            target_id=self.target_id.target_id,
            runtime_port=self.profile.runtime_port,
        )
        self.store.write_dokploy_target_record(
            self.target.model_copy(update={"pending_public_host_effects": (pending,)})
        )
        with self.assertRaises(ValueError):
            self.adapter().plan()
        self.assertEqual(self.dokploy_calls, [])

    def test_refuses_provider_placement_that_does_not_match_edge(self) -> None:
        self.actual["serverId"] = "production-server"
        with self.assertRaises(ValueError):
            self.adapter().plan()

    def test_refuses_existing_foreign_route_even_if_identical(self) -> None:
        desired = self.adapter()._desired(self.adapter()._authority())
        self.client.proxy_hosts = [
            NpmplusProxyHost.model_validate({"id": 12, **desired.model_dump()})
        ]
        with self.assertRaises(ValueError):
            self.adapter().plan()
        self.assertFalse([call for call in self.client.calls if call != "list"])

    def test_refuses_wildcard_and_other_host_type_collisions(self) -> None:
        self.client.proxy_hosts = [
            _npmplus_proxy_host().model_copy(update={"domain_names": ("*.example.test",)})
        ]
        with self.assertRaises(ValueError):
            self.adapter().plan()
        self.client.proxy_hosts = []
        self.client.other_domains = ("bridge-testing.example.test",)
        with self.assertRaises(ValueError):
            self.adapter().plan()

    def test_refuses_expired_or_noncovering_certificate(self) -> None:
        for update in ({"domain_names": ("*.other.test",)}, {"expires_on": "2000-01-01T00:00:00Z"}):
            with self.subTest(update=update):
                original = self.client.certificate
                self.client.certificate = original.model_copy(update=update)
                with self.assertRaises(ValueError):
                    self.adapter().plan()
                self.client.certificate = original

    def test_missing_access_list_refuses_before_any_provider_effect(self) -> None:
        with self.assertRaises(ValueError):
            self.adapter(access_list_id=2).plan()
        self.assertEqual(self.dokploy_calls, [])
        self.assertFalse(any(call.startswith("create") for call in self.client.calls))

    def test_original_key_resumes_domain_present_edge_absent_without_repeating_domain(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        adapter = self.adapter(reviewed_plan_sha256=digest)
        with patch.object(
            self.client, "create_proxy_host", side_effect=ValueError("edge rejected")
        ):
            with self.assertRaises(ProviderMutationUnknownError):
                adapter.apply("original-key", self.lease)
        self.assertEqual(len(self.domains), 1)
        self.assertEqual(self.client.proxy_hosts, [])
        self.assertEqual(adapter.observe("wrong-key", "edge-create", "key").outcome, "unknown")
        recovery = adapter.observe("original-key", "edge-create", "key")
        self.assertEqual(recovery.outcome, "absent")
        self.assertTrue(recovery.retry_safe)
        adapter.apply("original-key", self.lease)
        self.assertTrue(adapter.readback()["applied"])
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])
        self.assertEqual(len(self.client.proxy_hosts), 1)

    def test_original_key_adopts_after_certificate_renewal_and_metadata_changes(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        create = self.client.create_proxy_host

        def lost_response(payload: Any) -> Any:
            create(payload)
            raise TimeoutError("lost final response")

        adapter = self.adapter(reviewed_plan_sha256=digest)
        with patch.object(self.client, "create_proxy_host", side_effect=lost_response):
            with self.assertRaises(ProviderMutationUnknownError):
                adapter.apply("original-key", self.lease)
        self.client.certificate = self.client.certificate.model_copy(
            update={"expires_on": (datetime.now(timezone.utc) + timedelta(days=60)).isoformat()}
        )
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"display_name": "Renamed product"})
        )
        self.store.write_private_health_endpoint_record(
            self.endpoint.model_copy(update={"source_label": "refreshed metadata"})
        )
        self.assertEqual(adapter.observe("original-key", "edge-create", "key").outcome, "present")
        self.assertTrue(adapter.readback()["applied"])
        self.assertEqual(self.client.calls.count("create"), 1)

    def test_dokploy_empty_nullable_domain_fields_match_readback(self) -> None:
        with patch(
            "control_plane.testing_private_ingress.api.fetch_dokploy_application_domains",
            side_effect=lambda **_: tuple(
                {key: "" if value is None else value for key, value in domain.items()}
                for domain in self.domains
            ),
        ):
            self.create()
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])

    def test_apply_missing_target_is_a_pre_effect_rejection(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        with patch.object(self.store, "read_provider_target_record", side_effect=FileNotFoundError):
            with self.assertRaises(ProviderMutationRejectedError):
                self.adapter(reviewed_plan_sha256=digest).apply("original-key", self.lease)
        self.assertEqual(self.dokploy_calls, [])

    def test_apply_lock_failure_is_a_pre_effect_rejection(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        with patch.object(
            self.store, "testing_replacement_execution_lock", side_effect=SQLAlchemyError
        ):
            with self.assertRaises(ProviderMutationRejectedError):
                self.adapter(reviewed_plan_sha256=digest).apply("original-key", self.lease)
        self.assertEqual(self.dokploy_calls, [])
        self.assertFalse(any(call.startswith("create") for call in self.client.calls))

    def test_partial_recovery_refuses_changed_runtime_port(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        adapter = self.adapter(reviewed_plan_sha256=digest)
        with patch.object(
            self.client, "create_proxy_host", side_effect=ValueError("edge rejected")
        ):
            with self.assertRaises(ProviderMutationUnknownError):
                adapter.apply("original-key", self.lease)
        self.store.write_product_profile_record(
            self.profile.model_copy(update={"runtime_port": self.profile.runtime_port + 1})
        )
        recovery = adapter.observe("original-key", "edge-create", "key")
        self.assertEqual(recovery.outcome, "unknown")
        self.assertFalse(recovery.retry_safe)
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])
        self.assertEqual(self.client.proxy_hosts, [])

    def test_refuses_configured_application_domain(self) -> None:
        self.domains = [{"domainId": "foreign", "host": "other.example.test"}]
        with self.assertRaises(ValueError):
            self.adapter().plan()

    def test_stale_plan_refuses_before_any_write(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        self.store.write_private_health_endpoint_record(
            self.endpoint.model_copy(update={"source_label": "changed"})
        )
        with self.assertRaises(ProviderMutationRejectedError):
            self.adapter(mode="apply", reviewed_plan_sha256=digest).apply(
                "original-key", self.lease
            )
        self.assertEqual(self.dokploy_calls, [])
        self.assertFalse([call for call in self.client.calls if call != "list"])

    def test_lost_create_response_observes_missing_edge_without_repeating_effect(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        with patch.object(
            self.client, "create_proxy_host", side_effect=TimeoutError("lost response")
        ):
            with self.assertRaises(ProviderMutationUnknownError):
                self.adapter(mode="apply", reviewed_plan_sha256=digest).apply(
                    "original-key", self.lease
                )
        observation = self.adapter(reviewed_plan_sha256=digest).observe(
            "original-key", "edge-create", "key"
        )
        self.assertEqual(observation.outcome, "absent")
        self.assertTrue(observation.retry_safe)
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])

    def test_original_key_adopts_lost_final_response_without_another_provider_write(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        create = self.client.create_proxy_host

        def lost_response(payload: Any) -> Any:
            create(payload)
            raise TimeoutError("Synthetic lost final response")

        adapter = self.adapter(mode="apply", reviewed_plan_sha256=digest)
        with patch.object(self.client, "create_proxy_host", side_effect=lost_response):
            with self.assertRaises(ProviderMutationUnknownError):
                adapter.apply("original-key", self.lease)
        wrong_key = adapter.observe("different-key", "edge-create", "key")
        self.assertEqual(wrong_key.outcome, "unknown")
        observed = adapter.observe("original-key", "edge-create", "key")
        self.assertEqual(observed.outcome, "present")
        self.assertEqual(self.dokploy_calls, ["/api/domain.create"])
        self.assertEqual(self.client.calls.count("create"), 1)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.context, instance_name="testing"
            ).domains,
            self.client.proxy_hosts[0].domain_names,
        )
        with self.assertRaises(FileNotFoundError):
            self.adapter(reviewed_plan_sha256="b" * 64).readback()

    def test_partial_private_operation_holds_private_route_without_blocking_deploy(self) -> None:
        digest = self.adapter().plan()["plan_sha256"]
        adapter = self.adapter(reviewed_plan_sha256=digest)

        def run(current: Any, key: str) -> Any:
            return run_durable_provider_operation(
                store=self.store,
                scope="testing-private-fixture",
                route_path=ROUTE,
                idempotency_key=key,
                request_fingerprint=key,
                lease_owner="fixture-worker",
                response_trace_id="fixture-trace",
                adapter=current,
            )

        def uncertain_edge(key: str, lease: ProviderOperationLease) -> ProviderMutationOutcome:
            lease.checkpoint_effect("apply_private_testing_edge")
            raise ProviderMutationUnknownError("Synthetic lost edge response")

        # The actual partial-provider path is covered above. Use the real
        # adapter's fence identity with a synthetic effect here: SQLite cannot
        # renew a lease from a second connection inside the provider bundle's
        # write transaction (the HTTP operation requires PostgreSQL).
        with patch.object(adapter, "apply", side_effect=uncertain_edge):
            private_result = run(adapter, "original-key")
        self.assertEqual(private_result.status, "reconcile_required")
        # A separate durable deployment reservation can complete, while a fresh
        # private-route attempt remains blocked by the genuine unknown outcome.
        deploy = Mock()
        deploy.target_key.return_value = dokploy_generic_web_provider_target_key(self.provider)
        deploy.reconciliation_key.return_value = "next-testing-image"

        def complete_deploy(key: str, lease: ProviderOperationLease) -> ProviderMutationOutcome:
            lease.checkpoint_effect("deploy_testing_image")
            return ProviderMutationOutcome(
                response_status_code=202, response_payload={"status": "accepted"}
            )

        deploy.apply.side_effect = complete_deploy
        self.assertEqual(run(deploy, "testing-deploy").status, "completed")
        deploy.apply.assert_called_once()
        self.assertEqual(run(adapter, "different-private-key").status, "target_busy")
        held = self.store.read_idempotency_record(
            scope="testing-private-fixture", route_path=ROUTE, idempotency_key="original-key"
        )
        self.assertIsNotNone(held)
        assert held is not None
        self.assertEqual(held.state, "reconcile_required")
        self.assertEqual(self.dokploy_calls, [])

    def test_original_key_adopts_lost_disable_response(self) -> None:
        self.create()
        digest = self.adapter(operation="disable").plan()["plan_sha256"]
        disable = self.client.disable_proxy_host

        def lost_response(host_id: int) -> Any:
            disable(host_id)
            raise TimeoutError("Synthetic lost disable response")

        adapter = self.adapter(operation="disable", reviewed_plan_sha256=digest)
        with patch.object(self.client, "disable_proxy_host", side_effect=lost_response):
            with self.assertRaises(ProviderMutationUnknownError):
                adapter.apply("disable-key", self.lease)
        self.assertFalse(self.client.proxy_hosts[0].enabled)
        self.assertEqual(adapter.observe("other-key", "edge-disable", "key").outcome, "unknown")
        self.assertEqual(adapter.observe("disable-key", "edge-disable", "key").outcome, "present")
        self.assertEqual(sum(call.startswith("disable:") for call in self.client.calls), 1)

    def test_owned_route_drift_refuses_disable(self) -> None:
        self.create()
        digest = self.adapter(operation="disable").plan()["plan_sha256"]
        self.client.proxy_hosts[0].access_list_id = 0
        with self.assertRaises(ValueError):
            self.adapter(operation="disable").plan()
        with self.assertRaises(ProviderMutationRejectedError):
            self.adapter(operation="disable", reviewed_plan_sha256=digest).apply(
                "disable-key", self.lease
            )
        self.assertTrue(self.client.proxy_hosts[0].enabled)
        self.assertFalse(any(call.startswith("disable:") for call in self.client.calls))

    async def test_director_can_plan_with_no_ingress_write_grant(self) -> None:
        response = await self.post()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertFalse(response.json()["result"]["applied"])

    async def test_scoped_agent_cannot_use_director_private_route(self) -> None:
        with scoped_agent_transport():
            response = await self.post()
        self.assertEqual(response.status_code, 403, response.text)
        self.factory.assert_not_called()
        self.assertEqual(self.dokploy_calls, [])

    async def test_apply_refuses_missing_postgres_fencing(self) -> None:
        response = await self.post(mode="apply", reviewed_plan_sha256="a" * 64)
        self.assertEqual(response.status_code, 503, response.text)
        self.assertEqual(self.dokploy_calls, [])

    async def test_request_rejects_non_testing_and_unprotected_route(self) -> None:
        for update in ({"instance": "prod"}, {"access_list_id": 0}, {"certificate_id": 0}):
            with self.subTest(update=update):
                response = await self.post(**update)
                self.assertEqual(response.status_code, 400, response.text)
