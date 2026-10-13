import unittest

import click
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from typing import Any
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord

from control_plane.contracts.preview_lifecycle_plan_record import PreviewLifecyclePlanRecord
from control_plane.workflows.preview_desired_state import build_preview_desired_state_record
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.generic_web_preview import GenericWebPreviewDestroyResult
from control_plane.workflows.preview_lifecycle_cleanup import build_preview_lifecycle_cleanup_record
from tests.test_generic_web_preview import _profile


class PreviewLifecycleCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        def closed_prs(
            *, profile: LaunchplaneProductProfileRecord, **_kwargs: Any
        ) -> PreviewDesiredStateRecord:
            return build_preview_desired_state_record(
                product=profile.product,
                context=profile.preview.context,
                source="test",
                discovered_at="2026-10-13T00:00:00Z",
                repository=profile.repository,
                anchor_repo=profile.repository.partition("/")[2],
                preview_slug_prefix="pr-",
                desired_previews=(),
            )

        mock = patch(
            "control_plane.workflows.preview_lifecycle_cleanup.discover_generic_web_preview_desired_state",
            side_effect=closed_prs,
        )
        mock.start()
        self.addCleanup(mock.stop)

    def test_cleanup_uses_the_declared_postgres_transport(self) -> None:
        from tests.test_generic_web_preview_extensions import _driver_profile

        profile = _driver_profile()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = FilesystemRecordStore(root / "state")
            store.write_product_profile_record(profile)
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-video",
                    context=profile.preview.context,
                    anchor_repo=profile.repository.partition("/")[2],
                    anchor_pr_number=42,
                    anchor_pr_url="https://example.test/pr/42",
                    preview_label="video/pr-42",
                    canonical_url="https://preview.example.test",
                    state="active",
                    created_at="2026-10-12T00:00:00Z",
                    updated_at="2026-10-12T00:00:00Z",
                    eligible_at="2026-10-12T00:00:00Z",
                )
            )
            plan = PreviewLifecyclePlanRecord(
                plan_id="cleanup-video",
                product=profile.product,
                context=profile.preview.context,
                planned_at="2026-10-12T00:00:01Z",
                source="test",
                status="pass",
                inventory_scan_id="scan-video",
                orphaned_slugs=("pr-42",),
            )
            result = GenericWebPreviewDestroyResult(
                destroy_status="pass",
                destroy_started_at="2026-10-12T00:01:00Z",
                destroy_finished_at="2026-10-12T00:01:01Z",
                product=profile.product,
                context=profile.preview.context,
                preview_slug="pr-42",
                application_name="video-pr-42-app",
                application_id="preview-target",
            )
            with (
                patch(
                    "control_plane.workflows.preview_lifecycle_cleanup.apply_generic_web_preview_driver_destroy",
                    return_value=({}, result.model_dump(mode="json")),
                ) as extension,
                patch(
                    "control_plane.workflows.preview_lifecycle_cleanup._execute_generic_web_preview_destroy_unserialized"
                ) as plain,
            ):
                cleanup = build_preview_lifecycle_cleanup_record(
                    plan=plan,
                    requested_at="2026-10-12T00:02:00Z",
                    source="test",
                    apply=True,
                    destroy_reason="orphan",
                    control_plane_root=root,
                    record_store=store,
                    timeout_seconds=300,
                    driver_id="generic-web",
                    profile=profile,
                )
            self.assertEqual(cleanup.status, "pass")
            self.assertEqual(store.read_preview_record("preview-video").state, "destroyed")
            self.assertEqual(extension.call_args.kwargs["profile"], profile)
            self.assertEqual(extension.call_args.kwargs["request"].anchor_pr_number, 42)
            plain.assert_not_called()

    def test_sweep_records_inventory_failures_and_continues_other_products(self) -> None:
        from control_plane.preview_lifecycle_cleanup_routes import (
            PreviewLifecycleSweepEnvelope,
            build_preview_lifecycle_sweep,
        )

        first = _profile()
        second = first.model_copy(update={"product": "other-example"})
        with TemporaryDirectory() as directory:
            root = Path(directory)
            store = FilesystemRecordStore(state_dir=root / "state")
            with patch(
                "control_plane.preview_lifecycle_cleanup_routes.execute_generic_web_preview_inventory",
                side_effect=[
                    click.ClickException("incomplete first inventory"),
                    click.ClickException("incomplete second inventory"),
                ],
            ):
                result = build_preview_lifecycle_sweep(
                    control_plane_root=root,
                    record_store=store,
                    request=PreviewLifecycleSweepEnvelope(
                        apply=True, destroy_reason="remove orphans"
                    ),
                    requested_profiles=(first, second),
                )
        self.assertEqual(result["status"], "fail")
        self.assertEqual(
            result["profiles"],
            [
                {
                    "product": profile.product,
                    "context": profile.preview.context,
                    "driver_id": profile.driver_id,
                    "cleanup_driver_id": "generic-web",
                    "held_reservations": [],
                    "held_reservations_truncated": False,
                    "status": "fail",
                    "error_message": message,
                }
                for profile, message in (
                    (first, "incomplete first inventory"),
                    (second, "incomplete second inventory"),
                )
            ],
        )

    def test_generic_web_cleanup_destroys_orphan_with_matching_preview_record(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_preview_record(
                PreviewRecord(
                    preview_id="preview-syo-testing-sellyouroutboard-pr-42",
                    context="sellyouroutboard-testing",
                    anchor_repo="site-repository",
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
                planned_at="2026-04-30T21:00:01Z",
                source="test",
                status="pass",
                inventory_scan_id="preview-inventory-scan-syo-testing-1",
                orphaned_slugs=("preview-42-site",),
            )

            with patch(
                "control_plane.workflows.preview_lifecycle_cleanup._execute_generic_web_preview_destroy_unserialized",
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
                refused = build_preview_lifecycle_cleanup_record(
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
                    profile=_profile().model_copy(update={"repository": "example/unrelated"}),
                )
                self.assertEqual(refused.status, "blocked")
                destroy.assert_not_called()
                self.assertEqual(
                    store.read_preview_record("preview-syo-testing-sellyouroutboard-pr-42").state,
                    "active",
                )
                store.write_product_profile_record(
                    _profile().model_copy(update={"repository": "example/site-repository"})
                )
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
                    profile=_profile().model_copy(update={"repository": "example/site-repository"}),
                )

            self.assertEqual(record.status, "pass")
            self.assertEqual(record.destroyed_slugs, ("preview-42-site",))
            self.assertEqual(record.results[0].anchor_pr_number, 42)
            self.assertEqual(record.results[0].anchor_repo, "site-repository")
            destroy.assert_called_once()
            preview = store.read_preview_record("preview-syo-testing-sellyouroutboard-pr-42")
            self.assertEqual(preview.state, "destroyed")
            self.assertEqual(preview.destroy_reason, "test_cleanup")

    def test_generic_web_cleanup_blocks_slug_that_does_not_match_template(self) -> None:
        plan = PreviewLifecyclePlanRecord(
            plan_id="preview-lifecycle-plan-syo-testing-1",
            product="sellyouroutboard",
            context="sellyouroutboard-testing",
            planned_at="2026-04-30T21:00:01Z",
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
            profile=_profile(),
            timeout_seconds=300,
            driver_id="generic-web",
            preview_slug_template="preview-{number}-site",
        )

        self.assertEqual(record.status, "blocked")
        self.assertEqual(record.blocked_slugs, ("bad-slug",))

    def test_generic_web_cleanup_treats_missing_application_as_destroyed(self) -> None:
        with TemporaryDirectory() as temporary_directory_name:
            root = Path(temporary_directory_name)
            store = FilesystemRecordStore(state_dir=root / "state")
            store.write_product_profile_record(_profile())
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
                planned_at="2026-04-30T21:00:01Z",
                source="test",
                status="pass",
                inventory_scan_id="preview-inventory-scan-syo-testing-1",
                orphaned_slugs=("preview-42-site",),
            )

            with patch(
                "control_plane.workflows.preview_lifecycle_cleanup._execute_generic_web_preview_destroy_unserialized",
                return_value=GenericWebPreviewDestroyResult(
                    destroy_status="pass",
                    destroy_started_at="2026-04-30T21:01:00Z",
                    destroy_finished_at="2026-04-30T21:01:02Z",
                    product="sellyouroutboard",
                    context="sellyouroutboard-testing",
                    preview_slug="preview-42-site",
                    application_name="syo-preview-preview-42-site",
                    application_id="",
                ),
            ):
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
                    profile=_profile(),
                )

            self.assertEqual(record.status, "pass")
            self.assertEqual(record.destroyed_slugs, ("preview-42-site",))
            self.assertEqual(record.failed_slugs, ())
            self.assertEqual(record.results[0].status, "destroyed")
            self.assertEqual(record.results[0].application_id, "")
            self.assertEqual(record.results[0].error_message, "")
            preview = store.read_preview_record("preview-syo-testing-sellyouroutboard-pr-42")
            self.assertEqual(preview.state, "destroyed")
            self.assertEqual(preview.destroy_reason, "test_cleanup")


if __name__ == "__main__":
    unittest.main()
