from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Protocol, cast

from control_plane.contracts.privileged_operation import (
    ManagedSecretReencryptionPlanInput,
    PrivilegedOperationActor,
    PrivilegedOperationConflictError,
    PrivilegedOperationEventRecord,
    PrivilegedOperationEventWriteStatus,
    PrivilegedOperationRecord,
    build_privileged_operation_event_id,
    build_privileged_operation_id,
    privileged_operation_evidence_digest,
    privileged_operation_record_digest,
    privileged_operation_request_digest,
)
from control_plane.privileged_operation_registry import (
    MANAGED_SECRET_REENCRYPTION_DESCRIPTOR,
    read_privileged_operation_descriptor,
)


DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS = 30 * 60
MIN_PRIVILEGED_OPERATION_TTL_SECONDS = 5 * 60
MAX_PRIVILEGED_OPERATION_TTL_SECONDS = 24 * 60 * 60


class PrivilegedOperationStore(Protocol):
    def write_privileged_operation_plan(
        self,
        record: PrivilegedOperationRecord,
        event: PrivilegedOperationEventRecord,
    ) -> PrivilegedOperationEventWriteStatus: ...

    def transition_privileged_operation(
        self,
        record: PrivilegedOperationRecord,
        event: PrivilegedOperationEventRecord,
    ) -> PrivilegedOperationEventWriteStatus: ...

    def read_privileged_operation_record(
        self,
        operation_id: str,
    ) -> PrivilegedOperationRecord: ...

    def list_privileged_operation_records(
        self,
        *,
        status: str = "",
        descriptor_id: str = "",
        limit: int | None = None,
    ) -> tuple[PrivilegedOperationRecord, ...]: ...

    def list_privileged_operation_event_records(
        self,
        *,
        operation_id: str = "",
        limit: int | None = None,
    ) -> tuple[PrivilegedOperationEventRecord, ...]: ...


class PrivilegedOperationStoreUnavailableError(TypeError):
    pass


class PrivilegedOperationNotCancellableError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class PrivilegedOperationWriteResult:
    write_status: PrivilegedOperationEventWriteStatus
    record: PrivilegedOperationRecord
    event: PrivilegedOperationEventRecord


def require_privileged_operation_store(record_store: object) -> PrivilegedOperationStore:
    required_methods = (
        "write_privileged_operation_plan",
        "transition_privileged_operation",
        "read_privileged_operation_record",
        "list_privileged_operation_records",
        "list_privileged_operation_event_records",
    )
    if any(
        not callable(getattr(record_store, method_name, None)) for method_name in required_methods
    ):
        raise PrivilegedOperationStoreUnavailableError(
            "Privileged-operation planning requires operation record and event storage."
        )
    return cast(PrivilegedOperationStore, record_store)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def _read_privileged_operation_event(
    *,
    store: PrivilegedOperationStore,
    operation_id: str,
    event_id: str,
) -> PrivilegedOperationEventRecord | None:
    return next(
        (
            event
            for event in store.list_privileged_operation_event_records(
                operation_id=operation_id,
                limit=10,
            )
            if event.event_id == event_id
        ),
        None,
    )


def _replay_existing_plan(
    *,
    store: PrivilegedOperationStore,
    operation_id: str,
    actor: PrivilegedOperationActor,
    source_event_id: str,
    request: ManagedSecretReencryptionPlanInput,
    expires_in_seconds: int,
) -> PrivilegedOperationWriteResult | None:
    try:
        record = store.read_privileged_operation_record(operation_id)
    except FileNotFoundError:
        return None
    expected_expiry = datetime.fromisoformat(record.created_at) + timedelta(
        seconds=expires_in_seconds
    )
    if (
        record.descriptor_id != MANAGED_SECRET_REENCRYPTION_DESCRIPTOR.descriptor_id
        or record.descriptor_version != MANAGED_SECRET_REENCRYPTION_DESCRIPTOR.descriptor_version
        or record.safety_class != MANAGED_SECRET_REENCRYPTION_DESCRIPTOR.safety_class
        or record.source_event_id != source_event_id
        or record.requested_by != actor
        or record.request_digest != privileged_operation_request_digest(request)
        or datetime.fromisoformat(record.expires_at) != expected_expiry
    ):
        raise PrivilegedOperationConflictError(
            "Privileged-operation plan replay changed the original request."
        )
    event_id = build_privileged_operation_event_id(
        operation_id=operation_id,
        action="planned",
        source_kind="browser_api",
        source_event_id=source_event_id,
    )
    event = _read_privileged_operation_event(
        store=store,
        operation_id=operation_id,
        event_id=event_id,
    )
    if event is None:
        raise PrivilegedOperationConflictError(
            "Privileged-operation plan exists without its planned event."
        )
    return PrivilegedOperationWriteResult(
        write_status="replayed",
        record=record,
        event=event,
    )


