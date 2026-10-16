from datetime import datetime, timezone
from types import SimpleNamespace
import unittest

from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from control_plane.contracts.product_environment_read_model import build_product_site_overview
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from tests.test_product_environment_read_model import _PreviewRecordStore, _site_profile_payload


class PreviewInventoryEvidenceTests(unittest.TestCase):
    def test_product_uses_real_empty_scan_instead_of_default_preview_provenance(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_site_profile_payload())
        base_store = _PreviewRecordStore(profile, ())
        scan = PreviewInventoryScanRecord(
            scan_id="observed-empty",
            context=profile.preview.context,
            scanned_at=datetime.now(timezone.utc).isoformat(),
            source="provider",
            status="pass",
            preview_count=0,
        )
        # model_copy also lets the regression run on the original scan schema.
        scan = scan.model_copy(
            update={"product": profile.product, "repository": profile.repository}
        )
        store = SimpleNamespace(
            read_product_profile_record=base_store.read_product_profile_record,
            list_product_profile_records=base_store.list_product_profile_records,
            list_preview_records=base_store.list_preview_records,
            list_public_ingress_incident_records=base_store.list_public_ingress_incident_records,
            list_preview_inventory_scan_records=lambda **_: (scan,),
        )
        overview = build_product_site_overview(
            record_store=store,
            product=profile.product,
            action_allowed=lambda *_: True,
        )
        self.assertEqual(overview.preview.trust_state, "verified")
        self.assertEqual(overview.preview.provenance.source_record_id, scan.scan_id)
        self.assertEqual(overview.preview.provenance.refreshed_at, scan.scanned_at)

    def test_reads_do_not_refresh_stale_failed_or_truncated_scans(self) -> None:
        from datetime import timedelta
        from control_plane.preview_inventory_evidence import (
            PREVIEW_INVENTORY_STALE_AFTER,
            inventory_scan_evidence,
        )

        now = datetime.now(timezone.utc)
        scan = PreviewInventoryScanRecord(
            scan_id="scan",
            context="preview",
            scanned_at=now.isoformat(),
            source="provider",
            status="pass",
            preview_count=0,
        )
        for changes, expected in (
            ({}, "empty"),
            ({"preview_count": 1, "preview_slugs": ("pr-7",)}, "nonempty"),
            ({"status": "fail"}, "failed"),
            ({"truncated": True}, "failed"),
            ({"status": "unavailable"}, "unavailable"),
            ({"status": "unsupported"}, "unsupported"),
            (
                {
                    "scanned_at": (
                        now - PREVIEW_INVENTORY_STALE_AFTER - timedelta(seconds=1)
                    ).isoformat()
                },
                "stale",
            ),
            ({"scanned_at": (now + timedelta(days=1)).isoformat()}, "failed"),
            ({"scanned_at": "invalid"}, "failed"),
        ):
            with self.subTest(expected=expected, changes=changes):
                record = scan.model_copy(update=changes)
                result = inventory_scan_evidence(record)
                self.assertEqual(result.status, expected)
                self.assertEqual(result.provenance.refreshed_at, record.scanned_at)
                if expected not in {"empty", "nonempty"}:
                    self.assertNotEqual(result.provenance.freshness_status, "verified")

    def test_scan_scope_and_latest_failure_override_old_success(self) -> None:
        from control_plane.preview_inventory_evidence import read_preview_inventory_evidence

        profile = LaunchplaneProductProfileRecord.model_validate(_site_profile_payload())
        scan = PreviewInventoryScanRecord(
            scan_id="old",
            context=profile.preview.context,
            scanned_at=datetime.now(timezone.utc).isoformat(),
            source="provider",
            status="pass",
            preview_count=0,
            product=profile.product,
            repository=profile.repository,
        )
        failed = scan.model_copy(update={"scan_id": "failed", "status": "fail"})
        foreign = scan.model_copy(update={"scan_id": "foreign", "repository": "other/site"})
        store = SimpleNamespace(
            list_preview_inventory_scan_records=lambda **_: (foreign, failed, scan)
        )
        evidence = read_preview_inventory_evidence(
            record_store=store,
            context=profile.preview.context,
            product=profile.product,
            repository=profile.repository,
        )
        self.assertEqual(evidence.status, "failed")
        self.assertEqual(evidence.provenance.source_record_id, failed.scan_id)
        legacy = scan.model_copy(update={"product": "", "repository": ""})
        store.list_preview_inventory_scan_records = lambda **_: (legacy,)
        store.list_product_profile_records = lambda: (
            profile,
            profile.model_copy(update={"product": "other"}),
        )
        self.assertEqual(
            read_preview_inventory_evidence(
                record_store=store,
                context=profile.preview.context,
                product=profile.product,
                repository=profile.repository,
            ).status,
            "missing",
        )

    def test_product_and_driver_agree_with_counts_and_unmatched_identities(self) -> None:
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from control_plane.storage.filesystem import FilesystemRecordStore
        from control_plane.drivers.registry import build_driver_context_view
        from tests.test_product_environment_read_model import _preview_record

        profile = LaunchplaneProductProfileRecord.model_validate(_site_profile_payload())
        with TemporaryDirectory() as directory:
            store = FilesystemRecordStore(state_dir=Path(directory))
            store.write_product_profile_record(profile)
            store.write_preview_record(
                _preview_record(
                    preview_id="recorded",
                    context=profile.preview.context,
                    anchor_repo="example-site",
                    state="active",
                    updated_at=datetime.now(timezone.utc).isoformat(),
                )
            )
            scan = PreviewInventoryScanRecord(
                scan_id="actual",
                context=profile.preview.context,
                product=profile.product,
                repository=profile.repository,
                scanned_at=datetime.now(timezone.utc).isoformat(),
                source="provider",
                status="pass",
                preview_count=1,
                preview_slugs=("pr-2",),
            )
            store.write_preview_inventory_scan_record(scan)
            overview = build_product_site_overview(
                record_store=store, product=profile.product, action_allowed=lambda *_: True
            )
            driver = build_driver_context_view(
                record_store=store, context_name=profile.preview.context
            ).drivers[0]
            self.assertEqual(overview.preview.provenance, driver.preview_inventory_provenance)
            self.assertEqual(overview.preview.active_count, 1)
            assert overview.preview.inventory is not None
            self.assertEqual(overview.preview.inventory.observed_count, scan.preview_count)
            self.assertEqual(overview.preview.unobserved_record_slugs, ("pr-1",))
            self.assertEqual(overview.preview.untracked_observed_slugs, ("pr-2",))
            denied = build_product_site_overview(
                record_store=store, product=profile.product, action_allowed=lambda *_: False
            )
            assert denied.preview.inventory is not None
            self.assertEqual(denied.preview.inventory.observed_slugs, ())
            self.assertEqual(denied.preview.untracked_observed_slugs, ())
