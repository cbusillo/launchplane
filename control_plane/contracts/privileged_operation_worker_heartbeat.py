from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


PRIVILEGED_OPERATION_WORKER_KIND = "privileged-operation"
PRIVILEGED_OPERATION_WORKER_HEARTBEAT_RETENTION_SECONDS = 7 * 24 * 60 * 60
PRIVILEGED_OPERATION_WORKER_HEARTBEAT_FUTURE_SKEW_SECONDS = 60
PRIVILEGED_OPERATION_WORKER_HEARTBEAT_MIN_FRESHNESS_SECONDS = 120
PRIVILEGED_OPERATION_WORKER_HEARTBEAT_MAX_FRESHNESS_SECONDS = 900

_SHA256_PATTERN = re.compile(r"^[a-f0-9]{64}$")
_IMMUTABLE_IMAGE_REFERENCE_PATTERN = re.compile(r"^[^\s@]+@sha256:[a-f0-9]{64}$")


def privileged_operation_worker_identity_sha256(runtime_identity: str) -> str:
    normalized_identity = runtime_identity.strip().lower()
    if not normalized_identity:
        raise ValueError("privileged-operation worker runtime identity is required")
    return hashlib.sha256(
        f"privileged-operation-worker:{normalized_identity}".encode("utf-8")
    ).hexdigest()


def normalize_privileged_operation_worker_image_reference(image_reference: str) -> str:
    normalized_reference = image_reference.strip()
    if _IMMUTABLE_IMAGE_REFERENCE_PATTERN.fullmatch(normalized_reference):
        return normalized_reference
    return ""


def privileged_operation_worker_heartbeat_freshness_seconds(poll_interval_seconds: int) -> int:
    return min(
        PRIVILEGED_OPERATION_WORKER_HEARTBEAT_MAX_FRESHNESS_SECONDS,
        max(
            PRIVILEGED_OPERATION_WORKER_HEARTBEAT_MIN_FRESHNESS_SECONDS,
            4 * poll_interval_seconds,
        ),
    )


class PrivilegedOperationWorkerHeartbeatRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    worker_kind: Literal["privileged-operation"] = "privileged-operation"
    worker_identity_sha256: str
    image_reference: str = ""
    poll_interval_seconds: int = Field(ge=1, le=300)
    last_poll_succeeded_at: str

    @field_validator("worker_identity_sha256", mode="after")
    @classmethod
    def _validate_sha256(cls, value: str) -> str:
        normalized_value = value.strip().lower()
        if not _SHA256_PATTERN.fullmatch(normalized_value):
            raise ValueError("privileged-operation worker identity must be a sha256 digest")
        return normalized_value

    @field_validator("image_reference", mode="after")
    @classmethod
    def _validate_image_reference(cls, value: str) -> str:
        normalized_value = value.strip()
        if normalized_value and not normalize_privileged_operation_worker_image_reference(
            normalized_value
        ):
            raise ValueError(
                "privileged-operation worker image must be an immutable repository@sha256 reference"
            )
        return normalized_value

    @field_validator("last_poll_succeeded_at", mode="after")
    @classmethod
    def _validate_timestamp(cls, value: str) -> str:
        normalized_value = value.strip()
        try:
            parsed_value = datetime.fromisoformat(normalized_value)
        except ValueError as error:
            raise ValueError(
                "privileged-operation worker heartbeat timestamp is invalid"
            ) from error
        if parsed_value.tzinfo is None:
            raise ValueError("privileged-operation worker heartbeat timestamp requires a timezone")
        return parsed_value.astimezone(timezone.utc).isoformat()
