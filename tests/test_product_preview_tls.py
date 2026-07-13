import unittest
from typing import cast

from pydantic import ValidationError

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.product_preview_tls import (
    ProductPreviewTlsApplyRequest,
    build_product_preview_tls_plan,
    updated_product_preview_tls_profile,
)
from tests.support.profiles import _product_profile_payload


def _profile() -> LaunchplaneProductProfileRecord:
    payload = _product_profile_payload()
    payload["product"] = "odoo-product"
    payload["driver_id"] = "odoo"
    preview = cast(dict[str, object], payload["preview"])
    preview["domain_certificate_type"] = "none"
    payload["updated_at"] = "2026-07-12T00:00:00Z"
    payload["source"] = "existing-source"
    return LaunchplaneProductProfileRecord.model_validate(payload)


def _request(
    *,
    mode: str = "dry-run",
    reviewed_plan_sha256: str = "",
) -> ProductPreviewTlsApplyRequest:
    return ProductPreviewTlsApplyRequest.model_validate(
        {
            "product": "odoo-product",
            "mode": mode,
            "domain_certificate_type": "letsencrypt",
            "reason": "Enable trusted preview TLS.",
            "reviewed_plan_sha256": reviewed_plan_sha256,
        }
    )


class ProductPreviewTlsTests(unittest.TestCase):
    def test_dry_run_builds_deterministic_redacted_plan(self) -> None:
        profile = _profile()
        plan = build_product_preview_tls_plan(profile=profile, request=_request())

        self.assertEqual(plan.current_value, "none")
        self.assertEqual(plan.requested_value, "letsencrypt")
        self.assertTrue(plan.changed)
        self.assertEqual(plan.profile_updated_at_before, "2026-07-12T00:00:00Z")
        self.assertEqual(plan.source_label, "service:product-preview-tls")
        self.assertRegex(plan.plan_sha256, r"^[0-9a-f]{64}$")

        apply_request = _request(mode="apply", reviewed_plan_sha256=plan.plan_sha256)
        apply_plan = build_product_preview_tls_plan(profile=profile, request=apply_request)
        self.assertEqual(apply_plan.plan_sha256, plan.plan_sha256)

    def test_apply_requires_reviewed_plan_sha256(self) -> None:
        with self.assertRaises(ValidationError):
            _request(mode="apply")

    def test_dry_run_rejects_reviewed_plan_sha256(self) -> None:
        with self.assertRaises(ValidationError):
            _request(reviewed_plan_sha256="a" * 64)

    def test_request_rejects_unknown_schema_version(self) -> None:
        with self.assertRaises(ValidationError):
            ProductPreviewTlsApplyRequest.model_validate(
                {
                    "schema_version": 2,
                    "product": "odoo-product",
                    "domain_certificate_type": "letsencrypt",
                    "reason": "Enable trusted preview TLS.",
                }
            )

    def test_request_rejects_caller_owned_source_label(self) -> None:
        with self.assertRaises(ValidationError):
            ProductPreviewTlsApplyRequest.model_validate(
                {
                    "product": "odoo-product",
                    "domain_certificate_type": "letsencrypt",
                    "reason": "Enable trusted preview TLS.",
                    "source_label": "caller-controlled",
                }
            )

    def test_update_preserves_unrelated_profile_fields(self) -> None:
        profile = _profile()
        plan = build_product_preview_tls_plan(profile=profile, request=_request())
        request = _request(mode="apply", reviewed_plan_sha256=plan.plan_sha256)

        updated = updated_product_preview_tls_profile(
            profile=profile,
            request=request,
            updated_at="2026-07-12T01:00:00Z",
        )

        expected = profile.model_dump(mode="json")
        expected["preview"]["domain_certificate_type"] = "letsencrypt"
        expected["updated_at"] = "2026-07-12T01:00:00Z"
        expected["source"] = "service:product-preview-tls"
        self.assertEqual(updated.model_dump(mode="json"), expected)

    def test_plan_rejects_mismatched_loaded_profile(self) -> None:
        request = ProductPreviewTlsApplyRequest.model_validate(
            {
                "product": "different-product",
                "domain_certificate_type": "letsencrypt",
                "reason": "Enable trusted preview TLS.",
            }
        )

        with self.assertRaisesRegex(ValueError, "does not match"):
            build_product_preview_tls_plan(profile=_profile(), request=request)

    def test_plan_rejects_non_odoo_profile(self) -> None:
        profile = _profile().model_copy(update={"driver_id": "generic-web"})

        with self.assertRaisesRegex(ValueError, "requires the Odoo driver"):
            build_product_preview_tls_plan(profile=profile, request=_request())


if __name__ == "__main__":
    unittest.main()
