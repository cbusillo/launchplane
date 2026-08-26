from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.repository_inventory import (
    RepositoryInventoryRecord,
    normalize_utc_timestamp,
    required_decimal_id,
)
from control_plane.workflows.ship import utc_now_timestamp


class RepositoryInventoryConflictError(ValueError):
    """Raised when an append-only repository inventory stream conflicts."""


class RepositoryInventorySequenceError(ValueError):
    """Raised when a repository inventory stream is not contiguous."""


class RepositoryInventoryReadStore(Protocol):
    def list_repository_inventory_records(
        self, *, repository_id: str = "", limit: int | None = None
    ) -> tuple[RepositoryInventoryRecord, ...]: ...


class RepositoryInventoryStore(RepositoryInventoryReadStore, Protocol):
    def write_repository_inventory_record(
        self, record: RepositoryInventoryRecord
    ) -> Literal["written", "replayed"]: ...


RepositoryInventoryLookupStatus = Literal["available", "missing", "ambiguous"]
RepositoryInventoryApplyMode = Literal["dry_run", "apply"]
RepositoryInventoryApplyStatus = Literal["would_apply", "would_replay", "applied", "replayed"]


class RepositoryInventoryReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: RepositoryInventoryLookupStatus
    repository_id: str
    current_record: RepositoryInventoryRecord | None = None
    history_count: int = Field(default=0, ge=0)
    generated_at: str

    @model_validator(mode="after")
    def _validate(self) -> RepositoryInventoryReadModel:
        self.repository_id = required_decimal_id(self.repository_id, "repository_id")
        self.generated_at = normalize_utc_timestamp(self.generated_at, "generated_at")
        if (self.status == "available") != (self.current_record is not None):
            raise ValueError("repository inventory availability must match current_record")
        return self


class RepositoryInventoryApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: RepositoryInventoryApplyMode = "apply"
    expected_current_record_id: str = ""
    record: RepositoryInventoryRecord

    @model_validator(mode="after")
    def _validate(self) -> RepositoryInventoryApplyEnvelope:
        if self.schema_version != 1:
            raise ValueError("Unsupported repository inventory apply schema version.")
        self.expected_current_record_id = self.expected_current_record_id.strip()
        return self


class RepositoryInventoryApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: RepositoryInventoryApplyStatus
    mode: RepositoryInventoryApplyMode
    repository_id: str
    inventory_revision: int
    record_id: str
    inventory_digest: str
    supersedes_record_id: str | None = None
    applied_at: str


@dataclass(frozen=True)
class RepositoryInventoryAppendPlan:
    status: Literal["written", "replayed"]
    current_record: RepositoryInventoryRecord | None


def require_repository_inventory_read_store(record_store: object) -> RepositoryInventoryReadStore:
    if not callable(getattr(record_store, "list_repository_inventory_records", None)):
        raise TypeError("Launchplane record store does not support repository inventory reads")
    return cast(RepositoryInventoryReadStore, record_store)


def require_repository_inventory_store(record_store: object) -> RepositoryInventoryStore:
    read_store = require_repository_inventory_read_store(record_store)
    if not callable(getattr(record_store, "write_repository_inventory_record", None)):
        raise TypeError("Launchplane record store does not support repository inventory writes")
    return cast(RepositoryInventoryStore, read_store)


def get_repository_inventory_read_model(
    *, repository_id: str, store: RepositoryInventoryReadStore
) -> RepositoryInventoryReadModel:
    normalized_id = required_decimal_id(repository_id, "repository_id")
    records = tuple(
        record
        for record in store.list_repository_inventory_records(repository_id=normalized_id)
        if record.repository_id == normalized_id
    )
    if not records:
        return RepositoryInventoryReadModel(
            status="missing", repository_id=normalized_id, generated_at=utc_now_timestamp()
        )
    highest_revision = max(record.inventory_revision for record in records)
    current_records = tuple(
        record for record in records if record.inventory_revision == highest_revision
    )
    if len(current_records) != 1:
        return RepositoryInventoryReadModel(
            status="ambiguous",
            repository_id=normalized_id,
            history_count=len(records),
            generated_at=utc_now_timestamp(),
        )
    return RepositoryInventoryReadModel(
        status="available",
        repository_id=normalized_id,
        current_record=current_records[0],
        history_count=len(records),
        generated_at=utc_now_timestamp(),
    )


def plan_repository_inventory_append(
    *, records: tuple[RepositoryInventoryRecord, ...], record: RepositoryInventoryRecord
) -> RepositoryInventoryAppendPlan:
    stream = tuple(existing for existing in records if existing.repository_id == record.repository_id)
    if not stream:
        if record.inventory_revision != 1 or record.supersedes_record_id:
            raise RepositoryInventorySequenceError(
                "First repository inventory record requires revision 1 and no supersedes_record_id."
            )
        return RepositoryInventoryAppendPlan(status="written", current_record=None)
    highest_revision = max(existing.inventory_revision for existing in stream)
    current = tuple(existing for existing in stream if existing.inventory_revision == highest_revision)
    if len(current) != 1:
        raise RepositoryInventoryConflictError("Repository inventory history has an ambiguous current revision.")
    current_record = current[0]
    same_revision = tuple(
        existing for existing in stream if existing.inventory_revision == record.inventory_revision
    )
    if len(same_revision) > 1:
        raise RepositoryInventoryConflictError("Repository inventory revision is ambiguous.")
    if same_revision:
        if same_revision[0] == record and same_revision[0].inventory_digest == record.inventory_digest:
            return RepositoryInventoryAppendPlan(status="replayed", current_record=current_record)
        raise RepositoryInventoryConflictError(
            "Repository inventory revision already exists with a different payload."
        )
    if record.inventory_revision != current_record.inventory_revision + 1:
        raise RepositoryInventorySequenceError("Repository inventory revision must append contiguously.")
    if record.supersedes_record_id != current_record.record_id:
        raise RepositoryInventorySequenceError(
            "Repository inventory supersedes_record_id must equal the current record ID."
        )
    return RepositoryInventoryAppendPlan(status="written", current_record=current_record)


def apply_repository_inventory(
    *,
    store: RepositoryInventoryReadStore,
    record: RepositoryInventoryRecord,
    expected_current_record_id: str = "",
    mode: RepositoryInventoryApplyMode = "apply",
) -> RepositoryInventoryApplyResult:
    plan = plan_repository_inventory_append(
        records=store.list_repository_inventory_records(repository_id=record.repository_id), record=record
    )
    expected = expected_current_record_id.strip()
    current_id = plan.current_record.record_id if plan.current_record else ""
    if plan.status == "written" and expected != current_id:
        raise RepositoryInventoryConflictError("Expected current repository inventory record does not match.")
    if mode == "apply" and plan.status == "written":
        require_repository_inventory_store(store).write_repository_inventory_record(record)
        status: RepositoryInventoryApplyStatus = "applied"
    elif mode == "apply":
        status = "replayed"
    elif plan.status == "written":
        status = "would_apply"
    else:
        status = "would_replay"
    return RepositoryInventoryApplyResult(
        status=status,
        mode=mode,
        repository_id=record.repository_id,
        inventory_revision=record.inventory_revision,
        record_id=record.record_id,
        inventory_digest=record.inventory_digest,
        supersedes_record_id=record.supersedes_record_id,
        applied_at=record.recorded_at,
    )
