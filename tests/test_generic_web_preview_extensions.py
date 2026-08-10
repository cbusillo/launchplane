from pathlib import Path
import unittest
from unittest.mock import MagicMock, patch

import click

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductImageProfile,
    ProductLaneProfile,
    ProductPreviewProfile,
)
from control_plane.drivers.generic_web_preview_extensions import (
    apply_generic_web_preview_driver_refresh,
)
from control_plane.drivers.generic_web_preview_dispatch import (
    GenericWebPreviewDestroyEnvelope,
    GenericWebPreviewRefreshEnvelope,
)
from control_plane.generic_web_preview_http import (
    apply_generic_web_preview_destroy_result,
    apply_generic_web_preview_refresh_result,
)
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDestroyRequest,
    GenericWebPreviewRefreshRequest,
)


def _driver_profile(*, driver_id: str = "verireel") -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord(
        product="verireel",
        display_name="VeriReel",
        repository="cbusillo/verireel",
        driver_id=driver_id,
        image=ProductImageProfile(repository="ghcr.io/cbusillo/verireel-app"),
        runtime_port=3000,
        health_path="/api/health",
        lanes=(
            ProductLaneProfile(
                instance="testing",
                context="verireel",
                base_url="https://testing.example.test",
            ),
        ),
        preview=ProductPreviewProfile(
            enabled=True,
            context="verireel-testing",
            slug_template="pr-{number}",
            app_name_prefix="ver-preview",
            template_instance="testing",
            data_transport_mode="driver",
        ),
        updated_at="2026-07-12T03:00:00Z",
        source="test",
    )


