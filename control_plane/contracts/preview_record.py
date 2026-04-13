from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PreviewState = Literal[
    "pending",
    "active",
    "failed",
    "paused",
    "teardown_pending",
    "destroyed",
]


class PreviewRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    preview_id: str
    context: str
    anchor_repo: str
    anchor_pr_number: int = Field(ge=1)
    anchor_pr_url: str
    preview_label: str
    canonical_url: str
    state: PreviewState
    created_at: str
    updated_at: str
    eligible_at: str
    paused_at: str = ""
    destroy_after: str = ""
    destroyed_at: str = ""
    destroy_reason: str = ""
    active_generation_id: str = ""
    serving_generation_id: str = ""
    latest_generation_id: str = ""
    latest_manifest_fingerprint: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "PreviewRecord":
        if not self.preview_id.strip():
            raise ValueError("preview record requires preview_id")
        if not self.context.strip():
            raise ValueError("preview record requires context")
        if not self.anchor_repo.strip():
            raise ValueError("preview record requires anchor_repo")
        if not self.anchor_pr_url.strip():
            raise ValueError("preview record requires anchor_pr_url")
        if not self.preview_label.strip():
            raise ValueError("preview record requires preview_label")
        if not self.canonical_url.strip():
            raise ValueError("preview record requires canonical_url")
        if not self.created_at.strip():
            raise ValueError("preview record requires created_at")
        if not self.updated_at.strip():
            raise ValueError("preview record requires updated_at")
        if not self.eligible_at.strip():
            raise ValueError("preview record requires eligible_at")
        return self
