import unittest

from pydantic import ValidationError

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_candidate_ref,
    build_merge_train_batch_id,
    build_merge_train_batch_landing_plan,
)
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainCombinedCandidateOwnerReview,
    MergeTrainOwnerEvidenceBinding,
    MergeTrainRollingStep,
    MergeTrainStructuralDeltaFingerprint,
    MergeTrainStructuralCandidateResult,
    MergeTrainStructuralEntryBinding,
    MergeTrainStructuralEntryObservation,
    MergeTrainStructuralEvaluationInput,
    MergeTrainStructuralProvenance,
    MergeTrainStructuralSubject,
)
from control_plane.merge_train_structural_provenance import (
    evaluate_merge_train_structural_candidate,
)


class MergeTrainStructuralProvenanceTests(unittest.TestCase):
    def test_single_candidate_is_exact_only_on_recorded_base(self) -> None:
        candidate_record, landing_record = _records((_entry(1, 1),))

        exact = _evaluate(candidate_record, landing_record, target_position=1)
        moved = _evaluate(
            candidate_record,
            landing_record,
            target_position=1,
            base_sha="unrelated-base",
        )

        self.assertEqual(exact.status, "exact")
        self.assertIn("structural_single_entry_exact", exact.reason_codes)
        self.assertEqual(moved.status, "mismatch")
        self.assertIn("structural_base_sha_mismatch", moved.reason_codes)

    def test_missing_impact_or_landing_evidence_is_unknown(self) -> None:
        candidate_record, landing_record = _records((_entry(1, 1), _entry(2, 2)))
        missing_impact = _evaluation(
            candidate_record,
            landing_record,
            target_position=1,
            include_deltas=False,
        )
        first = landing_record.landing_plan.entries[0]
        incomplete_first = _landing_entry(
            first,
            status="merged",
            merge_commit_sha="landed-1",
        )
        incomplete_record = _landing_record(
            landing_record,
            entries=(incomplete_first, landing_record.landing_plan.entries[1]),
        )

        impact_result = evaluate_merge_train_structural_candidate(
            evaluation=missing_impact,
            candidate_record=candidate_record,
            landing_plan_record=landing_record,
        )
        landing_result = _evaluate(
            candidate_record,
            incomplete_record,
            target_position=2,
            base_sha="landed-1",
            base_tree_sha="tree-candidate-1",
        )

        self.assertEqual(impact_result.status, "unknown")
        self.assertIn("structural_impact_unknown", impact_result.reason_codes)
        self.assertEqual(landing_result.status, "unknown")
        self.assertIn("structural_landing_evidence_unavailable", landing_result.reason_codes)

    def test_recorded_landing_and_no_op_support_later_rolling_evaluation(self) -> None:
        candidate_record, landing_record = _records(
            (_entry(1, 1), _entry(2, 2), _entry(3, 3)),
            no_op_positions={2},
        )
        provenance = candidate_record.candidate.structural_provenance
        assert provenance is not None
        first = _landing_entry(
            landing_record.landing_plan.entries[0],
            status="merged",
            recorded_rolling_base_sha="base-main",
            recorded_rolling_base_tree_sha="tree-base",
            landed_head_sha="head-1",
            landed_head_tree_sha="tree-head-1",
            merge_commit_sha="landed-1",
            merge_commit_tree_sha=provenance.steps[0].result_tree_sha,
        )
        second = _landing_entry(
            landing_record.landing_plan.entries[1],
            status="skipped",
            recorded_rolling_base_sha="landed-1",
            recorded_rolling_base_tree_sha=provenance.steps[0].result_tree_sha,
            landed_head_sha="head-2",
            landed_head_tree_sha="tree-head-2",
            merge_commit_sha="landed-1",
            merge_commit_tree_sha=provenance.steps[0].result_tree_sha,
        )
        landing_record = _landing_record(
            landing_record,
            entries=(first, second, landing_record.landing_plan.entries[2]),
        )

        result = _evaluate(
            candidate_record,
            landing_record,
            target_position=3,
            base_sha="landed-1",
            base_tree_sha=provenance.steps[0].result_tree_sha,
        )

        self.assertEqual(result.status, "recorded_rolling")
        self.assertIn("structural_rolling_chain_recorded", result.reason_codes)

    def test_mixed_case_repository_identity_normalizes_consistently(self) -> None:
        candidate_record, landing_record = _records((_entry(1, 1),), repository="Example/Repo")
        evaluation = _evaluation(candidate_record, landing_record, target_position=1)
        payload = evaluation.model_dump(mode="json")
        payload["repository"] = "EXAMPLE/REPO"

        result = evaluate_merge_train_structural_candidate(
            evaluation=MergeTrainStructuralEvaluationInput.model_validate(payload),
            candidate_record=candidate_record,
            landing_plan_record=landing_record,
        )

        self.assertEqual(candidate_record.candidate.repository, "example/repo")
        self.assertEqual(landing_record.landing_plan.repository, "example/repo")
        self.assertEqual(result.status, "exact")

    def test_evaluator_is_total_for_truncated_and_reordered_landing_plans(self) -> None:
        candidate_record, landing_record = _records((_entry(1, 1), _entry(2, 2)))
        truncated_plan = landing_record.landing_plan.model_copy(
            update={"entries": landing_record.landing_plan.entries[:1]}
        )
        reordered_plan = landing_record.landing_plan.model_copy(
            update={"entries": tuple(reversed(landing_record.landing_plan.entries))}
        )

        truncated = evaluate_merge_train_structural_candidate(
            evaluation=_evaluation(candidate_record, landing_record, target_position=1),
            candidate_record=candidate_record,
            landing_plan_record=landing_record.model_copy(update={"landing_plan": truncated_plan}),
        )
        reordered = evaluate_merge_train_structural_candidate(
            evaluation=_evaluation(candidate_record, landing_record, target_position=1),
            candidate_record=candidate_record,
            landing_plan_record=landing_record.model_copy(update={"landing_plan": reordered_plan}),
        )

        self.assertEqual(truncated.status, "unknown")
        self.assertIn("structural_landing_evidence_unavailable", truncated.reason_codes)
        self.assertEqual(reordered.status, "mismatch")
        self.assertIn("structural_position_mismatch", reordered.reason_codes)

    def test_landing_entry_identity_must_match_candidate_provenance(self) -> None:
        candidate_record, landing_record = _records((_entry(1, 1),))
        cases = (
            ({"expected_head_sha": "different-head"}, "structural_head_sha_mismatch"),
            (
                {"expected_head_tree_sha": "different-head-tree"},
                "structural_head_tree_mismatch",
            ),
            ({"expected_base_sha": "different-base"}, "structural_base_sha_mismatch"),
            (
                {"recorded_candidate_result_sha": "different-result"},
                "structural_rolling_chain_broken",
            ),
        )

        for updates, reason in cases:
            with self.subTest(reason=reason):
                landing_entry = _landing_entry(
                    landing_record.landing_plan.entries[0],
                    **updates,
                )
                plan = MergeTrainBatchLandingPlan.model_validate(
                    {
                        **landing_record.landing_plan.model_dump(mode="python"),
                        "entries": (landing_entry,),
                        "landing_plan_sha256": "",
                    }
                )
                record = MergeTrainBatchLandingPlanRecord.model_validate(
                    {
                        **landing_record.model_dump(mode="python"),
                        "landing_plan": plan,
                    }
                )

                result = _evaluate(candidate_record, record, target_position=1)

                self.assertEqual(result.status, "mismatch")
                self.assertIn(reason, result.reason_codes)

    def test_delta_drift_overlap_same_subject_and_expansion_require_bound_evidence(self) -> None:
        candidate_record, landing_record = _records((_entry(1, 1), _entry(2, 2)))
        shared = MergeTrainStructuralSubject(product="video", system="verification")
        evaluation = _evaluation(
            candidate_record,
            landing_record,
            target_position=1,
            current_paths={1: ("shared.py",), 2: ("shared.py",)},
            reviewed_paths={1: ("old.py",), 2: ("shared.py",)},
            current_subjects={1: (shared,), 2: (shared,)},
            reviewed_subjects={1: (), 2: (shared,)},
        )

        blocked = evaluate_merge_train_structural_candidate(
            evaluation=evaluation,
            candidate_record=candidate_record,
            landing_plan_record=landing_record,
        )
        reviewed_evaluation = _with_owner_evidence(evaluation)
        reviewed = evaluate_merge_train_structural_candidate(
            evaluation=reviewed_evaluation,
            candidate_record=candidate_record,
            landing_plan_record=landing_record,
        )

        self.assertEqual(blocked.status, "mismatch")
        self.assertTrue(
            {
                "structural_delta_drift",
                "structural_changed_path_overlap",
                "structural_impact_expanded",
                "structural_same_subject_combined_review_required",
            }.issubset(blocked.reason_codes)
        )
        self.assertEqual(reviewed.status, "exact")
        self.assertIn("structural_combined_owner_review_recorded", reviewed.reason_codes)

    def test_combined_owner_evidence_is_non_authoritative_and_digest_bound(self) -> None:
        candidate_record, landing_record = _records((_entry(1, 1), _entry(2, 2)))
        evaluation = _evaluation(
            candidate_record,
            landing_record,
            target_position=1,
            current_paths={1: ("shared.py",), 2: ("shared.py",)},
            reviewed_paths={1: ("shared.py",), 2: ("shared.py",)},
        )
        with self.assertRaisesRegex(ValidationError, "exact L1 event bindings"):
            MergeTrainCombinedCandidateOwnerReview(
                evidence_bindings=(),
                candidate_sha256=evaluation.active_candidate_sha256,
                landing_plan_sha256=evaluation.active_landing_plan_sha256,
                policy_key=evaluation.policy_key,
                policy_sha256=evaluation.policy_sha256,
                entries=evaluation.entries,
            )
        review = _owner_evidence(evaluation)
        round_tripped = MergeTrainCombinedCandidateOwnerReview.model_validate(
            review.model_dump(mode="json")
        )
        tampered_payload = review.model_dump(mode="json")
        tampered_payload["entries_sha256"] = "0" * 64

        self.assertFalse(review.authoritative)
        self.assertFalse(review.authorizes_merge)
        self.assertEqual(round_tripped.review_sha256, review.review_sha256)
        with self.assertRaisesRegex(ValidationError, "entry digest"):
            MergeTrainCombinedCandidateOwnerReview.model_validate(tampered_payload)

    def test_provenance_and_landing_digest_round_trip_after_progress(self) -> None:
        candidate_record, landing_record = _records((_entry(1, 1),))
        provenance = candidate_record.candidate.structural_provenance
        assert provenance is not None
        provenance_round_trip = MergeTrainStructuralProvenance.model_validate(
            provenance.model_dump(mode="json")
        )
        entry = _landing_entry(
            landing_record.landing_plan.entries[0],
            status="merged",
            recorded_rolling_base_sha="base-main",
            recorded_rolling_base_tree_sha="tree-base",
            landed_head_sha="head-1",
            landed_head_tree_sha="tree-head-1",
            merge_commit_sha="landed-1",
            merge_commit_tree_sha="tree-candidate-1",
        )
        progressed = MergeTrainBatchLandingPlan.model_validate(
            {
                **landing_record.landing_plan.model_dump(mode="python"),
                "entries": (entry,),
            }
        )
        progressed_round_trip = MergeTrainBatchLandingPlan.model_validate(
            progressed.model_dump(mode="json")
        )

        self.assertEqual(provenance_round_trip.provenance_sha256, provenance.provenance_sha256)
        self.assertEqual(
            progressed_round_trip.landing_plan_sha256,
            landing_record.landing_plan.landing_plan_sha256,
        )


