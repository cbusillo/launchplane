import unittest

from pydantic import ValidationError

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_candidate_ref,
    build_merge_train_batch_id,
    build_merge_train_batch_landing_plan,
)
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainCombinedCandidateOwnerReview,
    MergeTrainRollingStep,
    MergeTrainStructuralEntryBinding,
    MergeTrainStructuralEntryObservation,
    MergeTrainStructuralEvaluationInput,
    MergeTrainStructuralProvenance,
    MergeTrainStructuralSubject,
    MergeTrainStackCollapseRootProof,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapseEntry,
    MergeTrainStackCollapseMutation,
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
)
from control_plane.merge_train_structural_provenance import (
    evaluate_merge_train_structural_candidate,
)


class MergeTrainStructuralProvenanceTests(unittest.TestCase):
    def test_single_candidate_is_exact_only_on_recorded_base(self) -> None:
        candidate_record, landing_record = _records((_entry(1, 1, "head-1", "tree-head-1"),))

        exact = evaluate_merge_train_structural_candidate(
            evaluation=_evaluation(candidate_record, landing_record, target_position=1),
            candidate_record=candidate_record,
            landing_plan_record=landing_record,
        )
        moved = evaluate_merge_train_structural_candidate(
            evaluation=_evaluation(
                candidate_record,
                landing_record,
                target_position=1,
                base_sha="unrelated-base",
            ),
            candidate_record=candidate_record,
            landing_plan_record=landing_record,
        )

        self.assertEqual(exact.status, "exact")
        self.assertIn("structural_single_entry_exact", exact.reason_codes)
        self.assertEqual(moved.status, "mismatch")
        self.assertIn("structural_base_sha_mismatch", moved.reason_codes)

    def test_batch_allows_only_recorded_landed_rolling_base(self) -> None:
        candidate_record, landing_record = _records(
            (
                _entry(1, 1, "head-1", "tree-head-1"),
                _entry(2, 2, "head-2", "tree-head-2"),
            )
        )
        provenance = candidate_record.candidate.structural_provenance
        self.assertIsNotNone(provenance)
        assert provenance is not None
        first_step = provenance.steps[0]
        first = landing_record.landing_plan.entries[0].model_copy(
            update={
                "status": "merged",
                "recorded_rolling_base_sha": "base-main",
                "recorded_rolling_base_tree_sha": "tree-base",
                "landed_head_sha": "head-1",
                "landed_head_tree_sha": "tree-head-1",
                "merge_commit_sha": "landed-1",
                "merge_commit_tree_sha": first_step.result_tree_sha,
            }
        )
        landing_record = landing_record.model_copy(
            update={
                "landing_plan": landing_record.landing_plan.model_copy(
                    update={"entries": (first, landing_record.landing_plan.entries[1])}
                )
            }
        )

        result = evaluate_merge_train_structural_candidate(
            evaluation=_evaluation(
                candidate_record,
                landing_record,
                target_position=2,
                base_sha="landed-1",
                base_tree_sha=first_step.result_tree_sha,
            ),
            candidate_record=candidate_record,
            landing_plan_record=landing_record,
        )

        self.assertEqual(result.status, "recorded_rolling")
        self.assertIn("structural_rolling_chain_recorded", result.reason_codes)

    def test_head_queue_plan_and_policy_drift_fail_closed(self) -> None:
        candidate_record, landing_record = _records(
            (
                _entry(1, 1, "head-1", "tree-head-1"),
                _entry(2, 2, "head-2", "tree-head-2"),
            )
        )
        cases = (
            ({"head_sha": "changed-head"}, "structural_head_sha_mismatch"),
            ({"candidate_sha256": "changed-candidate"}, "structural_candidate_digest_mismatch"),
            ({"landing_plan_sha256": "changed-plan"}, "structural_landing_plan_digest_mismatch"),
            ({"policy_sha256": "changed-policy"}, "structural_policy_mismatch"),
        )
        for changes, reason in cases:
            with self.subTest(reason=reason):
                evaluation = _evaluation(candidate_record, landing_record, target_position=1)
                payload = evaluation.model_dump(mode="json")
                if "head_sha" in changes:
                    payload["entries"][0]["head_sha"] = changes["head_sha"]
                elif "candidate_sha256" in changes:
                    payload["active_candidate_sha256"] = changes["candidate_sha256"]
                elif "landing_plan_sha256" in changes:
                    payload["active_landing_plan_sha256"] = changes["landing_plan_sha256"]
                else:
                    payload["policy_sha256"] = changes["policy_sha256"]
                result = evaluate_merge_train_structural_candidate(
                    evaluation=MergeTrainStructuralEvaluationInput.model_validate(payload),
                    candidate_record=candidate_record,
                    landing_plan_record=landing_record,
                )
                self.assertEqual(result.status, "mismatch")
                self.assertIn(reason, result.reason_codes)

    def test_same_subject_requires_exact_combined_candidate_review(self) -> None:
        shared = MergeTrainStructuralSubject(product="video", system="verification")
        candidate_record, landing_record = _records(
            (
                _entry(1, 1, "head-1", "tree-head-1", subjects=(shared,)),
                _entry(2, 2, "head-2", "tree-head-2", subjects=(shared,)),
            )
        )
        evaluation = _evaluation(candidate_record, landing_record, target_position=1)

        blocked = evaluate_merge_train_structural_candidate(
            evaluation=evaluation,
            candidate_record=candidate_record,
            landing_plan_record=landing_record,
        )
        reviewed = evaluate_merge_train_structural_candidate(
            evaluation=evaluation.model_copy(
                update={
                    "combined_owner_review": MergeTrainCombinedCandidateOwnerReview(
                        evidence_id="owner-review-1",
                        candidate_sha256=evaluation.active_candidate_sha256,
                        landing_plan_sha256=evaluation.active_landing_plan_sha256,
                        policy_key=evaluation.policy_key,
                        policy_sha256=evaluation.policy_sha256,
                        entries=evaluation.entries,
                    )
                }
            ),
            candidate_record=candidate_record,
            landing_plan_record=landing_record,
        )

        self.assertEqual(blocked.status, "mismatch")
        self.assertIn("structural_same_subject_combined_review_required", blocked.reason_codes)
        self.assertEqual(reviewed.status, "exact")
        self.assertIn("structural_combined_owner_review_recorded", reviewed.reason_codes)

    def test_legacy_missing_provenance_is_unknown(self) -> None:
        candidate_record, landing_record = _records((_entry(1, 1, "head-1", "tree-head-1"),))
        legacy = candidate_record.model_copy(
            update={
                "candidate": candidate_record.candidate.model_copy(
                    update={
                        "candidate_tree_sha": "",
                        "candidate_sha256": "",
                        "structural_provenance": None,
                    }
                )
            }
        )

        result = evaluate_merge_train_structural_candidate(
            evaluation=_evaluation(candidate_record, landing_record, target_position=1),
            candidate_record=legacy,
            landing_plan_record=landing_record,
        )

        self.assertEqual(result.status, "unknown")
        self.assertNotEqual(result.status, "exact")

    def test_no_op_step_and_digest_tamper_are_explicit(self) -> None:
        entry = _entry(1, 1, "head-1", "tree-head-1")
        provenance = _provenance((entry,), no_op=True)
        self.assertEqual(provenance.steps[0].kind, "no_op_already_contained")
        payload = provenance.model_dump(mode="json")
        payload["provenance_sha256"] = "0" * 64
        with self.assertRaisesRegex(ValidationError, "digest does not match"):
            MergeTrainStructuralProvenance.model_validate(payload)

    def test_proven_stack_collapse_root_is_recorded_rolling(self) -> None:
        candidate_record, _ = _records((_entry(10, 1, "collapsed-root", "tree-collapsed-root"),))
        proof = MergeTrainStackCollapseRootProof(
            collapse_record_id="collapse-record",
            collapse_id="collapse-id",
            root_pull_request_number=10,
            original_root_head_sha="root-original",
            collapsed_root_head_sha="collapsed-root",
            collapsed_root_tree_sha="tree-collapsed-root",
        )
        original = candidate_record.candidate.structural_provenance
        assert original is not None
        payload = original.model_dump(
            mode="json", exclude={"provenance_sha256", "candidate_sha256"}
        )
        payload["stack_collapse_root"] = proof.model_dump(mode="json")
        provenance = MergeTrainStructuralProvenance.model_validate(payload)
        candidate = candidate_record.candidate.model_copy(
            update={
                "candidate_sha256": provenance.candidate_sha256,
                "stack_collapse_root": proof,
                "structural_provenance": provenance,
            }
        )
        candidate_record = candidate_record.model_copy(update={"candidate": candidate})
        landing_record = MergeTrainBatchLandingPlanRecord(
            record_id="landing-record-stack",
            source="test",
            updated_at="2026-08-11T04:01:00Z",
            landing_plan=build_merge_train_batch_landing_plan(
                candidate=candidate,
                merge_method="merge",
                created_at="2026-08-11T04:01:00Z",
            ),
        )
        collapse_record = MergeTrainStackCollapsePlanRecord(
            record_id="collapse-record",
            source="test",
            updated_at="2026-08-11T03:59:00Z",
            plan=MergeTrainStackCollapsePlan(
                collapse_id="collapse-id",
                repository="example/repo",
                base_branch="main",
                root_pull_request_number=10,
                root_initial_head_sha="root-original",
                root_head_ref="feature/root",
                policy_key="example/repo:main",
                policy_sha256="policy-digest",
                status="waiting_for_root_checks",
                entries=(
                    MergeTrainStackCollapseEntry(
                        pull_request_number=10,
                        position=1,
                        head_sha="root-original",
                        head_ref="feature/root",
                        base_sha="base-main",
                        base_ref="main",
                    ),
                    MergeTrainStackCollapseEntry(
                        pull_request_number=11,
                        position=2,
                        head_sha="child-head",
                        head_ref="feature/child",
                        base_sha="root-original",
                        base_ref="feature/root",
                    ),
                ),
                mutations=(
                    MergeTrainStackCollapseMutation(
                        child_pull_request_number=11,
                        parent_pull_request_number=10,
                        child_head_sha="child-head",
                        expected_parent_head_sha="root-original",
                        parent_head_ref="feature/root",
                        status="mutated",
                        merge_commit_sha="collapsed-root",
                    ),
                ),
                created_at="2026-08-11T03:58:00Z",
                updated_at="2026-08-11T03:59:00Z",
            ),
        )

        result = evaluate_merge_train_structural_candidate(
            evaluation=_evaluation(candidate_record, landing_record, target_position=1),
            candidate_record=candidate_record,
            landing_plan_record=landing_record,
            stack_collapse_record=collapse_record,
        )

        self.assertEqual(result.status, "recorded_rolling")
        self.assertIn("structural_stack_root_recorded", result.reason_codes)


