from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import Mock, patch

from control_plane import preview_lifecycle_scheduler
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.preview_lifecycle_cleanup_routes import (
    PreviewLifecycleSweepCancelled,
    PreviewLifecycleSweepEnvelope,
    build_preview_lifecycle_sweep,
)
from control_plane.preview_lifecycle_scheduler import (
    SWEEP_INTERVAL_SECONDS,
    SWEEP_LEASE_SECONDS,
    SWEEP_RECEIPT_FINGERPRINT,
    PreviewLifecycleScheduler,
    _ROUTE,
    _SCOPE,
    sweep_slot,
)
from control_plane.storage.postgres import PostgresRecordStore
from tests.support.profiles import _generic_site_profile_payload
from tests.support.stores import _sqlite_database_url


def _sweep_result(status: str = "partial") -> dict[str, object]:
    return {
        "status": status,
        "apply": False,
        "profile_count": 1,
        "profiles": [
            {
                "product": "example-site",
                "context": "example-site-preview",
                "status": "report_only",
                "orphaned_slugs": ["pr-1", "pr-2"],
                "failed_slugs": [],
                "held_reservations": [{"classification": "opaque"}],
            }
        ],
    }


class PreviewLifecycleSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(self.directory.name) / "records.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        # Start exactly at a slot boundary so tests can step across it.
        self.now = float(sweep_slot(datetime.now(timezone.utc).timestamp()) + 1)
        self.now = (
            self.now * SWEEP_INTERVAL_SECONDS
            + preview_lifecycle_scheduler.SWEEP_SLOT_OFFSET_SECONDS
        )
        self.sweep = Mock(return_value=_sweep_result())

    def make_scheduler(self) -> PreviewLifecycleScheduler:
        return PreviewLifecycleScheduler(
            self.store,
            control_plane_root=Path(self.directory.name),
            clock=lambda: self.now,
            run_sweep=self.sweep,
        )

    def receipt(self) -> dict[str, object]:
        record = self.store.read_idempotency_record(
            scope=_SCOPE, route_path=_ROUTE, idempotency_key=str(sweep_slot(self.now))
        )
        assert record is not None
        return dict(record.response_payload)

    def test_sweeps_once_per_daily_slot_across_replicas_and_restart(self) -> None:
        self.assertTrue(self.make_scheduler().run_once())
        self.assertFalse(self.make_scheduler().run_once())
        self.now += SWEEP_INTERVAL_SECONDS - 1
        self.assertFalse(self.make_scheduler().run_once())
        self.now += 1
        self.assertTrue(self.make_scheduler().run_once())
        self.assertEqual(self.sweep.call_count, 2)
        receipt = self.receipt()
        self.assertEqual(receipt["status"], "partial")
        self.assertEqual(
            receipt["profiles"],
            [
                {
                    "product": "example-site",
                    "context": "example-site-preview",
                    "status": "report_only",
                    "orphaned": 2,
                    "failed": 0,
                    "held_reservations": 1,
                }
            ],
        )

    def test_scheduled_sweep_never_applies(self) -> None:
        scheduler = PreviewLifecycleScheduler(
            self.store, control_plane_root=Path(self.directory.name), clock=lambda: self.now
        )
        with patch.object(
            preview_lifecycle_scheduler,
            "build_preview_lifecycle_sweep",
            return_value=_sweep_result("pass"),
        ) as build:
            self.assertTrue(scheduler.run_once())
        self.assertFalse(build.call_args.kwargs["request"].apply)
        self.assertIs(build.call_args.kwargs["record_store"], self.store)

    def test_failed_sweep_is_recorded_and_waits_for_the_next_slot(self) -> None:
        self.sweep.side_effect = RuntimeError("provider inventory unavailable")
        with self.assertLogs(preview_lifecycle_scheduler._LOGGER, level="ERROR"):
            self.assertTrue(self.make_scheduler().run_once())
        self.assertEqual(self.receipt()["status"], "error")
        self.assertFalse(self.make_scheduler().run_once())
        self.sweep.side_effect = None
        self.now += SWEEP_INTERVAL_SECONDS
        self.assertTrue(self.make_scheduler().run_once())
        self.assertEqual(self.receipt()["status"], "partial")

    def test_stopping_ends_the_sweep_between_products_without_a_receipt(self) -> None:
        profile = LaunchplaneProductProfileRecord.model_validate(_generic_site_profile_payload())
        with self.assertRaises(PreviewLifecycleSweepCancelled):
            build_preview_lifecycle_sweep(
                control_plane_root=Path(self.directory.name),
                record_store=self.store,
                request=PreviewLifecycleSweepEnvelope(),
                requested_profiles=(profile,),
                should_stop=lambda: True,
            )
        self.assertEqual(self.store.list_preview_inventory_scan_records(), ())
        scheduler = PreviewLifecycleScheduler(
            self.store, control_plane_root=Path(self.directory.name), clock=lambda: self.now
        )

        def stop_during_sweep(**kwargs: object) -> dict[str, object]:
            scheduler.stop()
            should_stop = kwargs["should_stop"]
            assert callable(should_stop)
            if should_stop():
                raise PreviewLifecycleSweepCancelled()
            return _sweep_result()

        with patch.object(
            preview_lifecycle_scheduler,
            "build_preview_lifecycle_sweep",
            side_effect=stop_during_sweep,
        ):
            self.assertFalse(scheduler.run_once())
        self.assertIsNone(
            self.store.read_idempotency_record(
                scope=_SCOPE, route_path=_ROUTE, idempotency_key=str(sweep_slot(self.now))
            )
        )
        self.assertTrue(self.make_scheduler().run_once())

    def test_replica_waits_for_lease_holder_and_recovers_an_expired_lease(self) -> None:
        with patch.object(
            self.store,
            "_database_mutation_timestamp",
            side_effect=lambda _session: (
                datetime.fromtimestamp(self.now, timezone.utc).isoformat().replace("+00:00", "Z")
            ),
        ):
            claim = self.store.reserve_mutation(
                scope=_SCOPE,
                route_path=_ROUTE,
                idempotency_key="active",
                request_fingerprint=SWEEP_RECEIPT_FINGERPRINT,
                lease_owner="dead-worker-fixture",
                lease_seconds=SWEEP_LEASE_SECONDS,
            )
            self.assertEqual(claim.status, "acquired")
            self.assertFalse(self.make_scheduler().run_once())
            self.sweep.assert_not_called()
            self.now += SWEEP_LEASE_SECONDS + 1
            self.assertTrue(self.make_scheduler().run_once())
        self.sweep.assert_called_once()


if __name__ == "__main__":
    unittest.main()
