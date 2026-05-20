from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


MergeTrainPrFeedbackEvent = Literal[
    "queued",
    "building",
    "waiting",
    "blocked",
    "stale_policy",
    "completed",
]
MergeTrainPrFeedbackDeliveryStatus = Literal["delivered", "skipped", "failed"]


class MergeTrainPrFeedbackRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    feedback_id: str
    repository: str
    base_branch: str
    pull_request_number: int = Field(gt=0)
    pull_request_url: str
    event: MergeTrainPrFeedbackEvent
    marker: str
    comment_markdown: str
    source: str
    recorded_at: str
    policy_key: str = ""
    policy_sha256: str = ""
    controller_action: str = ""
    controller_record_id: str = ""
    delivery_status: MergeTrainPrFeedbackDeliveryStatus
    delivery_action: str = ""
    comment_id: int = 0
    comment_url: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "MergeTrainPrFeedbackRecord":
        if not self.feedback_id.strip():
            raise ValueError("merge train PR feedback requires feedback_id")
        self.repository = _normalize_repository(self.repository)
        self.base_branch = _normalize_required_value(
            self.base_branch, "merge train PR feedback requires base_branch"
        )
        self.pull_request_url = _normalize_required_value(
            self.pull_request_url, "merge train PR feedback requires pull_request_url"
        )
        self.marker = _normalize_required_value(
            self.marker, "merge train PR feedback requires marker"
        )
        self.comment_markdown = _normalize_required_value(
            self.comment_markdown,
            "merge train PR feedback requires comment_markdown",
        )
        self.source = _normalize_required_value(
            self.source, "merge train PR feedback requires source"
        )
        self.recorded_at = _normalize_required_value(
            self.recorded_at, "merge train PR feedback requires recorded_at"
        )
        self.policy_key = self.policy_key.strip()
        self.policy_sha256 = self.policy_sha256.strip()
        self.controller_action = self.controller_action.strip()
        self.controller_record_id = self.controller_record_id.strip()
        self.delivery_action = self.delivery_action.strip()
        self.comment_url = self.comment_url.strip()
        self.error_message = self.error_message.strip()
        return self


def build_merge_train_pr_feedback_id(
    *,
    repository: str,
    base_branch: str,
    pull_request_number: int,
    event: MergeTrainPrFeedbackEvent,
    marker: str,
    recorded_at: str,
) -> str:
    normalized_repository = _normalize_repository(repository).replace("/", "-")
    normalized_base = _slug(base_branch)
    identity = _feedback_identity_digest(
        repository=repository,
        base_branch=base_branch,
        pull_request_number=pull_request_number,
        event=event,
        marker=marker,
        recorded_at=recorded_at,
    )
    return (
        "merge-train-pr-feedback-"
        f"{normalized_repository}-{normalized_base}-pr-{pull_request_number}-{identity}"
    )


def merge_train_pr_feedback_marker(
    *, repository: str, base_branch: str, pull_request_number: int
) -> str:
    digest = hashlib.sha256(
        json.dumps(
            {
                "repository": _normalize_repository(repository),
                "base_branch": _normalize_required_value(base_branch, "base_branch"),
                "pull_request_number": pull_request_number,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]
    return f"<!-- launchplane-merge-train:{digest} -->"


def _feedback_identity_digest(
    *,
    repository: str,
    base_branch: str,
    pull_request_number: int,
    event: str,
    marker: str,
    recorded_at: str,
) -> str:
    return hashlib.sha256(
        json.dumps(
            {
                "repository": _normalize_repository(repository),
                "base_branch": _normalize_required_value(base_branch, "base_branch"),
                "pull_request_number": pull_request_number,
                "event": event.strip(),
                "marker": marker.strip(),
                "recorded_at": _normalize_required_value(recorded_at, "recorded_at"),
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:16]


def _normalize_repository(repository: str) -> str:
    normalized = repository.strip()
    if not normalized or "/" not in normalized:
        raise ValueError("merge train PR feedback requires owner/name repository")
    return normalized


def _normalize_required_value(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized


def _slug(value: str) -> str:
    normalized = "".join(
        character.lower() if character.isalnum() else "-" for character in value.strip()
    ).strip("-")
    return normalized or "default"
