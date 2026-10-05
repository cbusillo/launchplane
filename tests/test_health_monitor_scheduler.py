from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
import unittest
from unittest.mock import Mock, patch

from control_plane.health_monitor_scheduler import (
    HealthMonitorScheduler,
    MONITOR_COMPLETION_GRACE_SECONDS,
    MONITOR_INTERVAL_SECONDS,
)
from control_plane.contracts.public_ingress_monitoring import (
    PublicIngressNotificationDestination,
    PublicIngressNotificationPolicyRecord,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    PublicIngressNotificationDriverSet,
    record_monitor_cadence,
    run_public_ingress_monitor_once,
)
from tests.support.stores import _sqlite_database_url
from tests.test_public_ingress_monitor import _Store, _profile


class HealthMonitorSchedulerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.store = PostgresRecordStore(
            database_url=_sqlite_database_url(Path(self.directory.name) / "records.sqlite3")
        )
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.now = float(int(datetime.now(timezone.utc).timestamp() / MONITOR_INTERVAL_SECONDS))
        self.now *= MONITOR_INTERVAL_SECONDS
        self.probe = Mock()
        self.alert = Mock()
        self.scheduler = self.make_scheduler()
        self.scheduler._write_completion("enabled", self.now)

    def make_scheduler(self) -> HealthMonitorScheduler:
        return HealthMonitorScheduler(
            self.store,
            clock=lambda: self.now,
            run_monitor=self.probe,
            report_cadence=self.alert,
        )

    def test_runs_once_per_slot_across_replicas_and_restart(self) -> None:
        self.assertTrue(self.scheduler.run_once())
        self.assertFalse(self.make_scheduler().run_once())
        self.now += MONITOR_INTERVAL_SECONDS - 1
        self.assertFalse(self.scheduler.run_once())
        self.now += 1
        self.assertTrue(self.scheduler.run_once())
        self.assertEqual(self.probe.call_count, 2)

    def test_missed_run_alerts_then_recovers_only_after_completion(self) -> None:
        self.now += MONITOR_COMPLETION_GRACE_SECONDS - 1
        self.assertFalse(self.scheduler.watch_once())
        self.alert.assert_not_called()
        self.now += 1
        self.assertTrue(self.scheduler.watch_once())
        self.assertTrue(self.scheduler.watch_once())
        self.assertEqual(self.alert.call_count, 1)
        self.assertTrue(self.alert.call_args.args[0])
        self.scheduler.run_once()
        self.assertFalse(self.scheduler.watch_once())
        self.assertFalse(self.alert.call_args.args[0])

    def test_failure_is_not_completion_and_can_retry(self) -> None:
        self.probe.side_effect = RuntimeError("probe failed")
        with self.assertRaises(RuntimeError):
            self.scheduler.run_once()
        self.now += MONITOR_COMPLETION_GRACE_SECONDS
        self.assertTrue(self.scheduler.watch_once())
        self.probe.side_effect = None
        self.assertTrue(self.scheduler.run_once())
        self.assertFalse(self.scheduler.watch_once())

    def test_watchdog_alerts_during_blocked_probe_and_other_replica_waits(self) -> None:
        entered = Event()
        release = Event()

        def blocked_probe() -> None:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test failed to release the probe")

        self.probe.side_effect = blocked_probe
        thread = Thread(target=self.scheduler.run_once)
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.now += MONITOR_INTERVAL_SECONDS + MONITOR_COMPLETION_GRACE_SECONDS
            self.assertTrue(self.scheduler.watch_once())
            self.assertFalse(self.make_scheduler().run_once())
            self.assertEqual(self.probe.call_count, 1)
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_restart_reports_gap_before_first_probe(self) -> None:
        self.scheduler.run_once()
        self.now += 3 * MONITOR_INTERVAL_SECONDS + MONITOR_COMPLETION_GRACE_SECONDS
        scheduler = self.make_scheduler()
        order: list[str] = []
        scheduler.report_cadence = lambda missed, _at: order.append("missed" if missed else "ok")
        scheduler.run_monitor = lambda: order.append("probe")
        with patch.object(scheduler, "_loop", return_value=None):
            scheduler.start()
            scheduler.stop()
        self.assertEqual(order, ["missed"])
        scheduler.run_once()
        self.assertEqual(order[-1], "probe")


class MonitorCadenceIncidentTests(unittest.TestCase):
    def test_cadence_alert_does_not_replace_site_incident_and_uses_existing_policy(self) -> None:
        profile = _profile()
        store = _Store((profile,))
        policy = PublicIngressNotificationPolicyRecord(
            policy_id="existing",
            product=profile.product,
            context=profile.lanes[0].context,
            instance=profile.lanes[0].instance,
            check_name=profile.lanes[0].health_monitoring.checks[0].name,
            check_kind="public_http",
            destinations=(
                PublicIngressNotificationDestination(
                    destination_id="existing-discord",
                    kind="discord",
                    discord_webhook_secret="existing-secret",
                ),
            ),
            created_at="2026-10-04T12:00:00Z",
            updated_at="2026-10-04T12:00:00Z",
        )
        store.notification_policies.append(policy)
        sender = Mock()
        drivers = PublicIngressNotificationDriverSet(
            secret_resolver=lambda _name: "https://discord.com/api/webhooks/test/test",
            discord_sender=sender,
        )

        def failed_get(_url: str, _timeout: int) -> HttpObservation:
            return HttpObservation(503, "https://example.test", 0)

        run_public_ingress_monitor_once(
            record_store=store, checked_at="2026-10-04T12:00:00Z", http_get=failed_get, notify=False
        )
        record_monitor_cadence(
            record_store=store,
            missed=True,
            observed_at="2026-10-04T12:05:00Z",
            notification_drivers=drivers,
        )
        open_incidents = store.list_public_ingress_incident_records(status="open")
        self.assertEqual(len(open_incidents), 2)
        cadence = next(item for item in open_incidents if item.failure_code == "monitor_run_missed")
        self.assertTrue(policy.matches(cadence))
        self.assertEqual(sender.call_count, 1)
        self.assertFalse(policy.model_copy(update={"product": "another-site"}).matches(cadence))
        self.assertFalse(policy.model_copy(update={"check_name": "another-check"}).matches(cadence))
        run_public_ingress_monitor_once(
            record_store=store, checked_at="2026-10-04T12:06:00Z", http_get=failed_get, notify=False
        )
        self.assertEqual(len(store.list_public_ingress_incident_records(status="open")), 2)
        record_monitor_cadence(
            record_store=store,
            missed=False,
            observed_at="2026-10-04T12:07:00Z",
            notification_drivers=drivers,
        )
        remaining = store.list_public_ingress_incident_records(status="open")
        self.assertEqual(len(remaining), 1)
        self.assertNotEqual(remaining[0].failure_code, "monitor_run_missed")
        self.assertEqual(sender.call_count, 2)