def create_privileged_operation_plan(
    *,
    record_store: object,
    requester_github_id: int,
    requester_login: str,
    source_event_id: str,
    request: ManagedSecretReencryptionPlanInput,
    expires_in_seconds: int = DEFAULT_PRIVILEGED_OPERATION_TTL_SECONDS,
    now: Callable[[], datetime] = _utc_now,
) -> PrivilegedOperationWriteResult:
    if (
        not MIN_PRIVILEGED_OPERATION_TTL_SECONDS
        <= expires_in_seconds
        <= MAX_PRIVILEGED_OPERATION_TTL_SECONDS
    ):
        raise ValueError("Privileged-operation plan expiry must be between 300 and 86400 seconds.")
    store = require_privileged_operation_store(record_store)
    actor = PrivilegedOperationActor(
        identity_type="github_human",
        github_id=requester_github_id,
        login=requester_login,
    )
    operation_id = build_privileged_operation_id(
        github_id=requester_github_id,
        source_event_id=source_event_id,
    )
    replay = _replay_existing_plan(
        store=store,
        operation_id=operation_id,
        actor=actor,
        source_event_id=source_event_id,
        request=request,
        expires_in_seconds=expires_in_seconds,
    )
    if replay is not None:
        return replay
    registration = read_privileged_operation_descriptor("managed-secret-reencryption")
    evidence = registration.planner(record_store, request)
    created_at = now().astimezone(timezone.utc)
    record = PrivilegedOperationRecord(
        operation_id=operation_id,
        descriptor_id=registration.descriptor.descriptor_id,
        descriptor_version=registration.descriptor.descriptor_version,
        safety_class=registration.descriptor.safety_class,
        status="planned",
        source_event_id=source_event_id,
        requested_by=actor,
        request=request,
        request_digest=privileged_operation_request_digest(request),
        evidence=evidence,
        evidence_digest=privileged_operation_evidence_digest(evidence),
        created_at=_timestamp(created_at),
        updated_at=_timestamp(created_at),
        expires_at=_timestamp(created_at + timedelta(seconds=expires_in_seconds)),
    )
    event = PrivilegedOperationEventRecord(
        operation_id=operation_id,
        sequence=1,
        action="planned",
        occurred_at=record.created_at,
        source_kind="browser_api",
        source_event_id=source_event_id,
        actor=actor,
        resulting_record_digest=privileged_operation_record_digest(record),
    )
    try:
        write_status = store.write_privileged_operation_plan(record, event)
    except PrivilegedOperationConflictError:
        replay = _replay_existing_plan(
            store=store,
            operation_id=operation_id,
            actor=actor,
            source_event_id=source_event_id,
            request=request,
            expires_in_seconds=expires_in_seconds,
        )
        if replay is not None:
            return replay
        raise
    persisted_record = store.read_privileged_operation_record(operation_id)
    persisted_event = next(
        (
            stored_event
            for stored_event in store.list_privileged_operation_event_records(
                operation_id=operation_id,
                limit=2,
            )
            if stored_event.event_id == event.event_id
        ),
        event,
    )
    return PrivilegedOperationWriteResult(
        write_status=write_status,
        record=persisted_record,
        event=persisted_event,
    )


def expire_privileged_operation_if_due(
    *,
    record_store: object,
    record: PrivilegedOperationRecord,
    now: Callable[[], datetime] = _utc_now,
) -> PrivilegedOperationRecord:
    if record.status != "planned":
        return record
    current_time = now().astimezone(timezone.utc)
    if current_time < datetime.fromisoformat(record.expires_at):
        return record
    store = require_privileged_operation_store(record_store)
    occurred_at = _timestamp(current_time)
    reason = "Privileged-operation plan expired before approval or execution existed."
    expired_record = record.model_copy(
        update={
            "status": "expired",
            "updated_at": occurred_at,
            "terminal_at": occurred_at,
            "terminal_reason": reason,
        }
    )
    source_event_id = f"expiry:{record.operation_id}"
    event = PrivilegedOperationEventRecord(
        event_id=build_privileged_operation_event_id(
            operation_id=record.operation_id,
            action="expired",
            source_kind="system",
            source_event_id=source_event_id,
        ),
        operation_id=record.operation_id,
        sequence=2,
        action="expired",
        occurred_at=occurred_at,
        source_kind="system",
        source_event_id=source_event_id,
        actor=PrivilegedOperationActor(identity_type="system", login="system"),
        reason=reason,
        resulting_record_digest=privileged_operation_record_digest(expired_record),
    )
    try:
        store.transition_privileged_operation(expired_record, event)
    except PrivilegedOperationConflictError:
        return store.read_privileged_operation_record(record.operation_id)
    return expired_record


