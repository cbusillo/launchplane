from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.release_review import ReleaseVersion


ReleaseRequestSource = Literal["director_request", "finish_line"]
RELEASE_REQUEST_ROUTE = "/v1/release-review/requests"


class ReleaseRequestDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str = Field(pattern=r"^[0-9a-f]{64}$")
    product: str = Field(min_length=1)
    repository: str = Field(min_length=1)
    owner_github_id: str = Field(pattern=r"^[0-9]+$")
    checklist_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    production: ReleaseVersion
    candidate: ReleaseVersion
    client_decision_record_id: str = ""
    source: ReleaseRequestSource
    reason: str = Field(min_length=1, max_length=1000)
    decision_reference: str = Field(default="", max_length=2000)
    actor: str = Field(min_length=1)
    decided_at: str

    @model_validator(mode="after")
    def validate_decision(self) -> "ReleaseRequestDecisionRecord":
        if not self.reason.strip() or not self.actor.strip():
            raise ValueError("A release request decision requires a reason and actor.")
        if self.source == "finish_line" and not self.decision_reference.strip():
            raise ValueError("A finish-line request requires its recorded agreement.")
        if datetime.fromisoformat(self.decided_at).tzinfo is None:
            raise ValueError("A release request decision requires a timezone.")
        if self.production == self.candidate:
            raise ValueError("A release request requires an unreleased candidate.")
        return self
