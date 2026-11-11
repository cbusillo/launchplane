from typing import Annotated, Literal
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
_SLEEP_SOURCE_PREFIX = "preview-inventory-scope-v2:"
_EXACT_SLEEP_SOURCE_PREFIX = "preview-inventory-scope-v3:"
_SCOPED_READER_MESSAGE = "Scoped preview inventory; use a current reader for details."


SleepBindingText = Annotated[str, Field(min_length=1, max_length=512, pattern=r"^\S+$")]


class PreviewSleepBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    product: SleepBindingText
    context: SleepBindingText
    instance: SleepBindingText
    preview_id: SleepBindingText
    generation_id: SleepBindingText
    provider_target_id: SleepBindingText
    artifact_id: SleepBindingText
    source_git_ref: SleepBindingText
    image_reference: SleepBindingText


class PreviewSleepServiceState(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    running: bool | None = None
    healthy: bool | None = None


class PreviewSleepServices(BaseModel):
    model_config = ConfigDict(extra="forbid")

    web: PreviewSleepServiceState = PreviewSleepServiceState()
    database: PreviewSleepServiceState = PreviewSleepServiceState()
    script_runner: PreviewSleepServiceState = PreviewSleepServiceState()


class PreviewSleepReceipts(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sleep_present: bool | None = None
    wake_present: bool | None = None
    persisted_state: Literal["observed", "missing", "invalid", "unavailable", "unknown"] = "unknown"
    identity_agrees: bool | None = None
    mode_agrees: bool | None = None
    sleep_agrees: bool | None = None
    wake_agrees: bool | None = None


class PreviewSleepEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    coverage: Literal["complete", "partial", "unavailable", "not_attempted", "legacy"] = "legacy"
    completed_at: str = Field(default="", max_length=64)
    freshness: Literal["fresh", "stale", "invalid", "unknown"] = "unknown"
    binding: PreviewSleepBinding | None = None
    gateway_mode: Literal["awake", "asleep", "unknown"] = "unknown"
    activity_requested: bool | None = None
    active_connections: int | None = Field(default=None, ge=0)
    services: PreviewSleepServices = PreviewSleepServices()
    receipts: PreviewSleepReceipts = PreviewSleepReceipts()
    phase: Literal["awake", "asleep", "stopping", "waking", "inconsistent", "unknown"] = "unknown"
    current_binding: Literal["current", "changed", "unknown"] = "unknown"


class PreviewSleepObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    slug: str
    state: Literal["awake", "asleep", "cannot_sleep", "unknown"]
    reason: str = ""
    evidence: PreviewSleepEvidence | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class _ScanSource(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: str
    product: str
    repository: str
    truncated: bool
    status: PreviewInventoryScanStatus
    error_message: str
    sleep_observations: tuple[PreviewSleepObservation, ...] = ()


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
    sleep_observations: tuple[PreviewSleepObservation, ...] = ()

    @model_validator(mode="before")
    @classmethod
    def _read_scoped_source(cls, value: object) -> object:
        if (
            isinstance(value, dict)
            and isinstance(source := value.get("source"), str)
            and source.startswith(
                (_SCOPED_SOURCE_PREFIX, _SLEEP_SOURCE_PREFIX, _EXACT_SLEEP_SOURCE_PREFIX)
            )
            # Inventory callers control source, but cannot supply the persisted
            # compatibility message. Source alone must never establish scope.
            and value.get("error_message") == _SCOPED_READER_MESSAGE
        ):
            metadata = _ScanSource.model_validate_json(source.partition(":")[2])
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
        for field in ("product", "repository", "truncated", "sleep_observations"):
            payload.pop(field, None)
        if (
            self.product
            or self.repository
            or self.truncated
            or self.sleep_observations
            or self.status not in {"pass", "fail"}
        ):
            metadata = _ScanSource(
                source=self.source,
                product=self.product,
                repository=self.repository,
                truncated=self.truncated,
                status=self.status,
                error_message=self.error_message,
                sleep_observations=self.sleep_observations,
            )
            metadata_payload = metadata.model_dump()
            prefix = _SLEEP_SOURCE_PREFIX if self.sleep_observations else _SCOPED_SOURCE_PREFIX
            if any(item.evidence is not None for item in self.sleep_observations):
                prefix = _EXACT_SLEEP_SOURCE_PREFIX
            if not self.sleep_observations:
                metadata_payload.pop("sleep_observations")
            payload["source"] = prefix + json.dumps(
                metadata_payload, sort_keys=True, separators=(",", ":")
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
        sleep_slugs = [item.slug for item in self.sleep_observations]
        if len(sleep_slugs) != len(set(sleep_slugs)) or set(sleep_slugs) - set(self.preview_slugs):
            raise ValueError("Sleep observations must identify unique observed previews.")
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
