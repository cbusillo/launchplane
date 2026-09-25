from __future__ import annotations

from datetime import datetime, timezone
from typing import Callable, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidateRecord
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingEntry
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingPlan
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingPlanRecord
from control_plane.contracts.merge_train_controller_state import MergeTrainControllerStateRecord
from control_plane.contracts.merge_train_admission import MergeTrainAdmissionDecision
from control_plane.contracts.merge_admission_record import MergeAdmissionRecord
from control_plane.contracts.merge_admission_record import MergeLandingOutcomeRecord
from control_plane.contracts.merge_admission_record import (
    validate_merge_landing_outcome_for_admission,
)
from control_plane.contracts.merge_train_admission import (
    build_merge_train_controller_admission_decision,
)
from control_plane.contracts.merge_train_admission import evaluate_merge_train_admission
from control_plane.contracts.merge_train_run_record import MergeTrainRunRecord
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
)

MergeTrainControllerPolicyStatus = Literal["current", "stale", "unchecked"]
MergeTrainReconciliationClassification = Literal[
    "missing_preceding_admission",
    "admission_without_outcome",
    "outcome_reconcile_required",
    "outcome_rejected",
    "outcome_landed",
    "binding_unavailable",
    "binding_stale",
]
MergeTrainReconciliationBindingDetail = Literal[
    "",
    "current_policy_unavailable",
    "controller_policy_changed",
    "plan_reference_conflict",
    "plan_reference_incomplete",
    "plan_record_unavailable",
    "plan_binding_changed",
    "plan_entry_limit_exceeded",
    "selected_entry_unavailable",
    "history_reader_unavailable",
    "admission_limit_exceeded",
    "admission_binding_changed",
    "admission_identity_missing",
    "outcome_binding_changed",
    "history_unavailable",
    "outcome_status_unknown",
]
_LANDING_RECONCILIATION_PHASES = frozenset(
    {
        "merge_batch_entries",
        "admit_pull_request",
        "merge_pull_request",
        "landing_entry_merged",
        "retire_stale_policy_landing",
    }
)
_MAX_RECONCILIATION_DIAGNOSTICS = 25


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


class MergeTrainDryRunQueueEntrySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    pull_request_number: int
    title: str = ""
    url: str = ""
    eligible: bool = False
    ineligible_reasons: tuple[str, ...] = ()
    mergeable: str = ""
    required_checks_status: str = ""


class MergeTrainLatestDryRunSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intended_next_action: str
    next_action_detail: str
    queue_count: int
    eligible_count: int
    selected_pr_number: int | None = None
    queue_entries: tuple[MergeTrainDryRunQueueEntrySummary, ...] = ()


