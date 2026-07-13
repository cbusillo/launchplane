from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EveryCodeWorkRequestSource = Literal["github_issue_label", "manual", "reconciliation"]
EveryCodeWorkRequestState = Literal["queued", "claimed", "running", "done", "blocked"]
EveryCodeWorkRequestRecoveryPolicy = Literal["safe_requeue", "manual_review"]

EVERY_CODE_WORK_REQUEST_DEFAULT_LEASE_SECONDS = 1800
EVERY_CODE_WORK_REQUEST_MAX_SAFE_ATTEMPTS = 3


class EveryCodeWorkRequestRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    request_id: str
    lifecycle_id: str = ""
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
    lease_expires_at: str = ""
    fencing_token: int = Field(default=0, ge=0)
    attempt: int = Field(default=0, ge=0)
    started_at: str = ""
    finished_at: str = ""
    result_pr_url: str = ""
    result_summary: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "EveryCodeWorkRequestRecord":
        if not self.request_id.strip():
            raise ValueError("Every Code work request requires request_id")
        if not self.lifecycle_id.strip():
            self.lifecycle_id = build_every_code_work_request_lifecycle_id(
                request_id=self.request_id, queued_at=self.queued_at
            )
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
    fencing_token: int = Field(default=0, ge=0)
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


class EveryCodeWorkRequestHeartbeat(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str
    fencing_token: int = Field(ge=1)
    heartbeat_at: str
    lease_expires_at: str

    @model_validator(mode="after")
    def _validate_heartbeat(self) -> "EveryCodeWorkRequestHeartbeat":
        if not self.host.strip():
            raise ValueError("Every Code heartbeat requires host")
        if not self.heartbeat_at.strip():
            raise ValueError("Every Code heartbeat requires heartbeat_at")
        if not self.lease_expires_at.strip():
            raise ValueError("Every Code heartbeat requires lease_expires_at")
        return self


def build_every_code_work_request_id(
    *, repository: str, issue_number: int, trigger_label: str
) -> str:
    normalized_repository = repository.strip().lower()
    normalized_label = trigger_label.strip().lower()
    if not normalized_repository or issue_number < 1 or not normalized_label:
        raise ValueError(
            "Every Code work request id requires repository, issue_number, and trigger_label"
        )
    digest = hashlib.sha256(
        f"{normalized_repository}#{issue_number}:{normalized_label}".encode("utf-8")
    ).hexdigest()[:16]
    return f"every-code-{normalized_repository.replace('/', '-')}-{issue_number}-{digest}"


def build_every_code_work_request_lifecycle_id(
    *, request_id: str, queued_at: str, previous_lifecycle_id: str = ""
) -> str:
    normalized_request_id = request_id.strip().lower()
    normalized_queued_at = queued_at.strip()
    if not normalized_request_id or not normalized_queued_at:
        raise ValueError("Every Code work request lifecycle id requires request_id and queued_at")
    digest = hashlib.sha256(
        json.dumps(
            {
                "request_id": normalized_request_id,
                "queued_at": normalized_queued_at,
                "previous_lifecycle_id": previous_lifecycle_id.strip(),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"every-code-lifecycle-{digest}"


def claim_every_code_work_request(
    record: EveryCodeWorkRequestRecord,
    *,
    host: str,
    claimed_at: str,
    lease_expires_at: str = "",
    lease_seconds: int = EVERY_CODE_WORK_REQUEST_DEFAULT_LEASE_SECONDS,
) -> EveryCodeWorkRequestRecord | None:
    normalized_host = host.strip()
    if not normalized_host:
        raise ValueError("Every Code work request claim requires host")
    if not claimed_at.strip():
        raise ValueError("Every Code work request claim requires claimed_at")
    if record.state != "queued":
        return None
    new_attempt = record.attempt + 1
    resolved_lease_expires_at = lease_expires_at.strip() or _add_seconds_to_timestamp(
        claimed_at, lease_seconds
    )
    return record.model_copy(
        update={
            "state": "claimed",
            "claimed_at": claimed_at,
            "claimed_by_host": normalized_host,
            "lease_expires_at": resolved_lease_expires_at,
            "fencing_token": new_attempt,
            "attempt": new_attempt,
            "updated_at": claimed_at,
        }
    )


def heartbeat_every_code_work_request(
    record: EveryCodeWorkRequestRecord,
    heartbeat: EveryCodeWorkRequestHeartbeat,
) -> EveryCodeWorkRequestRecord | None:
    if record.state not in {"claimed", "running"}:
        return None
    if record.claimed_by_host.strip() != heartbeat.host.strip():
        return None
    if record.fencing_token != heartbeat.fencing_token:
        return None
    return record.model_copy(
        update={
            "lease_expires_at": heartbeat.lease_expires_at,
            "updated_at": heartbeat.heartbeat_at,
        }
    )


def classify_stale_every_code_work_request_recovery(
    record: EveryCodeWorkRequestRecord,
) -> EveryCodeWorkRequestRecoveryPolicy:
    if record.attempt <= EVERY_CODE_WORK_REQUEST_MAX_SAFE_ATTEMPTS:
        return "safe_requeue"
    return "manual_review"


def apply_every_code_work_request_status(
    record: EveryCodeWorkRequestRecord, update: EveryCodeWorkRequestStatusUpdate
) -> EveryCodeWorkRequestRecord:
    if record.state == "queued":
        raise ValueError("Every Code work request must be claimed before status updates")
    if record.state in {"done", "blocked"}:
        raise ValueError("finished Every Code work request cannot be updated")
    if record.claimed_by_host.strip() != update.host.strip():
        raise ValueError("Every Code status update host does not match claim host")
    if update.fencing_token > 0 and record.fencing_token != update.fencing_token:
        raise ValueError(
            f"Every Code status update fencing token {update.fencing_token} "
            f"does not match record fencing token {record.fencing_token}"
        )

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


def requeue_every_code_work_request(
    record: EveryCodeWorkRequestRecord,
    *,
    queued_at: str,
    trigger_actor: str = "",
) -> EveryCodeWorkRequestRecord:
    if not queued_at.strip():
        raise ValueError("Every Code requeue requires queued_at")
    if record.state not in {"done", "blocked", "claimed", "running"}:
        raise ValueError("Every Code requeue requires a terminal or active work request")

    return record.model_copy(
        update={
            "state": "queued",
            "lifecycle_id": build_every_code_work_request_lifecycle_id(
                request_id=record.request_id,
                queued_at=queued_at,
                previous_lifecycle_id=record.lifecycle_id,
            ),
            "trigger_actor": trigger_actor.strip() or record.trigger_actor,
            "queued_at": queued_at,
            "updated_at": queued_at,
            "claimed_at": "",
            "claimed_by_host": "",
            "lease_expires_at": "",
            "fencing_token": 0,
            "started_at": "",
            "finished_at": "",
            "result_pr_url": "",
            "result_summary": "",
            "error_message": "",
        }
    )


def recover_stale_every_code_work_request(
    record: EveryCodeWorkRequestRecord,
    *,
    recovered_at: str,
) -> EveryCodeWorkRequestRecord:
    if record.state not in {"claimed", "running"}:
        raise ValueError("Every Code stale recovery requires an active work request")
    if not recovered_at.strip():
        raise ValueError("Every Code stale recovery requires recovered_at")
    if classify_stale_every_code_work_request_recovery(record) == "safe_requeue":
        return requeue_every_code_work_request(record, queued_at=recovered_at)
    return record.model_copy(
        update={
            "state": "blocked",
            "finished_at": recovered_at,
            "updated_at": recovered_at,
            "error_message": (
                f"Stale lease expired after {record.attempt} attempt(s); "
                "manual review required before requeue."
            ),
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
    if record.result_pr_url.strip() and record.result_pr_url.strip() != normalized_pr_url:
        return None
    if record.state in {"done", "blocked"}:
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
    if not record.claimed_at.strip():
        updates["claimed_at"] = closed_at
    if not record.claimed_by_host.strip():
        updates["claimed_by_host"] = "github-pull-request-close"
    if not record.started_at.strip():
        updates["started_at"] = closed_at
    return record.model_copy(update=updates)


def close_every_code_work_request_for_issue(
    record: EveryCodeWorkRequestRecord,
    *,
    closed_at: str,
    reason: str = "",
) -> EveryCodeWorkRequestRecord | None:
    if not closed_at.strip():
        raise ValueError("Every Code issue close update requires closed_at")
    if record.state in {"done", "blocked"}:
        return None

    normalized_reason = reason.strip().lower()
    suffix = f" ({normalized_reason})" if normalized_reason else ""
    summary = f"Source issue closed{suffix}: {record.issue_url.strip()}"
    updates: dict[str, object] = {
        "state": "done",
        "updated_at": closed_at,
        "finished_at": closed_at,
        "result_summary": summary,
        "error_message": "",
    }
    if not record.claimed_at.strip():
        updates["claimed_at"] = closed_at
    if not record.claimed_by_host.strip():
        updates["claimed_by_host"] = "github-issue-close"
    if not record.started_at.strip():
        updates["started_at"] = closed_at
    return record.model_copy(update=updates)


def _add_seconds_to_timestamp(timestamp: str, seconds: int) -> str:
    try:
        dt = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return timestamp
    result = dt.astimezone(UTC) + timedelta(seconds=seconds)
    return result.replace(microsecond=0).isoformat().replace("+00:00", "Z")
