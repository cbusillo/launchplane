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
    discover_generic_web_preview_desired_state,
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


if __name__ == "__main__":
    unittest.main()
