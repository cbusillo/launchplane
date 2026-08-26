from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta, timezone
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

RepositoryInventoryState = Literal["tracked", "retired"]

REPOSITORY_INVENTORY_READ_ACTION = "repository_inventory.read"
REPOSITORY_INVENTORY_WRITE_ACTION = "repository_inventory.write"

_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class RepositoryInventoryRecord(BaseModel):
    """Append-only, inert inventory evidence for one immutable GitHub repository."""

    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str = ""
    repository_id: str
    repository_owner_id: str
    repository: str
    inventory_state: RepositoryInventoryState
    inventory_revision: int = Field(ge=1)
    recorded_at: str
    source: str
    reason: str
    supersedes_record_id: str | None = None
    inventory_digest: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> RepositoryInventoryRecord:
        if self.schema_version != 1:
            raise ValueError("Unsupported repository inventory schema version.")
        self.repository_id = required_decimal_id(self.repository_id, "repository_id")
        self.repository_owner_id = required_decimal_id(
            self.repository_owner_id, "repository_owner_id"
        )
        self.repository = normalize_repository(self.repository, "repository")
        self.recorded_at = normalize_utc_timestamp(self.recorded_at, "recorded_at")
        self.source = required_token(self.source, "source")
        self.reason = required_token(self.reason, "reason")
        if self.supersedes_record_id is not None:
            self.supersedes_record_id = required_token(
                self.supersedes_record_id, "supersedes_record_id"
            )
        expected_record_id = build_repository_inventory_record_id(
            repository_id=self.repository_id,
            inventory_revision=self.inventory_revision,
        )
        if self.record_id:
            self.record_id = required_token(self.record_id, "record_id")
            if self.record_id != expected_record_id:
                raise ValueError(
                    "repository inventory record_id must include repository_id and inventory_revision"
                )
        else:
            self.record_id = expected_record_id
        expected_digest = repository_inventory_digest(self)
        if self.inventory_digest:
            self.inventory_digest = normalize_sha256(self.inventory_digest, "inventory_digest")
            if self.inventory_digest != expected_digest:
                raise ValueError("repository inventory inventory_digest does not match payload")
        else:
            self.inventory_digest = expected_digest
        return self


def build_repository_inventory_record_id(*, repository_id: str, inventory_revision: int) -> str:
    return f"repository-inventory-{required_decimal_id(repository_id, 'repository_id')}-r{inventory_revision}"


def repository_inventory_digest(record: RepositoryInventoryRecord) -> str:
    payload = {
        "schema_version": record.schema_version,
        "record_id": record.record_id,
        "repository_id": record.repository_id,
        "repository_owner_id": record.repository_owner_id,
        "repository": record.repository,
        "inventory_state": record.inventory_state,
        "inventory_revision": record.inventory_revision,
        "recorded_at": record.recorded_at,
        "source": record.source,
        "reason": record.reason,
        "supersedes_record_id": record.supersedes_record_id,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def required_token(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"repository inventory {label} is required")
    return normalized


def required_decimal_id(value: str, label: str) -> str:
    normalized = required_token(value, label)
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise ValueError(f"repository inventory {label} requires a positive numeric ID")
    return normalized


def normalize_repository(value: str, label: str) -> str:
    normalized = required_token(value, label).lower()
    owner, separator, name = normalized.partition("/")
    if not separator or not owner or not name or "/" in name:
        raise ValueError(f"repository inventory {label} must be a GitHub owner/name")
    return normalized


def normalize_utc_timestamp(value: str, label: str) -> str:
    normalized = required_token(value, label)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"repository inventory {label} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"repository inventory {label} requires a timezone-aware UTC timestamp")
    if parsed.utcoffset() != timedelta(0):
        raise ValueError(f"repository inventory {label} must be UTC")
    return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def normalize_sha256(value: str, label: str) -> str:
    normalized = required_token(value, label).lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"repository inventory {label} must be a lowercase SHA-256")
    return normalized
