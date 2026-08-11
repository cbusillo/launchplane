from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.advisory_check_projection import is_launchplane_projected_check


MergeReadinessState = Literal[
    "ready",
    "blocked_owner_evidence",
    "blocked_checks",
    "blocked_engineering_review",
    "blocked_policy",
    "blocked_candidate_identity",
    "unknown",
]
MergeReadinessTechnicalCheckStatus = Literal["pass", "pending", "fail", "unknown"]
MergeReadinessStructuralCandidateStatus = Literal[
    "exact", "recorded_rolling", "mismatch", "unknown"
]
MergeReadinessPolicyDimension = Literal[
    "impact",
    "technical_checks",
    "engineering_review",
    "ruleset",
    "merge_train",
    "authorization",
    "admission_algorithm",
]
MergeReadinessReasonCode = Literal[
    "owner_not_required",
    "owner_acceptance_valid",
    "owner_acceptance_missing",
    "owner_changes_requested",
    "owner_acceptance_revoked",
    "owner_acceptance_stale",
    "owner_evidence_stale",
    "owner_evidence_unavailable",
    "owner_authority_unavailable",
    "owner_authority_denied",
    "owner_preview_evidence_unavailable",
    "owner_preview_evidence_stale",
    "owner_review_expired",
    "owner_preview_isolation_insufficient",
    "owner_contributing_identity_unknown",
    "owner_self_review_denied",
    "owner_review_context_missing",
    "owner_evidence_head_mismatch",
    "owner_evidence_tree_mismatch",
    "checks_passed",
    "checks_pending",
    "checks_failed",
    "checks_unknown",
    "checks_head_mismatch",
    "engineering_review_approved",
    "engineering_review_pending",
    "engineering_review_changes_requested",
    "engineering_review_blocked",
    "engineering_review_unknown",
    "engineering_review_stale",
    "engineering_review_head_mismatch",
    "engineering_review_tree_mismatch",
    "engineering_review_evidence_missing",
    "policy_fingerprints_match",
    "policy_impact_missing",
    "policy_impact_drift",
    "policy_technical_checks_missing",
    "policy_technical_checks_drift",
    "policy_engineering_review_missing",
    "policy_engineering_review_drift",
    "policy_ruleset_missing",
    "policy_ruleset_drift",
    "policy_merge_train_missing",
    "policy_merge_train_drift",
    "policy_authorization_missing",
    "policy_authorization_drift",
    "policy_admission_algorithm_missing",
    "policy_admission_algorithm_drift",
    "candidate_exact",
    "candidate_recorded_rolling",
    "candidate_identity_mismatch",
    "candidate_identity_unknown",
    "candidate_queue_mismatch",
    "candidate_base_mismatch",
    "candidate_head_mismatch",
    "controller_scope_mismatch",
    "controller_lease_held",
    "controller_lease_missing",
    "controller_lease_lost",
    "controller_lease_expired",
    "expected_sha_match",
    "expected_sha_mismatch",
]

MERGE_READINESS_STATE_PRECEDENCE: tuple[MergeReadinessState, ...] = (
    "unknown",
    "blocked_candidate_identity",
    "blocked_policy",
    "blocked_engineering_review",
    "blocked_checks",
    "blocked_owner_evidence",
    "ready",
)
MERGE_READINESS_POLICY_DIMENSIONS: tuple[MergeReadinessPolicyDimension, ...] = (
    "impact",
    "technical_checks",
    "engineering_review",
    "ruleset",
    "merge_train",
    "authorization",
    "admission_algorithm",
)

_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


