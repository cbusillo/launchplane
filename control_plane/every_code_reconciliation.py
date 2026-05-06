from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, Sequence

from control_plane.contracts.every_code_work_request import (
    EveryCodeWorkRequestRecord,
    build_every_code_work_request_id,
)
from control_plane.workflows.ship import utc_now_timestamp


EveryCodeReconciliationStatus = Literal["created", "deduped", "skipped"]


class EveryCodeReconciliationStore(Protocol):
    def create_every_code_work_request_record_if_absent(
        self, record: EveryCodeWorkRequestRecord
    ) -> tuple[EveryCodeWorkRequestRecord, bool]: ...


@dataclass(frozen=True)
class EveryCodeIssueReconciliationResult:
    status: EveryCodeReconciliationStatus
    detail: str
    request: EveryCodeWorkRequestRecord | None = None

    def as_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {"status": self.status, "detail": self.detail}
        if self.request is not None:
            payload["request"] = self.request.model_dump(mode="json")
        return payload


def reconcile_every_code_issue(
    *,
    record_store: EveryCodeReconciliationStore,
    repository: str,
    issue_number: int,
    issue_url: str,
    issue_title: str,
    labels: Sequence[str],
    trigger_label: str = "every-code",
    actor: str = "reconciliation",
) -> EveryCodeIssueReconciliationResult:
    normalized_repository = repository.strip()
    normalized_issue_url = issue_url.strip()
    normalized_trigger_label = trigger_label.strip()
    normalized_labels = tuple(label.strip() for label in labels if label.strip())

    if not normalized_trigger_label:
        raise ValueError("Every Code issue reconciliation requires a trigger label")
    if not normalized_repository or "/" not in normalized_repository:
        raise ValueError("Every Code issue reconciliation requires owner/repo repository")
    if issue_number < 1:
        raise ValueError("Every Code issue reconciliation requires a positive issue number")
    if not normalized_issue_url:
        raise ValueError("Every Code issue reconciliation requires issue_url")

    if normalized_trigger_label.casefold() not in {label.casefold() for label in normalized_labels}:
        return EveryCodeIssueReconciliationResult(
            status="skipped",
            detail=f"Issue is not labeled {normalized_trigger_label!r}.",
        )

    now = utc_now_timestamp()
    record = EveryCodeWorkRequestRecord(
        request_id=build_every_code_work_request_id(
            repository=normalized_repository,
            issue_number=issue_number,
            trigger_label=normalized_trigger_label,
        ),
        source="reconciliation",
        state="queued",
        repository=normalized_repository,
        issue_number=issue_number,
        issue_url=normalized_issue_url,
        issue_title=issue_title.strip(),
        trigger_label=normalized_trigger_label,
        trigger_actor=actor.strip(),
        queued_at=now,
        updated_at=now,
    )
    stored_record, created = record_store.create_every_code_work_request_record_if_absent(record)
    if created:
        return EveryCodeIssueReconciliationResult(
            status="created",
            detail="Queued Every Code work request from reconciled issue facts.",
            request=stored_record,
        )
    return EveryCodeIssueReconciliationResult(
        status="deduped",
        detail="Every Code work request already exists for this issue and label.",
        request=stored_record,
    )
