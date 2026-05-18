from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchLandingPlanRecord,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
)


MergeTrainControllerAction = Literal[
    "idle",
    "build_candidate",
    "observe_candidate",
    "candidate_failed",
    "candidate_stopped",
    "plan_landing",
    "land_batch",
    "batch_landed",
    "admit_collapsed_root",
    "execute_stack_collapse",
]


class MergeTrainControllerDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    action: MergeTrainControllerAction
    reason: str
    candidate_record_id: str = ""
    landing_plan_record_id: str = ""
    stack_collapse_plan_record_id: str = ""


def decide_merge_train_controller_record_action(
    *,
    candidate_records: tuple[MergeTrainBatchCandidateRecord, ...],
    landing_plan_records: tuple[MergeTrainBatchLandingPlanRecord, ...],
    stack_collapse_plan_records: tuple[MergeTrainStackCollapsePlanRecord, ...],
) -> MergeTrainControllerDecision:
    active_landing_record = latest_merge_train_batch_landing_plan_record(
        landing_plan_records
    )
    if active_landing_record is not None:
        waiting_collapse_record = latest_merge_train_stack_collapse_plan_record(
            stack_collapse_plan_records,
            plan_status="waiting_for_root_checks",
        )
        return MergeTrainControllerDecision(
            action="land_batch",
            reason="active landing plan has planned entries",
            landing_plan_record_id=active_landing_record.record_id,
            stack_collapse_plan_record_id=""
            if waiting_collapse_record is None
            else waiting_collapse_record.record_id,
        )

    active_candidate_record = latest_merge_train_batch_candidate_record(candidate_records)
    if active_candidate_record is not None:
        if active_candidate_record.candidate.status == "failed":
            return MergeTrainControllerDecision(
                action="candidate_failed",
                reason="latest active candidate failed",
                candidate_record_id=active_candidate_record.record_id,
            )
        if active_candidate_record.candidate.status in {"stale", "blocked"}:
            return MergeTrainControllerDecision(
                action="candidate_stopped",
                reason=f"latest active candidate is {active_candidate_record.candidate.status}",
                candidate_record_id=active_candidate_record.record_id,
            )
        if active_candidate_record.candidate.status in {"planned", "building"}:
            return MergeTrainControllerDecision(
                action="build_candidate",
                reason="candidate branch needs build/update",
                candidate_record_id=active_candidate_record.record_id,
            )
        return MergeTrainControllerDecision(
            action="observe_candidate",
            reason="candidate branch is ready for check observation",
            candidate_record_id=active_candidate_record.record_id,
        )

    passed_candidate_record = latest_passed_merge_train_batch_candidate_record(
        candidate_records
    )
    if passed_candidate_record is not None:
        completed_landing_record = latest_completed_merge_train_batch_landing_plan_record(
            landing_plan_records=landing_plan_records,
            batch_id=passed_candidate_record.candidate.batch_id,
            candidate_sha=passed_candidate_record.candidate.candidate_sha,
        )
        if completed_landing_record is not None:
            return MergeTrainControllerDecision(
                action="batch_landed",
                reason="passed candidate already has a completed landing plan",
                candidate_record_id=passed_candidate_record.record_id,
                landing_plan_record_id=completed_landing_record.record_id,
            )
        return MergeTrainControllerDecision(
            action="plan_landing",
            reason="latest passed candidate needs a landing plan",
            candidate_record_id=passed_candidate_record.record_id,
        )

    waiting_collapse_record = latest_merge_train_stack_collapse_plan_record(
        stack_collapse_plan_records,
        plan_status="waiting_for_root_checks",
    )
    if waiting_collapse_record is not None:
        return MergeTrainControllerDecision(
            action="admit_collapsed_root",
            reason="collapsed stack root is waiting for root checks",
            stack_collapse_plan_record_id=waiting_collapse_record.record_id,
        )

    planned_collapse_record = latest_merge_train_stack_collapse_plan_record(
        stack_collapse_plan_records,
        plan_status="planned",
    )
    if planned_collapse_record is not None:
        return MergeTrainControllerDecision(
            action="execute_stack_collapse",
            reason="stack collapse plan is ready to execute",
            stack_collapse_plan_record_id=planned_collapse_record.record_id,
        )

    return MergeTrainControllerDecision(
        action="idle",
        reason="no active merge train records require controller action",
    )


