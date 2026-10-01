from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.repository_human_admission import (
    RepositoryHumanRolePolicyRecord,
    _normalize_sha256,
    _required_decimal_id,
    _required_token,
)
from control_plane.workflows.ship import utc_now_timestamp


class RepositoryHumanRolePolicyConflictError(ValueError):
    """Raised when repository human role-policy history is replayed differently or has conflicting authority."""


class RepositoryHumanRolePolicySequenceError(ValueError):
    """Raised when repository human role-policy revision sequence is invalid."""


class RepositoryHumanRolePolicyReadStore(Protocol):
    def list_repository_human_role_policy_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RepositoryHumanRolePolicyRecord, ...]: ...


class RepositoryHumanRolePolicyStore(RepositoryHumanRolePolicyReadStore, Protocol):
    def write_repository_human_role_policy_record(
        self,
        record: RepositoryHumanRolePolicyRecord,
    ) -> Literal["written", "replayed"]: ...

    def read_repository_human_role_policy_record(
        self,
        record_id: str,
    ) -> RepositoryHumanRolePolicyRecord: ...


RepositoryHumanRolePolicyAppendStatus = Literal["written", "replayed"]
RepositoryHumanRolePolicyReadStatus = Literal["available", "missing", "ambiguous"]
RepositoryHumanRolePolicyApplyMode = Literal["dry_run", "apply"]
RepositoryHumanRolePolicyApplyStatus = Literal[
    "would_apply",
    "would_replay",
    "applied",
    "replayed",
]


@dataclass(frozen=True)
class RepositoryHumanRolePolicyAppendPlan:
    status: RepositoryHumanRolePolicyAppendStatus
    current_record: RepositoryHumanRolePolicyRecord | None
    superseded_current_record: RepositoryHumanRolePolicyRecord | None = None


class RepositoryHumanRolePolicyReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: RepositoryHumanRolePolicyReadStatus
    repository_id: str
    product: str
    context: str
    current_record: RepositoryHumanRolePolicyRecord | None = None
    history_count: int = Field(default=0, ge=0)
    generated_at: str

    @model_validator(mode="after")
    def _validate_read_model(self) -> "RepositoryHumanRolePolicyReadModel":
        if self.schema_version != 1:
            raise ValueError("Unsupported repository human role-policy read model schema version.")
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.product = _required_token(self.product, "product")
        self.context = _required_token(self.context, "context")
        self.generated_at = _normalize_utc_timestamp(self.generated_at, "generated_at")
        if self.status in {"missing", "ambiguous"} and self.current_record is not None:
            raise ValueError(
                "unavailable repository human role-policy read model cannot include current_record"
            )
        if self.status == "available" and self.current_record is None:
            raise ValueError(
                "available repository human role-policy read model requires current_record"
            )
        return self


class RepositoryHumanRolePolicyApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: RepositoryHumanRolePolicyApplyMode = "apply"
    expected_current_record_id: str = ""
    expected_current_role_policy_digest: str = ""
    record: RepositoryHumanRolePolicyRecord

    @model_validator(mode="after")
    def _validate_envelope(self) -> "RepositoryHumanRolePolicyApplyEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported repository human role-policy apply schema version.")
        self.expected_current_record_id = self.expected_current_record_id.strip()
        self.expected_current_role_policy_digest = (
            self.expected_current_role_policy_digest.strip().lower()
        )
        return self


class RepositoryHumanRolePolicyApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: RepositoryHumanRolePolicyApplyStatus
    mode: RepositoryHumanRolePolicyApplyMode
    repository_id: str
    product: str
    context: str
    role_policy_revision: int = Field(ge=1)
    record_id: str
    role_policy_digest: str
    supersedes_record_id: str | None = None
    effective_at: str

    @model_validator(mode="after")
    def _validate_result(self) -> "RepositoryHumanRolePolicyApplyResult":
        if self.schema_version != 1:
            raise ValueError(
                "Unsupported repository human role-policy apply result schema version."
            )
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.product = _required_token(self.product, "product")
        self.context = _required_token(self.context, "context")
        self.record_id = _required_token(self.record_id, "record_id")
        self.role_policy_digest = _normalize_sha256(
            self.role_policy_digest,
            "role_policy_digest",
        )
        if self.supersedes_record_id is not None:
            self.supersedes_record_id = _required_token(
                self.supersedes_record_id,
                "supersedes_record_id",
            )
        self.effective_at = _normalize_utc_timestamp(self.effective_at, "effective_at")
        return self


def require_repository_human_role_policy_read_store(
    record_store: object,
) -> RepositoryHumanRolePolicyReadStore:
    required_methods = ("list_repository_human_role_policy_records",)
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support repository human role-policy reads: "
            f"{missing_summary}"
        )
    return cast(RepositoryHumanRolePolicyReadStore, record_store)


def require_repository_human_role_policy_store(
    record_store: object,
) -> RepositoryHumanRolePolicyStore:
    required_methods = (
        "list_repository_human_role_policy_records",
        "write_repository_human_role_policy_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support repository human role-policy writes: "
            f"{missing_summary}"
        )
    return cast(RepositoryHumanRolePolicyStore, record_store)


def normalize_repository_human_role_policy_lookup_scope(
    *, repository_id: str, product: str, context: str
) -> tuple[str, str, str]:
    return (
        _required_decimal_id(repository_id, "repository_id"),
        _required_token(product, "product"),
        _required_token(context, "context"),
    )


def get_repository_human_role_policy_read_model(
    *,
    repository_id: str,
    product: str,
    context: str,
    store: RepositoryHumanRolePolicyReadStore,
) -> RepositoryHumanRolePolicyReadModel:
    normalized_repository_id, normalized_product, normalized_context = (
        normalize_repository_human_role_policy_lookup_scope(
            repository_id=repository_id,
            product=product,
            context=context,
        )
    )
    records = store.list_repository_human_role_policy_records(
        repository_id=normalized_repository_id,
        product=normalized_product,
        context=normalized_context,
    )
    matching_records = tuple(
        record
        for record in records
        if record.repository_id == normalized_repository_id
        and record.product == normalized_product
        and record.context == normalized_context
    )
    if not matching_records:
        return RepositoryHumanRolePolicyReadModel(
            status="missing",
            repository_id=normalized_repository_id,
            product=normalized_product,
            context=normalized_context,
            history_count=0,
            generated_at=utc_now_timestamp(),
        )
    try:
        current_record = _validate_repository_human_role_policy_history(matching_records)
    except (RepositoryHumanRolePolicyConflictError, RepositoryHumanRolePolicySequenceError):
        return RepositoryHumanRolePolicyReadModel(
            status="ambiguous",
            repository_id=normalized_repository_id,
            product=normalized_product,
            context=normalized_context,
            history_count=len(matching_records),
            generated_at=utc_now_timestamp(),
        )
    return RepositoryHumanRolePolicyReadModel(
        status="available",
        repository_id=normalized_repository_id,
        product=normalized_product,
        context=normalized_context,
        current_record=current_record,
        history_count=len(matching_records),
        generated_at=utc_now_timestamp(),
    )


