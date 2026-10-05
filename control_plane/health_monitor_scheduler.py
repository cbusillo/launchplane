"""Service-owned monitor slots and an independent completion watchdog."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime, timezone
import logging
from threading import Event, Thread
import time
import uuid

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
    complete_launchplane_mutation_reservation,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.provider_operations import _ReservationHeartbeat
from control_plane.workflows.public_ingress_monitor import (
    public_ingress_notification_drivers,
    record_monitor_cadence,
    run_public_ingress_monitor_once,
)

MONITOR_INTERVAL_SECONDS = 30 * 60
MONITOR_COMPLETION_GRACE_SECONDS = 5 * 60
MONITOR_POLL_SECONDS = 30
_SCOPE = "launchplane:health-monitor-scheduler"
_ROUTE = "service:health-monitor-scheduler"
_LOGGER = logging.getLogger(__name__)


def monitor_slot(now: float) -> int:
    return int(now // MONITOR_INTERVAL_SECONDS)


def _timestamp(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


class HealthMonitorScheduler:
    def __init__(
        self,
        store: PostgresRecordStore,
        *,
        clock: Callable[[], float] = time.time,
        run_monitor: Callable[[], object] | None = None,
        report_cadence: Callable[[bool, str], object] | None = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.run_monitor = run_monitor or self._run_monitor
        self.report_cadence = report_cadence or self._report_cadence
        self._owner = str(uuid.uuid4())
        self._stop = Event()
        self._threads: list[Thread] = []
        self._last_report: tuple[int, bool] | None = None

    def _read(self, key: str) -> LaunchplaneIdempotencyRecord | None:
        return self.store.read_idempotency_record(
            scope=_SCOPE, route_path=_ROUTE, idempotency_key=key
        )

    def _write_completion(self, key: str, now: float) -> None:
        self.store.write_idempotency_record(
            LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_mutation_reservation_id(
                    scope=_SCOPE, route_path=_ROUTE, idempotency_key=key
                ),
                scope=_SCOPE,
                route_path=_ROUTE,
                idempotency_key=key,
                request_fingerprint="health-monitor-scheduler-v1",
                response_status_code=200,
                response_trace_id=f"health-monitor:{key}",
                recorded_at=_timestamp(now),
                response_payload={"completed_at": _timestamp(now)},
            )
        )

    def _ensure_enabled(self) -> None:
        # A durable start marker distinguishes first activation from an outage.
        result = self.store.reserve_mutation(
            scope=_SCOPE,
            route_path=_ROUTE,
            idempotency_key="enabled",
            request_fingerprint="health-monitor-scheduler-v1",
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
        self._stop.clear()
        # Watch before probing: restart after an outage must report the missed slot.
        self.watch_once()
        self._threads = [
            Thread(target=self._loop, args=(self.run_once,), name="health-monitor", daemon=True),
            Thread(
                target=self._loop, args=(self.watch_once,), name="health-monitor-watch", daemon=True
            ),
        ]
        for thread in self._threads:
            thread.start()

    def stop(self) -> None:
        self._stop.set()
        for thread in self._threads:
            thread.join()
        self._threads.clear()

    def _loop(self, action: Callable[[], object]) -> None:
        while not self._stop.is_set():
            try:
                action()
            except Exception:  # noqa: BLE001 - keep the watchdog and next attempt alive.
                _LOGGER.exception("Health monitor scheduler pass failed")
            self._stop.wait(MONITOR_POLL_SECONDS)

    def run_once(self) -> bool:
        now = self.clock()
        key = str(monitor_slot(now))
        if self._read(key) is not None:
            return False
        claim = self.store.reserve_mutation(
            scope=_SCOPE,
            route_path=_ROUTE,
            idempotency_key="active",
            request_fingerprint="health-monitor-scheduler-v1",
            lease_owner=self._owner,
            lease_seconds=MONITOR_POLL_SECONDS * 4,
        )
        if claim.status != "acquired":
            return False
        heartbeat = _ReservationHeartbeat(
            store=self.store,
            reservation=claim.record,
            lease_seconds=MONITOR_POLL_SECONDS * 4,
            interval_seconds=MONITOR_POLL_SECONDS,
        )
        heartbeat.start()
        try:
            # Another replica may have finished this slot while we waited for the lease.
            if self._read(key) is not None:
                return False
            self.run_monitor()
            heartbeat.assert_current()
            _reservation, failure = heartbeat.stop()
            if failure:
                raise RuntimeError("Health monitor completion lease is not held")
            self._write_completion(key, self.clock())
            _LOGGER.info("Health monitor slot %s completed", key)
            return True
        finally:
            reservation, _failure = heartbeat.stop()
            self.store.release_mutation_reservation(reservation=reservation)

    def watch_once(self) -> bool:
        now = self.clock()
        enabled = self._read("enabled")
        if enabled is None or enabled.state != "completed":
            # A replica may have stopped while recording first activation.
            self._ensure_enabled()
            enabled = self._read("enabled")
            if enabled is None or enabled.state != "completed":
                return False
        due_slot = monitor_slot(now - MONITOR_COMPLETION_GRACE_SECONDS)
        enabled_at = datetime.fromisoformat(enabled.created_at.replace("Z", "+00:00")).timestamp()
        if now < enabled_at + MONITOR_COMPLETION_GRACE_SECONDS:
            return False
        enabled_slot = monitor_slot(enabled_at)
        if due_slot < enabled_slot:
            return False
        completed = self._read(str(due_slot))
        missed = completed is None
        report_key = (due_slot, missed)
        if self._last_report != report_key:
            self.report_cadence(missed, _timestamp(now))
            self._last_report = report_key
        if missed:
            _LOGGER.error("Health monitor slot %s missed its completion deadline", due_slot)
        return missed

    def _run_monitor(self) -> object:
        return run_public_ingress_monitor_once(
            record_store=self.store,
            notification_drivers=public_ingress_notification_drivers(record_store=self.store),
            runtime_identity_confirmation_delay_seconds=30,
        )

    def _report_cadence(self, missed: bool, observed_at: str) -> None:
        record_monitor_cadence(
            record_store=self.store,
            missed=missed,
            observed_at=observed_at,
            notification_drivers=public_ingress_notification_drivers(record_store=self.store),
        )
