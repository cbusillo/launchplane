from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
import logging
from typing import Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_candidate,
    build_merge_train_batch_candidate_record,
    build_merge_train_batch_landing_plan,
    build_merge_train_batch_landing_plan_record,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerLeaseLostError,
    MergeTrainControllerReconciliationRequiredError,
    MergeTrainControllerStateRecord,
    build_merge_train_controller_state_record,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicy, MergeTrainRepositoryPolicy
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
    build_merge_train_stack_collapse_plan,
    build_merge_train_stack_collapse_plan_record,
    execute_merge_train_stack_collapse_plan,
    reconcile_merge_train_stack_children_after_root_landing,
)
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainStackCollapseRootProof,
)
from control_plane.merge_train import (
    MergeTrainDryRunResult,
    build_merge_train_dry_run_result,
    discover_merge_train_stack,
)
from control_plane.merge_admission import (
    GuardedMergeAdmission,
    MergeAdmissionDeniedError,
    MergeAdmissionEvaluator,
    MergeAdmissionRecordStore,
)
from control_plane.merge_train_batch_candidate import (
    MergeTrainBatchCandidateRecordStore,
    merge_train_snapshot_has_stack_topology,
)
from control_plane.merge_train_batch_landing import (
    MergeTrainBatchLandingPlanRecordStore,
    validate_stack_collapse_record_for_landing,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    GitHubMergeTrainSnapshotReader,
    MergeTrainGitHubError,
    MergeTrainGitHubStaleHeadError,
    MergeTrainGitHubTransport,
    UrllibMergeTrainGitHubTransport,
)
from control_plane.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecordStore,
    stack_collapse_expected_root_head_sha,
)
from control_plane.workflows.merge_train_controller import (
    latest_completed_merge_train_batch_landing_plan_record as latest_completed_merge_train_batch_landing_progress_record,
    latest_merge_train_batch_candidate_progress_record,
    latest_merge_train_batch_landing_progress_record,
    latest_merge_train_stack_collapse_progress_record,
)


_LOGGER = logging.getLogger(__name__)
DEFAULT_MERGE_TRAIN_CONTROLLER_LEASE_SECONDS = 300
MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION = "merge_train_controller_run_once"
MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS = (
    "admit_collapsed_root",
    "build_candidate",
    "execute_stack_collapse",
    "land_batch",
    MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
    "observe_candidate",
    "plan_candidate",
    "plan_landing",
    "plan_stack_collapse",
    "reflow_candidate",
)


class MergeTrainControllerRunOnceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str = "main"
    mutate: bool = False
    github_api_base_url: str = "https://api.github.com"

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainControllerRunOnceEnvelope":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        self.github_api_base_url = self.github_api_base_url.strip() or "https://api.github.com"
        if not self.repository:
            raise ValueError("merge train controller requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train controller requires base_branch")
        return self


class MergeTrainControllerRequestError(ValueError):
    pass


class MergeTrainControllerStateRecordStore(Protocol):
    def list_merge_train_controller_state_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainControllerStateRecord, ...]: ...

    def acquire_merge_train_controller_state_record(
        self,
        *,
        repository: str,
        base_branch: str,
        policy_key: str,
        policy_sha256: str,
        lease_owner: str,
        lease_seconds: int,
        initial_active_action: str,
        initial_active_phase: str,
        adoptable_active_actions: tuple[str, ...],
    ) -> MergeTrainControllerStateRecord: ...

    def compare_and_set_merge_train_controller_state_record(
        self,
        *,
        record: MergeTrainControllerStateRecord,
        expected_lease_owner: str,
        expected_lease_acquired_at: str,
        lease_seconds: int,
    ) -> MergeTrainControllerStateRecord: ...


@dataclass(frozen=True)
class MergeTrainControllerRunOnceResult:
    accepted_result: dict[str, object]
    records: dict[str, str]


@dataclass
class MergeTrainControllerLeaseContext:
    record: MergeTrainControllerStateRecord
    record_store: MergeTrainControllerStateRecordStore | None = None
    lease_seconds: int = DEFAULT_MERGE_TRAIN_CONTROLLER_LEASE_SECONDS

    @property
    def owner(self) -> str:
        return self.record.lease_owner

    @property
    def acquisition_token(self) -> str:
        return self.record.lease_acquired_at

    def checkpoint(self, **updates: object) -> MergeTrainControllerStateRecord:
        if self.record_store is None:
            raise RuntimeError("read-only merge train controller cannot checkpoint")
        self.record = update_merge_train_controller_state(
            record_store=self.record_store,
            current_record=self.record,
            lease_owner=self.owner,
            lease_acquired_at=self.acquisition_token,
            lease_seconds=self.lease_seconds,
            **updates,
        )
        return self.record

    def read_current(self) -> MergeTrainControllerStateRecord:
        if self.record_store is None:
            return self.record
        records = self.record_store.list_merge_train_controller_state_records(
            repository=self.record.repository,
            base_branch=self.record.base_branch,
            limit=1,
        )
        if not records:
            raise MergeTrainControllerLeaseLostError(
                "Persisted merge train controller state is missing."
            )
        return records[0]

    def release(
        self,
        *,
        reconciliation_status: str = "clean",
        reconciliation_detail: str = "",
        clear_active_state: bool = True,
    ) -> MergeTrainControllerStateRecord:
        if self.record_store is None:
            return self.record
        self.record = release_merge_train_controller_lease(
            record_store=self.record_store,
            current_record=self.record,
            lease_owner=self.owner,
            lease_acquired_at=self.acquisition_token,
            lease_seconds=self.lease_seconds,
            reconciliation_status=reconciliation_status,
            reconciliation_detail=reconciliation_detail,
            clear_active_state=clear_active_state,
        )
        return self.record


@contextmanager
def merge_train_controller_mutation_fence(
    *,
    record_store: MergeTrainControllerStateRecordStore,
    repository: str,
    base_branch: str,
    policy_key: str,
    policy_sha256: str,
    trace_id: str,
    active_action: str,
    active_phase: str,
    active_record_id: str = "",
) -> Iterator[MergeTrainControllerLeaseContext]:
    lease_owner = merge_train_controller_lease_owner(trace_id=trace_id)
    controller_state = record_store.acquire_merge_train_controller_state_record(
        repository=repository,
        base_branch=base_branch,
        policy_key=policy_key,
        policy_sha256=policy_sha256,
        lease_owner=lease_owner,
        lease_seconds=DEFAULT_MERGE_TRAIN_CONTROLLER_LEASE_SECONDS,
        initial_active_action=active_action,
        initial_active_phase=active_phase,
        adoptable_active_actions=(active_action,),
    )
    lease = MergeTrainControllerLeaseContext(
        record=controller_state,
        record_store=record_store,
    )
    if controller_state.reconciliation_status == "adopted":
        lease.release(
            reconciliation_status="required",
            reconciliation_detail=controller_state.reconciliation_detail,
            clear_active_state=False,
        )
        raise MergeTrainControllerReconciliationRequiredError(
            "merge train controller has durable work that must be resumed through the controller route"
        )
    try:
        lease.checkpoint(
            active_action=active_action,
            active_phase=active_phase,
            active_record_id=active_record_id,
            active_pull_request_number=None,
            step_payload={},
        )
        yield lease
    except MergeTrainControllerLeaseLostError:
        raise
    except Exception as error:
        try:
            lease.release(
                reconciliation_status="required",
                reconciliation_detail=_controller_exception_reconciliation_detail(error),
                clear_active_state=False,
            )
        except Exception as release_error:  # noqa: BLE001
            _LOGGER.warning(
                "Merge train mutation fence could not record failure state",
                extra={
                    "repository": repository,
                    "base_branch": base_branch,
                    "lease_owner": lease_owner,
                    "release_error_type": type(release_error).__name__,
                },
            )
        raise
    else:
        lease.release()


