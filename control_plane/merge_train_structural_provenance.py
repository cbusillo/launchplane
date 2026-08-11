from __future__ import annotations

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
)
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainCombinedCandidateOwnerReview,
    MergeTrainStructuralCandidateResult,
    MergeTrainStructuralEntryObservation,
    MergeTrainStructuralEvaluationInput,
    MergeTrainStructuralReasonCode,
    MergeTrainStructuralSubject,
)
from control_plane.merge_train_stack_collapse import stack_collapse_expected_root_head_sha


def evaluate_merge_train_structural_candidate(
    *,
    evaluation: MergeTrainStructuralEvaluationInput,
    candidate_record: MergeTrainBatchCandidateRecord | None,
    landing_plan_record: MergeTrainBatchLandingPlanRecord | None,
    stack_collapse_record: MergeTrainStackCollapsePlanRecord | None = None,
) -> MergeTrainStructuralCandidateResult:
    if candidate_record is None:
        return _result("unknown", "structural_evidence_unavailable")
    if candidate_record.status == "superseded":
        return _result("unknown", "structural_record_superseded")
    candidate = candidate_record.candidate
    provenance = candidate.structural_provenance
    if provenance is None:
        return _result("unknown", "structural_provenance_missing")
    if candidate.status != "passed" or not provenance.complete:
        return _result(
            "unknown",
            "structural_candidate_incomplete",
            candidate_sha256=candidate.candidate_sha256,
            provenance_sha256=provenance.provenance_sha256,
        )
    if landing_plan_record is None:
        return _result(
            "unknown",
            "structural_landing_plan_missing",
            candidate_sha256=candidate.candidate_sha256,
            provenance_sha256=provenance.provenance_sha256,
        )
    if landing_plan_record.status == "superseded":
        return _result(
            "unknown",
            "structural_landing_plan_superseded",
            candidate_sha256=candidate.candidate_sha256,
            provenance_sha256=provenance.provenance_sha256,
        )
    landing_plan = landing_plan_record.landing_plan
    common_mismatch = _common_identity_mismatch(
        evaluation=evaluation,
        candidate_record=candidate_record,
        landing_plan_record=landing_plan_record,
    )
    if common_mismatch is not None:
        return _result(
            "mismatch",
            common_mismatch,
            candidate_sha256=candidate.candidate_sha256,
            landing_plan_sha256=landing_plan.landing_plan_sha256,
            provenance_sha256=provenance.provenance_sha256,
        )
    queue_mismatch = _queue_identity_mismatch(
        observed=evaluation.entries,
        candidate_record=candidate_record,
    )
    if queue_mismatch is not None:
        return _result(
            "mismatch",
            queue_mismatch,
            candidate_sha256=candidate.candidate_sha256,
            landing_plan_sha256=landing_plan.landing_plan_sha256,
            provenance_sha256=provenance.provenance_sha256,
        )
    target_observation = next(
        (
            entry
            for entry in evaluation.entries
            if entry.position == evaluation.target_queue_position
        ),
        None,
    )
    if target_observation is None:
        return _bound_result(
            evaluation, candidate_record, landing_plan_record, "structural_entry_absent"
        )
    if target_observation.pull_request_number != evaluation.target_pull_request_number:
        return _bound_result(
            evaluation,
            candidate_record,
            landing_plan_record,
            "structural_position_mismatch",
        )
    impact_result = _impact_composition_result(
        evaluation=evaluation,
        candidate_record=candidate_record,
        landing_plan_record=landing_plan_record,
    )
    if impact_result is not None:
        return impact_result
    stack_reason = _stack_root_reason(
        evaluation=evaluation,
        candidate_record=candidate_record,
        stack_collapse_record=stack_collapse_record,
    )
    if stack_reason == "structural_stack_root_unproven":
        return _bound_result(
            evaluation,
            candidate_record,
            landing_plan_record,
            stack_reason,
        )
    if evaluation.target_queue_position == 1:
        if evaluation.observed_base_sha != candidate.base_sha:
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_base_sha_mismatch",
            )
        if evaluation.observed_base_tree_sha != provenance.base_tree_sha:
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_base_tree_mismatch",
            )
        if stack_reason == "structural_stack_root_recorded":
            return _success_result(
                status="recorded_rolling",
                reason_codes=_success_reasons(
                    evaluation,
                    "structural_stack_root_recorded",
                ),
                evaluation=evaluation,
                candidate_record=candidate_record,
                landing_plan_record=landing_plan_record,
            )
        return _success_result(
            status="exact",
            reason_codes=_success_reasons(
                evaluation,
                (
                    "structural_single_entry_exact"
                    if len(candidate.entries) == 1
                    else "structural_batch_entry_exact"
                ),
            ),
            evaluation=evaluation,
            candidate_record=candidate_record,
            landing_plan_record=landing_plan_record,
        )
    rolling_reason = _rolling_base_mismatch(
        evaluation=evaluation,
        candidate_record=candidate_record,
        landing_plan_record=landing_plan_record,
    )
    if rolling_reason is not None:
        return _bound_result(
            evaluation,
            candidate_record,
            landing_plan_record,
            rolling_reason,
        )
    return _success_result(
        status="recorded_rolling",
        reason_codes=_success_reasons(evaluation, "structural_rolling_chain_recorded"),
        evaluation=evaluation,
        candidate_record=candidate_record,
        landing_plan_record=landing_plan_record,
    )


