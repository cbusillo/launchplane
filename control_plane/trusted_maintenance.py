from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, NamedTuple, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.tenant_merge_eligibility import (
    TenantAdmissionPathResult,
    TenantAdmissionPathState,
    TenantMergeCandidate,
    TenantRepositoryClassificationRecord,
)
from control_plane.contracts.trusted_maintenance import (
    TrustedMaintenanceActorRule,
    TrustedMaintenanceEvidenceBinding,
    TrustedMaintenanceEvidenceRecord,
    TrustedMaintenancePolicyRecord,
    _normalize_sha256,
    _normalize_utc_timestamp,
    _parse_utc_timestamp,
    _required_decimal_id,
    _required_token,
    trusted_maintenance_expires_at,
)
from control_plane.workflows.ship import utc_now_timestamp


class TrustedMaintenancePolicyConflictError(ValueError):
    """Raised when trusted-maintenance policy history replays differently."""


class TrustedMaintenancePolicySequenceError(ValueError):
    """Raised when trusted-maintenance policy revision history is invalid."""


class TrustedMaintenanceAuthorityError(ValueError):
    """Raised when current classification or policy authority is missing or ambiguous."""


class TrustedMaintenanceRuleMatchError(ValueError):
    """Raised when signed GitHub actor/sender/event provenance matches no exact rule."""


class TrustedMaintenanceEvidenceConflictError(ValueError):
    """Raised when immutable trusted-maintenance evidence replays differently."""


TrustedMaintenancePolicyReadStatus = Literal["available", "missing", "ambiguous"]
TrustedMaintenancePolicyAppendStatus = Literal["written", "replayed"]
TrustedMaintenanceEvidenceAppendStatus = Literal["written", "replayed"]
TrustedMaintenancePolicyApplyMode = Literal["dry_run", "apply"]
TrustedMaintenancePolicyApplyStatus = Literal[
    "would_apply",
    "would_replay",
    "applied",
    "replayed",
]


class TrustedMaintenancePolicyReadStore(Protocol):
    def list_trusted_maintenance_policy_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[TrustedMaintenancePolicyRecord, ...]: ...


class TrustedMaintenancePolicyStore(TrustedMaintenancePolicyReadStore, Protocol):
    def compare_and_write_trusted_maintenance_policy_record(
        self,
        record: TrustedMaintenancePolicyRecord,
        *,
        expected_current_record_id: str,
        expected_current_policy_digest: str,
    ) -> Literal["written", "replayed"]: ...

    def write_trusted_maintenance_policy_record(
        self,
        record: TrustedMaintenancePolicyRecord,
    ) -> Literal["written", "replayed"]: ...

    def read_trusted_maintenance_policy_record(
        self,
        record_id: str,
    ) -> TrustedMaintenancePolicyRecord: ...


class TrustedMaintenanceEvidenceReadStore(Protocol):
    def list_trusted_maintenance_evidence_records(
        self,
        *,
        repository_id: str = "",
        repository_owner_id: str = "",
        repository: str = "",
        product: str = "",
        context: str = "",
        evidence_id: str = "",
        binding_sha256: str = "",
        pull_request_number: int | None = None,
        head_sha: str = "",
        classification_digest: str = "",
        policy_record_id: str = "",
        policy_digest: str = "",
        matched_actor_rule_id: str = "",
        pr_author_github_id: int | None = None,
        sender_github_id: int | None = None,
        event_name: str = "",
        event_action: str = "",
        delivery_id: str = "",
        limit: int | None = None,
    ) -> tuple[TrustedMaintenanceEvidenceRecord, ...]: ...


class TrustedMaintenanceEvidenceStore(TrustedMaintenanceEvidenceReadStore, Protocol):
    def write_trusted_maintenance_evidence_record(
        self,
        record: TrustedMaintenanceEvidenceRecord,
    ) -> Literal["written", "replayed"]: ...

    def read_trusted_maintenance_evidence_record(
        self,
        evidence_id: str,
    ) -> TrustedMaintenanceEvidenceRecord: ...


class TrustedMaintenanceAuthorityReadStore(
    TrustedMaintenancePolicyReadStore,
    TrustedMaintenanceEvidenceReadStore,
    Protocol,
):
    def list_tenant_repository_classification_records(
        self,
        *,
        repository_id: str = "",
        limit: int | None = None,
    ) -> tuple[TenantRepositoryClassificationRecord, ...]: ...


class TrustedMaintenancePolicyReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: TrustedMaintenancePolicyReadStatus
    repository_id: str
    product: str
    context: str
    current_record: TrustedMaintenancePolicyRecord | None = None
    history_count: int = Field(default=0, ge=0)
    generated_at: str

    @model_validator(mode="after")
    def _validate_read_model(self) -> "TrustedMaintenancePolicyReadModel":
        if self.schema_version != 1:
            raise ValueError("Unsupported trusted-maintenance policy read model schema version.")
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.product = _required_token(self.product, "product")
        self.context = _required_token(self.context, "context")
        self.generated_at = _normalize_utc_timestamp(self.generated_at, "generated_at")
        if self.status in {"missing", "ambiguous"} and self.current_record is not None:
            raise ValueError(
                "unavailable trusted-maintenance policy read model cannot include current_record"
            )
        if self.status == "available" and self.current_record is None:
            raise ValueError(
                "available trusted-maintenance policy read model requires current_record"
            )
        return self


class TrustedMaintenancePolicyApplyResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: TrustedMaintenancePolicyApplyStatus
    mode: TrustedMaintenancePolicyApplyMode
    repository_id: str
    product: str
    context: str
    policy_revision: int = Field(ge=1)
    record_id: str
    policy_digest: str
    supersedes_record_id: str | None = None
    effective_at: str

    @model_validator(mode="after")
    def _validate_result(self) -> "TrustedMaintenancePolicyApplyResult":
        if self.schema_version != 1:
            raise ValueError("Unsupported trusted-maintenance policy apply result schema version.")
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.product = _required_token(self.product, "product")
        self.context = _required_token(self.context, "context")
        self.record_id = _required_token(self.record_id, "record_id")
        self.policy_digest = _required_token(self.policy_digest, "policy_digest")
        if self.supersedes_record_id is not None:
            self.supersedes_record_id = _required_token(
                self.supersedes_record_id,
                "supersedes_record_id",
            )
        self.effective_at = _normalize_utc_timestamp(self.effective_at, "effective_at")
        return self


class TrustedMaintenancePolicyApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: TrustedMaintenancePolicyApplyMode = "apply"
    expected_current_record_id: str = ""
    expected_current_policy_digest: str = ""
    record: TrustedMaintenancePolicyRecord

    @model_validator(mode="after")
    def _validate_envelope(self) -> "TrustedMaintenancePolicyApplyEnvelope":
        if self.schema_version != 1:
            raise ValueError("Unsupported trusted-maintenance policy apply schema version.")
        self.expected_current_record_id = self.expected_current_record_id.strip()
        self.expected_current_policy_digest = self.expected_current_policy_digest.strip().lower()
        return self


class TrustedMaintenanceExpectedAuthority(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    classification_record_id: str
    classification_revision: int = Field(ge=1)
    classification_digest: str
    policy_record_id: str
    policy_revision: int = Field(ge=1)
    policy_digest: str

    @model_validator(mode="after")
    def _validate_expected_authority(self) -> "TrustedMaintenanceExpectedAuthority":
        if self.schema_version != 1:
            raise ValueError("Unsupported trusted-maintenance expected authority schema version.")
        self.classification_record_id = _required_token(
            self.classification_record_id,
            "classification_record_id",
        )
        self.classification_digest = _required_token(
            self.classification_digest,
            "classification_digest",
        )
        self.policy_record_id = _required_token(self.policy_record_id, "policy_record_id")
        self.policy_digest = _required_token(self.policy_digest, "policy_digest")
        return self


class TrustedMaintenanceGitHubEventFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    pr_author_github_id: int = Field(ge=1)
    pr_author_type: str
    pr_author_login: str = ""
    sender_github_id: int = Field(ge=1)
    sender_type: str
    sender_login: str = ""
    head_repository_id: str
    head_repository_owner_id: str
    head_repository: str
    event_name: str
    event_action: str
    source: str
    delivery_id: str
    signed_payload_sha256: str

    @model_validator(mode="after")
    def _validate_event_facts(self) -> "TrustedMaintenanceGitHubEventFacts":
        if self.schema_version != 1:
            raise ValueError("Unsupported trusted-maintenance GitHub event facts schema version.")
        self.pr_author_type = _required_token(self.pr_author_type, "pr_author_type")
        self.pr_author_login = self.pr_author_login.strip()
        self.sender_type = _required_token(self.sender_type, "sender_type")
        self.sender_login = self.sender_login.strip()
        self.head_repository_id = _required_decimal_id(
            self.head_repository_id,
            "head_repository_id",
        )
        self.head_repository_owner_id = _required_decimal_id(
            self.head_repository_owner_id,
            "head_repository_owner_id",
        )
        self.head_repository = self.head_repository.strip().lower()
        self.event_name = _required_token(self.event_name, "event_name")
        self.event_action = _required_token(self.event_action, "event_action")
        self.source = _required_token(self.source, "source")
        self.delivery_id = _required_token(self.delivery_id, "delivery_id")
        self.signed_payload_sha256 = _normalize_sha256(
            self.signed_payload_sha256,
            "signed_payload_sha256",
        )
        return self


class TrustedMaintenanceCurrentAuthority(NamedTuple):
    classification: TenantRepositoryClassificationRecord
    policy_record: TrustedMaintenancePolicyRecord


class TrustedMaintenanceCapturedEvidence(NamedTuple):
    record: TrustedMaintenanceEvidenceRecord
    path_result: TenantAdmissionPathResult


@dataclass(frozen=True)
class TrustedMaintenancePolicyAppendPlan:
    status: TrustedMaintenancePolicyAppendStatus
    current_record: TrustedMaintenancePolicyRecord | None
    superseded_current_record: TrustedMaintenancePolicyRecord | None = None


@dataclass(frozen=True)
class TrustedMaintenanceEvidenceAppendPlan:
    status: TrustedMaintenanceEvidenceAppendStatus
    existing_record: TrustedMaintenanceEvidenceRecord | None = None


def require_trusted_maintenance_policy_read_store(
    record_store: object,
) -> TrustedMaintenancePolicyReadStore:
    required_methods = ("list_trusted_maintenance_policy_records",)
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support trusted-maintenance policy reads: "
            f"{missing_summary}"
        )
    return cast(TrustedMaintenancePolicyReadStore, record_store)


