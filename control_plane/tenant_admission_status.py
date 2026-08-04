from __future__ import annotations

from typing import Literal, Protocol, cast
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.authz_policy_record import LaunchplaneAuthzPolicyRecord
from control_plane.contracts.manager_preview_approval import ManagerPreviewApprovalEventRecord
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.repository_human_admission import (
    RepositoryHumanRolePolicyRecord,
    TenantTechnicalHumanWaiverEventRecord,
)
from control_plane.contracts.tenant_merge_eligibility import (
    TenantAdmissionPathKind,
    TenantAdmissionPathResult,
    TenantAdmissionPathState,
    TenantMergeCandidate,
    TenantMergeEligibilityDecision,
    TenantMergeEligibilityEvidenceInputs,
    TenantRepositoryClassificationLookup,
    TenantRepositoryClassificationLookupStatus,
    TenantRepositoryClassificationRecord,
    _normalize_utc_timestamp,
    evaluate_tenant_merge_eligibility,
)
from control_plane.contracts.trusted_maintenance import (
    TrustedMaintenanceEvidenceRecord,
    TrustedMaintenancePolicyRecord,
)
from control_plane.durable_operation_authorization import read_active_authz_policy_record
from control_plane.manager_preview_approval import (
    build_current_manager_preview_approval_binding,
    evaluate_manager_preview_approval,
    manager_preview_approval_required,
)
from control_plane.repository_human_admission import (
    get_repository_human_role_policy_read_model,
    technical_human_waiver_path_result,
)
from control_plane.trusted_maintenance import (
    TrustedMaintenanceAuthorityError,
    TrustedMaintenancePolicyConflictError,
    TrustedMaintenancePolicySequenceError,
    trusted_maintenance_path_result_from_store,
)
from control_plane.workflows.ship import utc_now_timestamp


TENANT_ADMISSION_STATUS_READ_ACTION = "tenant_admission.read"
TENANT_ADMISSION_STATUS_CONTEXT = "tenant-admission"

TenantAdmissionStatusCategory = Literal[
    "engineering",
    "pending",
    "manager-approved",
    "technical-waived",
    "maintenance-admitted",
    "stale",
    "denied",
    "unavailable",
]


class TenantAdmissionStatusStore(Protocol):
    def list_tenant_repository_classification_records(
        self,
        *,
        repository_id: str = "",
        limit: int | None = None,
    ) -> tuple[TenantRepositoryClassificationRecord, ...]: ...

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

    def list_authz_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[LaunchplaneAuthzPolicyRecord, ...]: ...

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]: ...

    def read_preview_generation_record(self, generation_id: str) -> PreviewGenerationRecord: ...

    def list_manager_preview_approval_event_records(
        self,
        *,
        product: str = "",
        context: str = "",
        repository: str = "",
        pr_number: int | None = None,
        preview_id: str = "",
        action: str = "",
        limit: int | None = None,
    ) -> tuple[ManagerPreviewApprovalEventRecord, ...]: ...


class TenantAdmissionStatusReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    category: TenantAdmissionStatusCategory
    classification_status: TenantRepositoryClassificationLookupStatus
    classification_kind: Literal["engineering", "tenant_ui", ""] = ""
    classification_revision: int = Field(default=0, ge=0)
    classification_digest: str = ""
    decision: TenantMergeEligibilityDecision
    paths: TenantMergeEligibilityEvidenceInputs
    generated_at: str

    @model_validator(mode="after")
    def _validate_read_model(self) -> "TenantAdmissionStatusReadModel":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant admission status read model schema version.")
        self.generated_at = _normalize_utc_timestamp(self.generated_at, "generated_at")
        if self.category == "engineering" and (
            self.decision.status != "admitted"
            or self.decision.reason_code != "engineering_normal_flow"
        ):
            raise ValueError("Engineering tenant admission status requires engineering admission.")
        admitted_categories = {
            "manager-approved": "manager_preview_approval",
            "technical-waived": "technical_human_waiver",
            "maintenance-admitted": "trusted_maintenance",
        }
        expected_evidence_kind = admitted_categories.get(self.category)
        if expected_evidence_kind is not None and (
            self.decision.status != "admitted"
            or self.decision.evidence_kind != expected_evidence_kind
        ):
            raise ValueError("Admitted tenant status category does not match decision evidence.")
        return self