def _entry(pull_request_number: int, position: int) -> MergeTrainBatchEntry:
    return MergeTrainBatchEntry(
        pull_request_number=pull_request_number,
        position=position,
        head_sha=f"head-{pull_request_number}",
        head_tree_sha=f"tree-head-{pull_request_number}",
    )


def _provenance(
    entries: tuple[MergeTrainBatchEntry, ...],
    *,
    repository: str,
    no_op_positions: set[int],
) -> MergeTrainStructuralProvenance:
    parent_sha = "base-main"
    parent_tree = "tree-base"
    steps = []
    for entry in entries:
        no_op = entry.position in no_op_positions
        result_sha = parent_sha if no_op else f"candidate-{entry.position}"
        result_tree = parent_tree if no_op else f"tree-candidate-{entry.position}"
        steps.append(
            MergeTrainRollingStep(
                position=entry.position,
                pull_request_number=entry.pull_request_number,
                parent_sha=parent_sha,
                parent_tree_sha=parent_tree,
                head_sha=entry.head_sha,
                head_tree_sha=entry.head_tree_sha,
                result_sha=result_sha,
                result_tree_sha=result_tree,
                kind="no_op_already_contained" if no_op else "merge_commit",
            )
        )
        parent_sha, parent_tree = result_sha, result_tree
    return MergeTrainStructuralProvenance(
        repository=repository,
        base_branch="main",
        base_sha="base-main",
        base_tree_sha="tree-base",
        policy_key="example/repo:main",
        policy_sha256="policy-digest",
        entries=tuple(
            MergeTrainStructuralEntryBinding(
                position=entry.position,
                pull_request_number=entry.pull_request_number,
                head_sha=entry.head_sha,
                head_tree_sha=entry.head_tree_sha,
            )
            for entry in entries
        ),
        steps=tuple(steps),
        candidate_sha=parent_sha,
        candidate_tree_sha=parent_tree,
    )


