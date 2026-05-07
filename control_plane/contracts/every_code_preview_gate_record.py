from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EveryCodePreviewGateStatus = Literal[
    "pending",
    "ready",
    "blocked",
    "labeled",
    "cancelled",
]


class EveryCodePreviewGateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    gate_id: str
    request_id: str
    repository: str
    issue_number: int = Field(ge=1)
    issue_url: str
    issue_author: str = ""
    pr_number: int = Field(ge=1)
    pr_url: str
    head_sha: str
    status: EveryCodePreviewGateStatus
    created_at: str
    updated_at: str
    ready_at: str = ""
    labeled_at: str = ""
    blocked_at: str = ""
    cancelled_at: str = ""
    last_checked_at: str = ""
    pending_reason: str = ""
    blocked_reason: str = ""
    check_summary: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "EveryCodePreviewGateRecord":
        if not self.gate_id.strip():
            raise ValueError("Every Code preview gate requires gate_id")
        if not self.request_id.strip():
            raise ValueError("Every Code preview gate requires request_id")
        if not self.repository.strip() or "/" not in self.repository.strip():
            raise ValueError("Every Code preview gate requires owner/repo repository")
        if not self.issue_url.strip():
            raise ValueError("Every Code preview gate requires issue_url")
        if not self.pr_url.strip():
            raise ValueError("Every Code preview gate requires pr_url")
        if not self.head_sha.strip():
            raise ValueError("Every Code preview gate requires head_sha")
        if not self.created_at.strip():
            raise ValueError("Every Code preview gate requires created_at")
        if not self.updated_at.strip():
            raise ValueError("Every Code preview gate requires updated_at")
        if self.status == "ready" and not self.ready_at.strip():
            raise ValueError("ready Every Code preview gate requires ready_at")
        if self.status == "labeled" and not self.labeled_at.strip():
            raise ValueError("labeled Every Code preview gate requires labeled_at")
        if self.status == "blocked" and not self.blocked_at.strip():
            raise ValueError("blocked Every Code preview gate requires blocked_at")
        if self.status == "cancelled" and not self.cancelled_at.strip():
            raise ValueError("cancelled Every Code preview gate requires cancelled_at")
        return self


def build_every_code_preview_gate_id(
    *, repository: str, pr_number: int, head_sha: str
) -> str:
    normalized_repository = repository.strip().lower()
    normalized_sha = head_sha.strip().lower()
    if not normalized_repository or pr_number < 1 or not normalized_sha:
        raise ValueError(
            "Every Code preview gate id requires repository, pr_number, and head_sha"
        )
    digest = hashlib.sha256(
        f"{normalized_repository}#{pr_number}:{normalized_sha}".encode("utf-8")
    ).hexdigest()[:16]
    return (
        f"every-code-preview-gate-{normalized_repository.replace('/', '-')}-{pr_number}"
        f"-{normalized_sha[:12]}-{digest}"
    )
