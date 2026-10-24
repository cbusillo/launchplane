from datetime import datetime, timezone, timedelta
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock

from control_plane.clean_testing_snapshot import (
    CleanTestingSnapshot,
    preview_snapshot,
    publish_clean_testing_snapshot,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import _odoo_profile_payload_with_prod_lane
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord


class CleanTestingSnapshotTests(unittest.TestCase):
    def setUp(self) -> None:
        self.profile = LaunchplaneProductProfileRecord.model_validate(
            _odoo_profile_payload_with_prod_lane("example-odoo")
        )
        self.profile.preview.clean_testing_snapshot = True
        self.context = self.profile.preview.context
        self.now = datetime.now(timezone.utc).isoformat()
        self.snapshot = CleanTestingSnapshot(
            product=self.profile.product,
            context=self.context,
            snapshot_id="a" * 64,
            server_id="fixture-server",
            database_sha256="b" * 64,
            filestore_sha256="c" * 64,
            captured_at=self.now,
        )

    def test_preview_reads_only_the_completed_snapshot_without_a_live_source_fallback(self) -> None:
        store = Mock(spec=["read_clean_testing_snapshot"])
        store.read_clean_testing_snapshot.return_value = self.snapshot
        self.assertEqual(preview_snapshot(store, self.profile), self.snapshot)
        store.read_clean_testing_snapshot.assert_called_once_with(
            self.profile.product, self.context
        )
        store.read_clean_testing_snapshot.return_value = None
        with self.assertRaisesRegex(ValueError, "clean snapshot"):
            preview_snapshot(store, self.profile)

    def test_foreign_snapshot_is_refused_before_any_preview_copy(self) -> None:
        store = Mock(spec=["read_clean_testing_snapshot"])
        store.read_clean_testing_snapshot.return_value = self.snapshot.model_copy(
            update={"product": "another-product"}
        )
        with self.assertRaises(ValueError):
            preview_snapshot(store, self.profile)

    def test_failed_refresh_keeps_the_last_snapshot_in_persistent_storage(self) -> None:
        with TemporaryDirectory() as directory:
            store = PostgresRecordStore(database_url=f"sqlite+pysqlite:///{directory}/records.db")
            store.ensure_schema()
            self.addCleanup(store.close)
            store.write_clean_testing_snapshot(self.snapshot)
            complete = {
                "testing_copy_sanitized": "true",
                "integration_readback_ok": "true",
                "odoo_restore_completed": "true",
                "clean_testing_snapshot_completed": "true",
                "clean_snapshot_database_sha256": "e" * 64,
                "clean_snapshot_filestore_sha256": "f" * 64,
            }
            later = (datetime.fromisoformat(self.now) + timedelta(seconds=1)).isoformat()
            for failed in (
                "testing_copy_sanitized",
                "integration_readback_ok",
                "odoo_restore_completed",
                "clean_testing_snapshot_completed",
            ):
                evidence = {**complete, failed: "false"}
                with self.subTest(failed=failed):
                    with self.assertRaises(ValueError):
                        publish_clean_testing_snapshot(
                            store,
                            product=self.profile.product,
                            context=self.context,
                            snapshot_id="d" * 64,
                            server_id="fixture-server",
                            evidence=evidence,
                            captured_at=later,
                        )
                    self.assertEqual(
                        store.read_clean_testing_snapshot(self.profile.product, self.context),
                        self.snapshot,
                    )
            published = publish_clean_testing_snapshot(
                store,
                product=self.profile.product,
                context=self.context,
                snapshot_id="d" * 64,
                server_id="fixture-server",
                evidence=complete,
                captured_at=later,
            )
            self.assertEqual(
                store.read_clean_testing_snapshot(self.profile.product, self.context), published
            )


if __name__ == "__main__":
    unittest.main()
