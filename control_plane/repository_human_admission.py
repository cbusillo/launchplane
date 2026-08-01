from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from fnmatch import fnmatchcase
from typing import Literal, NamedTuple, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.repository_human_admission import (
    TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION,
    RepositoryHumanManagerAuthorityKind,
    RepositoryHumanRolePolicyProvenance,
    RepositoryHumanRolePolicyRecord,
    TenantTechnicalHumanWaiverAction,
    TenantTechnicalHumanWaiverAuthorization,
    TenantTechnicalHumanWaiverBinding,
    TenantTechnicalHumanWaiverEventRecord,
    _normalize_sha256,
    _required_decimal_id,
    _required_token,
)
from control_plane.contracts.tenant_merge_eligibility import (
    TenantAdmissionPathState,
    TenantAdmissionPathResult,
    TenantMergeCandidate,
    TenantRepositoryClassificationRecord,
)
from control_plane.service_auth import (
    GitHubHumanIdentity,
    GitHubHumanPolicyRule,
    LaunchplaneIdentity,
)
from control_plane.workflows.ship import utc_now_timestamp


class RepositoryHumanRolePolicyError(ValueError):
    pass


class RepositoryHumanRolePolicyConflictError(ValueError):
    """Raised when repository human role-policy history is replayed differently or has conflicting authority."""


class RepositoryHumanRolePolicySequenceError(ValueError):
    """Raised when repository human role-policy revision sequence is invalid."""


class TenantTechnicalHumanWaiverAuthorizationError(PermissionError):
    pass


class TenantTechnicalHumanWaiverStaleAuthorityError(ValueError):
    pass


class TenantTechnicalHumanWaiverRevokeCurrentError(ValueError):
    pass


class TenantTechnicalHumanWaiverEventConflictError(ValueError):
    """Raised when immutable tenant technical human waiver event history is replayed differently."""


class TenantTechnicalHumanWaiverWriteResult(NamedTuple):
    record: TenantTechnicalHumanWaiverEventRecord
    path_result: TenantAdmissionPathResult


class TenantTechnicalHumanWaiverCurrentAuthority(NamedTuple):
    classification: TenantRepositoryClassificationRecord
    role_policy_record: RepositoryHumanRolePolicyRecord
    authz_policy_record: LaunchplaneAuthzPolicyRecord


TenantTechnicalHumanWaiverApplyMode = Literal["dry_run", "apply"]
TenantTechnicalHumanWaiverApplyStatus = Literal[
    "would_apply",
    "would_replay",
    "applied",
    "replayed",
]


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


class TenantTechnicalHumanWaiverEventReadStore(Protocol):
    def list_tenant_technical_human_waiver_event_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        waiver_id: str = "",
        binding_sha256: str = "",
        pull_request_number: int | None = None,
        head_sha: str = "",
        classification_digest: str = "",
        role_policy_record_id: str = "",
        role_policy_digest: str = "",
        authz_policy_record_id: str = "",
        authz_policy_digest: str = "",
        action: str = "",
        author_github_id: int | None = None,
        limit: int | None = None,
    ) -> tuple[TenantTechnicalHumanWaiverEventRecord, ...]: ...


class TenantTechnicalHumanWaiverEventStore(
    TenantTechnicalHumanWaiverEventReadStore,
    Protocol,
):
    def write_tenant_technical_human_waiver_event_record(
        self,
        record: TenantTechnicalHumanWaiverEventRecord,
    ) -> Literal["written", "replayed"]: ...

    def read_tenant_technical_human_waiver_event_record(
        self,
        event_id: str,
    ) -> TenantTechnicalHumanWaiverEventRecord: ...


class TenantTechnicalHumanWaiverAuthorityReadStore(
    TenantTechnicalHumanWaiverEventReadStore,
    RepositoryHumanRolePolicyReadStore,
    Protocol,
):
    def list_tenant_repository_classification_records(
        self,
        *,
        repository_id: str = "",
        limit: int | None = None,
    ) -> tuple[TenantRepositoryClassificationRecord, ...]: ...

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]: ...


RepositoryHumanRolePolicyAppendStatus = Literal["written", "replayed"]
TenantTechnicalHumanWaiverEventAppendStatus = Literal["written", "replayed"]
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


@dataclass(frozen=True)
class TenantTechnicalHumanWaiverEventAppendPlan:
    status: TenantTechnicalHumanWaiverEventAppendStatus
    existing_record: TenantTechnicalHumanWaiverEventRecord | None = None


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


class TenantTechnicalHumanWaiverExpectedAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    classification_record_id: str
    classification_digest: str
    role_policy_record_id: str
    role_policy_digest: str
    authz_policy_record_id: str
    authz_policy_digest: str

    @model_validator(mode="after")
    def _validate_expected_authority(self) -> "TenantTechnicalHumanWaiverExpectedAuthority":
        if self.schema_version != 1:
            raise ValueError(
                "Unsupported tenant technical human waiver expected authority schema version."
            )
        self.classification_record_id = _required_token(
            self.classification_record_id,
            "classification_record_id",
        )
        self.classification_digest = _normalize_sha256(
            self.classification_digest,
            "classification_digest",
        )
        self.role_policy_record_id = _required_token(
            self.role_policy_record_id,
            "role_policy_record_id",
        )
        self.role_policy_digest = _normalize_sha256(
            self.role_policy_digest,
            "role_policy_digest",
        )
        self.authz_policy_record_id = _required_token(
            self.authz_policy_record_id,
            "authz_policy_record_id",
        )
        self.authz_policy_digest = _normalize_sha256(
            self.authz_policy_digest,
            "authz_policy_digest",
        )
        return self