def apply_repository_human_role_policy(
    *,
    store: RepositoryHumanRolePolicyReadStore,
    record: RepositoryHumanRolePolicyRecord,
    expected_current_record_id: str = "",
    expected_current_role_policy_digest: str = "",
    mode: RepositoryHumanRolePolicyApplyMode = "apply",
) -> RepositoryHumanRolePolicyApplyResult:
    normalized_expected_record_id = expected_current_record_id.strip()
    normalized_expected_digest = expected_current_role_policy_digest.strip().lower()
    records = store.list_repository_human_role_policy_records(
        repository_id=record.repository_id,
        product=record.product,
        context=record.context,
    )
    plan = plan_repository_human_role_policy_apply(
        records=records,
        record=record,
        expected_current_record_id=normalized_expected_record_id,
        expected_current_role_policy_digest=normalized_expected_digest,
    )
    if mode == "apply":
        if plan.status == "written":
            write_store = require_repository_human_role_policy_store(store)
            write_store.write_repository_human_role_policy_record(record)
            final_status: RepositoryHumanRolePolicyApplyStatus = "applied"
        else:
            final_status = "replayed"
    else:
        final_status = "would_apply" if plan.status == "written" else "would_replay"
    return _repository_human_role_policy_apply_result(
        record=record,
        mode=mode,
        status=final_status,
    )


def plan_repository_human_role_policy_apply(
    *,
    records: tuple[RepositoryHumanRolePolicyRecord, ...],
    record: RepositoryHumanRolePolicyRecord,
    expected_current_record_id: str,
    expected_current_role_policy_digest: str,
) -> RepositoryHumanRolePolicyAppendPlan:
    if record.status != "active":
        raise ValueError("Repository human role-policy apply requires an active candidate record.")
    normalized_expected_record_id = expected_current_record_id.strip()
    normalized_expected_digest = expected_current_role_policy_digest.strip().lower()
    if bool(normalized_expected_record_id) != bool(normalized_expected_digest):
        raise ValueError(
            "Repository human role-policy apply requires both expected current record ID "
            "and digest, or neither for revision 1."
        )
    if normalized_expected_digest:
        _normalize_sha256(normalized_expected_digest, "expected_current_role_policy_digest")
    if record.role_policy_revision > 1 and not normalized_expected_record_id:
        raise ValueError(
            "Repository human role-policy revisions after 1 require explicit expected "
            "current record ID and digest."
        )
    if record.role_policy_revision == 1 and (
        normalized_expected_record_id or normalized_expected_digest
    ):
        raise RepositoryHumanRolePolicyConflictError(
            "First repository human role-policy revision cannot declare an expected current tip."
        )

    matching_records = tuple(
        existing for existing in records if _role_policy_same_stream(existing, record)
    )
    plan = plan_repository_human_role_policy_append(
        records=matching_records,
        record=record,
    )
    if plan.status == "replayed":
        current_record = plan.current_record
        if current_record is None or current_record.record_id != record.record_id:
            raise RepositoryHumanRolePolicyConflictError(
                "Repository human role-policy revision is stale; retry with the current tip."
            )
        _require_replayed_role_policy_predecessor(
            records=matching_records,
            replayed_record=current_record,
            expected_current_record_id=normalized_expected_record_id,
            expected_current_role_policy_digest=normalized_expected_digest,
        )
        return plan
    if plan.current_record is None:
        if normalized_expected_record_id or normalized_expected_digest:
            raise RepositoryHumanRolePolicyConflictError(
                "Expected current repository human role-policy tip does not match current state: "
                "repository has no existing role-policy record."
            )
        return plan
    _require_expected_role_policy_tip(
        current_record=plan.current_record,
        expected_current_record_id=normalized_expected_record_id,
        expected_current_role_policy_digest=normalized_expected_digest,
        allow_empty_revision_1=False,
    )
    return plan