def require_tenant_admission_status_store(
    record_store: object,
) -> TenantAdmissionStatusStore:
    required_methods = (
        "list_tenant_repository_classification_records",
        "list_repository_human_role_policy_records",
        "list_tenant_technical_human_waiver_event_records",
        "list_trusted_maintenance_policy_records",
        "list_trusted_maintenance_evidence_records",
        "list_authz_policy_records",
        "list_preview_records",
        "read_preview_generation_record",
        "list_manager_preview_approval_event_records",
    )
    missing_methods = tuple(
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    )
    if missing_methods:
        raise TypeError(
            "Launchplane record store does not support tenant admission status reads: "
            + ", ".join(missing_methods)
        )
    return cast(TenantAdmissionStatusStore, record_store)


def get_tenant_admission_status(
    *,
    store: TenantAdmissionStatusStore,
    candidate: TenantMergeCandidate,
    evaluated_at: str = "",
) -> TenantAdmissionStatusReadModel:
    normalized_evaluated_at = _normalize_utc_timestamp(
        evaluated_at or utc_now_timestamp(),
        "evaluated_at",
    )
    classification_lookup = _classification_lookup(store=store, candidate=candidate)
    initial_decision = evaluate_tenant_merge_eligibility(
        candidate=candidate,
        classification_lookup=classification_lookup,
        evaluated_at=normalized_evaluated_at,
    )
    classification = _current_classification(
        candidate=candidate,
        lookup=classification_lookup,
        decision=initial_decision,
    )
    empty_paths = TenantMergeEligibilityEvidenceInputs()
    if classification is None or initial_decision.reason_code == "engineering_normal_flow":
        return _read_model(
            decision=initial_decision,
            classification_lookup=classification_lookup,
            classification=classification,
            paths=empty_paths,
            generated_at=normalized_evaluated_at,
        )

    trusted_maintenance = _trusted_maintenance_path(
        store=store,
        candidate=candidate,
        classification=classification,
        evaluated_at=normalized_evaluated_at,
    )
    technical_human_waiver = _technical_human_waiver_path(
        store=store,
        candidate=candidate,
        classification=classification,
        evaluated_at=normalized_evaluated_at,
    )
    manager_preview_approval = _manager_preview_approval_path(
        store=store,
        candidate=candidate,
        classification=classification,
        evaluated_at=normalized_evaluated_at,
    )
    paths = TenantMergeEligibilityEvidenceInputs(
        trusted_maintenance=trusted_maintenance,
        technical_human_waiver=technical_human_waiver,
        manager_preview_approval=manager_preview_approval,
    )
    decision = evaluate_tenant_merge_eligibility(
        candidate=candidate,
        classification_lookup=classification_lookup,
        evaluated_at=normalized_evaluated_at,
        evidence_inputs=paths,
    )
    return _read_model(
        decision=decision,
        classification_lookup=classification_lookup,
        classification=classification,
        paths=paths,
        generated_at=normalized_evaluated_at,
    )


def _classification_lookup(
    *,
    store: TenantAdmissionStatusStore,
    candidate: TenantMergeCandidate,
) -> TenantRepositoryClassificationLookup:
    records = store.list_tenant_repository_classification_records(
        repository_id=candidate.repository_id
    )
    if not records:
        return TenantRepositoryClassificationLookup(
            status="missing",
            detail="No repository classification is available for the numeric repository ID.",
        )
    return TenantRepositoryClassificationLookup(status="available", records=records)


def _current_classification(
    *,
    candidate: TenantMergeCandidate,
    lookup: TenantRepositoryClassificationLookup,
    decision: TenantMergeEligibilityDecision,
) -> TenantRepositoryClassificationRecord | None:
    if not decision.classification_digest or decision.classification_revision < 1:
        return None
    matches = tuple(
        record
        for record in lookup.records
        if record.repository_id == candidate.repository_id
        and record.classification_revision == decision.classification_revision
        and record.classification_digest == decision.classification_digest
    )
    return matches[0] if len(matches) == 1 else None


def _trusted_maintenance_path(
    *,
    store: TenantAdmissionStatusStore,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
    evaluated_at: str,
) -> TenantAdmissionPathResult:
    policies = store.list_trusted_maintenance_policy_records(
        repository_id=candidate.repository_id,
        repository_owner_id=candidate.repository_owner_id,
        repository=candidate.repository,
        product=candidate.product,
        context=candidate.context,
    )
    if not policies:
        return _path_result(
            candidate=candidate,
            classification=classification,
            path_kind="trusted_maintenance",
            state="pending",
        )
    try:
        path_result = trusted_maintenance_path_result_from_store(
            store=store,
            candidate=candidate,
            evaluated_at=evaluated_at,
        )
    except (
        TrustedMaintenanceAuthorityError,
        TrustedMaintenancePolicyConflictError,
        TrustedMaintenancePolicySequenceError,
        LookupError,
        TypeError,
        ValueError,
    ):
        return _path_result(
            candidate=candidate,
            classification=classification,
            path_kind="trusted_maintenance",
            state="unavailable",
        )
    return path_result or _path_result(
        candidate=candidate,
        classification=classification,
        path_kind="trusted_maintenance",
        state="pending",
    )