def execute_merge_train_controller_run_once(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    token: str,
    trace_id: str,
    recorded_at: str,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    controller_state_store: MergeTrainControllerStateRecordStore,
    admission_store: MergeAdmissionRecordStore,
    admission_evaluator: MergeAdmissionEvaluator,
    before_release: Callable[[MergeTrainControllerRunOnceResult], None] | None = None,
) -> MergeTrainControllerRunOnceResult:
    transport = UrllibMergeTrainGitHubTransport(
        token=token,
        api_base_url=request.github_api_base_url,
    )
    github_client = GitHubMergeTrainClient(transport=transport)
    lease_owner = merge_train_controller_lease_owner(trace_id=trace_id)
    if request.mutate:
        controller_state = controller_state_store.acquire_merge_train_controller_state_record(
            repository=request.repository,
            base_branch=request.base_branch,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
            lease_owner=lease_owner,
            lease_seconds=DEFAULT_MERGE_TRAIN_CONTROLLER_LEASE_SECONDS,
            initial_active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
            initial_active_phase="select_next_action",
            adoptable_active_actions=MERGE_TRAIN_CONTROLLER_ADOPTABLE_ACTIVE_ACTIONS,
        )
        lease = MergeTrainControllerLeaseContext(
            record=controller_state,
            record_store=controller_state_store,
        )
        recorded_at = lease.record.updated_at
    else:
        controller_state = latest_merge_train_controller_state_record(
            record_store=controller_state_store,
            repository=request.repository,
            base_branch=request.base_branch,
        ) or build_merge_train_controller_state_record(
            repository=request.repository,
            base_branch=request.base_branch,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
            updated_at=recorded_at,
        )
        lease = MergeTrainControllerLeaseContext(record=controller_state)
    try:
        result = _resume_merge_train_controller_state(
            request=request,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            trace_id=trace_id,
            recorded_at=recorded_at,
            github_client=github_client,
            landing_store=landing_store,
            stack_collapse_store=stack_collapse_store,
            lease=lease,
        )
        if result is None:
            active_landing_record = latest_merge_train_batch_landing_plan_record(
                record_store=landing_store,
                repository=request.repository,
                base_branch=request.base_branch,
            )
            if active_landing_record is not None:
                result = _advance_active_landing_record(
                    request=request,
                    policy_sha256=policy_sha256,
                    repository_policy=repository_policy,
                    trace_id=trace_id,
                    recorded_at=recorded_at,
                    github_client=github_client,
                    candidate_store=candidate_store,
                    landing_store=landing_store,
                    stack_collapse_store=stack_collapse_store,
                    admission_store=admission_store,
                    admission_evaluator=admission_evaluator,
                    active_landing_record=active_landing_record,
                    lease=lease,
                )
            else:
                result = _advance_without_active_landing(
                    request=request,
                    policy=policy,
                    policy_sha256=policy_sha256,
                    repository_policy=repository_policy,
                    transport=transport,
                    github_client=github_client,
                    trace_id=trace_id,
                    recorded_at=recorded_at,
                    candidate_store=candidate_store,
                    landing_store=landing_store,
                    stack_collapse_store=stack_collapse_store,
                    lease=lease,
                )
    except MergeTrainControllerLeaseLostError:
        raise
    except Exception as error:
        try:
            lease.release(
                reconciliation_status="required",
                reconciliation_detail=_controller_exception_reconciliation_detail(error),
                clear_active_state=False,
            )
        except Exception as release_error:  # noqa: BLE001
            _LOGGER.warning(
                "Merge train controller could not record failure state",
                extra={
                    "repository": request.repository,
                    "base_branch": request.base_branch,
                    "lease_owner": lease_owner,
                    "release_error_type": type(release_error).__name__,
                },
            )
        raise
    run_once_result = MergeTrainControllerRunOnceResult(
        accepted_result=result,
        records=_records_for_result(result),
    )
    if before_release is not None:
        try:
            before_release(run_once_result)
        except Exception as error:
            if request.mutate:
                try:
                    lease.release(
                        reconciliation_status="required",
                        reconciliation_detail=_controller_exception_reconciliation_detail(error),
                        clear_active_state=False,
                    )
                except Exception as release_error:  # noqa: BLE001
                    _LOGGER.warning(
                        "Merge train controller could not record pre-release failure state",
                        extra={
                            "repository": request.repository,
                            "base_branch": request.base_branch,
                            "lease_owner": lease_owner,
                            "release_error_type": type(release_error).__name__,
                        },
                    )
            raise
    if _controller_result_requires_reconciliation(result):
        lease.release(
            reconciliation_status="required",
            reconciliation_detail=_controller_result_reconciliation_detail(
                result=result,
                current_detail=lease.record.reconciliation_detail,
            ),
            clear_active_state=False,
        )
    else:
        lease.release()
    return run_once_result


def _resume_merge_train_controller_state(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    trace_id: str,
    recorded_at: str,
    github_client: GitHubMergeTrainClient,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object] | None:
    if not request.mutate:
        if not (
            lease.record.status in {"running", "reconcile_required"}
            and lease.record.active_action
            and lease.record.active_phase
        ):
            return None
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "resume_reconciliation",
            "controller_reconciliation_status": "required",
            "active_action": lease.record.active_action,
            "active_phase": lease.record.active_phase,
            "active_record_id": lease.record.active_record_id,
        }
    if lease.record.reconciliation_status != "adopted":
        return None
    if lease.record.active_action != "land_batch":
        return None
    if lease.record.active_phase in {
        "merge_batch_entries",
        "merge_pull_request",
        "landing_entry_merged",
    }:
        planned_record = _merge_train_landing_record_by_id(
            record_store=landing_store,
            repository=request.repository,
            base_branch=request.base_branch,
            record_id=lease.record.active_record_id,
        )
        if planned_record is None:
            raise MergeTrainControllerRequestError("merge train landing resume record is missing")
        landed_record = latest_completed_merge_train_batch_landing_plan_record(
            record_store=landing_store,
            repository=request.repository,
            base_branch=request.base_branch,
            batch_id=planned_record.landing_plan.batch_id,
            candidate_sha=planned_record.landing_plan.candidate_sha,
        )
        if landed_record is None:
            return None
        return _finish_landed_merge_train_batch(
            request=request,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            trace_id=trace_id,
            recorded_at=recorded_at,
            github_client=github_client,
            stack_collapse_store=stack_collapse_store,
            landed_record=landed_record,
            lease=lease,
        )
    if lease.record.active_phase not in {
        "cleanup_candidate_ref",
        "reconcile_stack_children",
        "stack_children_reconciled",
    }:
        return None
    landing_record_id = str(lease.record.step_payload.get("landing_plan_record_id") or "")
    if not landing_record_id:
        raise MergeTrainControllerRequestError(
            "merge train landing resume requires landing_plan_record_id"
        )
    landed_record = _merge_train_landing_record_by_id(
        record_store=landing_store,
        repository=request.repository,
        base_branch=request.base_branch,
        record_id=landing_record_id,
    )
    if landed_record is None:
        raise MergeTrainControllerRequestError("merge train landed resume record is missing")
    return _finish_landed_merge_train_batch(
        request=request,
        policy_sha256=policy_sha256,
        repository_policy=repository_policy,
        trace_id=trace_id,
        recorded_at=recorded_at,
        github_client=github_client,
        stack_collapse_store=stack_collapse_store,
        landed_record=landed_record,
        lease=lease,
    )


def _merge_train_landing_record_by_id(
    *,
    record_store: MergeTrainBatchLandingPlanRecordStore,
    repository: str,
    base_branch: str,
    record_id: str,
) -> MergeTrainBatchLandingPlanRecord | None:
    return next(
        (
            record
            for record in record_store.list_merge_train_batch_landing_plan_records(
                repository=repository,
                base_branch=base_branch,
                limit=100,
            )
            if record.record_id == record_id
        ),
        None,
    )


def latest_merge_train_batch_candidate_record(
    *,
    record_store: MergeTrainBatchCandidateRecordStore,
    repository: str,
    base_branch: str,
) -> MergeTrainBatchCandidateRecord | None:
    records = record_store.list_merge_train_batch_candidate_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    latest_record = latest_merge_train_batch_candidate_progress_record(records)
    if latest_record is None:
        return None
    if latest_record.candidate.status in {"passed", "stale", "blocked"}:
        return None
    return latest_record


def latest_passed_merge_train_batch_candidate_record(
    *,
    record_store: MergeTrainBatchCandidateRecordStore,
    landing_plan_record_store: MergeTrainBatchLandingPlanRecordStore,
    repository: str,
    base_branch: str,
) -> MergeTrainBatchCandidateRecord | None:
    records = record_store.list_merge_train_batch_candidate_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    latest_record = latest_merge_train_batch_candidate_progress_record(records)
    if latest_record is None or latest_record.candidate.status != "passed":
        return None
    completed_landing_record = latest_completed_merge_train_batch_landing_plan_record(
        record_store=landing_plan_record_store,
        repository=repository,
        base_branch=base_branch,
        batch_id=latest_record.candidate.batch_id,
        candidate_sha=latest_record.candidate.candidate_sha,
    )
    if completed_landing_record is not None:
        return None
    return latest_record


def _candidate_record_for_landing_plan(
    *,
    record_store: MergeTrainBatchCandidateRecordStore,
    landing_plan: MergeTrainBatchLandingPlan,
) -> MergeTrainBatchCandidateRecord | None:
    matches = tuple(
        record
        for record in record_store.list_merge_train_batch_candidate_records(
            repository=landing_plan.repository,
            base_branch=landing_plan.base_branch,
            status="active",
            limit=100,
        )
        if record.candidate.batch_id == landing_plan.batch_id
        and record.candidate.candidate_sha == landing_plan.candidate_sha
        and record.candidate.candidate_sha256 == landing_plan.candidate_sha256
    )
    return latest_merge_train_batch_candidate_progress_record(matches)


def latest_merge_train_batch_landing_plan_record(
    *,
    record_store: MergeTrainBatchLandingPlanRecordStore,
    repository: str,
    base_branch: str,
) -> MergeTrainBatchLandingPlanRecord | None:
    records = record_store.list_merge_train_batch_landing_plan_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    latest_record = latest_merge_train_batch_landing_progress_record(records)
    if latest_record is None:
        return None
    if not any(entry.status == "planned" for entry in latest_record.landing_plan.entries):
        return None
    return latest_record


def latest_completed_merge_train_batch_landing_plan_record(
    *,
    record_store: MergeTrainBatchLandingPlanRecordStore,
    repository: str,
    base_branch: str,
    batch_id: str,
    candidate_sha: str,
) -> MergeTrainBatchLandingPlanRecord | None:
    records = record_store.list_merge_train_batch_landing_plan_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    return latest_completed_merge_train_batch_landing_progress_record(
        landing_plan_records=records,
        batch_id=batch_id,
        candidate_sha=candidate_sha,
    )


