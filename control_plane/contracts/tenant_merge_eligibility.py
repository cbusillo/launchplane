from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


TenantRepositoryClassificationKind = Literal["engineering", "tenant_ui"]
TenantRepositoryClassificationLookupStatus = Literal["available", "missing", "unknown"]
TenantTrustedMaintenanceDecision = Literal["trusted_maintenance", "not_trusted"]
TenantManagerPreviewApprovalStatus = Literal[
    "pending",
    "approved",
    "changes_requested",
    "revoked",
    "stale",
    "unavailable",
]
TenantMergeEligibilityStatus = Literal["admitted", "blocked"]
TenantMergeEligibilityEvidenceKind = Literal[
    "none",
    "trusted_maintenance",
    "technical_human_waiver",
    "manager_preview_approval",
]
TenantMergeEligibilityReasonCode = Literal[
    "engineering_normal_flow",
    "trusted_maintenance_admitted",
    "technical_human_waiver_admitted",
    "manager_preview_approved",
    "manager_preview_required",
    "classification_missing",
    "classification_unknown",
    "classification_ambiguous",
    "classification_stale",
    "classification_identity_drift",
    "evidence_stale",
    "evidence_identity_drift",
    "evidence_head_mismatch",
]

_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class TenantMergeCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository_id: str
    repository_owner_id: str
    repository: str
    pull_request_number: int = Field(ge=1)
    head_sha: str

    @model_validator(mode="after")
    def _validate_candidate(self) -> "TenantMergeCandidate":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant merge candidate schema version.")
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.repository_owner_id = _required_decimal_id(
            self.repository_owner_id, "repository_owner_id"
        )
        self.repository = _normalize_repository(self.repository, "repository")
        self.head_sha = _normalize_git_sha(self.head_sha, "head_sha")
        return self


class TenantRepositoryClassificationRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    record_id: str = ""
    repository_id: str
    repository_owner_id: str
    repository: str
    classification_kind: TenantRepositoryClassificationKind
    classification_revision: int = Field(ge=1)
    classified_at: str
    current: bool = True
    classification_digest: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> "TenantRepositoryClassificationRecord":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant repository classification schema version.")
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.repository_owner_id = _required_decimal_id(
            self.repository_owner_id, "repository_owner_id"
        )
        self.repository = _normalize_repository(self.repository, "repository")
        self.classified_at = _normalize_utc_timestamp(self.classified_at, "classified_at")

        computed_record_id = build_tenant_repository_classification_record_id(
            repository_id=self.repository_id
        )
        if self.record_id:
            self.record_id = _required_token(self.record_id, "record_id")
            if self.record_id != computed_record_id:
                raise ValueError(
                    "tenant repository classification record_id must be keyed by repository_id"
                )
        else:
            self.record_id = computed_record_id

        computed_digest = tenant_repository_classification_digest(self)
        if self.classification_digest:
            self.classification_digest = _normalize_sha256(
                self.classification_digest, "classification_digest"
            )
            if self.classification_digest != computed_digest:
                raise ValueError(
                    "tenant repository classification classification_digest does not match payload"
                )
        else:
            self.classification_digest = computed_digest
        return self


class TenantRepositoryClassificationLookup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: TenantRepositoryClassificationLookupStatus = "available"
    records: tuple[TenantRepositoryClassificationRecord, ...] = ()
    detail: str = ""

    @model_validator(mode="after")
    def _validate_lookup(self) -> "TenantRepositoryClassificationLookup":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant repository classification lookup schema version.")
        self.detail = self.detail.strip()
        if self.status in {"missing", "unknown"} and self.records:
            raise ValueError(
                "tenant repository classification lookup cannot include records when unavailable"
            )
        return self


class TenantTrustedMaintenanceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    evidence_id: str
    scope: TenantMergeCandidate
    decision: TenantTrustedMaintenanceDecision
    decided_at: str
    current: bool = True
    evidence_digest: str = ""

    @model_validator(mode="after")
    def _validate_evidence(self) -> "TenantTrustedMaintenanceEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported trusted maintenance evidence schema version.")
        self.evidence_id = _required_token(self.evidence_id, "evidence_id")
        self.decided_at = _normalize_utc_timestamp(self.decided_at, "decided_at")
        computed_digest = tenant_trusted_maintenance_evidence_digest(self)
        if self.evidence_digest:
            self.evidence_digest = _normalize_sha256(self.evidence_digest, "evidence_digest")
            if self.evidence_digest != computed_digest:
                raise ValueError("trusted maintenance evidence_digest does not match payload")
        else:
            self.evidence_digest = computed_digest
        return self


class TenantTechnicalHumanWaiverEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    waiver_id: str
    scope: TenantMergeCandidate
    waiver_kind: Literal["technical_only"] = "technical_only"
    created_by_subject_kind: Literal["github_human"] = "github_human"
    selected_by_subject_kind: Literal["github_human"] = "github_human"
    authorized_human_github_id: int = Field(ge=1)
    authorized_human_login: str
    authorized_at: str
    current: bool = True
    waiver_digest: str = ""

    @model_validator(mode="after")
    def _validate_waiver(self) -> "TenantTechnicalHumanWaiverEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant technical human waiver schema version.")
        self.waiver_id = _required_token(self.waiver_id, "waiver_id")
        self.authorized_human_login = _required_token(
            self.authorized_human_login, "authorized_human_login"
        )
        self.authorized_at = _normalize_utc_timestamp(self.authorized_at, "authorized_at")
        computed_digest = tenant_technical_human_waiver_digest(self)
        if self.waiver_digest:
            self.waiver_digest = _normalize_sha256(self.waiver_digest, "waiver_digest")
            if self.waiver_digest != computed_digest:
                raise ValueError("technical human waiver_digest does not match payload")
        else:
            self.waiver_digest = computed_digest
        return self


class TenantManagerPreviewApprovalEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    approval_id: str
    scope: TenantMergeCandidate
    status: TenantManagerPreviewApprovalStatus
    binding_sha256: str
    evaluated_at: str
    current: bool = True
    event_id: str = ""
    approval_digest: str = ""

    @model_validator(mode="after")
    def _validate_approval(self) -> "TenantManagerPreviewApprovalEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant manager preview approval evidence schema version.")
        self.approval_id = _required_token(self.approval_id, "approval_id")
        self.event_id = self.event_id.strip()
        self.binding_sha256 = _normalize_sha256(self.binding_sha256, "binding_sha256")
        self.evaluated_at = _normalize_utc_timestamp(self.evaluated_at, "evaluated_at")
        computed_digest = tenant_manager_preview_approval_evidence_digest(self)
        if self.approval_digest:
            self.approval_digest = _normalize_sha256(self.approval_digest, "approval_digest")
            if self.approval_digest != computed_digest:
                raise ValueError("manager preview approval_digest does not match payload")
        else:
            self.approval_digest = computed_digest
        return self


class TenantMergeEligibilityEvidenceInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    trusted_maintenance: TenantTrustedMaintenanceEvidence | None = None
    technical_human_waiver: TenantTechnicalHumanWaiverEvidence | None = None
    manager_preview_approval: TenantManagerPreviewApprovalEvidence | None = None

    @model_validator(mode="after")
    def _validate_inputs(self) -> "TenantMergeEligibilityEvidenceInputs":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant merge eligibility evidence input schema version.")
        return self


class TenantMergeEligibilityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: TenantMergeEligibilityStatus
    reason_code: TenantMergeEligibilityReasonCode
    detail: str
    repository_id: str
    repository_owner_id: str
    repository: str
    pull_request_number: int = Field(ge=1)
    head_sha: str
    classification_kind: TenantRepositoryClassificationKind | Literal[""] = ""
    classification_revision: int = Field(default=0, ge=0)
    classification_digest: str = ""
    evidence_kind: TenantMergeEligibilityEvidenceKind = "none"
    evidence_id: str = ""
    evidence_digest: str = ""
    evaluated_at: str
    decision_binding_sha256: str = ""

    @model_validator(mode="after")
    def _validate_decision(self) -> "TenantMergeEligibilityDecision":
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant merge eligibility decision schema version.")
        self.detail = _required_token(self.detail, "detail")
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.repository_owner_id = _required_decimal_id(
            self.repository_owner_id, "repository_owner_id"
        )
        self.repository = _normalize_repository(self.repository, "repository")
        self.head_sha = _normalize_git_sha(self.head_sha, "head_sha")
        self.evaluated_at = _normalize_utc_timestamp(self.evaluated_at, "evaluated_at")
        self.evidence_id = self.evidence_id.strip()
        if self.classification_digest:
            self.classification_digest = _normalize_sha256(
                self.classification_digest, "classification_digest"
            )
        if self.evidence_digest:
            self.evidence_digest = _normalize_sha256(self.evidence_digest, "evidence_digest")
        if self.status == "admitted" and not self.classification_kind:
            raise ValueError("admitted tenant merge eligibility decisions require classification")
        if self.status == "admitted" and not self.classification_digest:
            raise ValueError(
                "admitted tenant merge eligibility decisions require classification digest"
            )
        if self.status == "admitted" and self.classification_kind == "tenant_ui":
            if self.evidence_kind == "none" or not self.evidence_id or not self.evidence_digest:
                raise ValueError("tenant UI admissions require binding evidence")
        if self.status == "blocked" and self.reason_code in {
            "engineering_normal_flow",
            "trusted_maintenance_admitted",
            "technical_human_waiver_admitted",
            "manager_preview_approved",
        }:
            raise ValueError("blocked tenant merge eligibility decision has admitting reason")
        computed_digest = tenant_merge_eligibility_decision_binding_sha256(self)
        if self.decision_binding_sha256:
            self.decision_binding_sha256 = _normalize_sha256(
                self.decision_binding_sha256, "decision_binding_sha256"
            )
            if self.decision_binding_sha256 != computed_digest:
                raise ValueError("tenant merge eligibility decision binding digest mismatch")
        else:
            self.decision_binding_sha256 = computed_digest
        return self

    @property
    def admitted(self) -> bool:
        return self.status == "admitted"


def build_tenant_repository_classification_record_id(*, repository_id: str) -> str:
    return (
        f"tenant-repository-classification-{_required_decimal_id(repository_id, 'repository_id')}"
    )


def tenant_repository_classification_digest(record: TenantRepositoryClassificationRecord) -> str:
    return _model_sha256(record, exclude={"classification_digest"})


def tenant_trusted_maintenance_evidence_digest(
    evidence: TenantTrustedMaintenanceEvidence,
) -> str:
    return _model_sha256(evidence, exclude={"evidence_digest"})


def tenant_technical_human_waiver_digest(evidence: TenantTechnicalHumanWaiverEvidence) -> str:
    return _model_sha256(evidence, exclude={"waiver_digest"})


def tenant_manager_preview_approval_evidence_digest(
    evidence: TenantManagerPreviewApprovalEvidence,
) -> str:
    return _model_sha256(evidence, exclude={"approval_digest"})


def tenant_merge_eligibility_decision_binding_sha256(
    decision: TenantMergeEligibilityDecision,
) -> str:
    return _model_sha256(decision, exclude={"decision_binding_sha256"})


