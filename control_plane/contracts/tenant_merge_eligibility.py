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
TenantAdmissionPathKind = Literal[
    "trusted_maintenance", "technical_human_waiver", "manager_preview_approval"
]
TenantAdmissionPathState = Literal["satisfied", "pending", "denied", "stale", "unavailable"]
TenantMergeEligibilityStatus = Literal["admitted", "blocked"]
TenantMergeEligibilityEvidenceKind = Literal[
    "none", "trusted_maintenance", "technical_human_waiver", "manager_preview_approval"
]
TenantMergeEligibilityReasonCode = Literal[
    "engineering_normal_flow",
    "trusted_maintenance_admitted",
    "technical_human_waiver_admitted",
    "manager_preview_approved",
    "manager_preview_required",
    "evidence_denied",
    "evidence_stale",
    "evidence_unavailable",
    "evidence_identity_drift",
    "evidence_head_mismatch",
    "evidence_policy_drift",
    "classification_missing",
    "classification_unknown",
    "classification_ambiguous",
    "classification_identity_drift",
]

_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class TenantMergeCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    context: str
    repository_id: str
    repository_owner_id: str
    repository: str
    pull_request_number: int = Field(ge=1)
    head_sha: str

    @model_validator(mode="after")
    def _validate_candidate(self) -> TenantMergeCandidate:
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant merge candidate schema version.")
        self.product = _required_token(self.product, "product")
        self.context = _required_token(self.context, "context")
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
    product: str
    context: str
    classification_kind: TenantRepositoryClassificationKind
    classification_revision: int = Field(ge=1)
    classified_at: str
    source: str
    reason: str
    supersedes_record_id: str | None = None
    classification_digest: str = ""

    @model_validator(mode="after")
    def _validate_record(self) -> TenantRepositoryClassificationRecord:
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant repository classification schema version.")
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.repository_owner_id = _required_decimal_id(
            self.repository_owner_id, "repository_owner_id"
        )
        self.repository = _normalize_repository(self.repository, "repository")
        self.product = _required_token(self.product, "product")
        self.context = _required_token(self.context, "context")
        self.classified_at = _normalize_utc_timestamp(self.classified_at, "classified_at")
        self.source = _required_token(self.source, "source")
        self.reason = _required_token(self.reason, "reason")
        if self.supersedes_record_id is not None:
            self.supersedes_record_id = _required_token(
                self.supersedes_record_id, "supersedes_record_id"
            )

        computed_record_id = build_tenant_repository_classification_record_id(
            repository_id=self.repository_id,
            classification_revision=self.classification_revision,
        )
        if self.record_id:
            self.record_id = _required_token(self.record_id, "record_id")
            if self.record_id != computed_record_id:
                raise ValueError(
                    "tenant repository classification record_id must include repository_id and classification_revision"
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
    def _validate_lookup(self) -> TenantRepositoryClassificationLookup:
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant repository classification lookup schema version.")
        self.detail = self.detail.strip()
        if self.status in {"missing", "unknown"} and self.records:
            raise ValueError(
                "tenant repository classification lookup cannot include records when unavailable"
            )
        return self


class TenantAdmissionPathResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    path_kind: TenantAdmissionPathKind
    state: TenantAdmissionPathState
    evidence_id: str = ""
    evidence_digest: str = ""
    repository_id: str
    repository_owner_id: str
    repository: str
    pull_request_number: int = Field(ge=1)
    head_sha: str
    classification_digest: str

    @model_validator(mode="after")
    def _validate_path_result(self) -> TenantAdmissionPathResult:
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant admission path result schema version.")
        self.evidence_id = self.evidence_id.strip()
        if self.evidence_digest:
            self.evidence_digest = _normalize_sha256(self.evidence_digest, "evidence_digest")
        self.repository_id = _required_decimal_id(self.repository_id, "repository_id")
        self.repository_owner_id = _required_decimal_id(
            self.repository_owner_id, "repository_owner_id"
        )
        self.repository = _normalize_repository(self.repository, "repository")
        self.head_sha = _normalize_git_sha(self.head_sha, "head_sha")
        self.classification_digest = _normalize_sha256(
            self.classification_digest, "classification_digest"
        )
        if self.state == "satisfied" and (not self.evidence_id or not self.evidence_digest):
            raise ValueError("Satisfied path result requires evidence_id and evidence_digest")
        return self


class TenantMergeEligibilityEvidenceInputs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    trusted_maintenance: TenantAdmissionPathResult | None = None
    technical_human_waiver: TenantAdmissionPathResult | None = None
    manager_preview_approval: TenantAdmissionPathResult | None = None

    @model_validator(mode="after")
    def _validate_inputs(self) -> TenantMergeEligibilityEvidenceInputs:
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant merge eligibility evidence input schema version.")
        if self.trusted_maintenance and self.trusted_maintenance.path_kind != "trusted_maintenance":
            raise ValueError("trusted_maintenance slot must have path_kind 'trusted_maintenance'")
        if (
            self.technical_human_waiver
            and self.technical_human_waiver.path_kind != "technical_human_waiver"
        ):
            raise ValueError(
                "technical_human_waiver slot must have path_kind 'technical_human_waiver'"
            )
        if (
            self.manager_preview_approval
            and self.manager_preview_approval.path_kind != "manager_preview_approval"
        ):
            raise ValueError(
                "manager_preview_approval slot must have path_kind 'manager_preview_approval'"
            )
        return self


class TenantMergeEligibilityDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: TenantMergeEligibilityStatus
    reason_code: TenantMergeEligibilityReasonCode
    detail: str
    product: str
    context: str
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
    def _validate_decision(self) -> TenantMergeEligibilityDecision:
        if self.schema_version != 1:
            raise ValueError("Unsupported tenant merge eligibility decision schema version.")
        self.detail = _required_token(self.detail, "detail")
        self.product = _required_token(self.product, "product")
        self.context = _required_token(self.context, "context")
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
        if self.status == "admitted":
            if not self.classification_kind or not self.classification_digest:
                raise ValueError(
                    "admitted tenant merge eligibility decisions require classification"
                )
            if self.classification_kind == "tenant_ui" and (
                self.evidence_kind == "none" or not self.evidence_id or not self.evidence_digest
            ):
                raise ValueError("tenant UI admissions require binding evidence")
        elif self.reason_code in {
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


def build_tenant_repository_classification_record_id(
    *, repository_id: str, classification_revision: int
) -> str:
    return f"tenant-repository-classification-{_required_decimal_id(repository_id, 'repository_id')}-r{classification_revision}"


def tenant_repository_classification_digest(record: TenantRepositoryClassificationRecord) -> str:
    return _model_sha256(record, exclude={"classification_digest"})


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
    selection = _select_current_classification(candidate=candidate, lookup=classification_lookup)
    classification = selection.classification
    if classification is None:
        return _decision(
            candidate=candidate,
            classification=None,
            status="blocked",
            reason_code=selection.blocked_reason[0],
            detail=selection.blocked_reason[1],
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
    paths = (
        (
            "trusted_maintenance",
            evidence.trusted_maintenance,
            "trusted_maintenance_admitted",
            "Tenant UI merge is eligible through current trusted-maintenance evidence.",
        ),
        (
            "technical_human_waiver",
            evidence.technical_human_waiver,
            "technical_human_waiver_admitted",
            "Tenant UI merge is eligible through a current exact-head technical human waiver.",
        ),
        (
            "manager_preview_approval",
            evidence.manager_preview_approval,
            "manager_preview_approved",
            "Tenant UI merge is eligible through exact manager preview approval.",
        ),
    )

    for path_kind, path_result, admitting_reason, detail in paths:
        if path_result is None:
            continue
        eval_res = _evaluate_path_result(
            candidate=candidate, classification=classification, path_result=path_result
        )
        if eval_res.admitted:
            return _decision(
                candidate=candidate,
                classification=classification,
                status="admitted",
                reason_code=admitting_reason,  # type: ignore
                detail=detail,
                evaluated_at=normalized_evaluated_at,
                evidence_kind=path_kind,  # type: ignore
                evidence_id=path_result.evidence_id,
                evidence_digest=path_result.evidence_digest,
            )

    blocked_code, blocked_detail = _determine_blocked_reason(
        candidate=candidate, classification=classification, evidence=evidence
    )
    return _decision(
        candidate=candidate,
        classification=classification,
        status="blocked",
        reason_code=blocked_code,
        detail=blocked_detail,
        evaluated_at=normalized_evaluated_at,
    )


@dataclass(frozen=True)
class _ClassificationSelection:
    classification: TenantRepositoryClassificationRecord | None
    blocked_reason: tuple[TenantMergeEligibilityReasonCode, str]


@dataclass(frozen=True)
class _PathEvaluation:
    admitted: bool = False
    blocked_reason: tuple[TenantMergeEligibilityReasonCode, str] | None = None


def _select_current_classification(
    *, candidate: TenantMergeCandidate, lookup: TenantRepositoryClassificationLookup
) -> _ClassificationSelection:
    if lookup.status == "missing":
        return _ClassificationSelection(
            None,
            (
                "classification_missing",
                "No repository classification record is available for this GitHub repository ID.",
            ),
        )
    if lookup.status == "unknown":
        return _ClassificationSelection(
            None,
            (
                "classification_unknown",
                "Repository classification lookup returned an unknown state.",
            ),
        )
    if not lookup.records:
        return _ClassificationSelection(
            None,
            (
                "classification_missing",
                "No repository classification record is available for this GitHub repository ID.",
            ),
        )

    matching_repo_records = tuple(
        r for r in lookup.records if r.repository_id == candidate.repository_id
    )
    if not matching_repo_records:
        return _ClassificationSelection(
            None,
            (
                "classification_identity_drift",
                "Repository classification records do not match the GitHub repository ID.",
            ),
        )

    max_rev = max(r.classification_revision for r in matching_repo_records)
    highest_rev_records = tuple(
        r for r in matching_repo_records if r.classification_revision == max_rev
    )
    if len(highest_rev_records) > 1:
        return _ClassificationSelection(
            None,
            (
                "classification_ambiguous",
                "Multiple repository classification records exist for the highest revision.",
            ),
        )

    classification = highest_rev_records[0]
    if (
        classification.repository_owner_id != candidate.repository_owner_id
        or classification.repository != candidate.repository
        or classification.product != candidate.product
        or classification.context != candidate.context
    ):
        return _ClassificationSelection(
            None,
            (
                "classification_identity_drift",
                "Repository classification identity/product/context does not match candidate identity.",
            ),
        )

    return _ClassificationSelection(classification, ("classification_missing", ""))


def _evaluate_path_result(
    *,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
    path_result: TenantAdmissionPathResult,
) -> _PathEvaluation:
    if (
        path_result.repository_id != candidate.repository_id
        or path_result.repository_owner_id != candidate.repository_owner_id
        or path_result.repository != candidate.repository
        or path_result.pull_request_number != candidate.pull_request_number
    ):
        return _PathEvaluation(
            blocked_reason=(
                "evidence_identity_drift",
                f"{path_result.path_kind} evidence does not match repository or PR identity.",
            )
        )

    if path_result.head_sha != candidate.head_sha:
        return _PathEvaluation(
            blocked_reason=(
                "evidence_head_mismatch",
                f"{path_result.path_kind} evidence is bound to a different PR head SHA.",
            )
        )

    if path_result.classification_digest != classification.classification_digest:
        return _PathEvaluation(
            blocked_reason=(
                "evidence_policy_drift",
                f"{path_result.path_kind} evidence classification digest does not match active classification digest.",
            )
        )

    if path_result.state == "satisfied":
        return _PathEvaluation(admitted=True)
    if path_result.state == "stale":
        return _PathEvaluation(
            blocked_reason=("evidence_stale", f"{path_result.path_kind} evidence state is stale.")
        )
    if path_result.state == "denied":
        return _PathEvaluation(
            blocked_reason=("evidence_denied", f"{path_result.path_kind} evidence state is denied.")
        )
    if path_result.state == "unavailable":
        return _PathEvaluation(
            blocked_reason=(
                "evidence_unavailable",
                f"{path_result.path_kind} evidence state is unavailable.",
            )
        )
    return _PathEvaluation(
        blocked_reason=(
            "manager_preview_required",
            "Manager preview approval is required before merge eligibility.",
        )
    )


def _determine_blocked_reason(
    *,
    candidate: TenantMergeCandidate,
    classification: TenantRepositoryClassificationRecord,
    evidence: TenantMergeEligibilityEvidenceInputs,
) -> tuple[TenantMergeEligibilityReasonCode, str]:
    if evidence.manager_preview_approval is not None:
        eval_res = _evaluate_path_result(
            candidate=candidate,
            classification=classification,
            path_result=evidence.manager_preview_approval,
        )
        if eval_res.blocked_reason is not None:
            return eval_res.blocked_reason

    for path_result in (evidence.technical_human_waiver, evidence.trusted_maintenance):
        if path_result is not None:
            eval_res = _evaluate_path_result(
                candidate=candidate, classification=classification, path_result=path_result
            )
            if eval_res.blocked_reason is not None:
                reason, _ = eval_res.blocked_reason
                if reason in {
                    "evidence_identity_drift",
                    "evidence_head_mismatch",
                    "evidence_policy_drift",
                }:
                    return eval_res.blocked_reason

    return (
        "manager_preview_required",
        "Tenant UI repository requires manager preview approval before merge eligibility.",
    )


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
        product=candidate.product,
        context=candidate.context,
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
