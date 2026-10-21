"""Real records and provider readback qualify the health-only edge path."""

from dataclasses import replace
from datetime import datetime, timedelta
import json
import os
import socket
from typing import Any, cast
from unittest.mock import Mock, patch
from urllib.parse import urlsplit

from control_plane import outbound_http
from control_plane.npmplus import NpmplusLocation
from control_plane.contracts.private_health_endpoint_record import (
    private_health_endpoint_record_sha256,
)
from control_plane.outbound_http import PublicHttpDestinationError, PublicHttpResponse
from control_plane.provider_operations import ProviderMutationRejectedError
from control_plane.testing_private_health import fetch_testing_private_health
from control_plane.contracts.ingress_route_audit_record import (
    IngressRouteAuditRecord,
    TESTING_PRIVATE_INGRESS_AUDIT_PREFIX,
)
from control_plane.workflows.ingress_provider import NPMPLUS_BASE_URL_ENV_KEY
import click
from tests.test_testing_private_ingress import PrivateIngressFixture
from tests.support.ingress import _npmplus_proxy_host
from tests.test_outbound_http import _FakeConnection, _FakeResponse, _resolver_for


class TestingPrivateHealthTests(PrivateIngressFixture):
    def setUp(self) -> None:
        super().setUp()
        self.transport = self.enterContext(
            patch(
                "control_plane.testing_private_health.request_private_edge_health",
                return_value=PublicHttpResponse(200, self.endpoint.url, 0, (), b'{"status":"ok"}'),
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_private_health.build_npmplus_ingress_client_from_env",
                return_value=self.client,
            )
        )
        self.enterContext(
            patch.dict("os.environ", {NPMPLUS_BASE_URL_ENV_KEY: "http://10.77.0.9:81"})
        )

    def fetch(self, **changes: Any) -> PublicHttpResponse:
        return fetch_testing_private_health(
            **{
                "root": self.root,
                "store": self.store,
                "product": self.profile.product,
                "context": self.context,
                "instance": "testing",
                "endpoint_key": self.endpoint.endpoint_key,
                "endpoint_sha256": private_health_endpoint_record_sha256(self.endpoint),
                "url": self.endpoint.url,
                "timeout_seconds": 3,
                **changes,
            }
        )

    def assert_refused(self, **changes: Any) -> None:
        self.transport.reset_mock()
        with self.assertRaises(PublicHttpDestinationError):
            self.fetch(**changes)
        self.transport.assert_not_called()

    def test_registration_alone_does_not_probe(self) -> None:
        self.assert_refused()

    def test_historical_pending_receipt_does_not_block_fresh_completed_route(self) -> None:
        self.create()
        receipt = self.store.list_ingress_route_audit_records()[0]
        self.store.write_ingress_route_audit_record(
            receipt.model_copy(
                update={
                    "record_id": TESTING_PRIVATE_INGRESS_AUDIT_PREFIX + "historical-plan",
                    "status": "pending",
                    "provider_host_id": None,
                    "recorded_at": (
                        datetime.fromisoformat(receipt.recorded_at) - timedelta(seconds=1)
                    ).isoformat(),
                }
            )
        )
        self.assertEqual(self.fetch().status_code, 200)

    def test_held_provider_operation_refuses_even_with_an_applied_receipt(self) -> None:
        self.create()
        self.store.reserve_mutation(
            scope="fixture",
            route_path="/fixture",
            idempotency_key="active-key",
            request_fingerprint="fixture",
            lease_owner="fixture",
            reconciliation_key="fixture",
            provider_target_key=self.adapter().target_key(),
        )
        self.assert_refused()

    def test_hand_reenabled_route_cannot_reuse_historical_enabled_receipt(self) -> None:
        self.create()
        receipt = self.store.list_ingress_route_audit_records()[0]
        # Existing second-precision receipts must not sort ahead of newer
        # subsecond receipts merely because 'Z' sorts after '.'.
        self.store.write_ingress_route_audit_record(
            receipt.model_copy(
                update={
                    "recorded_at": receipt.recorded_at[:19] + "Z",
                }
            )
        )
        digest = self.adapter(operation="disable").plan()["plan_sha256"]
        self.adapter(operation="disable", reviewed_plan_sha256=digest).apply(
            "disable-key", self.lease
        )
        self.client.enable_proxy_host(self.client.proxy_hosts[0].id)
        self.assert_refused()
        with self.assertRaises(ValueError):
            self.adapter().plan()
        digest = self.adapter(operation="disable").plan()["plan_sha256"]
        adapter = self.adapter(operation="disable", reviewed_plan_sha256=digest)
        previous = self.store.read_ingress_route_audit_record(
            TESTING_PRIVATE_INGRESS_AUDIT_PREFIX + str(digest)
        )
        write = self.store.write_ingress_route_audit_record
        for fail_history in (True, False):

            def failed_write(audit: IngressRouteAuditRecord) -> None:
                if (audit.record_id != previous.record_id) == fail_history:
                    raise RuntimeError("receipt persistence failed")
                write(audit)

            with (
                self.subTest(fail_history=fail_history),
                patch.object(
                    self.store, "write_ingress_route_audit_record", side_effect=failed_write
                ),
                patch.object(self.client, "disable_proxy_host") as toggle,
                self.assertRaises(ProviderMutationRejectedError),
            ):
                adapter.apply("cleanup-key", self.lease)
            self.assertEqual(
                self.store.read_ingress_route_audit_record(previous.record_id), previous
            )
            toggle.assert_not_called()
            self.assert_refused()
        with (
            patch.object(self.lease, "assert_current", side_effect=RuntimeError("lease lost")),
            self.assertRaises(ProviderMutationRejectedError),
        ):
            adapter.apply("cleanup-key", self.lease)
        self.assert_refused()
        self.assertTrue(
            any(
                audit.record_id != previous.record_id
                and audit.model_copy(update={"record_id": previous.record_id}) == previous
                for audit in self.store.list_ingress_route_audit_records()
            )
        )
        with self.assertRaises(ValueError):
            self.adapter().plan()
        adapter.apply("cleanup-key", self.lease)
        self.assertTrue(adapter.readback()["applied"])
        self.assertFalse(self.client.proxy_hosts[0].enabled)
        self.assert_refused()
        self.create()
        self.assertEqual(self.fetch().status_code, 200)

    def test_deleted_historical_host_does_not_poison_current_route(self) -> None:
        self.create()
        receipt = self.store.list_ingress_route_audit_records()[0]
        missing_host_id = receipt.provider_host_id + 1 if receipt.provider_host_id else 2
        self.store.write_ingress_route_audit_record(
            receipt.model_copy(
                update={
                    "record_id": TESTING_PRIVATE_INGRESS_AUDIT_PREFIX + "stale-host",
                    "provider_host_id": missing_host_id,
                    "recorded_at": (
                        datetime.fromisoformat(receipt.recorded_at) + timedelta(seconds=1)
                    ).isoformat(),
                }
            )
        )
        read = self.client.get_proxy_host

        def get_host(host_id: int, *, public_only: bool = False) -> Any:
            if host_id == missing_host_id:
                raise click.ClickException("Historical host was deleted")
            return read(host_id, public_only=public_only)

        with patch.object(self.client, "get_proxy_host", side_effect=get_host):
            self.assertEqual(self.fetch().status_code, 200)

    def test_fresh_protected_route_allows_only_own_health(self) -> None:
        self.create()
        self.assertEqual(self.fetch().status_code, 200)
        self.transport.assert_called_once_with(
            self.endpoint.url, edge_url=os.environ[NPMPLUS_BASE_URL_ENV_KEY], timeout_seconds=3
        )
        for fields in (
            {"url": "https://arbitrary.example.test/health"},
            {"url": self.endpoint.url.replace("/health", "/agent-session/connect")},
            {"instance": "prod"},
            {"context": "foreign-context"},
            {"product": "foreign-product"},
            {"endpoint_sha256": "stale"},
        ):
            with self.subTest(fields=fields):
                self.assert_refused(**fields)
        api_host = _npmplus_proxy_host(
            domain_names=("edge-api.example.test",),
            forward_scheme="http",
            forward_host="edge.internal",
            forward_port=81,
        )
        self.client.proxy_hosts.append(api_host)
        self.transport.side_effect = outbound_http.request_private_edge_health
        with (
            patch.dict("os.environ", {NPMPLUS_BASE_URL_ENV_KEY: "https://edge-api.example.test"}),
            patch(
                "control_plane.outbound_http._system_address_resolver",
                _resolver_for("10.77.0.9"),
            ),
            patch(
                "control_plane.outbound_http._private_http_connection",
                side_effect=lambda *args: _FakeConnection(_FakeResponse(status=200)),
            ),
        ):
            for direct_fields in (
                {"forward_host": "127.0.0.1"},
                {"forward_host": "npm-container"},
                {"advanced_config": "proxy_set_header Upgrade $http_upgrade;"},
                {"domain_names": (*api_host.domain_names, "alias.example.test")},
            ):
                with self.subTest(direct_fields=direct_fields):
                    self.client.proxy_hosts[-1] = api_host.model_copy(update=direct_fields)
                    self.transport.reset_mock()
                    self.assertEqual(self.fetch().status_code, 200)
                    self.transport.assert_called_once_with(
                        self.endpoint.url,
                        edge_url=os.environ[NPMPLUS_BASE_URL_ENV_KEY],
                        timeout_seconds=3,
                    )
        self.client.proxy_hosts[-1] = api_host
        connection = _FakeConnection(_FakeResponse(status=200))
        listener_ip, public_ip = "10.77.0.9", "93.184.216.34"
        resolve = Mock(
            side_effect=lambda hostname, port: _resolver_for(
                listener_ip if hostname == api_host.forward_host else public_ip
            )(hostname, port)
        )
        self.transport.side_effect = outbound_http.request_private_edge_health
        with (
            patch.dict("os.environ", {NPMPLUS_BASE_URL_ENV_KEY: "https://edge-api.example.test"}),
            patch("control_plane.outbound_http._system_address_resolver", resolve),
            patch(
                "control_plane.outbound_http._private_http_connection", return_value=connection
            ) as connect,
        ):
            for domains in (("foreign.example.test",), ("*.example.test",)):
                with self.subTest(api_domains=domains):
                    self.client.proxy_hosts[-1] = api_host.model_copy(
                        update={"domain_names": domains}
                    )
                    with self.assertRaises(PublicHttpDestinationError):
                        self.fetch()
                    connect.assert_not_called()
            self.client.proxy_hosts[-1] = api_host
            for answers in ((public_ip,), (listener_ip, public_ip)):
                with self.subTest(answers=answers):
                    resolve.side_effect = _resolver_for(*answers)
                    with self.assertRaises(PublicHttpDestinationError):
                        self.fetch()
                    connect.assert_not_called()
            resolve.reset_mock()
            resolve.side_effect = lambda hostname, port: _resolver_for(
                listener_ip if hostname == api_host.forward_host else public_ip
            )(hostname, port)
            self.assertEqual(self.fetch().status_code, 200)
        self.assertEqual(
            resolve.call_args_list,
            [
                (("edge-api.example.test", 443),),
                ((api_host.forward_host, 443),),
                (("edge-api.example.test", 443),),
            ],
        )
        destination = connect.call_args.args[0]
        self.assertEqual(destination.hostname, urlsplit(self.endpoint.url).hostname)
        self.assertEqual(str(destination.addresses[0].ip), listener_ip)
        self.assertEqual(connection.requests, [("GET", "/health", None, {})])

    def test_disabled_or_stale_edge_refuses(self) -> None:
        self.create()
        self.store.write_edge_endpoint_record(self.edge.model_copy(update={"status": "disabled"}))
        self.assert_refused()

    def test_provider_route_drift_refuses(self) -> None:
        self.create()
        original = self.client.proxy_hosts[0]
        for fields in (
            {"enabled": False},
            {"access_list_id": 0},
            {"access_list_id": 2},
            {"forward_host": "10.99.0.2"},
            {"certificate_id": 99},
            {"advanced_config": "unreviewed config"},
            {"domain_names": (*original.domain_names, "alias.example.test")},
        ):
            with self.subTest(fields=fields):
                self.client.proxy_hosts[0] = original.model_copy(update=fields)
                self.assert_refused()
        self.client.proxy_hosts[0] = original
        api_host = _npmplus_proxy_host(
            domain_names=("edge-api.example.test",),
            forward_scheme="http",
            forward_host="edge.internal",
            forward_port=81,
        )
        self.client.proxy_hosts.append(api_host)
        with (
            patch.dict("os.environ", {NPMPLUS_BASE_URL_ENV_KEY: "https://edge-api.example.test"}),
            patch(
                "control_plane.outbound_http._system_address_resolver",
                _resolver_for("93.184.216.34"),
            ),
        ):
            self.assertEqual(self.fetch().status_code, 200)
            for api_fields in (
                {"enabled": False},
                {"domain_names": (*api_host.domain_names, "alias.example.test")},
                {"advanced_config": "proxy_pass https://foreign.internal;"},
                {
                    "locations": (
                        NpmplusLocation(
                            path="/api",
                            forward_scheme="http",
                            forward_host="foreign.internal",
                            forward_port=81,
                        ),
                    )
                },
                {"forward_scheme": "path"},
                {"forward_port": None},
                {"forward_host": "foreign.internal/path"},
                {"forward_host": "foreign.internal:81"},
                {"forward_host": "foreign.internal?"},
                {"forward_host": "foreign.internal#"},
                {"forward_host": "*.internal"},
                {"forward_host": "~foreign.internal"},
                {"npmplus_auth_request": "tinyauth"},
                {"certificate_id": 0},
            ):
                with self.subTest(api_fields=api_fields):
                    self.client.proxy_hosts[-1] = api_host.model_copy(update=api_fields)
                    self.assert_refused()
            self.client.proxy_hosts[-1] = api_host
            with patch(
                "control_plane.outbound_http._system_address_resolver",
                side_effect=socket.gaierror,
            ):
                self.assert_refused()
            read = self.client.get_proxy_host
            with patch.object(
                self.client,
                "get_proxy_host",
                side_effect=lambda host_id, **kwargs: (
                    api_host.model_copy(update={"id": api_host.id + 1})
                    if host_id == api_host.id
                    else read(host_id, **kwargs)
                ),
            ):
                self.assert_refused()
            self.client.proxy_hosts.append(api_host.model_copy(update={"id": api_host.id + 1}))
            self.assert_refused()
            self.client.proxy_hosts.pop()
            self.client.other_domains = api_host.domain_names
            self.assert_refused()

    def test_absent_or_foreign_application_domain_refuses(self) -> None:
        self.create()
        original = tuple(self.domains)
        self.domains.clear()
        self.assert_refused()
        self.domains.extend({**value, "applicationId": "other-app"} for value in original)
        self.assert_refused()

    def test_current_receipt_authority_is_required(self) -> None:
        self.create()
        receipts = self.store.list_ingress_route_audit_records()
        receipt = receipts[0]
        self.store.write_ingress_route_audit_record(
            receipt.model_copy(update={"testing_authority_sha256": "0" * 64})
        )
        self.assert_refused()
        self.store.write_ingress_route_audit_record(
            receipt.model_copy(update={"status": "pending"})
        )
        self.assert_refused()

    def test_route_changed_during_transport_cannot_publish_success(self) -> None:
        self.create()
        response = cast(PublicHttpResponse, self.transport.return_value)

        def change_route(*args: Any, **kwargs: Any) -> PublicHttpResponse:
            self.client.proxy_hosts[0] = self.client.proxy_hosts[0].model_copy(
                update={"enabled": False}
            )
            return response

        self.transport.side_effect = change_route
        with self.assertRaises(PublicHttpDestinationError):
            self.fetch()
        self.client.proxy_hosts[0] = self.client.proxy_hosts[0].model_copy(update={"enabled": True})
        api_host = _npmplus_proxy_host(
            domain_names=("edge-api.example.test",), forward_host="edge.internal", forward_port=81
        )
        self.client.proxy_hosts.append(api_host)
        with (
            patch.dict("os.environ", {NPMPLUS_BASE_URL_ENV_KEY: "https://edge-api.example.test"}),
            patch(
                "control_plane.outbound_http._system_address_resolver",
                side_effect=_resolver_for("93.184.216.34"),
            ) as resolve,
        ):
            for change in ("upstream", "duplicate", "deleted", "dns_mode", "origin"):
                with self.subTest(api_change=change):

                    def change_api(*args: Any, **kwargs: Any) -> PublicHttpResponse:
                        if change == "upstream":
                            self.client.proxy_hosts[-1] = api_host.model_copy(
                                update={"forward_host": "foreign.internal"}
                            )
                        elif change == "duplicate":
                            self.client.proxy_hosts.append(
                                api_host.model_copy(update={"id": api_host.id + 1})
                            )
                        elif change == "deleted":
                            self.client.proxy_hosts.pop()
                        elif change == "dns_mode":
                            resolve.side_effect = _resolver_for("10.77.0.9")
                        else:
                            os.environ[NPMPLUS_BASE_URL_ENV_KEY] = "https://foreign.example.test"
                        return response

                    self.transport.side_effect = change_api
                    with self.assertRaises(PublicHttpDestinationError):
                        self.fetch()
                    self.client.proxy_hosts = [self.client.proxy_hosts[0], api_host]
                    resolve.side_effect = _resolver_for("93.184.216.34")

    async def test_monitor_uses_verified_edge_and_preserves_identity_check(self) -> None:
        from control_plane.contracts.environment_inventory import EnvironmentInventory
        from control_plane.contracts.promotion_record import DeploymentEvidence
        from control_plane.contracts.runtime_identity import RuntimeIdentity
        from control_plane.contracts.product_profile_record import ProductLaneHealthMonitoringPolicy
        from control_plane.workflows.public_ingress_monitor import (
            HttpObservation,
            run_public_ingress_monitor_once,
        )

        self.enterContext(
            patch(
                "control_plane.outbound_http._system_address_resolver",
                _resolver_for("93.184.216.34"),
            )
        )

        self.create()
        lane = self.profile.lanes[0].model_copy(
            update={
                "health_monitoring": ProductLaneHealthMonitoringPolicy.model_validate(
                    {
                        "monitoring_intent": "private",
                        "checks": [
                            {
                                "name": "private-runtime",
                                "kind": "private_http",
                                "private_endpoint_key": self.endpoint.endpoint_key,
                                "require_runtime_identity": True,
                            }
                        ],
                    }
                )
            }
        )
        self.store.write_product_profile_record(self.profile.model_copy(update={"lanes": (lane,)}))
        self.transport.return_value = replace(
            self.transport.return_value, headers=(("Content-Type", "application/json"),)
        )
        result = run_public_ingress_monitor_once(record_store=self.store, notify=False)
        self.assertTrue(self.transport.called)
        self.assertGreater(result.fail_count, 0)
        self.assertEqual(result.pass_count, 0)
        expected = RuntimeIdentity(
            product=self.profile.product,
            context=self.context,
            instance="testing",
            deployment_record_id="fixture-deploy",
            artifact_id="fixture-artifact",
            source_git_ref="fixture-source",
        )
        self.store.write_environment_inventory(
            EnvironmentInventory(
                context=self.context,
                instance="testing",
                source_git_ref=expected.source_git_ref,
                deployment_record_id=expected.deployment_record_id,
                runtime_identity=expected,
                deploy=DeploymentEvidence(
                    target_name=self.target.target_name,
                    target_type="application",
                    deploy_mode="docker",
                    status="pass",
                ),
                updated_at=self.profile.updated_at,
            )
        )
        for observed, succeeds in (
            (expected, True),
            (expected.model_copy(update={"instance": "prod"}), False),
        ):
            with self.subTest(instance=observed.instance):
                self.transport.return_value = replace(
                    self.transport.return_value,
                    body=json.dumps(
                        {"status": "ok", "runtime_identity": observed.model_dump(mode="json")}
                    ).encode(),
                )
                result = run_public_ingress_monitor_once(record_store=self.store, notify=False)
                if succeeds:
                    self.assertGreater(result.pass_count, 0)
                    self.assertEqual(result.fail_count, 0)
                else:
                    self.assertGreater(result.fail_count, 0)
                    self.assertEqual(result.pass_count, 0)
        # A working existing private-DNS transport needs no new edge receipt.
        self.transport.reset_mock()
        with patch(
            "control_plane.workflows.public_ingress_monitor.fetch_private_health_url",
            return_value=HttpObservation(
                200, self.endpoint.url, 0, {"runtime_identity": expected.model_dump(mode="json")}
            ),
        ):
            result = run_public_ingress_monitor_once(record_store=self.store, notify=False)
        self.assertGreater(result.pass_count, 0)
        self.transport.assert_not_called()