def latest_merge_train_stack_collapse_plan_record(
    *,
    record_store: MergeTrainStackCollapsePlanRecordStore,
    repository: str,
    base_branch: str,
    plan_status: str,
) -> MergeTrainStackCollapsePlanRecord | None:
    records = record_store.list_merge_train_stack_collapse_plan_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    latest_record = latest_merge_train_stack_collapse_progress_record(records)
    if latest_record is None or latest_record.plan.status != plan_status:
        return None
    return latest_record


def latest_merge_train_stack_collapse_plan_record_for_landing(
    *,
    record_store: MergeTrainStackCollapsePlanRecordStore,
    repository: str,
    base_branch: str,
    landing_plan: MergeTrainBatchLandingPlan,
    policy_sha256: str,
) -> MergeTrainStackCollapsePlanRecord | None:
    records = record_store.list_merge_train_stack_collapse_plan_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    compatible_records = tuple(
        record
        for record in records
        if _merge_train_stack_collapse_record_matches_landing_plan(
            collapse_record=record,
            landing_plan=landing_plan,
            policy_sha256=policy_sha256,
        )
    )
    return latest_merge_train_stack_collapse_progress_record(compatible_records)


def latest_merge_train_stack_collapse_plan_record_for_completed_landing(
    *,
    record_store: MergeTrainStackCollapsePlanRecordStore,
    repository: str,
    base_branch: str,
    landing_plan: MergeTrainBatchLandingPlan,
    policy_sha256: str,
) -> MergeTrainStackCollapsePlanRecord | None:
    root_entries = {entry.pull_request_number: entry for entry in landing_plan.entries}
    compatible_records = tuple(
        record
        for record in record_store.list_merge_train_stack_collapse_plan_records(
            repository=repository,
            base_branch=base_branch,
            status="active",
            limit=100,
        )
        if record.plan.status in {"waiting_for_root_checks", "ready_for_train"}
        and record.plan.policy_key == landing_plan.policy_key
        and record.plan.policy_sha256 == policy_sha256 == landing_plan.policy_sha256
        and record.plan.root_pull_request_number in root_entries
        and root_entries[record.plan.root_pull_request_number].expected_head_sha
        == stack_collapse_expected_root_head_sha(record.plan)
    )
    return latest_merge_train_stack_collapse_progress_record(compatible_records)