def plan_repository_human_role_policy_append(
    *,
    records: tuple[RepositoryHumanRolePolicyRecord, ...],
    record: RepositoryHumanRolePolicyRecord,
) -> RepositoryHumanRolePolicyAppendPlan:
    matching_records = tuple(
        existing for existing in records if _role_policy_same_stream(existing, record)
    )
    for existing in matching_records:
        if not _role_policy_same_scope(existing, record):
            raise RepositoryHumanRolePolicyConflictError(
                "Repository human role-policy history contains records for the same stream with different scope."
            )

    if not matching_records:
        if record.status != "active":
            raise RepositoryHumanRolePolicySequenceError(
                "First repository human role-policy record must be active."
            )
        if record.role_policy_revision != 1:
            raise RepositoryHumanRolePolicySequenceError(
                "First repository human role-policy record must have revision 1, "
                f"got {record.role_policy_revision}."
            )
        if record.supersedes_record_id:
            raise RepositoryHumanRolePolicySequenceError(
                "First repository human role-policy record cannot set supersedes_record_id."
            )
        return RepositoryHumanRolePolicyAppendPlan(status="written", current_record=None)

    current_record = _validate_repository_human_role_policy_history(matching_records)
    same_revision_records = tuple(
        existing
        for existing in matching_records
        if existing.role_policy_revision == record.role_policy_revision
    )
    if same_revision_records:
        existing = same_revision_records[0]
        if _role_policy_replay_matches(existing=existing, replay=record):
            return RepositoryHumanRolePolicyAppendPlan(
                status="replayed",
                current_record=current_record,
            )
        raise RepositoryHumanRolePolicyConflictError(
            "Repository human role-policy revision already exists with different payload."
        )

    if record.status != "active":
        raise RepositoryHumanRolePolicySequenceError(
            "New repository human role-policy revisions must be active."
        )
    expected_revision = current_record.role_policy_revision + 1
    if record.role_policy_revision != expected_revision:
        raise RepositoryHumanRolePolicySequenceError(
            f"Next repository human role-policy revision must be {expected_revision}, got {record.role_policy_revision}."
        )
    if record.supersedes_record_id != current_record.record_id:
        raise RepositoryHumanRolePolicySequenceError(
            "Repository human role-policy supersedes_record_id must be "
            f"'{current_record.record_id}', got '{record.supersedes_record_id}'."
        )
    superseded_current_record = RepositoryHumanRolePolicyRecord.model_validate(
        current_record.model_dump(mode="json") | {"status": "superseded"}
    )
    return RepositoryHumanRolePolicyAppendPlan(
        status="written",
        current_record=current_record,
        superseded_current_record=superseded_current_record,
    )


def _role_policy_same_stream(
    left: RepositoryHumanRolePolicyRecord,
    right: RepositoryHumanRolePolicyRecord,
) -> bool:
    return (
        left.repository_id == right.repository_id
        and left.product == right.product
        and left.context == right.context
    )


def _role_policy_same_scope(
    left: RepositoryHumanRolePolicyRecord,
    right: RepositoryHumanRolePolicyRecord,
) -> bool:
    return (
        _role_policy_same_stream(left, right)
        and left.repository_owner_id == right.repository_owner_id
        and left.repository == right.repository
    )


def _role_policy_replay_matches(
    *,
    existing: RepositoryHumanRolePolicyRecord,
    replay: RepositoryHumanRolePolicyRecord,
) -> bool:
    if (
        existing.record_id != replay.record_id
        or existing.role_policy_digest != replay.role_policy_digest
    ):
        return False
    return existing.status == replay.status or (
        existing.status == "superseded" and replay.status == "active"
    )


def _repository_human_role_policy_apply_result(
    *,
    record: RepositoryHumanRolePolicyRecord,
    mode: RepositoryHumanRolePolicyApplyMode,
    status: RepositoryHumanRolePolicyApplyStatus,
) -> RepositoryHumanRolePolicyApplyResult:
    return RepositoryHumanRolePolicyApplyResult(
        status=status,
        mode=mode,
        repository_id=record.repository_id,
        product=record.product,
        context=record.context,
        role_policy_revision=record.role_policy_revision,
        record_id=record.record_id,
        role_policy_digest=record.role_policy_digest,
        supersedes_record_id=record.supersedes_record_id,
        effective_at=record.effective_at,
    )