def _technical_human_waiver_path(
    *,
    store: TenantAdmissionStatusStore,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
    evaluated_at: str,
) -> TenantAdmissionPathResult:
    events = store.list_tenant_technical_human_waiver_event_records(
        repository_id=candidate.repository_id,
        repository_owner_id=candidate.repository_owner_id,
        repository=candidate.repository,
        product=candidate.product,
        context=candidate.context,
        pull_request_number=candidate.pull_request_number,
    )
    role_policy = get_repository_human_role_policy_read_model(
        repository_id=candidate.repository_id,
        product=candidate.product,
        context=candidate.context,
        store=store,
    )
    if role_policy.status == "missing":
        return _path_result(
            candidate=candidate,
            classification=classification,
            path_kind="technical_human_waiver",
            state="stale" if events else "pending",
        )
    if role_policy.status != "available" or role_policy.current_record is None:
        return _path_result(
            candidate=candidate,
            classification=classification,
            path_kind="technical_human_waiver",
            state="unavailable",
        )
    try:
        authz_policy = read_active_authz_policy_record(store)
        return technical_human_waiver_path_result(
            candidate=candidate,
            classification=classification,
            role_policy_record=role_policy.current_record,
            authz_policy_record=authz_policy,
            events=events,
            evaluated_at=evaluated_at,
        )
    except (LookupError, TypeError, ValueError):
        return _path_result(
            candidate=candidate,
            classification=classification,
            path_kind="technical_human_waiver",
            state="unavailable",
        )


def _manager_preview_approval_path(
    *,
    store: TenantAdmissionStatusStore,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
    evaluated_at: str,
) -> TenantAdmissionPathResult:
    subject_events = tuple(
        event
        for event in store.list_manager_preview_approval_event_records(
            product=candidate.product,
            context=candidate.context,
            pr_number=candidate.pull_request_number,
            limit=200,
        )
        if _manager_preview_subject_matches_candidate(
            repository=event.binding.repository,
            pull_request_url=event.binding.pr_url,
            candidate=candidate,
        )
    )
    try:
        policy_record = read_active_authz_policy_record(store)
    except (LookupError, TypeError, ValueError):
        return _path_result(
            candidate=candidate,
            classification=classification,
            path_kind="manager_preview_approval",
            state="unavailable",
        )
    if not manager_preview_approval_required(
        policy_record=policy_record,
        product=candidate.product,
        context=candidate.context,
    ):
        return _path_result(
            candidate=candidate,
            classification=classification,
            path_kind="manager_preview_approval",
            state="unavailable",
        )
    previews = tuple(
        preview
        for preview in store.list_preview_records(
            context_name=candidate.context,
            anchor_pr_number=candidate.pull_request_number,
            limit=10,
        )
        if preview.context == candidate.context
        and _manager_preview_subject_matches_candidate(
            repository=preview.anchor_repo,
            pull_request_url=preview.anchor_pr_url,
            candidate=candidate,
        )
        and preview.anchor_pr_number == candidate.pull_request_number
        and preview.state != "destroyed"
    )
    if len(previews) != 1:
        return _path_result(
            candidate=candidate,
            classification=classification,
            path_kind="manager_preview_approval",
            state="stale" if subject_events else "unavailable",
        )
    preview = previews[0]
    if not preview.serving_generation_id:
        return _path_result(
            candidate=candidate,
            classification=classification,
            path_kind="manager_preview_approval",
            state="stale" if subject_events else "unavailable",
        )
    try:
        generation = store.read_preview_generation_record(preview.serving_generation_id)
    except (FileNotFoundError, LookupError, TypeError, ValueError):
        return _path_result(
            candidate=candidate,
            classification=classification,
            path_kind="manager_preview_approval",
            state="unavailable",
        )
    role_policy_read_model = get_repository_human_role_policy_read_model(
        repository_id=candidate.repository_id,
        product=candidate.product,
        context=candidate.context,
        store=store,
    )
    if role_policy_read_model.status == "ambiguous":
        return _path_result(
            candidate=candidate,
            classification=classification,
            path_kind="manager_preview_approval",
            state="unavailable",
        )
    role_policy_record = role_policy_read_model.current_record
    decision = evaluate_manager_preview_approval(
        product=candidate.product,
        preview=preview,
        generation=generation,
        policy_record=policy_record,
        events=subject_events,
        evaluated_at=evaluated_at,
        role_policy_record=role_policy_record,
    )
    try:
        binding = build_current_manager_preview_approval_binding(
            product=candidate.product,
            preview=preview,
            generation=generation,
        )
    except (LookupError, TypeError, ValueError):
        binding = None
    evidence_id = decision.approval_id
    evidence_digest = decision.current_binding_sha256
    if binding is not None and (
        binding.product != candidate.product
        or binding.context != candidate.context
        or not _manager_preview_subject_matches_candidate(
            repository=binding.repository,
            pull_request_url=binding.pr_url,
            candidate=candidate,
        )
        or binding.pr_number != candidate.pull_request_number
        or binding.head_sha != candidate.head_sha
    ):
        return _path_result(
            candidate=candidate,
            classification=classification,
            path_kind="manager_preview_approval",
            state="stale",
            evidence_id=evidence_id,
            evidence_digest=evidence_digest,
        )
    decision_state: dict[str, TenantAdmissionPathState] = {
        "approved": "satisfied",
        "pending": "pending",
        "changes_requested": "denied",
        "revoked": "denied",
        "stale": "stale",
        "unavailable": "unavailable",
    }
    return _path_result(
        candidate=candidate,
        classification=classification,
        path_kind="manager_preview_approval",
        state=decision_state[decision.status],
        evidence_id=evidence_id,
        evidence_digest=evidence_digest,
    )