class TenantTechnicalHumanWaiverExpectedCurrent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    waiver_id: str
    event_digest: str

    @model_validator(mode="after")
    def _validate_expected_current(self) -> "TenantTechnicalHumanWaiverExpectedCurrent":
        if self.schema_version != 1:
            raise ValueError(
                "Unsupported tenant technical human waiver expected current schema version."
            )
        self.waiver_id = _required_token(self.waiver_id, "waiver_id")
        self.event_digest = _normalize_sha256(self.event_digest, "event_digest")
        return self


class TenantTechnicalHumanWaiverApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: TenantTechnicalHumanWaiverApplyMode = "apply"
    action: TenantTechnicalHumanWaiverAction
    candidate: TenantMergeCandidate
    expected_authority: TenantTechnicalHumanWaiverExpectedAuthority
    source_event_kind: str
    source_event_id: str
    reason: str
    expires_at: str = ""
    expected_current: TenantTechnicalHumanWaiverExpectedCurrent | None = None

    @model_validator(mode="after")
    def _validate_envelope(self) -> "TenantTechnicalHumanWaiverApplyEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant technical human waiver apply schema version.")
        self.source_event_kind = _required_token(
            self.source_event_kind,
            "source_event_kind",
        )
        self.source_event_id = _required_token(self.source_event_id, "source_event_id")
        self.reason = _required_token(self.reason, "reason")
        if self.expires_at:
            self.expires_at = _normalize_utc_timestamp(self.expires_at, "expires_at")
        if self.action == "created" and self.expected_current is not None:
            raise ValueError(
                "Tenant technical human waiver creation cannot declare expected current waiver."
            )
        if self.action == "revoked" and self.expected_current is None:
            raise ValueError(
                "Tenant technical human waiver revocation requires expected current waiver."
            )
        if self.action == "revoked" and self.expires_at:
            raise ValueError("Tenant technical human waiver revocation cannot declare expires_at.")
        return self


class TenantTechnicalHumanWaiverApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: TenantTechnicalHumanWaiverApplyStatus
    mode: TenantTechnicalHumanWaiverApplyMode
    action: TenantTechnicalHumanWaiverAction
    repository_id: str
    repository_owner_id: str
    repository: str
    product: str
    context: str
    pull_request_number: int = Field(ge=1)
    head_sha: str
    waiver_id: str
    event_id: str
    event_digest: str
    binding_sha256: str
    path_result: TenantAdmissionPathResult
    occurred_at: str
    recorded_at: str
    expires_at: str = ""
    dry_run: bool = False

    @model_validator(mode="after")
    def _validate_result(self) -> "TenantTechnicalHumanWaiverApplyResult":
        if self.schema_version != 1:
            raise ValueError(
                "Unsupported tenant technical human waiver apply result schema version."
            )
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.repository_owner_id = _required_decimal_id(
            self.repository_owner_id,
            "repository_owner_id",
        )
        self.repository = self.repository.strip().lower()
        self.product = _required_token(self.product, "product")
        self.context = _required_token(self.context, "context")
        self.head_sha = _required_token(self.head_sha, "head_sha").lower()
        self.waiver_id = _required_token(self.waiver_id, "waiver_id")
        self.event_id = _required_token(self.event_id, "event_id")
        self.event_digest = _normalize_sha256(self.event_digest, "event_digest")
        self.binding_sha256 = _normalize_sha256(self.binding_sha256, "binding_sha256")
        self.occurred_at = _normalize_utc_timestamp(self.occurred_at, "occurred_at")
        self.recorded_at = _normalize_utc_timestamp(self.recorded_at, "recorded_at")
        if self.expires_at:
            self.expires_at = _normalize_utc_timestamp(self.expires_at, "expires_at")
        if self.mode == "dry_run" and not self.dry_run:
            raise ValueError("Dry-run tenant technical human waiver result must set dry_run.")
        if self.mode == "apply" and self.dry_run:
            raise ValueError("Applied tenant technical human waiver result cannot be dry_run.")
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


def require_tenant_technical_human_waiver_authority_read_store(
    record_store: object,
) -> TenantTechnicalHumanWaiverAuthorityReadStore:
    required_methods = (
        "list_tenant_repository_classification_records",
        "list_repository_human_role_policy_records",
        "list_authz_policy_records",
        "list_tenant_technical_human_waiver_event_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support tenant technical human waiver reads: "
            f"{missing_summary}"
        )
    return cast(TenantTechnicalHumanWaiverAuthorityReadStore, record_store)


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


