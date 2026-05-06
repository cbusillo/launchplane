from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


EveryCodePrFeedbackKind = Literal[
    "issue_comment",
    "pull_request_review",
    "pull_request_review_comment",
]
EveryCodePrFeedbackStatus = Literal["pending", "applied", "ignored"]


class EveryCodePrFeedbackRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    feedback_id: str
    request_id: str
    repository: str
    pr_number: int = Field(ge=1)
    pr_url: str
    feedback_kind: EveryCodePrFeedbackKind
    github_delivery_id: str
    github_node_id: str = ""
    github_id: str = ""
    actor: str
    author_association: str = ""
    body: str
    html_url: str = ""
    submitted_at: str = ""
    received_at: str
    status: EveryCodePrFeedbackStatus = "pending"

    @model_validator(mode="after")
    def _validate_record(self) -> "EveryCodePrFeedbackRecord":
        if not self.feedback_id.strip():
            raise ValueError("Every Code PR feedback requires feedback_id")
        if not self.request_id.strip():
            raise ValueError("Every Code PR feedback requires request_id")
        if not self.repository.strip() or "/" not in self.repository.strip():
            raise ValueError("Every Code PR feedback requires owner/repo repository")
        if not self.pr_url.strip():
            raise ValueError("Every Code PR feedback requires pr_url")
        if not self.github_delivery_id.strip():
            raise ValueError("Every Code PR feedback requires github_delivery_id")
        if not self.github_node_id.strip() and not self.github_id.strip():
            raise ValueError("Every Code PR feedback requires github_node_id or github_id")
        if not self.actor.strip():
            raise ValueError("Every Code PR feedback requires actor")
        if not self.body.strip():
            raise ValueError("Every Code PR feedback requires body")
        if not self.received_at.strip():
            raise ValueError("Every Code PR feedback requires received_at")
        return self


def build_every_code_pr_feedback_id(
    *,
    repository: str,
    pr_number: int,
    github_delivery_id: str,
    github_node_id: str = "",
    github_id: str = "",
) -> str:
    normalized_repository = repository.strip().lower()
    identity = github_node_id.strip() or github_id.strip()
    if not normalized_repository or pr_number < 1 or not github_delivery_id.strip() or not identity:
        raise ValueError(
            "Every Code PR feedback id requires repository, pr_number, delivery id, and feedback id"
        )
    digest = hashlib.sha256(
        f"{normalized_repository}#{pr_number}:{github_delivery_id.strip()}:{identity}".encode(
            "utf-8"
        )
    ).hexdigest()[:16]
    return f"every-code-pr-feedback-{normalized_repository.replace('/', '-')}-{pr_number}-{digest}"
