from __future__ import annotations

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidateRecord
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingPlanRecord
from control_plane.contracts.merge_train_admission import MergeTrainAdmissionDecision
from control_plane.contracts.merge_train_admission import (
    build_merge_train_controller_admission_decision,
)
from control_plane.contracts.merge_train_admission import evaluate_merge_train_admission
from control_plane.contracts.merge_train_run_record import MergeTrainRunRecord
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
)


class MergeTrainControllerRecords(BaseModel):
    model_config = ConfigDict(extra="forbid", arbitrary_types_allowed=True)

    candidate_records: tuple[MergeTrainBatchCandidateRecord, ...]
    landing_plan_records: tuple[MergeTrainBatchLandingPlanRecord, ...]
    stack_collapse_plan_records: tuple[MergeTrainStackCollapsePlanRecord, ...]


class MergeTrainControllerRecordSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    record_id: str
    record_type: str
    status: str
    updated_at: str
    batch_id: str = ""
    pull_request_numbers: tuple[int, ...] = ()
    candidate_sha: str = ""
    required_checks_status: str = ""
    planned_count: int = 0
    merged_count: int = 0
    blocked_count: int = 0
    stale_count: int = 0
    skipped_count: int = 0


class MergeTrainControllerStatusReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    repository: str
    base_branch: str
    generated_at: str
    admission: MergeTrainAdmissionDecision
    latest_run: MergeTrainRunRecord | None = None
    controller_records: tuple[MergeTrainControllerRecordSummary, ...]


class MergeTrainRunHistoryStore(Protocol):
    def latest_merge_train_run_record(
        self, *, repository: str, base_branch: str
    ) -> MergeTrainRunRecord | None: ...

    def list_merge_train_batch_candidate_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchCandidateRecord, ...]: ...

    def list_merge_train_batch_landing_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchLandingPlanRecord, ...]: ...

    def list_merge_train_stack_collapse_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainStackCollapsePlanRecord, ...]: ...


def evaluate_merge_train_admission_from_store(
    *,
    store: MergeTrainRunHistoryStore,
    repository: str,
    base_branch: str,
    requested_at: str,
    poll_interval_seconds: int = 60,
    backoff_seconds: int = 300,
) -> MergeTrainAdmissionDecision:
    controller_records = _list_active_controller_records(
        store=store, repository=repository, base_branch=base_branch
    )
    controller_decision = build_merge_train_controller_admission_decision(
        candidate_records=controller_records.candidate_records,
        landing_plan_records=controller_records.landing_plan_records,
        stack_collapse_plan_records=controller_records.stack_collapse_plan_records,
    )
    return evaluate_merge_train_admission(
        repository=repository,
        base_branch=base_branch,
        requested_at=requested_at,
        latest_run=store.latest_merge_train_run_record(
            repository=repository,
            base_branch=base_branch,
        ),
        controller_decision=controller_decision,
        poll_interval_seconds=poll_interval_seconds,
        backoff_seconds=backoff_seconds,
    )


def build_merge_train_controller_status_read_model(
    *,
    store: MergeTrainRunHistoryStore,
    repository: str,
    base_branch: str,
    generated_at: str,
    poll_interval_seconds: int = 60,
    backoff_seconds: int = 300,
) -> MergeTrainControllerStatusReadModel:
    controller_records = _list_active_controller_records(
        store=store, repository=repository, base_branch=base_branch
    )
    controller_decision = build_merge_train_controller_admission_decision(
        candidate_records=controller_records.candidate_records,
        landing_plan_records=controller_records.landing_plan_records,
        stack_collapse_plan_records=controller_records.stack_collapse_plan_records,
    )
    latest_run = store.latest_merge_train_run_record(
        repository=repository,
        base_branch=base_branch,
    )
    admission = evaluate_merge_train_admission(
        repository=repository,
        base_branch=base_branch,
        requested_at=generated_at,
        latest_run=latest_run,
        controller_decision=controller_decision,
        poll_interval_seconds=poll_interval_seconds,
        backoff_seconds=backoff_seconds,
    )
    return MergeTrainControllerStatusReadModel(
        repository=repository,
        base_branch=base_branch,
        generated_at=generated_at,
        admission=admission,
        latest_run=latest_run,
        controller_records=_summarize_controller_records(
            controller_records=controller_records,
        ),
    )