def _advance_active_landing_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    trace_id: str,
    recorded_at: str,
    github_client: GitHubMergeTrainClient,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    admission_store: MergeAdmissionRecordStore,
    admission_evaluator: MergeAdmissionEvaluator,
    active_landing_record: MergeTrainBatchLandingPlanRecord,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object]:
    try:
        validate_merge_train_landing_record_for_controller(
            landing_record=active_landing_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    collapse_record = latest_merge_train_stack_collapse_plan_record_for_landing(
        record_store=stack_collapse_store,
        repository=request.repository,
        base_branch=request.base_branch,
        landing_plan=active_landing_record.landing_plan,
        policy_sha256=policy_sha256,
    )
    if collapse_record is not None:
        try:
            validate_stack_collapse_record_for_landing(
                collapse_record=collapse_record,
                landing_plan=active_landing_record.landing_plan,
                policy_sha256=policy_sha256,
            )
        except ValueError as error:
            raise MergeTrainControllerRequestError(str(error)) from error
        if not repository_policy.stack_child_disposition_label:
            raise ValueError(
                "merge train stack child disposition requires stack_child_disposition_label policy"
            )
    candidate_record = _candidate_record_for_landing_plan(
        record_store=candidate_store,
        landing_plan=active_landing_record.landing_plan,
    )
    if candidate_record is None:
        raise MergeTrainControllerRequestError(
            "merge train landing requires its exact active candidate record"
        )
    admission_guard = GuardedMergeAdmission(
        record_store=admission_store,
        evaluator=admission_evaluator,
        candidate_record=candidate_record,
        landing_plan_record=active_landing_record,
        controller_state=lease.record,
        controller_state_provider=lease.read_current,
        stack_collapse_record=collapse_record,
        trace_id=trace_id,
    )
    if not request.mutate:
        result: dict[str, object] = {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "land_batch",
            "merge_train_batch_landing_plan_record_id": active_landing_record.record_id,
        }
        if collapse_record is not None:
            result["merge_train_stack_collapse_plan_record_id"] = collapse_record.record_id
        return result

    lease.checkpoint(
        active_action="land_batch",
        active_phase="merge_batch_entries",
        active_record_id=active_landing_record.record_id,
        active_pull_request_number=None,
        step_payload={
            "landing_plan_record_id": active_landing_record.record_id,
            "landing_plan_id": active_landing_record.landing_plan.plan_id,
            "expected_effect_sha": active_landing_record.landing_plan.candidate_sha,
        },
    )

    def checkpoint_landing_progress(
        progress_plan: MergeTrainBatchLandingPlan,
        entry: MergeTrainBatchLandingEntry,
        phase: str,
    ) -> MergeTrainBatchLandingPlanRecord | None:
        state_phase = "admit_pull_request" if phase == "merge_entry" else "landing_entry_merged"
        lease.checkpoint(
            active_action="land_batch",
            active_phase=state_phase,
            active_record_id=active_landing_record.record_id,
            active_pull_request_number=entry.pull_request_number,
            step_payload={
                "landing_plan_record_id": active_landing_record.record_id,
                "landing_plan_id": progress_plan.plan_id,
                "batch_id": progress_plan.batch_id,
                "candidate_ref": progress_plan.candidate_ref,
                "expected_effect_sha": progress_plan.candidate_sha,
                "completed_entry_count": sum(
                    progress_entry.status == "merged" for progress_entry in progress_plan.entries
                ),
            },
        )
        if phase not in {"entry_merged", "entry_skipped"}:
            return None
        progress_record = build_merge_train_batch_landing_plan_record(
            landing_plan=progress_plan,
            source=f"service:controller:landing-progress:{trace_id}",
            updated_at=lease.record.updated_at,
        )
        landing_store.write_merge_train_batch_landing_plan_record(progress_record)
        return progress_record

    def checkpoint_provider_merge(
        progress_plan: MergeTrainBatchLandingPlan,
        entry: MergeTrainBatchLandingEntry,
    ) -> None:
        lease.checkpoint(
            active_action="land_batch",
            active_phase="merge_pull_request",
            active_record_id=active_landing_record.record_id,
            active_pull_request_number=entry.pull_request_number,
            step_payload={
                "landing_plan_record_id": active_landing_record.record_id,
                "landing_plan_id": progress_plan.plan_id,
                "batch_id": progress_plan.batch_id,
                "candidate_ref": progress_plan.candidate_ref,
                "expected_effect_sha": progress_plan.candidate_sha,
                "completed_entry_count": sum(
                    progress_entry.status == "merged" for progress_entry in progress_plan.entries
                ),
            },
        )

    try:
        landed_plan = github_client.land_batch_candidate(
            landing_plan=active_landing_record.landing_plan,
            admission_guard=admission_guard,
            recorded_at=recorded_at,
            provider_checkpoint=checkpoint_provider_merge,
            checkpoint=checkpoint_landing_progress,
        )
    except MergeAdmissionDeniedError as error:
        readiness = error.readiness
        blocked_landing_record = admission_guard.landing_plan_record
        return {
            "merge_train_batch_landing_plan_record_id": blocked_landing_record.record_id,
            "repository": blocked_landing_record.landing_plan.repository,
            "base_branch": blocked_landing_record.landing_plan.base_branch,
            "mode": "blocked",
            "controller_action": "block",
            "blocking_reason": {
                "code": error.reason_code,
                "message": str(error),
            },
            "merge_readiness": (
                {
                    "state": readiness.state,
                    "reason_codes": list(readiness.reason_codes),
                    "owner_states": sorted({facet.state for facet in readiness.owner_facets}),
                    "technical_checks_state": readiness.technical_checks.state,
                    "engineering_review_state": readiness.engineering_review.state,
                    "policy_state": readiness.policy.state,
                    "candidate_state": readiness.candidate.state,
                    "fence_state": readiness.fence.state,
                }
                if readiness is not None
                else None
            ),
            "landing_plan": blocked_landing_record.landing_plan.model_dump(mode="json"),
        }
    except MergeTrainGitHubStaleHeadError as error:
        stale_plan = stale_merge_train_landing_plan(active_landing_record.landing_plan)
        stale_record = build_merge_train_batch_landing_plan_record(
            landing_plan=stale_plan,
            source=f"service:controller:stale-landing:{trace_id}",
            updated_at=recorded_at,
        )
        landing_store.write_merge_train_batch_landing_plan_record(stale_record)
        message = str(error).strip() or "Merge train landing evidence no longer matches GitHub."
        return {
            "merge_train_batch_landing_plan_record_id": stale_record.record_id,
            "repository": stale_plan.repository,
            "base_branch": stale_plan.base_branch,
            "mode": "stale_landing",
            "controller_action": "land_batch",
            "landing_plan": stale_plan.model_dump(mode="json"),
            "error": {
                "code": "merge_train_github_stale_state",
                "message": message,
            },
            "details": {
                "github_status_code": error.status_code,
            },
        }

    landed_record = build_merge_train_batch_landing_plan_record(
        landing_plan=landed_plan,
        source=f"service:controller:land:{trace_id}",
        updated_at=recorded_at,
    )
    landing_store.write_merge_train_batch_landing_plan_record(landed_record)
    return _finish_landed_merge_train_batch(
        request=request,
        policy_sha256=policy_sha256,
        repository_policy=repository_policy,
        trace_id=trace_id,
        recorded_at=recorded_at,
        github_client=github_client,
        stack_collapse_store=stack_collapse_store,
        landed_record=landed_record,
        lease=lease,
    )


def _finish_landed_merge_train_batch(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    trace_id: str,
    recorded_at: str,
    github_client: GitHubMergeTrainClient,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    landed_record: MergeTrainBatchLandingPlanRecord,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object]:
    landed_plan = landed_record.landing_plan
    try:
        validate_merge_train_landing_record_for_controller(
            landing_record=landed_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    if any(entry.status != "merged" for entry in landed_plan.entries):
        raise MergeTrainControllerRequestError(
            "merge train cleanup requires a fully landed batch record"
        )
    collapse_record = latest_merge_train_stack_collapse_plan_record_for_completed_landing(
        record_store=stack_collapse_store,
        repository=request.repository,
        base_branch=request.base_branch,
        landing_plan=landed_plan,
        policy_sha256=policy_sha256,
    )
    if collapse_record is not None and not repository_policy.stack_child_disposition_label:
        raise MergeTrainControllerRequestError(
            "merge train stack child disposition requires stack_child_disposition_label policy"
        )
    lease.checkpoint(
        active_action="land_batch",
        active_phase="cleanup_candidate_ref",
        active_record_id=landed_record.record_id,
        active_pull_request_number=None,
        step_payload={
            **lease.record.step_payload,
            "landing_plan_record_id": landed_record.record_id,
            "candidate_ref": landed_plan.candidate_ref,
        },
    )
    candidate_ref_cleanup_result = cleanup_merge_train_batch_candidate_ref(
        github_client=github_client,
        landing_plan=landed_plan,
        trace_id=trace_id,
        lease=lease,
    )
    result = {
        "merge_train_batch_landing_plan_record_id": landed_record.record_id,
        "repository": landed_plan.repository,
        "base_branch": landed_plan.base_branch,
        "mode": "land",
        "controller_action": "land_batch",
        "landing_plan": landed_plan.model_dump(mode="json"),
        **candidate_ref_cleanup_result,
    }
    if candidate_ref_cleanup_result.get("candidate_ref_cleanup_status") == "failed":
        return result
    if collapse_record is None:
        if lease.record.step_payload.get("stack_collapse_plan_record_id"):
            raise MergeTrainControllerRequestError(
                "merge train stack collapse resume record is missing or incompatible"
            )
        return result
    if collapse_record.plan.status == "ready_for_train":
        result["merge_train_stack_collapse_plan_record_id"] = collapse_record.record_id
        result["stack_collapse_plan"] = collapse_record.plan.model_dump(mode="json")
        return result

    lease.checkpoint(
        active_action="land_batch",
        active_phase="reconcile_stack_children",
        active_record_id=collapse_record.record_id,
        active_pull_request_number=collapse_record.plan.root_pull_request_number,
        step_payload={
            **lease.record.step_payload,
            "landing_plan_record_id": landed_record.record_id,
            "stack_collapse_plan_record_id": collapse_record.record_id,
            "root_merge_commit_sha": next(
                entry.merge_commit_sha
                for entry in landed_plan.entries
                if entry.pull_request_number == collapse_record.plan.root_pull_request_number
            ),
        },
    )

    root_entry = next(
        (
            entry
            for entry in landed_plan.entries
            if entry.pull_request_number == collapse_record.plan.root_pull_request_number
        ),
        None,
    )
    if root_entry is None or root_entry.status != "merged":
        raise ValueError("merge train stack child disposition requires merged root PR")

    def checkpoint_child_disposition(
        progress_plan: MergeTrainStackCollapsePlan,
    ) -> None:
        next_disposition = next(
            (
                disposition
                for disposition in progress_plan.child_dispositions
                if disposition.status != "closed"
            ),
            None,
        )
        lease.checkpoint(
            active_action="land_batch",
            active_phase="reconcile_stack_children",
            active_record_id=collapse_record.record_id,
            active_pull_request_number=(
                next_disposition.pull_request_number
                if next_disposition is not None
                else collapse_record.plan.root_pull_request_number
            ),
            step_payload={
                **lease.record.step_payload,
                "completed_disposition_count": sum(
                    disposition.status == "closed"
                    for disposition in progress_plan.child_dispositions
                ),
            },
        )
        progress_record = build_merge_train_stack_collapse_plan_record(
            plan=progress_plan.model_copy(update={"updated_at": lease.record.updated_at}),
            source=f"service:controller:child-disposition-progress:{trace_id}",
            updated_at=lease.record.updated_at,
        )
        stack_collapse_store.write_merge_train_stack_collapse_plan_record(progress_record)

    reconciled_collapse_plan = reconcile_merge_train_stack_children_after_root_landing(
        plan=collapse_record.plan,
        disposition_client=github_client,
        root_merge_commit_sha=root_entry.merge_commit_sha,
        label=repository_policy.stack_child_disposition_label,
        updated_at=recorded_at,
        checkpoint=checkpoint_child_disposition,
    )
    reconciled_record = build_merge_train_stack_collapse_plan_record(
        plan=reconciled_collapse_plan,
        source=f"service:controller:child-disposition:{trace_id}",
        updated_at=recorded_at,
    )
    stack_collapse_store.write_merge_train_stack_collapse_plan_record(reconciled_record)
    lease.checkpoint(
        active_action="land_batch",
        active_phase="stack_children_reconciled",
        active_record_id=reconciled_record.record_id,
        active_pull_request_number=collapse_record.plan.root_pull_request_number,
        step_payload={
            **lease.record.step_payload,
            "stack_collapse_plan_record_id": reconciled_record.record_id,
            "completed_disposition_count": len(reconciled_collapse_plan.child_dispositions),
        },
    )
    result["merge_train_stack_collapse_plan_record_id"] = reconciled_record.record_id
    result["stack_collapse_plan"] = reconciled_collapse_plan.model_dump(mode="json")
    return result


def _advance_without_active_landing(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    transport: MergeTrainGitHubTransport,
    github_client: GitHubMergeTrainClient,
    trace_id: str,
    recorded_at: str,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object]:
    active_candidate_record = latest_merge_train_batch_candidate_record(
        record_store=candidate_store,
        repository=request.repository,
        base_branch=request.base_branch,
    )
    passed_candidate_record = None
    if active_candidate_record is None:
        passed_candidate_record = latest_passed_merge_train_batch_candidate_record(
            record_store=candidate_store,
            landing_plan_record_store=landing_store,
            repository=request.repository,
            base_branch=request.base_branch,
        )
    if active_candidate_record is not None:
        return _advance_active_candidate_record(
            request=request,
            policy=policy,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            transport=transport,
            github_client=github_client,
            trace_id=trace_id,
            recorded_at=recorded_at,
            candidate_store=candidate_store,
            active_candidate_record=active_candidate_record,
            lease=lease,
        )
    if passed_candidate_record is not None:
        return _advance_passed_candidate_record(
            request=request,
            policy=policy,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            transport=transport,
            github_client=github_client,
            trace_id=trace_id,
            recorded_at=recorded_at,
            candidate_store=candidate_store,
            landing_store=landing_store,
            stack_collapse_store=stack_collapse_store,
            passed_candidate_record=passed_candidate_record,
            lease=lease,
        )
    return _advance_without_candidate_record(
        request=request,
        policy=policy,
        policy_sha256=policy_sha256,
        repository_policy=repository_policy,
        transport=transport,
        github_client=github_client,
        trace_id=trace_id,
        recorded_at=recorded_at,
        candidate_store=candidate_store,
        stack_collapse_store=stack_collapse_store,
        lease=lease,
    )


def _advance_active_candidate_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    transport: MergeTrainGitHubTransport,
    github_client: GitHubMergeTrainClient,
    trace_id: str,
    recorded_at: str,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    active_candidate_record: MergeTrainBatchCandidateRecord,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object]:
    try:
        validate_merge_train_candidate_record_for_controller(
            candidate_record=active_candidate_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    if active_candidate_record.candidate.status == "failed":
        if request.mutate:
            lease.checkpoint(
                active_action="reflow_candidate",
                active_phase="read_queue_and_plan_replacement",
                active_record_id=active_candidate_record.record_id,
                active_pull_request_number=None,
                step_payload={
                    "candidate_record_id": active_candidate_record.record_id,
                    "batch_id": active_candidate_record.candidate.batch_id,
                },
            )
        reflow_result = try_reflow_failed_merge_train_candidate(
            candidate_store=candidate_store,
            active_candidate_record=active_candidate_record,
            policy=policy,
            policy_sha256=policy_sha256,
            transport=transport,
            repository=request.repository,
            base_branch=request.base_branch,
            recorded_at=recorded_at,
            trace_id=trace_id,
            mutate=request.mutate,
        )
        if reflow_result is not None:
            if request.mutate:
                lease.checkpoint(
                    active_action="reflow_candidate",
                    active_phase="replacement_recorded",
                    active_record_id=str(
                        reflow_result.get("merge_train_batch_candidate_record_id") or ""
                    ),
                    active_pull_request_number=None,
                    step_payload={
                        "superseded_candidate_record_id": active_candidate_record.record_id,
                        "replacement_candidate_record_id": str(
                            reflow_result.get("merge_train_batch_candidate_record_id") or ""
                        ),
                    },
                )
            return reflow_result
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "candidate_failed",
            "merge_train_batch_candidate_record_id": active_candidate_record.record_id,
            "candidate": active_candidate_record.candidate.model_dump(mode="json"),
        }

    candidate_build_error: MergeTrainGitHubStaleHeadError | None = None
    if active_candidate_record.candidate.status in {"planned", "building"}:
        controller_action = "build_candidate"
        if request.mutate:
            lease.checkpoint(
                active_action=controller_action,
                active_phase="build_candidate_ref",
                active_record_id=active_candidate_record.record_id,
                active_pull_request_number=None,
                step_payload={
                    "candidate_record_id": active_candidate_record.record_id,
                    "candidate_ref": active_candidate_record.candidate.candidate_ref,
                    "batch_id": active_candidate_record.candidate.batch_id,
                },
            )

        def checkpoint_candidate_progress(
            progress_candidate: MergeTrainBatchCandidate,
            entry: MergeTrainBatchEntry | None,
            phase: str,
        ) -> None:
            lease.checkpoint(
                active_action=controller_action,
                active_phase=phase.split(":", 1)[0],
                active_record_id=active_candidate_record.record_id,
                active_pull_request_number=(
                    entry.pull_request_number if entry is not None else None
                ),
                step_payload={
                    "candidate_record_id": active_candidate_record.record_id,
                    "candidate_ref": progress_candidate.candidate_ref,
                    "candidate_sha": progress_candidate.candidate_sha,
                    "completed_entry_count": (int(phase.split(":", 1)[1]) if ":" in phase else 0),
                },
            )

        if request.mutate:
            try:
                candidate = github_client.build_batch_candidate(
                    candidate=active_candidate_record.candidate,
                    checkpoint=checkpoint_candidate_progress,
                )
            except MergeTrainGitHubStaleHeadError as error:
                controller_action = "candidate_failed"
                candidate_build_error = error
                candidate = active_candidate_record.candidate.model_copy(
                    update={"status": "failed", "updated_at": recorded_at}
                )
        else:
            candidate = active_candidate_record.candidate
    else:
        controller_action = "observe_candidate"
        if request.mutate:
            lease.checkpoint(
                active_action=controller_action,
                active_phase="observe_required_checks",
                active_record_id=active_candidate_record.record_id,
                active_pull_request_number=None,
                step_payload={
                    "candidate_record_id": active_candidate_record.record_id,
                    "candidate_ref": active_candidate_record.candidate.candidate_ref,
                    "candidate_sha": active_candidate_record.candidate.candidate_sha,
                },
            )
        candidate = (
            github_client.observe_batch_candidate_checks(
                candidate=active_candidate_record.candidate
            )
            if request.mutate
            else active_candidate_record.candidate
        )
    result: dict[str, object] = {
        "repository": request.repository,
        "base_branch": request.base_branch,
        "mode": "dry-run" if not request.mutate else controller_action,
        "controller_action": controller_action,
        "merge_train_batch_candidate_record_id": active_candidate_record.record_id,
    }
    if candidate_build_error is not None:
        message = (
            str(candidate_build_error).strip()
            or "Merge train candidate evidence no longer matches GitHub."
        )
        result["error"] = {
            "code": "merge_train_github_stale_state",
            "message": message,
        }
        result["details"] = {
            "github_status_code": candidate_build_error.status_code,
            "failed_pull_request_number": lease.record.active_pull_request_number,
        }
    if request.mutate:
        updated_candidate_record = build_merge_train_batch_candidate_record(
            candidate=candidate,
            source=f"service:controller:{controller_action}:{trace_id}",
            updated_at=recorded_at,
        )
        candidate_store.write_merge_train_batch_candidate_record(updated_candidate_record)
        result["merge_train_batch_candidate_record_id"] = updated_candidate_record.record_id
        lease.checkpoint(
            active_action=controller_action,
            active_phase="candidate_result_recorded",
            active_record_id=updated_candidate_record.record_id,
            active_pull_request_number=None,
            step_payload={
                "candidate_record_id": updated_candidate_record.record_id,
                "candidate_ref": candidate.candidate_ref,
                "candidate_sha": candidate.candidate_sha,
                "candidate_status": candidate.status,
            },
        )
    result["candidate"] = candidate.model_dump(mode="json")
    return result


def _advance_passed_candidate_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    transport: MergeTrainGitHubTransport,
    github_client: GitHubMergeTrainClient,
    trace_id: str,
    recorded_at: str,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    passed_candidate_record: MergeTrainBatchCandidateRecord,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object]:
    try:
        validate_merge_train_candidate_record_for_controller(
            candidate_record=passed_candidate_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    completed_landing_record = latest_completed_merge_train_batch_landing_plan_record(
        record_store=landing_store,
        repository=request.repository,
        base_branch=request.base_branch,
        batch_id=passed_candidate_record.candidate.batch_id,
        candidate_sha=passed_candidate_record.candidate.candidate_sha,
    )
    if completed_landing_record is not None:
        try:
            validate_merge_train_landing_record_for_controller(
                landing_record=completed_landing_record,
                policy_key=repository_policy.policy_key,
                policy_sha256=policy_sha256,
            )
        except ValueError as error:
            raise MergeTrainControllerRequestError(str(error)) from error
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "batch_landed",
            "merge_train_batch_candidate_record_id": passed_candidate_record.record_id,
            "merge_train_batch_landing_plan_record_id": completed_landing_record.record_id,
            "landing_plan": completed_landing_record.landing_plan.model_dump(mode="json"),
        }
    snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
        repository=request.repository,
        base_branch=request.base_branch,
    )
    candidate_snapshot = snapshot
    stack_collapse_root = passed_candidate_record.candidate.stack_collapse_root
    if stack_collapse_root is not None:
        candidate_snapshot = snapshot.model_copy(
            update={
                "pull_requests": tuple(
                    pull_request
                    for pull_request in snapshot.pull_requests
                    if pull_request.number == stack_collapse_root.root_pull_request_number
                )
            }
        )
    dry_run_result = build_merge_train_dry_run_result(
        policy=policy,
        snapshot=candidate_snapshot,
    )
    candidate_matches_queue = _merge_train_candidate_matches_dry_run_queue(
        candidate=passed_candidate_record.candidate,
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
    )
    if not candidate_matches_queue:
        if request.mutate:
            lease.checkpoint(
                active_action="reflow_candidate",
                active_phase="supersede_stale_passed_candidate",
                active_record_id=passed_candidate_record.record_id,
                active_pull_request_number=None,
                step_payload={
                    "candidate_record_id": passed_candidate_record.record_id,
                    "batch_id": passed_candidate_record.candidate.batch_id,
                },
            )
            _supersede_active_merge_train_batch_candidate_records(
                record_store=candidate_store,
                repository=request.repository,
                base_branch=request.base_branch,
                batch_id=passed_candidate_record.candidate.batch_id,
                replacement_record_id=None,
            )
        result = _advance_without_candidate_record(
            request=request,
            policy=policy,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            transport=transport,
            github_client=github_client,
            candidate_store=candidate_store,
            stack_collapse_store=stack_collapse_store,
            trace_id=trace_id,
            recorded_at=recorded_at,
            lease=lease,
        )
        result["superseded_merge_train_batch_candidate_record_id"] = (
            passed_candidate_record.record_id
        )
        return result
    if not request.mutate:
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "plan_landing",
            "merge_train_batch_candidate_record_id": passed_candidate_record.record_id,
        }

    lease.checkpoint(
        active_action="plan_landing",
        active_phase="persist_landing_plan",
        active_record_id=passed_candidate_record.record_id,
        active_pull_request_number=None,
        step_payload={
            "candidate_record_id": passed_candidate_record.record_id,
            "batch_id": passed_candidate_record.candidate.batch_id,
            "candidate_sha": passed_candidate_record.candidate.candidate_sha,
        },
    )
    landing_plan = build_merge_train_batch_landing_plan(
        candidate=passed_candidate_record.candidate,
        merge_method=repository_policy.merge_method,
        created_at=recorded_at,
    )
    landing_record = build_merge_train_batch_landing_plan_record(
        landing_plan=landing_plan,
        source=f"service:controller:landing-plan:{trace_id}",
        updated_at=recorded_at,
    )
    landing_store.write_merge_train_batch_landing_plan_record(landing_record)
    lease.checkpoint(
        active_action="plan_landing",
        active_phase="landing_plan_recorded",
        active_record_id=landing_record.record_id,
        active_pull_request_number=None,
        step_payload={
            "candidate_record_id": passed_candidate_record.record_id,
            "landing_plan_record_id": landing_record.record_id,
            "batch_id": landing_plan.batch_id,
            "candidate_sha": landing_plan.candidate_sha,
        },
    )
    return {
        "merge_train_batch_landing_plan_record_id": landing_record.record_id,
        "repository": landing_plan.repository,
        "base_branch": landing_plan.base_branch,
        "mode": "plan_landing",
        "controller_action": "plan_landing",
        "landing_plan": landing_plan.model_dump(mode="json"),
    }


