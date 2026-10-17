from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Barrier, Event, Thread
import unittest
from unittest.mock import Mock, patch
from pydantic import JsonValue

from control_plane.health_monitor_scheduler import (
    HealthMonitorScheduler,
    MONITOR_COMPLETION_GRACE_SECONDS,
    MONITOR_INTERVAL_SECONDS,
    MONITOR_POLL_SECONDS,
    MONITOR_LEASE_SECONDS,
    MONITOR_RECEIPT_FINGERPRINT,
    _SCOPE,
    _ROUTE,
    monitor_lane_key,
    monitor_slot,
)
from control_plane.health_monitor_status import read_health_monitor_status
from control_plane.lane_runtime_verification import monitor_observation_provenance
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.provider_operations import _ReservationHeartbeat, DurableProviderOperationStore
from control_plane.contracts.public_ingress_monitoring import (
    health_monitor_freshness_seconds,
    health_monitor_interval_seconds,
    PublicIngressNotificationDestination,
    PublicIngressNotificationPolicyRecord,
)
from control_plane.contracts.runtime_environment_record import RuntimeEnvironmentRecord
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.public_ingress_monitor import (
    HttpObservation,
    MonitorRunCancelled,
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
        self.database_url = _sqlite_database_url(Path(self.directory.name) / "records.sqlite3")
        self.store = PostgresRecordStore(database_url=self.database_url)
        self.addCleanup(self.store.close)
        self.store.ensure_schema()
        self.store.write_product_profile_record(_profile())
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

    def test_production_and_testing_have_separate_due_slots(self) -> None:
        profile = _profile()
        testing = profile.lanes[0].model_copy(update={"instance": "testing"})
        self.store.write_product_profile_record(
            profile.model_copy(update={"lanes": (*profile.lanes, testing)})
        )
        self.now = monitor_slot(self.now, "testing") * health_monitor_interval_seconds("testing")
        self.assertTrue(self.scheduler.run_once())
        self.assertTrue(self.scheduler.run_once("testing"))
        self.now += health_monitor_interval_seconds("prod")
        self.assertTrue(self.scheduler.run_once())
        self.assertFalse(self.scheduler.run_once("testing"))
        self.now += health_monitor_interval_seconds("testing") - health_monitor_interval_seconds(
            "prod"
        )
        self.assertTrue(self.scheduler.run_once("testing"))
        self.assertEqual(
            self.probe.call_args.args[0], frozenset({("example-site", "example-site", "testing")})
        )

    def test_steady_probe_budget_has_no_extra_runs_between_slots(self) -> None:
        profile = _profile()
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": (
                        *profile.lanes,
                        profile.lanes[0].model_copy(update={"instance": "testing"}),
                    )
                }
            )
        )
        testing_interval = health_monitor_interval_seconds("testing")
        horizon = 2 * testing_interval
        for production_interval in (health_monitor_interval_seconds("prod"), 45):
            with (
                self.subTest(production_interval=production_interval),
                patch(
                    "control_plane.contracts.public_ingress_monitoring.PUBLIC_INGRESS_MONITOR_INTERVAL_SECONDS",
                    production_interval,
                ),
            ):
                self.now = (monitor_slot(self.now, "testing") + 1) * testing_interval
                start = self.now
                self.probe.reset_mock()
                poll_offsets = range(0, horizon, MONITOR_POLL_SECONDS)
                for offset in poll_offsets:
                    self.now = start + offset
                    for instance in ("prod", "testing"):
                        self.scheduler.run_once(instance)
                        self.assertFalse(self.make_scheduler().run_once(instance))
                checked = [lane for call in self.probe.call_args_list for lane in call.args[0]]
                for instance in ("prod", "testing"):
                    interval = health_monitor_interval_seconds(instance)
                    visited_slots = {int((start + offset) // interval) for offset in poll_offsets}
                    self.assertEqual(
                        sum(lane[2] == instance for lane in checked), len(visited_slots)
                    )

    def test_setting_change_checks_only_changed_lane_and_survives_restart(self) -> None:
        other = _profile().model_copy(
            update={
                "product": "other-site",
                "lanes": (_profile().lanes[0].model_copy(update={"context": "other-site"}),),
            }
        )
        self.store.write_product_profile_record(other)
        self.scheduler.run_once()
        self.now += 1
        self.store.write_runtime_environment_record(
            RuntimeEnvironmentRecord(
                scope="instance",
                context="example-site",
                instance="prod",
                env={"MODE": "updated"},
                updated_at=datetime.fromtimestamp(self.now, timezone.utc).isoformat(),
            )
        )
        self.assertTrue(self.scheduler.run_once())
        self.assertEqual(
            self.probe.call_args.args[0], frozenset({("example-site", "example-site", "prod")})
        )
        self.assertFalse(self.make_scheduler().run_once())
        status = read_health_monitor_status(self.store, now=self.now)
        self.assertEqual([change.product for change in status.lane_changes], ["example-site"])
        self.assertIsNotNone(
            self.scheduler._read(
                "change:" + monitor_lane_key(("example-site", "example-site", "prod"))
            )
        )

    def test_change_during_probe_is_not_acknowledged_as_checked(self) -> None:
        profile = _profile()

        def change_during_probe(_lanes: object) -> None:
            self.store.write_product_profile_record(
                profile.model_copy(update={"display_name": "Changed"})
            )

        self.probe.side_effect = change_during_probe
        self.scheduler.run_once()
        self.probe.side_effect = None
        self.assertTrue(self.scheduler.run_once())
        self.assertFalse(self.scheduler.run_once())

    def test_committed_authority_wakes_but_receipts_and_rollback_do_not(self) -> None:
        wake = Mock()
        unsubscribe = self.store.subscribe_health_monitor_changes(wake)
        self.addCleanup(unsubscribe)
        self.scheduler._write_completion("fixture", self.now)
        wake.assert_not_called()
        with self.store._session_factory() as session:
            from control_plane.storage.postgres import LaunchplaneProductProfileRow

            row = session.get(LaunchplaneProductProfileRow, "example-site")
            assert row is not None
            row.display_name = "Rolled back"
            session.flush()
            wake.assert_not_called()
            session.rollback()
            session.commit()
            wake.assert_not_called()
        self.store.write_product_profile_record(
            _profile().model_copy(update={"display_name": "Committed"})
        )
        wake.assert_called_once()

    def test_committed_change_wakes_worker_without_waiting_for_poll(self) -> None:
        completed, changed = Event(), Event()
        original_write = self.scheduler._write_completion
        scheduled_key = str(monitor_slot(self.now))

        def track_completion(
            key: str, now: float, *, payload: dict[str, JsonValue] | None = None
        ) -> None:
            original_write(key, now, payload=payload)
            if key == scheduled_key:
                completed.set()

        def probe(lanes: object) -> None:
            if completed.is_set():
                changed.set()

        self.scheduler.run_monitor = probe
        with (
            patch.object(self.scheduler, "_write_completion", side_effect=track_completion),
            patch("control_plane.health_monitor_scheduler.MONITOR_POLL_SECONDS", 3600),
        ):
            self.scheduler.start()
            try:
                self.assertTrue(completed.wait(5))
                self.store.write_product_profile_record(
                    _profile().model_copy(update={"display_name": "Wake now"})
                )
                self.assertTrue(changed.wait(5))
            finally:
                self.scheduler.stop()

    def test_savepoint_wakes_only_on_outer_commit_and_preserves_outer_changes(self) -> None:
        from control_plane.storage.postgres import LaunchplaneProductProfileRow

        wake = Mock()
        unsubscribe = self.store.subscribe_health_monitor_changes(wake)
        self.addCleanup(unsubscribe)
        for commit_nested, commit_outer, outer_write in (
            (True, True, False),
            (True, False, False),
            (False, True, False),
            (False, True, True),
        ):
            with self.subTest(
                commit_nested=commit_nested, commit_outer=commit_outer, outer_write=outer_write
            ):
                wake.reset_mock()
                with self.store._session_factory() as session:
                    row = session.get(LaunchplaneProductProfileRow, "example-site")
                    assert row is not None
                    if outer_write:
                        row.display_name = "outer write"
                        session.flush()
                    nested = session.begin_nested()
                    row.display_name = "nested write"
                    session.flush()
                    if commit_nested:
                        nested.commit()
                    else:
                        nested.rollback()
                    wake.assert_not_called()
                    if commit_outer:
                        session.commit()
                    else:
                        session.rollback()
                self.assertEqual(
                    wake.call_count, int(commit_outer and (commit_nested or outer_write))
                )

    def test_watchdog_does_not_depend_on_config_digest_reads(self) -> None:
        self.now += MONITOR_COMPLETION_GRACE_SECONDS
        with patch.object(
            self.store,
            "health_monitor_authority_revisions",
            side_effect=ValueError("invalid settings fixture"),
        ):
            self.assertTrue(self.scheduler.watch_once())
            self.alert.assert_called_once()
            with self.assertRaises(ValueError):
                self.scheduler.run_once()

    def test_another_replica_recovers_a_dead_workers_expired_lease(self) -> None:
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
                request_fingerprint=MONITOR_RECEIPT_FINGERPRINT,
                lease_owner="dead-worker-fixture",
                lease_seconds=MONITOR_LEASE_SECONDS,
            )
            self.assertEqual(claim.status, "acquired")
            peer = self.make_scheduler()
            self.assertFalse(peer.run_once())
            self.now += MONITOR_LEASE_SECONDS + 1
            self.assertTrue(peer.run_once())
        self.probe.assert_called_once()

    def test_service_probe_path_checks_lanes_in_parallel(self) -> None:
        first = _profile().model_copy(
            update={
                "lanes": (
                    _profile()
                    .lanes[0]
                    .model_copy(
                        update={"base_url": "", "health_url": "https://first.test/healthz"}
                    ),
                )
            }
        )
        second = _profile().model_copy(
            update={
                "product": "other-site",
                "lanes": (
                    _profile()
                    .lanes[0]
                    .model_copy(
                        update={
                            "context": "other-site",
                            "base_url": "",
                            "health_url": "https://second.test/healthz",
                        }
                    ),
                ),
            }
        )
        self.store.write_product_profile_record(first)
        self.store.write_product_profile_record(second)
        overlap = Barrier(2)

        def healthy(url: str, _timeout: int) -> HttpObservation:
            overlap.wait(5)
            return HttpObservation(status_code=200, final_url=url, redirect_count=0)

        lanes = frozenset(
            (profile.product, lane.context, lane.instance)
            for profile in (first, second)
            for lane in profile.lanes
        )
        with (
            patch(
                "control_plane.workflows.public_ingress_monitor.fetch_public_ingress_url",
                side_effect=healthy,
            ),
            patch(
                "control_plane.health_monitor_scheduler.public_ingress_notification_drivers",
                return_value=None,
            ),
        ):
            self.scheduler._run_monitor(lanes)
        for product in (first.product, second.product):
            self.assertEqual(
                self.store.list_public_ingress_observation_records(product=product)[0].status,
                "pass",
            )

    def test_testing_probe_does_not_hold_production_lease(self) -> None:
        profile = _profile()
        self.store.write_product_profile_record(
            profile.model_copy(
                update={
                    "lanes": (
                        *profile.lanes,
                        profile.lanes[0].model_copy(update={"instance": "testing"}),
                    )
                }
            )
        )
        entered, release = Event(), Event()

        def slow_testing(lanes: frozenset[tuple[str, str, str]]) -> None:
            if any(lane[2] == "testing" for lane in lanes):
                entered.set()
                if not release.wait(5):
                    raise TimeoutError("test did not release testing probe")

        self.scheduler.run_monitor = slow_testing
        thread = Thread(target=lambda: self.scheduler.run_once("testing"))
        thread.start()
        try:
            self.assertTrue(entered.wait(5))
            self.assertTrue(self.scheduler.run_once())
        finally:
            release.set()
            thread.join(5)
        self.assertFalse(thread.is_alive())

    def test_receipts_survive_restart_and_distinguish_miss_from_recovery(self) -> None:
        self.now += MONITOR_INTERVAL_SECONDS + MONITOR_COMPLETION_GRACE_SECONDS
        self.scheduler.watch_once()
        self.now += 1
        self.scheduler.run_once()
        self.scheduler.watch_once()
        reader = PostgresRecordStore(database_url=self.database_url)
        self.addCleanup(reader.close)
        with (
            patch.object(reader, "reserve_mutation", side_effect=AssertionError("read wrote")),
            patch.object(
                reader, "write_idempotency_record", side_effect=AssertionError("read wrote")
            ),
        ):
            evidence = read_health_monitor_status(reader, now=self.now, slots=2)
        current, prior = evidence.slots
        self.assertIsNotNone(current.completion)
        self.assertTrue(current.completed_on_time)
        self.assertIsNone(prior.completion)
        self.assertIsNone(prior.completed_on_time)
        self.assertIsNotNone(prior.missed_report)
        self.assertIsNotNone(prior.healthy_report)
        self.assertIsNotNone(evidence.activation)
        self.probe.assert_called_once()

    def test_historical_windows_preserve_slots_without_inventing_completion(self) -> None:
        self.scheduler.run_once()
        completed_slot = int(self.now // MONITOR_INTERVAL_SECONDS)
        self.now += 3 * MONITOR_INTERVAL_SECONDS
        recent = read_health_monitor_status(self.store, now=self.now, slots=3)
        self.assertTrue(all(slot.completion is None for slot in recent.slots))
        older = read_health_monitor_status(
            self.store, now=self.now, slots=1, end_slot=recent.next_end_slot
        )
        self.assertEqual(older.slots[0].slot, completed_slot)
        self.assertTrue(older.slots[0].completed_on_time)

    def test_paging_stops_at_activation_or_when_activation_is_unknown(self) -> None:
        activated_slot = monitor_slot(self.now)
        status = read_health_monitor_status(self.store, now=self.now, slots=48)
        self.assertEqual([slot.slot for slot in status.slots], [activated_slot])
        self.assertIsNone(status.next_end_slot)
        before_activation = read_health_monitor_status(
            self.store, now=self.now, end_slot=activated_slot - 1
        )
        self.assertEqual(before_activation.slots, [])
        self.assertIsNone(before_activation.next_end_slot)
        with patch.object(self.store, "read_idempotency_record", return_value=None):
            unknown = read_health_monitor_status(self.store, now=self.now)
        self.assertIsNone(unknown.activation)
        self.assertIsNone(unknown.next_end_slot)
        self.assertTrue(all(slot.completion is None for slot in unknown.slots))

    def test_receipt_read_refuses_invalid_ranges_and_unrelated_fingerprint(self) -> None:
        for slots, end_slot in ((0, None), (49, None), (1, -1), (1, monitor_slot(self.now) + 1)):
            with self.subTest(slots=slots, end_slot=end_slot), self.assertRaises(ValueError):
                read_health_monitor_status(self.store, now=self.now, slots=slots, end_slot=end_slot)
        self.scheduler.run_once()
        record = self.scheduler._read(str(monitor_slot(self.now)))
        assert record is not None
        self.store.write_idempotency_record(
            record.model_copy(update={"request_fingerprint": "unrelated"})
        )
        with self.assertRaises(ValueError):
            read_health_monitor_status(self.store, now=self.now)

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

        def blocked_probe(_lanes: object) -> None:
            entered.set()
            if not release.wait(5):
                raise TimeoutError("test failed to release the probe")

        self.probe.side_effect = blocked_probe
        results: list[bool] = []
        thread = Thread(target=lambda: results.append(self.scheduler.run_once()))
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
        self.assertEqual(results, [True])

    def test_restart_reports_gap_before_first_probe(self) -> None:
        self.scheduler.run_once()
        self.now += 3 * MONITOR_INTERVAL_SECONDS + MONITOR_COMPLETION_GRACE_SECONDS
        scheduler = self.make_scheduler()
        order: list[str] = []
        scheduler.report_cadence = lambda missed, _at, _lanes: order.append(
            "missed" if missed else "ok"
        )
        scheduler.run_monitor = lambda _lanes: order.append("probe")
        with patch.object(scheduler, "_loop", return_value=None):
            scheduler.start()
            self.assertEqual(order, ["missed"])
            scheduler.run_once()
            scheduler.stop()
        self.assertEqual(order[-1], "probe")

    def test_replicas_report_miss_and_recovery_once(self) -> None:
        peer = self.make_scheduler()
        self.now += MONITOR_COMPLETION_GRACE_SECONDS
        self.assertTrue(self.scheduler.watch_once())
        self.assertTrue(peer.watch_once())
        self.assertEqual(self.alert.call_count, 1)
        self.scheduler.run_once()
        self.assertFalse(peer.watch_once())
        self.assertFalse(self.scheduler.watch_once())
        self.assertEqual(self.alert.call_count, 2)

    def test_shutdown_cancels_unfinished_run_without_completing_slot(self) -> None:
        entered = Event()

        def until_shutdown(_lanes: object) -> None:
            entered.set()
            if not self.scheduler._stop.wait(5):
                raise TimeoutError("scheduler did not signal shutdown")
            raise MonitorRunCancelled()

        self.scheduler.run_monitor = until_shutdown
        self.scheduler.start()
        threads = list(self.scheduler._threads)
        try:
            self.assertTrue(entered.wait(5))
        finally:
            self.scheduler.stop()
        self.assertIsNone(self.scheduler._read(str(int(self.now // MONITOR_INTERVAL_SECONDS))))
        self.assertIsNone(self.scheduler._read("active"))
        self.assertFalse(any(thread.is_alive() for thread in threads))

    def test_unexpected_callback_failure_does_not_end_worker(self) -> None:
        calls: list[str] = []

        def unexpected_failure_then_stop() -> None:
            calls.append("attempt")
            if len(calls) == 1:
                raise LookupError("unexpected callback failure")
            self.scheduler._stop.set()

        with (
            patch("control_plane.health_monitor_scheduler.MONITOR_POLL_SECONDS", 0),
            self.assertLogs("control_plane.health_monitor_scheduler", level="ERROR"),
        ):
            self.scheduler._loop(unexpected_failure_then_stop)
        self.assertEqual(len(calls), 2)

    def test_slow_alert_delivery_cannot_block_probe_lease_renewal(self) -> None:
        probing = Event()
        reporting = Event()
        renewed = Event()
        release = Event()

        def hold_probe(_lanes: object) -> None:
            probing.set()
            if not release.wait(5):
                raise TimeoutError("probe was not released")

        def hold_alert(_missed: bool, _at: str, _lanes: object) -> None:
            reporting.set()
            if not release.wait(5):
                raise TimeoutError("alert was not released")

        def heartbeat_factory(
            *,
            store: DurableProviderOperationStore,
            reservation: LaunchplaneIdempotencyRecord,
            lease_seconds: int,
            interval_seconds: float,
        ) -> _ReservationHeartbeat:
            return _ReservationHeartbeat(
                store=store,
                reservation=reservation,
                lease_seconds=lease_seconds,
                interval_seconds=min(interval_seconds, 0.01),
            )

        original_renew = self.store.renew_mutation_reservation

        def track_renewal(
            *, reservation: LaunchplaneIdempotencyRecord, lease_seconds: int = 300
        ) -> object:
            result = original_renew(reservation=reservation, lease_seconds=lease_seconds)
            if (
                reporting.is_set()
                and reservation.idempotency_key == "active"
                and result.status == "updated"
            ):
                renewed.set()
            return result

        self.scheduler.run_monitor = hold_probe
        self.scheduler.report_cadence = hold_alert
        results: list[bool] = []
        runner = Thread(target=lambda: results.append(self.scheduler.run_once()))
        watcher = Thread(target=self.scheduler.watch_once)
        with (
            patch(
                "control_plane.health_monitor_scheduler._ReservationHeartbeat", heartbeat_factory
            ),
            patch.object(self.store, "renew_mutation_reservation", side_effect=track_renewal),
        ):
            runner.start()
            try:
                self.assertTrue(probing.wait(5))
                self.now += MONITOR_COMPLETION_GRACE_SECONDS
                watcher.start()
                self.assertTrue(reporting.wait(5))
                self.assertTrue(renewed.wait(5))
                self.assertFalse(self.make_scheduler().run_once())
            finally:
                release.set()
                runner.join(5)
                if watcher.ident is not None:
                    watcher.join(5)
        self.assertFalse(runner.is_alive())
        self.assertFalse(watcher.is_alive())
        self.assertEqual(results, [True])


class MonitorCadenceIncidentTests(unittest.TestCase):
    def test_parallel_probe_batch_has_independent_requests_and_stable_result_order(self) -> None:
        first = _profile(
            lane=_profile()
            .lanes[0]
            .model_copy(update={"base_url": "", "health_url": "https://first.test/healthz"})
        )
        second = _profile(
            lane=_profile()
            .lanes[0]
            .model_copy(
                update={
                    "context": "other-site",
                    "base_url": "",
                    "health_url": "https://second.test/healthz",
                }
            )
        ).model_copy(update={"product": "other-site"})
        profiles = (first, second)
        store = _Store(profiles)
        overlap = Barrier(2)

        def healthy(url: str, _timeout: int) -> HttpObservation:
            overlap.wait(5)
            return HttpObservation(status_code=200, final_url=url, redirect_count=0)

        result = run_public_ingress_monitor_once(
            record_store=store, http_get=healthy, notify=False, max_concurrent_probes=2
        )
        self.assertEqual(result.pass_count, len(profiles))
        self.assertEqual(
            [record.product for record in result.records], [first.product, second.product]
        )

    def test_healthy_observation_stays_fresh_while_next_cadence_publishes(self) -> None:
        store = _Store((_profile(),))
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:20:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200, final_url=url, redirect_count=0
            ),
            notify=False,
        )
        observed = store.records[-1]
        observed_at = datetime.fromisoformat(observed.observed_at.replace("Z", "+00:00"))
        for instance in ("prod", "testing"):
            with self.subTest(instance=instance):
                observation = observed.model_copy(update={"instance": instance})
                # A normal next run can publish three seconds after its nominal slot.
                next_publication = observed_at + timedelta(
                    seconds=health_monitor_interval_seconds(instance) + 3
                )
                self.assertEqual(
                    monitor_observation_provenance(
                        observation, now=next_publication
                    ).freshness_status,
                    "verified",
                )

    def test_observation_freshness_uses_the_observations_lane_cadence(self) -> None:
        store = _Store((_profile(),))
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:20:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200, final_url=url, redirect_count=0
            ),
            notify=False,
        )
        observed = store.records[-1]
        observed_at = datetime.fromisoformat(observed.observed_at.replace("Z", "+00:00"))
        for instance in ("prod", "testing"):
            with self.subTest(instance=instance):
                lane_observation = observed.model_copy(update={"instance": instance})
                deadline = observed_at + timedelta(
                    seconds=health_monitor_freshness_seconds(instance)
                )
                self.assertEqual(
                    monitor_observation_provenance(lane_observation, now=deadline).freshness_status,
                    "verified",
                )
                self.assertEqual(
                    monitor_observation_provenance(
                        lane_observation, now=deadline + timedelta(seconds=1)
                    ).freshness_status,
                    "stale",
                )

    def test_selected_lane_never_reconciles_another_lanes_incident(self) -> None:
        profile = _profile()
        testing = profile.lanes[0].model_copy(update={"instance": "testing"})
        store = _Store((profile.model_copy(update={"lanes": (*profile.lanes, testing)}),))
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:20:00Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=500, final_url=url, redirect_count=0
            ),
            notify=False,
        )
        testing_incident = next(
            incident for incident in store.incidents if incident.instance == "testing"
        )
        run_public_ingress_monitor_once(
            record_store=store,
            checked_at="2026-05-29T12:20:01Z",
            http_get=lambda url, _timeout: HttpObservation(
                status_code=200, final_url=url, redirect_count=0
            ),
            notify=False,
            lane_keys=frozenset({("example-site", "example-site", "prod")}),
        )
        self.assertEqual(
            next(
                incident
                for incident in store.incidents
                if incident.incident_id == testing_incident.incident_id
            ).status,
            "open",
        )
        self.assertEqual(
            [
                record.instance
                for record in store.records
                if record.observed_at == "2026-05-29T12:20:01Z"
            ],
            ["prod"],
        )

    def test_stopping_monitor_does_not_persist_partial_probe_batch(self) -> None:
        store = _Store((_profile(),))
        stopped = Event()

        def probe_then_stop(url: str, _timeout: int) -> HttpObservation:
            stopped.set()
            return HttpObservation(200, url, 0)

        with self.assertRaises(MonitorRunCancelled):
            run_public_ingress_monitor_once(
                record_store=store,
                http_get=probe_then_stop,
                should_stop=stopped.is_set,
            )
        self.assertEqual(store.records, [])

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
