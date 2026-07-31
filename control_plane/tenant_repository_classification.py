from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.tenant_merge_eligibility import (
    TenantRepositoryClassificationLookupStatus,
    TenantRepositoryClassificationRecord,
    _normalize_utc_timestamp,
    _required_decimal_id,
)
from control_plane.workflows.ship import utc_now_timestamp


class TenantRepositoryClassificationConflictError(ValueError):
    """Raised when immutable repository classification history is replayed differently or sequence conflicts occur."""


class TenantRepositoryClassificationSequenceError(ValueError):
    """Raised when repository classification revision sequence is invalid."""


class TenantRepositoryClassificationReadStore(Protocol):
    def list_tenant_repository_classification_records(
        self,
        *,
        repository_id: str = "",
        limit: int | None = None,
    ) -> tuple[TenantRepositoryClassificationRecord, ...]: ...


class TenantRepositoryClassificationStore(TenantRepositoryClassificationReadStore, Protocol):
    def write_tenant_repository_classification_record(
        self, record: TenantRepositoryClassificationRecord
    ) -> Literal["written", "replayed"]: ...

    def read_tenant_repository_classification_record(
        self, record_id: str
    ) -> TenantRepositoryClassificationRecord: ...


class TenantRepositoryClassificationReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: TenantRepositoryClassificationLookupStatus
    repository_id: str
    current_record: TenantRepositoryClassificationRecord | None = None
    history_count: int = Field(default=0, ge=0)
    generated_at: str

    @model_validator(mode="after")
    def _validate_read_model(self) -> TenantRepositoryClassificationReadModel:
        if self.schema_version != 1:
            raise ValueError(
                "Unsupported tenant repository classification read model schema version."
            )
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.generated_at = _normalize_utc_timestamp(self.generated_at, "generated_at")
        if self.status == "missing" and self.current_record is not None:
            raise ValueError("missing classification read model cannot include current_record")
        if self.status in {"ambiguous", "unknown"} and self.current_record is not None:
            raise ValueError("unavailable classification read model cannot include current_record")
        if self.status == "available" and self.current_record is None:
            raise ValueError("available classification read model requires current_record")
        return self


TenantRepositoryClassificationApplyMode = Literal["dry_run", "apply"]


class TenantRepositoryClassificationApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: TenantRepositoryClassificationApplyMode = "apply"
    expected_current_record_id: str = ""
    record: TenantRepositoryClassificationRecord

    @model_validator(mode="after")
    def _validate_envelope(self) -> TenantRepositoryClassificationApplyEnvelope:
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant repository classification apply schema version.")
        self.expected_current_record_id = self.expected_current_record_id.strip()
        return self


TenantRepositoryClassificationApplyStatus = Literal[
    "would_apply", "would_replay", "applied", "replayed"
]
TenantRepositoryClassificationAppendStatus = Literal["written", "replayed"]


@dataclass(frozen=True)
class TenantRepositoryClassificationAppendPlan:
    status: TenantRepositoryClassificationAppendStatus
    current_record: TenantRepositoryClassificationRecord | None


class TenantRepositoryClassificationApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: TenantRepositoryClassificationApplyStatus
    mode: TenantRepositoryClassificationApplyMode
    repository_id: str
    classification_revision: int
    record_id: str
    classification_digest: str
    supersedes_record_id: str | None = None
    applied_at: str

    @model_validator(mode="after")
    def _validate_result(self) -> TenantRepositoryClassificationApplyResult:
        if self.schema_version != 1:
            raise ValueError(
                "Unsupported tenant repository classification apply result schema version."
            )
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.record_id = self.record_id.strip()
        self.classification_digest = self.classification_digest.strip()
        self.applied_at = _normalize_utc_timestamp(self.applied_at, "applied_at")
        if self.supersedes_record_id is not None:
            self.supersedes_record_id = self.supersedes_record_id.strip()
        return self


def require_tenant_repository_classification_store(
    record_store: object,
) -> TenantRepositoryClassificationStore:
    required_methods = (
        "list_tenant_repository_classification_records",
        "write_tenant_repository_classification_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support tenant repository classification writes: "
            f"{missing_summary}"
        )
    return cast(TenantRepositoryClassificationStore, record_store)


def require_tenant_repository_classification_read_store(
    record_store: object,
) -> TenantRepositoryClassificationReadStore:
    required_methods = ("list_tenant_repository_classification_records",)
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support tenant repository classification reads: "
            f"{missing_summary}"
        )
    return cast(TenantRepositoryClassificationReadStore, record_store)


