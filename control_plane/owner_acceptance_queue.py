from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict

from control_plane.change_impact_service import ChangeImpactRepositoryEvidenceProvider
from control_plane.contracts.change_impact import ChangeImpactTargetReference
from control_plane.contracts.engineering_review_decision import EngineeringReviewDecisionRecord
from control_plane.contracts.owner_acceptance import (
    OwnerAcceptanceDecision,
    OwnerAcceptanceEventRecord,
)
from control_plane.owner_acceptance import (
    OwnerAcceptanceEvaluationUnavailableError,
    evaluate_owner_acceptance,
    require_owner_acceptance_event_store,
)

OWNER_ACCEPTANCE_QUEUE_LIMIT = 50
_QUEUE_SCAN_LIMIT = 500


class OwnerAcceptanceQueueReadStore(Protocol):
    def list_engineering_review_decision_records(
        self,
        *,
        repository: str = "",
        pull_request_number: int | None = None,
        head_sha: str = "",
        work_request_id: str = "",
        limit: int | None = None,
    ) -> tuple[EngineeringReviewDecisionRecord, ...]: ...

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
    for method in (
        "list_engineering_review_decision_records",
        "list_owner_acceptance_event_records",
    ):
        if not callable(getattr(store, method, None)):
            raise TypeError(f"Owner acceptance queue storage requires {method}.")
    return cast(OwnerAcceptanceQueueReadStore, store)


class OwnerAcceptanceQueueEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    repository: str
    pull_request_number: int
    mode: Literal["shadow"] = "shadow"
    authoritative: Literal[False] = False
    enforcement_effect: Literal["none"] = "none"
    engineering_review_decision: EngineeringReviewDecisionRecord | None
    owner_acceptance_decision: OwnerAcceptanceDecision
    next_action: str


def build_owner_acceptance_queue(
    *,
    store: object,
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider,
    limit: int = OWNER_ACCEPTANCE_QUEUE_LIMIT,
) -> tuple[OwnerAcceptanceQueueEntry, ...]:
    queue_store = require_owner_acceptance_queue_read_store(store)

    all_erds = queue_store.list_engineering_review_decision_records(limit=_QUEUE_SCAN_LIMIT)
    latest_erd_by_target: dict[tuple[str, int], EngineeringReviewDecisionRecord] = {}
    for erd in all_erds:
        key = (erd.target.repository, erd.target.pull_request_number)
        if key not in latest_erd_by_target:
            latest_erd_by_target[key] = erd

    event_store = require_owner_acceptance_event_store(store)
    all_events = event_store.list_owner_acceptance_event_records(limit=_QUEUE_SCAN_LIMIT)
    event_targets: set[tuple[str, int]] = {
        (event.binding.repository, event.binding.pull_request_number)
        for event in all_events
    }

    all_targets = event_targets | set(latest_erd_by_target.keys())

    latest_event_at: dict[tuple[str, int], str] = {}
    for event in all_events:
        key = (event.binding.repository, event.binding.pull_request_number)
        if key not in latest_event_at or event.occurred_at > latest_event_at[key]:
            latest_event_at[key] = event.occurred_at

    def _sort_key(target: tuple[str, int]) -> str:
        erd = latest_erd_by_target.get(target)
        if erd is not None:
            return erd.evaluated_at
        return latest_event_at.get(target, "")

    sorted_targets = sorted(all_targets, key=_sort_key, reverse=True)
    bounded_targets = sorted_targets[:limit]

    evaluated_at = _now_utc()
    entries: list[OwnerAcceptanceQueueEntry] = []
    for repository, pull_request_number in bounded_targets:
        erd = latest_erd_by_target.get((repository, pull_request_number))
        try:
            decision = evaluate_owner_acceptance(
                store=store,
                repository_evidence_provider=repository_evidence_provider,
                target=ChangeImpactTargetReference(
                    repository=repository,
                    pull_request_number=pull_request_number,
                ),
                evaluated_at=evaluated_at,
            )
        except (OwnerAcceptanceEvaluationUnavailableError, TypeError, ValueError):
            continue
        entries.append(
            OwnerAcceptanceQueueEntry(
                repository=repository,
                pull_request_number=pull_request_number,
                engineering_review_decision=erd,
                owner_acceptance_decision=decision,
                next_action=_next_action(decision),
            )
        )

    return tuple(entries)


def _next_action(decision: OwnerAcceptanceDecision) -> str:
    status = decision.status
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


def _now_utc() -> str:
    return (
        datetime.now(timezone.utc)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )
