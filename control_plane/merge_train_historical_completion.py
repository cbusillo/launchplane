"""Read-only applicability evidence for an explicitly selected legacy fence."""

from dataclasses import dataclass
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.merge_admission_record import MergeAdmissionRecord
from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_train_controller_state import MergeTrainControllerStateRecord
from control_plane.contracts.merge_train_historical_completion import (
    MergeTrainHistoricalCompletionProviderEvidence,
    MergeTrainHistoricalCompletionSelector,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicyRecord
from control_plane.contracts.merge_train_stack_collapse import MergeTrainStackCollapsePlanRecord
from control_plane.contracts.merge_train_structural_provenance import MergeTrainStructuralProvenance
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubError,
    MergeTrainHistoricalCompletionProofError,
)
from control_plane.workflows.merge_train_controller import (
    latest_merge_train_batch_candidate_progress_record,
)


HistoricalCompletionStatus = Literal["eligible", "unsupported", "indeterminate"]
HistoricalCompletionReason = Literal[
    "exact_historical_completion",
    "not_assessed",
    "controller_unavailable",
    "controller_busy",
    "controller_not_reconciling",
    "selector_mismatch",
    "policy_unavailable",
    "policy_changed",
    "landing_plan_unavailable",
    "landing_plan_ambiguous",
    "landing_plan_binding_changed",
    "stored_evidence_incomplete",
    "entry_limit_exceeded",
    "candidate_unavailable",
    "candidate_history_limit_exceeded",
    "candidate_ambiguous",
    "candidate_binding_changed",
    "stack_batch_unsupported",
    "ordinary_target_unsupported",
    "admission_present",
    "store_unavailable",
    "store_state_changed",
    "provider_unavailable",
    "provider_invalid_response",
    "provider_binding_mismatch",
    "pull_request_not_merged",
    "no_op_unsupported",
    "base_not_contains_merge",
    "base_changed",
]


class HistoricalCompletionEntryAssessment(BaseModel):
    model_config = ConfigDict(extra="forbid")

    position: int
    pull_request_number: int
    status: HistoricalCompletionStatus
    reason_code: HistoricalCompletionReason


class MergeTrainHistoricalCompletionPreflight(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    repository: str
    base_branch: str
    generated_at: str
    selector: MergeTrainHistoricalCompletionSelector
    status: HistoricalCompletionStatus
    reason_code: HistoricalCompletionReason
    evidence_eligible: bool = False
    disposition_supported: bool = False
    mutation_enabled: bool = False
    provider_effect_attempted: Literal[False] = False
    admission_created: Literal[False] = False
    fence_released: Literal[False] = False
    automatic_retry_allowed: Literal[False] = False
    landing_plan_sha256: str = ""
    entries: tuple[HistoricalCompletionEntryAssessment, ...] = ()
    provider_evidence: MergeTrainHistoricalCompletionProviderEvidence | None = None


class HistoricalCompletionPreflightResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    base_branch: str
    mode: Literal["dry-run"] = "dry-run"
    controller_action: Literal["historical_completion_preflight"] = (
        "historical_completion_preflight"
    )
    historical_completion_preflight: MergeTrainHistoricalCompletionPreflight


class HistoricalCompletionPreflightResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: dict[str, str] = Field(default_factory=dict)
    result: HistoricalCompletionPreflightResult


class HistoricalCompletionSnapshotStore(Protocol):
    def list_merge_train_controller_state_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainControllerStateRecord, ...]: ...

    def list_merge_train_policy_records(
        self,
        *,
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainPolicyRecord, ...]: ...

    def list_merge_train_batch_landing_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        record_id: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchLandingPlanRecord, ...]: ...

    def list_merge_train_batch_candidate_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        batch_id: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchCandidateRecord, ...]: ...

    def list_merge_train_stack_collapse_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        root_pull_request_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[MergeTrainStackCollapsePlanRecord, ...]: ...

    def list_merge_admission_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        pull_request_number: int | None = None,
        landing_plan_id: str = "",
        limit: int | None = None,
    ) -> tuple[MergeAdmissionRecord, ...]: ...

    def has_ordinary_merge_train_target_fence(
        self,
        *,
        repository: str,
        base_branch: str,
    ) -> bool: ...


@dataclass(frozen=True)
class HistoricalCompletionSnapshot:
    controller: MergeTrainControllerStateRecord
    policy: MergeTrainPolicyRecord
    landing: MergeTrainBatchLandingPlanRecord
    candidate: MergeTrainBatchCandidateRecord
    source_payload_sha256: str = ""


