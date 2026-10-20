"""Execute edge continuation with real records and synthetic providers."""

from datetime import datetime, timedelta, timezone
from typing import Any
from unittest.mock import Mock, patch
import unittest

from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.dokploy_target_record import DokployTargetStaffTestingHold
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord
from control_plane.npmplus import NpmplusCertificate
from control_plane.odoo_testing_restore import FreshOdooTestingGeneration, generation_binding
from control_plane.contracts.odoo_testing_source import (
    NativeTestingCopyCapture,
    NativeTestingCopyArtifact,
)
from tests.test_odoo_testing_native_source import descriptor
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.provider_operations import (
    run_durable_provider_operation,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
)
from control_plane.testing_public_ingress import (
    ROUTE,
    TestingPublicIngressAdapter,
    TestingPublicIngressRequest,
)
from tests.test_testing_replacement_bootstrap import FreshTestingBootstrapScenario
from tests.support.ingress import _FakeNpmplusIngressClient, _npmplus_proxy_host
from tests.support.http import request
from tests.support.auth import scoped_agent_transport


class EdgeClient(_FakeNpmplusIngressClient):
    def __init__(self) -> None:
        super().__init__(
            (
                _npmplus_proxy_host().model_copy(
                    update={
                        "domain_names": ("retained.example.invalid",),
                        "forward_host": "192.0.2.20",
                        "forward_port": 443,
                        "forward_scheme": "https",
                        "advanced_config": "",
                        "locations": (),
                        "meta": {},
                    }
                ),
            )
        )
        self.certificate = NpmplusCertificate(
            id=int(self.proxy_hosts[0].certificate_id),
            domain_names=("*.example.invalid",),
            expires_on=(datetime.now(timezone.utc) + timedelta(days=30))
            .replace(tzinfo=None)
            .isoformat(sep=" "),
        )
        self.other_domains: tuple[str, ...] = ()
        self.restricted_hosts: set[int] = set()

    def list_other_host_domains(self) -> tuple[str, ...]:
        return self.other_domains

    def get_proxy_host(self, host_id: int, *, public_only: bool = False) -> Any:
        if public_only and host_id in self.restricted_hosts:
            raise ValueError("The fixture host has private TLS or unloaded config")
        return next(host for host in self.proxy_hosts if host.id == host_id)

    def get_certificate(self, certificate_id: int) -> NpmplusCertificate:
        if certificate_id != self.certificate.id:
            raise ValueError("Unknown fixture certificate")
        return self.certificate


