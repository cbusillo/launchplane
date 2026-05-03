import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from control_plane.contracts.preview_lifecycle_plan_record import PreviewLifecyclePlanRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.generic_web_preview import GenericWebPreviewDestroyResult
from control_plane.workflows.preview_lifecycle_cleanup import build_preview_lifecycle_cleanup_record
from control_plane.workflows.verireel_preview_driver import VeriReelPreviewDestroyResult


class PreviewLifecycleCleanupTests(unittest.TestCase):
    def test_generic_web_cleanup_destroys_orphan_with_matching_preview_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-syo-testing-sellyouroutboard-pr-42",
                    context="sellyouroutboard-testing",
                    anchor_repo="sellyouroutboard",
                    anchor_pr_number=42,
                    anchor_pr_url="https://github.com/cbusillo/sellyouroutboard/pull/42",
                    preview_label="sellyouroutboard/pr-42",
                    canonical_url="https://preview-42-site.example.com",
                    state="active",
                    created_at="2026-04-30T21:00:00Z",
                    updated_at="2026-04-30T21:00:00Z",
                    eligible_at="2026-04-30T21:00:00Z",
                )
            )
            plan = PreviewLifecyclePlanRecord(
                plan_id="preview-lifecycle-plan-syo-testing-1",
                product="sellyouroutboard",
                context="sellyouroutboard-testing",
                planned_at="2026-04-30T21:00:00Z",
                source="test",
                status="pass",
                inventory_scan_id="preview-inventory-scan-syo-testing-1",
                orphaned_slugs=("preview-42-site",),
            )

            with patch(
                "control_plane.workflows.preview_lifecycle_cleanup.execute_generic_web_preview_destroy",
                return_value=GenericWebPreviewDestroyResult(
                    destroy_status="pass",
                    destroy_started_at="2026-04-30T21:01:00Z",
                    destroy_finished_at="2026-04-30T21:01:02Z",
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    preview_slug="preview-42-site",
                    application_name="syo-preview-preview-42-site",
                    application_id="app-42",
                ),
            ) as destroy:
                record = build_preview_lifecycle_cleanup_record(
                    plan=plan,
                    requested_at="2026-04-30T21:02:00Z",
                    source="test",
                    apply=True,
                    destroy_reason="test_cleanup",
                    control_plane_root=root,
                    record_store=store,
                    timeout_seconds=300,
                    driver_id="generic-web",
                    preview_slug_template="preview-{number}-site",
                )

            self.assertEqual(record.status, "pass")
            self.assertEqual(record.destroyed_slugs, ("preview-42-site",))
            self.assertEqual(record.results[0].anchor_pr_number, 42)
            destroy.assert_called_once()
            preview = store.read_preview_record("preview-syo-testing-sellyouroutboard-pr-42")
            self.assertEqual(preview.state, "destroyed")
            self.assertEqual(preview.destroy_reason, "test_cleanup")

    def test_generic_web_cleanup_blocks_slug_that_does_not_match_template(self) -> None:
        plan = PreviewLifecyclePlanRecord(
            plan_id="preview-lifecycle-plan-syo-testing-1",
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            planned_at="2026-04-30T21:00:00Z",
            source="test",
            status="pass",
            inventory_scan_id="preview-inventory-scan-syo-testing-1",
            orphaned_slugs=("bad-slug",),
        )

        record = build_preview_lifecycle_cleanup_record(
            plan=plan,
            requested_at="2026-04-30T21:02:00Z",
            source="test",
            apply=True,
            destroy_reason="test_cleanup",
            control_plane_root=Path("."),
            record_store=object(),
            timeout_seconds=300,
            driver_id="generic-web",
            preview_slug_template="preview-{number}-site",
        )

        self.assertEqual(record.status, "blocked")
        self.assertEqual(record.blocked_slugs, ("bad-slug",))

    def test_verireel_cleanup_uses_plan_product_as_anchor_repo(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-video-site-testing-video-site-pr-42",
                    context="video-site-testing",
                    anchor_repo="video-site",
                    anchor_pr_number=42,
                    anchor_pr_url="https://github.com/every/video-site/pull/42",
                    preview_label="video-site/pr-42",
                    canonical_url="https://pr-42.video-preview.example.com",
                    state="active",
                    created_at="2026-04-30T21:00:00Z",
                    updated_at="2026-04-30T21:00:00Z",
                    eligible_at="2026-04-30T21:00:00Z",
                )
            )
            plan = PreviewLifecyclePlanRecord(
                plan_id="preview-lifecycle-plan-video-site-testing-1",
                product="video-site",
                context="video-site-testing",
                planned_at="2026-04-30T21:00:00Z",
                source="test",
                status="pass",
                inventory_scan_id="preview-inventory-scan-video-site-testing-1",
                orphaned_slugs=("pr-42",),
            )

            with patch(
                "control_plane.workflows.preview_lifecycle_cleanup.execute_verireel_preview_destroy",
                return_value=VeriReelPreviewDestroyResult(
                    destroy_status="pass",
                    destroy_started_at="2026-04-30T21:01:00Z",
                    destroy_finished_at="2026-04-30T21:01:02Z",
                    application_name="ver-preview-pr-42-app",
                    application_id="app-42",
                    preview_url="https://pr-42.video-preview.example.com",
                ),
            ) as destroy:
                record = build_preview_lifecycle_cleanup_record(
                    plan=plan,
                    requested_at="2026-04-30T21:02:00Z",
                    source="test",
                    apply=True,
                    destroy_reason="test_cleanup",
                    control_plane_root=root,
                    record_store=store,
                    timeout_seconds=300,
                    driver_id="verireel",
                    preview_slug_template="pr-{number}",
                )

            self.assertEqual(record.status, "pass")
            self.assertEqual(record.destroyed_slugs, ("pr-42",))
            self.assertEqual(record.results[0].anchor_repo, "video-site")
            destroy.assert_called_once()
            destroy_request = destroy.call_args.kwargs["request"]
            self.assertEqual(destroy_request.context, "video-site-testing")
            self.assertEqual(destroy_request.anchor_repo, "video-site")
            preview = store.read_preview_record("preview-video-site-testing-video-site-pr-42")
            self.assertEqual(preview.state, "destroyed")
            self.assertEqual(preview.destroy_reason, "test_cleanup")


if __name__ == "__main__":
    unittest.main()