def _records(
    entries: tuple[MergeTrainBatchEntry, ...],
    *,
    repository: str = "example/repo",
    no_op_positions: set[int] | None = None,
) -> tuple[MergeTrainBatchCandidateRecord, MergeTrainBatchLandingPlanRecord]:
    provenance = _provenance(
        entries,
        repository=repository,
        no_op_positions=no_op_positions or set(),
    )
    batch_id = build_merge_train_batch_id(
        repository=repository,
        base_branch="main",
        base_sha="base-main",
        entry_head_shas=tuple(entry.head_sha for entry in entries),
    )
    candidate = MergeTrainBatchCandidate(
        batch_id=batch_id,
        repository=repository,
        base_branch="main",
        base_sha="base-main",
        policy_key="example/repo:main",
        policy_sha256="policy-digest",
        candidate_ref=build_merge_train_batch_candidate_ref(
            repository=repository,
            base_branch="main",
            batch_id=batch_id,
        ),
        candidate_sha=provenance.candidate_sha,
        candidate_tree_sha=provenance.candidate_tree_sha,
        candidate_sha256=provenance.candidate_sha256,
        status="passed",
        entries=entries,
        structural_provenance=provenance,
        created_at="2026-08-11T04:00:00Z",
        updated_at="2026-08-11T04:00:00Z",
    )
    candidate_record = MergeTrainBatchCandidateRecord(
        record_id="candidate-record",
        source="test",
        updated_at="2026-08-11T04:00:00Z",
        candidate=candidate,
    )
    plan = build_merge_train_batch_landing_plan(
        candidate=candidate,
        merge_method="merge",
        created_at="2026-08-11T04:01:00Z",
    )
    return candidate_record, MergeTrainBatchLandingPlanRecord(
        record_id="landing-record",
        source="test",
        updated_at="2026-08-11T04:01:00Z",
        landing_plan=plan,
    )


