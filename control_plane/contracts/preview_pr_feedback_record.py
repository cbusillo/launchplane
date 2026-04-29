from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PreviewPrFeedbackStatus = Literal["ready", "destroyed", "failed", "cleanup_failed"]
PreviewPrFeedbackDeliveryStatus = Literal["delivered", "skipped", "failed"]


class PreviewPrFeedbackRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    feedback_id: str
    product: str
    context: str
    source: str
    requested_at: str
    repository: str
    anchor_repo: str
    anchor_pr_number: int = Field(ge=1)
    anchor_pr_url: str
    status: PreviewPrFeedbackStatus
    marker: str
    comment_markdown: str
    preview_url: str = ""
    immutable_image_reference: str = ""
    refresh_image_reference: str = ""
    revision: str = ""
    run_url: str = ""
    failure_summary: str = ""
    delivery_status: PreviewPrFeedbackDeliveryStatus
    delivery_action: str = ""
    comment_id: int = 0
    comment_url: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "PreviewPrFeedbackRecord":
        if not self.feedback_id.strip():
            raise ValueError("preview PR feedback requires feedback_id")
        if not self.product.strip():
            raise ValueError("preview PR feedback requires product")
        if not self.context.strip():
            raise ValueError("preview PR feedback requires context")
        if not self.source.strip():
            raise ValueError("preview PR feedback requires source")
        if not self.requested_at.strip():
            raise ValueError("preview PR feedback requires requested_at")
        if not self.repository.strip():
            raise ValueError("preview PR feedback requires repository")
        if not self.anchor_repo.strip():
            raise ValueError("preview PR feedback requires anchor_repo")
        if not self.anchor_pr_url.strip():
            raise ValueError("preview PR feedback requires anchor_pr_url")
        if not self.marker.strip():
            raise ValueError("preview PR feedback requires marker")
        if not self.comment_markdown.strip():
            raise ValueError("preview PR feedback requires comment_markdown")
        return self


def build_preview_pr_feedback_id(*, context_name: str, anchor_pr_number: int, requested_at: str) -> str:
    normalized_timestamp = (
        requested_at.strip()
        .replace(":", "")
        .replace("-", "")
        .replace(".", "")
        .replace("+", "")
        .replace("Z", "Z")
    )
    return f"preview-pr-feedback-{context_name}-pr-{anchor_pr_number}-{normalized_timestamp}"
