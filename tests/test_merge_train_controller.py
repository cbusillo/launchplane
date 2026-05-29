import unittest

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_candidate_ref,
    build_merge_train_batch_id,
    build_merge_train_batch_landing_plan,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapseEntry,
    MergeTrainStackCollapseMutation,
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
    build_merge_train_stack_collapse_id,
)
from control_plane.workflows.merge_train_controller import (
    MergeTrainControllerDecision,
    decide_merge_train_controller_record_action,
)


class MergeTrainControllerDecisionTests(unittest.TestCase):
    def test_decision_accepts_service_admit_collapsed_root_action(self) -> None:
        decision = MergeTrainControllerDecision(
            action="admit_collapsed_root",
            reason="collapsed root is ready for batch admission",
            stack_collapse_plan_record_id="stack-ready",
        )

        self.assertEqual(decision.action, "admit_collapsed_root")
        self.assertEqual(decision.model_dump(mode="json")["action"], "admit_collapsed_root")

    def test_decision_is_idle_without_existing_records(self) -> None:
        decision = decide_merge_train_controller_record_action(
            candidate_records=(), landing_plan_records=(), stack_collapse_plan_records=()
        )

        self.assertEqual(decision.action, "idle")

    def test_decision_waits_on_pending_candidate(self) -> None:
        planned_record = _candidate_record(status="planned")
        building_record = _candidate_record(
            status="building",
            record_id="candidate-building",
            updated_at="2026-05-18T01:01:00Z",
        )

        decision = decide_merge_train_controller_record_action(
            candidate_records=(planned_record, building_record),
            landing_plan_records=(),
            stack_collapse_plan_records=(),
        )

        self.assertEqual(decision.action, "build_candidate")
        self.assertEqual(decision.candidate_record_id, "candidate-building")

    def test_decision_observes_candidate_waiting_for_checks(self) -> None:
        decision = decide_merge_train_controller_record_action(
            candidate_records=(_candidate_record(status="ready_for_checks"),),
            landing_plan_records=(),
            stack_collapse_plan_records=(),
        )

        self.assertEqual(decision.action, "observe_candidate")

    def test_decision_handles_failed_and_terminal_candidates(self) -> None:
        failed_decision = decide_merge_train_controller_record_action(
            candidate_records=(_candidate_record(status="failed"),),
            landing_plan_records=(),
            stack_collapse_plan_records=(),
        )
        stale_decision = decide_merge_train_controller_record_action(
            candidate_records=(_candidate_record(status="stale"),),
            landing_plan_records=(),
            stack_collapse_plan_records=(),
        )

        self.assertEqual(failed_decision.action, "candidate_failed")
        self.assertEqual(stale_decision.action, "candidate_stopped")

    def test_decision_stops_for_newest_terminal_candidate_over_older_passed_candidate(
        self,
    ) -> None:
        passed_record = _candidate_record(
            status="passed",
            record_id="candidate-passed",
            candidate_sha="candidate-sha",
        )
        stale_record = _candidate_record(
            status="stale",
            record_id="candidate-stale",
            updated_at="2026-05-18T01:01:00Z",
            candidate_sha="candidate-sha-stale",
        )
        blocked_record = _candidate_record(
            status="blocked",
            record_id="candidate-blocked",
            updated_at="2026-05-18T01:02:00Z",
            candidate_sha="candidate-sha-blocked",
        )

        stale_decision = decide_merge_train_controller_record_action(
            candidate_records=(passed_record, stale_record),
            landing_plan_records=(),
            stack_collapse_plan_records=(),
        )
        blocked_decision = decide_merge_train_controller_record_action(
            candidate_records=(passed_record, blocked_record),
            landing_plan_records=(),
            stack_collapse_plan_records=(),
        )

        self.assertEqual(stale_decision.action, "candidate_stopped")
        self.assertEqual(stale_decision.candidate_record_id, stale_record.record_id)
        self.assertEqual(blocked_decision.action, "candidate_stopped")
        self.assertEqual(blocked_decision.candidate_record_id, blocked_record.record_id)

    def test_decision_plans_landing_for_passed_candidate(self) -> None:
        passed_record = _candidate_record(status="passed", candidate_sha="candidate-sha")

        decision = decide_merge_train_controller_record_action(
            candidate_records=(passed_record,),
            landing_plan_records=(),
            stack_collapse_plan_records=(),
        )

        self.assertEqual(decision.action, "plan_landing")
        self.assertEqual(decision.candidate_record_id, passed_record.record_id)

    def test_decision_lands_active_landing_plan_before_candidate(self) -> None:
        passed_record = _candidate_record(status="passed", candidate_sha="candidate-sha")
        landing_record = _landing_plan_record(candidate=passed_record.candidate)

        decision = decide_merge_train_controller_record_action(
            candidate_records=(passed_record,),
            landing_plan_records=(landing_record,),
            stack_collapse_plan_records=(),
        )

        self.assertEqual(decision.action, "land_batch")
        self.assertEqual(decision.landing_plan_record_id, landing_record.record_id)

    def test_decision_ignores_terminal_landed_batch(self) -> None:
        passed_record = _candidate_record(status="passed", candidate_sha="candidate-sha")
        merged_landing_record = _landing_plan_record(
            candidate=passed_record.candidate,
            record_id="landing-merged",
            entry_status="merged",
            updated_at="2026-05-18T01:02:00Z",
        )

        decision = decide_merge_train_controller_record_action(
            candidate_records=(passed_record,),
            landing_plan_records=(merged_landing_record,),
            stack_collapse_plan_records=(),
        )

        self.assertEqual(decision.action, "idle")
        self.assertEqual(decision.landing_plan_record_id, "")

    def test_decision_treats_stale_landing_plan_as_terminal_for_candidate(self) -> None:
        passed_record = _candidate_record(status="passed", candidate_sha="candidate-sha")
        stale_landing_record = _landing_plan_record(
            candidate=passed_record.candidate,
            record_id="landing-stale",
            entry_status="stale",
            updated_at="2026-05-18T01:02:00Z",
        )

        decision = decide_merge_train_controller_record_action(
            candidate_records=(passed_record,),
            landing_plan_records=(stale_landing_record,),
            stack_collapse_plan_records=(),
        )

        self.assertEqual(decision.action, "idle")
        self.assertEqual(decision.landing_plan_record_id, "")

    def test_decision_supports_stack_collapse_records(self) -> None:
        planned_record = _stack_collapse_record(status="planned")
        waiting_record = _stack_collapse_record(
            status="waiting_for_root_checks",
            record_id="stack-waiting",
            updated_at="2026-05-18T01:01:00Z",
        )

        planned_decision = decide_merge_train_controller_record_action(
            candidate_records=(),
            landing_plan_records=(),
            stack_collapse_plan_records=(planned_record,),
        )
        waiting_decision = decide_merge_train_controller_record_action(
            candidate_records=(),
            landing_plan_records=(),
            stack_collapse_plan_records=(planned_record, waiting_record),
        )

        self.assertEqual(planned_decision.action, "execute_stack_collapse")
        self.assertEqual(planned_decision.stack_collapse_plan_record_id, planned_record.record_id)
        self.assertEqual(waiting_decision.action, "wait_for_root_checks")
        self.assertEqual(waiting_decision.stack_collapse_plan_record_id, "stack-waiting")