def apply_tenant_technical_human_waiver(
    *,
    store: TenantTechnicalHumanWaiverAuthorityReadStore,
    identity: LaunchplaneIdentity,
    envelope: TenantTechnicalHumanWaiverApplyEnvelope,
    observed_at: str,
) -> TenantTechnicalHumanWaiverApplyResult:
    current = tenant_technical_human_waiver_current_authority(
        store=store,
        candidate=envelope.candidate,
        expected_authority=envelope.expected_authority,
        evaluated_at=observed_at,
    )
    events = store.list_tenant_technical_human_waiver_event_records(
        repository_id=envelope.candidate.repository_id,
        repository_owner_id=envelope.candidate.repository_owner_id,
        repository=envelope.candidate.repository,
        product=envelope.candidate.product,
        context=envelope.candidate.context,
        pull_request_number=envelope.candidate.pull_request_number,
        head_sha=envelope.candidate.head_sha,
    )
    return build_tenant_technical_human_waiver_apply_result(
        identity=identity,
        envelope=envelope,
        classification=current.classification,
        role_policy_record=current.role_policy_record,
        authz_policy_record=current.authz_policy_record,
        events=events,
        observed_at=observed_at,
    )


def tenant_technical_human_waiver_current_authority(
    *,
    store: TenantTechnicalHumanWaiverAuthorityReadStore,
    candidate: TenantMergeCandidate,
    expected_authority: TenantTechnicalHumanWaiverExpectedAuthority,
    evaluated_at: str,
) -> TenantTechnicalHumanWaiverCurrentAuthority:
    normalized_evaluated_at = _normalize_utc_timestamp(evaluated_at, "evaluated_at")
    classification = _current_expected_tenant_ui_classification(
        store=store,
        candidate=candidate,
        expected_authority=expected_authority,
        evaluated_at=normalized_evaluated_at,
    )
    role_policy_record = _current_expected_repository_owner_role_policy(
        store=store,
        candidate=candidate,
        expected_authority=expected_authority,
        evaluated_at=normalized_evaluated_at,
    )
    authz_policy_record = _current_expected_waiver_authz_policy(
        store=store,
        candidate=candidate,
        expected_authority=expected_authority,
    )
    return TenantTechnicalHumanWaiverCurrentAuthority(
        classification=classification,
        role_policy_record=role_policy_record,
        authz_policy_record=authz_policy_record,
    )


def build_tenant_technical_human_waiver_apply_result(
    *,
    identity: LaunchplaneIdentity,
    envelope: TenantTechnicalHumanWaiverApplyEnvelope,
    classification: TenantRepositoryClassificationRecord,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    authz_policy_record: LaunchplaneAuthzPolicyRecord,
    events: tuple[TenantTechnicalHumanWaiverEventRecord, ...],
    observed_at: str,
) -> TenantTechnicalHumanWaiverApplyResult:
    normalized_observed_at = _normalize_utc_timestamp(observed_at, "observed_at")
    current_path = technical_human_waiver_path_result(
        candidate=envelope.candidate,
        classification=classification,
        role_policy_record=role_policy_record,
        authz_policy_record=authz_policy_record,
        events=events,
        evaluated_at=normalized_observed_at,
    )
    captured = capture_tenant_technical_human_waiver_event(
        identity=identity,
        candidate=envelope.candidate,
        classification=classification,
        role_policy_record=role_policy_record,
        authz_policy_record=authz_policy_record,
        action=envelope.action,
        occurred_at=normalized_observed_at,
        source_event_kind=envelope.source_event_kind,
        source_event_id=envelope.source_event_id,
        reason=envelope.reason,
        recorded_at=normalized_observed_at,
        expires_at=envelope.expires_at,
    )
    append_plan = plan_tenant_technical_human_waiver_event_append(
        records=events,
        record=captured.record,
    )
    if envelope.action == "created":
        if append_plan.status == "replayed":
            _require_replayed_create_current(
                path_result=current_path,
                existing_record=append_plan.existing_record,
            )
        elif current_path.state == "satisfied":
            raise TenantTechnicalHumanWaiverEventConflictError(
                "Tenant technical human waiver already satisfies this exact binding."
            )
        elif current_path.state not in {"pending", "stale", "denied"}:
            raise TenantTechnicalHumanWaiverEventConflictError(
                "Tenant technical human waiver creation requires a mutable current path."
            )
    else:
        _require_expected_current_waiver(
            path_result=current_path,
            expected_current=envelope.expected_current,
        )
    all_events = (
        tuple(events) if append_plan.status == "replayed" else tuple(events) + (captured.record,)
    )
    resulting_path = technical_human_waiver_path_result(
        candidate=envelope.candidate,
        classification=classification,
        role_policy_record=role_policy_record,
        authz_policy_record=authz_policy_record,
        events=all_events,
        evaluated_at=normalized_observed_at,
    )
    if envelope.action == "created" and resulting_path.state != "satisfied":
        raise TenantTechnicalHumanWaiverEventConflictError(
            "Tenant technical human waiver creation did not satisfy the exact binding."
        )
    if envelope.action == "revoked" and resulting_path.state != "denied":
        raise TenantTechnicalHumanWaiverEventConflictError(
            "Tenant technical human waiver revocation did not deny the exact binding."
        )
    if append_plan.status == "replayed" and envelope.action == "revoked":
        raise TenantTechnicalHumanWaiverEventConflictError(
            "Tenant technical human waiver revocation replay is stale for the current lifecycle."
        )

    status: TenantTechnicalHumanWaiverApplyStatus
    if envelope.mode == "dry_run":
        status = "would_replay" if append_plan.status == "replayed" else "would_apply"
    else:
        status = "replayed" if append_plan.status == "replayed" else "applied"
    return _tenant_technical_human_waiver_apply_result(
        record=captured.record,
        path_result=resulting_path,
        mode=envelope.mode,
        status=status,
        dry_run=envelope.mode == "dry_run",
        recorded_at=normalized_observed_at,
    )


