import unittest
from typing import cast

from pydantic import ValidationError

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.product_prelaunch_rebuild_policy import (
    ProductPrelaunchRebuildPolicyApplyRequest,
    ProductPrelaunchRebuildPolicyDriverError,
    ProductPrelaunchRebuildPolicyStateError,
    ProductPrelaunchRebuildPolicyTargetError,
    build_product_prelaunch_rebuild_policy_plan,
    updated_product_prelaunch_rebuild_policy_profile,
)
from tests.support.profiles import _odoo_profile_payload_with_prod_lane


def _profile() -> LaunchplaneProductProfileRecord:
    payload = _odoo_profile_payload_with_prod_lane("odoo-product")
    payload["product"] = "odoo-product"
    payload["driver_id"] = "odoo"
    lanes = list(cast(tuple[dict[str, object], ...], payload["lanes"]))
    prod_lane = lanes[1]
    prod_lane["odoo_data_policy"] = {
        "data_authority": "resettable",
        "allowed_rebuild_sources": ["empty"],
        "requires_backup_before_destroy": True,
        "requires_restore_proof": True,
        "requires_runtime_identity": True,
    }
    prod_lane["odoo_prelaunch_rebuild"] = {"enabled": False}
    prod_lane["health_monitoring"] = {
        "monitoring_intent": "prelaunch",
        "checks": [],
    }
    payload["updated_at"] = "2026-07-28T00:00:00Z"
    payload["source"] = "existing-source"
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _request(
    *,
    mode: str = "dry-run",
    reviewed_plan_sha256: str = "",
    enabled: bool = True,
) -> ProductPrelaunchRebuildPolicyApplyRequest:
    return ProductPrelaunchRebuildPolicyApplyRequest.model_validate(
        {
            "product": "odoo-product",
            "context": "cm",
            "instance": "prod",
            "enabled": enabled,
            "approval_issue_url": (
                "https://github.com/example/launchplane/issues/123" if enabled else ""
            ),
            "data_source_mode": "empty",
            "confirmation": "rebuild wip production",
            "expected_target_name": "expected-target" if enabled else "",
            "expected_domains": ["example.invalid"] if enabled else [],
            "mode": mode,
            "reason": "Authorize an issue-backed WIP rebuild.",
            "reviewed_plan_sha256": reviewed_plan_sha256,
        }
    )