def _manager_preview_subject_matches_candidate(
    *,
    repository: str,
    pull_request_url: str,
    candidate: TenantMergeCandidate,
) -> bool:
    repository_name = candidate.repository.split("/", 1)[1]
    if repository.casefold() not in {
        candidate.repository.casefold(),
        repository_name.casefold(),
    }:
        return False
    parsed_url = urlsplit(pull_request_url)
    path_parts = tuple(part for part in parsed_url.path.split("/") if part)
    owner, name = candidate.repository.split("/", 1)
    return (
        parsed_url.scheme == "https"
        and parsed_url.netloc.casefold() == "github.com"
        and not parsed_url.query
        and not parsed_url.fragment
        and len(path_parts) == 4
        and path_parts[0].casefold() == owner.casefold()
        and path_parts[1].casefold() == name.casefold()
        and path_parts[2].casefold() == "pull"
        and path_parts[3] == str(candidate.pull_request_number)
    )


def _path_result(
    *,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
    path_kind: TenantAdmissionPathKind,
    state: TenantAdmissionPathState,
    evidence_id: str = "",
    evidence_digest: str = "",
) -> TenantAdmissionPathResult:
    return TenantAdmissionPathResult(
        path_kind=path_kind,
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


def _read_model(
    *,
    decision: TenantMergeEligibilityDecision,
    classification_lookup: TenantRepositoryClassificationLookup,
    classification: TenantRepositoryClassificationRecord | None,
    paths: TenantMergeEligibilityEvidenceInputs,
    generated_at: str,
) -> TenantAdmissionStatusReadModel:
    return TenantAdmissionStatusReadModel(
        category=_status_category(decision=decision, paths=paths),
        classification_status=classification_lookup.status,
        classification_kind=classification.classification_kind if classification else "",
        classification_revision=(classification.classification_revision if classification else 0),
        classification_digest=(classification.classification_digest if classification else ""),
        decision=decision,
        paths=paths,
        generated_at=generated_at,
    )


def _status_category(
    *,
    decision: TenantMergeEligibilityDecision,
    paths: TenantMergeEligibilityEvidenceInputs,
) -> TenantAdmissionStatusCategory:
    if decision.reason_code == "engineering_normal_flow":
        return "engineering"
    if decision.status == "admitted":
        if decision.evidence_kind == "manager_preview_approval":
            return "manager-approved"
        if decision.evidence_kind == "technical_human_waiver":
            return "technical-waived"
        if decision.evidence_kind == "trusted_maintenance":
            return "maintenance-admitted"
        raise ValueError("Admitted tenant decision is missing admission evidence.")
    if decision.reason_code in {
        "classification_identity_drift",
        "evidence_identity_drift",
        "evidence_head_mismatch",
        "evidence_policy_drift",
        "evidence_stale",
    }:
        return "stale"
    if decision.reason_code == "evidence_denied":
        return "denied"
    if decision.reason_code in {
        "classification_missing",
        "classification_unknown",
        "classification_ambiguous",
        "evidence_unavailable",
    }:
        return "unavailable"
    manager_path = paths.manager_preview_approval
    if manager_path is not None:
        if manager_path.state == "denied":
            return "denied"
        if manager_path.state == "stale":
            return "stale"
        if manager_path.state == "unavailable":
            return "unavailable"
    return "pending"