def require_trusted_maintenance_policy_store(
    record_store: object,
) -> TrustedMaintenancePolicyStore:
    required_methods = (
        "list_trusted_maintenance_policy_records",
        "compare_and_write_trusted_maintenance_policy_record",
        "write_trusted_maintenance_policy_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support trusted-maintenance policy writes: "
            f"{missing_summary}"
        )
    return cast(TrustedMaintenancePolicyStore, record_store)


def require_trusted_maintenance_evidence_read_store(
    record_store: object,
) -> TrustedMaintenanceEvidenceReadStore:
    required_methods = ("list_trusted_maintenance_evidence_records",)
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support trusted-maintenance evidence reads: "
            f"{missing_summary}"
        )
    return cast(TrustedMaintenanceEvidenceReadStore, record_store)


def require_trusted_maintenance_evidence_store(
    record_store: object,
) -> TrustedMaintenanceEvidenceStore:
    required_methods = (
        "list_trusted_maintenance_evidence_records",
        "write_trusted_maintenance_evidence_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support trusted-maintenance evidence writes: "
            f"{missing_summary}"
        )
    return cast(TrustedMaintenanceEvidenceStore, record_store)


def get_trusted_maintenance_policy_read_model(
    *,
    repository_id: str,
    product: str,
    context: str,
    store: TrustedMaintenancePolicyReadStore,
) -> TrustedMaintenancePolicyReadModel:
    normalized_repository_id = _required_decimal_id(repository_id, "repository_id")
    normalized_product = _required_token(product, "product")
    normalized_context = _required_token(context, "context")
    records = store.list_trusted_maintenance_policy_records(
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
        return TrustedMaintenancePolicyReadModel(
            status="missing",
            repository_id=normalized_repository_id,
            product=normalized_product,
            context=normalized_context,
            history_count=0,
            generated_at=utc_now_timestamp(),
        )
    try:
        current_record = _validate_trusted_maintenance_policy_history(matching_records)
    except (TrustedMaintenancePolicyConflictError, TrustedMaintenancePolicySequenceError):
        return TrustedMaintenancePolicyReadModel(
            status="ambiguous",
            repository_id=normalized_repository_id,
            product=normalized_product,
            context=normalized_context,
            history_count=len(matching_records),
            generated_at=utc_now_timestamp(),
        )
    return TrustedMaintenancePolicyReadModel(
        status="available",
        repository_id=normalized_repository_id,
        product=normalized_product,
        context=normalized_context,
        current_record=current_record,
        history_count=len(matching_records),
        generated_at=utc_now_timestamp(),
    )


def apply_trusted_maintenance_policy(
    *,
    store: TrustedMaintenancePolicyReadStore,
    record: TrustedMaintenancePolicyRecord,
    expected_current_record_id: str = "",
    expected_current_policy_digest: str = "",
    mode: TrustedMaintenancePolicyApplyMode = "apply",
) -> TrustedMaintenancePolicyApplyResult:
    if mode == "apply":
        write_store = require_trusted_maintenance_policy_store(store)
        write_status = write_store.compare_and_write_trusted_maintenance_policy_record(
            record,
            expected_current_record_id=expected_current_record_id,
            expected_current_policy_digest=expected_current_policy_digest,
        )
        status: TrustedMaintenancePolicyApplyStatus = (
            "applied" if write_status == "written" else "replayed"
        )
    else:
        records = store.list_trusted_maintenance_policy_records(
            repository_id=record.repository_id,
            product=record.product,
            context=record.context,
        )
        plan = plan_trusted_maintenance_policy_apply(
            records=records,
            record=record,
            expected_current_record_id=expected_current_record_id,
            expected_current_policy_digest=expected_current_policy_digest,
        )
        status = "would_apply" if plan.status == "written" else "would_replay"
    return TrustedMaintenancePolicyApplyResult(
        status=status,
        mode=mode,
        repository_id=record.repository_id,
        product=record.product,
        context=record.context,
        policy_revision=record.policy_revision,
        record_id=record.record_id,
        policy_digest=record.policy_digest,
        supersedes_record_id=record.supersedes_record_id,
        effective_at=record.effective_at,
    )


def plan_trusted_maintenance_policy_apply(
    *,
    records: tuple[TrustedMaintenancePolicyRecord, ...],
    record: TrustedMaintenancePolicyRecord,
    expected_current_record_id: str,
    expected_current_policy_digest: str,
) -> TrustedMaintenancePolicyAppendPlan:
    if record.status != "active":
        raise ValueError("Trusted-maintenance policy apply requires an active candidate record.")
    normalized_expected_record_id = expected_current_record_id.strip()
    normalized_expected_digest = expected_current_policy_digest.strip().lower()
    if bool(normalized_expected_record_id) != bool(normalized_expected_digest):
        raise ValueError(
            "Trusted-maintenance policy apply requires both expected current record ID "
            "and digest, or neither for revision 1."
        )
    if record.policy_revision > 1 and not normalized_expected_record_id:
        raise ValueError(
            "Trusted-maintenance policy revisions after 1 require explicit expected current "
            "record ID and digest."
        )
    if record.policy_revision == 1 and (
        normalized_expected_record_id or normalized_expected_digest
    ):
        raise TrustedMaintenancePolicyConflictError(
            "First trusted-maintenance policy revision cannot declare an expected current tip."
        )

    matching_records = tuple(
        existing for existing in records if _policy_same_stream(existing, record)
    )
    plan = plan_trusted_maintenance_policy_append(records=matching_records, record=record)
    if plan.status == "replayed":
        current_record = plan.current_record
        if current_record is None or current_record.record_id != record.record_id:
            raise TrustedMaintenancePolicyConflictError(
                "Trusted-maintenance policy revision is stale; retry with the current tip."
            )
        _require_replayed_policy_predecessor(
            records=matching_records,
            replayed_record=current_record,
            expected_current_record_id=normalized_expected_record_id,
            expected_current_policy_digest=normalized_expected_digest,
        )
        return plan
    if plan.current_record is None:
        if normalized_expected_record_id or normalized_expected_digest:
            raise TrustedMaintenancePolicyConflictError(
                "Expected current trusted-maintenance policy tip does not match current state: "
                "repository has no existing trusted-maintenance policy record."
            )
        return plan
    _require_expected_policy_tip(
        current_record=plan.current_record,
        expected_current_record_id=normalized_expected_record_id,
        expected_current_policy_digest=normalized_expected_digest,
        allow_empty_revision_1=False,
    )
    return plan


def plan_trusted_maintenance_policy_append(
    *,
    records: tuple[TrustedMaintenancePolicyRecord, ...],
    record: TrustedMaintenancePolicyRecord,
) -> TrustedMaintenancePolicyAppendPlan:
    matching_records = tuple(
        existing for existing in records if _policy_same_stream(existing, record)
    )
    for existing in matching_records:
        if not _policy_same_scope(existing, record):
            raise TrustedMaintenancePolicyConflictError(
                "Trusted-maintenance policy history contains records for the same stream with different scope."
            )
    if not matching_records:
        if record.status != "active":
            raise TrustedMaintenancePolicySequenceError(
                "First trusted-maintenance policy record must be active."
            )
        if record.policy_revision != 1:
            raise TrustedMaintenancePolicySequenceError(
                "First trusted-maintenance policy record must have revision 1, "
                f"got {record.policy_revision}."
            )
        if record.supersedes_record_id:
            raise TrustedMaintenancePolicySequenceError(
                "First trusted-maintenance policy record cannot set supersedes_record_id."
            )
        return TrustedMaintenancePolicyAppendPlan(status="written", current_record=None)

    current_record = _validate_trusted_maintenance_policy_history(matching_records)
    same_revision_records = tuple(
        existing
        for existing in matching_records
        if existing.policy_revision == record.policy_revision
    )
    if same_revision_records:
        existing = same_revision_records[0]
        if _policy_replay_matches(existing=existing, replay=record):
            return TrustedMaintenancePolicyAppendPlan(
                status="replayed",
                current_record=current_record,
            )
        raise TrustedMaintenancePolicyConflictError(
            "Trusted-maintenance policy revision already exists with different payload."
        )

    if record.status != "active":
        raise TrustedMaintenancePolicySequenceError(
            "New trusted-maintenance policy revisions must be active."
        )
    expected_revision = current_record.policy_revision + 1
    if record.policy_revision != expected_revision:
        raise TrustedMaintenancePolicySequenceError(
            f"Next trusted-maintenance policy revision must be {expected_revision}, got {record.policy_revision}."
        )
    if record.supersedes_record_id != current_record.record_id:
        raise TrustedMaintenancePolicySequenceError(
            "Trusted-maintenance policy supersedes_record_id must be "
            f"'{current_record.record_id}', got '{record.supersedes_record_id}'."
        )
    superseded_current_record = TrustedMaintenancePolicyRecord.model_validate(
        current_record.model_dump(mode="json") | {"status": "superseded"}
    )
    return TrustedMaintenancePolicyAppendPlan(
        status="written",
        current_record=current_record,
        superseded_current_record=superseded_current_record,
    )


def plan_trusted_maintenance_evidence_append(
    *,
    records: tuple[TrustedMaintenanceEvidenceRecord, ...],
    record: TrustedMaintenanceEvidenceRecord,
) -> TrustedMaintenanceEvidenceAppendPlan:
    same_id_records = tuple(
        existing for existing in records if existing.evidence_id == record.evidence_id
    )
    if len(same_id_records) > 1:
        raise TrustedMaintenanceEvidenceConflictError(
            "Trusted-maintenance evidence history has multiple records with the same evidence_id."
        )
    if same_id_records:
        existing = same_id_records[0]
        if existing.binding.binding_sha256 == record.binding.binding_sha256:
            return TrustedMaintenanceEvidenceAppendPlan(
                status="replayed",
                existing_record=existing,
            )
        raise TrustedMaintenanceEvidenceConflictError(
            "Trusted-maintenance evidence already exists with different payload."
        )
    return TrustedMaintenanceEvidenceAppendPlan(status="written")


def trusted_maintenance_current_authority(
    *,
    store: TrustedMaintenanceAuthorityReadStore,
    candidate: TenantMergeCandidate,
    expected_authority: TrustedMaintenanceExpectedAuthority | None = None,
    evaluated_at: str,
) -> TrustedMaintenanceCurrentAuthority:
    normalized_evaluated_at = _normalize_utc_timestamp(evaluated_at, "evaluated_at")
    classification = _current_tenant_ui_classification(
        store=store,
        candidate=candidate,
        expected_authority=expected_authority,
        evaluated_at=normalized_evaluated_at,
    )
    policy_record = _current_trusted_maintenance_policy(
        store=store,
        candidate=candidate,
        expected_authority=expected_authority,
        evaluated_at=normalized_evaluated_at,
    )
    return TrustedMaintenanceCurrentAuthority(
        classification=classification, policy_record=policy_record
    )


def capture_trusted_maintenance_evidence(
    *,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
    policy_record: TrustedMaintenancePolicyRecord,
    event_facts: TrustedMaintenanceGitHubEventFacts,
    occurred_at: str,
    recorded_at: str,
) -> TrustedMaintenanceCapturedEvidence:
    normalized_occurred_at = _normalize_utc_timestamp(occurred_at, "occurred_at")
    normalized_recorded_at = _normalize_utc_timestamp(recorded_at, "recorded_at")
    if normalized_occurred_at != normalized_recorded_at:
        raise ValueError("Trusted-maintenance evidence must use DB/server occurred_at=recorded_at.")
    _require_current_authority_for_capture(
        candidate=candidate,
        classification=classification,
        policy_record=policy_record,
        evaluated_at=normalized_occurred_at,
    )
    if (
        event_facts.head_repository_id != candidate.repository_id
        or event_facts.head_repository_owner_id != candidate.repository_owner_id
        or event_facts.head_repository != candidate.repository
    ):
        raise TrustedMaintenanceRuleMatchError(
            "Trusted-maintenance v1 requires a same-repository PR head."
        )
    matching_rules = _matching_actor_rules(policy_record=policy_record, event_facts=event_facts)
    if len(matching_rules) != 1:
        raise TrustedMaintenanceRuleMatchError(
            "Trusted-maintenance evidence requires exactly one explicit numeric Bot actor/sender/event rule."
        )
    matched_rule = matching_rules[0]
    binding = TrustedMaintenanceEvidenceBinding(
        repository_id=candidate.repository_id,
        repository_owner_id=candidate.repository_owner_id,
        repository=candidate.repository,
        product=candidate.product,
        context=candidate.context,
        pull_request_number=candidate.pull_request_number,
        head_sha=candidate.head_sha,
        classification_record_id=classification.record_id,
        classification_revision=classification.classification_revision,
        classification_digest=classification.classification_digest,
        policy_record_id=policy_record.record_id,
        policy_revision=policy_record.policy_revision,
        policy_digest=policy_record.policy_digest,
        matched_actor_rule_id=matched_rule.rule_id,
        pr_author_github_id=event_facts.pr_author_github_id,
        pr_author_type="Bot",
        pr_author_login=event_facts.pr_author_login,
        sender_github_id=event_facts.sender_github_id,
        sender_type="Bot",
        sender_login=event_facts.sender_login,
        head_repository_id=event_facts.head_repository_id,
        head_repository_owner_id=event_facts.head_repository_owner_id,
        head_repository=event_facts.head_repository,
        event_name=event_facts.event_name,
        event_action=event_facts.event_action,
        source=event_facts.source,
        delivery_id=event_facts.delivery_id,
        signed_payload_sha256=event_facts.signed_payload_sha256,
    )
    record = TrustedMaintenanceEvidenceRecord(
        binding=binding,
        occurred_at=normalized_occurred_at,
        recorded_at=normalized_recorded_at,
        expires_at=trusted_maintenance_expires_at(
            occurred_at=normalized_occurred_at,
            ttl_seconds=policy_record.evidence_ttl_seconds,
        ),
    )
    return TrustedMaintenanceCapturedEvidence(
        record=record,
        path_result=trusted_maintenance_path_result(
            candidate=candidate,
            classification=classification,
            policy_record=policy_record,
            evidence_records=(record,),
            evaluated_at=normalized_occurred_at,
        ),
    )


def trusted_maintenance_path_result(
    *,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
    policy_record: TrustedMaintenancePolicyRecord,
    evidence_records: tuple[TrustedMaintenanceEvidenceRecord, ...],
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
            path_kind="trusted_maintenance",
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

    if policy_record.status != "active":
        return path_result(state="unavailable")
    evaluated_datetime = _parse_utc_timestamp(normalized_evaluated_at, "evaluated_at")
    if _parse_utc_timestamp(policy_record.effective_at, "effective_at") > evaluated_datetime:
        return path_result(state="unavailable")
    if not _classification_matches_candidate(classification=classification, candidate=candidate):
        return path_result(state="stale")
    if classification.classification_kind != "tenant_ui":
        return path_result(state="stale")
    if not _policy_matches_candidate(policy_record=policy_record, candidate=candidate):
        return path_result(state="stale")

    current_evidence = tuple(
        evidence
        for evidence in evidence_records
        if _parse_utc_timestamp(evidence.occurred_at, "occurred_at") <= evaluated_datetime
        and _evidence_matches_current_authority(
            evidence=evidence,
            candidate=candidate,
            classification=classification,
            policy_record=policy_record,
        )
        and _evidence_rule_current(evidence=evidence, policy_record=policy_record)
        and _evidence_expiration_matches_policy(
            evidence=evidence,
            policy_record=policy_record,
        )
    )
    if not current_evidence:
        stale_evidence = tuple(
            evidence
            for evidence in evidence_records
            if _parse_utc_timestamp(evidence.occurred_at, "occurred_at") <= evaluated_datetime
            and evidence.binding.repository_id == candidate.repository_id
            and evidence.binding.pull_request_number == candidate.pull_request_number
        )
        return path_result(state="stale" if stale_evidence else "pending")

    latest = max(
        current_evidence,
        key=lambda evidence: (
            _parse_utc_timestamp(evidence.occurred_at, "occurred_at"),
            evidence.evidence_id,
        ),
    )
    if latest.expires_at and (
        _parse_utc_timestamp(latest.expires_at, "expires_at") <= evaluated_datetime
    ):
        return path_result(
            state="stale",
            evidence_id=latest.evidence_id,
            evidence_digest=latest.evidence_digest,
        )
    return path_result(
        state="satisfied",
        evidence_id=latest.evidence_id,
        evidence_digest=latest.evidence_digest,
    )


def trusted_maintenance_path_result_from_store(
    *,
    store: TrustedMaintenanceAuthorityReadStore,
    candidate: TenantMergeCandidate,
    evaluated_at: str,
) -> TenantAdmissionPathResult | None:
    current = trusted_maintenance_current_authority(
        store=store,
        candidate=candidate,
        evaluated_at=evaluated_at,
    )
    evidence = store.list_trusted_maintenance_evidence_records(
        repository_id=candidate.repository_id,
        repository_owner_id=candidate.repository_owner_id,
        repository=candidate.repository,
        product=candidate.product,
        context=candidate.context,
        pull_request_number=candidate.pull_request_number,
    )
    return trusted_maintenance_path_result(
        candidate=candidate,
        classification=current.classification,
        policy_record=current.policy_record,
        evidence_records=evidence,
        evaluated_at=evaluated_at,
    )


def _validate_trusted_maintenance_policy_history(
    records: tuple[TrustedMaintenancePolicyRecord, ...],
) -> TrustedMaintenancePolicyRecord:
    first_record = records[0]
    if any(not _policy_same_scope(first_record, record) for record in records[1:]):
        raise TrustedMaintenancePolicyConflictError(
            "Trusted-maintenance policy history contains records for the same stream with different scope."
        )
    active_records = tuple(record for record in records if record.status == "active")
    if len(active_records) != 1:
        raise TrustedMaintenancePolicyConflictError(
            "Trusted-maintenance policy history must contain exactly one active record."
        )
    current_record = active_records[0]
    revision_map = {record.policy_revision: record for record in records}
    if len(revision_map) != len(records):
        raise TrustedMaintenancePolicyConflictError(
            "Trusted-maintenance policy history contains duplicate revisions."
        )
    expected_revisions = tuple(range(1, current_record.policy_revision + 1))
    if tuple(sorted(revision_map)) != expected_revisions:
        raise TrustedMaintenancePolicySequenceError(
            "Trusted-maintenance policy history contains revision gaps."
        )
    for revision, record in revision_map.items():
        if revision == 1:
            if record.supersedes_record_id:
                raise TrustedMaintenancePolicySequenceError(
                    "First trusted-maintenance policy record cannot set supersedes_record_id."
                )
            continue
        predecessor = revision_map[revision - 1]
        if record.supersedes_record_id != predecessor.record_id:
            raise TrustedMaintenancePolicySequenceError(
                "Trusted-maintenance policy supersedes chain is invalid."
            )
    return current_record


def _current_tenant_ui_classification(
    *,
    store: TrustedMaintenanceAuthorityReadStore,
    candidate: TenantMergeCandidate,
    expected_authority: TrustedMaintenanceExpectedAuthority | None,
    evaluated_at: str,
) -> TenantRepositoryClassificationRecord:
    records = store.list_tenant_repository_classification_records(
        repository_id=candidate.repository_id
    )
    repository_records = tuple(
        record for record in records if record.repository_id == candidate.repository_id
    )
    if not repository_records:
        raise TrustedMaintenanceAuthorityError(
            "Trusted-maintenance evidence requires current repository classification."
        )
    highest_revision = max(record.classification_revision for record in repository_records)
    current_records = tuple(
        record
        for record in repository_records
        if record.classification_revision == highest_revision
    )
    if len(current_records) != 1:
        raise TrustedMaintenanceAuthorityError(
            "Trusted-maintenance evidence requires exactly one current repository classification."
        )
    current_record = current_records[0]
    if _parse_utc_timestamp(current_record.classified_at, "classified_at") > _parse_utc_timestamp(
        evaluated_at,
        "evaluated_at",
    ):
        raise TrustedMaintenanceAuthorityError(
            "Trusted-maintenance classification is not effective yet."
        )
    if not _classification_matches_candidate(classification=current_record, candidate=candidate):
        raise TrustedMaintenanceAuthorityError(
            "Trusted-maintenance classification does not match candidate."
        )
    if current_record.classification_kind != "tenant_ui":
        raise TrustedMaintenanceAuthorityError(
            "Trusted-maintenance requires tenant_ui classification."
        )
    if expected_authority is not None and (
        current_record.record_id != expected_authority.classification_record_id
        or current_record.classification_revision != expected_authority.classification_revision
        or current_record.classification_digest != expected_authority.classification_digest
    ):
        raise TrustedMaintenanceAuthorityError(
            "Expected trusted-maintenance classification authority is stale."
        )
    return current_record


def _current_trusted_maintenance_policy(
    *,
    store: TrustedMaintenanceAuthorityReadStore,
    candidate: TenantMergeCandidate,
    expected_authority: TrustedMaintenanceExpectedAuthority | None,
    evaluated_at: str,
) -> TrustedMaintenancePolicyRecord:
    records = store.list_trusted_maintenance_policy_records(
        repository_id=candidate.repository_id,
        product=candidate.product,
        context=candidate.context,
    )
    stream_records = tuple(
        record
        for record in records
        if record.repository_id == candidate.repository_id
        and record.product == candidate.product
        and record.context == candidate.context
    )
    if not stream_records:
        raise TrustedMaintenanceAuthorityError(
            "Trusted-maintenance evidence requires current trusted-maintenance policy history."
        )
    try:
        current_record = _validate_trusted_maintenance_policy_history(stream_records)
    except (TrustedMaintenancePolicyConflictError, TrustedMaintenancePolicySequenceError) as error:
        raise TrustedMaintenanceAuthorityError(
            "Trusted-maintenance evidence requires valid current trusted-maintenance policy history."
        ) from error
    if not _policy_matches_candidate(policy_record=current_record, candidate=candidate):
        raise TrustedMaintenanceAuthorityError(
            "Trusted-maintenance policy does not match candidate."
        )
    if _parse_utc_timestamp(current_record.effective_at, "effective_at") > _parse_utc_timestamp(
        evaluated_at,
        "evaluated_at",
    ):
        raise TrustedMaintenanceAuthorityError("Trusted-maintenance policy is not effective yet.")
    if expected_authority is not None and (
        current_record.record_id != expected_authority.policy_record_id
        or current_record.policy_revision != expected_authority.policy_revision
        or current_record.policy_digest != expected_authority.policy_digest
    ):
        raise TrustedMaintenanceAuthorityError(
            "Expected trusted-maintenance policy authority is stale."
        )
    return current_record


def _require_current_authority_for_capture(
    *,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
    policy_record: TrustedMaintenancePolicyRecord,
    evaluated_at: str,
) -> None:
    if not _classification_matches_candidate(classification=classification, candidate=candidate):
        raise TrustedMaintenanceAuthorityError(
            "Trusted-maintenance classification does not match candidate."
        )
    if classification.classification_kind != "tenant_ui":
        raise TrustedMaintenanceAuthorityError(
            "Trusted-maintenance evidence requires tenant_ui classification."
        )
    if _parse_utc_timestamp(classification.classified_at, "classified_at") > _parse_utc_timestamp(
        evaluated_at,
        "evaluated_at",
    ):
        raise TrustedMaintenanceAuthorityError(
            "Trusted-maintenance classification is not effective yet."
        )
    if policy_record.status != "active":
        raise TrustedMaintenanceAuthorityError(
            "Trusted-maintenance evidence requires active policy authority."
        )
    if _parse_utc_timestamp(policy_record.effective_at, "effective_at") > _parse_utc_timestamp(
        evaluated_at,
        "evaluated_at",
    ):
        raise TrustedMaintenanceAuthorityError("Trusted-maintenance policy is not effective yet.")
    if not _policy_matches_candidate(policy_record=policy_record, candidate=candidate):
        raise TrustedMaintenanceAuthorityError(
            "Trusted-maintenance policy does not match candidate."
        )


def _matching_actor_rules(
    *,
    policy_record: TrustedMaintenancePolicyRecord,
    event_facts: TrustedMaintenanceGitHubEventFacts,
) -> tuple[TrustedMaintenanceActorRule, ...]:
    if event_facts.pr_author_type != "Bot" or event_facts.sender_type != "Bot":
        return ()
    return tuple(
        rule
        for rule in policy_record.actor_rules
        if rule.actor_github_id == event_facts.pr_author_github_id
        and rule.actor_type == "Bot"
        and event_facts.sender_github_id in rule.sender_github_ids
        and rule.sender_type == "Bot"
        and _event_allowed(
            rule=rule,
            event_name=event_facts.event_name,
            event_action=event_facts.event_action,
        )
    )


def _event_allowed(
    *,
    rule: TrustedMaintenanceActorRule,
    event_name: str,
    event_action: str,
) -> bool:
    return any(
        allowed.event_name == event_name and event_action in allowed.actions
        for allowed in rule.allowed_events
    )


def _evidence_matches_current_authority(
    *,
    evidence: TrustedMaintenanceEvidenceRecord,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
    policy_record: TrustedMaintenancePolicyRecord,
) -> bool:
    binding = evidence.binding
    return (
        binding.repository_id == candidate.repository_id
        and binding.repository_owner_id == candidate.repository_owner_id
        and binding.repository == candidate.repository
        and binding.product == candidate.product
        and binding.context == candidate.context
        and binding.pull_request_number == candidate.pull_request_number
        and binding.head_sha == candidate.head_sha
        and binding.classification_record_id == classification.record_id
        and binding.classification_revision == classification.classification_revision
        and binding.classification_digest == classification.classification_digest
        and binding.policy_record_id == policy_record.record_id
        and binding.policy_revision == policy_record.policy_revision
        and binding.policy_digest == policy_record.policy_digest
        and binding.head_repository_id == candidate.repository_id
        and binding.head_repository_owner_id == candidate.repository_owner_id
        and binding.head_repository == candidate.repository
    )


def _evidence_rule_current(
    *,
    evidence: TrustedMaintenanceEvidenceRecord,
    policy_record: TrustedMaintenancePolicyRecord,
) -> bool:
    binding = evidence.binding
    matching_rules = tuple(
        rule for rule in policy_record.actor_rules if rule.rule_id == binding.matched_actor_rule_id
    )
    if len(matching_rules) != 1:
        return False
    rule = matching_rules[0]
    return (
        binding.pr_author_type == "Bot"
        and binding.pr_author_github_id == rule.actor_github_id
        and binding.sender_type == "Bot"
        and binding.sender_github_id in rule.sender_github_ids
        and _event_allowed(
            rule=rule, event_name=binding.event_name, event_action=binding.event_action
        )
    )


def _evidence_expiration_matches_policy(
    *,
    evidence: TrustedMaintenanceEvidenceRecord,
    policy_record: TrustedMaintenancePolicyRecord,
) -> bool:
    return evidence.expires_at == trusted_maintenance_expires_at(
        occurred_at=evidence.occurred_at,
        ttl_seconds=policy_record.evidence_ttl_seconds,
    )


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


def _policy_matches_candidate(
    *,
    policy_record: TrustedMaintenancePolicyRecord,
    candidate: TenantMergeCandidate,
) -> bool:
    return (
        policy_record.repository_id == candidate.repository_id
        and policy_record.repository_owner_id == candidate.repository_owner_id
        and policy_record.repository == candidate.repository
        and policy_record.product == candidate.product
        and policy_record.context == candidate.context
    )


def _policy_same_stream(
    left: TrustedMaintenancePolicyRecord,
    right: TrustedMaintenancePolicyRecord,
) -> bool:
    return (
        left.repository_id == right.repository_id
        and left.product == right.product
        and left.context == right.context
    )


def _policy_same_scope(
    left: TrustedMaintenancePolicyRecord,
    right: TrustedMaintenancePolicyRecord,
) -> bool:
    return (
        _policy_same_stream(left, right)
        and left.repository_owner_id == right.repository_owner_id
        and left.repository == right.repository
    )


def _policy_replay_matches(
    *,
    existing: TrustedMaintenancePolicyRecord,
    replay: TrustedMaintenancePolicyRecord,
) -> bool:
    if existing.record_id != replay.record_id or existing.policy_digest != replay.policy_digest:
        return False
    return existing.status == replay.status or (
        existing.status == "superseded" and replay.status == "active"
    )


def _require_expected_policy_tip(
    *,
    current_record: TrustedMaintenancePolicyRecord,
    expected_current_record_id: str,
    expected_current_policy_digest: str,
    allow_empty_revision_1: bool,
) -> None:
    normalized_expected_record_id = expected_current_record_id.strip()
    normalized_expected_digest = expected_current_policy_digest.strip().lower()
    if allow_empty_revision_1 and current_record.policy_revision == 1:
        if normalized_expected_record_id or normalized_expected_digest:
            raise TrustedMaintenancePolicyConflictError(
                "First trusted-maintenance policy revision cannot declare an expected current tip."
            )
        return
    if not normalized_expected_record_id or not normalized_expected_digest:
        raise ValueError(
            "Trusted-maintenance policy apply requires explicit expected current tip record ID and digest."
        )
    if normalized_expected_record_id != current_record.record_id:
        raise TrustedMaintenancePolicyConflictError(
            "Expected current trusted-maintenance policy record ID does not match active current record ID."
        )
    if normalized_expected_digest != current_record.policy_digest:
        raise TrustedMaintenancePolicyConflictError(
            "Expected current trusted-maintenance policy digest does not match active current digest."
        )


def _require_replayed_policy_predecessor(
    *,
    records: tuple[TrustedMaintenancePolicyRecord, ...],
    replayed_record: TrustedMaintenancePolicyRecord,
    expected_current_record_id: str,
    expected_current_policy_digest: str,
) -> None:
    if replayed_record.policy_revision == 1:
        _require_expected_policy_tip(
            current_record=replayed_record,
            expected_current_record_id=expected_current_record_id,
            expected_current_policy_digest=expected_current_policy_digest,
            allow_empty_revision_1=True,
        )
        return
    predecessor_record_id = replayed_record.supersedes_record_id
    predecessor = next(
        (
            existing
            for existing in records
            if existing.record_id == predecessor_record_id
            and existing.policy_revision == replayed_record.policy_revision - 1
        ),
        None,
    )
    if predecessor is None:
        raise TrustedMaintenancePolicyConflictError(
            "Replayed trusted-maintenance policy record has no valid predecessor."
        )
    _require_expected_policy_tip(
        current_record=predecessor,
        expected_current_record_id=expected_current_record_id,
        expected_current_policy_digest=expected_current_policy_digest,
        allow_empty_revision_1=False,
    )