class MergeTrainControllerLeaseDiagnostics(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    owner: str = ""
    active_action: str = ""
    active_phase: str = ""
    active_record_id: str = ""
    active_pull_request_number: int | None = None
    lease_age_seconds: int | None = None
    heartbeat_age_seconds: int | None = None
    lease_expires_at: str = ""
    reconciliation_status: str
    reconciliation_detail: str = ""


class MergeTrainReconciliationDiagnostic(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = 1
    classification: MergeTrainReconciliationClassification
    binding_detail: MergeTrainReconciliationBindingDetail = ""
    repository: str
    base_branch: str
    landing_plan_record_id: str = ""
    landing_plan_id: str = ""
    pull_request_number: int | None = None
    expected_head_sha: str = ""
    expected_head_tree_sha: str = ""


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
    latest_dry_run: MergeTrainLatestDryRunSummary | None = None
    controller_state: MergeTrainControllerStateRecord | None = None
    controller_diagnostics: MergeTrainControllerLeaseDiagnostics | None = None
    controller_records: tuple[MergeTrainControllerRecordSummary, ...]
    reconciliation_diagnostics: tuple[MergeTrainReconciliationDiagnostic, ...] = ()


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

    def list_merge_train_controller_state_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainControllerStateRecord, ...]: ...


class MergeTrainReconciliationReadStore(Protocol):
    def list_merge_admission_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        pull_request_number: int | None = None,
        landing_plan_record_id: str = "",
        landing_plan_id: str = "",
        attempt_id: str = "",
        limit: int | None = None,
    ) -> tuple[MergeAdmissionRecord, ...]: ...

    def list_merge_landing_outcome_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        pull_request_number: int | None = None,
        admission_id: str = "",
        status: str = "",
        observation_sequence: int | None = None,
        limit: int | None = None,
    ) -> tuple[MergeLandingOutcomeRecord, ...]: ...


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
    controller_state = _latest_controller_state(
        store=store,
        repository=repository,
        base_branch=base_branch,
    )
    latest_run = store.latest_merge_train_run_record(
        repository=repository,
        base_branch=base_branch,
    )
    actionable_records = _filter_actionable_controller_records(
        controller_records=controller_records,
        latest_run=latest_run,
        current_policy_key=current_policy_key,
        current_policy_sha256=current_policy_sha256,
        controller_state=controller_state,
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
        latest_run=latest_run,
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
    controller_state = _latest_controller_state(
        store=store,
        repository=repository,
        base_branch=base_branch,
    )
    latest_run = store.latest_merge_train_run_record(
        repository=repository,
        base_branch=base_branch,
    )
    actionable_records = _filter_actionable_controller_records(
        controller_records=controller_records,
        latest_run=latest_run,
        current_policy_key=current_policy_key,
        current_policy_sha256=current_policy_sha256,
        controller_state=controller_state,
    )
    controller_decision = build_merge_train_controller_admission_decision(
        candidate_records=actionable_records.candidate_records,
        landing_plan_records=actionable_records.landing_plan_records,
        stack_collapse_plan_records=actionable_records.stack_collapse_plan_records,
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
        latest_dry_run=_summarize_latest_dry_run(latest_run),
        controller_state=controller_state,
        controller_diagnostics=_controller_lease_diagnostics(
            controller_state=controller_state,
            generated_at=generated_at,
        ),
        controller_records=_summarize_controller_records(
            controller_records=controller_records,
            current_policy_key=current_policy_key,
            current_policy_sha256=current_policy_sha256,
        ),
        reconciliation_diagnostics=_reconciliation_diagnostics(
            store=store,
            controller_state=controller_state,
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


def _reconciliation_diagnostics(
    *,
    store: object,
    controller_state: MergeTrainControllerStateRecord | None,
    controller_records: MergeTrainControllerRecords,
    current_policy_key: str,
    current_policy_sha256: str,
) -> tuple[MergeTrainReconciliationDiagnostic, ...]:
    if (
        controller_state is None
        or controller_state.status != "reconcile_required"
        or controller_state.active_action != "land_batch"
        or controller_state.active_phase not in _LANDING_RECONCILIATION_PHASES
    ):
        return ()
    if not current_policy_key or not current_policy_sha256:
        return (
            _diagnostic(
                controller_state=controller_state,
                classification="binding_unavailable",
                binding_detail="current_policy_unavailable",
            ),
        )
    if (
        controller_state.policy_key != current_policy_key
        or controller_state.policy_sha256 != current_policy_sha256
    ):
        return (
            _diagnostic(
                controller_state=controller_state,
                classification="binding_stale",
                binding_detail="controller_policy_changed",
            ),
        )

    payload = controller_state.step_payload
    payload_record_id = _string_value(payload, "landing_plan_record_id")
    if (
        controller_state.active_record_id
        and payload_record_id
        and controller_state.active_record_id != payload_record_id
    ):
        return (
            _diagnostic(
                controller_state=controller_state,
                classification="binding_stale",
                binding_detail="plan_reference_conflict",
            ),
        )
    record_id = controller_state.active_record_id or payload_record_id
    plan_id = _string_value(payload, "landing_plan_id")
    expected_effect = _string_value(payload, "expected_effect_sha")
    if not record_id or not plan_id or not expected_effect:
        return (
            _diagnostic(
                controller_state=controller_state,
                classification="binding_unavailable",
                binding_detail="plan_reference_incomplete",
            ),
        )
    landing_record = next(
        (
            record
            for record in controller_records.landing_plan_records
            if record.record_id == record_id
        ),
        None,
    )
    if landing_record is None:
        return (
            _diagnostic(
                controller_state=controller_state,
                classification="binding_unavailable",
                binding_detail="plan_record_unavailable",
            ),
        )
    plan = landing_record.landing_plan
    if (
        plan.plan_id != plan_id
        or plan.candidate_sha != expected_effect
        or plan.repository != controller_state.repository.strip().lower()
        or plan.base_branch != controller_state.base_branch
        or plan.policy_key != current_policy_key
        or plan.policy_sha256 != current_policy_sha256
    ):
        return (
            _diagnostic(
                controller_state=controller_state,
                classification="binding_stale",
                binding_detail="plan_binding_changed",
            ),
        )

    if len(plan.entries) > _MAX_RECONCILIATION_DIAGNOSTICS:
        return (
            _diagnostic(
                controller_state=controller_state,
                classification="binding_unavailable",
                binding_detail="plan_entry_limit_exceeded",
            ),
        )
    entries = _diagnostic_entries(plan=plan, controller_state=controller_state)
    if not entries:
        if controller_state.active_pull_request_number is None:
            return ()
        return (
            _diagnostic(
                controller_state=controller_state,
                classification="binding_unavailable",
                binding_detail="selected_entry_unavailable",
            ),
        )
    readers = _reconciliation_readers(store)
    if readers is None:
        return (
            _diagnostic(
                controller_state=controller_state,
                classification="binding_unavailable",
                binding_detail="history_reader_unavailable",
            ),
        )
    admission_reader, outcome_reader = readers
    return tuple(
        _entry_reconciliation_diagnostic(
            admission_reader=admission_reader,
            outcome_reader=outcome_reader,
            landing_plan_record_id=record_id,
            plan=plan,
            entry=entry,
        )
        for entry in entries
    )


def _reconciliation_readers(
    store: object,
) -> (
    tuple[
        Callable[..., tuple[MergeAdmissionRecord, ...]],
        Callable[..., tuple[MergeLandingOutcomeRecord, ...]],
    ]
    | None
):
    try:
        reconciliation_store = cast(MergeTrainReconciliationReadStore, store)
        return (
            reconciliation_store.list_merge_admission_records,
            reconciliation_store.list_merge_landing_outcome_records,
        )
    except AttributeError:
        return None


def _diagnostic_entries(
    *, plan: MergeTrainBatchLandingPlan, controller_state: MergeTrainControllerStateRecord
) -> tuple[MergeTrainBatchLandingEntry, ...]:
    if controller_state.active_pull_request_number is None:
        return tuple(entry for entry in plan.entries if entry.status in {"planned", "merging"})
    return tuple(
        entry
        for entry in plan.entries
        if entry.pull_request_number == controller_state.active_pull_request_number
    )


def _entry_reconciliation_diagnostic(
    *,
    admission_reader: Callable[..., tuple[MergeAdmissionRecord, ...]],
    outcome_reader: Callable[..., tuple[MergeLandingOutcomeRecord, ...]],
    landing_plan_record_id: str,
    plan: MergeTrainBatchLandingPlan,
    entry: MergeTrainBatchLandingEntry,
) -> MergeTrainReconciliationDiagnostic:
    def diagnostic(
        classification: MergeTrainReconciliationClassification,
        binding_detail: MergeTrainReconciliationBindingDetail = "",
    ) -> MergeTrainReconciliationDiagnostic:
        return _diagnostic(
            repository=plan.repository,
            base_branch=plan.base_branch,
            landing_plan_record_id=landing_plan_record_id,
            landing_plan_id=plan.plan_id,
            entry=entry,
            classification=classification,
            binding_detail=binding_detail,
        )

    try:
        admissions = tuple(
            admission_reader(
                repository=plan.repository,
                base_branch=plan.base_branch,
                pull_request_number=entry.pull_request_number,
                landing_plan_id=plan.plan_id,
                limit=_MAX_RECONCILIATION_DIAGNOSTICS + 1,
            )
        )
        if not admissions:
            return diagnostic("missing_preceding_admission")
        if len(admissions) > _MAX_RECONCILIATION_DIAGNOSTICS:
            return diagnostic("binding_unavailable", "admission_limit_exceeded")
        admission = admissions[0]
        if not _admission_matches_entry(admission=admission, plan=plan, entry=entry):
            return diagnostic("binding_stale", "admission_binding_changed")
        if not admission.admission_id:
            return diagnostic("binding_unavailable", "admission_identity_missing")
        outcomes = tuple(outcome_reader(admission_id=admission.admission_id, limit=1))
        if not outcomes:
            return diagnostic("admission_without_outcome")
        outcome = outcomes[0]
        try:
            validate_merge_landing_outcome_for_admission(
                admission=admission,
                outcome=outcome,
            )
        except ValueError:
            return diagnostic("binding_stale", "outcome_binding_changed")
    except (AttributeError, LookupError, TypeError, ValueError):
        return diagnostic("binding_unavailable", "history_unavailable")
    classification = {
        "reconcile_required": "outcome_reconcile_required",
        "rejected": "outcome_rejected",
        "landed": "outcome_landed",
    }.get(outcome.status)
    if classification is None:
        return diagnostic("binding_unavailable", "outcome_status_unknown")
    return diagnostic(cast(MergeTrainReconciliationClassification, classification))


def _admission_matches_entry(
    *,
    admission: MergeAdmissionRecord,
    plan: MergeTrainBatchLandingPlan,
    entry: MergeTrainBatchLandingEntry,
) -> bool:
    return (
        admission.repository == plan.repository
        and admission.base_branch == plan.base_branch
        and admission.pull_request_number == entry.pull_request_number
        and admission.landing_plan_id == plan.plan_id
        and admission.batch_id == plan.batch_id
        and admission.candidate_sha == plan.candidate_sha
        and admission.expected_effect_sha == plan.candidate_sha
        and admission.pull_request_head_sha == entry.expected_head_sha
        and (
            not entry.expected_head_tree_sha
            or admission.pull_request_head_tree_sha == entry.expected_head_tree_sha
        )
    )


def _diagnostic(
    *,
    classification: MergeTrainReconciliationClassification,
    binding_detail: MergeTrainReconciliationBindingDetail = "",
    repository: str = "",
    base_branch: str = "",
    landing_plan_record_id: str = "",
    landing_plan_id: str = "",
    entry: MergeTrainBatchLandingEntry | None = None,
    controller_state: MergeTrainControllerStateRecord | None = None,
) -> MergeTrainReconciliationDiagnostic:
    if controller_state is not None:
        repository = controller_state.repository
        base_branch = controller_state.base_branch
        payload = controller_state.step_payload
        landing_plan_record_id = controller_state.active_record_id or _string_value(
            payload, "landing_plan_record_id"
        )
        landing_plan_id = _string_value(payload, "landing_plan_id")
    values: dict[str, object] = {
        "classification": classification,
        "binding_detail": binding_detail,
        "repository": repository,
        "base_branch": base_branch,
        "landing_plan_record_id": landing_plan_record_id,
        "landing_plan_id": landing_plan_id,
    }
    if entry is not None:
        values.update(
            pull_request_number=entry.pull_request_number,
            expected_head_sha=entry.expected_head_sha,
            expected_head_tree_sha=entry.expected_head_tree_sha,
        )
    return MergeTrainReconciliationDiagnostic.model_validate(values)


def _string_value(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _latest_controller_state(
    *, store: MergeTrainRunHistoryStore, repository: str, base_branch: str
) -> MergeTrainControllerStateRecord | None:
    records = store.list_merge_train_controller_state_records(
        repository=repository,
        base_branch=base_branch,
        limit=1,
    )
    return records[0] if records else None


def _controller_lease_diagnostics(
    *,
    controller_state: MergeTrainControllerStateRecord | None,
    generated_at: str,
) -> MergeTrainControllerLeaseDiagnostics | None:
    if controller_state is None:
        return None
    return MergeTrainControllerLeaseDiagnostics(
        status=controller_state.status,
        owner=controller_state.lease_owner or controller_state.last_owner,
        active_action=controller_state.active_action,
        active_phase=controller_state.active_phase,
        active_record_id=controller_state.active_record_id,
        active_pull_request_number=controller_state.active_pull_request_number,
        lease_age_seconds=_timestamp_age_seconds(
            generated_at=generated_at,
            timestamp=controller_state.lease_acquired_at,
        ),
        heartbeat_age_seconds=_timestamp_age_seconds(
            generated_at=generated_at,
            timestamp=controller_state.heartbeat_at,
        ),
        lease_expires_at=controller_state.lease_expires_at,
        reconciliation_status=controller_state.reconciliation_status,
        reconciliation_detail=controller_state.reconciliation_detail,
    )


def _timestamp_age_seconds(*, generated_at: str, timestamp: str) -> int | None:
    if not timestamp:
        return None
    generated = _parse_timestamp(generated_at)
    observed = _parse_timestamp(timestamp)
    return max(0, int((generated - observed).total_seconds()))


def _parse_timestamp(value: str) -> datetime:
    normalized = value.strip().replace("Z", "+00:00")
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _summarize_latest_dry_run(
    latest_run: MergeTrainRunRecord | None,
) -> MergeTrainLatestDryRunSummary | None:
    if latest_run is None or latest_run.mode != "dry_run":
        return None
    dry_run_result = latest_run.dry_run_result
    queue_payload = dry_run_result.get("queue")
    queue_entries = _summarize_dry_run_queue(queue_payload)
    return MergeTrainLatestDryRunSummary(
        intended_next_action=_string_field(dry_run_result, "intended_next_action"),
        next_action_detail=_string_field(dry_run_result, "next_action_detail"),
        queue_count=len(queue_entries),
        eligible_count=sum(1 for entry in queue_entries if entry.eligible),
        selected_pr_number=_selected_pr_number(dry_run_result.get("selected_pr")),
        queue_entries=queue_entries,
    )


def _summarize_dry_run_queue(payload: object) -> tuple[MergeTrainDryRunQueueEntrySummary, ...]:
    if not isinstance(payload, list | tuple):
        return ()
    entries: list[MergeTrainDryRunQueueEntrySummary] = []
    for item in payload:
        if not isinstance(item, dict):
            continue
        entries.append(
            MergeTrainDryRunQueueEntrySummary(
                pull_request_number=_int_field(item, "number"),
                title=_string_field(item, "title"),
                url=_string_field(item, "url"),
                eligible=_bool_field(item, "eligible"),
                ineligible_reasons=_string_tuple_field(item, "ineligible_reasons"),
                mergeable=_string_field(item, "mergeable"),
                required_checks_status=_string_field(item, "required_checks_status"),
            )
        )
    return tuple(entries)


def _selected_pr_number(payload: object) -> int | None:
    if not isinstance(payload, dict):
        return None
    value = payload.get("number")
    return value if isinstance(value, int) else None


def _string_field(payload: dict[str, object], key: str) -> str:
    value = payload.get(key)
    return value.strip() if isinstance(value, str) else ""


def _bool_field(payload: dict[str, object], key: str) -> bool:
    value = payload.get(key)
    return value if isinstance(value, bool) else False


def _int_field(payload: dict[str, object], key: str) -> int:
    value = payload.get(key)
    return value if isinstance(value, int) else 0


def _string_tuple_field(payload: dict[str, object], key: str) -> tuple[str, ...]:
    value = payload.get(key)
    if not isinstance(value, list | tuple):
        return ()
    return tuple(item.strip() for item in value if isinstance(item, str) and item.strip())


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
    latest_run: MergeTrainRunRecord | None,
    current_policy_key: str,
    current_policy_sha256: str,
    controller_state: MergeTrainControllerStateRecord | None,
) -> MergeTrainControllerRecords:
    controller_records = _filter_ordinary_job_binding_records(
        controller_records=controller_records,
        controller_state=controller_state,
    )
    if _latest_idle_run_supersedes_controller_records(
        latest_run=latest_run,
        controller_records=controller_records,
    ):
        return MergeTrainControllerRecords(
            candidate_records=(),
            landing_plan_records=(),
            stack_collapse_plan_records=(),
        )
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


def _filter_ordinary_job_binding_records(
    *,
    controller_records: MergeTrainControllerRecords,
    controller_state: MergeTrainControllerStateRecord | None,
) -> MergeTrainControllerRecords:
    active_binding = (
        controller_state.ordinary_job_binding
        if controller_state is not None and controller_state.status == "running"
        else None
    )
    return MergeTrainControllerRecords(
        candidate_records=tuple(
            record
            for record in controller_records.candidate_records
            if record.ordinary_job_binding == active_binding
        ),
        landing_plan_records=tuple(
            record
            for record in controller_records.landing_plan_records
            if record.ordinary_job_binding == active_binding
        ),
        stack_collapse_plan_records=tuple(
            record
            for record in controller_records.stack_collapse_plan_records
            if record.ordinary_job_binding == active_binding
        ),
    )


def _latest_idle_run_supersedes_controller_records(
    *,
    latest_run: MergeTrainRunRecord | None,
    controller_records: MergeTrainControllerRecords,
) -> bool:
    if (
        latest_run is None
        or latest_run.status != "idle"
        or latest_run.intended_next_action != "idle"
    ):
        return False
    latest_record_update = _latest_controller_record_update(controller_records)
    if not latest_record_update:
        return False
    return latest_run.recorded_at >= latest_record_update


def _latest_controller_record_update(controller_records: MergeTrainControllerRecords) -> str:
    updated_at_values = (
        tuple(record.updated_at for record in controller_records.candidate_records)
        + tuple(record.updated_at for record in controller_records.landing_plan_records)
        + tuple(record.updated_at for record in controller_records.stack_collapse_plan_records)
    )
    if not updated_at_values:
        return ""
    return max(updated_at_values)


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