class HistoricalCompletionAssessmentFailure(Exception):
    def __init__(
        self,
        status: HistoricalCompletionStatus,
        reason: HistoricalCompletionReason,
        pull_request_number: int | None = None,
    ) -> None:
        super().__init__(reason)
        self.status = status
        self.reason = reason
        self.pull_request_number = pull_request_number


def assess_merge_train_historical_completion(
    *,
    store: object,
    repository: str,
    base_branch: str,
    selector: MergeTrainHistoricalCompletionSelector,
    generated_at: str,
    github_client: GitHubMergeTrainClient,
    validated_snapshot: HistoricalCompletionSnapshot | None = None,
) -> MergeTrainHistoricalCompletionPreflight:
    """Observe an exact legacy plan without acquiring a lease or invoking admission."""
    normalized_repository = repository.strip().lower()
    normalized_base_branch = base_branch.strip()
    required = (
        "list_merge_train_controller_state_records",
        "list_merge_train_policy_records",
        "list_merge_train_batch_landing_plan_records",
        "list_merge_train_batch_candidate_records",
        "list_merge_train_stack_collapse_plan_records",
        "list_merge_admission_records",
        "has_ordinary_merge_train_target_fence",
    )
    evidence: MergeTrainHistoricalCompletionProviderEvidence | None = None
    snapshot: HistoricalCompletionSnapshot | None = validated_snapshot
    failure: HistoricalCompletionAssessmentFailure | None = None
    try:
        reader = cast(HistoricalCompletionSnapshotStore, store)
        if snapshot is None:
            if not all(callable(getattr(store, name, None)) for name in required):
                raise HistoricalCompletionAssessmentFailure("indeterminate", "store_unavailable")
            snapshot = read_historical_completion_snapshot(
                store=reader,
                repository=normalized_repository,
                base_branch=normalized_base_branch,
                selector=selector,
            )
        evidence = github_client.observe_historical_batch_completion(
            landing_plan=snapshot.landing.landing_plan,
            observed_at=generated_at,
        )
        if not isinstance(evidence, MergeTrainHistoricalCompletionProviderEvidence):
            raise HistoricalCompletionAssessmentFailure(
                "indeterminate", "provider_invalid_response"
            )
        if validated_snapshot is None:
            refreshed = read_historical_completion_snapshot(
                store=reader,
                repository=normalized_repository,
                base_branch=normalized_base_branch,
                selector=selector,
            )
            if refreshed != snapshot:
                raise HistoricalCompletionAssessmentFailure("indeterminate", "store_state_changed")
    except HistoricalCompletionAssessmentFailure as error:
        failure = error
    except MergeTrainHistoricalCompletionProofError as error:
        reasons: dict[str, HistoricalCompletionReason] = {
            "plan_invalid": "stored_evidence_incomplete",
            "plan_noop": "no_op_unsupported",
            "plan_unmerged": "pull_request_not_merged",
            "plan_bound": "entry_limit_exceeded",
            "provider_binding_mismatch": "provider_binding_mismatch",
            "provider_unavailable": "provider_unavailable",
            "provider_response_malformed": "provider_invalid_response",
            "base_not_contains_merge": "base_not_contains_merge",
            "target_moved": "base_changed",
        }
        failure = HistoricalCompletionAssessmentFailure(
            error.proof_status,
            reasons[error.reason_code],
            error.pull_request_number,
        )
    except MergeTrainGitHubError:
        failure = HistoricalCompletionAssessmentFailure("indeterminate", "provider_unavailable")
    except Exception:  # Provider/store failures never prove absence or authorize recovery.
        failure = HistoricalCompletionAssessmentFailure("indeterminate", "store_unavailable")
    status: HistoricalCompletionStatus = "eligible" if failure is None else failure.status
    reason: HistoricalCompletionReason = (
        "exact_historical_completion" if failure is None else failure.reason
    )
    entry_results = tuple(
        HistoricalCompletionEntryAssessment(
            position=entry.position,
            pull_request_number=entry.pull_request_number,
            status=(
                status
                if failure is None or failure.pull_request_number == entry.pull_request_number
                else "indeterminate"
            ),
            reason_code=(
                reason
                if failure is None or failure.pull_request_number == entry.pull_request_number
                else "not_assessed"
            ),
        )
        for entry in selector.expected_entries
    )
    return MergeTrainHistoricalCompletionPreflight(
        repository=normalized_repository,
        base_branch=normalized_base_branch,
        generated_at=generated_at,
        selector=selector,
        status=status,
        reason_code=reason,
        evidence_eligible=failure is None and evidence is not None,
        landing_plan_sha256=snapshot.landing.landing_plan.landing_plan_sha256 if snapshot else "",
        entries=entry_results,
        provider_evidence=evidence,
    )


