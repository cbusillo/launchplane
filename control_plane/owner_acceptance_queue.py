from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.owner_acceptance import (
    OwnerAcceptanceBinding,
    OwnerAcceptanceDecisionStatus,
    OwnerAcceptanceEventRecord,
)

OWNER_ACCEPTANCE_QUEUE_LIMIT = 50


class OwnerAcceptanceQueueReadStore(Protocol):
    def list_owner_acceptance_event_records(
        self,
        *,
        repository_id: str = "",
        repository: str = "",
        pull_request_number: int | None = None,
        product: str = "",
        system: str = "",
        action: str = "",
        acceptance_action: str = "",
        limit: int | None = None,
    ) -> tuple[OwnerAcceptanceEventRecord, ...]: ...


def require_owner_acceptance_queue_read_store(store: object) -> OwnerAcceptanceQueueReadStore:
    if not callable(getattr(store, "list_owner_acceptance_event_records", None)):
        raise TypeError("Owner acceptance queue storage requires list_owner_acceptance_event_records.")
    return cast(OwnerAcceptanceQueueReadStore, store)


class OwnerAcceptanceQueueEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    repository_id: str
    repository: str
    pull_request_number: int
    product: str
    system: str
    action: str
    environment: str
    mode: Literal["shadow"] = "shadow"
    authoritative: Literal[False] = False
    enforcement_effect: Literal["none"] = "none"
    verification_required: Literal[True] = True
    ledger_status: OwnerAcceptanceDecisionStatus
    next_action: str
    latest_event: OwnerAcceptanceEventRecord
    latest_binding: OwnerAcceptanceBinding
    occurred_at: str


@dataclass(frozen=True)
class OwnerAcceptanceQueueBuildResult:
    total: int
    candidate: int
    truncated: bool
    has_more: bool
    entries: tuple[OwnerAcceptanceQueueEntry, ...]


def build_owner_acceptance_queue(
    *,
    store: object,
    repository: str = "",
    status: str = "",
    limit: int = OWNER_ACCEPTANCE_QUEUE_LIMIT,
) -> OwnerAcceptanceQueueBuildResult:
    queue_store = require_owner_acceptance_queue_read_store(store)

    all_events = queue_store.list_owner_acceptance_event_records()

    # Fold by full subject: (repository_id, pull_request_number, product, system, action, environment)
    # Latest event per subject by (occurred_at, event_id) — deterministic tie-break
    latest_by_subject: dict[tuple[str, int, str, str, str, str], OwnerAcceptanceEventRecord] = {}
    for event in all_events:
        b = event.binding
        key = (b.repository_id, b.pull_request_number, b.product, b.system, b.action, b.environment)
        existing = latest_by_subject.get(key)
        if existing is None or (event.occurred_at, event.event_id) > (existing.occurred_at, existing.event_id):
            latest_by_subject[key] = event

    total = len(latest_by_subject)

    # Build candidate entries — malformed actions raise ValueError and fail the route
    all_entries: list[OwnerAcceptanceQueueEntry] = []
    for event in latest_by_subject.values():
        ledger_status = _ledger_status_from_action(event.action)
        all_entries.append(
            OwnerAcceptanceQueueEntry(
                repository_id=event.binding.repository_id,
                repository=event.binding.repository,
                pull_request_number=event.binding.pull_request_number,
                product=event.binding.product,
                system=event.binding.system,
                action=event.binding.action,
                environment=event.binding.environment,
                ledger_status=ledger_status,
                next_action=_next_action(ledger_status),
                latest_event=event,
                latest_binding=event.binding,
                occurred_at=event.occurred_at,
            )
        )

    # Apply repository/status filters BEFORE bounded pagination
    normalized_repository = repository.strip().lower()
    normalized_status = status.strip().lower()
    filtered = [
        entry for entry in all_entries
        if (not normalized_repository or normalized_repository in entry.repository)
        and (not normalized_status or entry.ledger_status == normalized_status)
    ]
    candidate = len(filtered)

    # Sort newest-first by occurred_at, then event_id for determinism
    filtered.sort(key=lambda e: (e.occurred_at, e.latest_event.event_id), reverse=True)

    truncated = candidate > limit
    entries = tuple(filtered[:limit])

    return OwnerAcceptanceQueueBuildResult(
        total=total,
        candidate=candidate,
        truncated=truncated,
        has_more=truncated,
        entries=entries,
    )


def _ledger_status_from_action(action: str) -> OwnerAcceptanceDecisionStatus:
    if action == "accepted":
        return "accepted"
    if action == "changes_requested":
        return "changes_requested"
    if action == "revoked":
        return "revoked"
    if action == "superseded":
        return "stale"
    if action == "invalidated":
        return "unavailable"
    raise ValueError(f"Unknown Owner acceptance action: {action!r}")


def _next_action(status: OwnerAcceptanceDecisionStatus) -> str:
    if status == "pending":
        return "Owner acceptance required"
    if status == "changes_requested":
        return "Engineer must address Owner feedback before re-review"
    if status == "revoked":
        return "Re-acceptance required after changes"
    if status == "stale":
        return "Re-evaluation required; binding has changed"
    if status == "unavailable":
        return "Evidence unavailable; retry after prerequisites are met"
    if status == "not_required":
        return "No Owner acceptance required for this engineering-only change"
    if status == "accepted":
        return "Owner acceptance is current"
    return ""


def now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