def _entry(
    pull_request_number: int,
    position: int,
    head_sha: str,
    head_tree_sha: str,
    *,
    subjects: tuple[MergeTrainStructuralSubject, ...] = (),
) -> MergeTrainBatchEntry:
    return MergeTrainBatchEntry(
        pull_request_number=pull_request_number,
        position=position,
        head_sha=head_sha,
        head_tree_sha=head_tree_sha,
        impact_status="known",
        affected_subjects=subjects,
    )


def _provenance(
    entries: tuple[MergeTrainBatchEntry, ...], *, no_op: bool = False
) -> MergeTrainStructuralProvenance:
    parent_sha = "base-main"
    parent_tree = "tree-base"
    steps = []
    for entry in entries:
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
        repository="example/repo",
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
                impact_status=entry.impact_status,
                affected_subjects=entry.affected_subjects,
            )
            for entry in entries
        ),
        steps=tuple(steps),
        candidate_sha=parent_sha,
        candidate_tree_sha=parent_tree,
    )


def _records(
    entries: tuple[MergeTrainBatchEntry, ...],
) -> tuple[MergeTrainBatchCandidateRecord, MergeTrainBatchLandingPlanRecord]:
    provenance = _provenance(entries)
    batch_id = build_merge_train_batch_id(
        repository="example/repo",
        base_branch="main",
        base_sha="base-main",
        entry_head_shas=tuple(entry.head_sha for entry in entries),
    )
    candidate = MergeTrainBatchCandidate(
        batch_id=batch_id,
        repository="example/repo",
        base_branch="main",
        base_sha="base-main",
        policy_key="example/repo:main",
        policy_sha256="policy-digest",
        candidate_ref=build_merge_train_batch_candidate_ref(
            repository="example/repo", base_branch="main", batch_id=batch_id
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


def _evaluation(
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_record: MergeTrainBatchLandingPlanRecord,
    *,
    target_position: int,
    base_sha: str = "base-main",
    base_tree_sha: str = "tree-base",
) -> MergeTrainStructuralEvaluationInput:
    candidate = candidate_record.candidate
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
        entries=tuple(
            MergeTrainStructuralEntryObservation(
                position=entry.position,
                pull_request_number=entry.pull_request_number,
                head_sha=entry.head_sha,
                head_tree_sha=entry.head_tree_sha,
                impact_status=entry.impact_status,
                affected_subjects=entry.affected_subjects,
            )
            for entry in candidate.entries
        ),
    )


if __name__ == "__main__":
    unittest.main()