def tenant_technical_human_waiver_apply_response_payload(
    *,
    trace_id: str,
    result: TenantTechnicalHumanWaiverApplyResult,
) -> dict[str, object]:
    return {
        "status": "ok",
        "trace_id": trace_id,
        "result": result.model_dump(mode="json"),
    }


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


def plan_tenant_technical_human_waiver_event_append(
    *,
    records: tuple[TenantTechnicalHumanWaiverEventRecord, ...],
    record: TenantTechnicalHumanWaiverEventRecord,
) -> TenantTechnicalHumanWaiverEventAppendPlan:
    same_id_records = tuple(
        existing for existing in records if existing.event_id == record.event_id
    )
    if len(same_id_records) > 1:
        raise TenantTechnicalHumanWaiverEventConflictError(
            "Tenant technical human waiver event history has multiple records with the same event_id."
        )
    if same_id_records:
        existing = same_id_records[0]
        if existing == record:
            return TenantTechnicalHumanWaiverEventAppendPlan(
                status="replayed",
                existing_record=existing,
            )
        raise TenantTechnicalHumanWaiverEventConflictError(
            "Tenant technical human waiver event already exists with different payload."
        )
    return TenantTechnicalHumanWaiverEventAppendPlan(status="written")


def repository_owner_role_policy_provenance(
    *,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    repository_id: str,
    repository_owner_id: str,
    repository: str,
    product: str,
    context: str,
    github_id: int,
    evaluated_at: str,
) -> RepositoryHumanRolePolicyProvenance:
    if role_policy_record.status != "active":
        raise RepositoryHumanRolePolicyError("Repository human role policy must be active.")
    if github_id < 1:
        raise RepositoryHumanRolePolicyError("Repository human role lookup requires a GitHub ID.")
    normalized_evaluated_at = _normalize_utc_timestamp(evaluated_at, "evaluated_at")
    if role_policy_record.effective_at > normalized_evaluated_at:
        raise RepositoryHumanRolePolicyError("Repository human role policy is not effective yet.")
    _require_role_policy_scope_match(
        role_policy_record=role_policy_record,
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        repository=repository,
        product=product,
        context=context,
    )
    if github_id not in role_policy_record.repository_owner_github_ids:
        raise RepositoryHumanRolePolicyError("GitHub human is not a repository owner.")
    return _role_policy_provenance(
        role_policy_record=role_policy_record,
        authority_kind="repository_owner",
        evaluated_at=normalized_evaluated_at,
    )


def manager_role_policy_provenance(
    *,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    repository_id: str,
    repository_owner_id: str,
    repository: str,
    product: str,
    context: str,
    github_id: int,
    evaluated_at: str,
) -> RepositoryHumanRolePolicyProvenance:
    if role_policy_record.status != "active":
        raise RepositoryHumanRolePolicyError("Repository human role policy must be active.")
    if github_id < 1:
        raise RepositoryHumanRolePolicyError("Manager role lookup requires a GitHub ID.")
    normalized_evaluated_at = _normalize_utc_timestamp(evaluated_at, "evaluated_at")
    if role_policy_record.effective_at > normalized_evaluated_at:
        raise RepositoryHumanRolePolicyError("Repository human role policy is not effective yet.")
    _require_role_policy_scope_match(
        role_policy_record=role_policy_record,
        repository_id=repository_id,
        repository_owner_id=repository_owner_id,
        repository=repository,
        product=product,
        context=context,
    )
    if github_id in role_policy_record.manager_primary_github_ids:
        return _role_policy_provenance(
            role_policy_record=role_policy_record,
            authority_kind="manager_primary",
            evaluated_at=normalized_evaluated_at,
        )
    if github_id in role_policy_record.manager_backup_github_ids:
        return _role_policy_provenance(
            role_policy_record=role_policy_record,
            authority_kind="manager_backup",
            evaluated_at=normalized_evaluated_at,
        )
    active_delegations = tuple(
        delegation
        for delegation in role_policy_record.manager_delegations
        if delegation.delegated_manager_github_id == github_id
        and delegation.delegated_by_github_id
        in (
            set(role_policy_record.manager_primary_github_ids)
            | set(role_policy_record.manager_backup_github_ids)
        )
        and delegation.starts_at <= normalized_evaluated_at < delegation.expires_at
        and (not delegation.revoked_at or delegation.revoked_at > normalized_evaluated_at)
    )
    if len(active_delegations) != 1:
        raise RepositoryHumanRolePolicyError(
            "GitHub human does not have exactly one active manager authority."
        )
    return _role_policy_provenance(
        role_policy_record=role_policy_record,
        authority_kind="manager_delegated",
        evaluated_at=normalized_evaluated_at,
        delegation_id=active_delegations[0].delegation_id,
    )