class ProductPrelaunchRebuildPolicyTests(unittest.TestCase):
    def test_dry_run_builds_deterministic_typed_plan(self) -> None:
        profile = _profile()
        plan = build_product_prelaunch_rebuild_policy_plan(
            profile=profile,
            request=_request(),
        )

        self.assertFalse(plan.current_policy.enabled)
        self.assertTrue(plan.requested_policy.enabled)
        self.assertEqual(plan.operation, "update")
        self.assertTrue(plan.changed)
        self.assertEqual(plan.data_authority, "resettable")
        self.assertEqual(plan.allowed_rebuild_sources, ("empty",))
        self.assertEqual(plan.monitoring_intent, "prelaunch")
        self.assertRegex(plan.plan_sha256, r"^[0-9a-f]{64}$")

        apply_request = _request(
            mode="apply",
            reviewed_plan_sha256=plan.plan_sha256,
        )
        apply_plan = build_product_prelaunch_rebuild_policy_plan(
            profile=profile,
            request=apply_request,
        )
        self.assertEqual(apply_plan.plan_sha256, plan.plan_sha256)

    def test_unchanged_disabled_policy_is_a_no_op(self) -> None:
        plan = build_product_prelaunch_rebuild_policy_plan(
            profile=_profile(),
            request=_request(enabled=False),
        )

        self.assertEqual(plan.operation, "unchanged")
        self.assertFalse(plan.changed)

    def test_apply_requires_reviewed_plan_sha256(self) -> None:
        with self.assertRaises(ValidationError):
            _request(mode="apply")

    def test_dry_run_rejects_reviewed_plan_sha256(self) -> None:
        with self.assertRaises(ValidationError):
            _request(reviewed_plan_sha256="a" * 64)

    def test_enabled_policy_requires_proof_fields(self) -> None:
        payload = _request().model_dump(mode="json")
        payload["approval_issue_url"] = ""

        with self.assertRaises(ValidationError):
            ProductPrelaunchRebuildPolicyApplyRequest.model_validate(payload)

    def test_disabled_policy_clears_proof_fields(self) -> None:
        payload = _request().model_dump(mode="json")
        payload["enabled"] = False
        request = ProductPrelaunchRebuildPolicyApplyRequest.model_validate(payload)

        self.assertEqual(
            request.requested_policy().model_dump(mode="json"),
            {
                "enabled": False,
                "approval_issue_url": "",
                "data_source_mode": "empty",
                "confirmation": "",
                "expected_target_name": "",
                "expected_domains": [],
            },
        )

    def test_update_preserves_unrelated_profile_fields(self) -> None:
        profile = _profile()
        plan = build_product_prelaunch_rebuild_policy_plan(
            profile=profile,
            request=_request(),
        )
        request = _request(mode="apply", reviewed_plan_sha256=plan.plan_sha256)

        updated = updated_product_prelaunch_rebuild_policy_profile(
            profile=profile,
            request=request,
            updated_at="2026-07-28T01:00:00Z",
        )

        expected = profile.model_dump(mode="json")
        expected["lanes"][1]["odoo_prelaunch_rebuild"] = request.requested_policy().model_dump(
            mode="json"
        )
        expected["updated_at"] = "2026-07-28T01:00:00Z"
        expected["source"] = "service:product-prelaunch-rebuild-policy"
        self.assertEqual(updated.model_dump(mode="json"), expected)

    def test_plan_rejects_non_odoo_profile(self) -> None:
        profile = _profile().model_copy(update={"driver_id": "generic-web"})

        with self.assertRaises(ProductPrelaunchRebuildPolicyDriverError):
            build_product_prelaunch_rebuild_policy_plan(profile=profile, request=_request())

    def test_plan_rejects_mismatched_product(self) -> None:
        payload = _request().model_dump(mode="json")
        payload["product"] = "different-product"
        request = ProductPrelaunchRebuildPolicyApplyRequest.model_validate(payload)

        with self.assertRaises(ProductPrelaunchRebuildPolicyTargetError):
            build_product_prelaunch_rebuild_policy_plan(profile=_profile(), request=request)

    def test_plan_rejects_missing_lane(self) -> None:
        payload = _request().model_dump(mode="json")
        payload["instance"] = "missing"
        request = ProductPrelaunchRebuildPolicyApplyRequest.model_validate(payload)

        with self.assertRaises(ProductPrelaunchRebuildPolicyTargetError):
            build_product_prelaunch_rebuild_policy_plan(profile=_profile(), request=request)

    def test_plan_rejects_non_prelaunch_lane(self) -> None:
        payload = _profile().model_dump(mode="json")
        payload["lanes"][1]["health_monitoring"] = {
            "monitoring_intent": "public",
            "checks": [
                {
                    "name": "public-ingress",
                    "kind": "public_http",
                    "enabled": True,
                    "require_runtime_identity": True,
                }
            ],
        }
        profile = LaunchplaneProductProfileRecord.model_validate(payload)

        with self.assertRaises(ProductPrelaunchRebuildPolicyStateError):
            build_product_prelaunch_rebuild_policy_plan(profile=profile, request=_request())

    def test_plan_rejects_disallowed_data_source(self) -> None:
        payload = _profile().model_dump(mode="json")
        payload["lanes"][1]["odoo_data_policy"]["allowed_rebuild_sources"] = []
        profile = LaunchplaneProductProfileRecord.model_validate(payload)

        with self.assertRaises(ProductPrelaunchRebuildPolicyStateError):
            build_product_prelaunch_rebuild_policy_plan(profile=profile, request=_request())

    def test_empty_rebuild_requires_resettable_data_authority(self) -> None:
        payload = _profile().model_dump(mode="json")
        payload["lanes"][1]["odoo_data_policy"]["data_authority"] = "restorable"
        profile = LaunchplaneProductProfileRecord.model_validate(payload)

        with self.assertRaises(ProductPrelaunchRebuildPolicyStateError):
            build_product_prelaunch_rebuild_policy_plan(profile=profile, request=_request())

    def test_upstream_restore_accepts_restorable_data_authority(self) -> None:
        profile_payload = _profile().model_dump(mode="json")
        profile_payload["lanes"][1]["odoo_data_policy"] = {
            "data_authority": "restorable",
            "allowed_rebuild_sources": ["upstream_restore"],
            "upstream_source": "operator-supplied-source",
            "requires_backup_before_destroy": True,
            "requires_restore_proof": True,
            "requires_runtime_identity": True,
        }
        profile = LaunchplaneProductProfileRecord.model_validate(profile_payload)
        request_payload = _request().model_dump(mode="json")
        request_payload["data_source_mode"] = "upstream_restore"
        request = ProductPrelaunchRebuildPolicyApplyRequest.model_validate(request_payload)

        plan = build_product_prelaunch_rebuild_policy_plan(
            profile=profile,
            request=request,
        )

        self.assertEqual(plan.data_authority, "restorable")
        self.assertEqual(plan.requested_policy.data_source_mode, "upstream_restore")


if __name__ == "__main__":
    unittest.main()