def _candidate_record(
    *,
    status: str,
    record_id: str = "candidate-record",
    updated_at: str = "2026-05-18T01:00:00Z",
    candidate_sha: str = "",
) -> MergeTrainBatchCandidateRecord:
    entries = (_batch_entry(),)
    batch_id = build_merge_train_batch_id(
        repository="example/merge-train-repo",
        base_branch="main",
        base_sha="base-sha",
        entry_head_shas=tuple(entry.head_sha for entry in entries),
    )
    return MergeTrainBatchCandidateRecord(
        record_id=record_id,
        source="test",
        updated_at=updated_at,
        candidate=MergeTrainBatchCandidate(
            batch_id=batch_id,
            repository="example/merge-train-repo",
            base_branch="main",
            base_sha="base-sha",
            policy_key="example/merge-train-repo:main",
            policy_sha256="policy-sha",
            candidate_ref=build_merge_train_batch_candidate_ref(
                repository="example/merge-train-repo",
                base_branch="main",
                batch_id=batch_id,
            ),
            candidate_sha=candidate_sha,
            status=status,  # type: ignore[arg-type]
            entries=entries,
            required_checks_status="pass" if status == "passed" else "unknown",
            created_at="2026-05-18T00:59:00Z",
            updated_at=updated_at,
        ),
    )


def _landing_plan_record(
    *,
    candidate: MergeTrainBatchCandidate,
    record_id: str = "landing-record",
    entry_status: str = "planned",
    updated_at: str = "2026-05-18T01:01:00Z",
) -> MergeTrainBatchLandingPlanRecord:
    landing_plan = build_merge_train_batch_landing_plan(
        candidate=candidate,
        merge_method="squash",
        created_at=updated_at,
    )
    landing_plan = landing_plan.model_copy(
        update={
            "entries": tuple(
                entry.model_copy(update={"status": entry_status}) for entry in landing_plan.entries
            )
        }
    )
    return MergeTrainBatchLandingPlanRecord(
        record_id=record_id,
        source="test",
        updated_at=updated_at,
        landing_plan=landing_plan,
    )


def _stack_collapse_record(
    *,
    status: str,
    record_id: str = "stack-record",
    updated_at: str = "2026-05-18T01:00:00Z",
) -> MergeTrainStackCollapsePlanRecord:
    entries = (
        MergeTrainStackCollapseEntry(
            pull_request_number=1,
            position=1,
            head_sha="head-1",
            head_ref="feature/root",
            base_ref="main",
        ),
        MergeTrainStackCollapseEntry(
            pull_request_number=2,
            position=2,
            head_sha="head-2",
            head_ref="feature/child",
            base_ref="feature/root",
        ),
    )
    collapse_id = build_merge_train_stack_collapse_id(
        repository="example/merge-train-repo",
        base_branch="main",
        root_pull_request_number=1,
        entry_head_shas=tuple(entry.head_sha for entry in entries),
    )
    return MergeTrainStackCollapsePlanRecord(
        record_id=record_id,
        source="test",
        updated_at=updated_at,
        plan=MergeTrainStackCollapsePlan(
            collapse_id=collapse_id,
            repository="example/merge-train-repo",
            base_branch="main",
            root_pull_request_number=1,
            root_initial_head_sha="head-1",
            root_head_ref="feature/root",
            policy_key="example/merge-train-repo:main",
            policy_sha256="policy-sha",
            status=status,  # type: ignore[arg-type]
            entries=entries,
            mutations=(
                MergeTrainStackCollapseMutation(
                    child_pull_request_number=2,
                    parent_pull_request_number=1,
                    child_head_sha="head-2",
                    expected_parent_head_sha="head-1",
                    parent_head_ref="feature/root",
                ),
            ),
            created_at="2026-05-18T00:59:00Z",
            updated_at=updated_at,
        ),
    )


def _batch_entry() -> MergeTrainBatchEntry:
    return MergeTrainBatchEntry(
        pull_request_number=1,
        position=1,
        head_sha="head-1",
        title="PR 1",
        url="https://github.com/example/merge-train-repo/pull/1",
    )