def get_tenant_repository_classification_read_model(
    *,
    repository_id: str,
    store: TenantRepositoryClassificationReadStore,
) -> TenantRepositoryClassificationReadModel:
    normalized_repo_id = _required_decimal_id(repository_id, "repository_id")
    records = store.list_tenant_repository_classification_records(repository_id=normalized_repo_id)
    matching_records = tuple(r for r in records if r.repository_id == normalized_repo_id)
    if not matching_records:
        return TenantRepositoryClassificationReadModel(
            status="missing",
            repository_id=normalized_repo_id,
            current_record=None,
            history_count=0,
            generated_at=utc_now_timestamp(),
        )
    highest_revision = max(record.classification_revision for record in matching_records)
    current_records = tuple(
        record for record in matching_records if record.classification_revision == highest_revision
    )
    if len(current_records) != 1:
        return TenantRepositoryClassificationReadModel(
            status="ambiguous",
            repository_id=normalized_repo_id,
            current_record=None,
            history_count=len(matching_records),
            generated_at=utc_now_timestamp(),
        )
    current_record = current_records[0]
    return TenantRepositoryClassificationReadModel(
        status="available",
        repository_id=normalized_repo_id,
        current_record=current_record,
        history_count=len(matching_records),
        generated_at=utc_now_timestamp(),
    )


def apply_tenant_repository_classification(
    *,
    store: TenantRepositoryClassificationReadStore,
    record: TenantRepositoryClassificationRecord,
    expected_current_record_id: str = "",
    mode: TenantRepositoryClassificationApplyMode = "apply",
) -> TenantRepositoryClassificationApplyResult:
    normalized_expected = expected_current_record_id.strip()
    records = store.list_tenant_repository_classification_records(
        repository_id=record.repository_id
    )
    plan = plan_tenant_repository_classification_append(records=records, record=record)
    if plan.status == "written":
        if plan.current_record is None and normalized_expected:
            raise TenantRepositoryClassificationConflictError(
                f"Expected current classification record ID '{normalized_expected}' does not match current state: repository has no existing classification record."
            )
        if plan.current_record is not None and normalized_expected != plan.current_record.record_id:
            raise TenantRepositoryClassificationConflictError(
                f"Expected current classification record ID '{normalized_expected}' does not match active current record ID '{plan.current_record.record_id}'."
            )

    if mode == "apply":
        if plan.status == "written":
            write_store = require_tenant_repository_classification_store(store)
            write_store.write_tenant_repository_classification_record(record)
            final_status: TenantRepositoryClassificationApplyStatus = "applied"
        else:
            final_status = "replayed"
    else:
        final_status = "would_apply" if plan.status == "written" else "would_replay"

    return TenantRepositoryClassificationApplyResult(
        status=final_status,
        mode=mode,
        repository_id=record.repository_id,
        classification_revision=record.classification_revision,
        record_id=record.record_id,
        classification_digest=record.classification_digest,
        supersedes_record_id=record.supersedes_record_id,
        applied_at=record.classified_at,
    )


def plan_tenant_repository_classification_append(
    *,
    records: tuple[TenantRepositoryClassificationRecord, ...],
    record: TenantRepositoryClassificationRecord,
) -> TenantRepositoryClassificationAppendPlan:
    matching_records = tuple(
        existing for existing in records if existing.repository_id == record.repository_id
    )
    if not matching_records:
        if record.classification_revision != 1:
            raise TenantRepositoryClassificationSequenceError(
                f"First classification record for repository {record.repository_id} must have revision 1, got {record.classification_revision}."
            )
        if record.supersedes_record_id:
            raise TenantRepositoryClassificationSequenceError(
                "First classification record for a repository cannot set supersedes_record_id."
            )
        return TenantRepositoryClassificationAppendPlan(
            status="written",
            current_record=None,
        )

    highest_revision = max(existing.classification_revision for existing in matching_records)
    current_records = tuple(
        existing
        for existing in matching_records
        if existing.classification_revision == highest_revision
    )
    if len(current_records) != 1:
        raise TenantRepositoryClassificationConflictError(
            "Tenant repository classification history has multiple records at the highest revision."
        )
    current_record = current_records[0]
    same_revision_records = tuple(
        existing
        for existing in matching_records
        if existing.classification_revision == record.classification_revision
    )
    if len(same_revision_records) > 1:
        raise TenantRepositoryClassificationConflictError(
            "Tenant repository classification revision is ambiguous."
        )
    if same_revision_records:
        existing = same_revision_records[0]
        if existing.classification_digest == record.classification_digest and existing == record:
            return TenantRepositoryClassificationAppendPlan(
                status="replayed",
                current_record=current_record,
            )
        raise TenantRepositoryClassificationConflictError(
            "Tenant repository classification revision already exists with different payload."
        )
    if record.classification_revision != current_record.classification_revision + 1:
        raise TenantRepositoryClassificationSequenceError(
            f"Next classification revision must be {current_record.classification_revision + 1}, got {record.classification_revision}."
        )
    if record.supersedes_record_id != current_record.record_id:
        raise TenantRepositoryClassificationSequenceError(
            f"Classification record supersedes_record_id must be '{current_record.record_id}', got '{record.supersedes_record_id}'."
        )
    return TenantRepositoryClassificationAppendPlan(
        status="written",
        current_record=current_record,
    )
