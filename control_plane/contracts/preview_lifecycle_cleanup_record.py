from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PreviewLifecycleCleanupStatus = Literal["report_only", "pass", "fail", "blocked"]
PreviewLifecycleCleanupResultStatus = Literal["planned", "destroyed", "failed", "blocked"]


class PreviewLifecycleCleanupResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_slug: str
    anchor_repo: str = ""
    anchor_pr_number: int | None = Field(default=None, ge=1)
    status: PreviewLifecycleCleanupResultStatus
    application_name: str = ""
    application_id: str = ""
    preview_url: str = ""
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_result(self) -> "PreviewLifecycleCleanupResult":
        if not self.preview_slug.strip():
            raise ValueError("preview lifecycle cleanup result requires preview_slug")
        return self


class PreviewLifecycleCleanupRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    cleanup_id: str
    product: str
    context: str
    plan_id: str
    inventory_scan_id: str = ""
    requested_at: str
    source: str
    apply: bool = False
    status: PreviewLifecycleCleanupStatus
    planned_slugs: tuple[str, ...] = ()
    destroyed_slugs: tuple[str, ...] = ()
    failed_slugs: tuple[str, ...] = ()
    blocked_slugs: tuple[str, ...] = ()
    results: tuple[PreviewLifecycleCleanupResult, ...] = ()
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "PreviewLifecycleCleanupRecord":
        if not self.cleanup_id.strip():
            raise ValueError("preview lifecycle cleanup requires cleanup_id")
        if not self.product.strip():
            raise ValueError("preview lifecycle cleanup requires product")
        if not self.context.strip():
            raise ValueError("preview lifecycle cleanup requires context")
        if not self.plan_id.strip():
            raise ValueError("preview lifecycle cleanup requires plan_id")
        if not self.requested_at.strip():
            raise ValueError("preview lifecycle cleanup requires requested_at")
        if not self.source.strip():
            raise ValueError("preview lifecycle cleanup requires source")
        return self


def build_preview_lifecycle_cleanup_id(*, context_name: str, requested_at: str) -> str:
    normalized_timestamp = (
        requested_at.strip()
        .replace(":", "")
        .replace("-", "")
        .replace(".", "")
        .replace("+", "")
        .replace("Z", "Z")
    )
    return f"preview-lifecycle-cleanup-{context_name}-{normalized_timestamp}"