def capture_tenant_technical_human_waiver_event(
    *,
    identity: LaunchplaneIdentity,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    authz_policy_record: LaunchplaneAuthzPolicyRecord,
    action: TenantTechnicalHumanWaiverAction,
    occurred_at: str,
    source_event_kind: str,
    source_event_id: str,
    reason: str,
    recorded_at: str,
    expires_at: str = "",
) -> TenantTechnicalHumanWaiverWriteResult:
    if not isinstance(identity, GitHubHumanIdentity):
        raise TenantTechnicalHumanWaiverAuthorizationError(
            "Tenant technical human waiver requires a GitHub human identity."
        )
    if identity.github_id < 1:
        raise TenantTechnicalHumanWaiverAuthorizationError(
            "Tenant technical human waiver requires a stable GitHub numeric identity."
        )
    if authz_policy_record.status != "active" or authz_policy_record.policy.schema_version != 2:
        raise TenantTechnicalHumanWaiverAuthorizationError(
            "Tenant technical human waiver requires one active schema-v2 authorization policy."
        )
    if not _classification_matches_candidate(classification=classification, candidate=candidate):
        raise ValueError("Tenant technical human waiver classification does not match candidate.")
    if classification.classification_kind != "tenant_ui":
        raise ValueError("Tenant technical human waiver requires tenant_ui classification.")

    normalized_occurred_at = _normalize_utc_timestamp(occurred_at, "occurred_at")
    normalized_recorded_at = _normalize_utc_timestamp(recorded_at, "recorded_at")
    if normalized_occurred_at > normalized_recorded_at:
        raise ValueError("Tenant technical human waiver cannot be recorded before it occurred.")
    try:
        role_provenance = repository_owner_role_policy_provenance(
            role_policy_record=role_policy_record,
            repository_id=candidate.repository_id,
            repository_owner_id=candidate.repository_owner_id,
            repository=candidate.repository,
            product=candidate.product,
            context=candidate.context,
            github_id=identity.github_id,
            evaluated_at=normalized_occurred_at,
        )
    except RepositoryHumanRolePolicyError as error:
        raise TenantTechnicalHumanWaiverAuthorizationError(str(error)) from error
    matching_rules = _matching_managed_waiver_rules_for_identity(
        authz_policy_record=authz_policy_record,
        identity=identity,
        product=candidate.product,
        context=candidate.context,
    )
    if len(matching_rules) != 1:
        raise TenantTechnicalHumanWaiverAuthorizationError(
            "Tenant technical human waiver requires exactly one managed authz policy rule with the caller GitHub ID."
        )
    matching_rule = matching_rules[0]
    binding = TenantTechnicalHumanWaiverBinding(
        repository_id=candidate.repository_id,
        repository_owner_id=candidate.repository_owner_id,
        repository=candidate.repository,
        product=candidate.product,
        context=candidate.context,
        pull_request_number=candidate.pull_request_number,
        head_sha=candidate.head_sha,
        classification_revision=classification.classification_revision,
        classification_digest=classification.classification_digest,
        role_policy_record_id=role_policy_record.record_id,
        role_policy_revision=role_policy_record.role_policy_revision,
        role_policy_digest=role_policy_record.role_policy_digest,
        authz_policy_record_id=authz_policy_record.record_id,
        authz_policy_revision=authz_policy_record.revision,
        authz_policy_digest=authz_policy_record.policy_sha256,
    )
    authorization = TenantTechnicalHumanWaiverAuthorization(
        author_github_id=identity.github_id,
        author_login=identity.login,
        managed_set_id=cast(str, matching_rule.managed_set_id),
        managed_rule_id=cast(str, matching_rule.managed_rule_id),
        authz_policy_record_id=authz_policy_record.record_id,
        authz_policy_revision=authz_policy_record.revision,
        authz_policy_digest=authz_policy_record.policy_sha256,
        authz_policy_source=authz_policy_record.source,
        role_policy_provenance=role_provenance,
        authorized_at=normalized_occurred_at,
    )
    record = TenantTechnicalHumanWaiverEventRecord(
        binding=binding,
        action=action,
        occurred_at=normalized_occurred_at,
        recorded_at=normalized_recorded_at,
        source_event_kind=source_event_kind,
        source_event_id=source_event_id,
        reason=reason,
        authorization=authorization,
        expires_at=expires_at,
    )
    return TenantTechnicalHumanWaiverWriteResult(
        record=record,
        path_result=technical_human_waiver_path_result(
            candidate=candidate,
            classification=classification,
            role_policy_record=role_policy_record,
            authz_policy_record=authz_policy_record,
            events=(record,),
            evaluated_at=normalized_occurred_at,
        ),
    )