def _delta(
    entry: MergeTrainBatchEntry,
    *,
    paths: tuple[str, ...],
    subjects: tuple[MergeTrainStructuralSubject, ...],
) -> MergeTrainStructuralDeltaFingerprint:
    return MergeTrainStructuralDeltaFingerprint(
        head_sha=entry.head_sha,
        head_tree_sha=entry.head_tree_sha,
        changed_paths=paths,
        affected_subjects=subjects,
    )


def _evaluation(
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_record: MergeTrainBatchLandingPlanRecord,
    *,
    target_position: int,
    base_sha: str = "base-main",
    base_tree_sha: str = "tree-base",
    include_deltas: bool = True,
    current_paths: dict[int, tuple[str, ...]] | None = None,
    reviewed_paths: dict[int, tuple[str, ...]] | None = None,
    current_subjects: dict[int, tuple[MergeTrainStructuralSubject, ...]] | None = None,
    reviewed_subjects: dict[int, tuple[MergeTrainStructuralSubject, ...]] | None = None,
) -> MergeTrainStructuralEvaluationInput:
    candidate = candidate_record.candidate
    current_paths = current_paths or {}
    reviewed_paths = reviewed_paths or current_paths
    current_subjects = current_subjects or {}
    reviewed_subjects = reviewed_subjects or current_subjects
    observations = []
    for entry in candidate.entries:
        default_paths = (f"src/pr-{entry.pull_request_number}.py",)
        current_delta = (
            _delta(
                entry,
                paths=current_paths.get(entry.pull_request_number, default_paths),
                subjects=current_subjects.get(entry.pull_request_number, ()),
            )
            if include_deltas
            else None
        )
        reviewed_delta = (
            _delta(
                entry,
                paths=reviewed_paths.get(entry.pull_request_number, default_paths),
                subjects=reviewed_subjects.get(entry.pull_request_number, ()),
            )
            if include_deltas
            else None
        )
        observations.append(
            MergeTrainStructuralEntryObservation(
                position=entry.position,
                pull_request_number=entry.pull_request_number,
                head_sha=entry.head_sha,
                head_tree_sha=entry.head_tree_sha,
                reviewed_delta=reviewed_delta,
                current_delta=current_delta,
            )
        )
    return MergeTrainStructuralEvaluationInput(
        repository=candidate.repository,
        base_branch=candidate.base_branch,
        target_pull_request_number=candidate.entries[target_position - 1].pull_request_number,
        target_queue_position=target_position,
        observed_base_sha=base_sha,
        observed_base_tree_sha=base_tree_sha,
        policy_key=candidate.policy_key,
        policy_sha256=candidate.policy_sha256,
        active_candidate_sha256=candidate.candidate_sha256,
        active_landing_plan_sha256=landing_record.landing_plan.landing_plan_sha256,
        entries=tuple(observations),
    )


