from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PreviewPrFeedbackStatus = Literal[
    "pending",
    "ready",
    "destroyed",
    "failed",
    "cleanup_failed",
    "unsupported",
    "cleared",
]
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
    remediates_feedback_id: str = ""
    remediation_reason: str = ""
    remediation_issue_reference: str = ""
    remediation_plan_sha256: str = ""
    remediation_actor: str = ""

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
        remediation_values = (
            self.remediates_feedback_id,
            self.remediation_reason,
            self.remediation_issue_reference,
            self.remediation_plan_sha256,
            self.remediation_actor,
        )
        if any(value.strip() for value in remediation_values):
            if not all(value.strip() for value in remediation_values):
                raise ValueError("preview PR feedback remediation audit fields must be complete")
            if self.status not in {"cleared", "destroyed"}:
                raise ValueError("preview PR feedback remediation requires a terminal status")
            digest = self.remediation_plan_sha256.strip().lower()
            if len(digest) != 64 or any(
                character not in "0123456789abcdef" for character in digest
            ):
                raise ValueError(
                    "preview PR feedback remediation_plan_sha256 must be a SHA-256 digest"
                )
            self.remediation_plan_sha256 = digest
        return self


def build_preview_pr_feedback_id(
    *, context_name: str, anchor_pr_number: int, requested_at: str
) -> str:
    normalized_timestamp = (
        requested_at.strip()
        .replace(":", "")
        .replace("-", "")
        .replace(".", "")
        .replace("+", "")
        .replace("Z", "Z")
    )
    return f"preview-pr-feedback-{context_name}-pr-{anchor_pr_number}-{normalized_timestamp}"