def technical_human_waiver_path_result(
    *,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    authz_policy_record: LaunchplaneAuthzPolicyRecord,
    events: tuple[TenantTechnicalHumanWaiverEventRecord, ...],
    evaluated_at: str,
) -> TenantAdmissionPathResult:
    normalized_evaluated_at = _normalize_utc_timestamp(evaluated_at, "evaluated_at")

    def path_result(
        *,
        state: TenantAdmissionPathState,
        evidence_id: str = "",
        evidence_digest: str = "",
    ) -> TenantAdmissionPathResult:
        return TenantAdmissionPathResult(
            path_kind="technical_human_waiver",
            state=state,
            evidence_id=evidence_id,
            evidence_digest=evidence_digest,
            repository_id=candidate.repository_id,
            repository_owner_id=candidate.repository_owner_id,
            repository=candidate.repository,
            pull_request_number=candidate.pull_request_number,
            head_sha=candidate.head_sha,
            classification_digest=classification.classification_digest,
        )

    if role_policy_record.status != "active" or authz_policy_record.status != "active":
        return path_result(state="unavailable")
    if role_policy_record.effective_at > normalized_evaluated_at:
        return path_result(state="unavailable")
    if not _role_policy_matches_candidate(
        role_policy_record=role_policy_record,
        candidate=candidate,
    ) or not _classification_matches_candidate(classification=classification, candidate=candidate):
        return path_result(state="stale")
    if (
        classification.classification_kind != "tenant_ui"
        or not classification.classification_digest
    ):
        return path_result(state="stale")

    current_events = tuple(
        event
        for event in events
        if event.occurred_at <= normalized_evaluated_at
        and event.binding.repository_id == candidate.repository_id
        and event.binding.repository_owner_id == candidate.repository_owner_id
        and event.binding.repository == candidate.repository
        and event.binding.product == candidate.product
        and event.binding.context == candidate.context
        and event.binding.pull_request_number == candidate.pull_request_number
        and event.binding.head_sha == candidate.head_sha
        and event.binding.classification_revision == classification.classification_revision
        and event.binding.classification_digest == classification.classification_digest
        and event.binding.role_policy_record_id == role_policy_record.record_id
        and event.binding.role_policy_revision == role_policy_record.role_policy_revision
        and event.binding.role_policy_digest == role_policy_record.role_policy_digest
        and event.binding.authz_policy_record_id == authz_policy_record.record_id
        and event.binding.authz_policy_revision == authz_policy_record.revision
        and event.binding.authz_policy_digest == authz_policy_record.policy_sha256
    )
    if not current_events:
        stale_events = tuple(
            event
            for event in events
            if event.occurred_at <= normalized_evaluated_at
            and event.binding.repository_id == candidate.repository_id
            and event.binding.pull_request_number == candidate.pull_request_number
        )
        return path_result(state="stale" if stale_events else "pending")

    latest = max(
        current_events,
        key=lambda event: (event.occurred_at, event.action == "revoked", event.event_id),
    )
    if latest.action == "revoked":
        return path_result(
            state="denied",
            evidence_id=latest.waiver_id,
            evidence_digest=latest.event_digest,
        )
    if latest.expires_at and latest.expires_at <= normalized_evaluated_at:
        return path_result(
            state="stale",
            evidence_id=latest.waiver_id,
            evidence_digest=latest.event_digest,
        )
    if not _waiver_authz_rule_current(
        event=latest,
        authz_policy_record=authz_policy_record,
        product=candidate.product,
        context=candidate.context,
    ):
        return path_result(
            state="stale",
            evidence_id=latest.waiver_id,
            evidence_digest=latest.event_digest,
        )
    if latest.authorization.author_github_id not in role_policy_record.repository_owner_github_ids:
        return path_result(
            state="stale",
            evidence_id=latest.waiver_id,
            evidence_digest=latest.event_digest,
        )
    return path_result(
        state="satisfied",
        evidence_id=latest.waiver_id,
        evidence_digest=latest.event_digest,
    )


def manager_authority_current(
    *,
    provenance: RepositoryHumanRolePolicyProvenance | None,
    role_policy_record: RepositoryHumanRolePolicyRecord | None,
    manager_github_id: int,
    evaluated_at: str,
) -> bool:
    if provenance is None:
        return role_policy_record is None
    if role_policy_record is None or role_policy_record.status != "active":
        return False
    if (
        provenance.role_policy_record_id != role_policy_record.record_id
        or provenance.role_policy_revision != role_policy_record.role_policy_revision
        or provenance.role_policy_digest != role_policy_record.role_policy_digest
        or provenance.repository_id != role_policy_record.repository_id
        or provenance.repository_owner_id != role_policy_record.repository_owner_id
        or provenance.repository != role_policy_record.repository
    ):
        return False
    try:
        current = manager_role_policy_provenance(
            role_policy_record=role_policy_record,
            repository_id=provenance.repository_id,
            repository_owner_id=provenance.repository_owner_id,
            repository=provenance.repository,
            product=provenance.product,
            context=provenance.context,
            github_id=manager_github_id,
            evaluated_at=evaluated_at,
        )
    except RepositoryHumanRolePolicyError:
        return False
    return (
        current.authority_kind == provenance.authority_kind
        and current.delegation_id == provenance.delegation_id
    )


def _waiver_authz_rule_current(
    *,
    event: TenantTechnicalHumanWaiverEventRecord,
    authz_policy_record: LaunchplaneAuthzPolicyRecord,
    product: str,
    context: str,
) -> bool:
    authorization = event.authorization
    if authz_policy_record.policy.schema_version != 2:
        return False
    if (
        authorization.authz_policy_record_id != authz_policy_record.record_id
        or authorization.authz_policy_revision != authz_policy_record.revision
        or authorization.authz_policy_digest != authz_policy_record.policy_sha256
    ):
        return False
    matching_rules = tuple(
        rule
        for rule in authz_policy_record.policy.github_humans
        if rule.managed_set_id == authorization.managed_set_id
        and rule.managed_rule_id == authorization.managed_rule_id
    )
    if len(matching_rules) != 1:
        return False
    rule = matching_rules[0]
    return (
        authorization.author_github_id in rule.github_ids
        and TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION in rule.actions
        and (not rule.products or product in rule.products)
        and (not rule.contexts or context in rule.contexts)
    )