class GenericWebPreviewExtensionTests(unittest.TestCase):
    @patch(
        "control_plane.drivers.generic_web_preview_extensions.apply_verireel_preview_refresh_result"
    )
    def test_verireel_refresh_delegates_driver_owned_transport(
        self, apply_refresh: MagicMock
    ) -> None:
        apply_refresh.return_value = (
            {"preview_id": "preview-verireel-testing-verireel-pr-42"},
            {
                "refresh_status": "pass",
                "refresh_started_at": "2026-07-12T03:00:00Z",
                "refresh_finished_at": "2026-07-12T03:01:00Z",
                "application_name": "ver-preview-pr-42",
                "application_id": "app-pr-42",
                "preview_url": "https://pr-42.preview.example.test",
                "runtime_identity": {
                    "product": "verireel",
                    "context": "verireel-testing",
                    "instance": "pr-42",
                    "environment_kind": "preview",
                    "deployment_record_id": "deployment-verireel-testing-pr-42",
                    "artifact_id": "ghcr.io/cbusillo/verireel-app:pr-42-a1b2c3d4",
                    "source_git_ref": "a1b2c3d4",
                    "image_reference": "ghcr.io/cbusillo/verireel-app:pr-42-a1b2c3d4",
                    "preview_id": "pr-42",
                },
                "error_message": "",
            },
        )
        request = GenericWebPreviewRefreshRequest(
            product="verireel",
            anchor_pr_number=42,
            anchor_pr_url="https://github.com/cbusillo/verireel/pull/42",
            anchor_head_sha="a1b2c3d4",
            image_reference="ghcr.io/cbusillo/verireel-app:pr-42-a1b2c3d4",
            timeout_seconds=240,
        )

        records, result = apply_generic_web_preview_refresh_result(
            control_plane_root=Path("."),
            record_store=object(),
            request=GenericWebPreviewRefreshEnvelope(
                product="verireel",
                refresh=request,
            ),
            profile=_driver_profile(),
        )

        self.assertEqual(records["preview_id"], "preview-verireel-testing-verireel-pr-42")
        self.assertEqual(result["product"], "verireel")
        self.assertEqual(result["context"], "verireel-testing")
        self.assertEqual(result["preview_slug"], "pr-42")
        readiness = result["readiness"]
        smoke = result["smoke"]
        runtime_identity = result["runtime_identity"]
        self.assertIsInstance(readiness, dict)
        self.assertIsInstance(smoke, dict)
        self.assertIsInstance(runtime_identity, dict)
        assert isinstance(readiness, dict)
        assert isinstance(smoke, dict)
        assert isinstance(runtime_identity, dict)
        self.assertEqual(readiness["readiness_status"], "pass")
        self.assertEqual(smoke["smoke_status"], "pass")
        self.assertEqual(runtime_identity["source_git_ref"], "a1b2c3d4")
        delegated = apply_refresh.call_args.kwargs["request"].refresh
        self.assertEqual(delegated.context, "verireel-testing")
        self.assertEqual(delegated.anchor_repo, "verireel")
        self.assertEqual(delegated.preview_slug, "pr-42")
        self.assertEqual(delegated.anchor_head_sha, "a1b2c3d4")
        self.assertEqual(delegated.timeout_seconds, 240)

    @patch(
        "control_plane.drivers.generic_web_preview_extensions.apply_verireel_preview_destroy_result"
    )
    def test_verireel_destroy_delegates_database_cleanup(self, apply_destroy: MagicMock) -> None:
        apply_destroy.return_value = (
            {"transition": "destroyed"},
            {
                "destroy_status": "pass",
                "destroy_started_at": "2026-07-12T03:00:00Z",
                "destroy_finished_at": "2026-07-12T03:01:00Z",
                "application_name": "ver-preview-pr-42",
                "application_id": "app-pr-42",
                "preview_url": "https://pr-42.preview.example.test",
                "error_message": "",
            },
        )
        request = GenericWebPreviewDestroyRequest(
            product="verireel",
            anchor_pr_number=42,
            destroy_reason="pull_request_closed",
            timeout_seconds=240,
        )

        records, result = apply_generic_web_preview_destroy_result(
            control_plane_root=Path("."),
            record_store=object(),
            request=GenericWebPreviewDestroyEnvelope(
                product="verireel",
                destroy=request,
            ),
            profile=_driver_profile(),
        )

        self.assertEqual(records["transition"], "destroyed")
        self.assertEqual(result["product"], "verireel")
        self.assertEqual(result["context"], "verireel-testing")
        self.assertEqual(result["preview_slug"], "pr-42")
        self.assertEqual(result["destroy_outcome"], "destroyed")
        delegated = apply_destroy.call_args.kwargs["request"].destroy
        self.assertEqual(delegated.context, "verireel-testing")
        self.assertEqual(delegated.anchor_repo, "verireel")
        self.assertEqual(delegated.preview_slug, "pr-42")
        self.assertEqual(delegated.destroy_reason, "pull_request_closed")

    @patch(
        "control_plane.drivers.generic_web_preview_extensions.apply_verireel_preview_destroy_result"
    )
    def test_verireel_destroy_without_resource_or_record_returns_noop(
        self, apply_destroy: MagicMock
    ) -> None:
        apply_destroy.return_value = (
            {"transition": "destroyed_missing_preview"},
            {
                "destroy_status": "pass",
                "destroy_started_at": "2026-07-12T03:00:00Z",
                "destroy_finished_at": "2026-07-12T03:01:00Z",
                "application_name": "ver-preview-pr-42",
                "application_id": "",
                "preview_url": "",
                "error_message": "",
            },
        )

        _, result = apply_generic_web_preview_destroy_result(
            control_plane_root=Path("."),
            record_store=object(),
            request=GenericWebPreviewDestroyEnvelope(
                product="verireel",
                destroy=GenericWebPreviewDestroyRequest(
                    product="verireel",
                    anchor_pr_number=42,
                    destroy_reason="pull_request_closed",
                ),
            ),
            profile=_driver_profile(),
        )

        self.assertEqual(result["destroy_outcome"], "no_preview_recorded")

    def test_unknown_driver_owned_transport_fails_closed(self) -> None:
        with self.assertRaisesRegex(
            click.ClickException,
            "does not register a generic-web preview refresh extension",
        ):
            apply_generic_web_preview_driver_refresh(
                control_plane_root=Path("."),
                record_store=object(),
                request=GenericWebPreviewRefreshRequest(
                    product="verireel",
                    anchor_pr_number=42,
                    anchor_head_sha="a1b2c3d4",
                    image_reference="ghcr.io/cbusillo/verireel-app:pr-42-a1b2c3d4",
                ),
                profile=_driver_profile(driver_id="odoo"),
            )


if __name__ == "__main__":
    unittest.main()