class TestingPublicIngressScenario(FreshTestingBootstrapScenario):
    database_url = ""

    def setUp(self) -> None:
        self.seed(self.database_url)
        self.patches()
        source_observation = descriptor().model_copy(
            update={"product": self.profile.product, "context": self.fixture.context}
        )
        generation = FreshOdooTestingGeneration(
            product=self.profile.product,
            context=self.fixture.context,
            operation_id=self.fixture.operation.operation_id,
            target_id=self.resources.target_id,
            namespace=self.resources.namespace,
            domain_host=self.request.domain_host,
            resources_binding_sha256=self.resources.binding_sha256,
            bootstrap_plan_sha256="a" * 64,
            request_sha256="b" * 64,
            prepared_at=self.profile.updated_at,
            source_mode="container-export",
            source_binding_sha256=source_observation.binding_sha256,
        )
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={
                    "lanes": tuple(
                        lane.model_copy(
                            update={
                                "base_url": "https://" + generation.domain_host,
                                "health_url": "https://"
                                + generation.domain_host
                                + "/launchplane/health",
                            }
                        )
                        if lane.instance == "testing"
                        else lane
                        for lane in self.profile.lanes
                    )
                }
            )
        )
        target = self.store.read_dokploy_target_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "domains": (generation.domain_host,),
                    "env": {
                        **target.env,
                        "ODOO_PROJECT_NAME": generation.namespace,
                        "ODOO_STACK_NAME": generation.namespace,
                    },
                }
            )
        )
        self.store.write_fresh_odoo_testing_generation(generation)
        self.copy_witness = NativeTestingCopyCapture(
            product=self.profile.product,
            context=self.fixture.context,
            generation_binding_sha256=generation_binding(generation),
            source_binding_sha256=source_observation.binding_sha256,
            capture_id="c" * 32,
            database=NativeTestingCopyArtifact(sha256="d" * 64, size=10),
            filestore=NativeTestingCopyArtifact(sha256="f" * 64, size=20),
            captured_at=generation.prepared_at,
            source_observation=source_observation,
        )
        self.store.write_native_testing_copy_capture(self.copy_witness)
        from tests.support.disposable_lanes import record_safety

        preparation = self.store.read_disposable_replacement_preparation(
            self.fixture.operation.operation_id
        )
        assert preparation is not None
        record_safety(
            self.store,
            self.profile.product,
            self.fixture.context,
            "testing",
            replaces_operation_id=self.fixture.operation.operation_id,
            replaced_target_id=preparation.retained_target_id,
            production_resources="separation_ready",
        )
        self.assertTrue(self.store.supersede_disposable_testing_replacement(self.fixture.operation))
        superseded = self.store.read_odoo_stable_target_replacement_operation_record(
            self.fixture.operation.operation_id
        )
        self.assertEqual(superseded.status, "superseded")
        self.store.request_fresh_testing_reconcile(generation)
        self.client = EdgeClient()
        self.endpoint = EdgeEndpointRecord(
            endpoint_key="fixture-edge",
            server_name="fixture-server",
            upstream_host="192.0.2.20",
            upstream_port=443,
            updated_at=self.profile.updated_at,
        )
        self.store.write_edge_endpoint_record(self.endpoint)
        self.old_binding = EnvironmentRouteBindingRecord.model_validate(
            {
                "product": self.profile.product,
                "context": self.fixture.context,
                "instance": "testing",
                "provider_target": {
                    "provider_id": "dokploy",
                    "target_category": "compose",
                    "target_name": "retained-target",
                },
                "ingress": {
                    "provider": "npmplus",
                    "endpoint_key": self.endpoint.endpoint_key,
                    "termination_kind": "edge",
                },
                "domains": [{"domain_name": "retained.example.invalid"}],
                "tls": {"owner": "provider"},
                "status": "active",
                "source": {
                    "source_kind": "service",
                    "source_label": "fixture",
                    "refreshed_at": self.profile.updated_at,
                },
                "updated_at": self.profile.updated_at,
            }
        )
        self.store.write_route_binding_record(self.old_binding)
        self.edge_request = TestingPublicIngressRequest(
            context=self.fixture.context,
            operation_id=self.fixture.operation.operation_id,
            reason="Continue the current restored testing generation",
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
                return_value={"composeId": "fresh-compose", "serverId": "fixture-server-id"},
            )
        )
        self.server = {
            "serverId": "fixture-server-id",
            "name": self.endpoint.server_name,
            "ipAddress": self.endpoint.upstream_host,
        }
        self.enterContext(
            patch(
                "control_plane.testing_public_ingress.api.dokploy_request",
                side_effect=lambda **_: self.server,
            )
        )
        self.edge_lease = Mock(spec=["assert_current", "checkpoint_effect"])

    def edge(self) -> TestingPublicIngressAdapter:
        return TestingPublicIngressAdapter(
            self.fixture.root,
            self.store,
            self.profile.product,
            self.edge_request,
            "fixture-trace",
            self.client,
        )

    def review_edge(self) -> None:
        self.edge_request = self.edge_request.model_copy(
            update={
                "mode": "apply",
                "reviewed_plan_sha256": self.edge().plan()["plan_sha256"],
            }
        )

    def publish(self) -> Any:
        return self.edge().apply("fixture-provider-key", self.edge_lease)


