from __future__ import annotations

from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.tenant_merge_eligibility import (
    TenantMergeCandidate,
    TenantMergeEligibilityDecision,
    TenantMergeEligibilityEvidenceInputs,
    TenantRepositoryClassificationLookup,
    TenantRepositoryClassificationLookupStatus,
    TenantRepositoryClassificationRecord,
    _normalize_utc_timestamp,
    evaluate_tenant_merge_eligibility,
)
from control_plane.workflows.ship import utc_now_timestamp


TENANT_ADMISSION_STATUS_READ_ACTION = "tenant_admission.read"
TENANT_ADMISSION_STATUS_CONTEXT = "tenant-admission"

TenantAdmissionStatusCategory = Literal[
    "engineering",
    "eligible",
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
        self, *, repository_id: str = "", limit: int | None = None
    ) -> tuple[TenantRepositoryClassificationRecord, ...]: ...


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
        if self.category == "eligible" and (
            self.decision.status != "admitted" or self.decision.reason_code != "tenant_normal_flow"
        ):
            raise ValueError("Eligible tenant status requires normal-flow admission.")
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
    required_methods = ("list_tenant_repository_classification_records",)
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
    *, store: TenantAdmissionStatusStore, candidate: TenantMergeCandidate, evaluated_at: str = ""
) -> TenantAdmissionStatusReadModel:
    normalized_evaluated_at = _normalize_utc_timestamp(
        evaluated_at or utc_now_timestamp(), "evaluated_at"
    )
    classification_lookup = _classification_lookup(store=store, candidate=candidate)
    decision = evaluate_tenant_merge_eligibility(
        candidate=candidate,
        classification_lookup=classification_lookup,
        evaluated_at=normalized_evaluated_at,
    )
    return _read_model(
        decision=decision,
        classification_lookup=classification_lookup,
        classification=_current_classification(
            candidate=candidate, lookup=classification_lookup, decision=decision
        ),
        paths=TenantMergeEligibilityEvidenceInputs(),
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


def _read_model(
    *,
    decision: TenantMergeEligibilityDecision,
    classification_lookup: TenantRepositoryClassificationLookup,
    classification: TenantRepositoryClassificationRecord | None,
    paths: TenantMergeEligibilityEvidenceInputs,
    generated_at: str,
) -> TenantAdmissionStatusReadModel:
    return TenantAdmissionStatusReadModel(
        category=_status_category(decision=decision),
        classification_status=classification_lookup.status,
        classification_kind=classification.classification_kind if classification else "",
        classification_revision=(classification.classification_revision if classification else 0),
        classification_digest=(classification.classification_digest if classification else ""),
        decision=decision,
        paths=paths,
        generated_at=generated_at,
    )


def _status_category(*, decision: TenantMergeEligibilityDecision) -> TenantAdmissionStatusCategory:
    if decision.reason_code == "engineering_normal_flow":
        return "engineering"
    if decision.reason_code == "tenant_normal_flow":
        return "eligible"
    if decision.reason_code == "classification_identity_drift":
        return "stale"
    return "unavailable"
