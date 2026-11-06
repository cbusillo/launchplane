from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any
import unittest
from unittest.mock import Mock, patch

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.dokploy.compose import compose_web_domain_route_payload
from control_plane.dokploy_target_setup_http import CREATE_COMPOSE_SOURCE_LABEL
from control_plane.http_app import create_launchplane_fastapi_app
from control_plane.npmplus import NpmplusCertificate
from control_plane.outbound_http import PublicTlsProbeResult
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.service_auth import LaunchplaneAuthzPolicy
from tests.http_app_test_support import _local_operator_bearer_config, _RejectingVerifier
from tests.support.auth import scoped_agent_transport
from tests.support.http import request
from tests.support.profiles import _odoo_preview_profile_payload
from tests.test_testing_public_ingress import EdgeClient


class FirstTestingPublicIngressTests(unittest.IsolatedAsyncioTestCase):
    database_url = ""

    def setUp(self) -> None:
        temporary = TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = PostgresRecordStore(
            database_url=self.database_url or f"sqlite+pysqlite:///{self.root / 'state.db'}"
        )
        self.store.ensure_schema()
        self.addCleanup(self.store.close)
        payload = _odoo_preview_profile_payload("example-odoo")
        payload["lanes"] = (
            {
                "context": "example",
                "instance": "testing",
                "odoo_data_policy": {
                    "data_authority": "resettable",
                    "allowed_rebuild_sources": ["empty"],
                },
            },
        )
        payload["preview"] = {"enabled": True, "context": "example", "slug_template": "pr-{number}"}
        self.profile = LaunchplaneProductProfileRecord.model_validate(payload)
        self.store.write_product_profile_record(self.profile)
        self.domain = "new-testing.example.invalid"
        self.target = DokployTargetRecord(
            context="example",
            instance="testing",
            target_type="compose",
            source_type="github",
            domains=(self.domain,),
            updated_at=self.profile.updated_at,
        )
        self.target_id = DokployTargetIdRecord(
            context="example",
            instance="testing",
            target_id="initial-compose",
            source_label=CREATE_COMPOSE_SOURCE_LABEL,
            updated_at=self.profile.updated_at,
        )
        self.store.write_dokploy_target_record(self.target)
        self.store.write_dokploy_target_id_record(self.target_id)
        self.store.write_provider_target_record(
            ProviderTargetRecord.from_dokploy_records(
                target_record=self.target, target_id_record=self.target_id
            )
        )
        self.endpoint = EdgeEndpointRecord(
            endpoint_key="nonproduction",
            server_name="shared-testing",
            upstream_host="192.0.2.20",
            upstream_scheme="https",
            upstream_port=443,
            updated_at=self.profile.updated_at,
        )
        self.store.write_edge_endpoint_record(self.endpoint)
        self.routes = [
            {
                "domainId": "initial-domain",
                **compose_web_domain_route_payload(
                    compose_id=self.target_id.target_id,
                    domain_host=self.domain,
                    runtime_port=self.profile.runtime_port,
                ),
            }
        ]
        self.live = {
            "composeId": self.target_id.target_id,
            "serverId": "testing-server",
            "environmentId": "testing-environment",
            "appName": "initial-compose",
            "name": "Initial testing",
            "composeType": "docker-compose",
            "composeStatus": "idle",
            "isolatedDeployment": False,
            "randomize": False,
            "serviceNetworks": [],
            "mounts": [],
            "domains": self.routes,
            "autoDeploy": False,
            "env": "",
        }
        self.client = EdgeClient()
        self.certificate = NpmplusCertificate(
            id=self.client.certificate.id,
            domain_names=("*.example.invalid",),
            expires_on=(datetime.now(timezone.utc) + timedelta(days=30)).isoformat(),
        )
        self.client.certificate = self.certificate
        self.enterContext(
            patch.object(
                self.client, "list_certificates", create=True, return_value=(self.certificate,)
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_public_ingress.source.read_dokploy_config",
                return_value=("provider.invalid", "fixture-secret"),
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_public_ingress.api.fetch_dokploy_target_payload",
                side_effect=lambda **_: self.live.copy(),
            )
        )
        self.enterContext(
            patch(
                "control_plane.testing_public_ingress.api.dokploy_request",
                side_effect=self.provider_read,
            )
        )
        self.enterContext(
            patch(
                "control_plane.http_routes.testing_public_ingress.build_npmplus_ingress_client_from_env",
                return_value=self.client,
            )
        )
        self.tls = self.enterContext(
            patch(
                "control_plane.testing_public_ingress.probe_public_tls",
                return_value=PublicTlsProbeResult(
                    status="valid", failure_code=None, summary="fixture"
                ),
            )
        )
        self.app = create_launchplane_fastapi_app(
            control_plane_root_path=self.root,
            authz_policy=LaunchplaneAuthzPolicy(),
            record_store_factory=lambda: self.store,
            verifier=_RejectingVerifier(),
            bearer_identity_config=_local_operator_bearer_config(),
        )

    def provider_read(self, **kwargs: Any) -> Any:
        self.assertNotIn(kwargs.get("method"), ("POST", "PUT", "DELETE"))
        if kwargs["path"] == "/api/server.one":
            return {
                "serverId": "testing-server",
                "name": self.endpoint.server_name,
                "ipAddress": self.endpoint.upstream_host,
            }
        if kwargs["path"] == "/api/domain.byComposeId":
            return self.routes
        return []

    async def plan(self, **changes: Any) -> Any:
        return await request(
            self.app,
            "POST",
            "/v1/products/example-odoo/testing-public-ingress",
            headers={"Authorization": "Bearer local-operator-token"},
            payload={
                "context": "example",
                "reason": "Publish the first approved testing hostname",
                "mode": "dry-run",
                **changes,
            },
        )

    async def test_first_use_plans_without_a_restored_generation_or_existing_binding(self) -> None:
        response = await self.plan()
        self.assertEqual(response.status_code, 202, response.text)
        self.assertFalse(response.json()["result"]["applied"])
        self.assertTrue(all(call == "list" for call in self.client.calls))
        self.tls.assert_not_called()
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), self.profile)
        self.assertIsNone(
            self.store.read_fresh_odoo_testing_generation(self.profile.product, "example")
        )
        with self.assertRaises(FileNotFoundError):
            self.store.read_route_binding_record(
                product=self.profile.product, context_name="example", instance_name="testing"
            )

    async def test_first_use_preserves_instance_and_director_identity_guards(self) -> None:
        with scoped_agent_transport():
            response = await self.plan()
        self.assertEqual(response.status_code, 403, response.text)
        response = await self.plan(instance="prod")
        self.assertEqual(response.status_code, 400, response.text)
        self.assertTrue(all(call == "list" for call in self.client.calls))

    def adapter(self) -> Any:
        from control_plane.testing_first_public_ingress import FirstTestingPublicIngressAdapter
        from control_plane.testing_public_ingress import TestingPublicIngressRequest

        return FirstTestingPublicIngressAdapter(
            self.root,
            self.store,
            self.profile.product,
            TestingPublicIngressRequest(
                context="example", reason="Publish initial testing ingress"
            ),
            "fixture-trace",
            self.client,
        )

    def reviewed(self) -> Any:
        adapter = self.adapter()
        adapter.request = adapter.request.model_copy(
            update={"mode": "apply", "reviewed_plan_sha256": adapter.plan()["plan_sha256"]}
        )
        return adapter

    async def test_publication_atomically_records_the_first_route_and_public_url(self) -> None:
        adapter = self.reviewed()
        self.assertFalse(self.tls.called)
        outcome = adapter.apply(
            "initial-edge-key", Mock(spec=["assert_current", "checkpoint_effect"])
        )
        self.assertTrue(outcome.response_payload["result"]["public_tls_verified"])
        self.assertEqual(self.client.calls.count("create"), 1)
        profile = self.store.read_product_profile_record(self.profile.product)
        self.assertEqual(profile.lanes[0].base_url, "https://" + self.domain)
        self.assertEqual(
            profile.lanes[0].health_url, "https://" + self.domain + self.profile.health_path
        )
        self.assertEqual(
            profile.model_dump(exclude={"lanes", "updated_at"}),
            self.profile.model_dump(exclude={"lanes", "updated_at"}),
        )
        self.assertIsNone(
            self.store.read_fresh_odoo_testing_generation(self.profile.product, "example")
        )
        self.assertEqual(self.store.list_deployment_records(), ())
        self.assertTrue(adapter.readback()["public_tls_verified"])

    async def test_holds_and_protected_data_refuse_before_effect(
        self,
    ) -> None:
        from tests.support.disposable_lanes import record_safety
        from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
        from control_plane.provider_operations import ProviderMutationRejectedError

        adapter = self.reviewed()
        protected = self.target.model_copy(
            update={
                "policies": self.target.policies.model_copy(
                    update={
                        "staff_testing_hold": DokployTargetStaffTestingHold(
                            recorded_at=self.profile.updated_at,
                            recorded_by="fixture",
                            reason="Staff are testing",
                        )
                    }
                )
            }
        )
        self.store.write_dokploy_target_record(protected)
        with self.assertRaises(ProviderMutationRejectedError):
            adapter.apply("initial-edge-key", Mock(spec=["assert_current", "checkpoint_effect"]))
        self.store.write_dokploy_target_record(self.target)
        for changes in (
            {"usage": "in_use"},
            {"data": "only_copy"},
            {"production_resources": "shared"},
        ):
            record_safety(self.store, self.profile.product, "example", "testing", **changes)
            with self.assertRaises(ProviderMutationRejectedError):
                adapter.apply(
                    "initial-edge-key", Mock(spec=["assert_current", "checkpoint_effect"])
                )
        self.assertNotIn("create", self.client.calls)

    async def test_unknown_create_is_adopted_under_original_custody_without_repeating_it(
        self,
    ) -> None:
        from control_plane.provider_operations import ProviderMutationUnknownError

        adapter = self.reviewed()
        create = self.client.create_proxy_host

        def lost_response(payload: Any) -> Any:
            create(payload)
            raise OSError("lost provider response")

        with patch.object(self.client, "create_proxy_host", side_effect=lost_response):
            with self.assertRaises(ProviderMutationUnknownError):
                adapter.apply(
                    "initial-edge-key", Mock(spec=["assert_current", "checkpoint_effect"])
                )
        observation = adapter.observe(
            "initial-edge-key", "create_testing_public_route", adapter.reconciliation_key()
        )
        self.assertEqual(observation.outcome, "present")
        self.assertEqual(self.client.calls.count("create"), 1)

    async def test_uncertain_absence_is_never_recreated(
        self,
    ) -> None:
        from control_plane.provider_operations import ProviderMutationUnknownError

        adapter = self.reviewed()
        with patch.object(
            self.client, "create_proxy_host", side_effect=OSError("unknown dispatch")
        ):
            with self.assertRaises(ProviderMutationUnknownError):
                adapter.apply(
                    "initial-edge-key", Mock(spec=["assert_current", "checkpoint_effect"])
                )
        self.assertEqual(
            adapter.observe(
                "initial-edge-key", "create_testing_public_route", adapter.reconciliation_key()
            ).outcome,
            "unknown",
        )
        self.assertNotIn("create", self.client.calls)

    async def test_origin_edge_and_certificate_drift_refuse_before_effect(self) -> None:
        adapter = self.reviewed()
        origin = self.routes[0].copy()
        self.routes[0]["port"] = self.profile.runtime_port + 1
        with self.assertRaises(ValueError):
            adapter.plan()
        self.routes[0] = origin
        for certificates in (
            (),
            (self.certificate, self.certificate.model_copy(update={"id": self.certificate.id + 1})),
        ):
            with patch.object(self.client, "list_certificates", return_value=certificates):
                with self.assertRaises(ValueError):
                    adapter.plan()
        other = self.endpoint.model_copy(update={"endpoint_key": "ambiguous"})
        self.store.write_edge_endpoint_record(other)
        with self.assertRaises(ValueError):
            adapter.plan()
        self.assertNotIn("create", self.client.calls)

    async def test_profile_drift_after_creation_keeps_the_original_effect_fenced(self) -> None:
        from control_plane.provider_operations import ProviderMutationUnknownError

        adapter = self.reviewed()
        publish = adapter._publish

        def change_profile(*args: Any, **kwargs: Any) -> Any:
            self.store.write_product_profile_record(
                self.profile.model_copy(update={"display_name": "Changed while publishing"})
            )
            return publish(*args, **kwargs)

        with patch.object(adapter, "_publish", side_effect=change_profile):
            with self.assertRaises(ProviderMutationUnknownError):
                adapter.apply(
                    "initial-edge-key", Mock(spec=["assert_current", "checkpoint_effect"])
                )
        with self.assertRaises(FileNotFoundError):
            self.store.read_route_binding_record(
                product=self.profile.product, context_name="example", instance_name="testing"
            )
        self.assertEqual(
            adapter.observe(
                "initial-edge-key", "create_testing_public_route", adapter.reconciliation_key()
            ).outcome,
            "unknown",
        )
        self.assertEqual(self.client.calls.count("create"), 1)

    async def test_busy_testing_executor_stops_before_any_route_effect(self) -> None:
        from contextlib import nullcontext
        from control_plane.provider_operations import ProviderMutationRejectedError

        adapter = self.reviewed()
        with patch.object(
            self.store, "testing_replacement_execution_lock", return_value=nullcontext(None)
        ):
            with self.assertRaises(ProviderMutationRejectedError):
                adapter.apply(
                    "initial-edge-key", Mock(spec=["assert_current", "checkpoint_effect"])
                )
        self.assertNotIn("create", self.client.calls)
