from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_readiness import MergeReadinessStructuralCandidateStatus


MergeTrainStructuralCandidateStatus = MergeReadinessStructuralCandidateStatus
MergeTrainRollingStepKind = Literal["merge_commit", "no_op_already_contained"]
MergeTrainStructuralImpactStatus = Literal["known", "unknown"]
MergeTrainStructuralReasonCode = Literal[
    "structural_single_entry_exact",
    "structural_batch_entry_exact",
    "structural_rolling_chain_recorded",
    "structural_stack_root_recorded",
    "structural_combined_owner_review_recorded",
    "structural_evidence_unavailable",
    "structural_record_superseded",
    "structural_candidate_incomplete",
    "structural_provenance_missing",
    "structural_landing_plan_missing",
    "structural_landing_plan_superseded",
    "structural_repository_mismatch",
    "structural_base_branch_mismatch",
    "structural_policy_mismatch",
    "structural_candidate_digest_mismatch",
    "structural_landing_plan_digest_mismatch",
    "structural_provenance_digest_mismatch",
    "structural_candidate_sha_mismatch",
    "structural_candidate_tree_mismatch",
    "structural_entry_absent",
    "structural_position_mismatch",
    "structural_head_sha_mismatch",
    "structural_head_tree_mismatch",
    "structural_queue_drift",
    "structural_base_sha_mismatch",
    "structural_base_tree_mismatch",
    "structural_rolling_chain_broken",
    "structural_prior_entry_not_landed",
    "structural_prior_entry_head_mismatch",
    "structural_prior_entry_tree_mismatch",
    "structural_impact_unknown",
    "structural_impact_expanded",
    "structural_same_subject_combined_review_required",
    "structural_combined_owner_review_mismatch",
    "structural_stack_root_unproven",
]


