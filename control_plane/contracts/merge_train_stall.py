from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from control_plane.contracts.repository_inventory import normalize_repository


class MergeTrainStallWorkflow(BaseModel):
    """Existing sender's repository/ref, supplied by the active policy record."""

    model_config = ConfigDict(extra="forbid")
    repository: str
    ref: str = Field(min_length=1)

    @field_validator("repository")
    @classmethod
    def valid_repository(cls, value: str) -> str:
        return normalize_repository(value, "repository")


class MergeTrainStallObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    progress_key: str = ""
    progress_since: str = ""
    ready_since: dict[str, str] = Field(default_factory=dict)
    reasons: tuple[str, ...] = ()
    suspected_signals: tuple[str, ...] = ()


class MergeTrainStallRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str
    observed_at: str
    observations: dict[str, MergeTrainStallObservation] = Field(default_factory=dict)
    issue_number: int | None = None
    issue_state: Literal["open", "closed"] = "closed"
    issue_body_sha256: str = ""
    notification_key: str = ""
    notified_key: str = ""
    delivery_error: str = ""

    @property
    def normalized_repository(self) -> str:
        return normalize_repository(self.repository, "repository")
