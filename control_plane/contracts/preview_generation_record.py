from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.promotion_record import ReleaseStatus

PreviewGenerationState = Literal[
    "resolving",
    "building",
    "deploying",
    "verifying",
    "ready",
    "failed",
    "superseded",
]
PreviewSourceSelection = Literal["anchor", "companion", "baseline"]


class PreviewSourceRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    git_sha: str
    selection: PreviewSourceSelection

    @model_validator(mode="after")
    def _validate_source(self) -> "PreviewSourceRecord":
        if not self.repo.strip():
            raise ValueError("preview source record requires repo")
        if not self.git_sha.strip():
            raise ValueError("preview source record requires git_sha")
        return self


class PreviewPullRequestSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    pr_number: int = Field(ge=1)
    head_sha: str
    pr_url: str

    @model_validator(mode="after")
    def _validate_summary(self) -> "PreviewPullRequestSummary":
        if not self.repo.strip():
            raise ValueError("preview PR summary requires repo")
        if not self.head_sha.strip():
            raise ValueError("preview PR summary requires head_sha")
        if not self.pr_url.strip():
            raise ValueError("preview PR summary requires pr_url")
        return self


class PreviewGenerationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    generation_id: str
    preview_id: str
    sequence: int = Field(ge=1)
    state: PreviewGenerationState
    requested_reason: str
    requested_at: str
    started_at: str = ""
    ready_at: str = ""
    finished_at: str = ""
    superseded_at: str = ""
    failed_at: str = ""
    expires_at: str = ""
    resolved_manifest_fingerprint: str
    artifact_id: str = ""
    baseline_release_tuple_id: str = ""
    source_map: tuple[PreviewSourceRecord, ...] = ()
    anchor_summary: PreviewPullRequestSummary
    companion_summaries: tuple[PreviewPullRequestSummary, ...] = ()
    deploy_status: ReleaseStatus = "pending"
    verify_status: ReleaseStatus = "pending"
    overall_health_status: ReleaseStatus = "pending"
    failure_stage: str = ""
    failure_summary: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "PreviewGenerationRecord":
        if not self.generation_id.strip():
            raise ValueError("preview generation record requires generation_id")
        if not self.preview_id.strip():
            raise ValueError("preview generation record requires preview_id")
        if not self.requested_reason.strip():
            raise ValueError("preview generation record requires requested_reason")
        if not self.requested_at.strip():
            raise ValueError("preview generation record requires requested_at")
        if not self.resolved_manifest_fingerprint.strip():
            raise ValueError("preview generation record requires resolved_manifest_fingerprint")
        return self
