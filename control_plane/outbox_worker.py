from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
import time
from typing import Protocol

from control_plane.contracts.outbox_delivery import OutboxDeliveryRecord
from control_plane.workflows.generic_web_promotion_workflow import (
    dispatch_generic_web_promotion_workflow_delivery,
)
from control_plane.workflows.public_ingress_monitor import (
    PublicIngressNotificationDriverSet,
    deliver_public_ingress_notification_outbox_delivery,
)


DEFAULT_OUTBOX_WORKER_LEASE_SECONDS = 300
DEFAULT_OUTBOX_WORKER_POLL_SECONDS = 5
DEFAULT_OUTBOX_WORKER_ERROR_BACKOFF_SECONDS = 10
DEFAULT_OUTBOX_WORKER_MAX_CONSECUTIVE_ERRORS = 5


class OutboxWorkerStore(Protocol):
    def claim_next_outbox_delivery_record(
        self,
        *,
        lease_owner: str,
        lease_seconds: int = 300,
        now: str = "",
    ) -> object: ...

    def mark_outbox_delivery_provider_started(
        self,
        *,
        record: OutboxDeliveryRecord,
        lease_owner: str,
        provider_operation_key: str,
        provider_id: str = "",
        updated_at: str = "",
    ) -> object: ...

    def complete_outbox_delivery_record(
        self,
        *,
        record: OutboxDeliveryRecord,
        lease_owner: str,
    ) -> object: ...


@dataclass(frozen=True)
class OutboxWorkerStepResult:
    status: str
    delivery_id: str = ""
    kind: str = ""
    error_code: str = ""


def run_outbox_worker_once(
    *,
    record_store: OutboxWorkerStore,
    control_plane_root: Path,
    lease_owner: str,
    lease_seconds: int = DEFAULT_OUTBOX_WORKER_LEASE_SECONDS,
    notification_drivers: PublicIngressNotificationDriverSet | None = None,
) -> OutboxWorkerStepResult:
    claim = record_store.claim_next_outbox_delivery_record(
        lease_owner=lease_owner,
        lease_seconds=lease_seconds,
    )
    if getattr(claim, "status", "") == "empty":
        return OutboxWorkerStepResult(status="idle")
    record = getattr(claim, "record", None)
    if not isinstance(record, OutboxDeliveryRecord):
        return OutboxWorkerStepResult(status="idle")
    if record.kind == "github_workflow_dispatch":
        result = dispatch_generic_web_promotion_workflow_delivery(
            record=record,
            control_plane_root=control_plane_root,
            mark_provider_started=lambda started_record, provider_operation_key, provider_id: (
                _mark_provider_started(
                    record_store=record_store,
                    record=started_record,
                    lease_owner=lease_owner,
                    provider_operation_key=provider_operation_key,
                    provider_id=provider_id,
                )
            ),
        )
    elif record.kind == "public_ingress_notification":
        result = deliver_public_ingress_notification_outbox_delivery(
            record=record,
            drivers=notification_drivers or PublicIngressNotificationDriverSet(),
            mark_provider_started=lambda provider_operation_key, provider_id: (
                _mark_provider_started(
                    record_store=record_store,
                    record=record,
                    lease_owner=lease_owner,
                    provider_operation_key=provider_operation_key,
                    provider_id=provider_id,
                )
            ),
        )
    elif record.kind == "operator_authorization_recovery_alert":
        result = record.model_copy(
            update={
                "state": "delivered",
                "action": "recorded_local_authorization_recovery_alert",
                "error_code": "",
            }
        )
    else:
        result = record.model_copy(
            update={"state": "failed", "error_code": "unsupported_outbox_kind"}
        )
    completion = record_store.complete_outbox_delivery_record(
        record=result,
        lease_owner=lease_owner,
    )
    completed = getattr(completion, "record", result)
    if not isinstance(completed, OutboxDeliveryRecord):
        completed = result
    if getattr(completion, "status", "") != "updated":
        return OutboxWorkerStepResult(
            status="completion_failed",
            delivery_id=record.delivery_id,
            kind=record.kind,
            error_code=str(getattr(completion, "status", "unknown")),
        )
    return OutboxWorkerStepResult(
        status=completed.state,
        delivery_id=completed.delivery_id,
        kind=completed.kind,
        error_code=completed.error_code,
    )


def run_outbox_worker_loop(
    *,
    record_store: OutboxWorkerStore,
    control_plane_root: Path,
    lease_owner: str,
    should_stop: Callable[[], bool],
    lease_seconds: int = DEFAULT_OUTBOX_WORKER_LEASE_SECONDS,
    poll_seconds: int = DEFAULT_OUTBOX_WORKER_POLL_SECONDS,
    error_backoff_seconds: int = DEFAULT_OUTBOX_WORKER_ERROR_BACKOFF_SECONDS,
    max_consecutive_errors: int = DEFAULT_OUTBOX_WORKER_MAX_CONSECUTIVE_ERRORS,
    notification_drivers: PublicIngressNotificationDriverSet | None = None,
) -> None:
    consecutive_errors = 0
    while not should_stop():
        try:
            result = run_outbox_worker_once(
                record_store=record_store,
                control_plane_root=control_plane_root,
                lease_owner=lease_owner,
                lease_seconds=lease_seconds,
                notification_drivers=notification_drivers,
            )
            consecutive_errors = 0
        except Exception:  # noqa: BLE001 - worker loop backoff keeps process bounded.
            consecutive_errors += 1
            if consecutive_errors >= max_consecutive_errors:
                raise
            time.sleep(max(1, error_backoff_seconds))
            continue
        if result.status == "idle":
            time.sleep(max(1, poll_seconds))


def _mark_provider_started(
    *,
    record_store: OutboxWorkerStore,
    record: OutboxDeliveryRecord,
    lease_owner: str,
    provider_operation_key: str,
    provider_id: str,
) -> None:
    update = record_store.mark_outbox_delivery_provider_started(
        record=record,
        lease_owner=lease_owner,
        provider_operation_key=provider_operation_key,
        provider_id=provider_id,
    )
    if getattr(update, "status", "") != "updated":
        raise RuntimeError("outbox_provider_marker_not_recorded")
