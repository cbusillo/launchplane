"""Service-owned monitor slots and an independent completion watchdog."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from functools import partial
import hashlib
import logging
from threading import Event, Thread
import time
import uuid
from pydantic import JsonValue

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
    complete_launchplane_mutation_reservation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.public_ingress_monitoring import (
    PUBLIC_INGRESS_MONITOR_INTERVAL_SECONDS,
    health_monitor_interval_seconds,
)
from control_plane.provider_operations import _ReservationHeartbeat
from control_plane.workflows.public_ingress_monitor import (
    MonitorRunCancelled,
    public_ingress_notification_drivers,
    record_monitor_cadence,
    run_public_ingress_monitor_once,
)

MONITOR_INTERVAL_SECONDS = PUBLIC_INGRESS_MONITOR_INTERVAL_SECONDS
MONITOR_COMPLETION_GRACE_SECONDS = MONITOR_INTERVAL_SECONDS
MONITOR_POLL_SECONDS = 5
MONITOR_LEASE_SECONDS = 30
MONITOR_MAX_CONCURRENT_PROBES = 16
MONITOR_RECEIPT_FINGERPRINT = "health-monitor-scheduler-v2"
# New epoch: half-hour receipts and activation cannot prove a one-minute schedule.
_SCOPE = "launchplane:health-monitor-scheduler:v2"
_ROUTE = "service:health-monitor-scheduler"
_LOGGER = logging.getLogger(__name__)
LaneKey = tuple[str, str, str]


def monitor_slot(now: float, instance: str = "prod") -> int:
    return int(now // health_monitor_interval_seconds(instance))


def monitor_key(key: str, instance: str = "prod") -> str:
    return f"testing:{key}" if instance == "testing" else key


def monitor_lane_key(lane: LaneKey) -> str:
    return "lane:" + hashlib.sha256(repr(lane).encode()).hexdigest()


def _timestamp(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


class HealthMonitorScheduler:
    def __init__(
        self,
        store: PostgresRecordStore,
        *,
        clock: Callable[[], float] = time.time,
        run_monitor: Callable[[frozenset[LaneKey]], object] | None = None,
        report_cadence: Callable[[bool, str, frozenset[LaneKey]], object] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.run_monitor = run_monitor or self._run_monitor
        self.report_cadence = report_cadence or self._report_cadence
        self._owner = str(uuid.uuid4())
        self._stop = Event()
        self._wake = {instance: Event() for instance in ("prod", "testing")}
        self._unsubscribe: Callable[[], None] | None = None
        self._threads: list[Thread] = []
        self._last_report: dict[str, tuple[int, bool]] = {}

    def _read(self, key: str) -> LaunchplaneIdempotencyRecord | None:
        return self.store.read_idempotency_record(
            scope=_SCOPE, route_path=_ROUTE, idempotency_key=key
        )

    def _write_completion(
        self, key: str, now: float, *, payload: dict[str, JsonValue] | None = None
    ) -> None:
        self.store.write_idempotency_record(
            LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_mutation_reservation_id(
                    scope=_SCOPE, route_path=_ROUTE, idempotency_key=key
                ),
                scope=_SCOPE,
                route_path=_ROUTE,
                idempotency_key=key,
                request_fingerprint=MONITOR_RECEIPT_FINGERPRINT,
                response_status_code=200,
                response_trace_id=f"health-monitor:{key}",
                recorded_at=_timestamp(now),
                response_payload={"completed_at": _timestamp(now), **(payload or {})},
            )
        )

    def _ensure_enabled(self, instance: str = "prod") -> None:
        # A durable start marker distinguishes first activation from an outage.
        result = self.store.reserve_mutation(
            scope=_SCOPE,
            route_path=_ROUTE,
            idempotency_key=monitor_key("enabled", instance),
            request_fingerprint=MONITOR_RECEIPT_FINGERPRINT,
            lease_owner=self._owner,
        )
        if result.status == "acquired":
            completion = self.store.complete_mutation_reservation(
                completion=complete_launchplane_mutation_reservation(
                    result.record,
                    response_status_code=200,
                    response_trace_id="health-monitor:enabled",
                    completed_at=_timestamp(self.clock()),
                    response_payload={},
                )
            )
            if completion.status != "completed":
                raise RuntimeError("Health monitor activation could not be recorded")

    def start(self) -> None:
        self._ensure_enabled()
        self._ensure_enabled("testing")
        self._stop.clear()
        self._unsubscribe = self.store.subscribe_health_monitor_changes(self.wake)
        # Watch before probing: restart after an outage must report the missed slot.
        self.watch_once()
        self.watch_once("testing")
        self._threads = []
        for instance in ("prod", "testing"):
            for name, action in (("probe", self.run_once), ("watch", self.watch_once)):
                self._threads.append(
                    Thread(
                        target=self._loop,
                        args=(
                            partial(action, instance),
                            self._wake[instance] if name == "probe" else None,
                        ),
                        name=f"health-monitor-{instance}-{name}",
                        daemon=True,
                    )
                )
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        self.wake()
        for thread in self._threads:
            thread.join()
        self._threads.clear()
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    def wake(self) -> None:
        for wake in self._wake.values():
            wake.set()

    def _loop(self, action: Callable[[], object], wake: Event | None = None) -> None:
        while not self._stop.is_set():
            if wake is not None:
                wake.clear()
            try:
                action()
            except MonitorRunCancelled:
                pass
            except Exception:  # noqa: BLE001 - keep the watchdog and next attempt alive.
                _LOGGER.exception("Health monitor scheduler pass failed")
            (wake or self._stop).wait(MONITOR_POLL_SECONDS)

    @contextmanager
    def _lease(self, key: str) -> Iterator[_ReservationHeartbeat | None]:
        claim = self.store.reserve_mutation(
            scope=_SCOPE,
            route_path=_ROUTE,
            idempotency_key=key,
            request_fingerprint=MONITOR_RECEIPT_FINGERPRINT,
            lease_owner=self._owner,
            lease_seconds=MONITOR_LEASE_SECONDS,
        )
        if claim.status != "acquired":
            yield None
            return
        heartbeat = _ReservationHeartbeat(
            store=self.store,
            reservation=claim.record,
            lease_seconds=MONITOR_LEASE_SECONDS,
            interval_seconds=10,
        )
        heartbeat.start()
        try:
            yield heartbeat
        finally:
            reservation, _failure = heartbeat.stop()
            self.store.release_mutation_reservation(reservation=reservation)

    def _revisions(self, instance: str) -> dict[LaneKey, str]:
        return {
            lane: revision
            for lane, revision in self.store.health_monitor_authority_revisions().items()
            if (lane[2] == "testing") == (instance == "testing")
        }

    def run_once(self, instance: str = "prod") -> bool:
        key = monitor_key(str(monitor_slot(self.clock(), instance)), instance)
        revisions = self._revisions(instance)
        if not revisions:
            return False
        if self._read(key) is not None and all(
            (receipt := self._read(monitor_lane_key(lane))) is not None
            and receipt.response_payload.get("authority_revision") == revision
            for lane, revision in revisions.items()
        ):
            return False
        with self._lease(monitor_key("active", instance)) as heartbeat:
            if heartbeat is None:
                return False
            # Read authority and receipts after claiming, so replicas cannot duplicate probes.
            revisions = self._revisions(instance)
            scheduled = self._read(key) is None
            selected = {
                lane: revision
                for lane, revision in revisions.items()
                if scheduled
                or (receipt := self._read(monitor_lane_key(lane))) is None
                or receipt.response_payload.get("authority_revision") != revision
            }
            if not selected:
                return False
            self.run_monitor(frozenset(selected))
            if self._stop.is_set():
                raise MonitorRunCancelled()
            heartbeat.assert_current()
            for lane, revision in selected.items():
                self._write_completion(
                    monitor_lane_key(lane),
                    self.clock(),
                    payload={
                        "authority_revision": revision,
                        "reason": "scheduled" if scheduled else "change",
                    },
                )
                if not scheduled:
                    self._write_completion("change:" + monitor_lane_key(lane), self.clock())
            if scheduled:
                self._write_completion(key, self.clock())
            _LOGGER.info(
                "Health monitor %s slot %s completed (%s, %s lanes)",
                instance,
                key,
                "scheduled" if scheduled else "change",
                len(selected),
            )
            return True

    def watch_once(self, instance: str = "prod") -> bool:
        now = self.clock()
        enabled = self._read(monitor_key("enabled", instance))
        if enabled is None or enabled.state != "completed":
            # A replica may have stopped while recording first activation.
            self._ensure_enabled(instance)
            enabled = self._read(monitor_key("enabled", instance))
            if enabled is None or enabled.state != "completed":
                return False
        grace = health_monitor_interval_seconds(instance)
        due_slot = monitor_slot(now - grace, instance)
        enabled_at = datetime.fromisoformat(enabled.created_at.replace("Z", "+00:00")).timestamp()
        if now < enabled_at + grace:
            return False
        enabled_slot = monitor_slot(enabled_at, instance)
        if due_slot < enabled_slot:
            return False

        def is_missed() -> bool:
            return (
                self._read(monitor_key(str(due_slot), instance)) is None
                and self._read(monitor_key(str(monitor_slot(self.clock(), instance)), instance))
                is None
            )

        missed = is_missed()
        if self._last_report.get(instance) == (due_slot, missed):
            return missed
        lanes = frozenset(
            (profile.product, lane.context, lane.instance)
            for profile in self.store.list_product_profile_records()
            if profile.is_active
            for lane in profile.lanes
            if (lane.instance == "testing") == (instance == "testing")
        )
        if not lanes:
            return False
        with self._lease(monitor_key("watch-active", instance)) as heartbeat:
            if heartbeat is None:
                return missed
            # Serialize missed and recovery reporting, and re-read after claiming.
            missed = is_missed()
            key = monitor_key(f"watch:{due_slot}:{missed}", instance)
            if self._read(key) is None:
                self.report_cadence(missed, _timestamp(self.clock()), lanes)
                if self._stop.is_set():
                    raise MonitorRunCancelled()
                heartbeat.assert_current()
                self._write_completion(key, self.clock())
            self._last_report[instance] = (due_slot, missed)
        if missed:
            _LOGGER.error("Health monitor slot %s missed its completion deadline", due_slot)
        return missed

    def _run_monitor(self, lanes: frozenset[LaneKey]) -> object:
        return run_public_ingress_monitor_once(
            record_store=self.store,
            notification_drivers=public_ingress_notification_drivers(record_store=self.store),
            runtime_identity_confirmation_delay_seconds=30,
            should_stop=self._stop.is_set,
            sleep=self._wait,
            lane_keys=lanes,
            max_concurrent_probes=MONITOR_MAX_CONCURRENT_PROBES,
        )

    def _report_cadence(self, missed: bool, observed_at: str, lanes: frozenset[LaneKey]) -> None:
        record_monitor_cadence(
            record_store=self.store,
            missed=missed,
            should_stop=self._stop.is_set,
            observed_at=observed_at,
            notification_drivers=public_ingress_notification_drivers(record_store=self.store),
            lane_keys=lanes,
        )

    def _wait(self, seconds: float) -> None:
        self._stop.wait(seconds)
