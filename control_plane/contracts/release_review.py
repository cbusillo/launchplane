"""Client decisions about the complete change being promoted to production."""

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
    preview_era_notes: bool = Field(
        default=False,
        exclude_if=lambda value: not value,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class SharedSourceReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    production_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_commit: str = Field(pattern=r"^[0-9a-f]{40}$")
    items: tuple[ReleaseReviewItem, ...]
    untracked_commits: tuple[str, ...] = ()


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
    # Preserve the serialized shape and digest of historical website-only decisions.
    shared_sources: tuple[SharedSourceReview, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


ReleaseDecision = Literal["accepted", "changes_requested", "overridden"]
# How an accepted release runs, fixed when the Client accepts it: "" starts nothing.
ReleaseStart = Literal["", "promote", "promote_with_rollback_drill"]


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
    # Omitted when empty, so decisions recorded before acceptance started releases
    # serialize as they did.
    release_start: ReleaseStart = Field(default="", exclude_if=lambda value: value == "")

    acceptance_source: Literal["client_session", "director_standing"] = Field(
        default="client_session", exclude_if=lambda value: value == "client_session"
    )

    @model_validator(mode="after")
    def validate_decision(self) -> "ReleaseReviewDecisionRecord":
        if self.product != self.checklist.product:
            raise ValueError("Release decision and checklist products must match.")
        if self.decision != "accepted" and not self.reason.strip():
            raise ValueError("Requesting changes or overriding requires a reason.")
        if self.decision != "overridden" and self.actor_github_id != self.checklist.owner_github_id:
            raise ValueError("Only the product's Client can accept or request changes.")
        if self.acceptance_source == "director_standing" and self.decision != "accepted":
            raise ValueError("Standing acceptance can only record acceptance.")
        if self.release_start and self.decision != "accepted":
            raise ValueError("Only the Client's acceptance starts a release.")
        return self


# Why the release checklist could not be compiled. Fixed codes only: the
# underlying errors can carry private URLs, so they never reach a response.
ReleaseEvidenceReason = Literal[
    "testing_lane_missing",
    "source_control_access_unavailable",
    "production_identity_missing",
    "candidate_identity_missing",
    "release_record_missing",
    "github_read_failed",
]


class ReleaseReviewStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    required: bool = True
    approved: bool = False
    checklist_complete: bool | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    checklist: ReleaseChecklist | None = None
    checklist_digest: str = ""
    blockers: tuple[str, ...] = ()
    latest_decision: ReleaseReviewDecisionRecord | None = None
    unavailable_reason: ReleaseEvidenceReason | None = None
