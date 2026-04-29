from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PreviewLifecyclePlanStatus = Literal["pass", "missing_inventory", "fail"]


class PreviewLifecycleDesiredPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_slug: str
    anchor_repo: str = ""
    anchor_pr_number: int | None = Field(default=None, ge=1)
    anchor_pr_url: str = ""
    head_sha: str = ""

    @model_validator(mode="after")
    def _validate_preview(self) -> "PreviewLifecycleDesiredPreview":
        if not self.preview_slug.strip():
            raise ValueError("preview lifecycle desired preview requires preview_slug")
        return self


class PreviewLifecyclePlanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    plan_id: str
    product: str
    context: str
    planned_at: str
    source: str
    status: PreviewLifecyclePlanStatus
    inventory_scan_id: str = ""
    desired_previews: tuple[PreviewLifecycleDesiredPreview, ...] = ()
    desired_slugs: tuple[str, ...] = ()
    actual_slugs: tuple[str, ...] = ()
    keep_slugs: tuple[str, ...] = ()
    orphaned_slugs: tuple[str, ...] = ()
    missing_slugs: tuple[str, ...] = ()
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "PreviewLifecyclePlanRecord":
        if not self.plan_id.strip():
            raise ValueError("preview lifecycle plan requires plan_id")
        if not self.product.strip():
            raise ValueError("preview lifecycle plan requires product")
        if not self.context.strip():
            raise ValueError("preview lifecycle plan requires context")
        if not self.planned_at.strip():
            raise ValueError("preview lifecycle plan requires planned_at")
        if not self.source.strip():
            raise ValueError("preview lifecycle plan requires source")
        desired_from_previews = tuple(preview.preview_slug for preview in self.desired_previews)
        if desired_from_previews and desired_from_previews != self.desired_slugs:
            raise ValueError("preview lifecycle plan desired_slugs must match desired_previews")
        if self.status == "pass" and not self.inventory_scan_id.strip():
            raise ValueError("passing preview lifecycle plan requires inventory_scan_id")
        return self


def build_preview_lifecycle_plan_id(*, context_name: str, planned_at: str) -> str:
    normalized_timestamp = (
        planned_at.strip()
        .replace(":", "")
        .replace("-", "")
        .replace(".", "")
        .replace("+", "")
        .replace("Z", "Z")
    )
    return f"preview-lifecycle-plan-{context_name}-{normalized_timestamp}"
