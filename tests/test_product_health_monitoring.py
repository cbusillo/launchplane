from __future__ import annotations

import unittest
from typing import cast

from pydantic import ValidationError

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.product_health_monitoring import (
    ProductHealthMonitoringApplyRequest,
    ProductHealthMonitoringCheckKindError,
    ProductHealthMonitoringTargetError,
    build_product_health_monitoring_plan,
    updated_product_health_monitoring_profile,
)
from tests.support.profiles import _product_profile_payload


def _profile() -> LaunchplaneProductProfileRecord:
    payload = _product_profile_payload()
    payload["product"] = "odoo-product"
    payload["driver_id"] = "odoo"
    payload["updated_at"] = "2026-07-22T00:00:00Z"
    payload["source"] = "existing-source"
    lanes = cast(list[dict[str, object]], payload["lanes"])
    lanes[0]["context"] = "cm"
    lanes[0]["instance"] = "testing"
    lanes[0]["base_url"] = "https://cm-testing.example.com"
    lanes[0]["health_url"] = "https://cm-testing.example.com/launchplane/health"
    lanes[0]["health_monitoring"] = {
        "monitoring_intent": "public",
        "checks": [
            {
                "name": "public-ingress",
                "kind": "public_http",
                "enabled": True,
                "url": "",
                "require_runtime_identity": False,
            },
            {
                "name": "provider-health",
                "kind": "provider",
                "enabled": True,
                "provider": "example",
                "provider_check": "ready",
            },
        ],
    }
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _request(
    *,
    mode: str = "dry-run",
    check_name: str = "public-ingress",
    check_kind: str = "public_http",
    monitoring_intent: str = "public",
    enabled: bool = True,
    require_runtime_identity: bool = True,
    private_endpoint_key: str = "",
    reviewed_plan_sha256: str = "",
) -> ProductHealthMonitoringApplyRequest:
    return ProductHealthMonitoringApplyRequest.model_validate(
        {
            "product": "odoo-product",
            "context": "cm",
            "instance": "testing",
            "check_name": check_name,
            "check_kind": check_kind,
            "monitoring_intent": monitoring_intent,
            "enabled": enabled,
            "require_runtime_identity": require_runtime_identity,
            "private_endpoint_key": private_endpoint_key,
            "mode": mode,
            "reason": "Require strict public runtime identity.",
            "reviewed_plan_sha256": reviewed_plan_sha256,
        }
    )