def _current_expected_tenant_ui_classification(
    *,
    store: TenantTechnicalHumanWaiverAuthorityReadStore,
    candidate: TenantMergeCandidate,
    expected_authority: TenantTechnicalHumanWaiverExpectedAuthority,
    evaluated_at: str,
) -> TenantRepositoryClassificationRecord:
    records = store.list_tenant_repository_classification_records(
        repository_id=candidate.repository_id
    )
    repository_records = tuple(
        record for record in records if record.repository_id == candidate.repository_id
    )
    if not repository_records:
        raise TenantTechnicalHumanWaiverStaleAuthorityError(
            "Tenant technical human waiver requires current repository classification."
        )
    highest_revision = max(record.classification_revision for record in repository_records)
    current_records = tuple(
        record
        for record in repository_records
        if record.classification_revision == highest_revision
    )
    if len(current_records) != 1:
        raise TenantTechnicalHumanWaiverStaleAuthorityError(
            "Tenant technical human waiver requires exactly one current repository classification."
        )
    current_record = current_records[0]
    if current_record.classified_at > evaluated_at:
        raise TenantTechnicalHumanWaiverStaleAuthorityError(
            "Tenant technical human waiver classification is not effective yet."
        )
    if not _classification_matches_candidate(
        classification=current_record,
        candidate=candidate,
    ):
        raise TenantTechnicalHumanWaiverStaleAuthorityError(
            "Tenant technical human waiver classification does not match candidate."
        )
    if current_record.classification_kind != "tenant_ui":
        raise TenantTechnicalHumanWaiverStaleAuthorityError(
            "Tenant technical human waiver requires tenant_ui classification."
        )
    if (
        current_record.record_id != expected_authority.classification_record_id
        or current_record.classification_digest != expected_authority.classification_digest
    ):
        raise TenantTechnicalHumanWaiverStaleAuthorityError(
            "Expected tenant repository classification authority is stale."
        )
    return current_record


def _current_expected_repository_owner_role_policy(
    *,
    store: TenantTechnicalHumanWaiverAuthorityReadStore,
    candidate: TenantMergeCandidate,
    expected_authority: TenantTechnicalHumanWaiverExpectedAuthority,
    evaluated_at: str,
) -> RepositoryHumanRolePolicyRecord:
    records = store.list_repository_human_role_policy_records(
        repository_id=candidate.repository_id,
        product=candidate.product,
        context=candidate.context,
        status="active",
    )
    matching_records = tuple(
        record
        for record in records
        if _role_policy_matches_candidate(role_policy_record=record, candidate=candidate)
    )
    if len(matching_records) != 1:
        raise TenantTechnicalHumanWaiverStaleAuthorityError(
            "Tenant technical human waiver requires exactly one active repository role policy."
        )
    current_record = matching_records[0]
    if current_record.effective_at > evaluated_at:
        raise TenantTechnicalHumanWaiverStaleAuthorityError(
            "Tenant technical human waiver role policy is not effective yet."
        )
    if (
        current_record.record_id != expected_authority.role_policy_record_id
        or current_record.role_policy_digest != expected_authority.role_policy_digest
    ):
        raise TenantTechnicalHumanWaiverStaleAuthorityError(
            "Expected repository human role-policy authority is stale."
        )
    return current_record


def _current_expected_waiver_authz_policy(
    *,
    store: TenantTechnicalHumanWaiverAuthorityReadStore,
    candidate: TenantMergeCandidate,
    expected_authority: TenantTechnicalHumanWaiverExpectedAuthority,
) -> LaunchplaneAuthzPolicyRecord:
    records = store.list_authz_policy_records(status="active")
    if len(records) != 1:
        raise TenantTechnicalHumanWaiverStaleAuthorityError(
            "Tenant technical human waiver requires exactly one active authz policy."
        )
    current_record = records[0]
    if current_record.policy.schema_version != 2:
        raise TenantTechnicalHumanWaiverAuthorizationError(
            "Tenant technical human waiver requires schema-v2 authz policy."
        )
    if (
        current_record.record_id != expected_authority.authz_policy_record_id
        or current_record.policy_sha256 != expected_authority.authz_policy_digest
    ):
        raise TenantTechnicalHumanWaiverStaleAuthorityError(
            "Expected tenant technical human waiver authz authority is stale."
        )
    return current_record


def _matching_managed_waiver_rules_for_identity(
    *,
    authz_policy_record: LaunchplaneAuthzPolicyRecord,
    identity: GitHubHumanIdentity,
    product: str,
    context: str,
) -> tuple[GitHubHumanPolicyRule, ...]:
    if authz_policy_record.policy.schema_version != 2:
        return ()

    def intersects(values: frozenset[str], allowed_values: tuple[str, ...]) -> bool:
        return any(
            fnmatchcase(value.strip(), allowed_value)
            for value in values
            for allowed_value in allowed_values
        )

    return tuple(
        rule
        for rule in authz_policy_record.policy.github_humans
        if rule.managed_set_id is not None
        and rule.managed_rule_id is not None
        and TENANT_TECHNICAL_HUMAN_WAIVER_WRITE_ACTION in rule.actions
        and (not rule.products or product in rule.products)
        and (not rule.contexts or context in rule.contexts)
        and not rule.instances
        and bool(rule.github_ids)
        and identity.github_id in rule.github_ids
        and (not rule.organizations or intersects(identity.organizations, rule.organizations))
        and (not rule.teams or intersects(identity.teams, rule.teams))
        and (not rule.roles or identity.role in rule.roles)
    )