def _list_active_controller_records(
    *, store: MergeTrainRunHistoryStore, repository: str, base_branch: str
) -> MergeTrainControllerRecords:
    return MergeTrainControllerRecords(
        candidate_records=store.list_merge_train_batch_candidate_records(
            repository=repository,
            base_branch=base_branch,
            status="active",
            limit=25,
        ),
        landing_plan_records=store.list_merge_train_batch_landing_plan_records(
            repository=repository,
            base_branch=base_branch,
            status="active",
            limit=25,
        ),
        stack_collapse_plan_records=store.list_merge_train_stack_collapse_plan_records(
            repository=repository,
            base_branch=base_branch,
            status="active",
            limit=25,
        ),
    )


def _summarize_controller_records(
    *, controller_records: MergeTrainControllerRecords
) -> tuple[MergeTrainControllerRecordSummary, ...]:
    summaries = [
        *(_candidate_summary(record) for record in controller_records.candidate_records),
        *(_landing_plan_summary(record) for record in controller_records.landing_plan_records),
        *(
            _stack_collapse_summary(record)
            for record in controller_records.stack_collapse_plan_records
        ),
    ]
    return tuple(
        sorted(summaries, key=lambda summary: (summary.updated_at, summary.record_id), reverse=True)
    )


def _candidate_summary(
    record: MergeTrainBatchCandidateRecord,
) -> MergeTrainControllerRecordSummary:
    candidate = record.candidate
    return MergeTrainControllerRecordSummary(
        record_id=record.record_id,
        record_type="batch_candidate",
        status=candidate.status,
        updated_at=record.updated_at,
        batch_id=candidate.batch_id,
        pull_request_numbers=tuple(entry.pull_request_number for entry in candidate.entries),
        candidate_sha=candidate.candidate_sha,
        required_checks_status=candidate.required_checks_status,
    )


def _landing_plan_summary(
    record: MergeTrainBatchLandingPlanRecord,
) -> MergeTrainControllerRecordSummary:
    entries = record.landing_plan.entries
    return MergeTrainControllerRecordSummary(
        record_id=record.record_id,
        record_type="batch_landing_plan",
        status=_dominant_landing_status(record),
        updated_at=record.updated_at,
        batch_id=record.landing_plan.batch_id,
        pull_request_numbers=tuple(entry.pull_request_number for entry in entries),
        candidate_sha=record.landing_plan.candidate_sha,
        planned_count=sum(1 for entry in entries if entry.status == "planned"),
        merged_count=sum(1 for entry in entries if entry.status == "merged"),
        blocked_count=sum(1 for entry in entries if entry.status == "blocked"),
        stale_count=sum(1 for entry in entries if entry.status == "stale"),
        skipped_count=sum(1 for entry in entries if entry.status == "skipped"),
    )


def _stack_collapse_summary(
    record: MergeTrainStackCollapsePlanRecord,
) -> MergeTrainControllerRecordSummary:
    plan = record.plan
    return MergeTrainControllerRecordSummary(
        record_id=record.record_id,
        record_type="stack_collapse_plan",
        status=plan.status,
        updated_at=record.updated_at,
        pull_request_numbers=tuple(entry.pull_request_number for entry in plan.entries),
        planned_count=sum(1 for mutation in plan.mutations if mutation.status == "planned"),
        merged_count=sum(1 for mutation in plan.mutations if mutation.status == "mutated"),
        blocked_count=sum(1 for mutation in plan.mutations if mutation.status == "blocked"),
        stale_count=sum(1 for mutation in plan.mutations if mutation.status == "stale"),
    )


def _dominant_landing_status(record: MergeTrainBatchLandingPlanRecord) -> str:
    statuses = {entry.status for entry in record.landing_plan.entries}
    if not statuses:
        return "empty"
    for status in ("blocked", "stale", "merging", "planned", "skipped"):
        if status in statuses:
            return status
    return "merged"
