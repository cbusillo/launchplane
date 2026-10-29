from itertools import count
from typing import Any
import unittest
from unittest.mock import patch

import click

from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.product_profile_record import (
    ProductLaneHealthCheck,
    ProductLaneHealthMonitoringPolicy,
)
from control_plane.workflows import public_ingress_monitor as monitor
from control_plane.workflows.runtime_identity_health import (
    wait_for_runtime_identity_healthcheck_with_retry,
)
from tests import test_generic_web_promotion as fixture


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
                raise click.ClickException("Inert public health failure")
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

    def test_private_checks_keep_existing_public_health_verification(self) -> None:
        original = fixture._profile()
        self.store.profile = self.store.profile.model_copy(
            update={
                "lanes": tuple(
                    lane.model_copy(update={"health_url": public.health_url})
                    for lane, public in zip(self.store.profile.lanes, original.lanes, strict=True)
                )
            }
        )
        result = self.promote()
        self.assertEqual(result.promotion_status, "fail")

    def test_private_intent_with_no_enabled_check_refuses_before_deploy(self) -> None:
        lane = self.store.profile.lanes[1]
        checks = tuple(
            check.model_copy(update={"enabled": False}) for check in lane.health_monitoring.checks
        )
        self.store.profile = self.store.profile.model_copy(
            update={
                "lanes": (
                    self.store.profile.lanes[0],
                    lane.model_copy(
                        update={
                            "health_monitoring": lane.health_monitoring.model_copy(
                                update={"checks": checks}
                            )
                        }
                    ),
                )
            }
        )
        with self.assertRaises(click.ClickException):
            self.promote()
        self.assertEqual(self.provider.deployed_artifacts, [])

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