def evaluate_tenant_merge_eligibility(
    *,
    candidate: TenantMergeCandidate,
    classification_lookup: TenantRepositoryClassificationLookup,
    evaluated_at: str,
    evidence_inputs: TenantMergeEligibilityEvidenceInputs | None = None,
) -> TenantMergeEligibilityDecision:
    normalized_evaluated_at = _normalize_utc_timestamp(evaluated_at, "evaluated_at")
    selection = _select_current_classification(
        candidate=candidate,
        lookup=classification_lookup,
    )
    classification = selection.classification
    if classification is None:
        reason_code, detail = selection.blocked_reason
        return _decision(
            candidate=candidate,
            classification=None,
            status="blocked",
            reason_code=reason_code,
            detail=detail,
            evaluated_at=normalized_evaluated_at,
        )

    if classification.classification_kind == "engineering":
        return _decision(
            candidate=candidate,
            classification=classification,
            status="admitted",
            reason_code="engineering_normal_flow",
            detail="Engineering repository is eligible for the normal merge flow.",
            evaluated_at=normalized_evaluated_at,
        )

    evidence = evidence_inputs or TenantMergeEligibilityEvidenceInputs()
    first_blocked_reason: tuple[TenantMergeEligibilityReasonCode, str] | None = None

    trusted_maintenance_evidence = evidence.trusted_maintenance
    trusted_maintenance = _evaluate_trusted_maintenance_evidence(
        candidate=candidate,
        evidence=trusted_maintenance_evidence,
    )
    if trusted_maintenance.admitted and trusted_maintenance_evidence is not None:
        return _decision(
            candidate=candidate,
            classification=classification,
            status="admitted",
            reason_code="trusted_maintenance_admitted",
            detail="Tenant UI merge is eligible through current trusted-maintenance evidence.",
            evaluated_at=normalized_evaluated_at,
            evidence_kind="trusted_maintenance",
            evidence_id=trusted_maintenance_evidence.evidence_id,
            evidence_digest=trusted_maintenance_evidence.evidence_digest,
        )
    first_blocked_reason = _first_blocked_reason(
        first_blocked_reason, trusted_maintenance.blocked_reason
    )

    technical_human_waiver_evidence = evidence.technical_human_waiver
    technical_waiver = _evaluate_technical_human_waiver(
        candidate=candidate,
        evidence=technical_human_waiver_evidence,
    )
    if technical_waiver.admitted and technical_human_waiver_evidence is not None:
        return _decision(
            candidate=candidate,
            classification=classification,
            status="admitted",
            reason_code="technical_human_waiver_admitted",
            detail="Tenant UI merge is eligible through a current exact-head technical human waiver.",
            evaluated_at=normalized_evaluated_at,
            evidence_kind="technical_human_waiver",
            evidence_id=technical_human_waiver_evidence.waiver_id,
            evidence_digest=technical_human_waiver_evidence.waiver_digest,
        )
    first_blocked_reason = _first_blocked_reason(
        first_blocked_reason, technical_waiver.blocked_reason
    )

    manager_preview_approval_evidence = evidence.manager_preview_approval
    manager_approval = _evaluate_manager_preview_approval(
        candidate=candidate,
        evidence=manager_preview_approval_evidence,
    )
    if manager_approval.admitted and manager_preview_approval_evidence is not None:
        return _decision(
            candidate=candidate,
            classification=classification,
            status="admitted",
            reason_code="manager_preview_approved",
            detail="Tenant UI merge is eligible through exact manager preview approval.",
            evaluated_at=normalized_evaluated_at,
            evidence_kind="manager_preview_approval",
            evidence_id=manager_preview_approval_evidence.approval_id,
            evidence_digest=manager_preview_approval_evidence.approval_digest,
        )
    first_blocked_reason = _first_blocked_reason(
        first_blocked_reason, manager_approval.blocked_reason
    )

    reason_code, detail = first_blocked_reason or (
        "manager_preview_required",
        "Tenant UI repository requires manager preview approval before merge eligibility.",
    )
    return _decision(
        candidate=candidate,
        classification=classification,
        status="blocked",
        reason_code=reason_code,
        detail=detail,
        evaluated_at=normalized_evaluated_at,
    )


@dataclass(frozen=True)
class _ClassificationSelection:
    classification: TenantRepositoryClassificationRecord | None
    blocked_reason: tuple[TenantMergeEligibilityReasonCode, str]


@dataclass(frozen=True)
class _EvidenceEvaluation:
    admitted: bool = False
    blocked_reason: tuple[TenantMergeEligibilityReasonCode, str] | None = None


