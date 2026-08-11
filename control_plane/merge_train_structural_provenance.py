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
    merge_train_structural_evaluation_entries_sha256,
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
    plan_shape_result = _landing_plan_shape_result(
        evaluation=evaluation,
        candidate_record=candidate_record,
        landing_plan_record=landing_plan_record,
    )
    if plan_shape_result is not None:
        return plan_shape_result
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
    impact_result, combined_review_used = _impact_composition_result(
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
                    "structural_stack_root_recorded",
                    combined_review_used=combined_review_used,
                ),
                evaluation=evaluation,
                candidate_record=candidate_record,
                landing_plan_record=landing_plan_record,
            )
        return _success_result(
            status="exact",
            reason_codes=_success_reasons(
                (
                    "structural_single_entry_exact"
                    if len(candidate.entries) == 1
                    else "structural_batch_entry_exact"
                ),
                combined_review_used=combined_review_used,
            ),
            evaluation=evaluation,
            candidate_record=candidate_record,
            landing_plan_record=landing_plan_record,
        )
    rolling_result = _rolling_base_result(
        evaluation=evaluation,
        candidate_record=candidate_record,
        landing_plan_record=landing_plan_record,
    )
    if rolling_result is not None:
        return rolling_result
    return _success_result(
        status="recorded_rolling",
        reason_codes=_success_reasons(
            "structural_rolling_chain_recorded",
            combined_review_used=combined_review_used,
        ),
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


def _landing_plan_shape_result(
    *,
    evaluation: MergeTrainStructuralEvaluationInput,
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_plan_record: MergeTrainBatchLandingPlanRecord,
) -> MergeTrainStructuralCandidateResult | None:
    candidate_entries = candidate_record.candidate.entries
    landing_entries = landing_plan_record.landing_plan.entries
    if len(landing_entries) < len(candidate_entries):
        return _bound_result(
            evaluation,
            candidate_record,
            landing_plan_record,
            "structural_landing_evidence_unavailable",
            status="unknown",
        )
    if len(landing_entries) > len(candidate_entries):
        return _bound_result(
            evaluation,
            candidate_record,
            landing_plan_record,
            "structural_position_mismatch",
        )
    expected = tuple((entry.position, entry.pull_request_number) for entry in candidate_entries)
    observed = tuple((entry.position, entry.pull_request_number) for entry in landing_entries)
    if observed != expected:
        return _bound_result(
            evaluation,
            candidate_record,
            landing_plan_record,
            "structural_position_mismatch",
        )
    provenance = candidate_record.candidate.structural_provenance
    assert provenance is not None
    for candidate_entry, landing_entry, provenance_step in zip(
        candidate_entries,
        landing_entries,
        provenance.steps,
        strict=True,
    ):
        if landing_entry.expected_head_sha != candidate_entry.head_sha:
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_head_sha_mismatch",
            )
        if landing_entry.expected_head_tree_sha != candidate_entry.head_tree_sha:
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_head_tree_mismatch",
            )
        if landing_entry.expected_base_sha != candidate_record.candidate.base_sha:
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_base_sha_mismatch",
            )
        recorded_step = (
            landing_entry.recorded_candidate_parent_sha,
            landing_entry.recorded_candidate_parent_tree_sha,
            landing_entry.recorded_candidate_result_sha,
            landing_entry.recorded_candidate_result_tree_sha,
        )
        expected_step = (
            provenance_step.parent_sha,
            provenance_step.parent_tree_sha,
            provenance_step.result_sha,
            provenance_step.result_tree_sha,
        )
        if recorded_step != expected_step:
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_rolling_chain_broken",
            )
    return None


def _impact_composition_result(
    *,
    evaluation: MergeTrainStructuralEvaluationInput,
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_plan_record: MergeTrainBatchLandingPlanRecord,
) -> tuple[MergeTrainStructuralCandidateResult | None, bool]:
    if any(
        entry.reviewed_delta is None or entry.current_delta is None for entry in evaluation.entries
    ):
        return (
            _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_impact_unknown",
                status="unknown",
            ),
            False,
        )
    reasons: list[MergeTrainStructuralReasonCode] = []
    if any(
        entry.reviewed_delta.fingerprint_sha256 != entry.current_delta.fingerprint_sha256
        for entry in evaluation.entries
        if entry.reviewed_delta is not None and entry.current_delta is not None
    ):
        reasons.append("structural_delta_drift")
    if any(
        not set(entry.current_delta.affected_subjects).issubset(
            entry.reviewed_delta.affected_subjects
        )
        for entry in evaluation.entries
        if entry.reviewed_delta is not None and entry.current_delta is not None
    ):
        reasons.append("structural_impact_expanded")
    path_positions: dict[str, set[int]] = {}
    subject_positions: dict[tuple[str, str], set[int]] = {}
    for entry in evaluation.entries:
        current_delta = entry.current_delta
        assert current_delta is not None
        for path in current_delta.changed_paths:
            path_positions.setdefault(path, set()).add(entry.position)
        for subject in current_delta.affected_subjects:
            subject_positions.setdefault((subject.product, subject.system), set()).add(
                entry.position
            )
    if any(len(positions) > 1 for positions in path_positions.values()):
        reasons.append("structural_changed_path_overlap")
    if any(len(positions) > 1 for positions in subject_positions.values()):
        reasons.append("structural_same_subject_combined_review_required")
    if not reasons:
        return None, False
    review = evaluation.combined_owner_review
    if review is None:
        return (
            _bound_result_reasons(
                evaluation,
                candidate_record,
                landing_plan_record,
                tuple(reasons),
            ),
            False,
        )
    if not _combined_review_matches(review=review, evaluation=evaluation):
        return (
            _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_combined_owner_review_mismatch",
            ),
            False,
        )
    return None, True


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
        and review.entries_sha256
        == merge_train_structural_evaluation_entries_sha256(evaluation.entries)
        and bool(review.evidence_bindings)
        and review.authoritative is False
        and review.authorizes_merge is False
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


