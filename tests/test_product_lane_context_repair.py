import unittest

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
    product_profile_record_sha256,
)
from control_plane.product_lane_context_repair import (
    ProductLaneContextRepairBoundaryError,
    ProductLaneContextRepairRequest,
    build_product_lane_context_repair_plan,
    updated_product_lane_context_repair_profile,
)


def _profile() -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="sellyouroutboard",
        display_name="SellYourOutboard",
        repository="cbusillo/sellyouroutboard",
        driver_id="generic-web",
        image=ProductImageProfile(repository="ghcr.io/cbusillo/sellyouroutboard"),
        runtime_port=3000,
        health_path="/api/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="sellyouroutboard-testing",
            ),
        ),
        historical_contexts=("sellyouroutboard-testing",),
        preview=ProductPreviewProfile(
            enabled=True,
            context="sellyouroutboard-preview",
        ),
        updated_at="2026-08-05T17:01:06Z",
        source="service:generic-web-onboarding",
    )


def _provider_target(*, context: str, target_id: str = "target-testing") -> ProviderTargetRecord:
    return ProviderTargetRecord(
        context=context,
        instance="testing",
        provider_id="dokploy",
        target_category="application",
        target_id=target_id,
        display_name=f"{context}-testing",
        provider_target_type="application",
        updated_at="2026-08-05T17:47:15Z",
        source_label="test",
    )


class _Store:
    def __init__(
        self,
        *,
        profile: LaunchplaneProductProfileRecord | None = None,
        provider_targets: tuple[ProviderTargetRecord, ...] | None = None,
    ) -> None:
        self.profile = profile or _profile()
        self.provider_targets = provider_targets or (
            _provider_target(context="sellyouroutboard-testing"),
            _provider_target(context="sellyouroutboard"),
        )

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def list_physical_provider_target_records(self) -> tuple[ProviderTargetRecord, ...]:
        return self.provider_targets


def _request(**updates: object) -> ProductLaneContextRepairRequest:
    payload: dict[str, object] = {
        "product": "sellyouroutboard",
        "instance": "testing",
        "expected_current_context": "sellyouroutboard-testing",
        "requested_context": "sellyouroutboard",
        "reason": "Repair the legacy testing lane pointer.",
    }
    payload.update(updates)
    return ProductLaneContextRepairRequest.model_validate(payload)


