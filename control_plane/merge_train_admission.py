from __future__ import annotations

from typing import Literal, Protocol

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

MergeTrainControllerPolicyStatus = Literal["current", "stale", "unchecked"]


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
    policy_key: str = ""
    policy_sha256: str = ""
    policy_status: MergeTrainControllerPolicyStatus = "unchecked"
    stale_reason: str = ""
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
    current_policy_key: str = ""
    current_policy_sha256: str = ""
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
    current_policy_key: str = "",
    current_policy_sha256: str = "",
    poll_interval_seconds: int = 60,
    backoff_seconds: int = 300,
) -> MergeTrainAdmissionDecision:
    controller_records = _list_active_controller_records(
        store=store, repository=repository, base_branch=base_branch
    )
    actionable_records = _filter_actionable_controller_records(
        controller_records=controller_records,
        current_policy_key=current_policy_key,
        current_policy_sha256=current_policy_sha256,
    )
    controller_decision = build_merge_train_controller_admission_decision(
        candidate_records=actionable_records.candidate_records,
        landing_plan_records=actionable_records.landing_plan_records,
        stack_collapse_plan_records=actionable_records.stack_collapse_plan_records,
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
    current_policy_key: str = "",
    current_policy_sha256: str = "",
    poll_interval_seconds: int = 60,
    backoff_seconds: int = 300,
) -> MergeTrainControllerStatusReadModel:
    controller_records = _list_active_controller_records(
        store=store, repository=repository, base_branch=base_branch
    )
    actionable_records = _filter_actionable_controller_records(
        controller_records=controller_records,
        current_policy_key=current_policy_key,
        current_policy_sha256=current_policy_sha256,
    )
    controller_decision = build_merge_train_controller_admission_decision(
        candidate_records=actionable_records.candidate_records,
        landing_plan_records=actionable_records.landing_plan_records,
        stack_collapse_plan_records=actionable_records.stack_collapse_plan_records,
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
        current_policy_key=current_policy_key,
        current_policy_sha256=current_policy_sha256,
        admission=admission,
        latest_run=latest_run,
        controller_records=_summarize_controller_records(
            controller_records=controller_records,
            current_policy_key=current_policy_key,
            current_policy_sha256=current_policy_sha256,
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
    *,
    controller_records: MergeTrainControllerRecords,
    current_policy_key: str = "",
    current_policy_sha256: str = "",
) -> tuple[MergeTrainControllerRecordSummary, ...]:
    summaries = [
        *(
            _candidate_summary(
                record,
                current_policy_key=current_policy_key,
                current_policy_sha256=current_policy_sha256,
            )
            for record in controller_records.candidate_records
        ),
        *(
            _landing_plan_summary(
                record,
                current_policy_key=current_policy_key,
                current_policy_sha256=current_policy_sha256,
            )
            for record in controller_records.landing_plan_records
        ),
        *(
            _stack_collapse_summary(
                record,
                current_policy_key=current_policy_key,
                current_policy_sha256=current_policy_sha256,
            )
            for record in controller_records.stack_collapse_plan_records
        ),
    ]
    return tuple(
        sorted(summaries, key=lambda summary: (summary.updated_at, summary.record_id), reverse=True)
    )


def _candidate_summary(
    record: MergeTrainBatchCandidateRecord,
    *,
    current_policy_key: str = "",
    current_policy_sha256: str = "",
) -> MergeTrainControllerRecordSummary:
    candidate = record.candidate
    policy_status, stale_reason = _policy_status_fields(
        policy_key=candidate.policy_key,
        policy_sha256=candidate.policy_sha256,
        current_policy_key=current_policy_key,
        current_policy_sha256=current_policy_sha256,
    )
    return MergeTrainControllerRecordSummary(
        record_id=record.record_id,
        record_type="batch_candidate",
        status=candidate.status,
        updated_at=record.updated_at,
        policy_key=candidate.policy_key,
        policy_sha256=candidate.policy_sha256,
        policy_status=policy_status,
        stale_reason=stale_reason,
        batch_id=candidate.batch_id,
        pull_request_numbers=tuple(entry.pull_request_number for entry in candidate.entries),
        candidate_sha=candidate.candidate_sha,
        required_checks_status=candidate.required_checks_status,
    )


def _landing_plan_summary(
    record: MergeTrainBatchLandingPlanRecord,
    *,
    current_policy_key: str = "",
    current_policy_sha256: str = "",
) -> MergeTrainControllerRecordSummary:
    entries = record.landing_plan.entries
    plan = record.landing_plan
    policy_status, stale_reason = _policy_status_fields(
        policy_key=plan.policy_key,
        policy_sha256=plan.policy_sha256,
        current_policy_key=current_policy_key,
        current_policy_sha256=current_policy_sha256,
    )
    return MergeTrainControllerRecordSummary(
        record_id=record.record_id,
        record_type="batch_landing_plan",
        status=_dominant_landing_status(record),
        updated_at=record.updated_at,
        policy_key=plan.policy_key,
        policy_sha256=plan.policy_sha256,
        policy_status=policy_status,
        stale_reason=stale_reason,
        batch_id=plan.batch_id,
        pull_request_numbers=tuple(entry.pull_request_number for entry in entries),
        candidate_sha=plan.candidate_sha,
        planned_count=sum(1 for entry in entries if entry.status == "planned"),
        merged_count=sum(1 for entry in entries if entry.status == "merged"),
        blocked_count=sum(1 for entry in entries if entry.status == "blocked"),
        stale_count=sum(1 for entry in entries if entry.status == "stale"),
        skipped_count=sum(1 for entry in entries if entry.status == "skipped"),
    )


def _stack_collapse_summary(
    record: MergeTrainStackCollapsePlanRecord,
    *,
    current_policy_key: str = "",
    current_policy_sha256: str = "",
) -> MergeTrainControllerRecordSummary:
    plan = record.plan
    policy_status, stale_reason = _policy_status_fields(
        policy_key=plan.policy_key,
        policy_sha256=plan.policy_sha256,
        current_policy_key=current_policy_key,
        current_policy_sha256=current_policy_sha256,
    )
    return MergeTrainControllerRecordSummary(
        record_id=record.record_id,
        record_type="stack_collapse_plan",
        status=plan.status,
        updated_at=record.updated_at,
        policy_key=plan.policy_key,
        policy_sha256=plan.policy_sha256,
        policy_status=policy_status,
        stale_reason=stale_reason,
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


def _filter_actionable_controller_records(
    *,
    controller_records: MergeTrainControllerRecords,
    current_policy_key: str,
    current_policy_sha256: str,
) -> MergeTrainControllerRecords:
    if not current_policy_key and not current_policy_sha256:
        return _filter_terminal_candidate_stop_records(controller_records)
    policy_current_records = MergeTrainControllerRecords(
        candidate_records=tuple(
            record
            for record in controller_records.candidate_records
            if _policy_is_current(
                policy_key=record.candidate.policy_key,
                policy_sha256=record.candidate.policy_sha256,
                current_policy_key=current_policy_key,
                current_policy_sha256=current_policy_sha256,
            )
        ),
        landing_plan_records=tuple(
            record
            for record in controller_records.landing_plan_records
            if _policy_is_current(
                policy_key=record.landing_plan.policy_key,
                policy_sha256=record.landing_plan.policy_sha256,
                current_policy_key=current_policy_key,
                current_policy_sha256=current_policy_sha256,
            )
        ),
        stack_collapse_plan_records=tuple(
            record
            for record in controller_records.stack_collapse_plan_records
            if _policy_is_current(
                policy_key=record.plan.policy_key,
                policy_sha256=record.plan.policy_sha256,
                current_policy_key=current_policy_key,
                current_policy_sha256=current_policy_sha256,
            )
        ),
    )
    return _filter_terminal_candidate_stop_records(policy_current_records)


def _filter_terminal_candidate_stop_records(
    controller_records: MergeTrainControllerRecords,
) -> MergeTrainControllerRecords:
    candidate_records = controller_records.candidate_records
    latest_candidate_record = _latest_candidate_progress_record(candidate_records)
    if latest_candidate_record is not None and latest_candidate_record.candidate.status in {
        "blocked",
        "stale",
    }:
        candidate_records = ()
    return MergeTrainControllerRecords(
        candidate_records=candidate_records,
        landing_plan_records=controller_records.landing_plan_records,
        stack_collapse_plan_records=controller_records.stack_collapse_plan_records,
    )


def _latest_candidate_progress_record(
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


def _policy_status_fields(
    *,
    policy_key: str,
    policy_sha256: str,
    current_policy_key: str,
    current_policy_sha256: str,
) -> tuple[MergeTrainControllerPolicyStatus, str]:
    if not current_policy_key and not current_policy_sha256:
        return "unchecked", ""
    if _policy_is_current(
        policy_key=policy_key,
        policy_sha256=policy_sha256,
        current_policy_key=current_policy_key,
        current_policy_sha256=current_policy_sha256,
    ):
        return "current", ""
    if policy_key != current_policy_key:
        return "stale", "policy_key_mismatch"
    return "stale", "policy_digest_mismatch"


def _policy_is_current(
    *,
    policy_key: str,
    policy_sha256: str,
    current_policy_key: str,
    current_policy_sha256: str,
) -> bool:
    return policy_key == current_policy_key and policy_sha256 == current_policy_sha256