def _rolling_base_result(
    *,
    evaluation: MergeTrainStructuralEvaluationInput,
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_plan_record: MergeTrainBatchLandingPlanRecord,
) -> MergeTrainStructuralCandidateResult | None:
    candidate = candidate_record.candidate
    provenance = candidate.structural_provenance
    if provenance is None:
        return _bound_result(
            evaluation,
            candidate_record,
            landing_plan_record,
            "structural_provenance_missing",
            status="unknown",
        )
    plan = landing_plan_record.landing_plan
    expected_parent_sha = candidate.base_sha
    expected_parent_tree_sha = provenance.base_tree_sha
    for index in range(evaluation.target_queue_position - 1):
        if (
            index >= len(plan.entries)
            or index >= len(candidate.entries)
            or index >= len(provenance.steps)
        ):
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_landing_evidence_unavailable",
                status="unknown",
            )
        landing_entry = plan.entries[index]
        candidate_entry = candidate.entries[index]
        candidate_step = provenance.steps[index]
        if landing_entry.status not in {"merged", "skipped"}:
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_prior_entry_not_landed",
                status="unknown",
            )
        if not all(
            (
                landing_entry.recorded_rolling_base_sha,
                landing_entry.recorded_rolling_base_tree_sha,
                landing_entry.landed_head_sha,
                landing_entry.landed_head_tree_sha,
                landing_entry.merge_commit_sha,
                landing_entry.merge_commit_tree_sha,
            )
        ):
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_landing_evidence_unavailable",
                status="unknown",
            )
        if landing_entry.landed_head_sha != candidate_entry.head_sha:
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_prior_entry_head_mismatch",
            )
        if landing_entry.landed_head_tree_sha != candidate_entry.head_tree_sha:
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_prior_entry_tree_mismatch",
            )
        if (
            landing_entry.recorded_candidate_parent_sha != candidate_step.parent_sha
            or landing_entry.recorded_candidate_parent_tree_sha != candidate_step.parent_tree_sha
            or landing_entry.recorded_candidate_result_sha != candidate_step.result_sha
            or landing_entry.recorded_candidate_result_tree_sha != candidate_step.result_tree_sha
        ):
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_rolling_chain_broken",
            )
        if (
            landing_entry.recorded_rolling_base_sha != expected_parent_sha
            or landing_entry.recorded_rolling_base_tree_sha != expected_parent_tree_sha
        ):
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_rolling_chain_broken",
            )
        if landing_entry.status == "skipped":
            if (
                candidate_step.kind != "no_op_already_contained"
                or landing_entry.merge_commit_sha != expected_parent_sha
                or landing_entry.merge_commit_tree_sha != expected_parent_tree_sha
            ):
                return _bound_result(
                    evaluation,
                    candidate_record,
                    landing_plan_record,
                    "structural_rolling_chain_broken",
                )
        elif candidate_step.kind != "merge_commit":
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_rolling_chain_broken",
            )
        if landing_entry.merge_commit_tree_sha != candidate_step.result_tree_sha:
            return _bound_result(
                evaluation,
                candidate_record,
                landing_plan_record,
                "structural_rolling_chain_broken",
            )
        expected_parent_sha = landing_entry.merge_commit_sha
        expected_parent_tree_sha = landing_entry.merge_commit_tree_sha
    if evaluation.observed_base_sha != expected_parent_sha:
        return _bound_result(
            evaluation,
            candidate_record,
            landing_plan_record,
            "structural_base_sha_mismatch",
        )
    if evaluation.observed_base_tree_sha != expected_parent_tree_sha:
        return _bound_result(
            evaluation,
            candidate_record,
            landing_plan_record,
            "structural_base_tree_mismatch",
        )
    return None


def _success_reasons(
    primary_reason: MergeTrainStructuralReasonCode,
    *,
    combined_review_used: bool,
) -> tuple[MergeTrainStructuralReasonCode, ...]:
    reasons = [primary_reason]
    if combined_review_used:
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
    *,
    status: str = "mismatch",
) -> MergeTrainStructuralCandidateResult:
    return _bound_result_reasons(
        evaluation,
        candidate_record,
        landing_plan_record,
        (reason,),
        status=status,
    )


def _bound_result_reasons(
    evaluation: MergeTrainStructuralEvaluationInput,
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_plan_record: MergeTrainBatchLandingPlanRecord,
    reasons: tuple[MergeTrainStructuralReasonCode, ...],
    *,
    status: str = "mismatch",
) -> MergeTrainStructuralCandidateResult:
    candidate = candidate_record.candidate
    provenance = candidate.structural_provenance
    return MergeTrainStructuralCandidateResult.model_validate(
        {
            "status": status,
            "reason_codes": reasons,
            "effective_base_sha": evaluation.observed_base_sha,
            "effective_base_tree_sha": evaluation.observed_base_tree_sha,
            "candidate_sha256": candidate.candidate_sha256,
            "landing_plan_sha256": landing_plan_record.landing_plan.landing_plan_sha256,
            "provenance_sha256": provenance.provenance_sha256 if provenance is not None else "",
        }
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