def _require_expected_current_waiver(
    *,
    path_result: TenantAdmissionPathResult,
    expected_current: TenantTechnicalHumanWaiverExpectedCurrent | None,
) -> None:
    if expected_current is None:
        raise TenantTechnicalHumanWaiverRevokeCurrentError(
            "Tenant technical human waiver revocation requires expected current waiver."
        )
    if path_result.state != "satisfied":
        raise TenantTechnicalHumanWaiverRevokeCurrentError(
            "Tenant technical human waiver revocation requires a current satisfied waiver."
        )
    if (
        path_result.evidence_id != expected_current.waiver_id
        or path_result.evidence_digest != expected_current.event_digest
    ):
        raise TenantTechnicalHumanWaiverRevokeCurrentError(
            "Expected current tenant technical human waiver does not match current state."
        )


def _require_replayed_create_current(
    *,
    path_result: TenantAdmissionPathResult,
    existing_record: TenantTechnicalHumanWaiverEventRecord | None,
) -> None:
    if existing_record is None:
        raise TenantTechnicalHumanWaiverEventConflictError(
            "Tenant technical human waiver replay missing existing event."
        )
    if existing_record.action != "created":
        raise TenantTechnicalHumanWaiverEventConflictError(
            "Tenant technical human waiver replay action changed."
        )
    if (
        path_result.state != "satisfied"
        or path_result.evidence_id != existing_record.waiver_id
        or path_result.evidence_digest != existing_record.event_digest
    ):
        raise TenantTechnicalHumanWaiverEventConflictError(
            "Tenant technical human waiver creation replay is stale for the current lifecycle."
        )


def _tenant_technical_human_waiver_apply_result(
    *,
    record: TenantTechnicalHumanWaiverEventRecord,
    path_result: TenantAdmissionPathResult,
    mode: TenantTechnicalHumanWaiverApplyMode,
    status: TenantTechnicalHumanWaiverApplyStatus,
    dry_run: bool,
    recorded_at: str,
) -> TenantTechnicalHumanWaiverApplyResult:
    binding = record.binding
    return TenantTechnicalHumanWaiverApplyResult(
        status=status,
        mode=mode,
        action=record.action,
        repository_id=binding.repository_id,
        repository_owner_id=binding.repository_owner_id,
        repository=binding.repository,
        product=binding.product,
        context=binding.context,
        pull_request_number=binding.pull_request_number,
        head_sha=binding.head_sha,
        waiver_id=record.waiver_id,
        event_id=record.event_id,
        event_digest=record.event_digest,
        binding_sha256=binding.binding_sha256,
        path_result=path_result,
        occurred_at=record.occurred_at,
        recorded_at=recorded_at,
        expires_at=record.expires_at,
        dry_run=dry_run,
    )


def _role_policy_provenance(
    *,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    authority_kind: RepositoryHumanManagerAuthorityKind | Literal["repository_owner"],
    evaluated_at: str,
    delegation_id: str = "",
) -> RepositoryHumanRolePolicyProvenance:
    return RepositoryHumanRolePolicyProvenance(
        repository_id=role_policy_record.repository_id,
        repository_owner_id=role_policy_record.repository_owner_id,
        repository=role_policy_record.repository,
        product=role_policy_record.product,
        context=role_policy_record.context,
        role_policy_record_id=role_policy_record.record_id,
        role_policy_revision=role_policy_record.role_policy_revision,
        role_policy_digest=role_policy_record.role_policy_digest,
        role_policy_source=role_policy_record.source,
        authority_kind=authority_kind,
        delegation_id=delegation_id,
        evaluated_at=evaluated_at,
    )


def _require_role_policy_scope_match(
    *,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    repository_id: str,
    repository_owner_id: str,
    repository: str,
    product: str,
    context: str,
) -> None:
    if (
        role_policy_record.repository_id != repository_id
        or role_policy_record.repository_owner_id != repository_owner_id
        or role_policy_record.repository != repository.lower()
        or role_policy_record.product != product
        or role_policy_record.context != context
    ):
        raise RepositoryHumanRolePolicyError(
            "Repository human role policy does not match repository scope."
        )


def _role_policy_matches_candidate(
    *,
    role_policy_record: RepositoryHumanRolePolicyRecord,
    candidate: TenantMergeCandidate,
) -> bool:
    return (
        role_policy_record.repository_id == candidate.repository_id
        and role_policy_record.repository_owner_id == candidate.repository_owner_id
        and role_policy_record.repository == candidate.repository
        and role_policy_record.product == candidate.product
        and role_policy_record.context == candidate.context
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


def _classification_matches_candidate(
    *,
    classification: TenantRepositoryClassificationRecord,
    candidate: TenantMergeCandidate,
) -> bool:
    return (
        classification.repository_id == candidate.repository_id
        and classification.repository_owner_id == candidate.repository_owner_id
        and classification.repository == candidate.repository
        and classification.product == candidate.product
        and classification.context == candidate.context
    )


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