class MergeTrainStructuralSubject(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    product: str
    system: str

    @model_validator(mode="after")
    def _validate_subject(self) -> "MergeTrainStructuralSubject":
        object.__setattr__(self, "product", _required_token(self.product, "product"))
        object.__setattr__(self, "system", _required_token(self.system, "system"))
        return self


class MergeTrainStructuralEntryBinding(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position: int = Field(ge=1)
    pull_request_number: int = Field(ge=1)
    head_sha: str
    head_tree_sha: str
    impact_status: MergeTrainStructuralImpactStatus = "unknown"
    affected_subjects: tuple[MergeTrainStructuralSubject, ...] = ()

    @model_validator(mode="after")
    def _validate_binding(self) -> "MergeTrainStructuralEntryBinding":
        object.__setattr__(self, "head_sha", _required_token(self.head_sha, "head_sha"))
        object.__setattr__(
            self,
            "head_tree_sha",
            _required_token(self.head_tree_sha, "head_tree_sha"),
        )
        subjects = tuple(
            sorted(set(self.affected_subjects), key=lambda item: (item.product, item.system))
        )
        if self.impact_status == "unknown" and subjects:
            raise ValueError("Unknown structural impact cannot carry affected subjects")
        object.__setattr__(self, "affected_subjects", subjects)
        return self


class MergeTrainRollingStep(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position: int = Field(ge=1)
    pull_request_number: int = Field(ge=1)
    parent_sha: str
    parent_tree_sha: str
    head_sha: str
    head_tree_sha: str
    result_sha: str
    result_tree_sha: str
    kind: MergeTrainRollingStepKind

    @model_validator(mode="after")
    def _validate_step(self) -> "MergeTrainRollingStep":
        for field_name in (
            "parent_sha",
            "parent_tree_sha",
            "head_sha",
            "head_tree_sha",
            "result_sha",
            "result_tree_sha",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        if self.kind == "no_op_already_contained":
            if self.result_sha != self.parent_sha or self.result_tree_sha != self.parent_tree_sha:
                raise ValueError("Already-contained rolling steps must preserve parent identity")
        elif self.result_sha == self.parent_sha:
            raise ValueError("Merge-commit rolling steps must advance the candidate SHA")
        return self


class MergeTrainStackCollapseRootProof(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    collapse_record_id: str
    collapse_id: str
    root_pull_request_number: int = Field(ge=1)
    original_root_head_sha: str
    collapsed_root_head_sha: str
    collapsed_root_tree_sha: str = ""

    @model_validator(mode="after")
    def _validate_proof(self) -> "MergeTrainStackCollapseRootProof":
        for field_name in (
            "collapse_record_id",
            "collapse_id",
            "original_root_head_sha",
            "collapsed_root_head_sha",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        object.__setattr__(self, "collapsed_root_tree_sha", self.collapsed_root_tree_sha.strip())
        return self


class MergeTrainStructuralProvenance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    repository: str
    base_branch: str
    base_sha: str
    base_tree_sha: str
    policy_key: str
    policy_sha256: str
    entries: tuple[MergeTrainStructuralEntryBinding, ...]
    steps: tuple[MergeTrainRollingStep, ...]
    candidate_sha: str
    candidate_tree_sha: str
    stack_collapse_root: MergeTrainStackCollapseRootProof | None = None
    provenance_sha256: str = ""
    candidate_sha256: str = ""

    @model_validator(mode="after")
    def _validate_provenance(self) -> "MergeTrainStructuralProvenance":
        object.__setattr__(self, "repository", _normalize_repository(self.repository))
        for field_name in (
            "base_branch",
            "base_sha",
            "base_tree_sha",
            "policy_key",
            "policy_sha256",
            "candidate_sha",
            "candidate_tree_sha",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        if not self.entries or not self.steps:
            raise ValueError("Structural provenance requires entries and rolling steps")
        positions = tuple(entry.position for entry in self.entries)
        if positions != tuple(range(1, len(self.entries) + 1)):
            raise ValueError("Structural provenance entry positions must be contiguous")
        if len(self.steps) > len(self.entries):
            raise ValueError("Structural provenance cannot contain more steps than entries")
        if tuple(step.position for step in self.steps) != tuple(range(1, len(self.steps) + 1)):
            raise ValueError("Structural provenance rolling positions must be contiguous")
        previous_sha = self.base_sha
        previous_tree_sha = self.base_tree_sha
        for entry, step in zip(self.entries, self.steps, strict=False):
            if (step.position, step.pull_request_number, step.head_sha, step.head_tree_sha) != (
                entry.position,
                entry.pull_request_number,
                entry.head_sha,
                entry.head_tree_sha,
            ):
                raise ValueError("Structural provenance step does not match its entry binding")
            if step.parent_sha != previous_sha or step.parent_tree_sha != previous_tree_sha:
                raise ValueError("Structural provenance rolling chain is broken")
            previous_sha = step.result_sha
            previous_tree_sha = step.result_tree_sha
        if previous_sha != self.candidate_sha or previous_tree_sha != self.candidate_tree_sha:
            raise ValueError("Structural provenance terminal identity does not match candidate")
        stack_root = self.stack_collapse_root
        if stack_root is not None:
            root_entry = next(
                (
                    entry
                    for entry in self.entries
                    if entry.pull_request_number == stack_root.root_pull_request_number
                ),
                None,
            )
            if root_entry is None or root_entry.head_sha != stack_root.collapsed_root_head_sha:
                raise ValueError("Structural provenance stack root does not match an entry")
            if (
                stack_root.collapsed_root_tree_sha
                and root_entry.head_tree_sha != stack_root.collapsed_root_tree_sha
            ):
                raise ValueError("Structural provenance stack root tree does not match entry")
        expected_provenance_digest = merge_train_structural_provenance_sha256(self)
        if self.provenance_sha256:
            if self.provenance_sha256.strip().lower() != expected_provenance_digest:
                raise ValueError("Structural provenance digest does not match payload")
        object.__setattr__(self, "provenance_sha256", expected_provenance_digest)
        expected_candidate_digest = merge_train_structural_candidate_sha256(self)
        if self.candidate_sha256:
            if self.candidate_sha256.strip().lower() != expected_candidate_digest:
                raise ValueError("Structural candidate digest does not match payload")
        object.__setattr__(self, "candidate_sha256", expected_candidate_digest)
        return self

    @property
    def complete(self) -> bool:
        return len(self.steps) == len(self.entries)


class MergeTrainStructuralEntryObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position: int = Field(ge=1)
    pull_request_number: int = Field(ge=1)
    head_sha: str
    head_tree_sha: str
    impact_status: MergeTrainStructuralImpactStatus = "unknown"
    affected_subjects: tuple[MergeTrainStructuralSubject, ...] = ()

    @model_validator(mode="after")
    def _validate_observation(self) -> "MergeTrainStructuralEntryObservation":
        binding = MergeTrainStructuralEntryBinding.model_validate(self.model_dump(mode="json"))
        object.__setattr__(self, "head_sha", binding.head_sha)
        object.__setattr__(self, "head_tree_sha", binding.head_tree_sha)
        object.__setattr__(self, "affected_subjects", binding.affected_subjects)
        return self


class MergeTrainCombinedCandidateOwnerReview(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    evidence_id: str
    candidate_sha256: str
    landing_plan_sha256: str
    policy_key: str
    policy_sha256: str
    entries: tuple[MergeTrainStructuralEntryObservation, ...]

    @model_validator(mode="after")
    def _validate_review(self) -> "MergeTrainCombinedCandidateOwnerReview":
        for field_name in (
            "evidence_id",
            "candidate_sha256",
            "landing_plan_sha256",
            "policy_key",
            "policy_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        _validate_observation_positions(self.entries)
        if any(entry.impact_status != "known" for entry in self.entries):
            raise ValueError("Combined-candidate Owner review requires known impact subjects")
        return self


class MergeTrainStructuralEvaluationInput(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    repository: str
    base_branch: str
    target_pull_request_number: int = Field(ge=1)
    target_queue_position: int = Field(ge=1)
    observed_base_sha: str
    observed_base_tree_sha: str
    policy_key: str
    policy_sha256: str
    active_candidate_sha256: str
    active_landing_plan_sha256: str
    entries: tuple[MergeTrainStructuralEntryObservation, ...]
    combined_owner_review: MergeTrainCombinedCandidateOwnerReview | None = None

    @model_validator(mode="after")
    def _validate_input(self) -> "MergeTrainStructuralEvaluationInput":
        object.__setattr__(self, "repository", _normalize_repository(self.repository))
        for field_name in (
            "base_branch",
            "observed_base_sha",
            "observed_base_tree_sha",
            "policy_key",
            "policy_sha256",
            "active_candidate_sha256",
            "active_landing_plan_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        _validate_observation_positions(self.entries)
        return self


class MergeTrainStructuralCandidateResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: MergeTrainStructuralCandidateStatus
    reason_codes: tuple[MergeTrainStructuralReasonCode, ...]
    effective_base_sha: str = ""
    effective_base_tree_sha: str = ""
    candidate_sha256: str = ""
    landing_plan_sha256: str = ""
    provenance_sha256: str = ""

    @model_validator(mode="after")
    def _validate_result(self) -> "MergeTrainStructuralCandidateResult":
        if not self.reason_codes:
            raise ValueError("Structural candidate result requires reason codes")
        object.__setattr__(self, "reason_codes", tuple(sorted(set(self.reason_codes))))
        for field_name in (
            "effective_base_sha",
            "effective_base_tree_sha",
            "candidate_sha256",
            "landing_plan_sha256",
            "provenance_sha256",
        ):
            object.__setattr__(self, field_name, str(getattr(self, field_name)).strip())
        return self


def merge_train_structural_provenance_sha256(
    provenance: MergeTrainStructuralProvenance,
) -> str:
    return _canonical_sha256(
        provenance.model_dump(
            mode="json",
            exclude={"provenance_sha256", "candidate_sha256"},
        )
    )


def merge_train_structural_candidate_sha256(
    provenance: MergeTrainStructuralProvenance,
) -> str:
    return _canonical_sha256(
        {
            "schema_version": 1,
            "repository": provenance.repository,
            "base_branch": provenance.base_branch,
            "base_sha": provenance.base_sha,
            "base_tree_sha": provenance.base_tree_sha,
            "policy_key": provenance.policy_key,
            "policy_sha256": provenance.policy_sha256,
            "entries": [entry.model_dump(mode="json") for entry in provenance.entries],
            "candidate_sha": provenance.candidate_sha,
            "candidate_tree_sha": provenance.candidate_tree_sha,
            "stack_collapse_root": (
                provenance.stack_collapse_root.model_dump(mode="json")
                if provenance.stack_collapse_root is not None
                else None
            ),
            "provenance_sha256": merge_train_structural_provenance_sha256(provenance),
        }
    )


def _validate_observation_positions(
    entries: tuple[MergeTrainStructuralEntryObservation, ...],
) -> None:
    if not entries:
        raise ValueError("Structural evaluation requires queue entries")
    positions = tuple(entry.position for entry in entries)
    if positions != tuple(range(1, len(entries) + 1)):
        raise ValueError("Structural evaluation queue positions must be contiguous")
    pull_request_numbers = tuple(entry.pull_request_number for entry in entries)
    if len(pull_request_numbers) != len(set(pull_request_numbers)):
        raise ValueError("Structural evaluation queue pull requests must be unique")


def _normalize_repository(value: str) -> str:
    normalized = _required_token(value, "repository").lower()
    if normalized.count("/") != 1 or not all(normalized.split("/", 1)):
        raise ValueError("repository must use owner/name")
    return normalized


def _required_token(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty")
    return normalized


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()
