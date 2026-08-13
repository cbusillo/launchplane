from __future__ import annotations

import unittest

from pydantic import ValidationError

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.product_stable_lane_repair import (
    ProductStableLaneRepairBoundaryError,
    ProductStableLaneRepairRequest,
    build_product_stable_lane_repair_plan,
    updated_product_stable_lane_repair_profile,
)
from tests.support.profiles import _product_profile_payload


def _profile() -> LaunchplaneProductProfileRecord:
    payload = _product_profile_payload()
    payload["product"] = "example-product"
    payload["driver_id"] = "generic-web"
    payload["health_path"] = "/api/health"
    payload["lanes"] = [
        {
            "instance": "testing",
            "context": "example-product",
            "base_url": "https://testing.example.com",
            "health_url": "https://testing.example.com/api/health",
        }
    ]
    payload["updated_at"] = "2026-08-12T00:00:00Z"
    payload["source"] = "test:existing-profile"
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _dokploy_target(*, domains: tuple[str, ...] = ("www.example.com",)) -> DokployTargetRecord:
    return DokployTargetRecord(
        context="example-product",
        instance="prod",
        target_type="application",
        target_name="example-prod",
        source_type="docker",
        healthcheck_path="/api/health",
        domains=domains,
        updated_at="2026-08-12T00:00:00Z",
        source_label="test:target",
    )


def _dokploy_target_id() -> DokployTargetIdRecord:
    return DokployTargetIdRecord(
        context="example-product",
        instance="prod",
        target_id="prod-target-123",
        updated_at="2026-08-12T00:00:00Z",
        source_label="test:target",
    )


class _Store:
    def __init__(
        self,
        *,
        profile: LaunchplaneProductProfileRecord | None = None,
        dokploy_target: DokployTargetRecord | None = None,
        provider_target: ProviderTargetRecord | None = None,
    ) -> None:
        self.profile = profile or _profile()
        self.dokploy_target = dokploy_target or _dokploy_target()
        self.dokploy_target_id = _dokploy_target_id()
        self.provider_target = provider_target or ProviderTargetRecord.from_dokploy_records(
            target_record=self.dokploy_target,
            target_id_record=self.dokploy_target_id,
        )

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord:
        return self.provider_target

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord:
        return self.dokploy_target

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord:
        return self.dokploy_target_id


def _request(
    *,
    mode: str = "dry-run",
    base_url: str = "https://www.example.com",
    reviewed_plan_sha256: str = "",
) -> ProductStableLaneRepairRequest:
    return ProductStableLaneRepairRequest.model_validate(
        {
            "product": "example-product",
            "context": "example-product",
            "instance": "prod",
            "base_url": base_url,
            "mode": mode,
            "reason": "Restore the missing production lane.",
            "reviewed_plan_sha256": reviewed_plan_sha256,
        }
    )


class ProductStableLaneRepairTests(unittest.TestCase):
    def test_dry_run_builds_target_bound_plan(self) -> None:
        plan, profile, replacement, provider_target, dokploy_target, dokploy_target_id = (
            build_product_stable_lane_repair_plan(record_store=_Store(), request=_request())
        )

        self.assertEqual(plan.base_url, "https://www.example.com")
        self.assertEqual(plan.health_url, "https://www.example.com/api/health")
        self.assertEqual(plan.preserved_lane_instances, ("testing",))
        self.assertEqual(plan.target.target_id, "prod-target-123")
        self.assertRegex(plan.plan_sha256, r"^[0-9a-f]{64}$")
        self.assertEqual(len(replacement.lanes), 2)
        self.assertEqual(replacement.lanes[-1].instance, "prod")
        self.assertEqual(replacement.lanes[-1].context, "example-product")
        self.assertEqual(profile.source, "test:existing-profile")
        self.assertEqual(provider_target.target_id, dokploy_target_id.target_id)
        self.assertEqual(dokploy_target.target_name, provider_target.display_name)

    def test_apply_requires_reviewed_plan(self) -> None:
        with self.assertRaises(ValidationError):
            _request(mode="apply")

    def test_plan_is_identical_for_apply_request(self) -> None:
        store = _Store()
        dry_run = build_product_stable_lane_repair_plan(record_store=store, request=_request())[0]
        apply_plan = build_product_stable_lane_repair_plan(
            record_store=store,
            request=_request(mode="apply", reviewed_plan_sha256=dry_run.plan_sha256),
        )[0]

        self.assertEqual(apply_plan.plan_sha256, dry_run.plan_sha256)

    def test_update_preserves_all_unrelated_profile_fields(self) -> None:
        _, profile, replacement, *_ = build_product_stable_lane_repair_plan(
            record_store=_Store(), request=_request()
        )
        updated = updated_product_stable_lane_repair_profile(
            replacement_profile=replacement,
            updated_at="2026-08-12T01:00:00Z",
        )

        original = profile.model_dump(mode="json")
        result = updated.model_dump(mode="json")
        original_lanes = original.pop("lanes")
        result_lanes = result.pop("lanes")
        original["updated_at"] = "2026-08-12T01:00:00Z"
        original["source"] = "service:product-stable-lane-repair"
        self.assertEqual(result, original)
        self.assertEqual(result_lanes[:-1], original_lanes)
        self.assertEqual(
            {
                "instance": result_lanes[-1]["instance"],
                "context": result_lanes[-1]["context"],
                "base_url": result_lanes[-1]["base_url"],
                "health_url": result_lanes[-1]["health_url"],
            },
            {
                "instance": "prod",
                "context": "example-product",
                "base_url": "https://www.example.com",
                "health_url": "https://www.example.com/api/health",
            },
        )

    def test_rejects_untracked_base_url(self) -> None:
        with self.assertRaisesRegex(ProductStableLaneRepairBoundaryError, "not owned"):
            build_product_stable_lane_repair_plan(
                record_store=_Store(),
                request=_request(base_url="https://other.example.com"),
            )

    def test_rejects_existing_lane_instance(self) -> None:
        profile = _profile().model_copy(
            update={
                "lanes": (
                    *_profile().lanes,
                    _profile().lanes[0].model_copy(update={"instance": "prod"}),
                )
            }
        )
        with self.assertRaisesRegex(ProductStableLaneRepairBoundaryError, "absent"):
            build_product_stable_lane_repair_plan(
                record_store=_Store(profile=profile), request=_request()
            )

    def test_rejects_mismatched_provider_projection(self) -> None:
        provider_target = ProviderTargetRecord.from_dokploy_records(
            target_record=_dokploy_target(),
            target_id_record=_dokploy_target_id(),
        ).model_copy(update={"display_name": "different-target"})
        with self.assertRaisesRegex(ProductStableLaneRepairBoundaryError, "does not match"):
            build_product_stable_lane_repair_plan(
                record_store=_Store(provider_target=provider_target), request=_request()
            )


if __name__ == "__main__":
    unittest.main()