def latest_merge_train_batch_candidate_record(
    records: tuple[MergeTrainBatchCandidateRecord, ...],
) -> MergeTrainBatchCandidateRecord | None:
    latest_record = latest_merge_train_batch_candidate_progress_record(records)
    if latest_record is None:
        return None
    if latest_record.candidate.status == "passed":
        return None
    return latest_record


def latest_passed_merge_train_batch_candidate_record(
    records: tuple[MergeTrainBatchCandidateRecord, ...],
) -> MergeTrainBatchCandidateRecord | None:
    latest_record = latest_merge_train_batch_candidate_progress_record(records)
    if latest_record is None:
        return None
    if latest_record.candidate.status != "passed":
        return None
    return latest_record


def latest_merge_train_batch_landing_plan_record(
    records: tuple[MergeTrainBatchLandingPlanRecord, ...],
) -> MergeTrainBatchLandingPlanRecord | None:
    latest_record = latest_merge_train_batch_landing_progress_record(records)
    if latest_record is None:
        return None
    if not any(entry.status == "planned" for entry in latest_record.landing_plan.entries):
        return None
    return latest_record


def latest_completed_merge_train_batch_landing_plan_record(
    *,
    landing_plan_records: tuple[MergeTrainBatchLandingPlanRecord, ...],
    batch_id: str,
    candidate_sha: str,
) -> MergeTrainBatchLandingPlanRecord | None:
    matching_records = tuple(
        record
        for record in landing_plan_records
        if record.landing_plan.batch_id == batch_id
        and record.landing_plan.candidate_sha == candidate_sha
    )
    latest_record = latest_merge_train_batch_landing_progress_record(matching_records)
    if latest_record is None:
        return None
    if not latest_record.landing_plan.entries:
        return None
    if any(entry.status != "merged" for entry in latest_record.landing_plan.entries):
        return None
    return latest_record


def latest_merge_train_stack_collapse_plan_record(
    records: tuple[MergeTrainStackCollapsePlanRecord, ...],
    *,
    plan_status: str,
) -> MergeTrainStackCollapsePlanRecord | None:
    latest_record = latest_merge_train_stack_collapse_progress_record(records)
    if latest_record is None:
        return None
    if latest_record.plan.status != plan_status:
        return None
    return latest_record


def latest_merge_train_batch_candidate_progress_record(
    records: tuple[MergeTrainBatchCandidateRecord, ...],
) -> MergeTrainBatchCandidateRecord | None:
    if not records:
        return None
    status_rank = {
        "planned": 0,
        "building": 1,
        "ready_for_checks": 2,
        "passed": 3,
        "failed": 4,
        "stale": 4,
        "blocked": 4,
    }
    return max(
        records,
        key=lambda record: (
            record.updated_at,
            status_rank[record.candidate.status],
            record.record_id,
        ),
    )


def latest_merge_train_batch_landing_progress_record(
    records: tuple[MergeTrainBatchLandingPlanRecord, ...],
) -> MergeTrainBatchLandingPlanRecord | None:
    if not records:
        return None
    return max(
        records,
        key=lambda record: (
            record.updated_at,
            max(
                merge_train_batch_landing_entry_rank(entry.status)
                for entry in record.landing_plan.entries
            ),
            record.record_id,
        ),
    )


def latest_merge_train_stack_collapse_progress_record(
    records: tuple[MergeTrainStackCollapsePlanRecord, ...],
) -> MergeTrainStackCollapsePlanRecord | None:
    if not records:
        return None
    status_rank = {
        "planned": 0,
        "collapsing": 1,
        "waiting_for_root_checks": 2,
        "ready_for_train": 3,
        "blocked": 4,
        "stale": 4,
    }
    return max(
        records,
        key=lambda record: (record.updated_at, status_rank[record.plan.status], record.record_id),
    )


def merge_train_batch_landing_entry_rank(status: str) -> int:
    return {
        "planned": 0,
        "merging": 1,
        "merged": 2,
        "blocked": 3,
        "stale": 3,
        "skipped": 3,
    }[status]
