"""Real records and provider readback qualify the health-only edge path."""

from dataclasses import replace
import json
from typing import Any, cast
from unittest.mock import patch

from control_plane.contracts.private_health_endpoint_record import (
    private_health_endpoint_record_sha256,
)
from control_plane.outbound_http import PublicHttpDestinationError, PublicHttpResponse
from control_plane.testing_private_health import fetch_testing_private_health
from tests.test_testing_private_ingress import PrivateIngressFixture
from tests.test_outbound_http import _resolver_for


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
            patch.dict("os.environ", {"LAUNCHPLANE_NPMPLUS_BASE_URL": "http://10.77.0.9:81"})
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

    def test_fresh_protected_route_allows_only_own_health(self) -> None:
        self.create()
        self.assertEqual(self.fetch().status_code, 200)
        self.transport.assert_called_once_with(
            self.endpoint.url, edge_api_url="http://10.77.0.9:81", timeout_seconds=3
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
