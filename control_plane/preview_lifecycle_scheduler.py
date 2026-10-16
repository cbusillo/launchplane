"""Service-owned daily preview lifecycle sweep; scheduled runs only ever report."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from control_plane.preview_inventory_evidence import PREVIEW_INVENTORY_STALE_AFTER
from control_plane.preview_inventory_collection import collect_preview_inventory
import logging
from pathlib import Path
from threading import Event, Thread
import time
import uuid

from pydantic import JsonValue

from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    build_launchplane_mutation_reservation_id,
)
from control_plane.preview_lifecycle_cleanup_routes import (
    PreviewLifecycleSweepCancelled,
    PreviewLifecycleSweepEnvelope,
    build_preview_lifecycle_sweep,
    require_preview_lifecycle_sweep_store,
)
from control_plane.provider_operations import _ReservationHeartbeat
from control_plane.storage.postgres import PostgresRecordStore

SWEEP_INTERVAL_SECONDS = 24 * 60 * 60
# Slots roll over at 05:17 UTC, the time the retired scheduled workflow ran.
SWEEP_SLOT_OFFSET_SECONDS = 5 * 60 * 60 + 17 * 60
SWEEP_POLL_SECONDS = 60
SWEEP_LEASE_SECONDS = 60
SWEEP_RECEIPT_FINGERPRINT = "preview-lifecycle-scheduler-v1"
_SCOPE = "launchplane:preview-lifecycle-scheduler:v1"
_ROUTE = "service:preview-lifecycle-scheduler"
_LOGGER = logging.getLogger(__name__)


def sweep_slot(now: float) -> int:
    return int((now - SWEEP_SLOT_OFFSET_SECONDS) // SWEEP_INTERVAL_SECONDS)


def _timestamp(now: float) -> str:
    return datetime.fromtimestamp(now, timezone.utc).isoformat().replace("+00:00", "Z")


def sweep_summary(result: dict[str, object]) -> dict[str, JsonValue]:
    """Keep the receipt bounded; per-product detail lives in the lifecycle records."""
    profiles = result.get("profiles")
    entries = profiles if isinstance(profiles, list) else []

    def count(entry: dict[str, object], field: str) -> int:
        value = entry.get(field)
        return len(value) if isinstance(value, (list, tuple)) else 0

    return {
        "status": str(result.get("status", "")),
        "apply": bool(result.get("apply")),
        "profiles": [
            {
                "product": str(entry.get("product", "")),
                "context": str(entry.get("context", "")),
                "status": str(entry.get("status", "")),
                "orphaned": count(entry, "orphaned_slugs"),
                "failed": count(entry, "failed_slugs"),
                "held_reservations": count(entry, "held_reservations"),
            }
            for entry in entries
            if isinstance(entry, dict)
        ],
    }


class _PreviewScheduler:
    scope = _SCOPE
    route = _ROUTE
    fingerprint = SWEEP_RECEIPT_FINGERPRINT
    interval = SWEEP_INTERVAL_SECONDS
    offset = SWEEP_SLOT_OFFSET_SECONDS

    def slot(self) -> int:
        return int((self.clock() - self.offset) // self.interval)

    def __init__(
        self,
        store: PostgresRecordStore,
        *,
        control_plane_root: Path,
        clock: Callable[[], float] = time.time,
        run_sweep: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        self.store = store
        self.control_plane_root = control_plane_root
        self.clock = clock
        self.run_sweep = run_sweep or self._run_sweep
        self._owner = str(uuid.uuid4())
        self._stop = Event()
        self._thread: Thread | None = None

    def start(self) -> None:
        self._stop.clear()
        self._thread = Thread(
            target=self._loop, name=self.route.removeprefix("service:"), daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
            self._thread = None

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:  # noqa: BLE001 - keep the next slot alive.
                _LOGGER.exception("Preview lifecycle sweep schedule pass failed")
            self._stop.wait(SWEEP_POLL_SECONDS)

    def _read(self, key: str) -> LaunchplaneIdempotencyRecord | None:
        return self.store.read_idempotency_record(
            scope=self.scope, route_path=self.route, idempotency_key=key
        )

    def _write_completion(self, key: str, payload: dict[str, JsonValue]) -> None:
        now = self.clock()
        self.store.write_idempotency_record(
            LaunchplaneIdempotencyRecord(
                record_id=build_launchplane_mutation_reservation_id(
                    scope=self.scope, route_path=self.route, idempotency_key=key
                ),
                scope=self.scope,
                route_path=self.route,
                idempotency_key=key,
                request_fingerprint=self.fingerprint,
                response_status_code=200,
                response_trace_id=f"preview-lifecycle-sweep:{key}",
                recorded_at=_timestamp(now),
                response_payload={"completed_at": _timestamp(now), **payload},
            )
        )

    @contextmanager
    def _lease(self) -> Iterator[_ReservationHeartbeat | None]:
        claim = self.store.reserve_mutation(
            scope=self.scope,
            route_path=self.route,
            idempotency_key="active",
            request_fingerprint=self.fingerprint,
            lease_owner=self._owner,
            lease_seconds=SWEEP_LEASE_SECONDS,
        )
        if claim.status != "acquired":
            yield None
            return
        heartbeat = _ReservationHeartbeat(
            store=self.store,
            reservation=claim.record,
            lease_seconds=SWEEP_LEASE_SECONDS,
            interval_seconds=20,
        )
        heartbeat.start()
        try:
            yield heartbeat
        finally:
            reservation, _failure = heartbeat.stop()
            self.store.release_mutation_reservation(reservation=reservation)

    def run_once(self) -> bool:
        key = str(self.slot())
        if self._read(key) is not None:
            return False
        with self._lease() as heartbeat:
            # Re-read after claiming, so replicas and restarts sweep once per slot.
            if heartbeat is None or self._read(key) is not None:
                return False
            try:
                summary = sweep_summary(self.run_sweep())
            except PreviewLifecycleSweepCancelled:
                # Stopping is not a result; the slot runs again after restart.
                return False
            except Exception as error:
                # A failed slot waits for the next one, like the daily workflow did;
                # retrying every poll would repeat provider and GitHub reads all day.
                _LOGGER.exception("Preview lifecycle sweep slot %s failed", key)
                summary = {"status": "error", "apply": False, "error": type(error).__name__}
            heartbeat.assert_current()
            self._write_completion(key, summary)
        log = _LOGGER.warning if summary["status"] in {"fail", "error"} else _LOGGER.info
        log("Preview lifecycle sweep slot %s completed: %s", key, summary["status"])
        return True

    def _run_sweep(self) -> dict[str, object]:
        # Scheduled sweeps are report-only; cleanup apply stays an explicit,
        # separately authorized request to POST /v1/previews/lifecycle-sweep.
        return build_preview_lifecycle_sweep(
            control_plane_root=self.control_plane_root,
            record_store=require_preview_lifecycle_sweep_store(self.store),
            request=PreviewLifecycleSweepEnvelope(apply=False),
            should_stop=self._stop.is_set,
        )


class PreviewInventoryScheduler(_PreviewScheduler):
    scope = "launchplane:preview-inventory-scheduler:v1"
    route = "service:preview-inventory-scheduler"
    fingerprint = "preview-inventory-scheduler-v1"
    # Three opportunities to collect before the shared evidence deadline.
    interval = int(PREVIEW_INVENTORY_STALE_AFTER.total_seconds() / 3)
    offset = 0

    def _run_sweep(self) -> dict[str, object]:
        return collect_preview_inventory(
            control_plane_root=self.control_plane_root,
            record_store=self.store,
            should_stop=self._stop.is_set,
        )


class PreviewLifecycleScheduler(_PreviewScheduler):
    def __init__(
        self,
        store: PostgresRecordStore,
        *,
        control_plane_root: Path,
        clock: Callable[[], float] = time.time,
        run_sweep: Callable[[], dict[str, object]] | None = None,
    ) -> None:
        super().__init__(
            store, control_plane_root=control_plane_root, clock=clock, run_sweep=run_sweep
        )
        self.inventory_scheduler = PreviewInventoryScheduler(
            store,
            control_plane_root=control_plane_root,
            clock=clock,
        )

    def start(self) -> None:
        self.inventory_scheduler.start()
        super().start()

    def stop(self) -> None:
        self.inventory_scheduler.stop()
        super().stop()