def _advance_without_candidate_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    transport: MergeTrainGitHubTransport,
    github_client: GitHubMergeTrainClient,
    trace_id: str,
    recorded_at: str,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object]:
    waiting_collapse_record = latest_merge_train_stack_collapse_plan_record(
        record_store=stack_collapse_store,
        repository=request.repository,
        base_branch=request.base_branch,
        plan_status="waiting_for_root_checks",
    )
    if waiting_collapse_record is not None:
        waiting_result = _advance_waiting_stack_collapse_record(
            request=request,
            policy=policy,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            transport=transport,
            candidate_store=candidate_store,
            stack_collapse_store=stack_collapse_store,
            waiting_collapse_record=waiting_collapse_record,
            trace_id=trace_id,
            recorded_at=recorded_at,
            lease=lease,
        )
        if waiting_result is not None:
            return waiting_result

    planned_collapse_record = latest_merge_train_stack_collapse_plan_record(
        record_store=stack_collapse_store,
        repository=request.repository,
        base_branch=request.base_branch,
        plan_status="collapsing",
    ) or latest_merge_train_stack_collapse_plan_record(
        record_store=stack_collapse_store,
        repository=request.repository,
        base_branch=request.base_branch,
        plan_status="planned",
    )
    if planned_collapse_record is not None:
        planned_result = _advance_planned_stack_collapse_record(
            request=request,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            transport=transport,
            github_client=github_client,
            stack_collapse_store=stack_collapse_store,
            planned_collapse_record=planned_collapse_record,
            trace_id=trace_id,
            recorded_at=recorded_at,
            lease=lease,
        )
        if planned_result is not None:
            return planned_result

    return _advance_from_live_snapshot(
        request=request,
        policy=policy,
        policy_sha256=policy_sha256,
        transport=transport,
        candidate_store=candidate_store,
        stack_collapse_store=stack_collapse_store,
        trace_id=trace_id,
        recorded_at=recorded_at,
        lease=lease,
    )


def _advance_waiting_stack_collapse_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    transport: MergeTrainGitHubTransport,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    waiting_collapse_record: MergeTrainStackCollapsePlanRecord,
    trace_id: str,
    recorded_at: str,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object] | None:
    snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
        repository=request.repository,
        base_branch=request.base_branch,
    )
    root_pull_request = next(
        (
            pull_request
            for pull_request in snapshot.pull_requests
            if pull_request.number == waiting_collapse_record.plan.root_pull_request_number
        ),
        None,
    )
    if (
        root_pull_request is None
        or root_pull_request.head_sha
        != stack_collapse_expected_root_head_sha(waiting_collapse_record.plan)
    ):
        return None
    try:
        validate_merge_train_stack_collapse_record_for_controller(
            collapse_record=waiting_collapse_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    root_snapshot = snapshot.model_copy(update={"pull_requests": (root_pull_request,)})
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=root_snapshot)
    if dry_run_result.intended_next_action != "merge":
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "wait_for_root_checks",
            "merge_train_stack_collapse_plan_record_id": waiting_collapse_record.record_id,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
        }
    if not request.mutate:
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "admit_collapsed_root",
            "merge_train_stack_collapse_plan_record_id": waiting_collapse_record.record_id,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
        }

    lease.checkpoint(
        active_action="admit_collapsed_root",
        active_phase="persist_candidate_plan",
        active_record_id=waiting_collapse_record.record_id,
        active_pull_request_number=waiting_collapse_record.plan.root_pull_request_number,
        step_payload={
            "stack_collapse_plan_record_id": waiting_collapse_record.record_id,
            "collapse_id": waiting_collapse_record.plan.collapse_id,
        },
    )
    candidate = build_merge_train_batch_candidate(
        dry_run_result=dry_run_result,
        base_sha=root_snapshot.base_sha,
        policy_sha256=policy_sha256,
        created_at=recorded_at,
        stack_collapse_root=MergeTrainStackCollapseRootProof(
            collapse_record_id=waiting_collapse_record.record_id,
            collapse_id=waiting_collapse_record.plan.collapse_id,
            root_pull_request_number=waiting_collapse_record.plan.root_pull_request_number,
            original_root_head_sha=waiting_collapse_record.plan.root_initial_head_sha,
            collapsed_root_head_sha=stack_collapse_expected_root_head_sha(
                waiting_collapse_record.plan
            ),
        ),
    )
    candidate_record = build_merge_train_batch_candidate_record(
        candidate=candidate,
        source=f"service:controller:stack-collapse-admit:{trace_id}",
        updated_at=recorded_at,
    )
    candidate_store.write_merge_train_batch_candidate_record(candidate_record)
    lease.checkpoint(
        active_action="admit_collapsed_root",
        active_phase="candidate_plan_recorded",
        active_record_id=candidate_record.record_id,
        active_pull_request_number=waiting_collapse_record.plan.root_pull_request_number,
        step_payload={
            "stack_collapse_plan_record_id": waiting_collapse_record.record_id,
            "candidate_record_id": candidate_record.record_id,
            "collapse_id": waiting_collapse_record.plan.collapse_id,
        },
    )
    return {
        "merge_train_batch_candidate_record_id": candidate_record.record_id,
        "merge_train_stack_collapse_plan_record_id": waiting_collapse_record.record_id,
        "repository": candidate.repository,
        "base_branch": candidate.base_branch,
        "mode": "admit_collapsed_root",
        "controller_action": "admit_collapsed_root",
        "dry_run_result": dry_run_result.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
    }


