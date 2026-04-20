from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.preview_generation_record import (
    PreviewGenerationState,
    PreviewPullRequestSummary,
    PreviewSourceRecord,
)
from control_plane.contracts.preview_record import PreviewState
from control_plane.contracts.promotion_record import ReleaseStatus


class PreviewMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    anchor_repo: str
    anchor_pr_number: int = Field(ge=1)
    anchor_pr_url: str
    canonical_url: str = ""
    state: PreviewState = "pending"
    created_at: str = ""
    updated_at: str = ""
    eligible_at: str = ""
    paused_at: str = ""
    destroy_after: str = ""
    destroyed_at: str = ""
    destroy_reason: str = ""
    active_generation_id: str = ""
    serving_generation_id: str = ""
    latest_generation_id: str = ""
    latest_manifest_fingerprint: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "PreviewMutationRequest":
        if not self.context.strip():
            raise ValueError("preview mutation request requires context")
        if not self.anchor_repo.strip():
            raise ValueError("preview mutation request requires anchor_repo")
        if not self.anchor_pr_url.strip():
            raise ValueError("preview mutation request requires anchor_pr_url")
        return self


class PreviewGenerationMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    anchor_repo: str
    anchor_pr_number: int = Field(ge=1)
    anchor_pr_url: str
    anchor_head_sha: str
    sequence: int | None = Field(default=None, ge=1)
    generation_id: str = ""
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
    companion_summaries: tuple[PreviewPullRequestSummary, ...] = ()
    deploy_status: ReleaseStatus = "pending"
    verify_status: ReleaseStatus = "pending"
    overall_health_status: ReleaseStatus = "pending"
    failure_stage: str = ""
    failure_summary: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "PreviewGenerationMutationRequest":
        if not self.context.strip():
            raise ValueError("preview generation mutation request requires context")
        if not self.anchor_repo.strip():
            raise ValueError("preview generation mutation request requires anchor_repo")
        if not self.anchor_pr_url.strip():
            raise ValueError("preview generation mutation request requires anchor_pr_url")
        if not self.anchor_head_sha.strip():
            raise ValueError("preview generation mutation request requires anchor_head_sha")
        if not self.requested_reason.strip():
            raise ValueError("preview generation mutation request requires requested_reason")
        if not self.requested_at.strip():
            raise ValueError("preview generation mutation request requires requested_at")
        if not self.resolved_manifest_fingerprint.strip():
            raise ValueError(
                "preview generation mutation request requires resolved_manifest_fingerprint"
            )
        return self


class PreviewGenerationIntentRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    anchor_repo: str
    anchor_pr_number: int = Field(ge=1)
    anchor_pr_url: str
    anchor_head_sha: str
    state: PreviewGenerationState
    requested_reason: str
    requested_at: str

    @model_validator(mode="after")
    def _validate_request(self) -> "PreviewGenerationIntentRequest":
        if not self.context.strip():
            raise ValueError("preview generation intent request requires context")
        if not self.anchor_repo.strip():
            raise ValueError("preview generation intent request requires anchor_repo")
        if not self.anchor_pr_url.strip():
            raise ValueError("preview generation intent request requires anchor_pr_url")
        if not self.anchor_head_sha.strip():
            raise ValueError("preview generation intent request requires anchor_head_sha")
        if not self.requested_reason.strip():
            raise ValueError("preview generation intent request requires requested_reason")
        if not self.requested_at.strip():
            raise ValueError("preview generation intent request requires requested_at")
        return self


class PreviewDestroyMutationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    anchor_repo: str
    anchor_pr_number: int = Field(ge=1)
    destroyed_at: str
    destroy_reason: str

    @model_validator(mode="after")
    def _validate_request(self) -> "PreviewDestroyMutationRequest":
        if not self.context.strip():
            raise ValueError("preview destroy mutation request requires context")
        if not self.anchor_repo.strip():
            raise ValueError("preview destroy mutation request requires anchor_repo")
        if not self.destroyed_at.strip():
            raise ValueError("preview destroy mutation request requires destroyed_at")
        if not self.destroy_reason.strip():
            raise ValueError("preview destroy mutation request requires destroy_reason")
        return self


HarborMutationCommand = Literal["request-generation", "destroy-preview"]


class HarborPullRequestMutationIntent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    command: HarborMutationCommand
    manifest_resolution_required: bool = False
    preview_request: PreviewMutationRequest | None = None
    generation_request: PreviewGenerationMutationRequest | None = None
    generation_request_seed: PreviewGenerationIntentRequest | None = None
    destroy_request: PreviewDestroyMutationRequest | None = None

    @model_validator(mode="after")
    def _validate_intent(self) -> "HarborPullRequestMutationIntent":
        if self.command == "request-generation":
            if self.preview_request is None:
                raise ValueError("request-generation Harbor mutation intent requires preview_request")
            if self.destroy_request is not None:
                raise ValueError("request-generation Harbor mutation intent cannot include destroy_request")
            if self.generation_request is None and self.generation_request_seed is None:
                raise ValueError(
                    "request-generation Harbor mutation intent requires generation_request or generation_request_seed"
                )
            if self.generation_request is not None and self.generation_request_seed is not None:
                raise ValueError(
                    "request-generation Harbor mutation intent cannot include both generation_request and generation_request_seed"
                )
            if self.generation_request_seed is not None and not self.manifest_resolution_required:
                raise ValueError(
                    "request-generation Harbor mutation intent with generation_request_seed requires manifest_resolution_required"
                )
            return self

        if self.preview_request is not None:
            raise ValueError("destroy-preview Harbor mutation intent cannot include preview_request")
        if self.generation_request is not None or self.generation_request_seed is not None:
            raise ValueError("destroy-preview Harbor mutation intent cannot include generation payload")
        if self.destroy_request is None:
            raise ValueError("destroy-preview Harbor mutation intent requires destroy_request")
        if self.manifest_resolution_required:
            raise ValueError("destroy-preview Harbor mutation intent cannot require manifest resolution")
        return self