def _select_current_classification(
    *,
    candidate: TenantMergeCandidate,
    lookup: TenantRepositoryClassificationLookup,
) -> _ClassificationSelection:
    if lookup.status == "missing":
        return _ClassificationSelection(
            classification=None,
            blocked_reason=(
                "classification_missing",
                "No repository classification record is available for this GitHub repository ID.",
            ),
        )
    if lookup.status == "unknown":
        return _ClassificationSelection(
            classification=None,
            blocked_reason=(
                "classification_unknown",
                "Repository classification lookup returned an unknown state.",
            ),
        )

    if not lookup.records:
        return _ClassificationSelection(
            classification=None,
            blocked_reason=(
                "classification_missing",
                "No repository classification record is available for this GitHub repository ID.",
            ),
        )

    repository_id_matches = tuple(
        record for record in lookup.records if record.repository_id == candidate.repository_id
    )
    if not repository_id_matches:
        return _ClassificationSelection(
            classification=None,
            blocked_reason=(
                "classification_identity_drift",
                "Repository classification records do not match the immutable GitHub repository ID.",
            ),
        )

    current_matches = tuple(record for record in repository_id_matches if record.current)
    if not current_matches:
        return _ClassificationSelection(
            classification=None,
            blocked_reason=(
                "classification_stale",
                "Repository classification records for this GitHub repository ID are stale.",
            ),
        )
    if len(current_matches) != 1:
        return _ClassificationSelection(
            classification=None,
            blocked_reason=(
                "classification_ambiguous",
                "More than one current repository classification record matched this GitHub repository ID.",
            ),
        )

    classification = current_matches[0]
    if (
        classification.repository_owner_id != candidate.repository_owner_id
        or classification.repository != candidate.repository
    ):
        return _ClassificationSelection(
            classification=None,
            blocked_reason=(
                "classification_identity_drift",
                "Repository classification identity no longer matches the PR repository identity.",
            ),
        )
    return _ClassificationSelection(
        classification=classification,
        blocked_reason=("classification_missing", ""),
    )


def _evaluate_trusted_maintenance_evidence(
    *,
    candidate: TenantMergeCandidate,
    evidence: TenantTrustedMaintenanceEvidence | None,
) -> _EvidenceEvaluation:
    if evidence is None:
        return _EvidenceEvaluation()
    blocked_reason = _evidence_scope_blocked_reason(
        candidate=candidate,
        scope=evidence.scope,
        evidence_label="Trusted maintenance evidence",
    )
    if blocked_reason is not None:
        return _EvidenceEvaluation(blocked_reason=blocked_reason)
    if not evidence.current:
        return _EvidenceEvaluation(
            blocked_reason=(
                "evidence_stale",
                "Trusted maintenance evidence is not current for this PR head.",
            )
        )
    if evidence.decision == "trusted_maintenance":
        return _EvidenceEvaluation(admitted=True)
    return _EvidenceEvaluation()


def _evaluate_technical_human_waiver(
    *,
    candidate: TenantMergeCandidate,
    evidence: TenantTechnicalHumanWaiverEvidence | None,
) -> _EvidenceEvaluation:
    if evidence is None:
        return _EvidenceEvaluation()
    blocked_reason = _evidence_scope_blocked_reason(
        candidate=candidate,
        scope=evidence.scope,
        evidence_label="Technical human waiver",
    )
    if blocked_reason is not None:
        return _EvidenceEvaluation(blocked_reason=blocked_reason)
    if not evidence.current:
        return _EvidenceEvaluation(
            blocked_reason=(
                "evidence_stale",
                "Technical human waiver is not current for this PR head.",
            )
        )
    return _EvidenceEvaluation(admitted=True)


def _evaluate_manager_preview_approval(
    *,
    candidate: TenantMergeCandidate,
    evidence: TenantManagerPreviewApprovalEvidence | None,
) -> _EvidenceEvaluation:
    if evidence is None:
        return _EvidenceEvaluation()
    blocked_reason = _evidence_scope_blocked_reason(
        candidate=candidate,
        scope=evidence.scope,
        evidence_label="Manager preview approval",
    )
    if blocked_reason is not None:
        return _EvidenceEvaluation(blocked_reason=blocked_reason)
    if not evidence.current or evidence.status == "stale":
        return _EvidenceEvaluation(
            blocked_reason=(
                "evidence_stale",
                "Manager preview approval evidence is not current for this PR head.",
            )
        )
    if evidence.status == "approved":
        return _EvidenceEvaluation(admitted=True)
    return _EvidenceEvaluation()