def _advance_planned_stack_collapse_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    transport: MergeTrainGitHubTransport,
    github_client: GitHubMergeTrainClient,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    planned_collapse_record: MergeTrainStackCollapsePlanRecord,
    trace_id: str,
    recorded_at: str,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object] | None:
    snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
        repository=request.repository,
        base_branch=request.base_branch,
    )
    root_pull_request = next(
        (
            pull_request
            for pull_request in snapshot.pull_requests
            if pull_request.number == planned_collapse_record.plan.root_pull_request_number
        ),
        None,
    )
    if root_pull_request is None:
        return None
    current_head_shas = {
        entry.pull_request_number: entry.head_sha for entry in planned_collapse_record.plan.entries
    }
    for mutation in planned_collapse_record.plan.mutations:
        if mutation.status == "mutated" and mutation.merge_commit_sha:
            current_head_shas[mutation.parent_pull_request_number] = mutation.merge_commit_sha
    root_mutation = next(
        mutation
        for mutation in planned_collapse_record.plan.mutations
        if mutation.parent_pull_request_number
        == planned_collapse_record.plan.root_pull_request_number
    )
    expected_root_sha = current_head_shas[root_mutation.parent_pull_request_number]
    if root_pull_request.head_sha != expected_root_sha:
        observed_root_sha = github_client.find_stack_child_merge_commit(
            repository=planned_collapse_record.plan.repository,
            child_head_sha=current_head_shas[root_mutation.child_pull_request_number],
            expected_parent_head_sha=expected_root_sha,
            parent_head_ref=root_mutation.parent_head_ref,
            collapse_id=planned_collapse_record.plan.collapse_id,
            child_pull_request_number=root_mutation.child_pull_request_number,
            parent_pull_request_number=root_mutation.parent_pull_request_number,
        )
        if observed_root_sha != root_pull_request.head_sha:
            return None
    try:
        validate_merge_train_stack_collapse_record_for_controller(
            collapse_record=planned_collapse_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    result: dict[str, object] = {
        "repository": request.repository,
        "base_branch": request.base_branch,
        "mode": "dry-run" if not request.mutate else "execute_stack_collapse",
        "controller_action": "execute_stack_collapse",
        "merge_train_stack_collapse_plan_record_id": planned_collapse_record.record_id,
    }
    if request.mutate:
        lease.checkpoint(
            active_action="execute_stack_collapse",
            active_phase="merge_stack_branches",
            active_record_id=planned_collapse_record.record_id,
            active_pull_request_number=planned_collapse_record.plan.root_pull_request_number,
            step_payload={
                "stack_collapse_plan_record_id": planned_collapse_record.record_id,
                "collapse_id": planned_collapse_record.plan.collapse_id,
            },
        )

        def checkpoint_collapse_progress(
            progress_plan: MergeTrainStackCollapsePlan,
        ) -> None:
            next_mutation = next(
                (mutation for mutation in progress_plan.mutations if mutation.status == "planned"),
                None,
            )
            lease.checkpoint(
                active_action="execute_stack_collapse",
                active_phase="merge_stack_branches",
                active_record_id=planned_collapse_record.record_id,
                active_pull_request_number=(
                    next_mutation.parent_pull_request_number
                    if next_mutation is not None
                    else progress_plan.root_pull_request_number
                ),
                step_payload={
                    "stack_collapse_plan_record_id": planned_collapse_record.record_id,
                    "collapse_id": progress_plan.collapse_id,
                    "completed_mutation_count": sum(
                        mutation.status == "mutated" for mutation in progress_plan.mutations
                    ),
                },
            )
            progress_record = build_merge_train_stack_collapse_plan_record(
                plan=progress_plan.model_copy(update={"updated_at": lease.record.updated_at}),
                source=f"service:controller:stack-collapse-progress:{trace_id}",
                updated_at=lease.record.updated_at,
            )
            stack_collapse_store.write_merge_train_stack_collapse_plan_record(progress_record)

        executed_plan = execute_merge_train_stack_collapse_plan(
            plan=planned_collapse_record.plan,
            branch_client=github_client,
            updated_at=recorded_at,
            checkpoint=checkpoint_collapse_progress,
        )
        executed_record = build_merge_train_stack_collapse_plan_record(
            plan=executed_plan,
            source=f"service:controller:stack-collapse-execute:{trace_id}",
            updated_at=recorded_at,
        )
        stack_collapse_store.write_merge_train_stack_collapse_plan_record(executed_record)
        lease.checkpoint(
            active_action="execute_stack_collapse",
            active_phase="stack_collapse_recorded",
            active_record_id=executed_record.record_id,
            active_pull_request_number=executed_plan.root_pull_request_number,
            step_payload={
                "stack_collapse_plan_record_id": executed_record.record_id,
                "collapse_id": executed_plan.collapse_id,
                "completed_mutation_count": len(executed_plan.mutations),
            },
        )
        result["merge_train_stack_collapse_plan_record_id"] = executed_record.record_id
        result["stack_collapse_plan"] = executed_plan.model_dump(mode="json")
    else:
        result["stack_collapse_plan"] = planned_collapse_record.plan.model_dump(mode="json")
    return result


def _advance_from_live_snapshot(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    transport: MergeTrainGitHubTransport,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    trace_id: str,
    recorded_at: str,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object]:
    snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
        repository=request.repository,
        base_branch=request.base_branch,
    )
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    selected_pr = dry_run_result.selected_pr
    if selected_pr is not None and merge_train_snapshot_has_stack_topology(
        snapshot=snapshot, dry_run_result=dry_run_result
    ):
        stack_discovery = discover_merge_train_stack(
            snapshot=snapshot,
            root_pull_request_number=selected_pr.number,
        )
    else:
        stack_discovery = None
    if stack_discovery is not None and stack_discovery.status == "ready_for_collapse":
        controller_action = "plan_stack_collapse"
        stack_collapse_plan = build_merge_train_stack_collapse_plan(
            discovery_result=stack_discovery,
            policy_key=dry_run_result.policy_key,
            policy_sha256=policy_sha256,
            created_at=recorded_at,
        )
        result: dict[str, object] = {
            "repository": stack_collapse_plan.repository,
            "base_branch": stack_collapse_plan.base_branch,
            "mode": "dry-run" if not request.mutate else controller_action,
            "controller_action": controller_action,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
            "stack_discovery": stack_discovery.model_dump(mode="json"),
            "stack_collapse_plan": stack_collapse_plan.model_dump(mode="json"),
        }
        if request.mutate:
            lease.checkpoint(
                active_action=controller_action,
                active_phase="persist_stack_collapse_plan",
                active_record_id="",
                active_pull_request_number=stack_collapse_plan.root_pull_request_number,
                step_payload={"collapse_id": stack_collapse_plan.collapse_id},
            )
            stack_collapse_record = build_merge_train_stack_collapse_plan_record(
                plan=stack_collapse_plan,
                source=f"service:controller:stack-collapse-plan:{trace_id}",
                updated_at=recorded_at,
            )
            stack_collapse_store.write_merge_train_stack_collapse_plan_record(stack_collapse_record)
            result["merge_train_stack_collapse_plan_record_id"] = stack_collapse_record.record_id
            lease.checkpoint(
                active_action=controller_action,
                active_phase="stack_collapse_plan_recorded",
                active_record_id=stack_collapse_record.record_id,
                active_pull_request_number=stack_collapse_plan.root_pull_request_number,
                step_payload={
                    "stack_collapse_plan_record_id": stack_collapse_record.record_id,
                    "collapse_id": stack_collapse_plan.collapse_id,
                },
            )
        return result
    if stack_discovery is not None and stack_discovery.status == "unsupported":
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "stack_unsupported",
            "dry_run_result": dry_run_result.model_dump(mode="json"),
            "stack_discovery": stack_discovery.model_dump(mode="json"),
        }
    if dry_run_result.intended_next_action == "idle":
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "idle",
            "dry_run_result": dry_run_result.model_dump(mode="json"),
        }
    if dry_run_result.intended_next_action != "merge":
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": dry_run_result.intended_next_action,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
        }

    controller_action = "plan_candidate"
    candidate = build_merge_train_batch_candidate(
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
        policy_sha256=policy_sha256,
        created_at=recorded_at,
    )
    result = {
        "repository": candidate.repository,
        "base_branch": candidate.base_branch,
        "mode": "dry-run" if not request.mutate else controller_action,
        "controller_action": controller_action,
        "dry_run_result": dry_run_result.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
    }
    if request.mutate:
        lease.checkpoint(
            active_action=controller_action,
            active_phase="persist_candidate_plan",
            active_record_id="",
            active_pull_request_number=None,
            step_payload={
                "batch_id": candidate.batch_id,
                "candidate_ref": candidate.candidate_ref,
            },
        )
        candidate_record = build_merge_train_batch_candidate_record(
            candidate=candidate,
            source=f"service:controller:candidate-plan:{trace_id}",
            updated_at=recorded_at,
        )
        candidate_store.write_merge_train_batch_candidate_record(candidate_record)
        result["merge_train_batch_candidate_record_id"] = candidate_record.record_id
        lease.checkpoint(
            active_action=controller_action,
            active_phase="candidate_plan_recorded",
            active_record_id=candidate_record.record_id,
            active_pull_request_number=None,
            step_payload={
                "candidate_record_id": candidate_record.record_id,
                "batch_id": candidate.batch_id,
                "candidate_ref": candidate.candidate_ref,
            },
        )
    return result


def stale_merge_train_landing_plan(
    landing_plan: MergeTrainBatchLandingPlan,
) -> MergeTrainBatchLandingPlan:
    entries = tuple(
        type(entry).model_validate({**entry.model_dump(mode="python"), "status": "stale"})
        if entry.status in {"planned", "merging"}
        else entry
        for entry in landing_plan.entries
    )
    return MergeTrainBatchLandingPlan.model_validate(
        {**landing_plan.model_dump(mode="python"), "entries": entries}
    )


def try_reflow_failed_merge_train_candidate(
    *,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    active_candidate_record: MergeTrainBatchCandidateRecord,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    transport: MergeTrainGitHubTransport,
    repository: str,
    base_branch: str,
    recorded_at: str,
    trace_id: str,
    mutate: bool,
) -> dict[str, object] | None:
    try:
        snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
            repository=repository,
            base_branch=base_branch,
        )
    except Exception:
        return None
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    if dry_run_result.intended_next_action != "merge":
        return None
    queue_unchanged = _merge_train_candidate_matches_dry_run_queue(
        candidate=active_candidate_record.candidate,
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
    )
    if queue_unchanged and active_candidate_record.candidate.candidate_sha:
        return None
    candidate = build_merge_train_batch_candidate(
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
        policy_sha256=policy_sha256,
        created_at=recorded_at,
    )
    result: dict[str, object] = {
        "repository": candidate.repository,
        "base_branch": candidate.base_branch,
        "mode": "dry-run" if not mutate else "plan_candidate",
        "controller_action": "plan_candidate",
        "superseded_merge_train_batch_candidate_record_id": active_candidate_record.record_id,
        "dry_run_result": dry_run_result.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
    }
    if mutate:
        candidate_record = build_merge_train_batch_candidate_record(
            candidate=candidate,
            source=f"service:controller:candidate-reflow:{trace_id}",
            updated_at=recorded_at,
        )
        candidate_store.write_merge_train_batch_candidate_record(candidate_record)
        try:
            _supersede_active_merge_train_batch_candidate_records(
                record_store=candidate_store,
                repository=repository,
                base_branch=base_branch,
                batch_id=active_candidate_record.candidate.batch_id,
                replacement_record_id=candidate_record.record_id,
            )
        except Exception:
            _supersede_merge_train_batch_candidate_record(
                record_store=candidate_store,
                record=candidate_record,
            )
            raise
        result["merge_train_batch_candidate_record_id"] = candidate_record.record_id
    return result


def validate_merge_train_candidate_record_for_controller(
    *,
    candidate_record: MergeTrainBatchCandidateRecord,
    policy_key: str,
    policy_sha256: str,
) -> None:
    if candidate_record.candidate.policy_key != policy_key:
        raise ValueError("merge train candidate policy key no longer matches")
    if candidate_record.candidate.policy_sha256 != policy_sha256:
        raise ValueError("merge train candidate policy digest no longer matches")


def validate_merge_train_landing_record_for_controller(
    *,
    landing_record: MergeTrainBatchLandingPlanRecord,
    policy_key: str,
    policy_sha256: str,
) -> None:
    if landing_record.landing_plan.policy_key != policy_key:
        raise ValueError("merge train landing plan policy key no longer matches")
    if landing_record.landing_plan.policy_sha256 != policy_sha256:
        raise ValueError("merge train landing plan policy digest no longer matches")


def validate_merge_train_stack_collapse_record_for_controller(
    *,
    collapse_record: MergeTrainStackCollapsePlanRecord,
    policy_key: str,
    policy_sha256: str,
) -> None:
    if collapse_record.plan.policy_key != policy_key:
        raise ValueError("merge train stack collapse policy key no longer matches")
    if collapse_record.plan.policy_sha256 != policy_sha256:
        raise ValueError("merge train stack collapse policy digest no longer matches")