def _common_identity_mismatch(
    *,
    evaluation: MergeTrainStructuralEvaluationInput,
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_plan_record: MergeTrainBatchLandingPlanRecord,
) -> MergeTrainStructuralReasonCode | None:
    candidate = candidate_record.candidate
    provenance = candidate.structural_provenance
    if provenance is None:
        return "structural_provenance_missing"
    landing_plan = landing_plan_record.landing_plan
    if (
        candidate.repository != evaluation.repository
        or landing_plan.repository != evaluation.repository
    ):
        return "structural_repository_mismatch"
    if (
        candidate.base_branch != evaluation.base_branch
        or landing_plan.base_branch != evaluation.base_branch
    ):
        return "structural_base_branch_mismatch"
    if (
        candidate.policy_key != evaluation.policy_key
        or candidate.policy_sha256 != evaluation.policy_sha256
        or landing_plan.policy_key != evaluation.policy_key
        or landing_plan.policy_sha256 != evaluation.policy_sha256
    ):
        return "structural_policy_mismatch"
    if (
        not candidate.candidate_sha256
        or candidate.candidate_sha256 != evaluation.active_candidate_sha256
        or landing_plan.candidate_sha256 != candidate.candidate_sha256
    ):
        return "structural_candidate_digest_mismatch"
    if landing_plan.landing_plan_sha256 != evaluation.active_landing_plan_sha256:
        return "structural_landing_plan_digest_mismatch"
    if landing_plan.structural_provenance_sha256 != provenance.provenance_sha256:
        return "structural_provenance_digest_mismatch"
    if landing_plan.candidate_sha != candidate.candidate_sha:
        return "structural_candidate_sha_mismatch"
    if landing_plan.candidate_tree_sha != candidate.candidate_tree_sha:
        return "structural_candidate_tree_mismatch"
    if (
        landing_plan.batch_id != candidate.batch_id
        or landing_plan.candidate_ref != candidate.candidate_ref
    ):
        return "structural_candidate_digest_mismatch"
    return None


def _queue_identity_mismatch(
    *,
    observed: tuple[MergeTrainStructuralEntryObservation, ...],
    candidate_record: MergeTrainBatchCandidateRecord,
) -> MergeTrainStructuralReasonCode | None:
    candidate = candidate_record.candidate
    if len(observed) != len(candidate.entries):
        return "structural_queue_drift"
    for current, recorded in zip(observed, candidate.entries, strict=True):
        if (
            current.position != recorded.position
            or current.pull_request_number != recorded.pull_request_number
        ):
            return "structural_queue_drift"
        if current.head_sha != recorded.head_sha:
            return "structural_head_sha_mismatch"
        if current.head_tree_sha != recorded.head_tree_sha:
            return "structural_head_tree_mismatch"
    return None