def _evidence_scope_blocked_reason(
    *,
    candidate: TenantMergeCandidate,
    scope: TenantMergeCandidate,
    evidence_label: str,
) -> tuple[TenantMergeEligibilityReasonCode, str] | None:
    if (
        scope.repository_id != candidate.repository_id
        or scope.repository_owner_id != candidate.repository_owner_id
        or scope.repository != candidate.repository
        or scope.pull_request_number != candidate.pull_request_number
    ):
        return (
            "evidence_identity_drift",
            f"{evidence_label} does not match the exact repository and pull request identity.",
        )
    if scope.head_sha != candidate.head_sha:
        return (
            "evidence_head_mismatch",
            f"{evidence_label} is bound to a different PR head SHA.",
        )
    return None


def _first_blocked_reason(
    current: tuple[TenantMergeEligibilityReasonCode, str] | None,
    candidate: tuple[TenantMergeEligibilityReasonCode, str] | None,
) -> tuple[TenantMergeEligibilityReasonCode, str] | None:
    return current or candidate


def _decision(
    *,
    candidate: TenantMergeCandidate,
    status: TenantMergeEligibilityStatus,
    reason_code: TenantMergeEligibilityReasonCode,
    detail: str,
    evaluated_at: str,
    classification: TenantRepositoryClassificationRecord | None,
    evidence_kind: TenantMergeEligibilityEvidenceKind = "none",
    evidence_id: str = "",
    evidence_digest: str = "",
) -> TenantMergeEligibilityDecision:
    return TenantMergeEligibilityDecision(
        status=status,
        reason_code=reason_code,
        detail=detail,
        repository_id=candidate.repository_id,
        repository_owner_id=candidate.repository_owner_id,
        repository=candidate.repository,
        pull_request_number=candidate.pull_request_number,
        head_sha=candidate.head_sha,
        classification_kind=classification.classification_kind if classification else "",
        classification_revision=classification.classification_revision if classification else 0,
        classification_digest=classification.classification_digest if classification else "",
        evidence_kind=evidence_kind,
        evidence_id=evidence_id,
        evidence_digest=evidence_digest,
        evaluated_at=evaluated_at,
    )


def _required_decimal_id(value: str, label: str) -> str:
    normalized = _required_token(value, label)
    if not normalized.isdecimal() or int(normalized) <= 0:
        raise ValueError(f"tenant merge eligibility {label} requires a positive numeric ID")
    return normalized


def _normalize_repository(value: str, label: str) -> str:
    normalized = _required_token(value, label).lower()
    owner, separator, name = normalized.partition("/")
    if not separator or not owner.strip() or not name.strip() or "/" in name:
        raise ValueError(f"tenant merge eligibility {label} must be a GitHub owner/name")
    return normalized


def _normalize_git_sha(value: str, label: str) -> str:
    normalized = _required_token(value, label).lower()
    if _GIT_SHA_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"tenant merge eligibility {label} must be an exact Git SHA")
    return normalized


def _normalize_sha256(value: str, label: str) -> str:
    normalized = _required_token(value, label).lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"tenant merge eligibility {label} must be a lowercase SHA-256")
    return normalized


def _required_token(value: str, label: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"tenant merge eligibility requires {label}")
    return normalized


def _normalize_utc_timestamp(value: str, label: str) -> str:
    normalized = _required_token(value, label)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(
            f"tenant merge eligibility {label} must be an ISO-8601 timestamp"
        ) from error
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError(f"tenant merge eligibility {label} requires a timezone")
    return parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _model_sha256(model: BaseModel, *, exclude: set[str]) -> str:
    return _canonical_sha256(model.model_dump(mode="json", exclude=exclude, exclude_none=True))


def _canonical_sha256(payload: object) -> str:
    canonical_json = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical_json.encode("utf-8")).hexdigest()