def read_historical_completion_snapshot(
    *,
    store: HistoricalCompletionSnapshotStore,
    repository: str,
    base_branch: str,
    selector: MergeTrainHistoricalCompletionSelector,
) -> HistoricalCompletionSnapshot:
    controllers = store.list_merge_train_controller_state_records(
        repository=repository,
        base_branch=base_branch,
        limit=2,
    )
    if len(controllers) != 1:
        raise HistoricalCompletionAssessmentFailure("indeterminate", "controller_unavailable")
    controller = controllers[0]
    if controller.status == "running" or controller.lease_owner or controller.lease_expires_at:
        raise HistoricalCompletionAssessmentFailure("indeterminate", "controller_busy")
    if controller.status != "reconcile_required" or controller.active_action != "land_batch":
        raise HistoricalCompletionAssessmentFailure("unsupported", "controller_not_reconciling")
    if controller.active_phase not in {
        "merge_batch_entries",
        "merge_pull_request",
        "landing_entry_merged",
    }:
        raise HistoricalCompletionAssessmentFailure("unsupported", "controller_not_reconciling")
    if controller.ordinary_job_binding is not None:
        raise HistoricalCompletionAssessmentFailure("unsupported", "ordinary_target_unsupported")
    policies = store.list_merge_train_policy_records(status="active", limit=2)
    if len(policies) != 1:
        raise HistoricalCompletionAssessmentFailure("indeterminate", "policy_unavailable")
    policy = policies[0]
    try:
        repository_policy = policy.policy.find_repository_policy(
            repository=repository,
            base_branch=base_branch,
        )
    except ValueError as error:
        raise HistoricalCompletionAssessmentFailure(
            "indeterminate", "policy_unavailable"
        ) from error
    if (
        controller.repository.strip().lower() != repository.strip().lower()
        or controller.base_branch != base_branch
        or controller.policy_key != repository_policy.policy_key
        or controller.policy_sha256 != policy.policy_sha256
        or policy.policy_sha256 != selector.expected_policy_sha256
    ):
        raise HistoricalCompletionAssessmentFailure("unsupported", "policy_changed")
    payload = controller.step_payload
    if (
        controller.active_record_id != selector.expected_active_record_id
        or payload.get("landing_plan_record_id") != selector.expected_active_record_id
        or payload.get("expected_effect_sha") != selector.expected_effect_sha
        or payload.get("landing_plan_id") != selector.expected_landing_plan_id
    ):
        raise HistoricalCompletionAssessmentFailure("unsupported", "selector_mismatch")
    landings = store.list_merge_train_batch_landing_plan_records(
        repository=repository,
        base_branch=base_branch,
        record_id=selector.expected_active_record_id,
        limit=2,
    )
    if not landings:
        raise HistoricalCompletionAssessmentFailure("indeterminate", "landing_plan_unavailable")
    if len(landings) > 1:
        raise HistoricalCompletionAssessmentFailure("indeterminate", "landing_plan_ambiguous")
    landing = landings[0]
    if landing.record_id != selector.expected_active_record_id or landing.status != "active":
        raise HistoricalCompletionAssessmentFailure("unsupported", "landing_plan_binding_changed")
    plan = landing.landing_plan
    if landing.ordinary_job_binding is not None or store.has_ordinary_merge_train_target_fence(
        repository=repository,
        base_branch=base_branch,
    ):
        raise HistoricalCompletionAssessmentFailure("unsupported", "ordinary_target_unsupported")
    if (
        landing.status != "active"
        or plan.repository != repository.strip().lower()
        or plan.base_branch != base_branch
        or plan.policy_key != controller.policy_key
        or plan.policy_sha256 != controller.policy_sha256
        or plan.plan_id != selector.expected_landing_plan_id
        or plan.candidate_sha != selector.expected_effect_sha
    ):
        raise HistoricalCompletionAssessmentFailure("unsupported", "landing_plan_binding_changed")
    if not 1 <= len(plan.entries) <= 25:
        raise HistoricalCompletionAssessmentFailure("unsupported", "entry_limit_exceeded")
    if tuple(
        (
            entry.position,
            entry.pull_request_number,
            entry.expected_head_sha,
            entry.expected_head_tree_sha,
        )
        for entry in plan.entries
    ) != tuple(
        (
            entry.position,
            entry.pull_request_number,
            entry.expected_head_sha,
            entry.expected_head_tree_sha,
        )
        for entry in selector.expected_entries
    ):
        raise HistoricalCompletionAssessmentFailure("unsupported", "selector_mismatch")
    for entry in plan.entries:
        if entry.status not in {"planned", "merging"}:
            raise HistoricalCompletionAssessmentFailure(
                "unsupported", "stored_evidence_incomplete", entry.pull_request_number
            )
        if not all(
            (
                entry.expected_head_tree_sha,
                entry.expected_base_sha,
                entry.recorded_candidate_parent_sha,
                entry.recorded_candidate_parent_tree_sha,
                entry.recorded_candidate_result_sha,
                entry.recorded_candidate_result_tree_sha,
            )
        ):
            raise HistoricalCompletionAssessmentFailure(
                "indeterminate", "stored_evidence_incomplete", entry.pull_request_number
            )
        if store.list_merge_admission_records(
            repository=repository,
            base_branch=base_branch,
            pull_request_number=entry.pull_request_number,
            landing_plan_id=plan.plan_id,
            limit=1,
        ):
            raise HistoricalCompletionAssessmentFailure(
                "unsupported", "admission_present", entry.pull_request_number
            )
    candidates = store.list_merge_train_batch_candidate_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        batch_id=plan.batch_id,
        limit=101,
    )
    if len(candidates) > 100:
        raise HistoricalCompletionAssessmentFailure(
            "indeterminate", "candidate_history_limit_exceeded"
        )
    if any(
        record.candidate.candidate_sha
        and (
            record.candidate.candidate_sha != plan.candidate_sha
            or record.candidate.candidate_sha256 != plan.candidate_sha256
        )
        for record in candidates
    ):
        raise HistoricalCompletionAssessmentFailure("indeterminate", "candidate_ambiguous")
    compatible = tuple(
        record
        for record in candidates
        if record.candidate.batch_id == plan.batch_id
        and record.candidate.candidate_sha == plan.candidate_sha
        and record.candidate.candidate_sha256 == plan.candidate_sha256
    )
    candidate = latest_merge_train_batch_candidate_progress_record(compatible)
    if candidate is None:
        raise HistoricalCompletionAssessmentFailure("indeterminate", "candidate_unavailable")
    immutable = {
        record.candidate.model_dump_json(
            exclude={
                "status",
                "required_checks_status",
                "created_at",
                "updated_at",
            }
        )
        for record in compatible
    }
    if len(immutable) != 1:
        raise HistoricalCompletionAssessmentFailure("indeterminate", "candidate_ambiguous")
    candidate_plan = candidate.candidate
    provenance = candidate_plan.structural_provenance
    if candidate.ordinary_job_binding is not None or candidate_plan.stack_collapse_root is not None:
        raise HistoricalCompletionAssessmentFailure("unsupported", "stack_batch_unsupported")
    if (
        candidate_plan.repository != plan.repository
        or candidate_plan.base_branch != plan.base_branch
        or candidate_plan.policy_key != plan.policy_key
        or candidate_plan.policy_sha256 != plan.policy_sha256
        or candidate_plan.candidate_tree_sha != plan.candidate_tree_sha
        or provenance is None
        or not provenance.complete
        or provenance.provenance_sha256 != plan.structural_provenance_sha256
        or not _candidate_steps_match_plan(provenance, plan)
        or candidate_plan.base_sha != plan.entries[0].expected_base_sha
        or tuple(
            (entry.position, entry.pull_request_number, entry.head_sha, entry.head_tree_sha)
            for entry in candidate_plan.entries
        )
        != tuple(
            (
                entry.position,
                entry.pull_request_number,
                entry.expected_head_sha,
                entry.expected_head_tree_sha,
            )
            for entry in plan.entries
        )
    ):
        raise HistoricalCompletionAssessmentFailure("unsupported", "candidate_binding_changed")
    for entry in plan.entries:
        stacks = store.list_merge_train_stack_collapse_plan_records(
            repository=repository,
            base_branch=base_branch,
            status="active",
            root_pull_request_number=entry.pull_request_number,
            limit=1,
        )
        if stacks:
            raise HistoricalCompletionAssessmentFailure("unsupported", "stack_batch_unsupported")
    return HistoricalCompletionSnapshot(
        controller=controller, policy=policy, landing=landing, candidate=candidate
    )


def _candidate_steps_match_plan(
    provenance: MergeTrainStructuralProvenance,
    plan: MergeTrainBatchLandingPlan,
) -> bool:
    if len(provenance.steps) != len(plan.entries):
        return False
    return all(
        (
            step.position,
            step.pull_request_number,
            step.parent_sha,
            step.parent_tree_sha,
            step.head_sha,
            step.head_tree_sha,
            step.result_sha,
            step.result_tree_sha,
        )
        == (
            entry.position,
            entry.pull_request_number,
            entry.recorded_candidate_parent_sha,
            entry.recorded_candidate_parent_tree_sha,
            entry.expected_head_sha,
            entry.expected_head_tree_sha,
            entry.recorded_candidate_result_sha,
            entry.recorded_candidate_result_tree_sha,
        )
        for step, entry in zip(provenance.steps, plan.entries, strict=True)
    )
