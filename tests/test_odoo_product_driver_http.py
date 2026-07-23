import unittest

from pydantic import ValidationError

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.odoo_product_driver_http import (
    OdooProductMismatchError,
    OdooRouteDependencyError,
    resolve_odoo_product_route,
)


def _profile(
    *,
    product: str,
    driver_id: str = "odoo",
    lanes: tuple[ProductLaneProfile, ...] | None = None,
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product=product,
        display_name="Test Odoo Product",
        repository=f"example/{product}",
        driver_id=driver_id,
        image=ProductImageProfile(repository=f"registry.example/{product}"),
        runtime_port=8069,
        health_path="/web/health",
        lanes=lanes
        or (
            ProductLaneProfile(
                instance="testing",
                context="test_primary",
                base_url="https://test-primary.example.test",
                health_url="https://test-primary.example.test/web/health",
            ),
        ),
        preview=ProductPreviewProfile(enabled=False),
        updated_at="2026-07-23T00:00:00Z",
        source="test",
    )


class _ProfileStore:
    def __init__(self, *profiles: LaunchplaneProductProfileRecord) -> None:
        self.profiles = {profile.product: profile for profile in profiles}

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        try:
            return self.profiles[product]
        except KeyError as error:
            raise FileNotFoundError(product) from error

    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        return tuple(
            profile
            for profile in self.profiles.values()
            if not driver_id or profile.driver_id == driver_id
        )


class OdooProductDriverRouteTests(unittest.TestCase):
    def test_base_odoo_descriptor_cannot_authorize_product_route(self) -> None:
        with self.assertRaisesRegex(
            OdooProductMismatchError,
            "DB-backed product profile",
        ):
            resolve_odoo_product_route(
                record_store=_ProfileStore(),
                product="odoo",
                context="test_primary",
                instance="testing",
            )

    def test_owned_odoo_profile_lane_is_returned(self) -> None:
        profile = _profile(product="test-odoo-primary")

        resolved = resolve_odoo_product_route(
            record_store=_ProfileStore(profile),
            product=profile.product,
            context="test_primary",
            instance="testing",
        )

        self.assertEqual(resolved, profile)

    def test_unowned_odoo_profile_lane_is_rejected(self) -> None:
        profile = _profile(product="test-odoo-primary")

        with self.assertRaisesRegex(OdooProductMismatchError, "does not own"):
            resolve_odoo_product_route(
                record_store=_ProfileStore(profile),
                product=profile.product,
                context="test_primary",
                instance="prod",
            )

    def test_duplicate_profile_lane_ownership_is_rejected(self) -> None:
        primary_profile = _profile(product="test-odoo-primary")
        conflicting_profile = _profile(
            product="test-generic-conflict",
            driver_id="generic-web",
        )

        with self.assertRaisesRegex(OdooProductMismatchError, "ambiguous"):
            resolve_odoo_product_route(
                record_store=_ProfileStore(primary_profile, conflicting_profile),
                product=primary_profile.product,
                context="test_primary",
                instance="testing",
            )

    def test_product_profile_rejects_duplicate_lane_instances(self) -> None:
        with self.assertRaisesRegex(ValidationError, "lane instances must be unique"):
            _profile(
                product="test-odoo-ambiguous",
                lanes=(
                    ProductLaneProfile(instance="prod", context="test_primary"),
                    ProductLaneProfile(instance="prod", context="test_secondary"),
                ),
            )

    def test_non_odoo_profile_is_rejected(self) -> None:
        profile = _profile(product="test-generic-product", driver_id="generic-web")

        with self.assertRaisesRegex(OdooProductMismatchError, "not configured"):
            resolve_odoo_product_route(
                record_store=_ProfileStore(profile),
                product=profile.product,
                context="test_primary",
                instance="testing",
            )

    def test_missing_product_profile_is_dependency_error(self) -> None:
        with self.assertRaises(OdooRouteDependencyError):
            resolve_odoo_product_route(
                record_store=_ProfileStore(),
                product="test-missing-product",
                context="test_primary",
                instance="testing",
            )


if __name__ == "__main__":
    unittest.main()