def _required_token(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty")
    return normalized


def _normalize_repository(value: str) -> str:
    normalized = _required_token(value, "repository").lower()
    if normalized.count("/") != 1 or not all(normalized.split("/", 1)):
        raise ValueError("repository must use owner/name")
    return normalized


def _normalize_git_sha(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name).lower()
    if _GIT_SHA_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a Git SHA")
    return normalized


def _normalize_optional_git_sha(value: str, field_name: str) -> str:
    normalized = value.strip().lower()
    if normalized and _GIT_SHA_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a Git SHA when present")
    return normalized


def _normalize_sha256(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name).lower()
    if _SHA256_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a lowercase SHA-256 digest")
    return normalized


def _normalize_optional_sha256(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    return _normalize_sha256(value, field_name)


def _normalize_timestamp(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _normalize_reasons(
    reasons: tuple[MergeReadinessReasonCode, ...],
) -> tuple[MergeReadinessReasonCode, ...]:
    if not reasons:
        raise ValueError("merge readiness facets require at least one reason code")
    return tuple(sorted(set(reasons)))


class MergeReadinessTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    repository: str
    pull_request_number: int = Field(ge=1)
    base_branch: str
    base_sha: str
    pull_request_head_sha: str
    pull_request_tree_sha: str
    expected_effect_sha: str
    queue_position: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_target(self) -> "MergeReadinessTarget":
        if self.schema_version != 1:
            raise ValueError("Unsupported merge readiness target schema version.")
        object.__setattr__(self, "repository", _normalize_repository(self.repository))
        object.__setattr__(self, "base_branch", _required_token(self.base_branch, "base_branch"))
        for field_name in (
            "base_sha",
            "pull_request_head_sha",
            "pull_request_tree_sha",
            "expected_effect_sha",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_git_sha(str(getattr(self, field_name)), field_name),
            )
        return self


class MergeReadinessAdvisoryObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    state: str
    app_id: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _validate_observation(self) -> "MergeReadinessAdvisoryObservation":
        object.__setattr__(self, "name", _required_token(self.name, "name"))
        object.__setattr__(self, "state", _required_token(self.state, "state"))
        return self


class MergeReadinessTechnicalCheckEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    head_sha: str
    status: MergeReadinessTechnicalCheckStatus
    required_checks: tuple[str, ...] = ()
    advisory_observations: tuple[MergeReadinessAdvisoryObservation, ...] = ()

    @model_validator(mode="after")
    def _validate_evidence(self) -> "MergeReadinessTechnicalCheckEvidence":
        object.__setattr__(self, "head_sha", _normalize_git_sha(self.head_sha, "head_sha"))
        normalized_checks = tuple(
            sorted({_required_token(check, "required_checks") for check in self.required_checks})
        )
        if any(is_launchplane_projected_check(check) for check in normalized_checks):
            raise ValueError("Launchplane advisory checks cannot be required technical checks")
        object.__setattr__(self, "required_checks", normalized_checks)
        if any(
            not is_launchplane_projected_check(observation.name)
            for observation in self.advisory_observations
        ):
            raise ValueError("Advisory observations must use reserved Launchplane check names")
        object.__setattr__(
            self,
            "advisory_observations",
            tuple(
                sorted(
                    self.advisory_observations,
                    key=lambda item: (item.name.casefold(), item.app_id or 0, item.state),
                )
            ),
        )
        return self


class MergeReadinessEngineeringEvidenceReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_id: str
    head_sha: str
    tree_sha: str
    evidence_digest: str

    @model_validator(mode="after")
    def _validate_reference(self) -> "MergeReadinessEngineeringEvidenceReference":
        object.__setattr__(self, "run_id", _required_token(self.run_id, "run_id"))
        object.__setattr__(self, "head_sha", _normalize_git_sha(self.head_sha, "head_sha"))
        object.__setattr__(self, "tree_sha", _normalize_git_sha(self.tree_sha, "tree_sha"))
        object.__setattr__(
            self,
            "evidence_digest",
            _normalize_sha256(self.evidence_digest, "evidence_digest"),
        )
        return self


class MergeReadinessPolicyFingerprintEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dimension: MergeReadinessPolicyDimension
    expected_sha256: str
    current_sha256: str | None = None

    @model_validator(mode="after")
    def _validate_fingerprint(self) -> "MergeReadinessPolicyFingerprintEvidence":
        object.__setattr__(
            self,
            "expected_sha256",
            _normalize_sha256(self.expected_sha256, "expected_sha256"),
        )
        object.__setattr__(
            self,
            "current_sha256",
            _normalize_optional_sha256(self.current_sha256, "current_sha256"),
        )
        return self


class MergeReadinessPolicyFingerprints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    impact: MergeReadinessPolicyFingerprintEvidence
    technical_checks: MergeReadinessPolicyFingerprintEvidence
    engineering_review: MergeReadinessPolicyFingerprintEvidence
    ruleset: MergeReadinessPolicyFingerprintEvidence
    merge_train: MergeReadinessPolicyFingerprintEvidence
    authorization: MergeReadinessPolicyFingerprintEvidence
    admission_algorithm: MergeReadinessPolicyFingerprintEvidence

    @model_validator(mode="after")
    def _validate_dimensions(self) -> "MergeReadinessPolicyFingerprints":
        for dimension in MERGE_READINESS_POLICY_DIMENSIONS:
            if getattr(self, dimension).dimension != dimension:
                raise ValueError(f"{dimension} policy fingerprint uses the wrong dimension")
        return self

    def ordered(self) -> tuple[MergeReadinessPolicyFingerprintEvidence, ...]:
        return tuple(getattr(self, dimension) for dimension in MERGE_READINESS_POLICY_DIMENSIONS)


class MergeReadinessCandidateEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    structural_status: MergeReadinessStructuralCandidateStatus
    record_id: str = ""
    repository: str = ""
    base_sha: str = ""
    candidate_sha: str = ""
    pull_request_number: int | None = Field(default=None, ge=1)
    queue_position: int | None = Field(default=None, ge=1)
    pull_request_head_sha: str = ""

    @model_validator(mode="after")
    def _validate_candidate(self) -> "MergeReadinessCandidateEvidence":
        object.__setattr__(self, "record_id", self.record_id.strip())
        repository = self.repository.strip()
        if repository:
            repository = _normalize_repository(repository)
        object.__setattr__(self, "repository", repository)
        for field_name in ("base_sha", "candidate_sha", "pull_request_head_sha"):
            object.__setattr__(
                self,
                field_name,
                _normalize_optional_git_sha(str(getattr(self, field_name)), field_name),
            )
        return self


class MergeReadinessFenceEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    controller_key: str = ""
    controller_repository: str = ""
    controller_base_branch: str = ""
    controller_status: str = ""
    expected_lease_owner: str
    observed_lease_owner: str = ""
    lease_expires_at: str = ""
    observed_effect_sha: str = ""

    @model_validator(mode="after")
    def _validate_fence(self) -> "MergeReadinessFenceEvidence":
        object.__setattr__(self, "controller_key", self.controller_key.strip())
        repository = self.controller_repository.strip()
        if repository:
            repository = _normalize_repository(repository)
        object.__setattr__(self, "controller_repository", repository)
        for field_name in (
            "controller_base_branch",
            "controller_status",
            "observed_lease_owner",
            "lease_expires_at",
        ):
            object.__setattr__(self, field_name, str(getattr(self, field_name)).strip())
        object.__setattr__(
            self,
            "expected_lease_owner",
            _required_token(self.expected_lease_owner, "expected_lease_owner"),
        )
        object.__setattr__(
            self,
            "observed_effect_sha",
            _normalize_optional_git_sha(self.observed_effect_sha, "observed_effect_sha"),
        )
        if self.lease_expires_at:
            object.__setattr__(
                self,
                "lease_expires_at",
                _normalize_timestamp(self.lease_expires_at, "lease_expires_at"),
            )
        return self


class MergeReadinessOwnerFacet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product: str
    system: str
    action: str
    environment: str
    owner_status: str
    owner_reason_code: str
    binding_sha256: str = ""
    event_id: str = ""
    state: MergeReadinessState
    reason_codes: tuple[MergeReadinessReasonCode, ...]

    @model_validator(mode="after")
    def _validate_facet(self) -> "MergeReadinessOwnerFacet":
        for field_name in (
            "product",
            "system",
            "action",
            "environment",
            "owner_status",
            "owner_reason_code",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        object.__setattr__(self, "binding_sha256", self.binding_sha256.strip().lower())
        if self.binding_sha256:
            object.__setattr__(
                self,
                "binding_sha256",
                _normalize_sha256(self.binding_sha256, "binding_sha256"),
            )
        object.__setattr__(self, "event_id", self.event_id.strip())
        if self.state not in {"ready", "blocked_owner_evidence", "unknown"}:
            raise ValueError("Owner readiness facet uses an invalid canonical state")
        object.__setattr__(self, "reason_codes", _normalize_reasons(self.reason_codes))
        return self


class MergeReadinessTechnicalChecksFacet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    head_sha: str
    status: MergeReadinessTechnicalCheckStatus
    required_checks: tuple[str, ...]
    advisory_observations: tuple[MergeReadinessAdvisoryObservation, ...] = ()
    state: MergeReadinessState
    reason_codes: tuple[MergeReadinessReasonCode, ...]

    @model_validator(mode="after")
    def _validate_facet(self) -> "MergeReadinessTechnicalChecksFacet":
        object.__setattr__(self, "head_sha", _normalize_git_sha(self.head_sha, "head_sha"))
        normalized_checks = tuple(
            sorted({_required_token(check, "required_checks") for check in self.required_checks})
        )
        if any(is_launchplane_projected_check(check) for check in normalized_checks):
            raise ValueError("Launchplane advisory checks cannot be required technical checks")
        object.__setattr__(self, "required_checks", normalized_checks)
        if any(
            not is_launchplane_projected_check(observation.name)
            for observation in self.advisory_observations
        ):
            raise ValueError("Advisory observations must use reserved Launchplane check names")
        object.__setattr__(
            self,
            "advisory_observations",
            tuple(
                sorted(
                    self.advisory_observations,
                    key=lambda item: (item.name.casefold(), item.app_id or 0, item.state),
                )
            ),
        )
        if self.state not in {"ready", "blocked_checks", "unknown"}:
            raise ValueError("Technical-check readiness facet uses an invalid canonical state")
        object.__setattr__(self, "reason_codes", _normalize_reasons(self.reason_codes))
        return self


class MergeReadinessEngineeringReviewFacet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: str
    decision_id: str = ""
    decision_binding_sha256: str = ""
    head_sha: str = ""
    tree_sha: str = ""
    evidence: tuple[MergeReadinessEngineeringEvidenceReference, ...] = ()
    state: MergeReadinessState
    reason_codes: tuple[MergeReadinessReasonCode, ...]

    @model_validator(mode="after")
    def _validate_facet(self) -> "MergeReadinessEngineeringReviewFacet":
        object.__setattr__(self, "status", _required_token(self.status, "status"))
        object.__setattr__(self, "decision_id", self.decision_id.strip())
        digest = self.decision_binding_sha256.strip().lower()
        if digest:
            digest = _normalize_sha256(digest, "decision_binding_sha256")
        object.__setattr__(self, "decision_binding_sha256", digest)
        for field_name in ("head_sha", "tree_sha"):
            object.__setattr__(
                self,
                field_name,
                _normalize_optional_git_sha(str(getattr(self, field_name)), field_name),
            )
        object.__setattr__(
            self,
            "evidence",
            tuple(sorted(self.evidence, key=lambda item: item.run_id)),
        )
        if self.state not in {"ready", "blocked_engineering_review", "unknown"}:
            raise ValueError("Engineering-review readiness facet uses an invalid canonical state")
        object.__setattr__(self, "reason_codes", _normalize_reasons(self.reason_codes))
        return self


class MergeReadinessPolicyFacet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fingerprints: MergeReadinessPolicyFingerprints
    state: MergeReadinessState
    reason_codes: tuple[MergeReadinessReasonCode, ...]

    @model_validator(mode="after")
    def _validate_facet(self) -> "MergeReadinessPolicyFacet":
        if self.state not in {"ready", "blocked_policy", "unknown"}:
            raise ValueError("Policy readiness facet uses an invalid canonical state")
        object.__setattr__(self, "reason_codes", _normalize_reasons(self.reason_codes))
        return self


class MergeReadinessCandidateFacet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence: MergeReadinessCandidateEvidence
    state: MergeReadinessState
    reason_codes: tuple[MergeReadinessReasonCode, ...]

    @model_validator(mode="after")
    def _validate_facet(self) -> "MergeReadinessCandidateFacet":
        if self.state not in {"ready", "blocked_candidate_identity", "unknown"}:
            raise ValueError("Candidate readiness facet uses an invalid canonical state")
        object.__setattr__(self, "reason_codes", _normalize_reasons(self.reason_codes))
        return self


class MergeReadinessFenceFacet(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence: MergeReadinessFenceEvidence
    state: MergeReadinessState
    reason_codes: tuple[MergeReadinessReasonCode, ...]

    @model_validator(mode="after")
    def _validate_facet(self) -> "MergeReadinessFenceFacet":
        if self.state not in {"ready", "blocked_candidate_identity", "unknown"}:
            raise ValueError("Fence readiness facet uses an invalid canonical state")
        object.__setattr__(self, "reason_codes", _normalize_reasons(self.reason_codes))
        return self


class MergeReadinessResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["ephemeral"] = "ephemeral"
    authoritative: Literal[False] = False
    authorizes: tuple[str, ...] = ()
    target: MergeReadinessTarget
    state: MergeReadinessState
    reason_codes: tuple[MergeReadinessReasonCode, ...]
    owner_facets: tuple[MergeReadinessOwnerFacet, ...]
    technical_checks: MergeReadinessTechnicalChecksFacet
    engineering_review: MergeReadinessEngineeringReviewFacet
    policy: MergeReadinessPolicyFacet
    candidate: MergeReadinessCandidateFacet
    fence: MergeReadinessFenceFacet
    evaluated_at: str
    readiness_digest: str = ""

    @model_validator(mode="after")
    def _validate_result(self) -> "MergeReadinessResult":
        if self.schema_version != 1:
            raise ValueError("Unsupported merge readiness result schema version.")
        if self.authorizes:
            raise ValueError("Ephemeral merge readiness never authorizes an effect")
        if not self.owner_facets:
            raise ValueError("Merge readiness requires at least one Owner product facet")
        sorted_owner_facets = tuple(
            sorted(
                self.owner_facets,
                key=lambda item: (item.product, item.system, item.action, item.environment),
            )
        )
        owner_keys = tuple(
            (item.product, item.system, item.action, item.environment)
            for item in sorted_owner_facets
        )
        if len(owner_keys) != len(set(owner_keys)):
            raise ValueError("Merge readiness Owner product facets must be unique")
        object.__setattr__(self, "owner_facets", sorted_owner_facets)
        normalized_reasons = _normalize_reasons(self.reason_codes)
        expected_reasons = tuple(
            sorted(
                {
                    reason
                    for facet_reasons in (
                        *(facet.reason_codes for facet in sorted_owner_facets),
                        self.technical_checks.reason_codes,
                        self.engineering_review.reason_codes,
                        self.policy.reason_codes,
                        self.candidate.reason_codes,
                        self.fence.reason_codes,
                    )
                    for reason in facet_reasons
                }
            )
        )
        if normalized_reasons != expected_reasons:
            raise ValueError("Merge readiness reason codes must retain every facet reason")
        object.__setattr__(self, "reason_codes", normalized_reasons)
        facet_states = (
            *(facet.state for facet in sorted_owner_facets),
            self.technical_checks.state,
            self.engineering_review.state,
            self.policy.state,
            self.candidate.state,
            self.fence.state,
        )
        expected_state = merge_readiness_worst_state(facet_states)
        if self.state != expected_state:
            raise ValueError("Merge readiness state does not match deterministic aggregation")
        if self.state == "ready" and any(state != "ready" for state in facet_states):
            raise ValueError("Unknown or blocked merge readiness facets can never be ready")
        object.__setattr__(
            self,
            "evaluated_at",
            _normalize_timestamp(self.evaluated_at, "evaluated_at"),
        )
        expected_digest = merge_readiness_digest(self)
        if self.readiness_digest:
            normalized_digest = _normalize_sha256(self.readiness_digest, "readiness_digest")
            if normalized_digest != expected_digest:
                raise ValueError("Merge readiness digest does not match the deterministic payload")
        object.__setattr__(self, "readiness_digest", expected_digest)
        return self


def merge_readiness_worst_state(
    states: tuple[MergeReadinessState, ...],
) -> MergeReadinessState:
    if not states:
        raise ValueError("Merge readiness aggregation requires at least one state")
    precedence = {state: index for index, state in enumerate(MERGE_READINESS_STATE_PRECEDENCE)}
    return min(states, key=precedence.__getitem__)


def merge_readiness_digest(result: MergeReadinessResult) -> str:
    payload = result.model_dump(
        mode="json",
        exclude={"evaluated_at", "readiness_digest"},
    )
    technical_checks = payload.get("technical_checks")
    if isinstance(technical_checks, dict):
        technical_checks.pop("advisory_observations", None)
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