def _owner_evidence(
    evaluation: MergeTrainStructuralEvaluationInput,
) -> MergeTrainCombinedCandidateOwnerReview:
    return MergeTrainCombinedCandidateOwnerReview(
        evidence_bindings=(
            MergeTrainOwnerEvidenceBinding(
                event_id="owner-acceptance:l1:event-1",
                binding_sha256="a" * 64,
            ),
        ),
        candidate_sha256=evaluation.active_candidate_sha256,
        landing_plan_sha256=evaluation.active_landing_plan_sha256,
        policy_key=evaluation.policy_key,
        policy_sha256=evaluation.policy_sha256,
        entries=evaluation.entries,
    )


def _with_owner_evidence(
    evaluation: MergeTrainStructuralEvaluationInput,
) -> MergeTrainStructuralEvaluationInput:
    return MergeTrainStructuralEvaluationInput.model_validate(
        {
            **evaluation.model_dump(mode="python"),
            "combined_owner_review": _owner_evidence(evaluation),
        }
    )


def _landing_entry(
    entry: MergeTrainBatchLandingEntry, **updates: object
) -> MergeTrainBatchLandingEntry:
    return type(entry).model_validate({**entry.model_dump(mode="python"), **updates})


def _landing_record(
    record: MergeTrainBatchLandingPlanRecord,
    *,
    entries: tuple[MergeTrainBatchLandingEntry, ...],
) -> MergeTrainBatchLandingPlanRecord:
    plan = MergeTrainBatchLandingPlan.model_validate(
        {**record.landing_plan.model_dump(mode="python"), "entries": entries}
    )
    return MergeTrainBatchLandingPlanRecord.model_validate(
        {**record.model_dump(mode="python"), "landing_plan": plan}
    )


def _evaluate(
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_record: MergeTrainBatchLandingPlanRecord,
    *,
    target_position: int,
    base_sha: str = "base-main",
    base_tree_sha: str = "tree-base",
) -> MergeTrainStructuralCandidateResult:
    return evaluate_merge_train_structural_candidate(
        evaluation=_evaluation(
            candidate_record,
            landing_record,
            target_position=target_position,
            base_sha=base_sha,
            base_tree_sha=base_tree_sha,
        ),
        candidate_record=candidate_record,
        landing_plan_record=landing_record,
    )


if __name__ == "__main__":
    unittest.main()
