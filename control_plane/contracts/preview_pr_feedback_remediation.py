from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PreviewPrFeedbackRemediationMode = Literal["dry_run", "apply"]
PreviewPrFeedbackTerminalStatus = Literal["cleared", "destroyed"]


class PreviewPrFeedbackRemediationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    repository: str
    anchor_pr_number: int = Field(ge=1)
    anchor_pr_url: str
    desired_status: PreviewPrFeedbackTerminalStatus
    reason: str
    issue_reference: str
    current_feedback_id: str
    current_feedback_status: str
    current_comment_id: int = Field(ge=1)
    current_comment_url: str
    current_comment_body_sha256: str
    planned_delivery_action: Literal["delete_comment", "update_comment"]
    plan_sha256: str

    @model_validator(mode="after")
    def _validate_plan(self) -> "PreviewPrFeedbackRemediationPlan":
        for field_name in (
            "product",
            "context",
            "repository",
            "anchor_pr_url",
            "reason",
            "issue_reference",
            "current_feedback_id",
            "current_comment_url",
        ):
            if not getattr(self, field_name).strip():
                raise ValueError(f"preview PR feedback remediation requires {field_name}")
        for field_name in ("current_comment_body_sha256", "plan_sha256"):
            value = getattr(self, field_name).strip().lower()
            if len(value) != 64 or any(character not in "0123456789abcdef" for character in value):
                raise ValueError(
                    f"preview PR feedback remediation {field_name} must be a SHA-256 digest"
                )
            setattr(self, field_name, value)
        return self
