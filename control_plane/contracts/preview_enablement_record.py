from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.github_pull_request_event import PullRequestAction, PullRequestState
from control_plane.contracts.preview_generation_record import PreviewPullRequestSummary
from control_plane.contracts.preview_request_metadata import (
    HarborCompanionPullRequestReference,
    HarborPreviewRequestParseStatus,
)


class PreviewEnablementRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    context: str
    anchor_repo: str
    anchor_pr_number: int = Field(ge=1)
    anchor_pr_url: str
    anchor_head_sha: str
    action: PullRequestAction
    pr_state: PullRequestState
    updated_at: str
    label_enabled: bool = False
    action_label: str = ""
    request_metadata_status: HarborPreviewRequestParseStatus = "missing"
    request_metadata_error: str = ""
    request_metadata_baseline_channel: str = ""
    request_metadata_companions: tuple[HarborCompanionPullRequestReference, ...] = ()
    request_metadata_companion_summaries: tuple[PreviewPullRequestSummary, ...] = ()

    @model_validator(mode="after")
    def _validate_record(self) -> "PreviewEnablementRecord":
        if not self.record_id.strip():
            raise ValueError("preview enablement record requires record_id")
        if not self.context.strip():
            raise ValueError("preview enablement record requires context")
        if not self.anchor_repo.strip():
            raise ValueError("preview enablement record requires anchor_repo")
        if not self.anchor_pr_url.strip():
            raise ValueError("preview enablement record requires anchor_pr_url")
        if not self.anchor_head_sha.strip():
            raise ValueError("preview enablement record requires anchor_head_sha")
        if not self.updated_at.strip():
            raise ValueError("preview enablement record requires updated_at")
        if self.request_metadata_status == "invalid" and not self.request_metadata_error.strip():
            raise ValueError(
                "invalid preview enablement record requires request_metadata_error"
            )
        if self.request_metadata_status != "invalid" and self.request_metadata_error.strip():
            raise ValueError(
                "preview enablement record can only include request_metadata_error when status is invalid"
            )
        if self.request_metadata_status == "valid" and not self.request_metadata_baseline_channel.strip():
            raise ValueError(
                "valid preview enablement record requires request_metadata_baseline_channel"
            )
        if self.request_metadata_status != "valid" and self.request_metadata_baseline_channel.strip():
            raise ValueError(
                "preview enablement record can only include request_metadata_baseline_channel when status is valid"
            )
        if self.request_metadata_status != "valid" and self.request_metadata_companions:
            raise ValueError(
                "preview enablement record can only include request_metadata_companions when status is valid"
            )
        if self.request_metadata_status != "valid" and self.request_metadata_companion_summaries:
            raise ValueError(
                "preview enablement record can only include request_metadata_companion_summaries when status is valid"
            )
        if self.request_metadata_companion_summaries:
            companion_keys = tuple(
                (companion.repo.strip(), companion.pr_number)
                for companion in self.request_metadata_companions
            )
            summary_keys = tuple(
                (summary.repo.strip(), summary.pr_number)
                for summary in self.request_metadata_companion_summaries
            )
            if summary_keys != companion_keys:
                raise ValueError(
                    "preview enablement companion summaries must match request_metadata_companions"
                )
        return self
