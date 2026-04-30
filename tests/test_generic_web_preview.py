import unittest
from pathlib import Path
from unittest.mock import patch

import click

from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductPreviewProfile,
)
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDesiredStateRequest,
    GenericWebPreviewDestroyRequest,
    GenericWebPreviewInventoryRequest,
    discover_generic_web_preview_desired_state,
    execute_generic_web_preview_destroy,
    execute_generic_web_preview_inventory,
    preview_pr_number_from_slug,
    resolve_generic_web_preview_profile,
)
from control_plane.workflows.preview_desired_state import render_preview_slug


class _GenericWebPreviewStore:
    def __init__(self, profile: LaunchplaneProductProfileRecord) -> None:
        self.profile = profile

    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord:
        if product != self.profile.product:
            raise FileNotFoundError(product)
        return self.profile


def _profile(*, preview_enabled: bool = True) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="sellyouroutboard",
        display_name="SellYourOutboard.com",
        repository="cbusillo/sellyouroutboard",
        driver_id="generic-web",
        image=ProductImageProfile(repository="ghcr.io/cbusillo/sellyouroutboard"),
        runtime_port=3000,
        health_path="/api/health",
        preview=ProductPreviewProfile(
            enabled=preview_enabled,
            context="sellyouroutboard-testing" if preview_enabled else "",
            slug_template="preview-{number}-site",
            app_name_prefix="syo-preview",
        ),
        updated_at="2026-04-30T21:00:00Z",
        source="test",
    )


class GenericWebPreviewTests(unittest.TestCase):
    def test_render_preview_slug_uses_template_when_present(self) -> None:
        self.assertEqual(
            render_preview_slug(
                anchor_pr_number=123,
                preview_slug_prefix="pr-",
                preview_slug_template="preview-{number}-site",
            ),
            "preview-123-site",
        )

    def test_discover_generic_web_preview_desired_state_uses_profile_contract(self) -> None:
        store = _GenericWebPreviewStore(_profile())
        record = PreviewDesiredStateRecord(
            desired_state_id="preview-desired-state-syo-testing-1",
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            source="generic-web-preview",
            discovered_at="2026-04-30T21:00:00Z",
            repository="cbusillo/sellyouroutboard",
            label="preview",
            anchor_repo="sellyouroutboard",
            preview_slug_prefix="preview-",
            status="pass",
            desired_count=0,
        )

        with patch(
            "control_plane.workflows.generic_web_preview.discover_github_preview_desired_state",
            return_value=record,
        ) as discover:
            result = discover_generic_web_preview_desired_state(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewDesiredStateRequest(product="sellyouroutboard"),
                discovered_at="2026-04-30T21:00:00Z",
            )

        self.assertEqual(result, record)
        discover.assert_called_once()
        _, kwargs = discover.call_args
        self.assertEqual(kwargs["product"], "sellyouroutboard")
        self.assertEqual(kwargs["context"], "sellyouroutboard-testing")
        self.assertEqual(kwargs["repository"], "cbusillo/sellyouroutboard")
        self.assertEqual(kwargs["anchor_repo"], "sellyouroutboard")
        self.assertEqual(kwargs["preview_slug_prefix"], "preview-")
        self.assertEqual(kwargs["preview_slug_template"], "preview-{number}-site")

    def test_resolve_generic_web_preview_profile_rejects_disabled_preview(self) -> None:
        store = _GenericWebPreviewStore(_profile(preview_enabled=False))

        with self.assertRaises(click.ClickException):
            resolve_generic_web_preview_profile(
                record_store=store,
                product="sellyouroutboard",
            )

    def test_preview_pr_number_from_slug_uses_template(self) -> None:
        self.assertEqual(
            preview_pr_number_from_slug(
                preview_slug="preview-42-site",
                slug_template="preview-{number}-site",
            ),
            42,
        )
        self.assertIsNone(
            preview_pr_number_from_slug(
                preview_slug="not-preview-42-site",
                slug_template="preview-{number}-site",
            )
        )

    def test_execute_generic_web_preview_inventory_filters_by_app_prefix(self) -> None:
        store = _GenericWebPreviewStore(_profile())
        raw_projects = [
            {
                "environments": [
                    {
                        "applications": [
                            {"applicationId": "app-1", "name": "syo-preview-preview-42-site"},
                            {"applicationId": "app-2", "name": "other-preview-pr-1"},
                        ]
                    }
                ]
            }
        ]

        with (
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.dokploy_request",
                return_value=raw_projects,
            ),
        ):
            result = execute_generic_web_preview_inventory(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewInventoryRequest(product="sellyouroutboard"),
            )

        self.assertEqual(result.context, "sellyouroutboard-testing")
        self.assertEqual(result.app_name_prefix, "syo-preview")
        self.assertEqual([item.previewSlug for item in result.previews], ["preview-42-site"])

    def test_execute_generic_web_preview_destroy_deletes_domains_and_application(self) -> None:
        store = _GenericWebPreviewStore(_profile())
        requests: list[dict[str, object]] = []

        def _fake_dokploy_request(**kwargs):
            requests.append(kwargs)
            path = kwargs["path"]
            if path == "/api/project.all":
                return [
                    {
                        "environments": [
                            {
                                "applications": [
                                    {
                                        "applicationId": "app-1",
                                        "name": "syo-preview-preview-42-site",
                                    }
                                ]
                            }
                        ]
                    }
                ]
            if path == "/api/domain.byApplicationId":
                return [{"domainId": "domain-1"}]
            return {}

        with (
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.read_dokploy_config",
                return_value=("https://dokploy.example", "token"),
            ),
            patch(
                "control_plane.workflows.generic_web_preview.control_plane_dokploy.dokploy_request",
                side_effect=_fake_dokploy_request,
            ),
            patch(
                "control_plane.workflows.generic_web_preview.utc_now_timestamp",
                side_effect=["2026-04-30T21:00:00Z", "2026-04-30T21:00:02Z"],
            ),
        ):
            result = execute_generic_web_preview_destroy(
                control_plane_root=Path("."),
                record_store=store,
                request=GenericWebPreviewDestroyRequest(
                    product="sellyouroutboard",
                    preview_slug="preview-42-site",
                    destroy_reason="test",
                ),
            )

        self.assertEqual(result.destroy_status, "pass")
        self.assertEqual(result.application_id, "app-1")
        self.assertEqual(
            [request["path"] for request in requests],
            [
                "/api/project.all",
                "/api/domain.byApplicationId",
                "/api/domain.delete",
                "/api/application.delete",
            ],
        )


if __name__ == "__main__":
    unittest.main()
