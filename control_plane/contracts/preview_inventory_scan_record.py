from typing import Literal
import json

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SerializerFunctionWrapHandler,
    model_serializer,
    model_validator,
)


PreviewInventoryScanStatus = Literal["pass", "fail", "unsupported", "unavailable"]
PREVIEW_INVENTORY_COLLECTION_SOURCE = "service-preview-inventory"
_SCOPED_SOURCE_PREFIX = "preview-inventory-scope-v1:"
_SCOPED_READER_MESSAGE = "Scoped preview inventory; use a current reader for details."


class _ScanSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    product: str
    repository: str
    truncated: bool
    status: PreviewInventoryScanStatus
    error_message: str


class PreviewInventoryScanRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    scan_id: str
    context: str
    scanned_at: str
    source: str
    product: str = ""
    repository: str = ""
    truncated: bool = False
    status: PreviewInventoryScanStatus
    preview_count: int = Field(ge=0)
    preview_slugs: tuple[str, ...] = ()
    error_message: str = ""

    @model_validator(mode="before")
    @classmethod
    def _read_scoped_source(cls, value: object) -> object:
        if (
            isinstance(value, dict)
            and isinstance(source := value.get("source"), str)
            and source.startswith(_SCOPED_SOURCE_PREFIX)
            # Inventory callers control source, but cannot supply the persisted
            # compatibility message. Source alone must never establish scope.
            and value.get("error_message") == _SCOPED_READER_MESSAGE
        ):
            metadata = _ScanSource.model_validate_json(source[len(_SCOPED_SOURCE_PREFIX) :])
            return {**value, **metadata.model_dump()}
        return value

    @model_serializer(mode="wrap")
    def _write_compatible_payload(
        self, handler: SerializerFunctionWrapHandler
    ) -> dict[str, object]:
        # The deployed v1 reader forbids extra keys and recognizes only pass/fail.
        # Keep that payload readable after an image rollback. The existing opaque
        # source string carries versioned scope/outcome metadata for newer readers.
        payload: dict[str, object] = handler(self)
        for field in ("product", "repository", "truncated"):
            payload.pop(field, None)
        if self.product or self.repository or self.truncated or self.status not in {"pass", "fail"}:
            metadata = _ScanSource(
                source=self.source,
                product=self.product,
                repository=self.repository,
                truncated=self.truncated,
                status=self.status,
                error_message=self.error_message,
            )
            payload["source"] = _SCOPED_SOURCE_PREFIX + json.dumps(
                metadata.model_dump(), sort_keys=True, separators=(",", ":")
            )
            payload["status"] = "pass" if self.status == "pass" and not self.truncated else "fail"
            payload["error_message"] = _SCOPED_READER_MESSAGE
        return payload

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
