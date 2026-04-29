from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.preview_lifecycle_plan_record import PreviewLifecycleDesiredPreview


PreviewDesiredStateStatus = Literal["pass", "fail"]


class PreviewDesiredStateRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    desired_state_id: str
    product: str
    context: str
    source: str
    discovered_at: str
    repository: str
    label: str
    anchor_repo: str
    preview_slug_prefix: str = "pr-"
    status: PreviewDesiredStateStatus
    desired_count: int = Field(ge=0)
    desired_previews: tuple[PreviewLifecycleDesiredPreview, ...] = ()
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "PreviewDesiredStateRecord":
        if not self.desired_state_id.strip():
            raise ValueError("preview desired state requires desired_state_id")
        if not self.product.strip():
            raise ValueError("preview desired state requires product")
        if not self.context.strip():
            raise ValueError("preview desired state requires context")
        if not self.source.strip():
            raise ValueError("preview desired state requires source")
        if not self.discovered_at.strip():
            raise ValueError("preview desired state requires discovered_at")
        if not self.repository.strip():
            raise ValueError("preview desired state requires repository")
        if not self.label.strip():
            raise ValueError("preview desired state requires label")
        if not self.anchor_repo.strip():
            raise ValueError("preview desired state requires anchor_repo")
        if not self.preview_slug_prefix.strip():
            raise ValueError("preview desired state requires preview_slug_prefix")
        if self.desired_count != len(self.desired_previews):
            raise ValueError("preview desired state desired_count must match desired_previews")
        if self.status == "pass" and self.error_message.strip():
            raise ValueError("passing preview desired state must not include error_message")
        if self.status == "fail" and not self.error_message.strip():
            raise ValueError("failed preview desired state requires error_message")
        return self


def build_preview_desired_state_id(*, context_name: str, discovered_at: str) -> str:
    normalized_timestamp = (
        discovered_at.strip()
        .replace(":", "")
        .replace("-", "")
        .replace(".", "")
        .replace("+", "")
        .replace("Z", "Z")
    )
    return f"preview-desired-state-{context_name}-{normalized_timestamp}"