def _impact_composition_result(
    *,
    evaluation: MergeTrainStructuralEvaluationInput,
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_plan_record: MergeTrainBatchLandingPlanRecord,
) -> MergeTrainStructuralCandidateResult | None:
    candidate = candidate_record.candidate
    if any(entry.impact_status != "known" for entry in candidate.entries) or any(
        entry.impact_status != "known" for entry in evaluation.entries
    ):
        return _result(
            "unknown",
            "structural_impact_unknown",
            candidate_sha256=candidate.candidate_sha256,
            landing_plan_sha256=landing_plan_record.landing_plan.landing_plan_sha256,
            provenance_sha256=candidate.structural_provenance.provenance_sha256
            if candidate.structural_provenance is not None
            else "",
        )
    impact_changed = any(
        set(current.affected_subjects) != set(recorded.affected_subjects)
        for current, recorded in zip(evaluation.entries, candidate.entries, strict=True)
    )
    subject_counts: dict[MergeTrainStructuralSubject, int] = {}
    for entry in evaluation.entries:
        for subject in entry.affected_subjects:
            subject_counts[subject] = subject_counts.get(subject, 0) + 1
    same_subject = any(count > 1 for count in subject_counts.values())
    if not impact_changed and not same_subject:
        return None
    review = evaluation.combined_owner_review
    if review is None:
        return _bound_result(
            evaluation,
            candidate_record,
            landing_plan_record,
            (
                "structural_impact_expanded"
                if impact_changed
                else "structural_same_subject_combined_review_required"
            ),
        )
    if not _combined_review_matches(review=review, evaluation=evaluation):
        return _bound_result(
            evaluation,
            candidate_record,
            landing_plan_record,
            "structural_combined_owner_review_mismatch",
        )
    return None


def _combined_review_matches(
    *,
    review: MergeTrainCombinedCandidateOwnerReview,
    evaluation: MergeTrainStructuralEvaluationInput,
) -> bool:
    return (
        review.candidate_sha256 == evaluation.active_candidate_sha256
        and review.landing_plan_sha256 == evaluation.active_landing_plan_sha256
        and review.policy_key == evaluation.policy_key
        and review.policy_sha256 == evaluation.policy_sha256
        and review.entries == evaluation.entries
    )


def _stack_root_reason(
    *,
    evaluation: MergeTrainStructuralEvaluationInput,
    candidate_record: MergeTrainBatchCandidateRecord,
    stack_collapse_record: MergeTrainStackCollapsePlanRecord | None,
) -> MergeTrainStructuralReasonCode | None:
    candidate = candidate_record.candidate
    provenance = candidate.structural_provenance
    proof = provenance.stack_collapse_root if provenance is not None else None
    if proof is None:
        return None
    if stack_collapse_record is None or stack_collapse_record.status != "active":
        return "structural_stack_root_unproven"
    plan = stack_collapse_record.plan
    if (
        stack_collapse_record.record_id != proof.collapse_record_id
        or plan.collapse_id != proof.collapse_id
        or plan.root_pull_request_number != proof.root_pull_request_number
        or plan.root_initial_head_sha != proof.original_root_head_sha
        or stack_collapse_expected_root_head_sha(plan) != proof.collapsed_root_head_sha
        or plan.repository != evaluation.repository
        or plan.base_branch != evaluation.base_branch
        or plan.policy_key != evaluation.policy_key
        or plan.policy_sha256 != evaluation.policy_sha256
        or plan.status not in {"waiting_for_root_checks", "ready_for_train"}
    ):
        return "structural_stack_root_unproven"
    return "structural_stack_root_recorded"