def cleanup_merge_train_batch_candidate_ref(
    *,
    github_client: GitHubMergeTrainClient,
    landing_plan: MergeTrainBatchLandingPlan,
    trace_id: str,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object]:
    candidate_ref = str(
        lease.record.step_payload.get("candidate_ref") or landing_plan.candidate_ref
    )
    if lease.record.step_payload.get("cleanup_status") == "deleted":
        return {"candidate_ref_cleanup_status": "deleted"}
    if not github_client.candidate_ref_exists(
        repository=landing_plan.repository,
        reference=candidate_ref,
    ):
        lease.checkpoint(
            step_payload={
                **lease.record.step_payload,
                "candidate_ref": candidate_ref,
                "cleanup_status": "already_missing",
            },
        )
        return {"candidate_ref_cleanup_status": "already_missing"}
    try:
        deleted = github_client.cleanup_batch_candidate_ref(landing_plan=landing_plan)
    except MergeTrainGitHubError as error:
        message = str(error).strip() or "GitHub candidate ref cleanup failed."
        _LOGGER.warning(
            "Merge train candidate ref cleanup failed after landing persistence",
            extra={
                "trace_id": trace_id,
                "repository": landing_plan.repository,
                "base_branch": landing_plan.base_branch,
                "candidate_ref": landing_plan.candidate_ref,
                "github_status_code": error.status_code,
            },
        )
        result: dict[str, object] = {
            "candidate_ref_cleanup_status": "failed",
            "candidate_ref_cleanup_message": message,
        }
        if error.status_code is not None:
            result["candidate_ref_cleanup_github_status_code"] = error.status_code
        return result
    cleanup_status = "deleted" if deleted else "already_missing"
    lease.checkpoint(
        step_payload={
            **lease.record.step_payload,
            "candidate_ref": candidate_ref,
            "cleanup_status": cleanup_status,
        },
    )
    return {
        "candidate_ref_cleanup_status": cleanup_status,
    }


def _merge_train_stack_collapse_record_matches_landing_plan(
    *,
    collapse_record: MergeTrainStackCollapsePlanRecord,
    landing_plan: MergeTrainBatchLandingPlan,
    policy_sha256: str,
) -> bool:
    try:
        validate_stack_collapse_record_for_landing(
            collapse_record=collapse_record,
            landing_plan=landing_plan,
            policy_sha256=policy_sha256,
        )
    except ValueError:
        return False
    return True


def _merge_train_candidate_matches_dry_run_queue(
    *, candidate: MergeTrainBatchCandidate, dry_run_result: MergeTrainDryRunResult, base_sha: str
) -> bool:
    if candidate.repository != dry_run_result.repository:
        return False
    if candidate.base_branch != dry_run_result.base_branch:
        return False
    if candidate.base_sha != base_sha:
        return False
    candidate_entries = tuple(
        (entry.pull_request_number, entry.head_sha) for entry in candidate.entries
    )
    queue_by_number = {entry.number: entry for entry in dry_run_result.queue}
    current_entries: list[tuple[int, str]] = []
    for pull_request_number in dry_run_result.queue_order:
        queue_entry = queue_by_number[pull_request_number]
        if not queue_entry.eligible:
            return False
        current_entries.append((queue_entry.number, queue_entry.head_sha))
    return candidate_entries == tuple(current_entries)


def _supersede_active_merge_train_batch_candidate_records(
    *,
    record_store: MergeTrainBatchCandidateRecordStore,
    repository: str,
    base_branch: str,
    batch_id: str,
    replacement_record_id: str | None,
) -> None:
    records = record_store.list_merge_train_batch_candidate_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
    )
    for record in records:
        if replacement_record_id is not None and record.record_id == replacement_record_id:
            continue
        if record.candidate.batch_id != batch_id:
            continue
        _supersede_merge_train_batch_candidate_record(
            record_store=record_store,
            record=record,
        )


def _supersede_merge_train_batch_candidate_record(
    *,
    record_store: MergeTrainBatchCandidateRecordStore,
    record: MergeTrainBatchCandidateRecord,
) -> None:
    record_store.write_merge_train_batch_candidate_record(
        MergeTrainBatchCandidateRecord.model_validate(
            {**record.model_dump(mode="python"), "status": "superseded"}
        )
    )


def _records_for_result(result: dict[str, object]) -> dict[str, str]:
    record_keys = (
        "merge_train_batch_candidate_record_id",
        "merge_train_batch_landing_plan_record_id",
        "merge_train_stack_collapse_plan_record_id",
        "superseded_merge_train_batch_candidate_record_id",
    )
    return {
        key: value for key in record_keys if isinstance((value := result.get(key)), str) and value
    }


def _controller_result_requires_reconciliation(result: dict[str, object]) -> bool:
    if result.get("controller_reconciliation_status") == "required":
        return True
    if result.get("candidate_ref_cleanup_status") == "failed":
        return True
    stack_collapse_plan = result.get("stack_collapse_plan")
    return isinstance(stack_collapse_plan, dict) and stack_collapse_plan.get("status") == "blocked"


def _controller_result_reconciliation_detail(
    *,
    result: dict[str, object],
    current_detail: str,
) -> str:
    if result.get("controller_reconciliation_status") == "required":
        return current_detail
    if result.get("candidate_ref_cleanup_status") == "failed":
        return "retryable:candidate_ref_cleanup_failed"
    return "operator_required:controller_step_blocked"


def _controller_exception_reconciliation_detail(error: Exception) -> str:
    if isinstance(error, MergeTrainGitHubError):
        if error.status_code is None or error.status_code >= 500:
            return "retryable:github_request_failed"
        return "operator_required:github_request_rejected"
    if isinstance(error, (MergeTrainControllerRequestError, ValueError)):
        return "operator_required:invalid_controller_state"
    return f"operator_required:unexpected:{type(error).__name__}"


def merge_train_controller_lease_owner(*, trace_id: str) -> str:
    normalized_trace_id = trace_id.strip()
    if not normalized_trace_id:
        raise ValueError("merge train controller lease owner requires trace_id")
    return f"merge-train-controller:{normalized_trace_id}"


def latest_merge_train_controller_state_record(
    *,
    record_store: MergeTrainControllerStateRecordStore,
    repository: str,
    base_branch: str,
) -> MergeTrainControllerStateRecord | None:
    records = record_store.list_merge_train_controller_state_records(
        repository=repository,
        base_branch=base_branch,
        limit=1,
    )
    return records[0] if records else None


def require_merge_train_controller_state_record_store(
    record_store: object,
) -> MergeTrainControllerStateRecordStore:
    required_methods = (
        "list_merge_train_controller_state_records",
        "acquire_merge_train_controller_state_record",
        "compare_and_set_merge_train_controller_state_record",
    )
    if all(hasattr(record_store, method_name) for method_name in required_methods):
        return cast(MergeTrainControllerStateRecordStore, record_store)
    raise TypeError("record store does not support merge train controller state records")


def active_merge_train_controller_state_record(
    *,
    record_store: MergeTrainControllerStateRecordStore,
    repository: str,
    base_branch: str,
) -> MergeTrainControllerStateRecord | None:
    record = latest_merge_train_controller_state_record(
        record_store=record_store,
        repository=repository,
        base_branch=base_branch,
    )
    if record is None or record.status != "running":
        return None
    return record


def update_merge_train_controller_state(
    *,
    record_store: MergeTrainControllerStateRecordStore,
    current_record: MergeTrainControllerStateRecord,
    lease_owner: str,
    lease_acquired_at: str,
    lease_seconds: int = DEFAULT_MERGE_TRAIN_CONTROLLER_LEASE_SECONDS,
    **updates: object,
) -> MergeTrainControllerStateRecord:
    next_record = current_record.model_copy(
        update={
            **updates,
        }
    )
    return record_store.compare_and_set_merge_train_controller_state_record(
        record=next_record,
        expected_lease_owner=lease_owner,
        expected_lease_acquired_at=lease_acquired_at,
        lease_seconds=lease_seconds,
    )


def release_merge_train_controller_lease(
    *,
    record_store: MergeTrainControllerStateRecordStore,
    current_record: MergeTrainControllerStateRecord,
    lease_owner: str,
    lease_acquired_at: str,
    lease_seconds: int = DEFAULT_MERGE_TRAIN_CONTROLLER_LEASE_SECONDS,
    reconciliation_status: str = "clean",
    reconciliation_detail: str = "",
    clear_active_state: bool = True,
) -> MergeTrainControllerStateRecord:
    released_record = current_record.model_copy(
        update={
            "status": "idle" if reconciliation_status == "clean" else "reconcile_required",
            "lease_owner": "",
            "lease_acquired_at": "",
            "lease_expires_at": "",
            "heartbeat_at": "",
            "active_action": "" if clear_active_state else current_record.active_action,
            "active_phase": "" if clear_active_state else current_record.active_phase,
            "active_record_id": "" if clear_active_state else current_record.active_record_id,
            "active_pull_request_number": (
                None if clear_active_state else current_record.active_pull_request_number
            ),
            "step_payload": {} if clear_active_state else current_record.step_payload,
            "last_owner": lease_owner,
            "last_action": current_record.active_action,
            "last_phase": current_record.active_phase,
            "last_record_id": current_record.active_record_id,
            "last_pull_request_number": current_record.active_pull_request_number,
            "last_transition_at": current_record.updated_at,
            "reconciliation_status": reconciliation_status,
            "reconciliation_detail": reconciliation_detail,
        }
    )
    return record_store.compare_and_set_merge_train_controller_state_record(
        record=released_record,
        expected_lease_owner=lease_owner,
        expected_lease_acquired_at=lease_acquired_at,
        lease_seconds=lease_seconds,
    )