def _require_expected_role_policy_tip(
    *,
    current_record: RepositoryHumanRolePolicyRecord,
    expected_current_record_id: str,
    expected_current_role_policy_digest: str,
    allow_empty_revision_1: bool,
) -> None:
    normalized_expected_record_id = expected_current_record_id.strip()
    normalized_expected_digest = expected_current_role_policy_digest.strip().lower()
    if allow_empty_revision_1 and current_record.role_policy_revision == 1:
        if normalized_expected_record_id or normalized_expected_digest:
            raise RepositoryHumanRolePolicyConflictError(
                "First repository human role-policy revision cannot declare an expected current tip."
            )
        return
    if not normalized_expected_record_id or not normalized_expected_digest:
        raise ValueError(
            "Repository human role-policy apply requires explicit expected current tip "
            "record ID and digest."
        )
    if normalized_expected_record_id != current_record.record_id:
        raise RepositoryHumanRolePolicyConflictError(
            f"Expected current role-policy record ID '{normalized_expected_record_id}' "
            f"does not match active current record ID '{current_record.record_id}'."
        )
    if normalized_expected_digest != current_record.role_policy_digest:
        raise RepositoryHumanRolePolicyConflictError(
            "Expected current role-policy digest does not match the active current digest."
        )


def _require_replayed_role_policy_predecessor(
    *,
    records: tuple[RepositoryHumanRolePolicyRecord, ...],
    replayed_record: RepositoryHumanRolePolicyRecord,
    expected_current_record_id: str,
    expected_current_role_policy_digest: str,
) -> None:
    if replayed_record.role_policy_revision == 1:
        _require_expected_role_policy_tip(
            current_record=replayed_record,
            expected_current_record_id=expected_current_record_id,
            expected_current_role_policy_digest=expected_current_role_policy_digest,
            allow_empty_revision_1=True,
        )
        return
    predecessor_record_id = replayed_record.supersedes_record_id
    predecessor = next(
        (
            existing
            for existing in records
            if existing.record_id == predecessor_record_id
            and existing.role_policy_revision == replayed_record.role_policy_revision - 1
        ),
        None,
    )
    if predecessor is None:
        raise RepositoryHumanRolePolicyConflictError(
            "Replayed repository human role-policy record has no valid predecessor."
        )
    _require_expected_role_policy_tip(
        current_record=predecessor,
        expected_current_record_id=expected_current_record_id,
        expected_current_role_policy_digest=expected_current_role_policy_digest,
        allow_empty_revision_1=False,
    )


def _validate_repository_human_role_policy_history(
    records: tuple[RepositoryHumanRolePolicyRecord, ...],
) -> RepositoryHumanRolePolicyRecord:
    ordered_records = tuple(sorted(records, key=lambda item: item.role_policy_revision))
    revisions = tuple(record.role_policy_revision for record in ordered_records)
    if len(set(revisions)) != len(revisions):
        raise RepositoryHumanRolePolicyConflictError(
            "Repository human role-policy history has duplicate revisions."
        )
    for expected_revision, existing in enumerate(ordered_records, start=1):
        if existing.role_policy_revision != expected_revision:
            raise RepositoryHumanRolePolicySequenceError(
                "Repository human role-policy history must be contiguous from revision 1."
            )
        expected_supersedes = (
            None if expected_revision == 1 else ordered_records[expected_revision - 2].record_id
        )
        if existing.supersedes_record_id != expected_supersedes:
            raise RepositoryHumanRolePolicySequenceError(
                "Repository human role-policy history has an invalid supersedes_record_id link."
            )
    active_records = tuple(record for record in ordered_records if record.status == "active")
    if len(active_records) != 1:
        raise RepositoryHumanRolePolicyConflictError(
            "Repository human role-policy history must contain exactly one active record."
        )
    current_record = ordered_records[-1]
    if active_records[0].record_id != current_record.record_id:
        raise RepositoryHumanRolePolicyConflictError(
            "Repository human role-policy active record must be the highest revision."
        )
    return current_record


def _normalize_utc_timestamp(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"repository human admission requires {label}")
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"repository human admission {label} must be an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"repository human admission {label} requires a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
