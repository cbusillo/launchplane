from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EveryCodeWorkRequestSource = Literal["github_issue_label", "manual", "reconciliation"]
EveryCodeWorkRequestState = Literal["queued", "claimed", "running", "done", "blocked"]


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
        ).encode()
    ).hexdigest()[:16]
    return f"every-code-lifecycle-{digest}"