class TestingPublicIngressTests(TestingPublicIngressScenario):
    def test_private_or_unloaded_template_refuses_before_creation(self) -> None:
        self.client.restricted_hosts.add(self.client.proxy_hosts[0].id)
        with self.assertRaises(ValueError):
            self.edge().plan()
        self.assertNotIn("create", self.client.calls)

    def test_other_provider_host_types_refuse_before_creation(self) -> None:
        self.client.other_domains = (self.request.domain_host,)
        with self.assertRaises(ValueError):
            self.edge().plan()
        self.assertNotIn("create", self.client.calls)

    def test_audited_generated_comment_can_template_a_later_testing_generation(self) -> None:
        self.review_edge()
        self.publish()
        binding = self.store.read_route_binding_record(
            product=self.profile.product, context_name=self.fixture.context, instance_name="testing"
        )
        self.assertTrue(self.edge()._clean_template(self.client.proxy_hosts[-1], binding))
        changed = self.client.proxy_hosts[-1].model_copy(
            update={"advanced_config": "return 302 /foreign;"}
        )
        self.assertFalse(self.edge()._clean_template(changed, binding))

    def test_missing_testing_lane_is_rejected_without_stranding_provider_custody(self) -> None:
        self.review_edge()
        profile = self.store.read_product_profile_record(self.profile.product)
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": tuple(lane for lane in profile.lanes if lane.instance != "testing")
                }
            )
        )
        with self.assertRaises(ProviderMutationRejectedError):
            self.publish()
        self.assertNotIn("create", self.client.calls)

    def test_pending_recovery_refuses_changed_binding_authority(self) -> None:
        self.review_edge()
        create = self.client.create_proxy_host

        def lost(payload: Any) -> Any:
            create(payload)
            raise OSError("fixture lost response")

        with patch.object(self.client, "create_proxy_host", side_effect=lost):
            with self.assertRaises(ProviderMutationUnknownError):
                self.publish()
        changed = self.old_binding.model_copy(
            update={
                "provider_target": self.old_binding.provider_target.model_copy(
                    update={"target_name": "new-authority"}
                )
            }
        )
        self.store.write_route_binding_record(changed)
        self.assertEqual(
            self.edge()
            .observe("fixture-provider-key", "create_testing_public_route", "fixture")
            .outcome,
            "unknown",
        )
        self.assertEqual(self.client.calls.count("create"), 1)
        self.assertEqual(
            self.store.read_route_binding_record(
                product=self.profile.product,
                context_name=self.fixture.context,
                instance_name="testing",
            ),
            changed,
        )

    def test_recovery_tolerates_metadata_refresh_but_keeps_original_route_custody(self) -> None:
        self.review_edge()
        create = self.client.create_proxy_host

        def lost(payload: Any) -> Any:
            create(payload)
            raise OSError("fixture lost response")

        with patch.object(self.client, "create_proxy_host", side_effect=lost):
            with self.assertRaises(ProviderMutationUnknownError):
                self.publish()
        profile = self.store.read_product_profile_record(self.profile.product)
        self.store.write_product_profile_record(
            profile.model_copy(update={"display_name": "Renamed"})
        )
        self.client.certificate = self.client.certificate.model_copy(
            update={"expires_on": (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()}
        )
        self.client.proxy_hosts[0].meta["nginx_online"] = True
        self.store.write_route_binding_record(
            self.old_binding.model_copy(
                update={
                    "source": self.old_binding.source.model_copy(
                        update={"source_label": "refreshed"}
                    )
                }
            )
        )
        self.assertEqual(
            self.edge()
            .observe("fixture-provider-key", "create_testing_public_route", "fixture")
            .outcome,
            "present",
        )
        self.assertEqual(self.client.calls.count("create"), 1)
        binding = self.store.read_route_binding_record(
            product=self.profile.product, context_name=self.fixture.context, instance_name="testing"
        )
        self.store.write_route_binding_record(
            binding.model_copy(
                update={
                    "source": binding.source.model_copy(update={"source_label": "ordinary-refresh"})
                }
            )
        )
        self.assertTrue(self.edge().readback()["applied"])

    def test_held_or_reassigned_generation_refuses_before_provider_effect(self) -> None:
        self.review_edge()
        target = self.store.read_dokploy_target_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        self.store.write_dokploy_target_record(
            target.model_copy(
                update={
                    "policies": target.policies.model_copy(
                        update={
                            "staff_testing_hold": DokployTargetStaffTestingHold(
                                reason="staff session",
                                recorded_by="fixture",
                                recorded_at=self.profile.updated_at,
                            )
                        }
                    )
                }
            )
        )
        protected = self.store.read_fresh_odoo_testing_generation(
            self.profile.product, self.fixture.context
        )
        with self.assertRaises(ProviderMutationRejectedError):
            self.publish()
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(
                self.profile.product, self.fixture.context
            ),
            protected,
        )
        self.store.write_dokploy_target_record(target)
        target_id = self.store.read_dokploy_target_id_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        self.store.write_dokploy_target_id_record(
            target_id.model_copy(update={"target_id": "foreign-target"})
        )
        with self.assertRaises(ProviderMutationRejectedError):
            self.publish()
        self.assertNotIn("create", self.client.calls)

    def test_inconsistent_provider_projection_refuses_during_planning(self) -> None:
        provider = self.store.read_provider_target_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        self.store.write_provider_target_record(
            provider.model_copy(update={"display_name": "foreign-target"})
        )
        with self.assertRaises(ValueError):
            self.edge().plan()
        self.assertNotIn("create", self.client.calls)

    def test_binding_cas_rejects_candidate_from_another_audit(self) -> None:
        self.review_edge()
        original = self.store.publish_testing_public_ingress_binding

        def corrupt(**kwargs: Any) -> Any:
            candidate = kwargs["replacement"]
            kwargs["replacement"] = candidate.model_copy(
                update={
                    "ingress": candidate.ingress.model_copy(
                        update={"provider_evidence": {"audit_record": "foreign-audit"}}
                    )
                }
            )
            return original(**kwargs)

        with patch.object(
            self.store, "publish_testing_public_ingress_binding", side_effect=corrupt
        ):
            with self.assertRaises(ProviderMutationUnknownError):
                self.publish()
        self.assertEqual(
            self.store.read_route_binding_record(
                product=self.profile.product,
                context_name=self.fixture.context,
                instance_name="testing",
            ),
            self.old_binding,
        )

    def test_original_key_recovers_unknown_create_and_replays_once(self) -> None:
        if self.store.database_dialect_name != "postgresql":
            self.skipTest("Durable heartbeat fencing requires the explicit PostgreSQL fixture.")
        self.review_edge()

        def run() -> Any:
            return run_durable_provider_operation(
                store=self.store,
                scope="fixture-director",
                route_path=ROUTE.format(product=self.profile.product),
                idempotency_key="original-edge-key",
                request_fingerprint=canonical_json_sha256(
                    self.edge_request.model_dump(mode="json")
                ),
                lease_owner="fixture-lease",
                response_trace_id="fixture-trace",
                adapter=self.edge(),
            )

        create = self.client.create_proxy_host

        def lost(payload: Any) -> Any:
            create(payload)
            raise OSError("fixture lost response")

        with patch.object(self.client, "create_proxy_host", side_effect=lost):
            first = run()
        self.assertEqual(first.status, "reconcile_required")
        self.client.certificate = self.client.certificate.model_copy(
            update={"expires_on": (datetime.now(timezone.utc) + timedelta(days=90)).isoformat()}
        )
        self.client.proxy_hosts[0].meta["nginx_online"] = True
        self.assertEqual(run().status, "adopted")
        self.assertEqual(run().status, "replayed")
        self.assertEqual(self.client.calls.count("create"), 1)

    def test_create_and_binding_preserve_restored_generation_and_retained_route(self) -> None:
        generation = self.store.read_fresh_odoo_testing_generation(
            self.profile.product, self.fixture.context
        )
        target = self.store.read_dokploy_target_record(
            context_name=self.fixture.context, instance_name="testing"
        )
        profile = self.store.read_product_profile_record(self.profile.product)
        retained = self.client.proxy_hosts[0].model_copy(deep=True)
        self.review_edge()
        self.assertNotIn("create", self.client.calls)
        result = self.publish()
        self.assertTrue(result.response_payload["result"]["applied"])
        self.assertFalse(result.response_payload["result"]["public_health_verified"])
        self.assertEqual(self.client.calls.count("create"), 1)
        self.assertEqual(self.client.proxy_hosts[0], retained)
        self.assertEqual(
            self.store.read_native_testing_copy_capture(
                self.copy_witness.generation_binding_sha256
            ),
            self.copy_witness,
        )
        self.assertEqual(
            self.store.read_fresh_odoo_testing_generation(
                self.profile.product, self.fixture.context
            ),
            generation,
        )
        self.assertEqual(self.store.read_product_profile_record(self.profile.product), profile)
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.fixture.context, instance_name="testing"
            ),
            target,
        )
        self.assertEqual(
            self.store.read_dokploy_target_record(
                context_name=self.fixture.context, instance_name="prod"
            ),
            self.production_before,
        )
        binding = self.store.read_route_binding_record(
            product=self.profile.product, context_name=self.fixture.context, instance_name="testing"
        )
        assert generation is not None
        self.assertEqual(tuple(d.domain_name for d in binding.domains), (generation.domain_host,))
        self.assertEqual(binding.provider_target.target_name, target.target_name)
        self.assertTrue(self.edge().readback()["applied"])

    def test_absent_unknown_effect_is_never_created_again(self) -> None:
        self.review_edge()
        observation = self.edge().observe(
            "fixture-provider-key", "create_testing_public_route", "fixture"
        )
        self.assertEqual(observation.outcome, "unknown")
        self.assertNotIn("create", self.client.calls)

    def test_lost_create_reply_adopts_exact_host_without_second_create(self) -> None:
        self.review_edge()
        create = self.client.create_proxy_host

        def lost(payload: Any) -> Any:
            create(payload)
            raise OSError("fixture lost response")

        with patch.object(self.client, "create_proxy_host", side_effect=lost):
            with self.assertRaises(ProviderMutationUnknownError):
                self.publish()
        observation = self.edge().observe(
            "fixture-provider-key", "create_testing_public_route", "fixture"
        )
        self.assertEqual(observation.outcome, "present")
        self.assertEqual(self.client.calls.count("create"), 1)
        self.assertTrue(self.edge().readback()["applied"])

    def test_endpoint_drift_and_foreign_route_refuse_without_provider_write(self) -> None:
        self.review_edge()
        self.server["ipAddress"] = "192.0.2.99"
        with self.assertRaises(ProviderMutationRejectedError):
            self.publish()
        self.assertNotIn("create", self.client.calls)
        self.server["ipAddress"] = self.endpoint.upstream_host
        self.client.proxy_hosts.append(
            self.client.proxy_hosts[0].model_copy(
                update={"id": 999, "domain_names": (self.request.domain_host,)}
            )
        )
        with self.assertRaises(ValueError):
            self.edge().plan()
        self.assertNotIn("create", self.client.calls)

    def test_certificate_must_cover_exact_hostname_and_be_current(self) -> None:
        original = self.client.certificate
        for changes in (
            {"domain_names": ("*.elsewhere.invalid",)},
            {"domain_names": ("*.invalid",)},
            {"expires_on": (datetime.now(timezone.utc) - timedelta(days=1)).isoformat()},
        ):
            with self.subTest(changes=changes):
                self.client.certificate = original.model_copy(update=changes)
                with self.assertRaises(ValueError):
                    self.edge().plan()
        self.assertNotIn("create", self.client.calls)

    def test_actual_route_drift_is_not_hidden_by_preserved_ownership_marker(self) -> None:
        self.review_edge()
        self.publish()
        self.client.proxy_hosts[-1] = self.client.proxy_hosts[-1].model_copy(
            update={"forward_port": 444}
        )
        with self.assertRaises(ValueError):
            self.edge().readback()

    def test_foreign_binding_cannot_be_overwritten_after_provider_create(self) -> None:
        self.review_edge()
        original = self.store.publish_testing_public_ingress_binding
        foreign = self.old_binding.model_copy(
            update={
                "source": self.old_binding.source.model_copy(update={"source_kind": "operator"})
            }
        )

        def change(**kwargs: Any) -> Any:
            self.store.write_route_binding_record(foreign)
            return original(**kwargs)

        with patch.object(self.store, "publish_testing_public_ingress_binding", side_effect=change):
            with self.assertRaises(ProviderMutationUnknownError):
                self.publish()
        self.assertEqual(
            self.store.read_route_binding_record(
                product=self.profile.product,
                context_name=self.fixture.context,
                instance_name="testing",
            ),
            foreign,
        )


