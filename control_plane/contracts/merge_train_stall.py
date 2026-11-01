from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.contracts.repository_inventory import normalize_repository

STALL_WORKFLOW_REPOSITORY_KEY = "LAUNCHPLANE_MERGE_TRAIN_STALL_ALERT_REPOSITORY"
STALL_WORKFLOW_REF_KEY = "LAUNCHPLANE_MERGE_TRAIN_STALL_ALERT_REF"


def parse_merge_train_stall_repository(value: str) -> str:
    return normalize_repository(value, "repository")


def parse_merge_train_stall_ref(value: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError("merge train stall workflow ref is required")
    return normalized


class MergeTrainStallWorkflow(BaseModel):
    """Existing sender's repository/ref from Launchplane runtime settings."""

    model_config = ConfigDict(extra="forbid")
    repository: str
    ref: str = Field(min_length=1)

    @field_validator("repository")
    @classmethod
    def valid_repository(cls, value: str) -> str:
        return parse_merge_train_stall_repository(value)

    @field_validator("ref")
    @classmethod
    def valid_ref(cls, value: str) -> str:
        return parse_merge_train_stall_ref(value)


class MergeTrainStallObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    progress_key: str = ""
    progress_since: str = ""
    ready_since: dict[str, str] = Field(default_factory=dict)
    blocked_since: dict[str, str] = Field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    suspected_signals: tuple[str, ...] = ()
    queue_suspended: bool = False
    setting_gaps: tuple[str, ...] = Field(default=(), exclude_if=lambda value: not value)


class MergeTrainStallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str
    observed_at: str
    observations: dict[str, MergeTrainStallObservation] = Field(default_factory=dict)
    issue_number: int | None = None
    issue_state: Literal["open", "closed"] = "closed"
    issue_body_sha256: str = ""
    published_reason_sha256: str = ""
    published_issue_number: int | None = None
    notification_key: str = ""
    notified_key: str = ""
    delivery_error: str = ""

    @property
    def normalized_repository(self) -> str:
        return normalize_repository(self.repository, "repository")
