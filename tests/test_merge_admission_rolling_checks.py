from __future__ import annotations

import unittest
from unittest.mock import patch

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlan,
    build_merge_train_batch_candidate_ref,
    build_merge_train_batch_id,
    build_merge_train_batch_landing_plan,
)
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainStructuralEntryObservation,
    MergeTrainStructuralProvenance,
)
from control_plane.merge_admission import MergeAdmissionEvaluation
from control_plane.merge_admission_live import LiveMergeAdmissionEvaluator
from control_plane.merge_train import MergeTrainDryRunSnapshot
from control_plane.merge_train_github import GitHubMergeTrainClient
from tests.merge_train_policy_fixtures import build_test_merge_train_policy_record
from tests.test_merge_admission_live import (
    _EmptyEngineeringReviewStore,
    _StaticSnapshotReader,
    _UnusedRepositoryEvidenceProvider,
    _authz_policy_record,
    _queued_pull_request,
)
from tests.test_merge_admission_records import _guard_records
from tests.test_merge_readiness import BASE_SHA, REPOSITORY


FINAL_CANDIDATE_SHA = "9" * 40
LANDED_FIRST_SHA = "b" * 40


class _StrictCandidateChecks:
    def __init__(self, *, conclusion: str = "success") -> None:
        self.conclusion = conclusion

    def request(self, *, method: str, path: str, body: dict[str, object] | None = None) -> object:
        if method != "GET" or body is not None:
            raise AssertionError("Admission evidence must be read-only")
        if path.endswith("/branches/main"):
            return {
                "protected": True,
                "protection": {
                    "required_status_checks": {
                        "enforcement_level": "everyone",
                        "checks": [{"context": "ci-gate", "app_id": 1}],
                        "contexts": ["ci-gate"],
                    }
                },
            }
        if "/compare/" in path:
            # GitHub's actual first landing has the candidate step's tree, but
            # a distinct commit identity that is not an ancestor of the candidate.
            return {
                "status": "ahead"
                if path.endswith(f"/{BASE_SHA}...{FINAL_CANDIDATE_SHA}")
                else "diverged"
            }
        if path.endswith(f"/commits/{FINAL_CANDIDATE_SHA}/status"):
            return {"sha": FINAL_CANDIDATE_SHA, "statuses": []}
        if f"/commits/{FINAL_CANDIDATE_SHA}/check-runs?" in path:
            return {
                "check_runs": [
                    {
                        "name": "ci-gate",
                        "head_sha": FINAL_CANDIDATE_SHA,
                        "status": "completed",
                        "conclusion": self.conclusion,
                        "app": {"id": 1},
                    }
                ]
            }
        raise AssertionError(f"Unexpected GitHub read: {path}")


class RollingCandidateCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.policy = build_test_merge_train_policy_record(repository=REPOSITORY)
        candidate_record, landing_record, controller, _ = _guard_records(
            policy_sha256=self.policy.policy_sha256
        )
        original = candidate_record.candidate
        assert original.structural_provenance is not None
        second = MergeTrainBatchEntry(
            pull_request_number=2084, position=2, head_sha="7" * 40, head_tree_sha="8" * 40
        )
        entries = (*original.entries, second)
        provenance = original.structural_provenance.model_dump(mode="json")
        provenance.update(
            entries=[
                {
                    "position": entry.position,
                    "pull_request_number": entry.pull_request_number,
                    "head_sha": entry.head_sha,
                    "head_tree_sha": entry.head_tree_sha,
                    "impact_status": entry.impact_status,
                }
                for entry in entries
            ],
            steps=[
                *provenance["steps"],
                {
                    "position": 2,
                    "pull_request_number": second.pull_request_number,
                    "parent_sha": original.candidate_sha,
                    "parent_tree_sha": original.candidate_tree_sha,
                    "head_sha": second.head_sha,
                    "head_tree_sha": second.head_tree_sha,
                    "result_sha": FINAL_CANDIDATE_SHA,
                    "result_tree_sha": "a" * 40,
                    "kind": "merge_commit",
                },
            ],
            candidate_sha=FINAL_CANDIDATE_SHA,
            candidate_tree_sha="a" * 40,
            candidate_sha256="",
            provenance_sha256="",
        )
        batch_id = build_merge_train_batch_id(
            repository=REPOSITORY,
            base_branch="main",
            base_sha=BASE_SHA,
            entry_head_shas=tuple(entry.head_sha for entry in entries),
        )
        candidate = MergeTrainBatchCandidate.model_validate(
            {
                **original.model_dump(mode="json"),
                "batch_id": batch_id,
                "candidate_ref": build_merge_train_batch_candidate_ref(
                    repository=REPOSITORY, base_branch="main", batch_id=batch_id
                ),
                "entries": entries,
                "candidate_sha": FINAL_CANDIDATE_SHA,
                "candidate_tree_sha": "a" * 40,
                "candidate_sha256": "",
                "structural_provenance": MergeTrainStructuralProvenance.model_validate(provenance),
            }
        )
        self.candidate_record = candidate_record.model_copy(update={"candidate": candidate})
        self.plan = build_merge_train_batch_landing_plan(
            candidate=candidate, merge_method="merge", created_at=original.created_at
        )
        self.landing_record = landing_record
        self.controller = controller.model_copy(
            update={"step_payload": {"expected_effect_sha": FINAL_CANDIDATE_SHA}}
        )

    def _evaluate(
        self,
        *,
        after_first_landing: bool,
        base_tree: str | None = None,
        conclusion: str = "success",
    ) -> MergeAdmissionEvaluation:
        plan = self.plan
        if after_first_landing:
            first = plan.entries[0]
            landed = MergeTrainBatchLandingEntry.model_validate(
                {
                    **first.model_dump(mode="json"),
                    "status": "merged",
                    "recorded_rolling_base_sha": BASE_SHA,
                    "recorded_rolling_base_tree_sha": first.recorded_candidate_parent_tree_sha,
                    "landed_head_sha": first.expected_head_sha,
                    "landed_head_tree_sha": first.expected_head_tree_sha,
                    "merge_commit_sha": LANDED_FIRST_SHA,
                    "merge_commit_tree_sha": first.recorded_candidate_result_tree_sha,
                }
            )
            plan = MergeTrainBatchLandingPlan.model_validate(
                {**plan.model_dump(mode="json"), "entries": (landed, plan.entries[1])}
            )
        entry = plan.entries[int(after_first_landing)]
        observed_base = LANDED_FIRST_SHA if after_first_landing else BASE_SHA
        observed_tree = base_tree or entry.recorded_candidate_parent_tree_sha
        queue = tuple(
            _queued_pull_request(
                number=item.pull_request_number,
                head_sha=item.expected_head_sha,
                created_at=f"2026-08-11T03:00:0{item.position}Z",
            ).model_copy(update={"base_sha": observed_base})
            for item in plan.entries
            if item.status == "planned"
        )
        evaluator = LiveMergeAdmissionEvaluator(
            store=object(),
            repository_evidence_provider=_UnusedRepositoryEvidenceProvider(),
            technical_check_client=GitHubMergeTrainClient(
                transport=_StrictCandidateChecks(conclusion=conclusion)
            ),
            policy_record_provider=lambda: self.policy,
            snapshot_reader=_StaticSnapshotReader(
                MergeTrainDryRunSnapshot(
                    repository=REPOSITORY,
                    base_branch="main",
                    base_sha=observed_base,
                    pull_requests=queue,
                )
            ),
        )
        observations = [
            MergeTrainStructuralEntryObservation(
                position=item.position,
                pull_request_number=item.pull_request_number,
                head_sha=item.head_sha,
                head_tree_sha=item.head_tree_sha,
            )
            for item in self.candidate_record.candidate.entries
        ]
        with (
            patch.object(LiveMergeAdmissionEvaluator, "_entry_evidence", side_effect=observations),
            patch(
                "control_plane.merge_admission_live.require_engineering_review_decision_store",
                return_value=_EmptyEngineeringReviewStore(),
            ),
            patch(
                "control_plane.merge_admission_live.read_active_authz_policy_record",
                return_value=_authz_policy_record(),
            ),
        ):
            return evaluator.evaluate(
                candidate_record=self.candidate_record,
                landing_plan_record=self.landing_record.model_copy(update={"landing_plan": plan}),
                entry=entry,
                observed_base_sha=observed_base,
                observed_base_tree_sha=observed_tree,
                observed_head_sha=entry.expected_head_sha,
                observed_head_tree_sha=entry.expected_head_tree_sha,
                controller_state=self.controller,
                expected_lease_owner=self.controller.lease_owner,
                stack_collapse_record=None,
                evaluated_at="2026-08-11T03:01:00Z",
            )

    def test_both_entries_pass_strict_checks_across_a_recorded_first_landing(self) -> None:
        first = self._evaluate(after_first_landing=False)
        second = self._evaluate(after_first_landing=True)
        self.assertEqual(first.readiness.state, "ready")
        self.assertEqual(second.structural_result.status, "recorded_rolling")
        self.assertEqual(second.readiness.state, "ready")
        self.assertEqual(second.readiness.target.base_sha, LANDED_FIRST_SHA)

    def test_recorded_rolling_base_cannot_hide_failed_candidate_checks_or_tree_drift(self) -> None:
        failed_checks = self._evaluate(after_first_landing=True, conclusion="failure")
        drifted_tree = self._evaluate(after_first_landing=True, base_tree="c" * 40)
        self.assertEqual(failed_checks.readiness.technical_checks.state, "blocked_checks")
        self.assertNotEqual(drifted_tree.structural_result.status, "recorded_rolling")
        self.assertNotEqual(drifted_tree.readiness.state, "ready")
