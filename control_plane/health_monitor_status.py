"""Read bounded, persisted scheduler receipts without running the monitor."""

from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.health_monitor_scheduler import (
    MONITOR_RECEIPT_FINGERPRINT,
    _ROUTE,
    _SCOPE,
    monitor_slot,
    monitor_key,
    monitor_lane_key,
)
from control_plane.contracts.public_ingress_monitoring import health_monitor_interval_seconds
from control_plane.storage.postgres import PostgresRecordStore


class MonitorCompletionReceipt(BaseModel):
    record_id: str
    completed_at: str


class MonitorSlotEvidence(BaseModel):
    slot: int
    scheduled_at: str
    completion_deadline_at: str
    completion: MonitorCompletionReceipt | None
    completed_on_time: bool | None
    missed_report: MonitorCompletionReceipt | None
    healthy_report: MonitorCompletionReceipt | None


class MonitorLaneChange(BaseModel):
    product: str
    context: str
    instance: str
    completion: MonitorCompletionReceipt


class HealthMonitorStatus(BaseModel):
    observed_at: str
    interval_seconds: int
    completion_grace_seconds: int
    activation: MonitorCompletionReceipt | None
    slots: list[MonitorSlotEvidence]
    next_end_slot: int | None
    instance: Literal["prod", "testing"] = "prod"
    lane_changes: list[MonitorLaneChange] = []
    lane_changes_truncated: bool = False


class HealthMonitorStatusResponse(BaseModel):
    trace_id: str
    monitor: HealthMonitorStatus


class HealthMonitorSlotRangeError(ValueError):
    """The caller requested an out-of-bounds or future slot window."""


def _timestamp(seconds: float) -> str:
    return datetime.fromtimestamp(seconds, timezone.utc).isoformat().replace("+00:00", "Z")


def _receipt(record: LaunchplaneIdempotencyRecord | None) -> MonitorCompletionReceipt | None:
    if record is None or record.state != "completed":
        return None
    if (
        record.request_fingerprint != MONITOR_RECEIPT_FINGERPRINT
        or record.response_status_code != 200
    ):
        raise ValueError("Scheduler receipt has an unexpected fingerprint")
    return MonitorCompletionReceipt(record_id=record.record_id, completed_at=record.recorded_at)


def read_health_monitor_status(
    store: PostgresRecordStore,
    *,
    now: float,
    slots: int = 6,
    end_slot: int | None = None,
    instance: Literal["prod", "testing"] = "prod",
) -> HealthMonitorStatus:
    interval = health_monitor_interval_seconds(instance)
    current_slot = monitor_slot(now, instance)
    last_slot = current_slot if end_slot is None else end_slot
    if not 1 <= slots <= 48 or not 0 <= last_slot <= current_slot:
        raise HealthMonitorSlotRangeError("Read at most 48 current or historical scheduler slots")

    def read(key: str) -> MonitorCompletionReceipt | None:
        return _receipt(
            store.read_idempotency_record(
                scope=_SCOPE, route_path=_ROUTE, idempotency_key=monitor_key(key, instance)
            )
        )

    activation_record = store.read_idempotency_record(
        scope=_SCOPE, route_path=_ROUTE, idempotency_key=monitor_key("enabled", instance)
    )
    activation = _receipt(activation_record)
    activation_slot = (
        monitor_slot(
            datetime.fromisoformat(activation_record.created_at.replace("Z", "+00:00")).timestamp(),
            instance,
        )
        if activation is not None and activation_record is not None
        else None
    )
    first_slot = max(activation_slot or 0, last_slot - slots + 1)
    evidence = []
    for slot in range(last_slot, first_slot - 1, -1):
        scheduled = slot * interval
        deadline = scheduled + interval
        completion = read(str(slot))
        completed_seconds = (
            datetime.fromisoformat(completion.completed_at.replace("Z", "+00:00")).timestamp()
            if completion is not None
            else None
        )
        evidence.append(
            MonitorSlotEvidence(
                slot=slot,
                scheduled_at=_timestamp(scheduled),
                completion_deadline_at=_timestamp(deadline),
                completion=completion,
                completed_on_time=(
                    scheduled <= completed_seconds <= deadline
                    if completed_seconds is not None
                    else None
                ),
                missed_report=read(f"watch:{slot}:True"),
                healthy_report=read(f"watch:{slot}:False"),
            )
        )
    changes = []
    for profile in store.list_product_profile_records():
        for lane in profile.lanes:
            if (lane.instance == "testing") != (instance == "testing"):
                continue
            receipt = _receipt(
                store.read_idempotency_record(
                    scope=_SCOPE,
                    route_path=_ROUTE,
                    idempotency_key="change:"
                    + monitor_lane_key((profile.product, lane.context, lane.instance)),
                )
            )
            if receipt is not None:
                changes.append(
                    MonitorLaneChange(
                        product=profile.product,
                        context=lane.context,
                        instance=lane.instance,
                        completion=receipt,
                    )
                )
    changes.sort(key=lambda change: change.completion.completed_at, reverse=True)
    return HealthMonitorStatus(
        observed_at=_timestamp(now),
        interval_seconds=interval,
        completion_grace_seconds=interval,
        activation=activation,
        slots=evidence,
        next_end_slot=(
            first_slot - 1
            if activation_slot is not None and first_slot > activation_slot and evidence
            else None
        ),
        instance=instance,
        lane_changes=changes[:48],
        lane_changes_truncated=len(changes) > 48,
    )