class ProductHealthMonitoringTests(unittest.TestCase):
    def test_dry_run_builds_deterministic_exact_lane_plan(self) -> None:
        profile = _profile()
        plan = build_product_health_monitoring_plan(profile=profile, request=_request())

        self.assertEqual(plan.operation, "update")
        self.assertEqual(plan.current_enabled, True)
        self.assertEqual(plan.current_monitoring_intent, "public")
        self.assertEqual(plan.requested_monitoring_intent, "public")
        self.assertEqual(plan.current_require_runtime_identity, False)
        self.assertEqual(plan.requested_require_runtime_identity, True)
        self.assertEqual(
            plan.resolved_url,
            "https://cm-testing.example.com/launchplane/health",
        )
        self.assertTrue(plan.changed)
        self.assertRegex(plan.profile_sha256_before, r"^[0-9a-f]{64}$")
        self.assertRegex(plan.plan_sha256, r"^[0-9a-f]{64}$")

        apply_plan = build_product_health_monitoring_plan(
            profile=profile,
            request=_request(mode="apply", reviewed_plan_sha256=plan.plan_sha256),
        )
        self.assertEqual(apply_plan.plan_sha256, plan.plan_sha256)

    def test_update_preserves_unrelated_profile_and_check_fields(self) -> None:
        profile = _profile()
        plan = build_product_health_monitoring_plan(profile=profile, request=_request())
        request = _request(mode="apply", reviewed_plan_sha256=plan.plan_sha256)

        updated = updated_product_health_monitoring_profile(
            profile=profile,
            request=request,
            updated_at="2026-07-22T01:00:00Z",
        )

        expected = profile.model_dump(mode="json")
        expected["lanes"][0]["health_monitoring"]["checks"][0]["require_runtime_identity"] = True
        expected["updated_at"] = "2026-07-22T01:00:00Z"
        expected["source"] = "service:product-health-monitoring"
        self.assertEqual(updated.model_dump(mode="json"), expected)

    def test_missing_check_is_created_without_caller_topology(self) -> None:
        profile = _profile()
        request = _request(check_name="strict-health")
        plan = build_product_health_monitoring_plan(profile=profile, request=request)

        self.assertEqual(plan.operation, "create")
        updated = updated_product_health_monitoring_profile(
            profile=profile,
            request=request,
            updated_at="2026-07-22T01:00:00Z",
        )
        created = updated.lanes[0].health_monitoring.checks[-1]
        self.assertEqual(created.name, "strict-health")
        self.assertEqual(created.kind, "public_http")
        self.assertEqual(created.url, "")
        self.assertTrue(created.enabled)
        self.assertTrue(created.require_runtime_identity)

    def test_private_intent_adds_registered_private_check_without_exposing_url(self) -> None:
        profile = _profile()
        request = _request(
            check_name="private-health",
            check_kind="private_http",
            monitoring_intent="private",
            require_runtime_identity=True,
            private_endpoint_key="private-health-prod",
        )

        plan = build_product_health_monitoring_plan(profile=profile, request=request)
        updated = updated_product_health_monitoring_profile(
            profile=profile,
            request=request,
            updated_at="2026-07-22T01:00:00Z",
        )

        self.assertEqual(plan.requested_check_kind, "private_http")
        self.assertEqual(plan.requested_monitoring_intent, "private")
        self.assertEqual(plan.resolved_url, "")
        self.assertEqual(plan.private_endpoint_key, "private-health-prod")
        self.assertEqual(updated.lanes[0].health_monitoring.monitoring_intent, "private")
        private_check = updated.lanes[0].health_monitoring.checks[-1]
        self.assertEqual(private_check.kind, "private_http")
        self.assertEqual(private_check.private_endpoint_key, "private-health-prod")

    def test_plan_rejects_matching_non_public_check(self) -> None:
        with self.assertRaises(ProductHealthMonitoringCheckKindError):
            build_product_health_monitoring_plan(
                profile=_profile(),
                request=_request(check_name="provider-health"),
            )

    def test_plan_rejects_missing_or_mismatched_lane(self) -> None:
        request = _request().model_copy(update={"instance": "prod"})
        with self.assertRaises(ProductHealthMonitoringTargetError):
            build_product_health_monitoring_plan(profile=_profile(), request=request)

        request = _request().model_copy(update={"product": "different-product"})
        with self.assertRaises(ProductHealthMonitoringTargetError):
            build_product_health_monitoring_plan(profile=_profile(), request=request)

    def test_strict_check_rejects_unowned_or_non_https_url(self) -> None:
        profile_payload = _profile().model_dump(mode="json")
        profile_payload["lanes"][0]["health_monitoring"]["checks"][0]["url"] = (
            "https://unowned.example.com/health"
        )
        with self.assertRaisesRegex(ProductHealthMonitoringTargetError, "lane-owned host"):
            build_product_health_monitoring_plan(
                profile=LaunchplaneProductProfileRecord.model_validate(profile_payload),
                request=_request(),
            )

        profile_payload["lanes"][0]["health_monitoring"]["checks"][0]["url"] = (
            "http://cm-testing.example.com/health"
        )
        with self.assertRaisesRegex(ProductHealthMonitoringTargetError, "requires HTTPS"):
            build_product_health_monitoring_plan(
                profile=LaunchplaneProductProfileRecord.model_validate(profile_payload),
                request=_request(),
            )

    def test_request_guards_review_and_disabled_strict_state(self) -> None:
        with self.assertRaises(ValidationError):
            _request(mode="apply")
        with self.assertRaises(ValidationError):
            _request(reviewed_plan_sha256="a" * 64)
        with self.assertRaises(ValidationError):
            _request(enabled=False, require_runtime_identity=True)
        with self.assertRaises(ValidationError):
            _request(private_endpoint_key="private-health-prod")

    def test_private_intent_requires_enabled_private_check(self) -> None:
        with self.assertRaisesRegex(ValueError, "requires an enabled private HTTP"):
            build_product_health_monitoring_plan(
                profile=_profile(),
                request=_request(monitoring_intent="private"),
            )

    def test_request_rejects_caller_authority_fields(self) -> None:
        payload = _request().model_dump(mode="json")
        payload["url"] = "https://caller.example.com/health"
        with self.assertRaises(ValidationError):
            ProductHealthMonitoringApplyRequest.model_validate(payload)


if __name__ == "__main__":
    unittest.main()
