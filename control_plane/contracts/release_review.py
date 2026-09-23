"""Owner decisions about the complete change being promoted to production."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ReleaseVersion(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    artifact_id: str = Field(min_length=1)
    source_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    shared_addons_digest: str = ""


class ReleaseReviewItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    pull_request_number: int = Field(ge=1)
    title: str
    url: str
    head_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    merge_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    owner_test_notes: str
    already_reviewed: bool = False


class ReleaseChecklist(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product: str
    repository: str
    owner_github_id: str
    testing_url: str
    production: ReleaseVersion
    candidate: ReleaseVersion
    items: tuple[ReleaseReviewItem, ...]
    untracked_commits: tuple[str, ...] = ()
    additional_changes: tuple[str, ...] = ()


ReleaseDecision = Literal["accepted", "changes_requested", "overridden"]


class ReleaseReviewDecisionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record_id: str = Field(min_length=1)
    product: str = Field(min_length=1)
    checklist_digest: str = Field(pattern=r"^[0-9a-f]{64}$")
    checklist: ReleaseChecklist
    decision: ReleaseDecision
    reason: str = Field(default="", max_length=4000)
    actor_github_id: str = Field(pattern=r"^[0-9]+$")
    actor_github_login: str = Field(min_length=1)
    decided_at: str = Field(min_length=1)
    release_issue_url: str = ""

    @model_validator(mode="after")
    def validate_decision(self) -> "ReleaseReviewDecisionRecord":
        if self.product != self.checklist.product:
            raise ValueError("Release decision and checklist products must match.")
        if self.decision != "accepted" and not self.reason.strip():
            raise ValueError("Requesting changes or overriding requires a reason.")
        if self.decision != "overridden" and self.actor_github_id != self.checklist.owner_github_id:
            raise ValueError("Only the product Owner can accept or request changes.")
        return self


class ReleaseReviewStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool = True
    approved: bool = False
    checklist: ReleaseChecklist | None = None
    checklist_digest: str = ""
    blockers: tuple[str, ...] = ()
    latest_decision: ReleaseReviewDecisionRecord | None = None
