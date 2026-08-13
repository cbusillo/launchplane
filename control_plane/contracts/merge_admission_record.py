from __future__ import annotations

from datetime import UTC, datetime
import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_readiness import MergeReadinessResult
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerStateRecord,
)
from control_plane.contracts.merge_train_policy import MergeTrainMergeMethod
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainStructuralCandidateResult,
)


MergeLandingOutcomeStatus = Literal["landed", "rejected", "reconcile_required"]
MergeLandingObservedPullRequestState = Literal["", "open", "closed", "merged"]
MergeLandingOutcomeReason = Literal[
    "provider_and_git_confirmed",
    "provider_rejected",
    "reconciliation_confirmed_no_effect",
    "provider_transport_ambiguous",
    "process_interrupted",
    "lease_lost_after_admission",
    "landing_evidence_incomplete",
    "landing_evidence_contradicted",
]

_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_SHA256_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class MergeAdmissionFenceRejectedError(RuntimeError):
    """Raised when persisted controller authority no longer admits an effect."""


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


def _normalize_timestamp(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name)
    try:
        parsed = datetime.fromisoformat(normalized.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError(f"{field_name} must be an ISO-8601 timestamp") from error
    if parsed.tzinfo is None:
        raise ValueError(f"{field_name} must include a timezone")
    return parsed.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def _canonical_sha256(payload: object) -> str:
    encoded = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class MergeAdmissionRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    admission_id: str = ""
    admission_binding_sha256: str = ""
    attempt_id: str
    attempt_sequence: int = Field(ge=1)
    decision: Literal["admitted"] = "admitted"
    source: str
    repository: str
    base_branch: str
    pull_request_number: int = Field(ge=1)
    queue_position: int = Field(ge=1)
    batch_id: str
    candidate_record_id: str
    landing_plan_record_id: str
    landing_plan_id: str
    merge_method: MergeTrainMergeMethod
    effective_base_sha: str
    effective_base_tree_sha: str
    pull_request_head_sha: str
    pull_request_head_tree_sha: str
    candidate_sha: str
    candidate_tree_sha: str
    expected_effect_sha: str
    candidate_sha256: str
    structural_provenance_sha256: str
    landing_plan_sha256: str
    readiness: MergeReadinessResult
    structural_result: MergeTrainStructuralCandidateResult
    admission_algorithm_version: str
    controller_key: str
    lease_owner: str
    lease_acquired_at: str
    lease_expires_at: str
    created_at: str

    @model_validator(mode="after")
    def _validate_record(self) -> "MergeAdmissionRecord":
        object.__setattr__(self, "repository", _normalize_repository(self.repository))
        for field_name in (
            "attempt_id",
            "source",
            "base_branch",
            "batch_id",
            "candidate_record_id",
            "landing_plan_record_id",
            "landing_plan_id",
            "admission_algorithm_version",
            "controller_key",
            "lease_owner",
        ):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        for field_name in (
            "effective_base_sha",
            "effective_base_tree_sha",
            "pull_request_head_sha",
            "pull_request_head_tree_sha",
            "candidate_sha",
            "candidate_tree_sha",
            "expected_effect_sha",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_git_sha(str(getattr(self, field_name)), field_name),
            )
        for field_name in (
            "candidate_sha256",
            "structural_provenance_sha256",
            "landing_plan_sha256",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_sha256(str(getattr(self, field_name)), field_name),
            )
        for field_name in ("lease_acquired_at", "lease_expires_at", "created_at"):
            object.__setattr__(
                self,
                field_name,
                _normalize_timestamp(str(getattr(self, field_name)), field_name),
            )
        if self.readiness.state != "ready":
            raise ValueError("merge admission requires ready Level 2 evidence")
        target = self.readiness.target
        if (
            target.repository != self.repository
            or target.base_branch != self.base_branch
            or target.pull_request_number != self.pull_request_number
            or target.queue_position != self.queue_position
            or target.base_sha != self.effective_base_sha
            or target.pull_request_head_sha != self.pull_request_head_sha
            or target.pull_request_tree_sha != self.pull_request_head_tree_sha
            or target.expected_effect_sha != self.expected_effect_sha
        ):
            raise ValueError("merge admission identity does not match Level 2 evidence")
        if self.structural_result.status not in {"exact", "recorded_rolling"}:
            raise ValueError("merge admission requires exact structural provenance")
        if (
            self.structural_result.effective_base_sha != self.effective_base_sha
            or self.structural_result.effective_base_tree_sha != self.effective_base_tree_sha
            or self.structural_result.candidate_sha256 != self.candidate_sha256
            or self.structural_result.landing_plan_sha256 != self.landing_plan_sha256
            or self.structural_result.provenance_sha256 != self.structural_provenance_sha256
        ):
            raise ValueError("merge admission identity does not match structural evidence")
        if self.readiness.candidate.evidence.candidate_sha != self.candidate_sha:
            raise ValueError("merge admission candidate SHA does not match Level 2 evidence")
        if self.readiness.fence.evidence.controller_key != self.controller_key:
            raise ValueError("merge admission controller key does not match Level 2 evidence")
        if self.readiness.fence.evidence.observed_lease_owner != self.lease_owner:
            raise ValueError("merge admission lease owner does not match Level 2 evidence")
        if self.readiness.fence.evidence.lease_expires_at != self.lease_expires_at:
            raise ValueError("merge admission lease expiry does not match Level 2 evidence")
        expected_attempt_id = build_merge_effect_attempt_id(
            controller_key=self.controller_key,
            lease_owner=self.lease_owner,
            lease_acquired_at=self.lease_acquired_at,
            landing_plan_id=self.landing_plan_id,
            pull_request_number=self.pull_request_number,
            queue_position=self.queue_position,
            attempt_sequence=self.attempt_sequence,
            expected_effect_sha=self.expected_effect_sha,
        )
        if self.attempt_id != expected_attempt_id:
            raise ValueError("merge admission attempt ID does not match its deterministic identity")
        expected_binding = merge_admission_binding_sha256(self)
        binding = self.admission_binding_sha256.strip().lower()
        if binding and _normalize_sha256(binding, "admission_binding_sha256") != expected_binding:
            raise ValueError("merge admission binding digest does not match payload")
        expected_id = f"merge-admission-{expected_binding[:24]}"
        admission_id = self.admission_id.strip()
        if admission_id and admission_id != expected_id:
            raise ValueError("merge admission id does not match payload")
        object.__setattr__(self, "admission_binding_sha256", expected_binding)
        object.__setattr__(self, "admission_id", expected_id)
        return self


class MergeLandingOutcomeRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    outcome_id: str = ""
    outcome_binding_sha256: str = ""
    admission_id: str
    admission_binding_sha256: str
    attempt_id: str
    observation_sequence: int = Field(ge=1)
    prior_outcome_id: str = ""
    source: str
    repository: str
    base_branch: str
    pull_request_number: int = Field(ge=1)
    status: MergeLandingOutcomeStatus
    reason: MergeLandingOutcomeReason
    provider_effect_attempted: bool
    provider_conclusive_rejection: bool = False
    provider_status_code: int | None = Field(default=None, ge=100, le=599)
    provider_request_id: str = ""
    provider_message: str = ""
    observed_pull_request_state: MergeLandingObservedPullRequestState = ""
    observed_pull_request_head_sha: str = ""
    observed_pull_request_head_tree_sha: str = ""
    observed_base_sha: str = ""
    observed_base_tree_sha: str = ""
    merge_commit_sha: str = ""
    merge_commit_tree_sha: str = ""
    base_contains_merge_commit: bool | None = None
    exact_landing_confirmed: bool = False
    observed_at: str

    @model_validator(mode="after")
    def _validate_record(self) -> "MergeLandingOutcomeRecord":
        object.__setattr__(self, "repository", _normalize_repository(self.repository))
        for field_name in ("admission_id", "attempt_id", "source"):
            object.__setattr__(
                self,
                field_name,
                _required_token(str(getattr(self, field_name)), field_name),
            )
        object.__setattr__(
            self,
            "admission_binding_sha256",
            _normalize_sha256(self.admission_binding_sha256, "admission_binding_sha256"),
        )
        object.__setattr__(self, "base_branch", _required_token(self.base_branch, "base_branch"))
        object.__setattr__(self, "prior_outcome_id", self.prior_outcome_id.strip())
        object.__setattr__(self, "provider_request_id", self.provider_request_id.strip())
        object.__setattr__(self, "provider_message", self.provider_message.strip())
        for field_name in (
            "observed_pull_request_head_sha",
            "observed_pull_request_head_tree_sha",
            "observed_base_sha",
            "observed_base_tree_sha",
            "merge_commit_sha",
            "merge_commit_tree_sha",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_optional_git_sha(str(getattr(self, field_name)), field_name),
            )
        object.__setattr__(
            self, "observed_at", _normalize_timestamp(self.observed_at, "observed_at")
        )
        if self.observation_sequence == 1 and self.prior_outcome_id:
            raise ValueError("first landing outcome cannot reference a prior outcome")
        if self.observation_sequence > 1 and not self.prior_outcome_id:
            raise ValueError("reconciled landing outcome requires a prior outcome")
        if self.status == "landed":
            if self.provider_conclusive_rejection:
                raise ValueError("landed outcome cannot carry provider rejection")
            if not self.exact_landing_confirmed or self.base_contains_merge_commit is not True:
                raise ValueError("landed outcome requires exact provider and Git confirmation")
            if not all(
                (
                    self.observed_pull_request_head_sha,
                    self.observed_pull_request_head_tree_sha,
                    self.observed_base_sha,
                    self.observed_base_tree_sha,
                    self.merge_commit_sha,
                    self.merge_commit_tree_sha,
                )
            ):
                raise ValueError("landed outcome requires complete Git evidence")
            if self.reason != "provider_and_git_confirmed":
                raise ValueError("landed outcome requires provider_and_git_confirmed reason")
        elif self.status == "rejected":
            if self.exact_landing_confirmed or self.merge_commit_sha or self.merge_commit_tree_sha:
                raise ValueError("rejected outcome cannot claim a landing")
            if self.reason == "provider_rejected":
                if not self.provider_effect_attempted or not self.provider_conclusive_rejection:
                    raise ValueError("provider rejection requires conclusive provider refusal")
                if self.provider_status_code is None:
                    raise ValueError("provider rejection requires provider status code")
            elif self.reason == "reconciliation_confirmed_no_effect":
                if not self.provider_effect_attempted:
                    raise ValueError(
                        "no-effect reconciliation requires a preceding provider attempt"
                    )
                if self.provider_conclusive_rejection or self.provider_status_code is not None:
                    raise ValueError("no-effect reconciliation cannot claim provider rejection")
                if self.observed_pull_request_state != "open" or not all(
                    (
                        self.observed_pull_request_head_sha,
                        self.observed_pull_request_head_tree_sha,
                        self.observed_base_sha,
                        self.observed_base_tree_sha,
                    )
                ):
                    raise ValueError(
                        "no-effect reconciliation requires exact open PR and base evidence"
                    )
            else:
                raise ValueError("rejected outcome requires conclusive no-effect evidence")
        else:
            if self.provider_conclusive_rejection:
                raise ValueError("reconcile-required outcome cannot claim conclusive rejection")
            if self.exact_landing_confirmed:
                raise ValueError("reconcile-required outcome cannot claim exact landing")
            if self.reason in {"provider_and_git_confirmed", "provider_rejected"}:
                raise ValueError("reconcile-required outcome requires an ambiguity reason")
        expected_binding = merge_landing_outcome_binding_sha256(self)
        binding = self.outcome_binding_sha256.strip().lower()
        if binding and _normalize_sha256(binding, "outcome_binding_sha256") != expected_binding:
            raise ValueError("landing outcome binding digest does not match payload")
        expected_id = f"merge-landing-outcome-{expected_binding[:24]}"
        outcome_id = self.outcome_id.strip()
        if outcome_id and outcome_id != expected_id:
            raise ValueError("landing outcome id does not match payload")
        object.__setattr__(self, "outcome_binding_sha256", expected_binding)
        object.__setattr__(self, "outcome_id", expected_id)
        return self


def build_merge_effect_attempt_id(
    *,
    controller_key: str,
    lease_owner: str,
    lease_acquired_at: str,
    landing_plan_id: str,
    pull_request_number: int,
    queue_position: int,
    attempt_sequence: int,
    expected_effect_sha: str,
) -> str:
    if pull_request_number < 1 or queue_position < 1 or attempt_sequence < 1:
        raise ValueError("merge effect attempt identity requires positive numeric fields")
    payload = {
        "controller_key": _required_token(controller_key, "controller_key"),
        "lease_owner": _required_token(lease_owner, "lease_owner"),
        "lease_acquired_at": _normalize_timestamp(lease_acquired_at, "lease_acquired_at"),
        "landing_plan_id": _required_token(landing_plan_id, "landing_plan_id"),
        "pull_request_number": pull_request_number,
        "queue_position": queue_position,
        "attempt_sequence": attempt_sequence,
        "expected_effect_sha": _normalize_git_sha(expected_effect_sha, "expected_effect_sha"),
    }
    return f"merge-effect-attempt-{_canonical_sha256(payload)[:24]}"


def merge_admission_binding_sha256(record: MergeAdmissionRecord) -> str:
    payload = record.model_dump(
        mode="json",
        exclude={"admission_id", "admission_binding_sha256"},
    )
    readiness = payload.get("readiness")
    if isinstance(readiness, dict) and readiness.get("engineering_review_authority") == "required":
        readiness.pop("engineering_review_authority")
    return _canonical_sha256(payload)


def merge_landing_outcome_binding_sha256(record: MergeLandingOutcomeRecord) -> str:
    return _canonical_sha256(
        record.model_dump(
            mode="json",
            exclude={"outcome_id", "outcome_binding_sha256"},
        )
    )


def validate_merge_landing_outcome_for_admission(
    *,
    admission: MergeAdmissionRecord,
    outcome: MergeLandingOutcomeRecord,
) -> None:
    if (
        outcome.admission_id != admission.admission_id
        or outcome.admission_binding_sha256 != admission.admission_binding_sha256
        or outcome.attempt_id != admission.attempt_id
        or outcome.repository != admission.repository
        or outcome.base_branch != admission.base_branch
        or outcome.pull_request_number != admission.pull_request_number
    ):
        raise ValueError("landing outcome does not match its merge admission")
    if outcome.status == "landed":
        if (
            outcome.observed_pull_request_head_sha != admission.pull_request_head_sha
            or outcome.observed_pull_request_head_tree_sha != admission.pull_request_head_tree_sha
        ):
            raise ValueError("landed outcome head identity does not match admission")
    if outcome.reason == "reconciliation_confirmed_no_effect":
        if (
            outcome.observed_pull_request_state != "open"
            or outcome.observed_pull_request_head_sha != admission.pull_request_head_sha
            or outcome.observed_pull_request_head_tree_sha != admission.pull_request_head_tree_sha
            or outcome.observed_base_sha != admission.effective_base_sha
            or outcome.observed_base_tree_sha != admission.effective_base_tree_sha
        ):
            raise ValueError("no-effect reconciliation evidence does not match admission")


def validate_merge_landing_outcome_successor(
    *,
    prior: MergeLandingOutcomeRecord,
    successor: MergeLandingOutcomeRecord,
) -> None:
    if prior.status != "reconcile_required":
        raise ValueError("terminal landing outcomes cannot be superseded")
    if (
        successor.admission_id != prior.admission_id
        or successor.admission_binding_sha256 != prior.admission_binding_sha256
        or successor.attempt_id != prior.attempt_id
        or successor.observation_sequence != prior.observation_sequence + 1
        or successor.prior_outcome_id != prior.outcome_id
    ):
        raise ValueError("landing outcome reconciliation chain is invalid")


def validate_merge_admission_controller_fence(
    *,
    admission: MergeAdmissionRecord,
    controller_state: MergeTrainControllerStateRecord,
    admitted_at: str,
) -> None:
    normalized_admitted_at = _normalize_timestamp(admitted_at, "admitted_at")
    if normalized_admitted_at != admission.created_at:
        raise MergeAdmissionFenceRejectedError(
            "Persisted merge admission time does not match the actual admission time."
        )
    if (
        controller_state.status != "running"
        or controller_state.controller_key != admission.controller_key
        or controller_state.repository != admission.repository
        or controller_state.base_branch != admission.base_branch
        or controller_state.lease_owner != admission.lease_owner
        or _normalize_timestamp(controller_state.lease_acquired_at, "lease_acquired_at")
        != admission.lease_acquired_at
        or _normalize_timestamp(controller_state.lease_expires_at, "lease_expires_at")
        != admission.lease_expires_at
    ):
        raise MergeAdmissionFenceRejectedError(
            "Persisted merge controller lease changed before admission creation."
        )
    admitted_time = datetime.fromisoformat(normalized_admitted_at.replace("Z", "+00:00"))
    expires_time = datetime.fromisoformat(
        _normalize_timestamp(controller_state.lease_expires_at, "lease_expires_at").replace(
            "Z", "+00:00"
        )
    )
    if admitted_time >= expires_time:
        raise MergeAdmissionFenceRejectedError(
            "Persisted merge controller lease expired before admission creation."
        )
    merge_train_policy = admission.readiness.policy.fingerprints.merge_train
    if (
        merge_train_policy.expected_sha256 != controller_state.policy_sha256
        or merge_train_policy.current_sha256 != controller_state.policy_sha256
    ):
        raise MergeAdmissionFenceRejectedError(
            "Persisted merge controller policy changed before admission creation."
        )
    expected_effect_sha = str(controller_state.step_payload.get("expected_effect_sha") or "")
    landing_plan_id = str(controller_state.step_payload.get("landing_plan_id") or "")
    if (
        controller_state.active_action not in {"land_batch", "batch_landing_run_once"}
        or controller_state.active_pull_request_number != admission.pull_request_number
        or expected_effect_sha != admission.expected_effect_sha
        or landing_plan_id != admission.landing_plan_id
    ):
        raise MergeAdmissionFenceRejectedError(
            "Persisted merge controller effect fence changed before admission creation."
        )
