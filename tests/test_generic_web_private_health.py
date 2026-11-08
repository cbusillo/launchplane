from itertools import count
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import Mock, patch

import click

from control_plane.contracts.private_health_endpoint_record import (
    PrivateHealthEndpointRecord,
    private_health_endpoint_record_sha256,
)
from control_plane.contracts.product_profile_record import (
    ProductLaneHealthCheck,
    ProductLaneHealthMonitoringPolicy,
)
from control_plane.workflows import public_ingress_monitor as monitor
from control_plane.workflows.runtime_identity_health import (
    HealthcheckPass,
    wait_for_runtime_identity_healthcheck_with_retry,
)
from tests import test_generic_web_promotion as fixture
from tests.test_private_testing_trust import certificate_fixture
from control_plane.outbound_http import PublicHttpDestinationError, PublicHttpResponse
from control_plane.storage.postgres import PostgresRecordStore


class GenericWebPrivateHealthTests(unittest.TestCase):
    def setUp(self) -> None:
        fixture.prepare_production_promotion_test(self)
        self.store = fixture._store_with_production()
        self.endpoints: dict[str, PrivateHealthEndpointRecord] = {}
        lanes = []
        for lane in self.store.profile.lanes:
            key = "private-" + lane.instance
            self.endpoints[key] = PrivateHealthEndpointRecord(
                endpoint_key=key,
                product=self.store.profile.product,
                context=lane.context,
                instance=lane.instance,
                url=f"https://{lane.instance}.internal/health",
                updated_at=self.store.profile.updated_at,
            )
            lanes.append(
                lane.model_copy(
                    update={
                        "base_url": "",
                        "health_url": "",
                        "health_monitoring": ProductLaneHealthMonitoringPolicy(
                            monitoring_intent="private",
                            checks=(
                                ProductLaneHealthCheck(
                                    name="runtime",
                                    kind="private_http",
                                    private_endpoint_key=key,
                                ),
                            ),
                        ),
                    }
                )
            )
        self.store.profile = self.store.profile.model_copy(update={"lanes": tuple(lanes)})
        self.provider = fixture._ProductionProvider()
        self.requested: list[str] = []
        self.failure = ""
        self.public_failure = True

    def observe(self, url: str, _timeout: int, **_kwargs: Any) -> monitor.HttpObservation:
        self.requested.append(url)
        endpoint = next(endpoint for endpoint in self.endpoints.values() if endpoint.url == url)
        inventory = self.store.read_environment_inventory(
            context_name=endpoint.context, instance_name=endpoint.instance
        )
        identity = inventory.runtime_identity
        if endpoint.instance == "prod":
            identity = self.provider.running
        assert identity is not None
        candidate = self.store.read_environment_inventory(
            context_name=endpoint.context, instance_name="testing"
        ).artifact_identity
        assert candidate is not None
        failed = endpoint.instance == "testing" and self.failure == "source"
        failed |= (
            endpoint.instance == "prod"
            and identity.artifact_id == candidate.artifact_id
            and bool(self.failure)
        )
        if failed and self.failure == "unreachable":
            raise ConnectionRefusedError("inert refusal")
        if failed and self.failure == "wrong_identity":
            identity = identity.model_copy(update={"source_git_ref": "foreign"})
        return monitor.HttpObservation(
            status_code=503 if failed and self.failure != "wrong_identity" else 200,
            final_url=url,
            redirect_count=0,
            payload={"runtime_identity": identity.model_dump(mode="json")},
        )

    def promote(self) -> Any:
        def bounded_identity_check(**kwargs: Any) -> Any:
            if "wait_once" not in kwargs:
                if self.public_failure:
                    raise click.ClickException("Inert public health failure")
                assert self.provider.running is not None
                return HealthcheckPass(
                    payload={"runtime_identity": self.provider.running.model_dump(mode="json")}
                )
            clock = count()
            return wait_for_runtime_identity_healthcheck_with_retry(
                **{**kwargs, "sleep": lambda _seconds: None, "monotonic": lambda: next(clock)}
            )

        with (
            patch.object(
                self.store,
                "read_private_health_endpoint_record",
                side_effect=self.endpoints.__getitem__,
                create=True,
            ),
            patch.object(monitor, "fetch_private_health_url", side_effect=self.observe),
            patch.object(self.provider, "healthcheck", side_effect=bounded_identity_check),
        ):
            return fixture.GenericWebProdPromotionRollbackTests._promote(self.store, self.provider)

    def test_private_release_verifies_source_and_exact_replacement(self) -> None:
        result = self.promote()
        self.assertEqual(result.promotion_status, "pass")
        promotion = self.store.promotions[result.promotion_record_id]
        for evidence, instance in (
            (promotion.source_health, "testing"),
            (promotion.destination_health, "prod"),
        ):
            self.assertEqual(evidence.status, "pass")
            self.assertTrue(evidence.verified)
            self.assertEqual(evidence.runtime_identity_status, "match")
            self.assertEqual(evidence.urls, (self.endpoints["private-" + instance].url,))
        self.assertEqual(
            promotion.destination_health.observed_runtime_identity, self.provider.running
        )

    def test_unavailable_or_wrong_replacement_rolls_back_and_verifies_retained_identity(
        self,
    ) -> None:
        for failure in ("unreachable", "unhealthy", "wrong_identity"):
            with self.subTest(failure=failure):
                self.failure = failure
                result = self.promote()
                self.assertEqual(result.promotion_status, "fail")
                self.assertEqual(result.destination_health_status, "fail")
                self.assertEqual(result.rollback_status, "pass")
                promotion = self.store.promotions[result.promotion_record_id]
                self.assertEqual(promotion.rollback_health.runtime_identity_status, "match")
                self.assertEqual(
                    promotion.rollback_health.observed_runtime_identity, self.provider.running
                )
                assert self.provider.running is not None
                self.assertEqual(self.provider.running.artifact_id, fixture._PREVIOUS_ARTIFACT)

    def test_unhealthy_private_source_never_deploys(self) -> None:
        self.failure = "source"
        result = self.promote()
        self.assertEqual(result.promotion_status, "fail")
        self.assertEqual(result.source_health_status, "fail")
        self.assertEqual(self.provider.deployed_artifacts, [])

    def test_all_enabled_private_checks_must_pass(self) -> None:
        lane = self.store.profile.lanes[1]
        extra = self.endpoints["private-prod"].model_copy(
            update={"endpoint_key": "extra", "url": "https://extra.internal/health"}
        )
        self.endpoints[extra.endpoint_key] = extra
        policy = lane.health_monitoring.model_copy(
            update={
                "checks": (
                    *lane.health_monitoring.checks,
                    ProductLaneHealthCheck(
                        name="extra", kind="private_http", private_endpoint_key=extra.endpoint_key
                    ),
                )
            }
        )
        self.store.profile = self.store.profile.model_copy(
            update={
                "lanes": (
                    self.store.profile.lanes[0],
                    lane.model_copy(update={"health_monitoring": policy}),
                )
            }
        )
        observe = self.observe

        def unavailable_extra(url: str, timeout: int, **kwargs: Any) -> monitor.HttpObservation:
            if url == extra.url and self.provider.running is not None:
                candidate = self.store.inventories[(lane.context, "testing")].artifact_identity
                assert candidate is not None
                if self.provider.running.artifact_id == candidate.artifact_id:
                    raise ConnectionRefusedError("second required check is unavailable")
            return observe(url, timeout, **kwargs)

        with patch.object(self, "observe", side_effect=unavailable_extra):
            result = self.promote()
        self.assertEqual(result.promotion_status, "fail")
        self.assertEqual(result.rollback_status, "pass")

    def enable_public_checks(self) -> None:
        original = fixture._profile()
        self.store.profile = self.store.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(update={"health_url": public.health_url})
                    for lane, public in zip(self.store.profile.lanes, original.lanes, strict=True)
                )
            }
        )

    def test_private_checks_keep_existing_public_health_verification(self) -> None:
        self.enable_public_checks()
        result = self.promote()
        self.assertEqual(result.promotion_status, "fail")

    def test_mixed_lane_passes_when_both_public_and_private_checks_pass(self) -> None:
        self.enable_public_checks()
        self.public_failure = False
        result = self.promote()
        self.assertEqual(result.promotion_status, "pass")
        promotion = self.store.promotions[result.promotion_record_id]
        self.assertIn(self.store.profile.lanes[1].health_url, promotion.destination_health.urls)
        self.assertIn(self.endpoints["private-prod"].url, promotion.destination_health.urls)

    def test_testing_ca_reaches_the_shared_private_transport(self) -> None:
        endpoint = self.endpoints["private-testing"]
        ca, _, _ = certificate_fixture()
        self.endpoints[endpoint.endpoint_key] = PrivateHealthEndpointRecord.model_validate(
            {**endpoint.model_dump(), "tls_ca_pem": ca}
        )
        observe = self.observe

        def trusted(url: str, timeout: int, **kwargs: Any) -> monitor.HttpObservation:
            if url == endpoint.url:
                self.assertEqual(
                    kwargs.get("tls_ca_pem"), self.endpoints[endpoint.endpoint_key].tls_ca_pem
                )
            return observe(url, timeout, **kwargs)

        with patch.object(self, "observe", side_effect=trusted):
            self.assertEqual(self.promote().promotion_status, "pass")

    def test_testing_edge_uses_verified_route_only_after_private_address_refusal(self) -> None:
        endpoint = self.endpoints["private-testing"].model_copy(
            update={"url": "https://testing.example.com/health"}
        )
        root = Path("/inert/control-plane")
        response = PublicHttpResponse(
            status_code=200,
            final_url=endpoint.url,
            redirect_count=0,
            headers=(("content-type", "application/json"),),
            body=b'{"status":"ok"}',
        )
        store = Mock(spec=PostgresRecordStore)
        authority = private_health_endpoint_record_sha256(endpoint)
        with (
            patch.object(
                monitor,
                "fetch_private_health_url",
                side_effect=PublicHttpDestinationError("private_url", "inert"),
            ),
            patch(
                "control_plane.testing_private_health.fetch_testing_private_health",
                return_value=response,
            ) as edge,
        ):
            observed = monitor.fetch_scoped_private_health(
                record_store=store,
                product=endpoint.product,
                context=endpoint.context,
                instance=endpoint.instance,
                endpoint_key=endpoint.endpoint_key,
                endpoint_sha256=authority,
                url=endpoint.url,
                timeout_seconds=3,
                require_runtime_identity=True,
                root=root,
            )
        self.assertEqual(observed.status_code, response.status_code)
        self.assertEqual(edge.call_args.kwargs["root"], root)
        self.assertEqual(edge.call_args.kwargs["endpoint_key"], endpoint.endpoint_key)
        self.assertEqual(edge.call_args.kwargs["endpoint_sha256"], authority)

    def test_missing_disabled_or_foreign_private_authority_never_deploys(self) -> None:
        original = self.endpoints["private-prod"]
        for replacement in (
            None,
            original.model_copy(update={"status": "disabled"}),
            original.model_copy(update={"product": "foreign"}),
            original.model_copy(update={"instance": "testing"}),
        ):
            with self.subTest(replacement=replacement):
                if replacement is None:
                    self.endpoints.pop("private-prod", None)
                else:
                    self.endpoints["private-prod"] = replacement
                with self.assertRaises(click.ClickException):
                    self.promote()
                self.assertEqual(self.provider.deployed_artifacts, [])
        self.endpoints["private-prod"] = original
