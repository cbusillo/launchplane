from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EveryCodeWorkRequestSource = Literal["github_issue_label", "manual", "reconciliation"]
EveryCodeWorkRequestState = Literal["queued", "claimed", "running", "done", "blocked"]


class EveryCodeWorkRequestRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    request_id: str
    source: EveryCodeWorkRequestSource
    state: EveryCodeWorkRequestState
    repository: str
    issue_number: int = Field(ge=1)
    issue_url: str
    issue_title: str = ""
    trigger_label: str
    trigger_actor: str = ""
    github_delivery_id: str = ""
    queued_at: str
    updated_at: str
    claimed_at: str = ""
    claimed_by_host: str = ""
    started_at: str = ""
    finished_at: str = ""
    result_pr_url: str = ""
    result_summary: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "EveryCodeWorkRequestRecord":
        if not self.request_id.strip():
            raise ValueError("Every Code work request requires request_id")
        if not self.repository.strip() or "/" not in self.repository.strip():
            raise ValueError("Every Code work request requires owner/repo repository")
        if not self.issue_url.strip():
            raise ValueError("Every Code work request requires issue_url")
        if not self.trigger_label.strip():
            raise ValueError("Every Code work request requires trigger_label")
        if not self.queued_at.strip():
            raise ValueError("Every Code work request requires queued_at")
        if not self.updated_at.strip():
            raise ValueError("Every Code work request requires updated_at")
        if self.state in {"claimed", "running", "done", "blocked"}:
            if not self.claimed_at.strip():
                raise ValueError("claimed Every Code work request requires claimed_at")
            if not self.claimed_by_host.strip():
                raise ValueError("claimed Every Code work request requires claimed_by_host")
        if self.state in {"running", "done"} and not self.started_at.strip():
            raise ValueError("running Every Code work request requires started_at")
        if self.state in {"done", "blocked"} and not self.finished_at.strip():
            raise ValueError("finished Every Code work request requires finished_at")
        if self.state == "done" and self.error_message.strip():
            raise ValueError("done Every Code work request must not include error_message")
        if self.state == "blocked" and not self.error_message.strip():
            raise ValueError("blocked Every Code work request requires error_message")
        return self


class EveryCodeWorkRequestStatusUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    state: Literal["running", "done", "blocked"]
    host: str
    updated_at: str
    result_pr_url: str = ""
    result_summary: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_update(self) -> "EveryCodeWorkRequestStatusUpdate":
        if not self.host.strip():
            raise ValueError("Every Code status update requires host")
        if not self.updated_at.strip():
            raise ValueError("Every Code status update requires updated_at")
        if self.state == "done" and self.error_message.strip():
            raise ValueError("done Every Code status update must not include error_message")
        if self.state == "blocked" and not self.error_message.strip():
            raise ValueError("blocked Every Code status update requires error_message")
        return self


def build_every_code_work_request_id(
    *, repository: str, issue_number: int, trigger_label: str
) -> str:
    normalized_repository = repository.strip().lower()
    normalized_label = trigger_label.strip().lower()
    if not normalized_repository or issue_number < 1 or not normalized_label:
        raise ValueError("Every Code work request id requires repository, issue_number, and trigger_label")
    digest = hashlib.sha256(
        f"{normalized_repository}#{issue_number}:{normalized_label}".encode("utf-8")
    ).hexdigest()[:16]
    return f"every-code-{normalized_repository.replace('/', '-')}-{issue_number}-{digest}"


def claim_every_code_work_request(
    record: EveryCodeWorkRequestRecord, *, host: str, claimed_at: str
) -> EveryCodeWorkRequestRecord | None:
    normalized_host = host.strip()
    if not normalized_host:
        raise ValueError("Every Code work request claim requires host")
    if not claimed_at.strip():
        raise ValueError("Every Code work request claim requires claimed_at")
    if record.state != "queued":
        return None
    return record.model_copy(
        update={
            "state": "claimed",
            "claimed_at": claimed_at,
            "claimed_by_host": normalized_host,
            "updated_at": claimed_at,
        }
    )


def apply_every_code_work_request_status(
    record: EveryCodeWorkRequestRecord, update: EveryCodeWorkRequestStatusUpdate
) -> EveryCodeWorkRequestRecord:
    if record.state == "queued":
        raise ValueError("Every Code work request must be claimed before status updates")
    if record.state in {"done", "blocked"}:
        raise ValueError("finished Every Code work request cannot be updated")
    if record.claimed_by_host.strip() != update.host.strip():
        raise ValueError("Every Code status update host does not match claim host")

    updates: dict[str, object] = {
        "state": update.state,
        "updated_at": update.updated_at,
        "result_pr_url": update.result_pr_url,
        "result_summary": update.result_summary,
        "error_message": update.error_message,
    }
    if update.state == "running" and not record.started_at.strip():
        updates["started_at"] = update.updated_at
    if update.state in {"done", "blocked"}:
        updates["finished_at"] = update.updated_at
        if not record.started_at.strip():
            updates["started_at"] = update.updated_at
    return record.model_copy(update=updates)


def resume_every_code_work_request(
    record: EveryCodeWorkRequestRecord,
    *,
    host: str,
    resumed_at: str,
    result_summary: str,
) -> EveryCodeWorkRequestRecord:
    normalized_host = host.strip()
    if not normalized_host:
        raise ValueError("Every Code resume requires host")
    if not resumed_at.strip():
        raise ValueError("Every Code resume requires resumed_at")
    if record.claimed_by_host.strip() != normalized_host:
        raise ValueError("Every Code resume host does not match claim host")
    if record.state not in {"done", "blocked"}:
        raise ValueError("Every Code resume requires a terminal work request")
    return record.model_copy(
        update={
            "state": "running",
            "updated_at": resumed_at,
            "finished_at": "",
            "result_summary": result_summary.strip(),
            "error_message": "",
        }
    )


def close_every_code_work_request_for_pull_request(
    record: EveryCodeWorkRequestRecord,
    *,
    pr_url: str,
    merged: bool,
    closed_at: str,
) -> EveryCodeWorkRequestRecord | None:
    normalized_pr_url = pr_url.strip()
    if not normalized_pr_url:
        raise ValueError("Every Code PR close update requires pr_url")
    if not closed_at.strip():
        raise ValueError("Every Code PR close update requires closed_at")
    if record.result_pr_url.strip() != normalized_pr_url:
        return None
    if record.state in {"queued", "done", "blocked"}:
        return None

    state: EveryCodeWorkRequestState = "done" if merged else "blocked"
    summary = (
        f"Linked pull request merged: {normalized_pr_url}"
        if merged
        else f"Linked pull request closed without merge: {normalized_pr_url}"
    )
    error_message = "" if merged else summary
    updates: dict[str, object] = {
        "state": state,
        "updated_at": closed_at,
        "finished_at": closed_at,
        "result_pr_url": normalized_pr_url,
        "result_summary": summary,
        "error_message": error_message,
    }
    if not record.started_at.strip():
        updates["started_at"] = closed_at
    return record.model_copy(update=updates)