def _rolling_base_mismatch(
    *,
    evaluation: MergeTrainStructuralEvaluationInput,
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_plan_record: MergeTrainBatchLandingPlanRecord,
) -> MergeTrainStructuralReasonCode | None:
    candidate = candidate_record.candidate
    provenance = candidate.structural_provenance
    if provenance is None:
        return "structural_provenance_missing"
    plan = landing_plan_record.landing_plan
    expected_parent_sha = candidate.base_sha
    expected_parent_tree_sha = provenance.base_tree_sha
    for index in range(evaluation.target_queue_position - 1):
        landing_entry = plan.entries[index]
        candidate_entry = candidate.entries[index]
        candidate_step = provenance.steps[index]
        if landing_entry.status != "merged" or not landing_entry.merge_commit_sha:
            return "structural_prior_entry_not_landed"
        if landing_entry.landed_head_sha != candidate_entry.head_sha:
            return "structural_prior_entry_head_mismatch"
        if landing_entry.landed_head_tree_sha != candidate_entry.head_tree_sha:
            return "structural_prior_entry_tree_mismatch"
        if (
            landing_entry.recorded_rolling_base_sha != expected_parent_sha
            or landing_entry.recorded_rolling_base_tree_sha != expected_parent_tree_sha
        ):
            return "structural_rolling_chain_broken"
        if landing_entry.merge_commit_tree_sha != candidate_step.result_tree_sha:
            return "structural_rolling_chain_broken"
        expected_parent_sha = landing_entry.merge_commit_sha
        expected_parent_tree_sha = landing_entry.merge_commit_tree_sha
    if evaluation.observed_base_sha != expected_parent_sha:
        return "structural_base_sha_mismatch"
    if evaluation.observed_base_tree_sha != expected_parent_tree_sha:
        return "structural_base_tree_mismatch"
    return None


def _success_reasons(
    evaluation: MergeTrainStructuralEvaluationInput,
    primary_reason: MergeTrainStructuralReasonCode,
) -> tuple[MergeTrainStructuralReasonCode, ...]:
    reasons = [primary_reason]
    if evaluation.combined_owner_review is not None:
        reasons.append("structural_combined_owner_review_recorded")
    return tuple(reasons)


def _success_result(
    *,
    status: str,
    reason_codes: tuple[MergeTrainStructuralReasonCode, ...],
    evaluation: MergeTrainStructuralEvaluationInput,
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_plan_record: MergeTrainBatchLandingPlanRecord,
) -> MergeTrainStructuralCandidateResult:
    candidate = candidate_record.candidate
    provenance = candidate.structural_provenance
    return MergeTrainStructuralCandidateResult.model_validate(
        {
            "status": status,
            "reason_codes": reason_codes,
            "effective_base_sha": evaluation.observed_base_sha,
            "effective_base_tree_sha": evaluation.observed_base_tree_sha,
            "candidate_sha256": candidate.candidate_sha256,
            "landing_plan_sha256": landing_plan_record.landing_plan.landing_plan_sha256,
            "provenance_sha256": provenance.provenance_sha256 if provenance is not None else "",
        }
    )


def _bound_result(
    evaluation: MergeTrainStructuralEvaluationInput,
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_plan_record: MergeTrainBatchLandingPlanRecord,
    reason: MergeTrainStructuralReasonCode,
) -> MergeTrainStructuralCandidateResult:
    candidate = candidate_record.candidate
    provenance = candidate.structural_provenance
    return _result(
        "mismatch",
        reason,
        effective_base_sha=evaluation.observed_base_sha,
        effective_base_tree_sha=evaluation.observed_base_tree_sha,
        candidate_sha256=candidate.candidate_sha256,
        landing_plan_sha256=landing_plan_record.landing_plan.landing_plan_sha256,
        provenance_sha256=provenance.provenance_sha256 if provenance is not None else "",
    )


def _result(
    status: str,
    reason: MergeTrainStructuralReasonCode,
    *,
    effective_base_sha: str = "",
    effective_base_tree_sha: str = "",
    candidate_sha256: str = "",
    landing_plan_sha256: str = "",
    provenance_sha256: str = "",
) -> MergeTrainStructuralCandidateResult:
    return MergeTrainStructuralCandidateResult.model_validate(
        {
            "status": status,
            "reason_codes": (reason,),
            "effective_base_sha": effective_base_sha,
            "effective_base_tree_sha": effective_base_tree_sha,
            "candidate_sha256": candidate_sha256,
            "landing_plan_sha256": landing_plan_sha256,
            "provenance_sha256": provenance_sha256,
        }
    )