class TestingPublicIngressHttpTests(TestingPublicIngressScenario, unittest.IsolatedAsyncioTestCase):
    async def test_only_director_agent_and_exact_testing_input_reach_planning(self) -> None:
        with patch(
            "control_plane.http_routes.testing_public_ingress.build_npmplus_ingress_client_from_env",
            return_value=self.client,
        ):
            payload = self.edge_request.model_dump(mode="json")
            response = await request(
                self.fixture.app,
                "POST",
                ROUTE.format(product=self.profile.product),
                headers={"Authorization": "Bearer local-operator-token"},
                payload=payload,
            )
            self.assertEqual(response.status_code, 202, response.text)
            with scoped_agent_transport():
                denied = await request(
                    self.fixture.app,
                    "POST",
                    ROUTE.format(product=self.profile.product),
                    headers={"Authorization": "Bearer local-operator-token"},
                    payload=payload,
                )
            self.assertEqual(denied.status_code, 403)
            for changes in (
                {"instance": "prod"},
                {"domain_host": "foreign.invalid"},
                {"context": "other"},
            ):
                rejected = await request(
                    self.fixture.app,
                    "POST",
                    ROUTE.format(product=self.profile.product),
                    headers={"Authorization": "Bearer local-operator-token"},
                    payload={**payload, **changes},
                )
                self.assertGreaterEqual(rejected.status_code, 400)
        self.assertNotIn("create", self.client.calls)