class ProductLaneContextRepairTests(unittest.TestCase):
    def test_plan_changes_only_matching_lane_and_source(self) -> None:
        store = _Store()

        plan, original, replacement, _, _ = build_product_lane_context_repair_plan(
            record_store=store,
            request=_request(),
        )

        self.assertEqual(plan.current_context, "sellyouroutboard-testing")
        self.assertEqual(plan.requested_context, "sellyouroutboard")
        self.assertEqual(plan.source_target.target_id, plan.target_target.target_id)
        self.assertEqual(plan.preserved_preview_context, "sellyouroutboard-preview")
        self.assertEqual(replacement.lanes[0].context, "sellyouroutboard")
        self.assertEqual(replacement.preview, original.preview)
        self.assertEqual(replacement.historical_contexts, original.historical_contexts)
        self.assertEqual(replacement.updated_at, original.updated_at)
        self.assertEqual(replacement.source, "service:product-lane-context-repair")
        original_payload = original.model_dump(mode="json")
        replacement_payload = replacement.model_dump(mode="json")
        original_payload["lanes"][0]["context"] = "sellyouroutboard"
        original_payload["source"] = "service:product-lane-context-repair"
        self.assertEqual(replacement_payload, original_payload)

    def test_plan_digest_changes_with_provider_target_evidence(self) -> None:
        first_plan, _, _, _, _ = build_product_lane_context_repair_plan(
            record_store=_Store(),
            request=_request(),
        )
        changed_target = _provider_target(context="sellyouroutboard").model_copy(
            update={"updated_at": "2026-08-05T17:47:16Z"}
        )

        second_plan, _, _, _, _ = build_product_lane_context_repair_plan(
            record_store=_Store(
                provider_targets=(
                    _provider_target(context="sellyouroutboard-testing"),
                    changed_target,
                )
            ),
            request=_request(),
        )

        self.assertNotEqual(first_plan.plan_sha256, second_plan.plan_sha256)

    def test_apply_request_requires_reviewed_plan_and_profile_digests(self) -> None:
        with self.assertRaisesRegex(ValueError, "expected_profile_sha256"):
            _request(mode="apply", reviewed_plan_sha256="a" * 64)
        with self.assertRaisesRegex(ValueError, "reviewed 64-character"):
            _request(
                mode="apply",
                expected_profile_sha256=product_profile_record_sha256(_profile()),
            )

    def test_dry_run_rejects_reviewed_plan_digest(self) -> None:
        with self.assertRaisesRegex(ValueError, "dry-run rejects"):
            _request(reviewed_plan_sha256="a" * 64)

    def test_plan_rejects_profile_digest_drift(self) -> None:
        with self.assertRaisesRegex(
            ProductLaneContextRepairBoundaryError,
            "expected profile digest",
        ):
            build_product_lane_context_repair_plan(
                record_store=_Store(),
                request=_request(expected_profile_sha256="a" * 64),
            )

    def test_plan_rejects_mismatched_physical_targets(self) -> None:
        with self.assertRaisesRegex(
            ProductLaneContextRepairBoundaryError,
            "same physical target",
        ):
            build_product_lane_context_repair_plan(
                record_store=_Store(
                    provider_targets=(
                        _provider_target(context="sellyouroutboard-testing"),
                        _provider_target(
                            context="sellyouroutboard",
                            target_id="different-target",
                        ),
                    )
                ),
                request=_request(),
            )

    def test_plan_requires_canonical_requested_context(self) -> None:
        with self.assertRaisesRegex(
            ProductLaneContextRepairBoundaryError,
            "canonical product context",
        ):
            build_product_lane_context_repair_plan(
                record_store=_Store(),
                request=_request(requested_context="different-context"),
            )

    def test_plan_requires_source_context_in_history(self) -> None:
        profile = _profile().model_copy(update={"historical_contexts": ()})
        with self.assertRaisesRegex(
            ProductLaneContextRepairBoundaryError,
            "preserved as history",
        ):
            build_product_lane_context_repair_plan(
                record_store=_Store(profile=profile),
                request=_request(),
            )

    def test_plan_rejects_historical_canonical_context(self) -> None:
        profile = _profile().model_copy(
            update={
                "historical_contexts": (
                    "sellyouroutboard-testing",
                    "sellyouroutboard",
                )
            }
        )
        with self.assertRaisesRegex(
            ProductLaneContextRepairBoundaryError,
            "restore a historical context",
        ):
            build_product_lane_context_repair_plan(
                record_store=_Store(profile=profile),
                request=_request(),
            )

    def test_plan_requires_exact_matching_lane(self) -> None:
        with self.assertRaisesRegex(
            ProductLaneContextRepairBoundaryError,
            "exactly one matching current lane",
        ):
            build_product_lane_context_repair_plan(
                record_store=_Store(),
                request=_request(instance="prod"),
            )

    def test_plan_requires_both_provider_target_routes(self) -> None:
        with self.assertRaisesRegex(
            ProductLaneContextRepairBoundaryError,
            "exactly one provider target",
        ):
            build_product_lane_context_repair_plan(
                record_store=_Store(
                    provider_targets=(_provider_target(context="sellyouroutboard-testing"),)
                ),
                request=_request(),
            )

    def test_plan_rejects_unexpected_third_route_claim(self) -> None:
        with self.assertRaisesRegex(
            ProductLaneContextRepairBoundaryError,
            "unexpected route",
        ):
            build_product_lane_context_repair_plan(
                record_store=_Store(
                    provider_targets=(
                        _provider_target(context="sellyouroutboard-testing"),
                        _provider_target(context="sellyouroutboard"),
                        _provider_target(context="other-context"),
                    )
                ),
                request=_request(),
            )

    def test_updated_profile_sets_only_timestamp_after_plan(self) -> None:
        _, _, replacement, _, _ = build_product_lane_context_repair_plan(
            record_store=_Store(),
            request=_request(),
        )

        updated = updated_product_lane_context_repair_profile(
            replacement_profile=replacement,
            updated_at="2026-08-12T12:00:00Z",
        )

        self.assertEqual(updated.updated_at, "2026-08-12T12:00:00Z")
        self.assertEqual(updated.lanes[0].context, "sellyouroutboard")


if __name__ == "__main__":
    unittest.main()