def read_privileged_operation(
    *,
    record_store: object,
    operation_id: str,
    now: Callable[[], datetime] = _utc_now,
) -> PrivilegedOperationRecord:
    store = require_privileged_operation_store(record_store)
    record = store.read_privileged_operation_record(operation_id)
    return expire_privileged_operation_if_due(
        record_store=store,
        record=record,
        now=now,
    )


def list_privileged_operations(
    *,
    record_store: object,
    status: str = "",
    limit: int | None = None,
    now: Callable[[], datetime] = _utc_now,
) -> tuple[PrivilegedOperationRecord, ...]:
    store = require_privileged_operation_store(record_store)
    if status != "cancelled":
        for record in store.list_privileged_operation_records(
            status="planned",
            descriptor_id=MANAGED_SECRET_REENCRYPTION_DESCRIPTOR.descriptor_id,
        ):
            expire_privileged_operation_if_due(record_store=store, record=record, now=now)
    return store.list_privileged_operation_records(
        status=status,
        descriptor_id=MANAGED_SECRET_REENCRYPTION_DESCRIPTOR.descriptor_id,
        limit=limit,
    )


def cancel_privileged_operation(
    *,
    record_store: object,
    operation_id: str,
    actor_github_id: int,
    actor_login: str,
    source_event_id: str,
    reason: str,
    now: Callable[[], datetime] = _utc_now,
) -> PrivilegedOperationWriteResult:
    store = require_privileged_operation_store(record_store)
    normalized_reason = reason.strip()
    if not normalized_reason:
        raise ValueError("Privileged-operation cancellation requires a reason.")
    actor = PrivilegedOperationActor(
        identity_type="github_human",
        github_id=actor_github_id,
        login=actor_login,
    )
    event_id = build_privileged_operation_event_id(
        operation_id=operation_id,
        action="cancelled",
        source_kind="browser_api",
        source_event_id=source_event_id,
    )
    record = read_privileged_operation(
        record_store=store,
        operation_id=operation_id,
        now=now,
    )
    existing_event = _read_privileged_operation_event(
        store=store,
        operation_id=operation_id,
        event_id=event_id,
    )
    if existing_event is not None:
        if (
            record.status == "cancelled"
            and existing_event.action == "cancelled"
            and existing_event.actor == actor
            and existing_event.reason == normalized_reason
            and existing_event.resulting_record_digest == privileged_operation_record_digest(record)
        ):
            return PrivilegedOperationWriteResult(
                write_status="replayed",
                record=record,
                event=existing_event,
            )
        raise PrivilegedOperationConflictError(
            "Privileged-operation cancellation replay changed the original request."
        )
    if record.status != "planned":
        raise PrivilegedOperationNotCancellableError(
            f"Privileged-operation plan is already {record.status}."
        )
    occurred_at = _timestamp(now().astimezone(timezone.utc))
    cancelled_record = record.model_copy(
        update={
            "status": "cancelled",
            "updated_at": occurred_at,
            "terminal_at": occurred_at,
            "terminal_reason": normalized_reason,
        }
    )
    event = PrivilegedOperationEventRecord(
        event_id=event_id,
        operation_id=operation_id,
        sequence=2,
        action="cancelled",
        occurred_at=occurred_at,
        source_kind="browser_api",
        source_event_id=source_event_id,
        actor=actor,
        reason=normalized_reason,
        resulting_record_digest=privileged_operation_record_digest(cancelled_record),
    )
    try:
        write_status = store.transition_privileged_operation(cancelled_record, event)
    except PrivilegedOperationConflictError:
        persisted_record = store.read_privileged_operation_record(operation_id)
        persisted_event = _read_privileged_operation_event(
            store=store,
            operation_id=operation_id,
            event_id=event_id,
        )
        if (
            persisted_event is not None
            and persisted_record.status == "cancelled"
            and persisted_event.actor == actor
            and persisted_event.reason == normalized_reason
            and persisted_event.resulting_record_digest
            == privileged_operation_record_digest(persisted_record)
        ):
            return PrivilegedOperationWriteResult(
                write_status="replayed",
                record=persisted_record,
                event=persisted_event,
            )
        raise
    return PrivilegedOperationWriteResult(
        write_status=write_status,
        record=store.read_privileged_operation_record(operation_id),
        event=event,
    )
