from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


PreviewInventoryScanStatus = Literal["pass", "fail"]


class PreviewInventoryScanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    scan_id: str
    context: str
    scanned_at: str
    source: str
    status: PreviewInventoryScanStatus
    preview_count: int = Field(ge=0)
    preview_slugs: tuple[str, ...] = ()
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "PreviewInventoryScanRecord":
        if not self.scan_id.strip():
            raise ValueError("preview inventory scan record requires scan_id")
        if not self.context.strip():
            raise ValueError("preview inventory scan record requires context")
        if not self.scanned_at.strip():
            raise ValueError("preview inventory scan record requires scanned_at")
        if not self.source.strip():
            raise ValueError("preview inventory scan record requires source")
        if self.preview_count != len(self.preview_slugs):
            raise ValueError("preview inventory scan record preview_count must match preview_slugs")
        return self


def build_preview_inventory_scan_id(*, context_name: str, scanned_at: str) -> str:
    normalized_timestamp = (
        scanned_at.strip()
        .replace(":", "")
        .replace("-", "")
        .replace(".", "")
        .replace("+", "")
        .replace("Z", "Z")
    )
    return f"preview-inventory-scan-{context_name}-{normalized_timestamp}"
