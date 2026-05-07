from __future__ import annotations

import unittest
from typing import cast

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.product_read_service import (
    build_product_environment_list_service_payload,
    build_product_environment_read_service_result,
    build_product_profile_list_service_payload,
)


def _profile_payload(
    *, product: str = "example-site", driver_id: str = "generic-web"
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": product,
        "display_name": "Example Site",
        "repository": f"every/{product}",
        "driver_id": driver_id,
        "image": {"repository": f"ghcr.io/every/{product}"},
        "runtime_port": 3000,
        "health_path": "/healthz",
        "lanes": (
            {
                "instance": "testing",
                "context": "example-site-testing",
                "base_url": "https://testing.example.invalid",
                "health_url": "https://testing.example.invalid/healthz",
            },
            {
                "instance": "prod",
                "context": "example-site-prod",
                "base_url": "https://example.invalid",
                "health_url": "https://example.invalid/healthz",
            },
        ),
        "preview": {
            "enabled": True,
            "context": "shared-preview",
            "slug_template": "pr-{number}",
        },
        "updated_at": "2026-05-02T22:30:00Z",
        "source": "test",
    }


class _ProductReadStore:
    def __init__(self, profile: LaunchplaneProductProfileRecord) -> None:
        self.profile = profile

    def list_product_profile_records(
        self,
        *,
        driver_id: str = "",
    ) -> tuple[LaunchplaneProductProfileRecord, ...]:
        if driver_id and driver_id != self.profile.driver_id:
            return ()
        return (self.profile,)

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[object, ...]:
        return ()

    def list_deployment_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        return ()

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[object, ...]:
        return ()

    def list_backup_gate_records(
        self, *, context_name: str = "", instance_name: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        return ()

    def list_preview_desired_state_records(
        self, *, context_name: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        return ()

    def list_preview_lifecycle_cleanup_records(
        self, *, context_name: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        return ()

    def list_preview_pr_feedback_records(
        self, *, context_name: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        return ()

    def list_authz_policy_records(
        self, *, status: str = "", limit: int | None = None
    ) -> tuple[object, ...]:
        return ()

    def list_runtime_environment_records(
        self, *, context_name: str = "", scope: str = ""
    ) -> tuple[object, ...]:
        return ()

    def list_secret_binding_records(
        self, *, context_name: str = "", scope: str = ""
    ) -> tuple[object, ...]:
        return ()


class ProductReadServiceTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = LaunchplaneProductProfileRecord.model_validate(_profile_payload())
        self.store = _ProductReadStore(self.profile)

    def test_product_profile_list_payload_preserves_driver_filter(self) -> None:
        payload = build_product_profile_list_service_payload(
            record_store=self.store,
            driver_id="generic-web",
        )

        self.assertEqual(payload["driver_id"], "generic-web")
        profiles = cast(list[dict[str, object]], payload["profiles"])
        self.assertEqual(len(profiles), 1)

    def test_product_environment_result_identifies_authorization_target(self) -> None:
        result = build_product_environment_read_service_result(
            record_store=self.store,
            params={"product": "example-site", "environment": "prod"},
            action_allowed=lambda _action, _product, _context: True,
        )

        self.assertEqual(result.authorization_product, "example-site")
        self.assertEqual(result.authorization_context, "example-site-prod")
        self.assertEqual(
            result.denial_message,
            "Workflow cannot read the requested product environment.",
        )
        self.assertIn("environment", result.payload)

    def test_product_environment_list_payload_serializes_overviews(self) -> None:
        payload = build_product_environment_list_service_payload(
            record_store=self.store,
            action_allowed=lambda _action, _product, _context: True,
        )

        products = cast(list[dict[str, object]], payload["products"])
        self.assertEqual(len(products), 1)
        self.assertEqual(products[0]["product"], "example-site")


if __name__ == "__main__":
    unittest.main()
