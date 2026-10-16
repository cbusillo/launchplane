from pathlib import Path
from control_plane.contracts.preview_inventory_scan_record import PreviewInventoryScanRecord
from tempfile import TemporaryDirectory
from unittest.mock import patch
import unittest

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.preview_inventory_collection import collect_preview_inventory
from control_plane.preview_lifecycle_cleanup_routes import PreviewLifecycleSweepCancelled
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_preview import effective_preview_app_name_prefix
from tests.support.stores import _sqlite_database_url
from tests.test_product_environment_read_model import _site_profile_payload


class PreviewInventoryCollectionTests(unittest.TestCase):
    def setUp(self) -> None:
        directory = TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(self.root / "records.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.profile = LaunchplaneProductProfileRecord.model_validate(_site_profile_payload())
        self.store.write_product_profile_record(self.profile)

    def collect(self, payload: object) -> tuple[PreviewInventoryScanRecord, ...]:
        with (
            patch(
                "control_plane.preview_inventory_collection.dokploy_source.read_dokploy_config",
                return_value=("https://provider.invalid", "fixture"),
            ),
            patch(
                "control_plane.workflows.generic_web_preview.dokploy_api.dokploy_request",
                return_value=payload,
            ) as request,
        ):
            collect_preview_inventory(control_plane_root=self.root, record_store=self.store)
        # Only provider GET inventory is reachable, including on malformed responses.
        self.assertTrue(request.call_args_list)
        self.assertTrue(
            all(call.kwargs.get("method", "GET") == "GET" for call in request.call_args_list)
        )
        self.assertEqual(self.store.list_preview_lifecycle_cleanup_records(), ())
        self.assertEqual(self.store.list_preview_desired_state_records(), ())
        self.assertEqual(self.store.list_deployment_records(), ())
        return self.store.list_preview_inventory_scan_records()

    def test_real_empty_nonempty_and_failed_scans_persist_without_cleanup(self) -> None:
        first = self.collect([])[0]
        self.assertEqual(first.status, "pass")
        self.assertEqual(first.preview_count, 0)
        prefix = effective_preview_app_name_prefix(profile=self.profile)
        second = self.collect(
            [
                {
                    "environments": [
                        {
                            "applications": [
                                {"applicationId": "app-7", "name": f"{prefix}-pr-7"},
                                {"applicationId": "unrelated", "name": "another-product"},
                            ]
                        }
                    ]
                }
            ]
        )[0]
        self.assertEqual(second.preview_slugs, ("pr-7",))
        self.assertEqual(second.product, self.profile.product)
        self.assertEqual(second.repository, self.profile.repository)
        third = self.collect([{"environments": [{}]}])[0]
        self.assertEqual(third.status, "fail")
        self.assertNotEqual(third.scan_id, second.scan_id)
        self.assertNotEqual(third.scanned_at, first.scanned_at)

    def test_truncated_and_ambiguous_inventory_never_proves_empty(self) -> None:
        prefix = effective_preview_app_name_prefix(profile=self.profile)
        payloads: tuple[object, ...] = (
            {"items": [], "truncated": True},
            [{"environments": []}, None],
            [
                {
                    "environments": [
                        {
                            "applications": [
                                {"applicationId": "a", "name": f"{prefix}-pr-7"},
                                {"applicationId": "b", "name": f"{prefix}-pr-7"},
                            ]
                        }
                    ]
                }
            ],
        )
        for payload in payloads:
            with self.subTest(payload=payload):
                self.assertEqual(self.collect(payload)[0].status, "fail")

    def test_disabled_skipped_and_unsupported_and_unavailable_are_persisted(self) -> None:
        self.store.write_product_profile_record(
            self.profile.model_copy(
                update={"preview": self.profile.preview.model_copy(update={"enabled": False})}
            )
        )
        with patch(
            "control_plane.preview_inventory_collection.execute_generic_web_preview_inventory"
        ) as execute:
            collect_preview_inventory(control_plane_root=self.root, record_store=self.store)
            execute.assert_not_called()
        self.assertEqual(self.store.list_preview_inventory_scan_records(), ())
        self.store.write_product_profile_record(self.profile)
        with patch(
            "control_plane.preview_inventory_collection.execute_generic_web_preview_inventory",
            side_effect=RuntimeError("secret must not appear"),
        ):
            collect_preview_inventory(control_plane_root=self.root, record_store=self.store)
        scan = self.store.list_preview_inventory_scan_records()[0]
        self.assertEqual(scan.status, "unavailable")
        self.assertNotIn("secret", scan.error_message)
        with patch(
            "control_plane.preview_inventory_collection.read_driver_descriptor"
        ) as descriptor:
            descriptor.return_value.base_driver_id = "other"
            collect_preview_inventory(control_plane_root=self.root, record_store=self.store)
        self.assertEqual(self.store.list_preview_inventory_scan_records()[0].status, "unsupported")

    def test_odoo_collection_uses_complete_compose_inventory(self) -> None:
        profile = self.profile.model_copy(update={"driver_id": "odoo"})
        self.store.write_product_profile_record(profile)
        prefix = effective_preview_app_name_prefix(profile=profile)
        with (
            patch(
                "control_plane.preview_inventory_collection.dokploy_source.read_dokploy_config",
                return_value=("https://provider.invalid", "fixture"),
            ),
            patch(
                "control_plane.preview_inventory_collection._complete_preview_compose_inventory",
                return_value=(("env", "compose", f"{prefix}-pr-9"),),
            ),
        ):
            collect_preview_inventory(control_plane_root=self.root, record_store=self.store)
        self.assertEqual(
            self.store.list_preview_inventory_scan_records()[0].preview_slugs, ("pr-9",)
        )

    def test_shutdown_does_not_write_a_scan(self) -> None:
        with self.assertRaises(PreviewLifecycleSweepCancelled):
            collect_preview_inventory(
                control_plane_root=self.root, record_store=self.store, should_stop=lambda: True
            )
        self.assertEqual(self.store.list_preview_inventory_scan_records(), ())

    def test_caller_source_cannot_forge_persisted_product_scope(self) -> None:
        from control_plane.contracts.preview_inventory_scan_record import (
            PREVIEW_INVENTORY_COLLECTION_SOURCE,
        )
        from control_plane.preview_inventory_evidence import read_preview_inventory_evidence

        scoped = PreviewInventoryScanRecord(
            scan_id="another-product-scan",
            context=self.profile.preview.context,
            product="other-site",
            repository="example/other-site",
            scanned_at=self.profile.updated_at,
            source=PREVIEW_INVENTORY_COLLECTION_SOURCE,
            status="pass",
            preview_count=0,
        )
        supplied_source = scoped.model_dump()["source"]
        caller_scan = PreviewInventoryScanRecord(
            scan_id="caller-inventory",
            context=self.profile.preview.context,
            scanned_at=self.profile.updated_at,
            source=str(supplied_source),
            status="pass",
            preview_count=0,
        )
        self.assertEqual(caller_scan.product, "")
        self.store.write_preview_inventory_scan_record(caller_scan)
        restored = self.store.list_preview_inventory_scan_records()[0]
        self.assertEqual(restored.product, "")
        self.assertEqual(
            read_preview_inventory_evidence(
                record_store=self.store,
                context=self.profile.preview.context,
                product=scoped.product,
                repository=scoped.repository,
            ).status,
            "missing",
        )

    def test_lifecycle_query_filters_background_rows_before_limiting(self) -> None:
        from datetime import datetime, timedelta, timezone
        from control_plane.contracts.preview_inventory_scan_record import (
            PREVIEW_INVENTORY_COLLECTION_SOURCE,
        )
        from control_plane.preview_inventory_evidence import PREVIEW_INVENTORY_SCAN_READ_LIMIT

        now = datetime.now(timezone.utc)
        lifecycle = PreviewInventoryScanRecord(
            scan_id="lifecycle",
            context=self.profile.preview.context,
            scanned_at=now.isoformat(),
            source="explicit-lifecycle",
            status="pass",
            preview_count=0,
        )
        self.store.write_preview_inventory_scan_record(lifecycle)
        for index in range(PREVIEW_INVENTORY_SCAN_READ_LIMIT + 1):
            self.store.write_preview_inventory_scan_record(
                lifecycle.model_copy(
                    update={
                        "scan_id": f"background-{index}",
                        "source": PREVIEW_INVENTORY_COLLECTION_SOURCE,
                        "product": self.profile.product,
                        "repository": self.profile.repository,
                        "scanned_at": (now + timedelta(seconds=index + 1)).isoformat(),
                    }
                )
            )
        selected = self.store.list_preview_inventory_scan_records(
            context_name=self.profile.preview.context,
            limit=1,
            exclude_source=PREVIEW_INVENTORY_COLLECTION_SOURCE,
        )
        self.assertEqual(selected, (lifecycle,))
