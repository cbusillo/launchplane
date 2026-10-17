import hashlib
from control_plane.merge_train_policy_binding import merge_train_policy_bindings_match
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
import logging
from typing import TYPE_CHECKING, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    MergeTrainBatchEntry,
    MergeTrainBatchHeldOutEntry,
    MergeTrainBatchLandingEntry,
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_candidate,
    build_merge_train_batch_candidate_ref,
    build_merge_train_batch_candidate_record,
    build_merge_train_batch_landing_plan,
    build_merge_train_batch_landing_plan_record,
    dependency_not_ready_holds,
)
from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerLeaseLostError,
    MergeTrainControllerReconciliationRequiredError,
    MergeTrainControllerStateRecord,
    build_merge_train_controller_state_record,
)
from control_plane.contracts.merge_train_effect import (
    MergeTrainEffectLineage,
    MergeTrainSemanticEffectExecutor,
    StackChildCommentEffect,
    StackChildLabelEffect,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicy, MergeTrainRepositoryPolicy
from control_plane.contracts.merge_train_historical_completion import (
    MergeTrainHistoricalCompletionSelector,
)
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
    MergeTrainStackChildNotReadyError,
    build_merge_train_stack_collapse_plan,
    build_merge_train_stack_collapse_plan_record,
    execute_merge_train_stack_collapse_plan,
    reconcile_merge_train_stack_children_after_root_landing,
)
from control_plane.contracts.merge_train_structural_provenance import (
    MergeTrainStackCollapseRootProof,
)
from control_plane.merge_train import (
    apply_merge_train_branch_update_intent,
    apply_merge_train_block_intent,
    MergeTrainDryRunResult,
    MergeTrainDryRunSnapshot,
    MergeTrainQueueEntry,
    build_merge_train_dry_run_result,
    discover_merge_train_stack,
    merge_train_stack_child_readiness_check,
    merge_train_snapshot_of,
    merge_train_stack_child_readiness_reasons,
)
from control_plane.merge_admission import (
    GuardedMergeAdmission,
    MergeAdmissionDeniedError,
    MergeAdmissionEvaluator,
    MergeAdmissionRecordStore,
)
from control_plane.merge_train_batch_pull_request import (
    bound_closed_batch_pull_request,
    has_batch_pull_request,
    changed_closed_batch_body,
)

from control_plane.merge_train_batch_candidate import (
    MergeTrainBatchCandidateBaseHistoryReadStore,
    MergeTrainBatchCandidateRecordStore,
    merge_train_snapshot_has_stack_topology,
)
from control_plane.merge_train_batch_landing import (
    MergeTrainBatchLandingPlanRecordStore,
    validate_stack_collapse_record_for_landing,
)
from control_plane.merge_train_branch_refresh import (
    MergeTrainBranchRefreshWriteStore,
    merge_train_branch_refresh_recorder,
    require_merge_train_client_review_read_store,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubCandidateEntryConflictError,
    MergeTrainGitHubError,
    MergeTrainGitHubMergeRejectedError,
    MergeTrainGitHubStaleHeadError,
    MergeTrainGitHubTransport,
    UrllibMergeTrainGitHubTransport,
    merge_train_conflict_probe_ref,
    merge_train_construction_ref,
)
from control_plane.merge_train_original_review import (
    ORIGINAL_REVIEW_RECOVERY_CODE,
    original_review_recovery_records,
    original_review_recovery_details,
)
from control_plane.merge_train_diagnostics import controller_last_result
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.merge_train_stack_disposition import resolve_stack_child_disposition_label
from control_plane.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecordStore,
    stack_collapse_expected_root_head_sha,
)
from control_plane.merge_train_structural_provenance import (
    ordinary_candidate_is_exact_landing_dependency,
)
from control_plane.workflows.merge_train_controller import (
    latest_completed_merge_train_batch_landing_plan_record as latest_completed_merge_train_batch_landing_progress_record,
    latest_merge_train_batch_candidate_progress_record,
    latest_merge_train_batch_landing_progress_record,
    latest_merge_train_stack_collapse_progress_record,
)


if TYPE_CHECKING:
    from control_plane.merge_train_release_hold import MergeTrainReleaseHoldReader

_LOGGER = logging.getLogger(__name__)
_QUEUE_RETRY_RETIREMENT_SOURCE = "service:controller:queue-retry-retirement:"
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
    "update_branch",
)


class MergeTrainControllerRunOnceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str = "main"
    mutate: bool = False
    github_api_base_url: str = "https://api.github.com"
    historical_completion: MergeTrainHistoricalCompletionSelector | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

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


class _OrdinaryCandidateDependencyStore(Protocol):
    def list_ordinary_merge_train_batch_candidate_dependencies(
        self,
        *,
        landing_plan_record: MergeTrainBatchLandingPlanRecord,
    ) -> tuple[MergeTrainBatchCandidateRecord, ...]: ...


@dataclass(frozen=True)
class MergeTrainControllerRunOnceResult:
    accepted_result: dict[str, object]
    records: dict[str, str]


@dataclass
class MergeTrainControllerLeaseContext:
    record: MergeTrainControllerStateRecord
    record_store: MergeTrainControllerStateRecordStore | None = None
    lease_seconds: int = DEFAULT_MERGE_TRAIN_CONTROLLER_LEASE_SECONDS
    observations: dict[str, str] = field(default_factory=dict)

    def observe(self, **evidence: str) -> None:
        self.observations.update(evidence)

    def observe_failed_candidate(self, record: MergeTrainBatchCandidateRecord) -> None:
        candidate = record.candidate
        self.observations.update(
            source_failed_record_id=record.record_id,
            candidate_batch_id=candidate.batch_id,
            candidate_sha=candidate.candidate_sha,
            base_sha=candidate.base_sha,
            check_retry_of=candidate.check_retry_of,
            infrastructure_retry_of=candidate.infrastructure_retry_of,
            batch_body_retry_of=candidate.batch_body_retry_of,
        )

    def record_result(
        self,
        result: dict[str, object],
        *,
        pass_started_at: str,
        trace_id: str,
        reconciliation_required: bool,
        policy_key: str = "",
        policy_sha256: str = "",
    ) -> None:
        diagnosis, progress_key = controller_last_result(
            result=result,
            state=self.record,
            observations=self.observations,
            pass_started_at=pass_started_at,
            observed_at=utc_now_timestamp(),
            trace_id=trace_id,
            policy_key=policy_key or self.record.policy_key,
            policy_sha256=policy_sha256 or self.record.policy_sha256,
            reconciliation_required=reconciliation_required,
            persisted=self.record_store is not None,
        )
        if self.record_store is not None:
            # The existing fenced release persists this with the lease outcome.
            # Diagnostic metadata adds no renewal or independent write.
            self.record = self.record.model_copy(
                update={"last_result": diagnosis, "semantic_progress_key": progress_key}
            )
        result["last_controller_result"] = diagnosis.model_dump(mode="json")

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
            lease.record_result(
                {
                    "controller_action": lease.record.active_action,
                    "reason_code": _controller_exception_reconciliation_detail(error).split(";")[0],
                },
                pass_started_at=controller_state.updated_at,
                trace_id=trace_id,
                reconciliation_required=True,
            )
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
    release_hold_reader: "MergeTrainReleaseHoldReader | None" = None,
    effect_executor: MergeTrainSemanticEffectExecutor | None = None,
    branch_refresh_store: MergeTrainBranchRefreshWriteStore | None = None,
) -> MergeTrainControllerRunOnceResult:
    transport = UrllibMergeTrainGitHubTransport(
        token=token,
        api_base_url=request.github_api_base_url,
    )
    review_store = require_merge_train_client_review_read_store(
        branch_refresh_store or candidate_store, route="Controller"
    )
    github_client = GitHubMergeTrainClient(
        transport=transport,
        effect_executor=effect_executor,
        branch_refresh_store=review_store,
        branch_refresh_recorder=(
            merge_train_branch_refresh_recorder(
                store=branch_refresh_store,
                base_branch=request.base_branch,
                trace_id=trace_id,
            )
            if branch_refresh_store is not None
            else None
        ),
    )
    return execute_merge_train_controller_with_client(
        request=request,
        policy=policy,
        policy_sha256=policy_sha256,
        repository_policy=repository_policy,
        github_client=github_client,
        trace_id=trace_id,
        recorded_at=recorded_at,
        candidate_store=candidate_store,
        landing_store=landing_store,
        stack_collapse_store=stack_collapse_store,
        controller_state_store=controller_state_store,
        admission_store=admission_store,
        admission_evaluator=admission_evaluator,
        before_release=before_release,
        release_hold_reader=release_hold_reader,
    )


def execute_merge_train_controller_with_client(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    github_client: GitHubMergeTrainClient,
    trace_id: str,
    recorded_at: str,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    controller_state_store: MergeTrainControllerStateRecordStore,
    admission_store: MergeAdmissionRecordStore,
    admission_evaluator: MergeAdmissionEvaluator,
    before_release: Callable[[MergeTrainControllerRunOnceResult], None] | None = None,
    release_hold_reader: "MergeTrainReleaseHoldReader | None" = None,
) -> MergeTrainControllerRunOnceResult:
    github_client.begin_snapshot_pass(policy=repository_policy, read_only=not request.mutate)
    try:
        return _execute_merge_train_controller_with_client(
            request=request,
            policy=policy,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            github_client=github_client,
            trace_id=trace_id,
            recorded_at=recorded_at,
            candidate_store=candidate_store,
            landing_store=landing_store,
            stack_collapse_store=stack_collapse_store,
            controller_state_store=controller_state_store,
            admission_store=admission_store,
            admission_evaluator=admission_evaluator,
            before_release=before_release,
            release_hold_reader=release_hold_reader,
        )
    finally:
        github_client.end_snapshot_pass()


def _execute_merge_train_controller_with_client(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    github_client: GitHubMergeTrainClient,
    trace_id: str,
    recorded_at: str,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    controller_state_store: MergeTrainControllerStateRecordStore,
    admission_store: MergeAdmissionRecordStore,
    admission_evaluator: MergeAdmissionEvaluator,
    before_release: Callable[[MergeTrainControllerRunOnceResult], None] | None = None,
    release_hold_reader: "MergeTrainReleaseHoldReader | None" = None,
) -> MergeTrainControllerRunOnceResult:
    """Internal controller core with an explicit caller-owned scoped provider client.

    This constructs no transport or credentials. Ordinary callers must supply
    joined bound-record adapters and their scoped semantic executor; the legacy
    entry point above retains its established token and transport behavior.
    """
    if request.historical_completion is not None:
        raise MergeTrainControllerRequestError(
            "historical_completion_requires_legacy_service_preflight"
        )
    transport = github_client.transport
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
            candidate_store=candidate_store,
            landing_store=landing_store,
            stack_collapse_store=stack_collapse_store,
            admission_store=admission_store,
            lease=lease,
        )
        if result is not None and not request.mutate:
            history = stack_collapse_store.list_merge_train_stack_collapse_plan_records(
                repository=request.repository, base_branch=request.base_branch
            )
            # Completed landing cleanup resumes from its exact stored proof;
            # it does not perform fresh queue discovery.
            if history and lease.record.active_action == "land_batch":
                landing = _merge_train_landing_record_by_id(
                    record_store=landing_store,
                    repository=request.repository,
                    base_branch=request.base_branch,
                    record_id=lease.record.active_record_id,
                )
                if (
                    landing is not None
                    and latest_completed_merge_train_batch_landing_plan_record(
                        record_store=landing_store,
                        repository=request.repository,
                        base_branch=request.base_branch,
                        batch_id=landing.landing_plan.batch_id,
                        candidate_sha=landing.landing_plan.candidate_sha,
                        policy_sha256=landing.landing_plan.policy_sha256,
                        include_lineage_retirements=True,
                    )
                    is not None
                ):
                    history = ()
            if history:
                snapshot = github_client.read_merge_train_snapshot(
                    repository=request.repository, base_branch=request.base_branch
                )
                review_recovery = original_review_recovery_records(
                    records=history, snapshot=snapshot, ancestry=github_client
                )
                if review_recovery:
                    prior_details = result.get("details")
                    if isinstance(prior_details, dict) and prior_details.get("code"):
                        result["details"] = {
                            **prior_details,
                            "original_review_recovery": original_review_recovery_details(
                                review_recovery
                            ),
                        }
                    else:
                        result["details"] = original_review_recovery_details(review_recovery)
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
                    release_hold_reader=release_hold_reader,
                )
        lease.record_result(
            result,
            pass_started_at=recorded_at,
            trace_id=trace_id,
            reconciliation_required=_controller_result_requires_reconciliation(result),
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
        )
    except MergeTrainControllerLeaseLostError:
        raise
    except Exception as error:
        try:
            lease.record_result(
                {
                    "controller_action": lease.record.active_action,
                    "reason_code": _controller_exception_reconciliation_detail(error).split(";")[0],
                },
                pass_started_at=recorded_at,
                trace_id=trace_id,
                reconciliation_required=True,
                policy_key=repository_policy.policy_key,
                policy_sha256=policy_sha256,
            )
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
    _expose_persisted_conflict_holds(result)
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
                    lease.record_result(
                        {
                            **result,
                            "reason_code": _controller_exception_reconciliation_detail(error).split(
                                ";"
                            )[0],
                        },
                        pass_started_at=recorded_at,
                        trace_id=trace_id,
                        reconciliation_required=True,
                        policy_key=repository_policy.policy_key,
                        policy_sha256=policy_sha256,
                    )
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
    candidate_store: MergeTrainBatchCandidateRecordStore,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    admission_store: MergeAdmissionRecordStore,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object] | None:
    # An external merge is not a prior train effect. Observe it under the
    # existing lease instead of asking admission recovery to invent authority.
    if (
        lease.record.ordinary_job_binding is None
        and lease.record.active_action == "land_batch"
        and (
            "MergeAdmissionReconciliationRequiredError" in lease.record.reconciliation_detail
            or lease.record.step_payload.get("external_landing_source_record_id")
        )
    ):
        recovered = _recover_external_stack_landing(
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
            lease=lease,
        )
        if recovered is not None:
            return recovered
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
        "retire_stale_policy_landing",
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
            policy_sha256=planned_record.landing_plan.policy_sha256,
            include_lineage_retirements=True,
        )
        if landed_record is None:
            if lease.record.active_phase == "retire_stale_policy_landing":
                return _resume_unrecorded_landing_retirement(
                    trace_id=trace_id,
                    recorded_at=recorded_at,
                    github_client=github_client,
                    candidate_store=candidate_store,
                    landing_store=landing_store,
                    admission_store=admission_store,
                    landing_record=planned_record,
                    lease=lease,
                )
            return None
        if lease.record.active_phase == "retire_stale_policy_landing":
            return _finish_retired_policy_landing(
                candidate_store=candidate_store,
                retired_record=landed_record,
                lease=lease,
            )
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
        "annotate_historical_stack_child",
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
        policy_sha256=latest_record.candidate.policy_sha256,
    )
    if completed_landing_record is not None:
        return None
    return latest_record


def _candidate_record_for_landing_plan(
    *,
    record_store: MergeTrainBatchCandidateRecordStore,
    landing_plan_record: MergeTrainBatchLandingPlanRecord,
) -> MergeTrainBatchCandidateRecord | None:
    landing_plan = landing_plan_record.landing_plan
    if landing_plan_record.ordinary_job_binding is not None:
        if not hasattr(record_store, "list_ordinary_merge_train_batch_candidate_dependencies"):
            return None
        dependencies = cast(
            _OrdinaryCandidateDependencyStore,
            record_store,
        ).list_ordinary_merge_train_batch_candidate_dependencies(
            landing_plan_record=landing_plan_record,
        )
        matches = tuple(
            record
            for record in dependencies
            if ordinary_candidate_is_exact_landing_dependency(
                candidate_record=record,
                landing_plan_record=landing_plan_record,
            )
        )
        return matches[0] if len(matches) == 1 else None
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
    if not any(
        entry.status == "planned" for entry in latest_record.landing_plan.entries
    ) and not _ordinary_landing_terminal_success(latest_record):
        return None
    return latest_record


def _ordinary_landing_terminal_success(record: MergeTrainBatchLandingPlanRecord) -> bool:
    return (
        record.ordinary_job_binding is not None
        and bool(record.landing_plan.entries)
        and all(entry.status in {"merged", "skipped"} for entry in record.landing_plan.entries)
    )


def _landing_cleanup_allowed(record: MergeTrainBatchLandingPlanRecord) -> bool:
    allowed_statuses = (
        {"merged", "skipped"} if record.ordinary_job_binding is not None else {"merged"}
    )
    return bool(record.landing_plan.entries) and all(
        entry.status in allowed_statuses for entry in record.landing_plan.entries
    )


def latest_completed_merge_train_batch_landing_plan_record(
    *,
    record_store: MergeTrainBatchLandingPlanRecordStore,
    repository: str,
    base_branch: str,
    batch_id: str,
    candidate_sha: str,
    policy_sha256: str,
    include_lineage_retirements: bool = False,
) -> MergeTrainBatchLandingPlanRecord | None:
    """Return the terminal landing record for this batch candidate, if any.

    A lineage-change retirement had no provider effect, so it does not stop the
    same batch from landing when the queue returns to it; only resuming that
    retirement looks it up (#2843).
    """
    records = record_store.list_merge_train_batch_landing_plan_records(
        repository=repository,
        base_branch=base_branch,
        status="active",
        limit=25,
    )
    return latest_completed_merge_train_batch_landing_progress_record(
        landing_plan_records=tuple(
            record
            for record in records
            if (
                merge_train_policy_bindings_match(
                    record_store=record_store,
                    policy_key=record.landing_plan.policy_key,
                    recorded_sha256=record.landing_plan.policy_sha256,
                    current_sha256=policy_sha256,
                )
            )
            and (
                include_lineage_retirements
                or not record.source.startswith(_LINEAGE_RETIREMENT_SOURCE_PREFIX)
            )
        ),
        batch_id=batch_id,
        candidate_sha=candidate_sha,
    )


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
            record_store=record_store,
        )
    )
    return latest_merge_train_stack_collapse_progress_record(compatible_records)


def _group_stack_collapse_records(
    records: tuple[MergeTrainStackCollapsePlanRecord, ...],
) -> dict[str, list[MergeTrainStackCollapsePlanRecord]]:
    groups: dict[str, list[MergeTrainStackCollapsePlanRecord]] = {}
    for record in records:
        groups.setdefault(record.plan.collapse_id, []).append(record)
    return groups


def stack_collapse_records_for_completed_landing(
    *,
    record_store: MergeTrainStackCollapsePlanRecordStore,
    github_client: GitHubMergeTrainClient,
    repository: str,
    base_branch: str,
    landing_plan: MergeTrainBatchLandingPlan,
    policy_sha256: str,
) -> tuple[MergeTrainStackCollapsePlanRecord, ...]:
    """Recover each collapse carried by a merged root, including retired waits."""
    merged_root_heads = {
        entry.pull_request_number: entry.expected_head_sha
        for entry in landing_plan.entries
        if entry.status == "merged"
    }
    # Include retired waits: a fixed history limit could hide another merged root.
    records = record_store.list_merge_train_stack_collapse_plan_records(
        repository=repository, base_branch=base_branch
    )
    contained: dict[tuple[str, str], bool] = {}
    landed_records: list[MergeTrainStackCollapsePlanRecord] = []
    for progress_records in _group_stack_collapse_records(records).values():
        record = latest_merge_train_stack_collapse_progress_record(tuple(progress_records))
        if record is None or not (
            record.plan.status in {"waiting_for_root_checks", "ready_for_train"}
            and record.plan.policy_key == landing_plan.policy_key
            and (
                merge_train_policy_bindings_match(
                    record_store=record_store,
                    policy_key=record.plan.policy_key,
                    recorded_sha256=record.plan.policy_sha256,
                    current_sha256=policy_sha256,
                )
                and merge_train_policy_bindings_match(
                    record_store=record_store,
                    policy_key=landing_plan.policy_key,
                    recorded_sha256=policy_sha256,
                    current_sha256=landing_plan.policy_sha256,
                )
            )
            and record.plan.root_pull_request_number in merged_root_heads
        ):
            continue
        lineage = (
            stack_collapse_expected_root_head_sha(record.plan),
            merged_root_heads[record.plan.root_pull_request_number],
        )
        if lineage not in contained:
            contained[lineage] = lineage[0] == lineage[1] or github_client.branch_contains_commit(
                repository=repository, branch_ref=lineage[1], commit_sha=lineage[0]
            )
        if contained[lineage]:
            landed_records.append(record)
    # A later collapse of the same root replaces old child-head expectations.
    # Replaying both would reject a child legitimately updated before re-collapse.
    records_by_root: dict[int, MergeTrainStackCollapsePlanRecord] = {}
    for record in landed_records:
        root_number = record.plan.root_pull_request_number
        previous = records_by_root.get(root_number)
        if previous is None or (record.plan.created_at, record.updated_at, record.record_id) > (
            previous.plan.created_at,
            previous.updated_at,
            previous.record_id,
        ):
            records_by_root[root_number] = record
    return tuple(records_by_root.values())


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
    history = stack_collapse_store.list_merge_train_stack_collapse_plan_records(
        repository=request.repository, base_branch=request.base_branch
    )
    landing_numbers = {
        entry.pull_request_number for entry in active_landing_record.landing_plan.entries
    }
    history = tuple(
        record
        for record in history
        if any(entry.pull_request_number in landing_numbers for entry in record.plan.entries)
    )
    if history and not _ordinary_landing_terminal_success(active_landing_record):
        snapshot = github_client.read_merge_train_snapshot(
            repository=request.repository, base_branch=request.base_branch
        )
        review_recovery = original_review_recovery_records(
            records=history, snapshot=snapshot, ancestry=github_client, raise_on_unavailable=True
        )
        affected = {
            entry.pull_request_number for record in review_recovery for entry in record.plan.entries
        }
        if any(
            entry.pull_request_number in affected
            for entry in active_landing_record.landing_plan.entries
        ):
            if (
                active_landing_record.ordinary_job_binding is None
                and active_landing_record.landing_plan.policy_key == repository_policy.policy_key
                and all(
                    entry.status == "planned"
                    for entry in active_landing_record.landing_plan.entries
                )
                and lease.record.active_phase in {"select_next_action", "persist_landing_plan"}
            ):
                retired = _retire_changed_policy_landing(
                    request=request,
                    trace_id=trace_id,
                    recorded_at=recorded_at,
                    github_client=github_client,
                    candidate_store=candidate_store,
                    landing_store=landing_store,
                    admission_store=admission_store,
                    admission_evaluator=admission_evaluator,
                    landing_record=active_landing_record,
                    lease=lease,
                    retirement_source="original-review-recovery",
                )
                details = original_review_recovery_details(review_recovery)
                if not merge_train_policy_bindings_match(
                    record_store=landing_store,
                    policy_key=active_landing_record.landing_plan.policy_key,
                    recorded_sha256=active_landing_record.landing_plan.policy_sha256,
                    current_sha256=policy_sha256,
                ):
                    details["policy_changed"] = {
                        "code": "merge_train_stack_collapse_policy_changed",
                        "record_id": active_landing_record.record_id,
                        "policy_sha256": active_landing_record.landing_plan.policy_sha256,
                    }
                retired["details"] = details
                return retired
            return {
                "repository": request.repository,
                "base_branch": request.base_branch,
                "mode": "blocked",
                "controller_action": "original_review_recovery",
                "controller_reconciliation_status": "required",
                "merge_train_batch_landing_plan_record_id": active_landing_record.record_id,
                "details": original_review_recovery_details(review_recovery),
            }

    if (
        active_landing_record.ordinary_job_binding is None
        and active_landing_record.landing_plan.policy_key == repository_policy.policy_key
        and (
            not merge_train_policy_bindings_match(
                record_store=landing_store,
                policy_key=active_landing_record.landing_plan.policy_key,
                recorded_sha256=active_landing_record.landing_plan.policy_sha256,
                current_sha256=policy_sha256,
            )
        )
    ):
        return _retire_changed_policy_landing(
            request=request,
            trace_id=trace_id,
            recorded_at=recorded_at,
            github_client=github_client,
            candidate_store=candidate_store,
            landing_store=landing_store,
            admission_store=admission_store,
            admission_evaluator=admission_evaluator,
            landing_record=active_landing_record,
            lease=lease,
        )
    try:
        validate_merge_train_landing_record_for_controller(
            landing_record=active_landing_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
            record_store=landing_store,
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
                record_store=stack_collapse_store,
            )
        except ValueError as error:
            raise MergeTrainControllerRequestError(str(error)) from error
    candidate_record = _candidate_record_for_landing_plan(
        record_store=candidate_store,
        landing_plan_record=active_landing_record,
    )
    if candidate_record is None:
        raise MergeTrainControllerRequestError(
            (
                "ordinary merge train landing requires its exact candidate dependency"
                if active_landing_record.ordinary_job_binding is not None
                else "merge train landing requires its exact active candidate record"
            )
        )
    if active_landing_record.ordinary_job_binding is not None and collapse_record is not None:
        raise MergeTrainControllerRequestError(
            "ordinary merge train landing does not support stack collapse"
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
        if _ordinary_landing_terminal_success(active_landing_record):
            result["landing_progress"] = "cleanup_pending"
        if collapse_record is not None:
            result["merge_train_stack_collapse_plan_record_id"] = collapse_record.record_id
        return result

    if _ordinary_landing_terminal_success(active_landing_record):
        return _finish_landed_merge_train_batch(
            request=request,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            trace_id=trace_id,
            recorded_at=recorded_at,
            github_client=github_client,
            stack_collapse_store=stack_collapse_store,
            landed_record=active_landing_record,
            lease=lease,
        )

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

    checkpointed_progress_record: MergeTrainBatchLandingPlanRecord | None = None

    def checkpoint_landing_progress(
        progress_plan: MergeTrainBatchLandingPlan,
        entry: MergeTrainBatchLandingEntry,
        phase: str,
    ) -> MergeTrainBatchLandingPlanRecord | None:
        nonlocal checkpointed_progress_record
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
                    progress_entry.status == "merged"
                    or (
                        active_landing_record.ordinary_job_binding is not None
                        and progress_entry.status == "skipped"
                    )
                    for progress_entry in progress_plan.entries
                ),
            },
        )
        if phase not in {"entry_merged", "entry_skipped"}:
            return None
        progress_record = build_merge_train_batch_landing_plan_record(
            ordinary_job_binding=lease.record.ordinary_job_binding,
            landing_plan=progress_plan,
            source=f"service:controller:landing-progress:{trace_id}",
            updated_at=lease.record.updated_at,
        )
        persisted = landing_store.write_merge_train_batch_landing_plan_record(progress_record)
        if active_landing_record.ordinary_job_binding is not None:
            if (
                not isinstance(persisted, MergeTrainBatchLandingPlanRecord)
                or persisted != progress_record
            ):
                raise MergeTrainControllerRequestError(
                    "ordinary landing checkpoint did not persist the exact progress record"
                )
            checkpointed_progress_record = persisted
            return persisted
        return progress_record

    def checkpoint_provider_merge(
        progress_plan: MergeTrainBatchLandingPlan,
        entry: MergeTrainBatchLandingEntry,
    ) -> None:
        batch_attempt: dict[str, object] = {}
        if progress_plan.candidate_pull_request_number is not None:
            records = admission_store.list_merge_admission_records(
                repository=progress_plan.repository,
                base_branch=progress_plan.base_branch,
                landing_plan_id=progress_plan.plan_id,
            )
            attempt_records = [
                record
                for record in records
                if record.source == f"service:merge-admission:{trace_id}"
            ]
            if len(attempt_records) != len(progress_plan.entries) or {
                record.pull_request_number for record in attempt_records
            } != {member.pull_request_number for member in progress_plan.entries}:
                raise MergeTrainControllerRequestError(
                    "Batch dispatch requires every constituent admission from this pass."
                )
            batch_attempt = {
                "provider_pull_request_number": progress_plan.candidate_pull_request_number,
                "batch_admission_ids": sorted(record.admission_id for record in attempt_records),
            }
        lease.checkpoint(
            active_action="land_batch",
            active_phase="merge_pull_request",
            active_record_id=active_landing_record.record_id,
            active_pull_request_number=entry.pull_request_number,
            step_payload={
                **batch_attempt,
                "landing_plan_record_id": active_landing_record.record_id,
                "landing_plan_id": progress_plan.plan_id,
                "batch_id": progress_plan.batch_id,
                "candidate_ref": progress_plan.candidate_ref,
                "expected_effect_sha": progress_plan.candidate_sha,
                "completed_entry_count": sum(
                    progress_entry.status == "merged"
                    or (
                        active_landing_record.ordinary_job_binding is not None
                        and progress_entry.status == "skipped"
                    )
                    for progress_entry in progress_plan.entries
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
        structural_result = error.structural_result
        blocked_landing_record = admission_guard.landing_plan_record
        if _lineage_change_retires_landing(
            reason_code=error.reason_code,
            landing_record=blocked_landing_record,
            has_stack_collapse=collapse_record is not None,
        ):
            return _retire_changed_policy_landing(
                request=request,
                trace_id=trace_id,
                recorded_at=recorded_at,
                github_client=github_client,
                candidate_store=candidate_store,
                landing_store=landing_store,
                admission_store=admission_store,
                admission_evaluator=admission_evaluator,
                landing_record=blocked_landing_record,
                lease=lease,
                retirement_source="lineage-changed-landing",
            )
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
            "structural_provenance": (
                {
                    "status": structural_result.status,
                    "reason_codes": list(structural_result.reason_codes),
                    "effective_base_sha": structural_result.effective_base_sha,
                    "effective_base_tree_sha": structural_result.effective_base_tree_sha,
                    "candidate_sha256": structural_result.candidate_sha256,
                    "landing_plan_sha256": structural_result.landing_plan_sha256,
                    "provenance_sha256": structural_result.provenance_sha256,
                }
                if structural_result is not None
                else None
            ),
            "landing_plan": blocked_landing_record.landing_plan.model_dump(mode="json"),
        }
    except MergeTrainGitHubStaleHeadError as error:
        if active_landing_record.ordinary_job_binding is not None:
            preserved_record = admission_guard.landing_plan_record
            message = str(error).strip() or (
                "Ordinary merge train landing requires explicit recovery."
            )
            return {
                "merge_train_batch_landing_plan_record_id": preserved_record.record_id,
                "repository": preserved_record.landing_plan.repository,
                "base_branch": preserved_record.landing_plan.base_branch,
                "mode": "blocked",
                "controller_action": "land_batch",
                "controller_reconciliation_status": "required",
                "reconciliation_detail": "ordinary_landing_recovery_required",
                "landing_plan": preserved_record.landing_plan.model_dump(mode="json"),
                "error": {
                    "code": "ordinary_landing_recovery_required",
                    "message": message,
                },
                "details": {
                    "github_status_code": error.status_code,
                },
            }
        if active_landing_record.landing_plan.candidate_pull_request_number is not None:
            _fail_service_batch_candidate(
                github_client=github_client,
                candidate_store=candidate_store,
                candidate_record=admission_guard.candidate_record,
                lease=lease,
                trace_id=trace_id,
                recorded_at=recorded_at,
            )
        stale_plan = stale_merge_train_landing_plan(
            admission_guard.landing_plan_record.landing_plan
        )
        stale_record = build_merge_train_batch_landing_plan_record(
            ordinary_job_binding=lease.record.ordinary_job_binding,
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

    if active_landing_record.ordinary_job_binding is not None:
        if (
            checkpointed_progress_record is None
            or checkpointed_progress_record.landing_plan != landed_plan
        ):
            raise MergeTrainControllerRequestError(
                "ordinary landing did not return its exact persisted successor"
            )
        ordinary_result: dict[str, object] = {
            "merge_train_batch_landing_plan_record_id": checkpointed_progress_record.record_id,
            "repository": landed_plan.repository,
            "base_branch": landed_plan.base_branch,
            "mode": "land",
            "controller_action": "land_batch",
            "landing_plan": landed_plan.model_dump(mode="json"),
        }
        if any(entry.status in {"planned", "merging"} for entry in landed_plan.entries):
            ordinary_result["landing_progress"] = "partial"
            return ordinary_result
        if _ordinary_landing_terminal_success(checkpointed_progress_record):
            ordinary_result["landing_progress"] = "cleanup_pending"
            return ordinary_result
        raise MergeTrainControllerRequestError(
            "ordinary landing returned unsupported terminal progress"
        )

    landed_record = build_merge_train_batch_landing_plan_record(
        ordinary_job_binding=lease.record.ordinary_job_binding,
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


def _lineage_change_retires_landing(
    *,
    reason_code: str,
    landing_record: MergeTrainBatchLandingPlanRecord,
    has_stack_collapse: bool,
) -> bool:
    """Whether a lineage denial retires the plan instead of blocking on it.

    A queue that changed ahead of an unlanded plan never matches it again, so
    the plan is retired and the next pass replans from the live queue (#2843).
    Partial landings, collapsed stacks and ordinary-agent jobs keep blocking
    for explicit reconciliation.
    """
    return (
        reason_code
        in {
            "landing_lineage_changed",
            "head_check_reuse_unavailable",
            "original_pull_request_landing_required",
        }
        and landing_record.ordinary_job_binding is None
        and not has_stack_collapse
        and all(entry.status == "planned" for entry in landing_record.landing_plan.entries)
    )


_LINEAGE_RETIREMENT_SOURCE_PREFIX = "service:controller:lineage-changed-landing:"
_RETIRED_LANDING_SOURCE_PREFIXES = (
    "service:controller:policy-changed-landing:",
    _LINEAGE_RETIREMENT_SOURCE_PREFIX,
    "service:controller:original-review-recovery:",
    "service:controller:external-landing-observed:",
)


def _recover_external_stack_landing(
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
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object] | None:
    """Retire an externally merged collapsed root without claiming admission.

    This deliberately does not broaden historical-completion eligibility. It
    uses the live controller lease, current stack policy and checkpointed child
    disposition; flat, partial, ordinary and admitted effects keep their own
    recovery contracts.
    """
    source_id = (
        lease.record.step_payload.get("external_landing_source_record_id")
        or lease.record.active_record_id
    )
    if not isinstance(source_id, str):
        raise MergeTrainControllerRequestError("External recovery requires a record identifier.")
    source = _merge_train_landing_record_by_id(
        record_store=landing_store,
        repository=request.repository,
        base_branch=request.base_branch,
        record_id=source_id,
    )
    if source is None or source.ordinary_job_binding is not None:
        return None
    plan = source.landing_plan
    if len(plan.entries) != 1 or plan.entries[0].status != "planned":
        return None
    entry = plan.entries[0]
    # An admission in any lineage can represent an unresolved effect. Never
    # infer absence merely from the currently selected plan's admission list.
    if admission_store.list_merge_admission_records(
        repository=request.repository,
        base_branch=request.base_branch,
        pull_request_number=entry.pull_request_number,
        limit=1,
    ):
        return None
    completed = latest_completed_merge_train_batch_landing_plan_record(
        record_store=landing_store,
        repository=request.repository,
        base_branch=request.base_branch,
        batch_id=plan.batch_id,
        candidate_sha=plan.candidate_sha,
        policy_sha256=policy_sha256,
    )
    if completed is not None and completed.source.startswith(
        "service:controller:external-landing-observed:"
    ):
        if not request.mutate:
            return {
                "repository": request.repository,
                "base_branch": request.base_branch,
                "mode": "dry-run",
                "controller_action": "retire_stale_landing",
                "merge_train_batch_landing_plan_record_id": completed.record_id,
            }
        return _finish_retired_policy_landing(
            candidate_store=candidate_store, retired_record=completed, lease=lease
        )
    validate_merge_train_landing_record_for_controller(
        landing_record=source,
        policy_key=repository_policy.policy_key,
        policy_sha256=policy_sha256,
        record_store=landing_store,
    )
    candidate = _candidate_record_for_landing_plan(
        record_store=candidate_store, landing_plan_record=source
    )
    if (
        candidate is None
        or candidate.ordinary_job_binding is not None
        or candidate.candidate.stack_collapse_root is None
    ):
        return None
    collapse = latest_merge_train_stack_collapse_plan_record_for_landing(
        record_store=stack_collapse_store,
        repository=request.repository,
        base_branch=request.base_branch,
        landing_plan=plan,
        policy_sha256=policy_sha256,
    )
    if collapse is None or collapse.ordinary_job_binding is not None:
        raise MergeTrainControllerRequestError("External landing requires its completed stack.")
    validate_stack_collapse_record_for_landing(
        collapse_record=collapse,
        landing_plan=plan,
        policy_sha256=policy_sha256,
        record_store=stack_collapse_store,
    )
    if request.mutate:
        # Intent is not proof. Keep the selector before the provider read so a
        # transient read failure resumes fresh proof rather than losing the
        # recovery discriminator when the failure replaces reconciliation_detail.
        lease.checkpoint(
            active_action="land_batch",
            active_phase="observe_external_landing",
            active_record_id=source.record_id,
            step_payload={
                **lease.record.step_payload,
                "external_landing_source_record_id": source.record_id,
            },
        )
    evidence = github_client.observe_historical_batch_completion(
        landing_plan=plan, observed_at=recorded_at
    )
    observed = evidence.entries[0]
    # This model is transient input to the existing child-disposition proof.
    # It is never persisted as a successful Launchplane landing.
    observed_entry = MergeTrainBatchLandingEntry.model_validate(
        {
            **entry.model_dump(mode="json"),
            "status": "merged",
            "recorded_rolling_base_sha": observed.observed_parent_sha,
            "recorded_rolling_base_tree_sha": observed.observed_parent_tree_sha,
            "landed_head_sha": observed.observed_head_sha,
            "landed_head_tree_sha": observed.observed_head_tree_sha,
            "merge_commit_sha": observed.observed_merge_commit_sha,
            "merge_commit_tree_sha": observed.observed_merge_commit_tree_sha,
        }
    )
    observed_record = source.model_copy(
        update={"landing_plan": plan.model_copy(update={"entries": (observed_entry,)})}
    )
    if request.mutate:
        lease.checkpoint(
            active_action="land_batch",
            active_phase="observe_external_landing",
            active_record_id=source.record_id,
            step_payload={
                **lease.record.step_payload,
                "external_landing_source_record_id": source.record_id,
                "observed_external_merge_commit_sha": observed.observed_merge_commit_sha,
            },
        )
    result = _reconcile_completed_landing_after_policy_change(
        request=request,
        repository_policy=repository_policy,
        trace_id=trace_id,
        recorded_at=recorded_at,
        github_client=github_client,
        stack_collapse_store=stack_collapse_store,
        landed_record=observed_record,
        lease=lease,
    )
    # Do not expose the transient successful-entry shape as a train receipt.
    result.pop("landing_plan", None)
    if result.get("mode") == "blocked":
        return result
    if not request.mutate:
        result.update(
            mode="dry-run",
            controller_action="retire_stale_landing",
            reason_code="external_stack_landing_observed",
        )
        return result
    lease.checkpoint(
        active_phase="retire_stale_policy_landing",
        active_record_id=source.record_id,
        step_payload={
            **lease.record.step_payload,
            "retirement_source": "external-landing-observed",
        },
    )
    retired = build_merge_train_batch_landing_plan_record(
        landing_plan=stale_merge_train_landing_plan(plan),
        source=(
            "service:controller:external-landing-observed:"
            f"{observed.observed_merge_commit_sha}:{trace_id}"
        ),
        updated_at=lease.record.updated_at,
    )
    landing_store.write_merge_train_batch_landing_plan_record(retired)
    result = _finish_retired_policy_landing(
        candidate_store=candidate_store, retired_record=retired, lease=lease
    )
    result["reason_code"] = "external_stack_landing_observed"
    return result


def _retire_changed_policy_landing(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    trace_id: str,
    recorded_at: str,
    github_client: GitHubMergeTrainClient,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    admission_store: MergeAdmissionRecordStore,
    admission_evaluator: MergeAdmissionEvaluator,
    landing_record: MergeTrainBatchLandingPlanRecord,
    lease: MergeTrainControllerLeaseContext,
    retirement_source: str = "policy-changed-landing",
) -> dict[str, object]:
    plan = landing_record.landing_plan
    # Retirement changes no carried branch history. The same exact no-effect
    # proof below applies to collapsed roots; their obsolete collapse history
    # remains subject to normal current-policy/original-review recovery.
    candidate_record = _candidate_record_for_landing_plan(
        record_store=candidate_store, landing_plan_record=landing_record
    )
    if candidate_record is None:
        raise MergeTrainControllerRequestError(
            "Policy-change recovery requires the exact recorded candidate."
        )
    observed_pr_heads: dict[int, tuple[str, str]] = {}
    observed_base_sha, observed_base_tree_sha = _verify_retirement_has_no_effect(
        github_client=github_client,
        admission_store=admission_store,
        plan=plan,
        allow_moved_heads=retirement_source == "original-review-recovery",
        observed_pr_heads=observed_pr_heads,
    )
    if not request.mutate:
        return {
            "repository": plan.repository,
            "base_branch": plan.base_branch,
            "mode": "dry-run",
            "controller_action": "retire_stale_landing",
            "merge_train_batch_landing_plan_record_id": landing_record.record_id,
        }
    lease.checkpoint()
    guard = GuardedMergeAdmission(
        record_store=admission_store,
        evaluator=admission_evaluator,
        candidate_record=candidate_record,
        landing_plan_record=landing_record,
        controller_state=lease.record,
        trace_id=trace_id,
    )
    for entry in plan.entries:
        observed_head, observed_tree = observed_pr_heads.get(
            entry.pull_request_number, (entry.expected_head_sha, entry.expected_head_tree_sha)
        )
        guard.reconcile_existing_no_effect(
            entry=entry,
            observed_base_sha=observed_base_sha,
            observed_base_tree_sha=observed_base_tree_sha,
            observed_head_sha=observed_head,
            observed_head_tree_sha=observed_tree,
            observed_pull_request_state="open",
            observed_at=guard.observation_time_provider(),
        )
    lease.checkpoint(
        active_action="land_batch",
        active_phase="retire_stale_policy_landing",
        active_record_id=landing_record.record_id,
        active_pull_request_number=None,
        step_payload={
            "landing_plan_record_id": landing_record.record_id,
            "landing_plan_id": plan.plan_id,
            "expected_effect_sha": plan.candidate_sha,
            "retirement_source": retirement_source,
        },
    )
    return _record_landing_retirement(
        trace_id=trace_id,
        recorded_at=recorded_at,
        github_client=github_client,
        candidate_store=candidate_store,
        landing_store=landing_store,
        candidate_record=candidate_record,
        landing_record=landing_record,
        retirement_source=retirement_source,
        lease=lease,
    )


def _verify_retirement_has_no_effect(
    *,
    github_client: GitHubMergeTrainClient,
    admission_store: MergeAdmissionRecordStore,
    plan: MergeTrainBatchLandingPlan,
    allow_moved_heads: bool = False,
    observed_pr_heads: dict[int, tuple[str, str]] | None = None,
) -> tuple[str, str]:
    allow_changed_base = True
    for entry in plan.entries:
        admissions = admission_store.list_merge_admission_records(
            repository=plan.repository,
            base_branch=plan.base_branch,
            pull_request_number=entry.pull_request_number,
            landing_plan_id=plan.plan_id,
        )
        if admissions:
            outcomes = admission_store.list_merge_landing_outcome_records(
                admission_id=admissions[0].admission_id, limit=1
            )
            if not outcomes or outcomes[0].status != "rejected":
                allow_changed_base = False
    if allow_moved_heads:
        return github_client.verify_unlanded_batch(
            landing_plan=plan,
            allow_changed_base=allow_changed_base,
            allow_moved_heads=True,
            observed_pr_heads=observed_pr_heads,
        )
    return github_client.verify_unlanded_batch(
        landing_plan=plan,
        allow_changed_base=allow_changed_base,
    )


def _record_landing_retirement(
    *,
    trace_id: str,
    recorded_at: str,
    github_client: GitHubMergeTrainClient,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    candidate_record: MergeTrainBatchCandidateRecord,
    landing_record: MergeTrainBatchLandingPlanRecord,
    retirement_source: str,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object]:
    plan = landing_record.landing_plan
    if plan.candidate_pull_request_number is not None:
        # Keep the retirement phase so an interruption after the close resumes here (#2846).
        _close_service_batch_pull_request(
            github_client=github_client,
            candidate_record=candidate_record,
            lease=lease,
            active_phase="retire_stale_policy_landing",
        )
    retired_record = build_merge_train_batch_landing_plan_record(
        landing_plan=stale_merge_train_landing_plan(plan),
        source=f"service:controller:{retirement_source}:{trace_id}",
        updated_at=recorded_at,
    )
    landing_store.write_merge_train_batch_landing_plan_record(retired_record)
    return _finish_retired_policy_landing(
        candidate_store=candidate_store, retired_record=retired_record, lease=lease
    )


def _resume_unrecorded_landing_retirement(
    *,
    trace_id: str,
    recorded_at: str,
    github_client: GitHubMergeTrainClient,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    admission_store: MergeAdmissionRecordStore,
    landing_record: MergeTrainBatchLandingPlanRecord,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object] | None:
    """Finish a retirement interrupted before its record was written.

    The batch PR may already be closed, so a normal landing pass could record a
    generic stale landing instead. The constituents are verified again because
    they may have changed while the controller was down; closing again is a
    no-op on a closed PR.
    """
    retirement_source = lease.record.step_payload.get("retirement_source")
    if not isinstance(retirement_source, str) or retirement_source not in {
        "policy-changed-landing",
        "lineage-changed-landing",
        "original-review-recovery",
    }:
        return None
    candidate_record = _candidate_record_for_landing_plan(
        record_store=candidate_store, landing_plan_record=landing_record
    )
    if candidate_record is None:
        raise MergeTrainControllerRequestError(
            "Retirement resume requires the exact recorded candidate."
        )
    _verify_retirement_has_no_effect(
        github_client=github_client,
        admission_store=admission_store,
        plan=landing_record.landing_plan,
        allow_moved_heads=retirement_source == "original-review-recovery",
    )
    return _record_landing_retirement(
        trace_id=trace_id,
        recorded_at=recorded_at,
        github_client=github_client,
        candidate_store=candidate_store,
        landing_store=landing_store,
        candidate_record=candidate_record,
        landing_record=landing_record,
        retirement_source=retirement_source,
        lease=lease,
    )


def _finish_retired_policy_landing(
    *,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    retired_record: MergeTrainBatchLandingPlanRecord,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object]:
    plan = retired_record.landing_plan
    if (
        retired_record.ordinary_job_binding is not None
        or not retired_record.source.startswith(_RETIRED_LANDING_SOURCE_PREFIXES)
        or any(entry.status != "stale" for entry in plan.entries)
    ):
        raise MergeTrainControllerRequestError("Policy-change retirement evidence is incomplete.")
    lease.checkpoint()
    for record in candidate_store.list_merge_train_batch_candidate_records(
        repository=plan.repository, base_branch=plan.base_branch, status="active"
    ):
        if (
            record.candidate.batch_id == plan.batch_id
            and record.candidate.policy_sha256 == plan.policy_sha256
        ):
            _supersede_merge_train_batch_candidate_record(
                record_store=candidate_store, record=record
            )
    return {
        "repository": plan.repository,
        "base_branch": plan.base_branch,
        "mode": "stale_landing",
        "controller_action": "retire_stale_landing",
        "merge_train_batch_landing_plan_record_id": retired_record.record_id,
        "landing_plan": plan.model_dump(mode="json"),
        "candidate_ref_cleanup_status": "retained",
    }


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
    if (
        landed_record.ordinary_job_binding is None
        and landed_plan.policy_key == repository_policy.policy_key
        and (
            not merge_train_policy_bindings_match(
                record_store=stack_collapse_store,
                policy_key=landed_plan.policy_key,
                recorded_sha256=landed_plan.policy_sha256,
                current_sha256=policy_sha256,
            )
        )
        and _landing_cleanup_allowed(landed_record)
    ):
        return _reconcile_completed_landing_after_policy_change(
            request=request,
            repository_policy=repository_policy,
            trace_id=trace_id,
            recorded_at=recorded_at,
            github_client=github_client,
            stack_collapse_store=stack_collapse_store,
            landed_record=landed_record,
            lease=lease,
        )
    try:
        validate_merge_train_landing_record_for_controller(
            landing_record=landed_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
            record_store=stack_collapse_store,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    if not _landing_cleanup_allowed(landed_record):
        raise MergeTrainControllerRequestError(
            "merge train cleanup requires a fully landed batch record"
        )
    collapse_records = stack_collapse_records_for_completed_landing(
        record_store=stack_collapse_store,
        github_client=github_client,
        repository=request.repository,
        base_branch=request.base_branch,
        landing_plan=landed_plan,
        policy_sha256=policy_sha256,
    )
    if landed_record.ordinary_job_binding is not None and collapse_records:
        raise MergeTrainControllerRequestError(
            "ordinary merge train landing does not support stack collapse"
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
    if landed_record.ordinary_job_binding is not None:
        result["landing_progress"] = "cleanup_pending"
    if candidate_ref_cleanup_result.get("candidate_ref_cleanup_status") == "failed":
        return result
    if landed_record.ordinary_job_binding is not None:
        result["landing_progress"] = "complete"
    if not collapse_records:
        if lease.record.step_payload.get("stack_collapse_plan_record_id"):
            raise MergeTrainControllerRequestError(
                "merge train stack collapse resume record is missing or incompatible"
            )
        if landed_record.ordinary_job_binding is None:
            _annotate_historical_closed_stack_children(
                landed_record=landed_record,
                repository_policy=repository_policy,
                github_client=github_client,
                stack_collapse_store=stack_collapse_store,
                lease=lease,
            )
        return result
    reconciled_records = []
    for collapse_record in collapse_records:
        if collapse_record.plan.status != "ready_for_train":
            collapse_record = _reconcile_landed_stack_children(
                collapse_record=collapse_record,
                landed_record=landed_record,
                repository_policy=repository_policy,
                trace_id=trace_id,
                recorded_at=recorded_at,
                github_client=github_client,
                stack_collapse_store=stack_collapse_store,
                lease=lease,
            )
        reconciled_records.append(collapse_record)
    # Preserve the single-stack response for existing readers.
    newest_record = max(
        reconciled_records,
        key=lambda record: (record.plan.created_at, record.updated_at, record.record_id),
    )
    result["merge_train_stack_collapse_plan_record_id"] = newest_record.record_id
    result["stack_collapse_plan"] = newest_record.plan.model_dump(mode="json")
    _annotate_historical_closed_stack_children(
        landed_record=landed_record,
        repository_policy=repository_policy,
        github_client=github_client,
        stack_collapse_store=stack_collapse_store,
        lease=lease,
    )
    return result


def _reconcile_completed_landing_after_policy_change(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    repository_policy: MergeTrainRepositoryPolicy,
    trace_id: str,
    recorded_at: str,
    github_client: GitHubMergeTrainClient,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    landed_record: MergeTrainBatchLandingPlanRecord,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object]:
    """Verify completed history and finish child disposition under current policy."""
    plan = landed_record.landing_plan
    for entry in plan.entries:
        observed_merge = github_client.pull_request_is_merged(
            repository=request.repository,
            pull_request_number=entry.pull_request_number,
            expected_head_sha=entry.expected_head_sha,
        )
        if observed_merge != entry.merge_commit_sha or not github_client.branch_contains_commit(
            repository=request.repository,
            branch_ref=request.base_branch,
            commit_sha=entry.merge_commit_sha,
        ):
            raise MergeTrainGitHubStaleHeadError(
                "Completed landing no longer matches the merged PR and target branch.",
                status_code=409,
            )
    collapse_records = stack_collapse_records_for_completed_landing(
        record_store=stack_collapse_store,
        github_client=github_client,
        repository=request.repository,
        base_branch=request.base_branch,
        landing_plan=plan,
        policy_sha256=plan.policy_sha256,
    )
    result: dict[str, object] = {
        "repository": request.repository,
        "base_branch": request.base_branch,
        "mode": "land",
        "controller_action": "land_batch",
        "merge_train_batch_landing_plan_record_id": landed_record.record_id,
        "landing_plan": plan.model_dump(mode="json"),
        "candidate_ref_cleanup_status": "retained",
        "reason_code": "completed_landing_policy_changed",
    }
    missing_stack_record = bool(
        lease.record.step_payload.get("stack_collapse_plan_record_id") and not collapse_records
    )

    def blocked(reason: str, message: str) -> dict[str, object]:
        result.update(
            mode="blocked",
            reason_code=reason,
            controller_reconciliation_status="required",
            reconciliation_detail=reason,
            blocking_reason={
                "code": reason,
                "message": message,
            },
            details={
                "stack_collapse_plan_record_ids": [record.record_id for record in collapse_records],
                "expected_stack_collapse_plan_record_id": lease.record.step_payload.get(
                    "stack_collapse_plan_record_id", ""
                ),
            },
        )
        return result

    if missing_stack_record:
        return blocked(
            "completed_landing_stack_reconciliation_required",
            "Recorded stack collapse history is missing or incompatible.",
        )
    unfinished = tuple(
        record for record in collapse_records if record.plan.status != "ready_for_train"
    )
    # Historical plans are evidence of an already-landed effect, never authority
    # for another collapse or merge. Verify every child before any provider write.
    verified_records = []
    for record in unfinished:
        if record.ordinary_job_binding is not None:
            return blocked(
                "completed_landing_stack_binding_unsupported",
                "Completed legacy landing cannot reconcile ordinary-job stack history.",
            )
        root = next(
            entry
            for entry in plan.entries
            if entry.pull_request_number == record.plan.root_pull_request_number
        )
        # Squash/rebase rewrite ancestry; the provider's exact merged-root head
        # plus child containment in that head proves their carried content.
        root_refs = (
            (root.expected_head_sha, root.merge_commit_sha)
            if root.merge_method == "merge"
            else (root.expected_head_sha,)
        )
        dispositions = []
        for child in record.plan.child_dispositions:
            if child.completed:
                dispositions.append(child)
                continue  # A completed disposition cannot close a later reopened head.
            try:
                observed_head, observed_state = github_client.read_pull_request_head(
                    repository=request.repository,
                    pull_request_number=child.pull_request_number,
                )
                contained = all(
                    github_client.branch_contains_commit(
                        repository=request.repository,
                        branch_ref=root_ref,
                        commit_sha=child.expected_head_sha,
                    )
                    for root_ref in root_refs
                )
            except MergeTrainGitHubError as error:
                if error.rate_limited or error.status_code is None or error.status_code >= 500:
                    raise  # Preserve the controller's retry classification and reset evidence.
                return blocked(
                    "completed_landing_stack_child_evidence_unavailable",
                    f"Stack child PR #{child.pull_request_number} evidence is unavailable.",
                )
            if not contained:
                return blocked(
                    "completed_landing_stack_child_not_contained",
                    f"Stack child PR #{child.pull_request_number} is not contained in the landed root.",
                )
            if observed_head != child.expected_head_sha:
                child = type(child).model_validate(
                    {
                        **child.model_dump(),
                        "status": "preserved",
                        "preserved_head_sha": observed_head,
                        "preserved_state": observed_state,
                        "detail": f"Recorded child head landed; changed PR preserved {observed_state} without further provider effects",
                    }
                )
            dispositions.append(child)
        verified_records.append(
            record.model_copy(
                update={
                    "plan": record.plan.model_copy(
                        update={"child_dispositions": tuple(dispositions)}
                    )
                }
            )
        )
    for record in verified_records:
        if not request.mutate:
            continue
        reconciled = _reconcile_landed_stack_children(
            collapse_record=record,
            landed_record=landed_record,
            repository_policy=repository_policy,
            trace_id=trace_id,
            recorded_at=recorded_at,
            github_client=github_client,
            stack_collapse_store=stack_collapse_store,
            lease=lease,
        )
        result["merge_train_stack_collapse_plan_record_id"] = reconciled.record_id
        result["stack_collapse_plan"] = reconciled.plan.model_dump(mode="json")
    return result


def _annotate_historical_closed_stack_children(
    *,
    landed_record: MergeTrainBatchLandingPlanRecord,
    repository_policy: MergeTrainRepositoryPolicy,
    github_client: GitHubMergeTrainClient,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    lease: MergeTrainControllerLeaseContext,
) -> None:
    """Observe carried history after landing; never requalify or execute its plan."""
    landing = landed_record.landing_plan
    roots = {
        entry.pull_request_number: entry for entry in landing.entries if entry.status == "merged"
    }
    records = stack_collapse_store.list_merge_train_stack_collapse_plan_records(
        repository=landing.repository, base_branch=landing.base_branch
    )
    annotated = {
        (child.pull_request_number, child.expected_head_sha)
        for record in records
        if merge_train_policy_bindings_match(
            record_store=stack_collapse_store,
            policy_key=landing.policy_key,
            recorded_sha256=record.plan.policy_sha256,
            current_sha256=landing.policy_sha256,
        )
        and record.plan.root_pull_request_number in roots
        for child in record.plan.child_dispositions
        if child.status == "closed"
    }
    for progress in _group_stack_collapse_records(records).values():
        record = latest_merge_train_stack_collapse_progress_record(tuple(progress))
        if record is None:
            continue
        plan = record.plan
        root = roots.get(plan.root_pull_request_number)
        if (
            root is None
            or record.ordinary_job_binding is not None
            or plan.repository != landing.repository
            or plan.base_branch != landing.base_branch
            or plan.policy_key != landing.policy_key
            or merge_train_policy_bindings_match(
                record_store=stack_collapse_store,
                policy_key=landing.policy_key,
                recorded_sha256=plan.policy_sha256,
                current_sha256=landing.policy_sha256,
            )
            or not github_client.branch_contains_commit(
                repository=landing.repository,
                branch_ref=root.expected_head_sha,
                commit_sha=plan.root_initial_head_sha,
            )
        ):
            continue
        for child in plan.child_dispositions:
            identity = (child.pull_request_number, child.expected_head_sha)
            if child.completed or identity in annotated:
                continue
            try:
                closed = github_client.pull_request_is_closed(
                    repository=landing.repository,
                    pull_request_number=child.pull_request_number,
                    expected_head_sha=child.expected_head_sha,
                )
            except MergeTrainGitHubStaleHeadError:
                continue  # Old expectations cannot dispose of a moved child.
            if (
                not closed
                or not github_client.branch_contains_commit(
                    repository=landing.repository,
                    branch_ref=root.expected_head_sha,
                    commit_sha=child.expected_head_sha,
                )
                or not github_client.branch_contains_commit(
                    repository=landing.repository,
                    branch_ref=root.merge_commit_sha,
                    commit_sha=child.expected_head_sha,
                )
                or github_client.branch_contains_commit(
                    repository=landing.repository,
                    branch_ref=root.recorded_rolling_base_sha or root.expected_base_sha,
                    commit_sha=child.expected_head_sha,
                )
            ):
                continue
            label = resolve_stack_child_disposition_label(repository_policy)
            body = (
                f"Launchplane verified this already-closed stacked PR's head "
                f"`{child.expected_head_sha}` in root PR #{root.pull_request_number}, "
                f"landed by the merge train at `{root.merge_commit_sha}`.\n\n"
                "This restores landing annotations from carried stack history; "
                "it does not record a new child merge or historical admission."
            )
            lineage = MergeTrainEffectLineage(
                repository=landing.repository,
                base_branch=landing.base_branch,
                collapse_id=plan.collapse_id,
                batch_id=landing.batch_id,
                landing_plan_id=landing.plan_id,
            )
            lease.checkpoint(
                active_action="land_batch",
                active_phase="annotate_historical_stack_child",
                active_record_id=landed_record.record_id,
                active_pull_request_number=child.pull_request_number,
            )
            if not github_client.find_pull_request_comment_url(
                repository=landing.repository,
                pull_request_number=child.pull_request_number,
                body_contains=body,
            ):
                github_client.semantic_effect_executor.comment_stack_child(
                    StackChildCommentEffect(
                        lineage=lineage, pull_request_number=child.pull_request_number, body=body
                    )
                )
            lease.checkpoint()
            if label and not github_client.pull_request_has_label(
                repository=landing.repository,
                pull_request_number=child.pull_request_number,
                label=label,
            ):
                github_client.semantic_effect_executor.label_stack_child(
                    StackChildLabelEffect(
                        lineage=lineage, pull_request_number=child.pull_request_number, label=label
                    )
                )
            annotated.add(identity)


def _reconcile_landed_stack_children(
    *,
    collapse_record: MergeTrainStackCollapsePlanRecord,
    landed_record: MergeTrainBatchLandingPlanRecord,
    repository_policy: MergeTrainRepositoryPolicy,
    trace_id: str,
    recorded_at: str,
    github_client: GitHubMergeTrainClient,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    lease: MergeTrainControllerLeaseContext,
) -> MergeTrainStackCollapsePlanRecord:
    landed_plan = landed_record.landing_plan
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
                if not disposition.completed
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
                    disposition.completed for disposition in progress_plan.child_dispositions
                ),
            },
        )
        progress_record = build_merge_train_stack_collapse_plan_record(
            ordinary_job_binding=lease.record.ordinary_job_binding,
            plan=progress_plan.model_copy(update={"updated_at": lease.record.updated_at}),
            source=f"service:controller:child-disposition-progress:{trace_id}",
            updated_at=lease.record.updated_at,
        )
        stack_collapse_store.write_merge_train_stack_collapse_plan_record(progress_record)

    reconciled_collapse_plan = reconcile_merge_train_stack_children_after_root_landing(
        plan=collapse_record.plan,
        disposition_client=github_client,
        effect_executor=github_client.semantic_effect_executor,
        root_merge_commit_sha=root_entry.merge_commit_sha,
        label=resolve_stack_child_disposition_label(repository_policy),
        updated_at=recorded_at,
        checkpoint=checkpoint_child_disposition,
    )
    reconciled_record = build_merge_train_stack_collapse_plan_record(
        ordinary_job_binding=lease.record.ordinary_job_binding,
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
    return reconciled_record


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
    release_hold_reader: "MergeTrainReleaseHoldReader | None" = None,
) -> dict[str, object]:
    # A landing already under way finishes; nothing new is built or landed while
    # a Client-accepted release built from this repository runs.
    release_hold = release_hold_reader(request.repository) if release_hold_reader else None
    if release_hold is not None:
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "blocked",
            "controller_action": "release_hold",
            "blocking_reason": release_hold.blocking_reason(),
        }
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
    candidate_record = active_candidate_record or passed_candidate_record
    if (
        candidate_record is not None
        and candidate_record.ordinary_job_binding is None
        and candidate_record.candidate.policy_key == repository_policy.policy_key
        and (
            not merge_train_policy_bindings_match(
                record_store=candidate_store,
                policy_key=candidate_record.candidate.policy_key,
                recorded_sha256=candidate_record.candidate.policy_sha256,
                current_sha256=policy_sha256,
            )
        )
    ):
        if request.mutate:
            lease.checkpoint(
                active_action="reflow_candidate",
                active_phase="retire_changed_policy_candidate",
                active_record_id=candidate_record.record_id,
                active_pull_request_number=None,
            )
            _close_service_batch_pull_request(
                github_client=github_client, candidate_record=candidate_record, lease=lease
            )
            _supersede_active_merge_train_batch_candidate_records(
                record_store=candidate_store,
                repository=request.repository,
                base_branch=request.base_branch,
                batch_id=candidate_record.candidate.batch_id,
                replacement_record_id=None,
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
            stack_collapse_store=stack_collapse_store,
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


def _uses_protected_candidate_pr(
    *,
    candidate_record: MergeTrainBatchCandidateRecord,
    merge_method: str,
    dry_run_result: MergeTrainDryRunResult,
) -> bool:
    # Original-PR code-owner approval and proved head-check reuse keep their
    # existing landing routes. Otherwise a batch of any size lands its tested ref.
    members = set(dry_run_result.queue_order)
    return (
        candidate_record.ordinary_job_binding is None
        and merge_method == "merge"
        and candidate_record.candidate.head_check_reuse is None
        and bool(members)
        and not any(
            entry.requires_individual_landing
            for entry in dry_run_result.queue
            if entry.number in members
        )
    )


def _close_service_batch_pull_request(
    *,
    github_client: GitHubMergeTrainClient,
    candidate_record: MergeTrainBatchCandidateRecord,
    lease: MergeTrainControllerLeaseContext,
    active_phase: str = "close_batch_pull_request",
) -> None:
    candidate = candidate_record.candidate
    if (
        candidate_record.ordinary_job_binding is None
        and candidate.head_check_reuse is None
        and candidate.candidate_sha
    ):
        lease.checkpoint(active_phase=active_phase)
        github_client.close_batch_pull_request(candidate=candidate)


def _fail_service_batch_candidate(
    *,
    github_client: GitHubMergeTrainClient,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    candidate_record: MergeTrainBatchCandidateRecord,
    lease: MergeTrainControllerLeaseContext,
    trace_id: str,
    recorded_at: str,
) -> MergeTrainBatchCandidateRecord:
    _close_service_batch_pull_request(
        github_client=github_client,
        candidate_record=candidate_record,
        lease=lease,
    )
    failed_record = build_merge_train_batch_candidate_record(
        candidate=candidate_record.candidate.model_copy(
            update={"status": "failed", "updated_at": recorded_at}
        ),
        source=f"service:controller:batch-candidate-failed:{trace_id}",
        updated_at=recorded_at,
    )
    candidate_store.write_merge_train_batch_candidate_record(failed_record)
    return failed_record


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
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    active_candidate_record: MergeTrainBatchCandidateRecord,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object]:
    try:
        validate_merge_train_candidate_record_for_controller(
            candidate_record=active_candidate_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
            record_store=candidate_store,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    lease.observe(
        candidate_batch_id=active_candidate_record.candidate.batch_id,
        candidate_sha=active_candidate_record.candidate.candidate_sha,
        base_sha=active_candidate_record.candidate.base_sha,
    )
    if active_candidate_record.candidate.status == "failed":
        lease.observe_failed_candidate(active_candidate_record)
        if request.mutate:
            _close_service_batch_pull_request(
                github_client=github_client,
                candidate_record=active_candidate_record,
                lease=lease,
            )
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
            lease=lease,
            github_client=github_client,
            candidate_store=candidate_store,
            stack_collapse_store=stack_collapse_store,
            active_candidate_record=active_candidate_record,
            policy=policy,
            policy_sha256=policy_sha256,
            transport=transport,
            repository=request.repository,
            base_branch=request.base_branch,
            merge_method=repository_policy.merge_method,
            recorded_at=recorded_at,
            trace_id=trace_id,
            mutate=request.mutate,
        )
        if reflow_result is not None:
            if request.mutate and reflow_result.get("controller_action") in {
                "plan_candidate",
                "observe_candidate",
            }:
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

    reflow_result, current_queue = _reflow_stale_candidate_record(
        request=request,
        policy=policy,
        policy_sha256=policy_sha256,
        repository_policy=repository_policy,
        transport=transport,
        github_client=github_client,
        candidate_store=candidate_store,
        stack_collapse_store=stack_collapse_store,
        candidate_record=active_candidate_record,
        trace_id=trace_id,
        recorded_at=recorded_at,
        lease=lease,
    )
    if reflow_result is not None:
        return reflow_result

    candidate_build_error: MergeTrainGitHubStaleHeadError | None = None
    construction_evidence = (
        {
            "construction_ref": (
                active_candidate_record.candidate.candidate_ref
                if active_candidate_record.candidate.head_check_reuse is not None
                else merge_train_construction_ref(active_candidate_record.candidate.candidate_ref)
            )
        }
        if active_candidate_record.ordinary_job_binding is None
        else {}
    )
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
                    **construction_evidence,
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
                held_out = active_candidate_record.candidate.held_out
                if isinstance(error, MergeTrainGitHubCandidateEntryConflictError):
                    held_out = (
                        *held_out,
                        MergeTrainBatchHeldOutEntry(
                            pull_request_number=error.pull_request_number,
                            head_sha=error.head_sha,
                            probe_base_sha=active_candidate_record.candidate.base_sha,
                            conflicts_with_head_shas=tuple(
                                entry.head_sha
                                for entry in active_candidate_record.candidate.entries
                                if entry.pull_request_number
                                in _entries_ahead_of(
                                    candidate=active_candidate_record.candidate,
                                    pull_request_number=error.pull_request_number,
                                )
                            ),
                            conflicts_with=_entries_ahead_of(
                                candidate=active_candidate_record.candidate,
                                pull_request_number=error.pull_request_number,
                            ),
                        ),
                    )
                candidate = active_candidate_record.candidate.model_copy(
                    update={"status": "failed", "updated_at": recorded_at, "held_out": held_out}
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
        try:
            if request.mutate and _uses_protected_candidate_pr(
                candidate_record=active_candidate_record,
                merge_method=repository_policy.merge_method,
                dry_run_result=current_queue,
            ):
                lease.checkpoint(active_phase="ensure_batch_pull_request")
                github_client.ensure_batch_pull_request(candidate=active_candidate_record.candidate)
                lease.checkpoint(active_phase="observe_required_checks")
            if request.mutate:
                lease.observe(
                    candidate_checks_status="unknown",
                    candidate_sha=active_candidate_record.candidate.candidate_sha,
                )
            candidate = (
                github_client.observe_batch_candidate_checks(
                    candidate=active_candidate_record.candidate
                )
                if request.mutate
                else active_candidate_record.candidate
            )
            if request.mutate:
                lease.observe(candidate_checks_status=candidate.required_checks_status)
        except MergeTrainGitHubStaleHeadError as error:
            controller_action = "candidate_failed"
            candidate_build_error = error
            candidate = active_candidate_record.candidate.model_copy(
                update={"status": "failed", "updated_at": recorded_at}
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
            "code": (
                "merge_train_candidate_entry_conflict"
                if isinstance(candidate_build_error, MergeTrainGitHubCandidateEntryConflictError)
                else "merge_train_github_stale_state"
            ),
            "message": message,
        }
        result["details"] = {
            "github_status_code": candidate_build_error.status_code,
            "failed_pull_request_number": lease.record.active_pull_request_number,
            **construction_evidence,
        }
    if request.mutate:
        if candidate.status == "failed":
            _close_service_batch_pull_request(
                github_client=github_client,
                candidate_record=active_candidate_record,
                lease=lease,
            )
        updated_candidate_record = build_merge_train_batch_candidate_record(
            ordinary_job_binding=lease.record.ordinary_job_binding,
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
                **construction_evidence,
            },
        )
    result["candidate"] = candidate.model_dump(mode="json")
    return result


def _reflow_stale_candidate_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    transport: MergeTrainGitHubTransport,
    github_client: GitHubMergeTrainClient,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    candidate_record: MergeTrainBatchCandidateRecord,
    trace_id: str,
    recorded_at: str,
    lease: MergeTrainControllerLeaseContext,
) -> tuple[dict[str, object] | None, MergeTrainDryRunResult]:
    """Replan obsolete candidates before build, check observation, or landing."""
    snapshot = github_client.read_merge_train_snapshot(
        repository=request.repository,
        base_branch=request.base_branch,
    )
    snapshot = _without_obsolete_stack_collapse_pull_requests(
        snapshot=snapshot,
        records=_read_obsolete_stack_collapse_records(
            store=stack_collapse_store,
            github_client=github_client,
            snapshot=snapshot,
            repository_policy=repository_policy,
            policy_sha256=policy_sha256,
        ),
    )
    candidate_snapshot = _without_held_out_pull_requests(
        snapshot=snapshot,
        held_out=_surviving_held_out_entries(
            policy=policy,
            snapshot=snapshot,
            held_out=candidate_record.candidate.held_out,
            batch_landing=lease.record.ordinary_job_binding is None,
            isolating_entries=candidate_record.candidate.entries,
        ),
    )
    stack_collapse_root = candidate_record.candidate.stack_collapse_root
    if stack_collapse_root is not None:
        candidate_snapshot = snapshot.model_copy(
            update={
                "pull_requests": tuple(
                    pull_request
                    for pull_request in candidate_snapshot.pull_requests
                    if pull_request.number == stack_collapse_root.root_pull_request_number
                )
            }
        )
    # Pull requests that became eligible after this candidate was built wait for
    # the next one; only a change to its own members replans it.
    dry_run_result = build_merge_train_dry_run_result(
        policy=policy,
        snapshot=merge_train_snapshot_of(
            candidate_snapshot,
            [entry.pull_request_number for entry in candidate_record.candidate.entries],
        ),
        batch_landing=candidate_record.ordinary_job_binding is None,
        existing_candidate_size=len(candidate_record.candidate.entries),
        planned_handoff_heads=frozenset(
            (entry.pull_request_number, entry.head_sha)
            for entry in candidate_record.candidate.entries
        ),
    )
    candidate_matches_queue = _merge_train_candidate_matches_dry_run_queue(
        candidate=candidate_record.candidate,
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
    )
    if not candidate_matches_queue or (
        candidate_record.ordinary_job_binding is None
        and dry_run_result.intended_next_action == "update_branch"
    ):
        if request.mutate:
            lease.checkpoint(
                active_action="reflow_candidate",
                active_phase="supersede_stale_candidate",
                active_record_id=candidate_record.record_id,
                active_pull_request_number=None,
                step_payload={
                    "candidate_record_id": candidate_record.record_id,
                    "batch_id": candidate_record.candidate.batch_id,
                },
            )
            retry = candidate_record.candidate
            if (
                candidate_record.ordinary_job_binding is None
                and retry.candidate_sha
                and retry.status in {"building", "ready_for_checks"}
                and (
                    retry.check_retry_of
                    or retry.infrastructure_retry_of
                    or retry.batch_body_retry_of
                )
                and bound_closed_batch_pull_request(client=github_client, candidate=retry) is None
            ):
                # Record the controller's queue-retirement intent before closing.
                # A crash after the close must not turn it into a manual-close hold.
                lease.checkpoint(active_phase="record_retry_queue_retirement")
                retirement = build_merge_train_batch_candidate_record(
                    candidate=retry,
                    source=f"{_QUEUE_RETRY_RETIREMENT_SOURCE}{trace_id}",
                    updated_at=recorded_at,
                ).model_copy(update={"status": "superseded"})
                candidate_store.write_merge_train_batch_candidate_record(retirement)
            _close_service_batch_pull_request(
                github_client=github_client,
                candidate_record=candidate_record,
                lease=lease,
            )
            _supersede_active_merge_train_batch_candidate_records(
                record_store=candidate_store,
                repository=request.repository,
                base_branch=request.base_branch,
                batch_id=candidate_record.candidate.batch_id,
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
            held_out=candidate_record.candidate.held_out,
        )
        result["superseded_merge_train_batch_candidate_record_id"] = candidate_record.record_id
        return result, dry_run_result
    if (
        candidate_record.ordinary_job_binding is None
        and candidate_record.candidate.candidate_sha
        and (
            candidate_record.candidate.check_retry_of
            or candidate_record.candidate.infrastructure_retry_of
            or candidate_record.candidate.batch_body_retry_of
        )
        and any(
            record.source.startswith(_QUEUE_RETRY_RETIREMENT_SOURCE)
            and record.candidate.candidate_ref == candidate_record.candidate.candidate_ref
            and record.candidate.candidate_sha == candidate_record.candidate.candidate_sha
            and (
                merge_train_policy_bindings_match(
                    record_store=candidate_store,
                    policy_key=record.candidate.policy_key,
                    recorded_sha256=record.candidate.policy_sha256,
                    current_sha256=policy_sha256,
                )
            )
            for record in _candidate_history_on_base(
                candidate_store=candidate_store,
                repository=request.repository,
                base_branch=request.base_branch,
                base_sha=snapshot.base_sha,
            )
        )
    ):
        # The queue may return immediately after a crash during retirement,
        # while the pre-close record is still active. Resume its recorded intent
        # before the ordinary observation path sees the closed PR as a failure.
        recovery = _resume_known_failed_batch(
            candidate_store=candidate_store,
            github_client=github_client,
            policy=policy,
            policy_sha256=policy_sha256,
            snapshot=candidate_snapshot,
            dry_run_result=dry_run_result,
            held_out=candidate_record.candidate.held_out,
            merge_method=repository_policy.merge_method,
            recorded_at=recorded_at,
            trace_id=trace_id,
            mutate=request.mutate,
            lease=lease,
        )
        if recovery is not None:
            return recovery, dry_run_result
    if candidate_record.ordinary_job_binding is None and any(
        (
            pr.client_test_notes_error
            or (pr.owner_review_required and pr.required_checks_status != "pass")
        )
        for pr in dry_run_result.queue
        if pr.number in dry_run_result.queue_order
    ):
        waiting_pr = next(
            pr
            for pr in dry_run_result.queue
            if pr.number in dry_run_result.queue_order
            and (
                pr.client_test_notes_error
                or (pr.owner_review_required and pr.required_checks_status != "pass")
            )
        )
        dry_run_result = dry_run_result.model_copy(
            update={
                "selected_pr": waiting_pr,
                "intended_next_action": "block"
                if waiting_pr.client_test_notes_error or waiting_pr.required_checks_status == "fail"
                else "wait_for_checks",
                "next_action_detail": f"Pull request #{waiting_pr.number}: {waiting_pr.client_test_notes_error}"
                if waiting_pr.client_test_notes_error
                else f"Wait for current-head Client review and required checks on pull request #{waiting_pr.number}."
                if waiting_pr.required_checks_status != "fail"
                else f"Current-head review or required checks failed on pull request #{waiting_pr.number}.",
            }
        )
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": dry_run_result.intended_next_action,
            "merge_train_batch_candidate_record_id": candidate_record.record_id,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
        }, dry_run_result
    return None, dry_run_result


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
            record_store=candidate_store,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    completed_landing_record = latest_completed_merge_train_batch_landing_plan_record(
        record_store=landing_store,
        repository=request.repository,
        base_branch=request.base_branch,
        batch_id=passed_candidate_record.candidate.batch_id,
        candidate_sha=passed_candidate_record.candidate.candidate_sha,
        policy_sha256=passed_candidate_record.candidate.policy_sha256,
    )
    if completed_landing_record is not None:
        try:
            validate_merge_train_landing_record_for_controller(
                landing_record=completed_landing_record,
                policy_key=repository_policy.policy_key,
                policy_sha256=policy_sha256,
                record_store=landing_store,
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
    reflow_result, current_queue = _reflow_stale_candidate_record(
        request=request,
        policy=policy,
        policy_sha256=policy_sha256,
        repository_policy=repository_policy,
        transport=transport,
        github_client=github_client,
        candidate_store=candidate_store,
        stack_collapse_store=stack_collapse_store,
        candidate_record=passed_candidate_record,
        trace_id=trace_id,
        recorded_at=recorded_at,
        lease=lease,
    )
    if reflow_result is not None:
        return reflow_result
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
    batch_pull_request_number = None
    if passed_candidate_record.candidate.head_check_reuse is not None:
        lease.observe(
            candidate_checks_status="unknown",
            candidate_sha=passed_candidate_record.candidate.candidate_sha,
        )
        refreshed = github_client.observe_batch_candidate_checks(
            candidate=passed_candidate_record.candidate
        )
        lease.observe(candidate_checks_status=refreshed.required_checks_status)
        refreshed_record = build_merge_train_batch_candidate_record(
            candidate=refreshed,
            source=f"service:controller:head-check-revalidation:{trace_id}",
            updated_at=recorded_at,
        )
        candidate_store.write_merge_train_batch_candidate_record(refreshed_record)
        if refreshed.status != "passed":
            return {
                "repository": request.repository,
                "base_branch": request.base_branch,
                "mode": "observe_candidate",
                "controller_action": "observe_candidate",
                "merge_train_batch_candidate_record_id": refreshed_record.record_id,
                "candidate": refreshed.model_dump(mode="json"),
            }
        passed_candidate_record = refreshed_record
    if _uses_protected_candidate_pr(
        candidate_record=passed_candidate_record,
        merge_method=repository_policy.merge_method,
        dry_run_result=current_queue,
    ):
        try:
            batch_pull_request_number = github_client.ensure_batch_pull_request(
                candidate=passed_candidate_record.candidate
            )
        except MergeTrainGitHubStaleHeadError as error:
            failed_record = _fail_service_batch_candidate(
                github_client=github_client,
                candidate_store=candidate_store,
                candidate_record=passed_candidate_record,
                lease=lease,
                trace_id=trace_id,
                recorded_at=recorded_at,
            )
            return {
                "repository": request.repository,
                "base_branch": request.base_branch,
                "mode": "candidate_failed",
                "controller_action": "candidate_failed",
                "merge_train_batch_candidate_record_id": failed_record.record_id,
                "candidate": failed_record.candidate.model_dump(mode="json"),
                "error": {
                    "code": "merge_train_github_stale_state",
                    "message": "The recorded batch PR changed; this candidate was retired.",
                },
                "details": {"github_status_code": error.status_code},
            }
    landing_plan = build_merge_train_batch_landing_plan(
        candidate=passed_candidate_record.candidate,
        merge_method=repository_policy.merge_method,
        created_at=recorded_at,
        candidate_pull_request_number=batch_pull_request_number,
    )
    landing_record = build_merge_train_batch_landing_plan_record(
        ordinary_job_binding=lease.record.ordinary_job_binding,
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


def _obsolete_stack_collapse_records(
    *,
    records: tuple[MergeTrainStackCollapsePlanRecord, ...],
    snapshot: MergeTrainDryRunSnapshot,
    repository_policy: MergeTrainRepositoryPolicy,
    policy_sha256: str,
    record_store: object | None = None,
) -> tuple[MergeTrainStackCollapsePlanRecord, ...]:
    latest = tuple(
        progress
        for group in _group_stack_collapse_records(records).values()
        if (progress := latest_merge_train_stack_collapse_progress_record(tuple(group))) is not None
    )
    observed_heads = {pr.number: pr.head_sha for pr in snapshot.pull_requests}
    return tuple(
        record
        for record in latest
        if record.plan.status in {"planned", "collapsing", "waiting_for_root_checks"}
        and any(mutation.status == "mutated" for mutation in record.plan.mutations)
        and (record.status == "active" or "; retired:" in record.source)
        and (
            record.plan.policy_key != repository_policy.policy_key
            or (
                not merge_train_policy_bindings_match(
                    record_store=record_store,
                    policy_key=record.plan.policy_key,
                    recorded_sha256=record.plan.policy_sha256,
                    current_sha256=policy_sha256,
                )
            )
        )
        and observed_heads.get(record.plan.root_pull_request_number)
        == _stack_collapse_current_head_shas(record.plan)[record.plan.root_pull_request_number]
        and not (
            record.status == "superseded"
            and any(
                newer.plan.root_pull_request_number == record.plan.root_pull_request_number
                and newer.plan.collapse_id != record.plan.collapse_id
                and (newer.updated_at, newer.record_id) > (record.updated_at, record.record_id)
                for newer in latest
            )
        )
    )


def _without_obsolete_stack_collapse_pull_requests(
    *,
    snapshot: MergeTrainDryRunSnapshot,
    records: tuple[MergeTrainStackCollapsePlanRecord, ...],
) -> MergeTrainDryRunSnapshot:
    numbers = {entry.pull_request_number for record in records for entry in record.plan.entries}
    refs = {entry.head_ref for record in records for entry in record.plan.entries}
    return snapshot.model_copy(
        update={
            "pull_requests": tuple(
                pr
                for pr in snapshot.pull_requests
                if pr.number not in numbers and pr.head_ref not in refs and pr.base_ref not in refs
            )
        }
    )


def _read_obsolete_stack_collapse_records(
    *,
    store: MergeTrainStackCollapsePlanRecordStore,
    github_client: GitHubMergeTrainClient,
    snapshot: MergeTrainDryRunSnapshot,
    repository_policy: MergeTrainRepositoryPolicy,
    policy_sha256: str,
) -> tuple[MergeTrainStackCollapsePlanRecord, ...]:
    records = tuple(
        record
        for status in ("active", "superseded")
        for record in store.list_merge_train_stack_collapse_plan_records(
            repository=snapshot.repository, base_branch=snapshot.base_branch, status=status
        )
    )
    obsolete = _obsolete_stack_collapse_records(
        records=records,
        snapshot=snapshot,
        repository_policy=repository_policy,
        policy_sha256=policy_sha256,
        record_store=store,
    )

    review = original_review_recovery_records(
        records=records, snapshot=snapshot, ancestry=github_client
    )
    return tuple({record.record_id: record for record in (*obsolete, *review)}.values())


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
    held_out: tuple[MergeTrainBatchHeldOutEntry, ...] = (),
) -> dict[str, object]:
    snapshot: MergeTrainDryRunSnapshot | None = None
    waiting_records = stack_collapse_store.list_merge_train_stack_collapse_plan_records(
        repository=request.repository, base_branch=request.base_branch, status="active"
    )
    record_groups = _group_stack_collapse_records(waiting_records)
    latest_records = tuple(
        progress
        for records in record_groups.values()
        if (progress := latest_merge_train_stack_collapse_progress_record(tuple(records)))
        is not None
    )
    latest_waiting_records = tuple(
        record for record in latest_records if record.plan.status == "waiting_for_root_checks"
    )
    if latest_waiting_records:
        snapshot = github_client.read_merge_train_snapshot(
            repository=request.repository, base_branch=request.base_branch
        )
        root_heads = {pr.number: pr.head_sha for pr in snapshot.pull_requests}
        retired_collapse_ids: set[str] = set()
        for record in latest_waiting_records:
            collapse_id = record.plan.collapse_id
            observed_head = root_heads.get(record.plan.root_pull_request_number)
            if observed_head == stack_collapse_expected_root_head_sha(record.plan):
                continue
            retired_collapse_ids.add(collapse_id)
            if request.mutate:
                reason = (
                    "root_missing_from_open_snapshot"
                    if observed_head is None
                    else "root_head_changed"
                )
                _retire_obsolete_stack_collapse_record(
                    request=request,
                    stack_collapse_store=stack_collapse_store,
                    record=record,
                    reason=reason,
                    trace_id=trace_id,
                    lease=lease,
                )
        latest_waiting_records = tuple(
            record
            for record in latest_waiting_records
            if record.plan.collapse_id not in retired_collapse_ids
        )
    retired_records = stack_collapse_store.list_merge_train_stack_collapse_plan_records(
        repository=request.repository, base_branch=request.base_branch, status="superseded"
    )
    snapshot = snapshot or github_client.read_merge_train_snapshot(
        repository=request.repository, base_branch=request.base_branch
    )
    obsolete_records = _obsolete_stack_collapse_records(
        records=waiting_records + retired_records,
        snapshot=snapshot,
        repository_policy=repository_policy,
        policy_sha256=policy_sha256,
        record_store=stack_collapse_store,
    )
    policy_obsolete_records = obsolete_records
    review_recovery = original_review_recovery_records(
        records=waiting_records + retired_records,
        snapshot=snapshot,
        ancestry=github_client,
    )
    obsolete_records = tuple(
        {record.record_id: record for record in (*obsolete_records, *review_recovery)}.values()
    )
    block_code = (
        ORIGINAL_REVIEW_RECOVERY_CODE
        if review_recovery
        else "merge_train_stack_collapse_policy_changed"
    )
    obsolete_numbers = {
        entry.pull_request_number for record in obsolete_records for entry in record.plan.entries
    }
    obsolete_refs = {entry.head_ref for record in obsolete_records for entry in record.plan.entries}

    def independent(record: MergeTrainStackCollapsePlanRecord) -> bool:
        return all(
            entry.pull_request_number not in obsolete_numbers
            and entry.head_ref not in obsolete_refs
            for entry in record.plan.entries
        )

    def report_obsolete(result: dict[str, object]) -> dict[str, object]:
        if policy_obsolete_records:
            result["details"] = {
                "code": "merge_train_stack_collapse_policy_changed",
                "record_id": policy_obsolete_records[0].record_id,
                "entries": [
                    {
                        "record_id": record.record_id,
                        "root_pull_request_number": record.plan.root_pull_request_number,
                        "root_initial_head_sha": record.plan.root_initial_head_sha,
                        "original_child_pull_request_numbers": [
                            mutation.child_pull_request_number for mutation in record.plan.mutations
                        ],
                    }
                    for record in sorted(
                        policy_obsolete_records, key=lambda record: record.record_id
                    )
                ],
            }
        if review_recovery:
            details = original_review_recovery_details(review_recovery)
            if policy_obsolete_records:
                details["policy_changed"] = result["details"]
            result["details"] = details
        return result

    latest_records = tuple(record for record in latest_records if independent(record))
    latest_waiting_records = tuple(
        record for record in latest_waiting_records if independent(record)
    )
    active_collapse_ids = {record.plan.collapse_id for record in latest_records}
    active_root_numbers = {record.plan.root_pull_request_number for record in latest_records}
    retired_executions = tuple(
        progress
        for records in _group_stack_collapse_records(retired_records).values()
        if (progress := latest_merge_train_stack_collapse_progress_record(tuple(records)))
        is not None
        and progress.plan.collapse_id not in active_collapse_ids
        and progress.plan.root_pull_request_number not in active_root_numbers
        and progress.plan.status in {"planned", "collapsing"}
        and "; retired:" in progress.source
        and progress.plan.policy_key == repository_policy.policy_key
        and (
            merge_train_policy_bindings_match(
                record_store=stack_collapse_store,
                policy_key=progress.plan.policy_key,
                recorded_sha256=progress.plan.policy_sha256,
                current_sha256=policy_sha256,
            )
        )
    )
    if retired_executions:
        snapshot = snapshot or github_client.read_merge_train_snapshot(
            repository=request.repository, base_branch=request.base_branch
        )
        observed_heads = {pr.number: pr.head_sha for pr in snapshot.pull_requests}
        latest_records += tuple(
            record
            for record in retired_executions
            if observed_heads.get(record.plan.root_pull_request_number)
            == _stack_collapse_current_head_shas(record.plan)[record.plan.root_pull_request_number]
            and all(
                observed_heads[number] == head_sha
                for number, head_sha in _stack_collapse_current_head_shas(record.plan).items()
                if number in observed_heads
            )
            and all(
                mutation.child_pull_request_number in observed_heads
                for mutation in record.plan.mutations
                if mutation.status != "mutated"
            )
            and independent(record)
        )
    # Resume saved execution before reporting an unrelated root's pending checks.
    # Select progress per collapse first so completed histories cannot revive plans.
    for plan_status in ("collapsing", "planned"):
        planned_records = sorted(
            (record for record in latest_records if record.plan.status == plan_status),
            key=lambda record: (record.updated_at, record.record_id),
            reverse=True,
        )
        for planned_collapse_record in planned_records:
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
                if planned_collapse_record.status == "superseded" and not request.mutate:
                    planned_result.pop("merge_train_stack_collapse_plan_record_id", None)
                return report_obsolete(planned_result)

    pending_wait_result: dict[str, object] | None = None
    for waiting_collapse_record in sorted(
        latest_waiting_records,
        key=lambda record: (record.updated_at, record.record_id),
        reverse=True,
    ):
        waiting_result, snapshot = _advance_waiting_stack_collapse_record(
            github_client=github_client,
            request=request,
            policy=policy,
            policy_sha256=policy_sha256,
            repository_policy=repository_policy,
            transport=transport,
            candidate_store=candidate_store,
            stack_collapse_store=stack_collapse_store,
            waiting_collapse_record=waiting_collapse_record,
            snapshot=snapshot,
            trace_id=trace_id,
            recorded_at=recorded_at,
            lease=lease,
        )
        if waiting_result is None:
            continue
        if waiting_result["controller_action"] != "wait_for_root_checks":
            return report_obsolete(waiting_result)
        if pending_wait_result is None:
            pending_wait_result = waiting_result
    if obsolete_records:
        assert snapshot is not None
        snapshot = _without_obsolete_stack_collapse_pull_requests(
            snapshot=snapshot, records=obsolete_records
        )
    if pending_wait_result is not None:
        assert snapshot is not None
        queue_snapshot = _without_held_out_pull_requests(
            snapshot=snapshot,
            held_out=_surviving_held_out_entries(
                policy=policy,
                snapshot=snapshot,
                held_out=held_out,
                batch_landing=lease.record.ordinary_job_binding is None,
            ),
        )
        queue_result = build_merge_train_dry_run_result(
            policy=policy,
            snapshot=queue_snapshot,
            batch_landing=lease.record.ordinary_job_binding is None,
        )
        # Pending saved checks must not hide the ordinary queue's blocking reason.
        if queue_result.intended_next_action == "block":
            return report_obsolete(
                _apply_queue_block(
                    request=request,
                    dry_run_result=queue_result,
                    github_client=github_client,
                    lease=lease,
                )
            )
        return report_obsolete(pending_wait_result)

    live_result = _advance_from_live_snapshot(
        github_client=github_client,
        request=request,
        policy=policy,
        policy_sha256=policy_sha256,
        transport=transport,
        candidate_store=candidate_store,
        stack_collapse_store=stack_collapse_store,
        trace_id=trace_id,
        recorded_at=recorded_at,
        lease=lease,
        snapshot=snapshot,
        held_out=held_out,
    )
    if obsolete_records and live_result["controller_action"] == "idle":
        live_result = {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "blocked",
            "controller_action": "block",
            "blocking_reason": {
                "code": block_code,
                "message": (
                    "A code-owned child is still carried in another PR outside the base. Restore the original PR branches without the carry; preserve the recorded history and current-head reviews. Unrelated work can proceed."
                    if review_recovery
                    else "This saved stack requires its original policy or a new root head; unrelated work can proceed while its proof remains blocked."
                ),
            },
        }
    return report_obsolete(live_result)


def _advance_waiting_stack_collapse_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    repository_policy: MergeTrainRepositoryPolicy,
    transport: MergeTrainGitHubTransport,
    github_client: GitHubMergeTrainClient,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    waiting_collapse_record: MergeTrainStackCollapsePlanRecord,
    trace_id: str,
    recorded_at: str,
    lease: MergeTrainControllerLeaseContext,
    snapshot: MergeTrainDryRunSnapshot | None = None,
) -> tuple[dict[str, object] | None, MergeTrainDryRunSnapshot]:
    """Admit a collapsed root once its checks pass, or step aside.

    The plan waits only while the root's own checks are pending. A root that
    needs a branch update, is blocked, or has left the queue goes back to the
    live queue with the snapshot already read, so the controller refreshes it
    or moves on to the other ready pull requests instead of waiting forever.
    """
    snapshot = snapshot or github_client.read_merge_train_snapshot(
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
        return None, snapshot
    try:
        validate_merge_train_stack_collapse_record_for_controller(
            collapse_record=waiting_collapse_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
            record_store=stack_collapse_store,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    root_snapshot = snapshot.model_copy(update={"pull_requests": (root_pull_request,)})
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=root_snapshot)
    if dry_run_result.intended_next_action == "wait_for_checks":
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "wait_for_root_checks",
            "merge_train_stack_collapse_plan_record_id": waiting_collapse_record.record_id,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
        }, snapshot
    if dry_run_result.intended_next_action != "merge":
        return None, snapshot
    if not request.mutate:
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": "admit_collapsed_root",
            "merge_train_stack_collapse_plan_record_id": waiting_collapse_record.record_id,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
        }, snapshot

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
    # The root-only dry run cannot see the rest of the queue. Record its
    # dependency holds for failed-candidate recovery, which reads the full queue.
    dependency_holds = (
        dependency_not_ready_holds(
            build_merge_train_dry_run_result(policy=policy, snapshot=snapshot, batch_landing=True),
            root_snapshot.base_sha,
        )
        if lease.record.ordinary_job_binding is None
        else ()
    )
    candidate = build_merge_train_batch_candidate(
        ordinary_job_binding=lease.record.ordinary_job_binding,
        dry_run_result=dry_run_result,
        base_sha=root_snapshot.base_sha,
        policy_sha256=policy_sha256,
        created_at=recorded_at,
        held_out=dependency_holds,
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
        ordinary_job_binding=lease.record.ordinary_job_binding,
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
    }, snapshot


def _stack_collapse_current_head_shas(plan: MergeTrainStackCollapsePlan) -> dict[int, str]:
    heads = {entry.pull_request_number: entry.head_sha for entry in plan.entries}
    for mutation in plan.mutations:
        if mutation.status == "mutated" and mutation.merge_commit_sha:
            heads[mutation.parent_pull_request_number] = mutation.merge_commit_sha
    return heads


def _retire_obsolete_stack_collapse_record(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    record: MergeTrainStackCollapsePlanRecord,
    reason: str,
    trace_id: str,
    lease: MergeTrainControllerLeaseContext,
) -> None:
    if not request.mutate:
        return
    records = stack_collapse_store.list_merge_train_stack_collapse_plan_records(
        repository=request.repository, base_branch=request.base_branch, status="active"
    )
    # Retire all older progress first, so interruption cannot revive this plan.
    for progress in sorted(records, key=lambda item: item.record_id == record.record_id):
        if progress.plan.collapse_id != record.plan.collapse_id:
            continue
        lease.checkpoint(
            active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
            active_phase="supersede_inapplicable_collapse",
            active_record_id=progress.record_id,
            active_pull_request_number=record.plan.root_pull_request_number,
        )
        stack_collapse_store.write_merge_train_stack_collapse_plan_record(
            progress.model_copy(
                update={
                    "status": "superseded",
                    "source": f"{progress.source}; retired:{reason}:{trace_id}",
                }
            )
        )


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
    def retire(reason: str) -> None:
        _retire_obsolete_stack_collapse_record(
            request=request,
            stack_collapse_store=stack_collapse_store,
            record=planned_collapse_record,
            reason=reason,
            trace_id=trace_id,
            lease=lease,
        )

    def unproven_stack_merge(message: str) -> dict[str, object]:
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "blocked",
            "controller_action": "stack_unsupported",
            "blocking_reason": {
                "code": "merge_train_stack_merge_unproven",
                "message": message
                + " Review the current heads and post fresh checked handoffs before replanning.",
            },
            "merge_train_stack_collapse_plan_record_id": planned_collapse_record.record_id,
        }

    # Obsolete execution steps aside while its complete history stays available.
    if planned_collapse_record.plan.policy_key != repository_policy.policy_key or (
        not merge_train_policy_bindings_match(
            record_store=stack_collapse_store,
            policy_key=planned_collapse_record.plan.policy_key,
            recorded_sha256=planned_collapse_record.plan.policy_sha256,
            current_sha256=policy_sha256,
        )
    ):
        retire("policy_changed")
        return None
    snapshot = github_client.read_merge_train_snapshot(
        repository=request.repository,
        base_branch=request.base_branch,
    )
    collapse_members = {
        planned_collapse_record.plan.root_pull_request_number,
        *(
            mutation.child_pull_request_number
            for mutation in planned_collapse_record.plan.mutations
        ),
    }
    if any(
        pr.number in collapse_members and pr.requires_individual_landing
        for pr in snapshot.pull_requests
    ):
        retire("code_owned_changes_require_original_pull_requests")
        return None
    root_pull_request = next(
        (
            pull_request
            for pull_request in snapshot.pull_requests
            if pull_request.number == planned_collapse_record.plan.root_pull_request_number
        ),
        None,
    )
    if root_pull_request is None:
        retire("root_missing_from_open_snapshot")
        return None
    current_head_shas = _stack_collapse_current_head_shas(planned_collapse_record.plan)
    root_mutation = next(
        mutation
        for mutation in planned_collapse_record.plan.mutations
        if mutation.parent_pull_request_number
        == planned_collapse_record.plan.root_pull_request_number
    )
    expected_root_sha = current_head_shas[root_mutation.parent_pull_request_number]
    if root_pull_request.head_sha != expected_root_sha:
        try:
            observed_root_sha = github_client.find_stack_child_merge_commit(
                repository=planned_collapse_record.plan.repository,
                child_head_sha=current_head_shas[root_mutation.child_pull_request_number],
                expected_parent_head_sha=expected_root_sha,
                parent_head_ref=root_mutation.parent_head_ref,
                expected_merge_tree_sha=root_mutation.expected_merge_tree_sha,
                collapse_id=planned_collapse_record.plan.collapse_id,
                child_pull_request_number=root_mutation.child_pull_request_number,
                parent_pull_request_number=root_mutation.parent_pull_request_number,
            )
        except MergeTrainGitHubStaleHeadError as error:
            retire("root_moved")
            if root_mutation.expected_merge_tree_sha:
                return unproven_stack_merge(str(error))
            return None
        if observed_root_sha != root_pull_request.head_sha:
            # A ref probe and PR snapshot can disagree while GitHub converges.
            # Only a confirmed stale-head refusal above proves obsolescence.
            return None
    # A root or child that is no longer ready falls through to live discovery, which
    # reports why. A child missing from the open snapshot may already be merged, so the
    # executor recovers it or reads it fresh before merging.
    pull_requests_by_number = {
        pull_request.number: pull_request for pull_request in snapshot.pull_requests
    }
    pending_pull_request_numbers = (planned_collapse_record.plan.root_pull_request_number,) + tuple(
        mutation.child_pull_request_number
        for mutation in planned_collapse_record.plan.mutations
        if mutation.status != "mutated"
    )
    for pull_request_number in pending_pull_request_numbers:
        pull_request = pull_requests_by_number.get(pull_request_number)
        if pull_request is not None and merge_train_stack_child_readiness_reasons(
            repository_policy=repository_policy, pull_request=pull_request
        ):
            return None
    try:
        validate_merge_train_stack_collapse_record_for_controller(
            collapse_record=planned_collapse_record,
            policy_key=repository_policy.policy_key,
            policy_sha256=policy_sha256,
            record_store=stack_collapse_store,
        )
    except ValueError as error:
        raise MergeTrainControllerRequestError(str(error)) from error
    if planned_collapse_record.status == "superseded" and request.mutate:
        lease.checkpoint(
            active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
            active_phase="resume_returned_stack_root",
            active_record_id=planned_collapse_record.record_id,
            active_pull_request_number=planned_collapse_record.plan.root_pull_request_number,
        )
        planned_collapse_record = build_merge_train_stack_collapse_plan_record(
            ordinary_job_binding=lease.record.ordinary_job_binding,
            plan=planned_collapse_record.plan.model_copy(update={"updated_at": recorded_at}),
            source=f"service:controller:resume-retired-collapse:{trace_id}",
            updated_at=recorded_at,
        )
        stack_collapse_store.write_merge_train_stack_collapse_plan_record(planned_collapse_record)
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
                ordinary_job_binding=lease.record.ordinary_job_binding,
                plan=progress_plan.model_copy(update={"updated_at": lease.record.updated_at}),
                source=f"service:controller:stack-collapse-progress:{trace_id}",
                updated_at=lease.record.updated_at,
            )
            stack_collapse_store.write_merge_train_stack_collapse_plan_record(progress_record)

        try:
            executed_plan = execute_merge_train_stack_collapse_plan(
                plan=planned_collapse_record.plan,
                branch_client=github_client,
                child_readiness_reasons=merge_train_stack_child_readiness_check(
                    reader=github_client,
                    repository=planned_collapse_record.plan.repository,
                    repository_policy=repository_policy,
                ),
                recovered_child_readiness_reasons=merge_train_stack_child_readiness_check(
                    reader=github_client,
                    repository=planned_collapse_record.plan.repository,
                    repository_policy=repository_policy,
                    recovered_merge=True,
                ),
                effect_executor=github_client.semantic_effect_executor,
                updated_at=recorded_at,
                checkpoint=checkpoint_collapse_progress,
            )
        except MergeTrainGitHubStaleHeadError as error:
            retire("unproven_stack_recovery")
            return unproven_stack_merge(str(error))
        except MergeTrainStackChildNotReadyError as error:
            return {
                "repository": request.repository,
                "base_branch": request.base_branch,
                "mode": "blocked",
                "controller_action": "stack_unsupported",
                "blocking_reason": {
                    "code": "merge_train_stack_unsupported",
                    "message": str(error),
                },
                "merge_train_stack_collapse_plan_record_id": planned_collapse_record.record_id,
            }
        executed_record = build_merge_train_stack_collapse_plan_record(
            ordinary_job_binding=lease.record.ordinary_job_binding,
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


def _apply_queue_block(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    dry_run_result: MergeTrainDryRunResult,
    github_client: GitHubMergeTrainClient,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object]:
    result: dict[str, object] = {
        "repository": request.repository,
        "base_branch": request.base_branch,
        "mode": (
            "block" if request.mutate and lease.record.ordinary_job_binding is None else "dry-run"
        ),
        "controller_action": "block",
        "dry_run_result": dry_run_result.model_dump(mode="json"),
    }
    if request.mutate and lease.record.ordinary_job_binding is None:
        assert dry_run_result.selected_pr is not None
        lease.checkpoint(
            active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
            active_phase="block_pull_request",
            active_record_id="",
            active_pull_request_number=dry_run_result.selected_pr.number,
            step_payload={"blocked_label": dry_run_result.blocked_label},
        )
        block_result = apply_merge_train_block_intent(
            dry_run_result=dry_run_result, label_client=github_client
        )
        # Service batches hold this PR independently rather than stopping the driver.
        result["block_result"] = block_result.model_copy(
            update={"train_should_continue": True}
        ).model_dump(mode="json")
    return result


def _prepare_new_stack_disposition(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    repository_policy: MergeTrainRepositoryPolicy,
    github_client: GitHubMergeTrainClient,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object] | None:
    # Only fresh discovery calls this. Saved collapse/landing recovery must keep
    # its existing route even if preparation is temporarily unavailable.
    reason = "stack_disposition_label_conflicts_with_train_label"
    try:
        label = resolve_stack_child_disposition_label(repository_policy)
        reason = "stack_disposition_preparation_failed"
        if request.mutate:
            lease.checkpoint(
                active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
                active_phase="prepare_stack_child_disposition",
            )
        if github_client.ensure_repository_label(
            repository=request.repository, label=label, mutate=request.mutate
        ):
            return None
        reason = "stack_disposition_label_creation_required"
        message = "The disposition label is missing; a mutating pass prepares it before stack work."
    except ValueError as error:
        message = str(error)
    except MergeTrainGitHubError:
        message = "The provider could not prepare the stack child disposition label; no stack work started."
    return {
        "repository": request.repository,
        "base_branch": request.base_branch,
        "mode": "blocked",
        "controller_action": "block",
        "reason_code": reason,
        "blocking_reason": {"code": reason, "message": message},
        "details": {"prerequisite": "stack_child_disposition_label"},
    }


def _advance_from_live_snapshot(
    *,
    request: MergeTrainControllerRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    transport: MergeTrainGitHubTransport,
    github_client: GitHubMergeTrainClient,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    trace_id: str,
    recorded_at: str,
    lease: MergeTrainControllerLeaseContext,
    snapshot: MergeTrainDryRunSnapshot | None = None,
    held_out: tuple[MergeTrainBatchHeldOutEntry, ...] = (),
) -> dict[str, object]:
    if snapshot is None:
        snapshot = github_client.read_merge_train_snapshot(
            repository=request.repository,
            base_branch=request.base_branch,
        )
    held_out = _surviving_held_out_entries(
        policy=policy,
        snapshot=snapshot,
        held_out=held_out,
        batch_landing=lease.record.ordinary_job_binding is None,
    )
    snapshot = _without_held_out_pull_requests(snapshot=snapshot, held_out=held_out)
    dry_run_result = build_merge_train_dry_run_result(
        policy=policy,
        snapshot=snapshot,
        batch_landing=lease.record.ordinary_job_binding is None,
        new_candidate=True,
    )
    selected_pr = dry_run_result.selected_pr
    if selected_pr is not None:
        restored_records = tuple(
            record
            for record in stack_collapse_store.list_merge_train_stack_collapse_plan_records(
                repository=request.repository, base_branch=request.base_branch, status="superseded"
            )
            if "; retired:" in record.source
            and record.plan.status == "waiting_for_root_checks"
            and record.plan.root_pull_request_number == selected_pr.number
            and stack_collapse_expected_root_head_sha(record.plan) == selected_pr.head_sha
            and (
                merge_train_policy_bindings_match(
                    record_store=stack_collapse_store,
                    policy_key=record.plan.policy_key,
                    recorded_sha256=record.plan.policy_sha256,
                    current_sha256=policy_sha256,
                )
            )
            and record.plan.policy_key == dry_run_result.policy_key
            and all(
                pr.head_sha == disposition.expected_head_sha
                for disposition in record.plan.child_dispositions
                for pr in snapshot.pull_requests
                if pr.number == disposition.pull_request_number
            )
        )
        if restored_records:
            active_collapse_ids = {
                record.plan.collapse_id
                for record in stack_collapse_store.list_merge_train_stack_collapse_plan_records(
                    repository=request.repository, base_branch=request.base_branch, status="active"
                )
            }
            restored_records = tuple(
                record
                for record in restored_records
                if record.plan.collapse_id not in active_collapse_ids
            )
        restored_record = latest_merge_train_stack_collapse_progress_record(restored_records)
        if restored_record is not None:
            if request.mutate:
                lease.checkpoint(
                    active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
                    active_phase="resume_returned_stack_root",
                    active_record_id=restored_record.record_id,
                    active_pull_request_number=selected_pr.number,
                )
                restored_record = build_merge_train_stack_collapse_plan_record(
                    plan=restored_record.plan.model_copy(update={"updated_at": recorded_at}),
                    source=f"service:controller:resume-retired-collapse:{trace_id}",
                    updated_at=recorded_at,
                )
                stack_collapse_store.write_merge_train_stack_collapse_plan_record(restored_record)
            restored_result, snapshot = _advance_waiting_stack_collapse_record(
                request=request,
                policy=policy,
                policy_sha256=policy_sha256,
                repository_policy=policy.find_repository_policy(
                    repository=request.repository, base_branch=request.base_branch
                ),
                transport=transport,
                github_client=github_client,
                candidate_store=candidate_store,
                stack_collapse_store=stack_collapse_store,
                waiting_collapse_record=restored_record,
                trace_id=trace_id,
                recorded_at=recorded_at,
                lease=lease,
                snapshot=snapshot,
            )
            if restored_result is not None:
                if not request.mutate:
                    # A retired record is not an actionable phase handle.
                    restored_result.pop("merge_train_stack_collapse_plan_record_id", None)
                return restored_result
    if dry_run_result.intended_next_action == "block":
        # Report the queue's failure before rediscovering still-open carried children.
        return _apply_queue_block(
            request=request,
            dry_run_result=dry_run_result,
            github_client=github_client,
            lease=lease,
        )
    if selected_pr is not None and merge_train_snapshot_has_stack_topology(
        snapshot=snapshot, dry_run_result=dry_run_result
    ):
        stack_discovery = discover_merge_train_stack(
            policy=policy,
            snapshot=snapshot,
            root_pull_request_number=selected_pr.number,
        )
    else:
        stack_discovery = None
    if stack_discovery is not None and stack_discovery.status == "ready_for_collapse":
        preparation_block = _prepare_new_stack_disposition(
            request=request,
            repository_policy=policy.find_repository_policy(
                repository=request.repository, base_branch=request.base_branch
            ),
            github_client=github_client,
            lease=lease,
        )
        if preparation_block is not None:
            preparation_block["stack_discovery"] = stack_discovery.model_dump(mode="json")
            return preparation_block
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
                ordinary_job_binding=lease.record.ordinary_job_binding,
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
            "blocking_reason": {
                "code": "merge_train_stack_unsupported",
                "message": "; ".join(stack_discovery.unsupported_reasons),
            },
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
    probe = _ConflictProbeOutcome(snapshot, dry_run_result, held_out)
    if dry_run_result.intended_next_action == "merge":
        probe = _probe_queue_entry_conflicts(
            github_client=github_client,
            policy=policy,
            snapshot=snapshot,
            dry_run_result=dry_run_result,
            held_out=held_out,
            mutate=request.mutate,
            enabled=lease.record.ordinary_job_binding is None,
            lease=lease,
            new_candidate=True,
        )
        dry_run_result = probe.dry_run_result
        selected_pr = dry_run_result.selected_pr
    if (
        dry_run_result.intended_next_action == "update_branch"
        and request.mutate
        and selected_pr is not None
    ):
        lease.checkpoint(
            active_action="update_branch",
            active_phase="update_pull_request_branch",
            active_record_id="",
            active_pull_request_number=selected_pr.number,
            step_payload={"expected_head_sha": selected_pr.head_sha},
        )
        branch_update_result = apply_merge_train_branch_update_intent(
            dry_run_result=dry_run_result, branch_client=github_client
        )
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "update_branch",
            "controller_action": "update_branch",
            "dry_run_result": dry_run_result.model_dump(mode="json"),
            "branch_update_result": branch_update_result.model_dump(mode="json"),
            "conflict_probe": probe.report,
        }
    if dry_run_result.intended_next_action != "merge":
        return {
            "repository": request.repository,
            "base_branch": request.base_branch,
            "mode": "dry-run",
            "controller_action": dry_run_result.intended_next_action,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
        }

    if lease.record.ordinary_job_binding is None:
        recovery = _resume_known_failed_batch(
            candidate_store=candidate_store,
            github_client=github_client,
            policy=policy,
            policy_sha256=policy_sha256,
            snapshot=probe.snapshot,
            dry_run_result=probe.dry_run_result,
            held_out=probe.held_out,
            merge_method=policy.find_repository_policy(
                repository=request.repository, base_branch=request.base_branch
            ).merge_method,
            recorded_at=recorded_at,
            trace_id=trace_id,
            mutate=request.mutate,
            lease=lease,
            conflict_probe=probe.report,
        )
        if recovery is not None:
            return recovery
    controller_action = "plan_candidate"
    candidate = build_merge_train_batch_candidate(
        ordinary_job_binding=lease.record.ordinary_job_binding,
        dry_run_result=probe.dry_run_result,
        base_sha=probe.snapshot.base_sha,
        policy_sha256=policy_sha256,
        created_at=recorded_at,
        held_out=probe.held_out,
    )
    result = {
        "repository": candidate.repository,
        "base_branch": candidate.base_branch,
        "mode": "dry-run" if not request.mutate else controller_action,
        "controller_action": controller_action,
        "dry_run_result": probe.dry_run_result.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
    }
    if probe.report is not None:
        result["conflict_probe"] = probe.report
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
            ordinary_job_binding=lease.record.ordinary_job_binding,
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
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    active_candidate_record: MergeTrainBatchCandidateRecord,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    transport: MergeTrainGitHubTransport,
    github_client: GitHubMergeTrainClient,
    repository: str,
    base_branch: str,
    merge_method: str,
    recorded_at: str,
    trace_id: str,
    mutate: bool,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object] | None:
    try:
        snapshot = github_client.read_merge_train_snapshot(
            repository=repository,
            base_branch=base_branch,
        )
    except Exception:
        lease.observe(reason_code="recovery_queue_read_unavailable")
        return None
    snapshot = _without_obsolete_stack_collapse_pull_requests(
        snapshot=snapshot,
        records=_read_obsolete_stack_collapse_records(
            store=stack_collapse_store,
            github_client=github_client,
            snapshot=snapshot,
            repository_policy=policy.find_repository_policy(
                repository=repository, base_branch=base_branch
            ),
            policy_sha256=policy_sha256,
        ),
    )
    held_out = _surviving_held_out_entries(
        policy=policy,
        snapshot=snapshot,
        held_out=active_candidate_record.candidate.held_out,
        batch_landing=active_candidate_record.ordinary_job_binding is None,
        isolating_entries=active_candidate_record.candidate.entries,
    )
    snapshot = _without_held_out_pull_requests(snapshot=snapshot, held_out=held_out)
    dry_run_result = build_merge_train_dry_run_result(
        policy=policy,
        snapshot=snapshot,
        batch_landing=active_candidate_record.ordinary_job_binding is None,
        existing_candidate_size=len(active_candidate_record.candidate.entries),
    )
    queue_unchanged = _merge_train_candidate_matches_dry_run_queue(
        candidate=active_candidate_record.candidate,
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
    )
    current_queue = build_merge_train_dry_run_result(
        policy=policy,
        snapshot=snapshot,
        batch_landing=active_candidate_record.ordinary_job_binding is None,
    )
    # Readiness of a changed queue belongs to its newly capped selection. An
    # original-PR landing may also need a branch refresh before recovery.
    if not queue_unchanged or current_queue.intended_next_action == "update_branch":
        dry_run_result = current_queue
    if (
        dry_run_result.intended_next_action == "wait_for_checks"
        and active_candidate_record.ordinary_job_binding is None
    ):
        return {
            "repository": repository,
            "base_branch": base_branch,
            "mode": "dry-run",
            "controller_action": "wait_for_checks",
            "reason_code": "original_head_checks_pending",
            "merge_train_batch_candidate_record_id": active_candidate_record.record_id,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
        }
    if dry_run_result.intended_next_action not in {"merge", "update_branch"}:
        return None
    if (
        dry_run_result.intended_next_action == "merge"
        and queue_unchanged
        and active_candidate_record.candidate.candidate_sha
    ):
        failed = active_candidate_record.candidate
        if _uses_protected_candidate_pr(
            candidate_record=active_candidate_record,
            merge_method=merge_method,
            dry_run_result=dry_run_result,
        ):
            recovered = _retry_closed_candidate_after_passing_checks(
                candidate_store=candidate_store,
                active_candidate_record=active_candidate_record,
                github_client=github_client,
                policy=policy,
                policy_sha256=policy_sha256,
                snapshot=snapshot,
                dry_run_result=dry_run_result,
                held_out=held_out,
                repository=repository,
                base_branch=base_branch,
                recorded_at=recorded_at,
                trace_id=trace_id,
                mutate=mutate,
                lease=lease,
            )
            if recovered is not None:
                return recovered
            # Real failures must shrink the batch before any generated-body retry.
            recovery = _recover_failed_merge_train_candidate(
                candidate_store=candidate_store,
                active_candidate_record=active_candidate_record,
                github_client=github_client,
                policy=policy,
                policy_sha256=policy_sha256,
                snapshot=snapshot,
                dry_run_result=dry_run_result,
                held_out=held_out,
                repository=repository,
                base_branch=base_branch,
                recorded_at=recorded_at,
                trace_id=trace_id,
                mutate=mutate,
                lease=lease,
            )
            if recovery is not None:
                return recovery
            return {
                "repository": repository,
                "base_branch": base_branch,
                "mode": "dry-run",
                "controller_action": "candidate_failed",
                "merge_train_batch_candidate_record_id": active_candidate_record.record_id,
                "candidate": failed.model_dump(mode="json"),
                "reason_code": "failed_candidate_recovery_unavailable",
            }
        else:
            reobserved = _reobserve_failed_merge_train_candidate(
                candidate_store=candidate_store,
                active_candidate_record=active_candidate_record,
                github_client=github_client,
                merge_method=merge_method,
                repository=repository,
                base_branch=base_branch,
                recorded_at=recorded_at,
                trace_id=trace_id,
                mutate=mutate,
                lease=lease,
                held_out=held_out,
            )
            if reobserved is not None:
                return reobserved
            return _recover_failed_merge_train_candidate(
                candidate_store=candidate_store,
                active_candidate_record=active_candidate_record,
                github_client=github_client,
                policy=policy,
                policy_sha256=policy_sha256,
                snapshot=snapshot,
                dry_run_result=dry_run_result,
                held_out=held_out,
                repository=repository,
                base_branch=base_branch,
                recorded_at=recorded_at,
                trace_id=trace_id,
                mutate=mutate,
                lease=lease,
            )
    # The old membership comparison is complete; select a new capped replacement.
    dry_run_result = current_queue
    probe = _ConflictProbeOutcome(snapshot, dry_run_result, held_out)
    if dry_run_result.intended_next_action == "merge":
        probe = _probe_queue_entry_conflicts(
            github_client=github_client,
            policy=policy,
            snapshot=snapshot,
            dry_run_result=dry_run_result,
            held_out=held_out,
            mutate=mutate,
            enabled=active_candidate_record.ordinary_job_binding is None,
            lease=lease,
        )
    dry_run_result = probe.dry_run_result
    if (
        dry_run_result.intended_next_action == "update_branch"
        and mutate
        and dry_run_result.selected_pr is not None
    ):
        # The queue head is behind its base; a failed candidate must not keep it
        # from being refreshed. Retire the failed candidate, then update the branch.
        lease.checkpoint(
            active_action="update_branch",
            active_phase="update_pull_request_branch",
            active_record_id=active_candidate_record.record_id,
            active_pull_request_number=dry_run_result.selected_pr.number,
            step_payload={"expected_head_sha": dry_run_result.selected_pr.head_sha},
        )
        _supersede_active_merge_train_batch_candidate_records(
            record_store=candidate_store,
            repository=repository,
            base_branch=base_branch,
            batch_id=active_candidate_record.candidate.batch_id,
            replacement_record_id=None,
        )
        branch_update_result = apply_merge_train_branch_update_intent(
            dry_run_result=dry_run_result, branch_client=github_client
        )
        return {
            "repository": repository,
            "base_branch": base_branch,
            "mode": "update_branch",
            "controller_action": "update_branch",
            "superseded_merge_train_batch_candidate_record_id": active_candidate_record.record_id,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
            "branch_update_result": branch_update_result.model_dump(mode="json"),
            "conflict_probe": probe.report,
        }
    if probe.dry_run_result.intended_next_action != "merge":
        return None
    if active_candidate_record.ordinary_job_binding is None:
        recovery = _resume_known_failed_batch(
            candidate_store=candidate_store,
            github_client=github_client,
            policy=policy,
            policy_sha256=policy_sha256,
            snapshot=probe.snapshot,
            dry_run_result=probe.dry_run_result,
            held_out=probe.held_out,
            merge_method=merge_method,
            recorded_at=recorded_at,
            trace_id=trace_id,
            mutate=mutate,
            lease=lease,
            conflict_probe=probe.report,
        )
        if recovery is not None:
            if mutate:
                # The matching historical failure can belong to another batch.
                # Retire the current lineage too, without retiring the replacement.
                replacement_id = recovery.get("merge_train_batch_candidate_record_id")
                if recovery["controller_action"] == "block" and "candidate" not in recovery:
                    replacement_id = None
                details = recovery.get("details")
                recovery_source_id = recovery.get(
                    "superseded_merge_train_batch_candidate_record_id"
                ) or (
                    details.get("recovery_source_record_id") if isinstance(details, dict) else None
                )
                lease.checkpoint(
                    active_action="reflow_candidate",
                    active_phase="retire_reflowed_failed_lineage",
                    active_record_id=active_candidate_record.record_id,
                    active_pull_request_number=None,
                    step_payload={
                        "retired_candidate_record_id": active_candidate_record.record_id,
                        "recovery_source_record_id": recovery_source_id,
                        "replacement_record_id": replacement_id,
                    },
                )
                _supersede_active_merge_train_batch_candidate_records(
                    record_store=candidate_store,
                    repository=repository,
                    base_branch=base_branch,
                    batch_id=active_candidate_record.candidate.batch_id,
                    replacement_record_id=(
                        replacement_id if isinstance(replacement_id, str) else None
                    ),
                )
                recovery["details"] = {
                    **(details if isinstance(details, dict) else {}),
                    "retired_candidate_record_id": active_candidate_record.record_id,
                }
                recovery["retired_merge_train_batch_candidate_record_id"] = (
                    active_candidate_record.record_id
                )
            return recovery
    candidate = build_merge_train_batch_candidate(
        ordinary_job_binding=active_candidate_record.ordinary_job_binding,
        dry_run_result=probe.dry_run_result,
        base_sha=probe.snapshot.base_sha,
        policy_sha256=policy_sha256,
        created_at=recorded_at,
        held_out=probe.held_out,
    )
    if (
        active_candidate_record.candidate.candidate_sha
        and active_candidate_record.ordinary_job_binding is None
        and _uses_protected_candidate_pr(
            candidate_record=active_candidate_record,
            merge_method=merge_method,
            dry_run_result=probe.dry_run_result,
        )
        and _merge_train_candidate_matches_dry_run_queue(
            candidate=active_candidate_record.candidate,
            dry_run_result=probe.dry_run_result,
            base_sha=probe.snapshot.base_sha,
        )
    ):
        return _stop_failed_batch_after_conflict_probe(
            candidate_store=candidate_store,
            active_candidate_record=active_candidate_record,
            probe=probe,
            repository=repository,
            base_branch=base_branch,
            recorded_at=recorded_at,
            trace_id=trace_id,
            mutate=mutate,
            lease=lease,
        )
    result: dict[str, object] = {
        "repository": candidate.repository,
        "base_branch": candidate.base_branch,
        "mode": "dry-run" if not mutate else "plan_candidate",
        "controller_action": "plan_candidate",
        "superseded_merge_train_batch_candidate_record_id": active_candidate_record.record_id,
        "dry_run_result": probe.dry_run_result.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
    }
    if probe.report is not None and not candidate.batch_body_retry_of:
        result["conflict_probe"] = probe.report
    if mutate:
        result["merge_train_batch_candidate_record_id"] = _persist_replacement_candidate(
            candidate_store=candidate_store,
            active_candidate_record=active_candidate_record,
            candidate=candidate,
            repository=repository,
            base_branch=base_branch,
            recorded_at=recorded_at,
            trace_id=trace_id,
            lease=lease,
        )
    return result


def _candidate_history_on_base(
    *,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    repository: str,
    base_branch: str,
    base_sha: str,
) -> tuple[MergeTrainBatchCandidateRecord, ...]:
    return cast(
        MergeTrainBatchCandidateBaseHistoryReadStore, candidate_store
    ).list_merge_train_batch_candidate_records(
        repository=repository, base_branch=base_branch, base_sha=base_sha
    )


def _resume_known_failed_batch(
    *,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    github_client: GitHubMergeTrainClient,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    snapshot: MergeTrainDryRunSnapshot,
    dry_run_result: MergeTrainDryRunResult,
    held_out: tuple[MergeTrainBatchHeldOutEntry, ...],
    merge_method: str,
    recorded_at: str,
    trace_id: str,
    mutate: bool,
    lease: MergeTrainControllerLeaseContext,
    conflict_probe: dict[str, object] | None = None,
) -> dict[str, object] | None:
    """Resume recorded failure isolation rather than rebuild identical failed inputs."""
    compatible_history = tuple(
        record
        for record in _candidate_history_on_base(
            candidate_store=candidate_store,
            repository=snapshot.repository,
            base_branch=snapshot.base_branch,
            base_sha=snapshot.base_sha,
        )
        if record.ordinary_job_binding is None
        and record.candidate.policy_key == dry_run_result.policy_key
        and (
            merge_train_policy_bindings_match(
                record_store=candidate_store,
                policy_key=record.candidate.policy_key,
                recorded_sha256=record.candidate.policy_sha256,
                current_sha256=policy_sha256,
            )
        )
    )
    matching_records = tuple(
        record
        for record in compatible_history
        if _merge_train_candidate_matches_dry_run_queue(
            candidate=record.candidate,
            dry_run_result=dry_run_result,
            base_sha=snapshot.base_sha,
        )
    )
    if matching_records:
        latest = matching_records[0]
        retry = latest.candidate
        parent = next(
            (
                record
                for record in compatible_history
                if record.record_id
                in {retry.check_retry_of, retry.infrastructure_retry_of, retry.batch_body_retry_of}
                and record.candidate.required_checks_status == "fail"
            ),
            None,
        )
        unfinished_retry = (
            parent is not None
            and latest.status == "superseded"
            and retry.status in {"planned", "building", "ready_for_checks"}
        )
        if (
            parent is not None
            and latest.status == "superseded"
            and retry.status == "failed"
            and retry.required_checks_status not in {"pass", "fail"}
        ):
            return _stop_failed_batch_after_conflict_probe(
                candidate_store=candidate_store,
                active_candidate_record=latest,
                probe=_ConflictProbeOutcome(snapshot, dry_run_result, held_out, conflict_probe),
                repository=snapshot.repository,
                base_branch=snapshot.base_branch,
                recorded_at=recorded_at,
                trace_id=trace_id,
                mutate=mutate,
                lease=lease,
                stopped_result={"reason_code": "candidate_retry_unavailable"},
            )
        if unfinished_retry and retry.candidate_sha:
            try:
                lease.observe(candidate_checks_status="unknown", candidate_sha=retry.candidate_sha)
                observed = github_client.observe_batch_candidate_checks(candidate=retry)
                lease.observe(candidate_checks_status=observed.required_checks_status)
                closed = bound_closed_batch_pull_request(client=github_client, candidate=retry)
            except (MergeTrainGitHubError, MergeAdmissionDeniedError):
                return _stop_failed_batch_after_conflict_probe(
                    candidate_store=candidate_store,
                    active_candidate_record=latest,
                    probe=_ConflictProbeOutcome(snapshot, dry_run_result, held_out, conflict_probe),
                    repository=snapshot.repository,
                    base_branch=snapshot.base_branch,
                    recorded_at=recorded_at,
                    trace_id=trace_id,
                    mutate=mutate,
                    lease=lease,
                    stopped_result={"reason_code": "candidate_retry_evidence_unavailable"},
                )
            if observed.required_checks_status == "fail":
                latest = latest.model_copy(update={"candidate": observed})
                matching_records = (latest, *matching_records[1:])
                unfinished_retry = False
            elif closed is not None and latest.source.startswith(_QUEUE_RETRY_RETIREMENT_SOURCE):
                # The controller intentionally closed this unfinished retry.
                # Reconstruct it on its existing budget/ref, after rereading S.
                retry = retry.model_copy(update={"candidate_sha": ""})
            elif closed is not None:
                return _stop_failed_batch_after_conflict_probe(
                    candidate_store=candidate_store,
                    active_candidate_record=latest,
                    probe=_ConflictProbeOutcome(snapshot, dry_run_result, held_out, conflict_probe),
                    repository=snapshot.repository,
                    base_branch=snapshot.base_branch,
                    recorded_at=recorded_at,
                    trace_id=trace_id,
                    mutate=mutate,
                    lease=lease,
                    stopped_result={"reason_code": "candidate_retry_closed"},
                )
            else:
                # Construction already finished: resume observing this exact SHA.
                retry = observed
        if unfinished_retry and retry.batch_body_retry_of and parent is not None:
            # Legacy body retries did not require passing evidence. Keep the
            # current no-rebuild rule when resuming one of those records too.
            try:
                unfinished_retry = (
                    github_client.observe_batch_candidate_checks(
                        candidate=parent.candidate
                    ).required_checks_status
                    == "pass"
                )
            except MergeTrainGitHubError:
                unfinished_retry = False
        if unfinished_retry:
            candidate = build_merge_train_batch_candidate(
                dry_run_result=dry_run_result,
                base_sha=snapshot.base_sha,
                policy_sha256=policy_sha256,
                created_at=recorded_at,
                held_out=held_out,
            ).model_copy(
                update={
                    "batch_id": retry.batch_id,
                    "candidate_ref": retry.candidate_ref,
                    "check_retry_of": retry.check_retry_of,
                    "check_retry_evidence": retry.check_retry_evidence,
                    "infrastructure_retry_of": retry.infrastructure_retry_of,
                    "batch_body_retry_of": retry.batch_body_retry_of,
                }
            )
            if retry.candidate_sha:
                candidate = retry.model_copy(update={"held_out": held_out})
            result: dict[str, object] = {
                "repository": snapshot.repository,
                "base_branch": snapshot.base_branch,
                "mode": "plan_candidate" if mutate else "dry-run",
                "controller_action": "plan_candidate",
                "reason_code": "candidate_retry_resumed",
                "superseded_merge_train_batch_candidate_record_id": latest.record_id,
                "candidate": candidate.model_dump(mode="json"),
                "dry_run_result": dry_run_result.model_dump(mode="json"),
            }
            if mutate:
                result["merge_train_batch_candidate_record_id"] = _persist_replacement_candidate(
                    candidate_store=candidate_store,
                    active_candidate_record=latest,
                    candidate=candidate,
                    repository=snapshot.repository,
                    base_branch=snapshot.base_branch,
                    recorded_at=recorded_at,
                    trace_id=trace_id,
                    lease=lease,
                )
            return result
    failed_record = next(
        (
            record
            for record in matching_records
            if record.candidate.candidate_sha
            and (
                record.candidate.status == "failed"
                and record.candidate.required_checks_status == "fail"
                or record.candidate.status == "passed"
                and record.candidate.required_checks_status == "pass"
            )
        ),
        None,
    )
    if failed_record is None or failed_record.candidate.status == "passed":
        return None
    if _uses_protected_candidate_pr(
        candidate_record=failed_record, merge_method=merge_method, dry_run_result=dry_run_result
    ):
        recovered = _retry_closed_candidate_after_passing_checks(
            candidate_store=candidate_store,
            active_candidate_record=failed_record,
            github_client=github_client,
            policy=policy,
            policy_sha256=policy_sha256,
            snapshot=snapshot,
            dry_run_result=dry_run_result,
            held_out=held_out,
            repository=snapshot.repository,
            base_branch=snapshot.base_branch,
            recorded_at=recorded_at,
            trace_id=trace_id,
            mutate=mutate,
            lease=lease,
        )
    else:
        recovered = _reobserve_failed_merge_train_candidate(
            candidate_store=candidate_store,
            active_candidate_record=failed_record,
            github_client=github_client,
            merge_method=merge_method,
            repository=snapshot.repository,
            base_branch=snapshot.base_branch,
            recorded_at=recorded_at,
            trace_id=trace_id,
            mutate=mutate,
            lease=lease,
            held_out=held_out,
        )
    recovery = recovered or _recover_failed_merge_train_candidate(
        candidate_store=candidate_store,
        active_candidate_record=failed_record,
        github_client=github_client,
        policy=policy,
        policy_sha256=policy_sha256,
        snapshot=snapshot,
        dry_run_result=dry_run_result,
        held_out=held_out,
        repository=snapshot.repository,
        base_branch=snapshot.base_branch,
        recorded_at=recorded_at,
        trace_id=trace_id,
        mutate=mutate,
        lease=lease,
    )
    stopped = recovery or {
        "repository": snapshot.repository,
        "base_branch": snapshot.base_branch,
        "mode": "dry-run",
        "controller_action": "candidate_failed",
        "merge_train_batch_candidate_record_id": failed_record.record_id,
        "candidate": failed_record.candidate.model_dump(mode="json"),
        "reason_code": "failed_candidate_recovery_unavailable",
    }
    if stopped["controller_action"] != "candidate_failed" and not (
        stopped["controller_action"] == "block" and held_out
    ):
        return stopped
    return _stop_failed_batch_after_conflict_probe(
        candidate_store=candidate_store,
        active_candidate_record=failed_record,
        probe=_ConflictProbeOutcome(snapshot, dry_run_result, held_out, conflict_probe),
        repository=snapshot.repository,
        base_branch=snapshot.base_branch,
        recorded_at=recorded_at,
        trace_id=trace_id,
        mutate=mutate,
        lease=lease,
        stopped_result=stopped,
    )


def _persist_replacement_candidate(
    *,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    active_candidate_record: MergeTrainBatchCandidateRecord,
    candidate: MergeTrainBatchCandidate,
    repository: str,
    base_branch: str,
    recorded_at: str,
    trace_id: str,
    lease: MergeTrainControllerLeaseContext,
) -> str:
    # The probe can outlast the lease; renew it, or stop, before persisting.
    lease.checkpoint(
        active_action="reflow_candidate",
        active_phase="persist_replacement_candidate",
        active_record_id=active_candidate_record.record_id,
        active_pull_request_number=None,
        step_payload={
            "candidate_record_id": active_candidate_record.record_id,
            "batch_id": candidate.batch_id,
            "candidate_ref": candidate.candidate_ref,
        },
    )
    candidate_record = build_merge_train_batch_candidate_record(
        ordinary_job_binding=active_candidate_record.ordinary_job_binding,
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
    return candidate_record.record_id


def _retry_closed_candidate_after_passing_checks(
    *,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    active_candidate_record: MergeTrainBatchCandidateRecord,
    github_client: GitHubMergeTrainClient,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    snapshot: MergeTrainDryRunSnapshot,
    dry_run_result: MergeTrainDryRunResult,
    held_out: tuple[MergeTrainBatchHeldOutEntry, ...],
    repository: str,
    base_branch: str,
    recorded_at: str,
    trace_id: str,
    mutate: bool,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object] | None:
    failed = active_candidate_record.candidate
    lease.observe_failed_candidate(active_candidate_record)
    if failed.required_checks_status != "fail":
        lease.observe(reason_code="candidate_not_failed_checks")
        return None
    try:
        lease.observe(candidate_checks_status="unknown")
        observed = github_client.observe_batch_candidate_checks(candidate=failed)
        lease.observe(candidate_checks_status=observed.required_checks_status)
        if observed.required_checks_status != "pass":
            lease.observe(reason_code="candidate_checks_not_recovered")
            return None
        closed = bound_closed_batch_pull_request(client=github_client, candidate=failed)
        legacy_singleton = (
            closed is None
            and len(failed.entries) == 1
            and not has_batch_pull_request(client=github_client, candidate=failed)
        )
    except (MergeTrainGitHubError, MergeAdmissionDeniedError):
        lease.observe(reason_code="candidate_retry_evidence_unavailable")
        return None
    if closed is None:
        # Pre-existing singleton records may have no generated PR. Preserve
        # their exact-SHA recovery; a closed generated PR still needs a new ref.
        if legacy_singleton:
            return _reobserve_failed_merge_train_candidate(
                candidate_store=candidate_store,
                active_candidate_record=active_candidate_record,
                github_client=github_client,
                merge_method="merge",
                repository=repository,
                base_branch=base_branch,
                recorded_at=recorded_at,
                trace_id=trace_id,
                mutate=mutate,
                lease=lease,
                held_out=held_out,
            )
        lease.observe(reason_code="candidate_closed_batch_evidence_unavailable")
        return None
    details: dict[str, object] = {
        "observed_candidate_sha": failed.candidate_sha,
        "controller_pass_recorded_at": recorded_at,
        "required_checks_status": observed.required_checks_status,
        "closed_batch_pull_request_number": closed["number"],
    }
    # One replacement per failed record, not per unchanged queue. A replacement
    # may itself fail transiently and later pass its exact-head rerun. Earlier
    # recovery on the same base must not leave that new failure stuck forever.
    retry_used = any(
        record.candidate.check_retry_of == active_candidate_record.record_id
        for record in _candidate_history_on_base(
            candidate_store=candidate_store,
            repository=repository,
            base_branch=base_branch,
            base_sha=snapshot.base_sha,
        )
    )
    if retry_used:
        # This budget cannot suppress a still-unused generated-body retry.
        body_retry_used = failed.batch_body_retry_of or any(
            record.candidate.batch_body_retry_of
            and _merge_train_candidate_matches_dry_run_queue(
                candidate=record.candidate,
                dry_run_result=dry_run_result,
                base_sha=snapshot.base_sha,
            )
            for record in _candidate_history_on_base(
                candidate_store=candidate_store,
                repository=repository,
                base_branch=base_branch,
                base_sha=snapshot.base_sha,
            )
        )
        try:
            if not body_retry_used and changed_closed_batch_body(
                client=github_client, candidate=failed
            ):
                return _plan_failed_candidate_replacement(
                    reason_code="closed_batch_body_changed",
                    candidate_store=candidate_store,
                    github_client=github_client,
                    active_candidate_record=active_candidate_record,
                    policy=policy,
                    policy_sha256=policy_sha256,
                    snapshot=snapshot,
                    holds=held_out,
                    details=details,
                    repository=repository,
                    base_branch=base_branch,
                    recorded_at=recorded_at,
                    trace_id=trace_id,
                    mutate=mutate,
                    lease=lease,
                )
        except (MergeTrainGitHubError, MergeAdmissionDeniedError):
            pass
        return {
            "repository": repository,
            "base_branch": base_branch,
            "mode": "dry-run",
            "controller_action": "candidate_failed",
            "merge_train_batch_candidate_record_id": active_candidate_record.record_id,
            "candidate": failed.model_dump(mode="json"),
            "reason_code": "candidate_check_retry_used",
            "details": details,
        }
    return _plan_failed_candidate_replacement(
        reason_code="candidate_checks_recovered",
        candidate_store=candidate_store,
        github_client=github_client,
        active_candidate_record=active_candidate_record,
        policy=policy,
        policy_sha256=policy_sha256,
        snapshot=snapshot,
        holds=held_out,
        details=details,
        repository=repository,
        base_branch=base_branch,
        recorded_at=recorded_at,
        trace_id=trace_id,
        mutate=mutate,
        lease=lease,
    )


def _recover_failed_merge_train_candidate(
    *,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    active_candidate_record: MergeTrainBatchCandidateRecord,
    github_client: GitHubMergeTrainClient,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    snapshot: MergeTrainDryRunSnapshot,
    dry_run_result: MergeTrainDryRunResult,
    held_out: tuple[MergeTrainBatchHeldOutEntry, ...],
    repository: str,
    base_branch: str,
    recorded_at: str,
    trace_id: str,
    mutate: bool,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object] | None:
    """Move an unchanged candidate that failed its checks toward landing the rest.

    An infrastructure failure is rebuilt once with the same members; a second
    one stays stopped without blaming anyone. Other failures stay stopped until
    the base's own checks pass, then retest the first half while the rest wait.
    When one member fails alone on a green base, only that pull request is
    blocked. ``None`` keeps the failure stopped.
    """
    failed = active_candidate_record.candidate
    lease.observe_failed_candidate(active_candidate_record)
    if (
        active_candidate_record.ordinary_job_binding is not None
        or failed.required_checks_status != "fail"
        or not failed.candidate_sha
    ):
        return None
    try:
        failures = github_client.read_candidate_check_failures(candidate=failed)
    except MergeTrainGitHubError:
        lease.observe(reason_code="candidate_failure_read_unavailable")
        return None
    if not failures.conclusive:
        # A re-run in progress or a failure no longer visible blames nobody yet.
        lease.observe(reason_code="candidate_failure_evidence_unavailable")
        return None
    details: dict[str, object] = {
        "failed_checks": [
            {"name": check.name, "conclusion": check.conclusion, "url": check.url}
            for check in failures.checks
        ],
        "failed_tests": list(failures.failed_tests),
    }
    if failures.infrastructure_only:
        if _infrastructure_retry_used(
            candidate_store=candidate_store,
            failed=failed,
            dry_run_result=dry_run_result,
            base_sha=snapshot.base_sha,
        ):
            # Runners failed twice; that blames no pull request. Stay stopped.
            return {
                "repository": repository,
                "base_branch": base_branch,
                "mode": "dry-run",
                "controller_action": "candidate_failed",
                "merge_train_batch_candidate_record_id": active_candidate_record.record_id,
                "candidate": failed.model_dump(mode="json"),
                "reason_code": "infrastructure_retry_used",
                "details": details,
            }
        return _plan_failed_candidate_replacement(
            reason_code="infrastructure_retry",
            candidate_store=candidate_store,
            github_client=github_client,
            active_candidate_record=active_candidate_record,
            policy=policy,
            policy_sha256=policy_sha256,
            snapshot=snapshot,
            holds=held_out,
            details=details,
            repository=repository,
            base_branch=base_branch,
            recorded_at=recorded_at,
            trace_id=trace_id,
            mutate=mutate,
            lease=lease,
        )
    try:
        base_check_status = github_client.read_base_check_status(
            candidate=failed, failed_check_names=failures.check_identity_names
        )
    except MergeTrainGitHubError:
        base_check_status = "unknown"
    lease.observe(base_checks_status=base_check_status)
    if base_check_status != "pass":
        # Failure on an unverified base cannot identify a queued change as its cause.
        base_detail = (
            "has failing checks"
            if base_check_status == "fail"
            else "still has checks pending"
            if base_check_status == "pending"
            else "has missing or unreadable check evidence"
        )
        return {
            "repository": repository,
            "base_branch": base_branch,
            "mode": "dry-run",
            "controller_action": "candidate_failed",
            "merge_train_batch_candidate_record_id": active_candidate_record.record_id,
            "candidate": failed.model_dump(mode="json"),
            "reason_code": "base_checks_not_green",
            "blocking_reason": {
                "code": "merge_train_base_checks_not_green",
                "message": f"The base branch {base_branch} {base_detail}; "
                "queued pull requests are not blamed.",
            },
            "details": {
                **details,
                "base_sha": failed.base_sha,
                "base_checks_status": base_check_status,
            },
        }
    if len(failed.entries) > 1:
        cap = policy.find_repository_policy(
            repository=repository, base_branch=base_branch
        ).max_batch_size
        retest_size = len(failed.entries) // 2
        retested = failed.entries[: min(retest_size, cap) if cap is not None else retest_size]
        waiting = failed.entries[len(retested) :]
        return _plan_failed_candidate_replacement(
            reason_code="failure_isolation",
            candidate_store=candidate_store,
            github_client=github_client,
            active_candidate_record=active_candidate_record,
            policy=policy,
            policy_sha256=policy_sha256,
            snapshot=snapshot,
            holds=(
                *held_out,
                *(
                    MergeTrainBatchHeldOutEntry(
                        pull_request_number=entry.pull_request_number,
                        head_sha=entry.head_sha,
                        reason="failure_isolation",
                        probe_base_sha=failed.base_sha,
                    )
                    for entry in waiting
                ),
            ),
            details=details,
            repository=repository,
            base_branch=base_branch,
            recorded_at=recorded_at,
            trace_id=trace_id,
            mutate=mutate,
            lease=lease,
        )
    return _block_failed_candidate_culprit(
        candidate_store=candidate_store,
        active_candidate_record=active_candidate_record,
        github_client=github_client,
        dry_run_result=dry_run_result,
        details=details,
        repository=repository,
        base_branch=base_branch,
        mutate=mutate,
        lease=lease,
    )


def _infrastructure_retry_used(
    *,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    failed: MergeTrainBatchCandidate,
    dry_run_result: MergeTrainDryRunResult,
    base_sha: str,
) -> bool:
    return bool(failed.infrastructure_retry_of) or any(
        record.candidate.infrastructure_retry_of
        and _merge_train_candidate_matches_dry_run_queue(
            candidate=record.candidate, dry_run_result=dry_run_result, base_sha=base_sha
        )
        for record in _candidate_history_on_base(
            candidate_store=candidate_store,
            repository=failed.repository,
            base_branch=failed.base_branch,
            base_sha=base_sha,
        )
    )


def _plan_failed_candidate_replacement(
    *,
    reason_code: str,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    github_client: GitHubMergeTrainClient,
    active_candidate_record: MergeTrainBatchCandidateRecord,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    snapshot: MergeTrainDryRunSnapshot,
    holds: tuple[MergeTrainBatchHeldOutEntry, ...],
    details: dict[str, object],
    repository: str,
    base_branch: str,
    recorded_at: str,
    trace_id: str,
    mutate: bool,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object] | None:
    failed = active_candidate_record.candidate
    held = {(entry.pull_request_number, entry.head_sha) for entry in holds}
    expected_order = tuple(
        entry.pull_request_number
        for entry in failed.entries
        if (entry.pull_request_number, entry.head_sha) not in held
    )
    plan_dry_run = build_merge_train_dry_run_result(
        policy=policy,
        snapshot=_without_held_out_pull_requests(snapshot=snapshot, held_out=holds),
        batch_landing=True,
    )
    cap = policy.find_repository_policy(
        repository=repository, base_branch=base_branch
    ).max_batch_size
    expected_order = expected_order[:cap]
    if plan_dry_run.intended_next_action != "merge" or plan_dry_run.queue_order != expected_order:
        return None
    if reason_code == "failure_isolation":
        recovery = _resume_known_failed_batch(
            candidate_store=candidate_store,
            github_client=github_client,
            policy=policy,
            policy_sha256=policy_sha256,
            snapshot=snapshot,
            dry_run_result=plan_dry_run,
            held_out=holds,
            merge_method=policy.find_repository_policy(
                repository=repository, base_branch=base_branch
            ).merge_method,
            recorded_at=recorded_at,
            trace_id=trace_id,
            mutate=mutate,
            lease=lease,
        )
        if recovery is not None:
            if mutate:
                replacement_id = recovery.get("merge_train_batch_candidate_record_id")
                lease.checkpoint(
                    active_action="reflow_candidate",
                    active_phase="retire_reflowed_failed_lineage",
                    active_record_id=active_candidate_record.record_id,
                    active_pull_request_number=None,
                    step_payload={"replacement_record_id": replacement_id},
                )
                _supersede_active_merge_train_batch_candidate_records(
                    record_store=candidate_store,
                    repository=repository,
                    base_branch=base_branch,
                    batch_id=failed.batch_id,
                    replacement_record_id=(
                        replacement_id if isinstance(replacement_id, str) else None
                    ),
                )
                recovery["retired_merge_train_batch_candidate_record_id"] = (
                    active_candidate_record.record_id
                )
            return recovery
    candidate = build_merge_train_batch_candidate(
        dry_run_result=plan_dry_run,
        base_sha=snapshot.base_sha,
        policy_sha256=policy_sha256,
        created_at=recorded_at,
        held_out=holds,
    )
    retry_kind = {
        "infrastructure_retry": ("infrastructure-retry", "infrastructure_retry_of"),
        "candidate_checks_recovered": ("check-retry", "check_retry_of"),
        "closed_batch_body_changed": ("body-retry", "batch_body_retry_of"),
    }.get(reason_code)
    if retry_kind is not None:
        # A separate ref keeps the retry from rediscovering the failed run and batch PR.
        check_retry = reason_code == "candidate_checks_recovered"
        suffix, retry_field = retry_kind
        retry_identity = (
            "-" + hashlib.sha256(active_candidate_record.record_id.encode()).hexdigest()[:12]
            if check_retry
            else ""
        )
        batch_id = candidate.batch_id + retry_identity + "-" + suffix
        candidate = MergeTrainBatchCandidate.model_validate(
            {
                **candidate.model_dump(),
                "batch_id": batch_id,
                "candidate_ref": build_merge_train_batch_candidate_ref(
                    repository=repository, base_branch=base_branch, batch_id=batch_id
                ),
                retry_field: active_candidate_record.record_id,
                **(
                    {
                        "check_retry_evidence": {
                            "candidate_sha": details["observed_candidate_sha"],
                            "controller_pass_recorded_at": details["controller_pass_recorded_at"],
                            "required_checks_status": details["required_checks_status"],
                            "closed_batch_pull_request_number": details[
                                "closed_batch_pull_request_number"
                            ],
                        }
                    }
                    if check_retry
                    else {}
                ),
            }
        )
    result: dict[str, object] = {
        "repository": candidate.repository,
        "base_branch": candidate.base_branch,
        "mode": "plan_candidate" if mutate else "dry-run",
        "controller_action": "plan_candidate",
        "reason_code": reason_code,
        "superseded_merge_train_batch_candidate_record_id": active_candidate_record.record_id,
        "dry_run_result": plan_dry_run.model_dump(mode="json"),
        "candidate": candidate.model_dump(mode="json"),
        "details": details,
    }
    if mutate:
        result["merge_train_batch_candidate_record_id"] = _persist_replacement_candidate(
            candidate_store=candidate_store,
            active_candidate_record=active_candidate_record,
            candidate=candidate,
            repository=repository,
            base_branch=base_branch,
            recorded_at=recorded_at,
            trace_id=trace_id,
            lease=lease,
        )
    return result


def _block_failed_candidate_culprit(
    *,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    active_candidate_record: MergeTrainBatchCandidateRecord,
    github_client: GitHubMergeTrainClient,
    dry_run_result: MergeTrainDryRunResult,
    details: dict[str, object],
    repository: str,
    base_branch: str,
    mutate: bool,
    lease: MergeTrainControllerLeaseContext,
) -> dict[str, object] | None:
    """Block the one pull request whose candidate failed, so the rest can land."""
    failed = active_candidate_record.candidate
    culprit = next(
        (
            entry
            for entry in dry_run_result.queue
            if entry.number == failed.entries[0].pull_request_number
            and entry.head_sha == failed.entries[0].head_sha
        ),
        None,
    )
    if culprit is None:
        return None
    detail = (
        f"Pull request #{culprit.number} failed the merge-train candidate checks "
        "when tested without the other queued pull requests."
    )
    block_dry_run = dry_run_result.model_copy(
        update={
            "selected_pr": culprit,
            "intended_next_action": "block",
            "next_action_detail": detail,
        }
    )
    result: dict[str, object] = {
        "repository": repository,
        "base_branch": base_branch,
        "mode": "block" if mutate else "dry-run",
        "controller_action": "block",
        "reason_code": "candidate_failure_isolated",
        "merge_train_batch_candidate_record_id": active_candidate_record.record_id,
        "superseded_merge_train_batch_candidate_record_id": active_candidate_record.record_id,
        "dry_run_result": block_dry_run.model_dump(mode="json"),
        "blocking_reason": {"code": "merge_train_candidate_failure_isolated", "message": detail},
        "details": details,
    }
    if mutate:
        lease.checkpoint(
            active_action=MERGE_TRAIN_CONTROLLER_ACTIVE_ACTION,
            active_phase="block_pull_request",
            active_record_id=active_candidate_record.record_id,
            active_pull_request_number=culprit.number,
            step_payload={"blocked_label": block_dry_run.blocked_label},
        )
        # Retire the failed lineage first: a pass interrupted before the label
        # replans the whole queue instead of keeping the other members held.
        _supersede_active_merge_train_batch_candidate_records(
            record_store=candidate_store,
            repository=repository,
            base_branch=base_branch,
            batch_id=failed.batch_id,
            replacement_record_id=None,
        )
        block_result = apply_merge_train_block_intent(
            dry_run_result=block_dry_run, label_client=github_client
        )
        result["block_result"] = block_result.model_copy(
            update={"train_should_continue": True}
        ).model_dump(mode="json")
    return result


def _reobserve_failed_merge_train_candidate(
    *,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    active_candidate_record: MergeTrainBatchCandidateRecord,
    github_client: GitHubMergeTrainClient,
    merge_method: str,
    repository: str,
    base_branch: str,
    recorded_at: str,
    trace_id: str,
    mutate: bool,
    lease: MergeTrainControllerLeaseContext,
    held_out: tuple[MergeTrainBatchHeldOutEntry, ...],
) -> dict[str, object] | None:
    """Let an unchanged candidate recover when its failed checks pass on a re-run.

    Only a candidate that failed on check evidence is re-read, at its recorded
    SHA. A failed service batch PR is closed and never reopened, so a
    multi-entry merge batch stays failed until its queue changes.
    """
    candidate = active_candidate_record.candidate
    lease.observe_failed_candidate(active_candidate_record)
    if candidate.required_checks_status != "fail":
        return None
    if (
        active_candidate_record.ordinary_job_binding is None
        and len(candidate.entries) > 1
        and merge_method == "merge"
    ):
        return None
    try:
        lease.observe(candidate_checks_status="unknown")
        observed_candidate = github_client.observe_batch_candidate_checks(candidate=candidate)
    except MergeTrainGitHubError:
        lease.observe(reason_code="candidate_check_read_unavailable")
        return None
    lease.observe(candidate_checks_status=observed_candidate.required_checks_status)
    if observed_candidate.required_checks_status == "fail":
        return None
    observed_candidate = observed_candidate.model_copy(update={"held_out": held_out})
    result: dict[str, object] = {
        "repository": repository,
        "base_branch": base_branch,
        "mode": "observe_candidate" if mutate else "dry-run",
        "controller_action": "observe_candidate",
        "superseded_merge_train_batch_candidate_record_id": active_candidate_record.record_id,
        "candidate": observed_candidate.model_dump(mode="json"),
    }
    if mutate:
        lease.checkpoint(
            active_action="reflow_candidate",
            active_phase="persist_reobserved_candidate",
            active_record_id=active_candidate_record.record_id,
            active_pull_request_number=None,
            step_payload={"candidate_record_id": active_candidate_record.record_id},
        )
        candidate_record = build_merge_train_batch_candidate_record(
            ordinary_job_binding=active_candidate_record.ordinary_job_binding,
            candidate=observed_candidate,
            source=f"service:controller:candidate-reobserve:{trace_id}",
            updated_at=recorded_at,
        )
        candidate_store.write_merge_train_batch_candidate_record(candidate_record)
        # Progress ranks a failed record above a passed one in the same batch,
        # so the failed records must be retired for the new evidence to count.
        try:
            _supersede_active_merge_train_batch_candidate_records(
                record_store=candidate_store,
                repository=repository,
                base_branch=base_branch,
                batch_id=candidate.batch_id,
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
    record_store: object | None = None,
) -> None:
    if candidate_record.candidate.policy_key != policy_key:
        raise ValueError("merge train candidate policy key no longer matches")
    if not merge_train_policy_bindings_match(
        record_store=record_store,
        policy_key=candidate_record.candidate.policy_key,
        recorded_sha256=candidate_record.candidate.policy_sha256,
        current_sha256=policy_sha256,
    ):
        raise ValueError("merge train candidate policy digest no longer matches")


def validate_merge_train_landing_record_for_controller(
    *,
    landing_record: MergeTrainBatchLandingPlanRecord,
    policy_key: str,
    policy_sha256: str,
    record_store: object | None = None,
) -> None:
    if landing_record.landing_plan.policy_key != policy_key:
        raise ValueError("merge train landing plan policy key no longer matches")
    if not merge_train_policy_bindings_match(
        record_store=record_store,
        policy_key=landing_record.landing_plan.policy_key,
        recorded_sha256=landing_record.landing_plan.policy_sha256,
        current_sha256=policy_sha256,
    ):
        raise ValueError("merge train landing plan policy digest no longer matches")


def validate_merge_train_stack_collapse_record_for_controller(
    *,
    collapse_record: MergeTrainStackCollapsePlanRecord,
    policy_key: str,
    policy_sha256: str,
    record_store: object | None = None,
) -> None:
    if collapse_record.plan.policy_key != policy_key:
        raise ValueError("merge train stack collapse policy key no longer matches")
    if not merge_train_policy_bindings_match(
        record_store=record_store,
        policy_key=collapse_record.plan.policy_key,
        recorded_sha256=collapse_record.plan.policy_sha256,
        current_sha256=policy_sha256,
    ):
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
    if lease.record.ordinary_job_binding is not None:
        cleanup_status = str(lease.record.step_payload.get("cleanup_status") or "")
        if cleanup_status in {"deleted", "already_missing", "retained"}:
            return {"candidate_ref_cleanup_status": cleanup_status}
        try:
            deleted = github_client.cleanup_batch_candidate_ref(landing_plan=landing_plan)
        except MergeTrainGitHubError as error:
            return _candidate_ref_cleanup_failed_result(
                error=error,
                trace_id=trace_id,
                landing_plan=landing_plan,
            )
        cleanup_status = "deleted" if deleted else "retained"
        lease.checkpoint(
            step_payload={
                **lease.record.step_payload,
                "candidate_ref": candidate_ref,
                "cleanup_status": cleanup_status,
            },
        )
        return {"candidate_ref_cleanup_status": cleanup_status}
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
        return _candidate_ref_cleanup_failed_result(
            error=error,
            trace_id=trace_id,
            landing_plan=landing_plan,
        )
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


def _candidate_ref_cleanup_failed_result(
    *,
    error: MergeTrainGitHubError,
    trace_id: str,
    landing_plan: MergeTrainBatchLandingPlan,
) -> dict[str, object]:
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


def _merge_train_stack_collapse_record_matches_landing_plan(
    *,
    collapse_record: MergeTrainStackCollapsePlanRecord,
    landing_plan: MergeTrainBatchLandingPlan,
    policy_sha256: str,
    record_store: object | None = None,
) -> bool:
    try:
        validate_stack_collapse_record_for_landing(
            collapse_record=collapse_record,
            landing_plan=landing_plan,
            policy_sha256=policy_sha256,
            record_store=record_store,
        )
    except ValueError:
        return False
    return True


@dataclass(frozen=True)
class _ConflictProbeOutcome:
    snapshot: MergeTrainDryRunSnapshot
    dry_run_result: MergeTrainDryRunResult
    held_out: tuple[MergeTrainBatchHeldOutEntry, ...]
    report: dict[str, object] | None = None


def _conflict_probe_queue(
    dry_run_result: MergeTrainDryRunResult,
) -> tuple[MergeTrainQueueEntry, ...]:
    """Return the queue a candidate would batch, when it needs a conflict probe."""
    queue_by_number = {entry.number: entry for entry in dry_run_result.queue}
    queue = tuple(queue_by_number[number] for number in dry_run_result.queue_order)
    return queue if len(queue) > 1 else ()


def _probe_queue_entry_conflicts(
    *,
    github_client: GitHubMergeTrainClient,
    policy: MergeTrainPolicy,
    snapshot: MergeTrainDryRunSnapshot,
    dry_run_result: MergeTrainDryRunResult,
    held_out: tuple[MergeTrainBatchHeldOutEntry, ...],
    mutate: bool,
    enabled: bool,
    lease: MergeTrainControllerLeaseContext,
    new_candidate: bool = False,
) -> _ConflictProbeOutcome:
    """Hold out queued pull requests that conflict with the ones ahead of them.

    GitHub computes a pull request's mergeability only against its base, so two
    queued pull requests can each be clean and still conflict with each other.
    A mutating pass probes the queue before planning a multi-entry candidate,
    so the conflict is held out instead of found by a failed build. A dry run
    writes no ref and reports that the probe will run. Retired ordinary-agent
    jobs route every effect through their own ledger, so they keep only the
    build-time conflict handling.
    """
    unchanged = _ConflictProbeOutcome(
        snapshot=snapshot, dry_run_result=dry_run_result, held_out=held_out
    )
    queue = _conflict_probe_queue(dry_run_result)
    if not enabled or not queue:
        return unchanged
    probed_pull_request_numbers = [entry.number for entry in queue]
    if not mutate:
        return replace(
            unchanged,
            report={"status": "will_run", "pull_request_numbers": probed_pull_request_numbers},
        )
    probe_ref = merge_train_conflict_probe_ref(
        repository=dry_run_result.repository,
        base_branch=dry_run_result.base_branch,
        lease_owner=lease.owner,
        lease_acquired_at=lease.acquisition_token,
    )
    active_record_id = lease.record.active_record_id

    def checkpoint_probe(pull_request_number: int | None) -> None:
        lease.checkpoint(
            active_action="plan_candidate",
            active_phase="probe_entry_conflicts",
            active_record_id=active_record_id,
            active_pull_request_number=pull_request_number,
            step_payload={"probe_ref": probe_ref},
        )

    conflicts = github_client.probe_batch_entry_conflicts(
        repository=dry_run_result.repository,
        base_branch=dry_run_result.base_branch,
        base_sha=snapshot.base_sha,
        queue=queue,
        probe_ref=probe_ref,
        checkpoint=checkpoint_probe,
    )
    report: dict[str, object] = {
        "status": "ran",
        "pull_request_numbers": probed_pull_request_numbers,
        "held_out": [_held_out_diagnostic(entry) for entry in conflicts],
    }
    if not conflicts:
        return replace(unchanged, report=report)
    held_out = (*held_out, *conflicts)
    snapshot = _without_held_out_pull_requests(snapshot=snapshot, held_out=held_out)
    return _ConflictProbeOutcome(
        snapshot=snapshot,
        dry_run_result=build_merge_train_dry_run_result(
            policy=policy, snapshot=snapshot, batch_landing=enabled, new_candidate=new_candidate
        ),
        held_out=held_out,
        report=report,
    )


def _stop_failed_batch_after_conflict_probe(
    *,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    active_candidate_record: MergeTrainBatchCandidateRecord,
    probe: _ConflictProbeOutcome,
    repository: str,
    base_branch: str,
    recorded_at: str,
    trace_id: str,
    mutate: bool,
    lease: MergeTrainControllerLeaseContext,
    stopped_result: dict[str, object] | None = None,
) -> dict[str, object]:
    """Keep the failed batch stopped, but remember the pull requests the probe held out.

    The failed candidate keeps its status and retry budget. Its new held-out
    entries keep later passes from probing the same conflict again, until the
    hold's head or probed base/preceding lineage changes.
    """
    failed = active_candidate_record.candidate
    record_id = active_candidate_record.record_id
    superseded_record_id: str | None = None
    if mutate and (probe.held_out != failed.held_out or active_candidate_record.status != "active"):
        lease.checkpoint(
            active_action="reflow_candidate",
            active_phase="persist_held_out_entries",
            active_record_id=record_id,
            active_pull_request_number=None,
            step_payload={"candidate_record_id": record_id},
        )
        failed = failed.model_copy(update={"held_out": probe.held_out})
        updated_record = build_merge_train_batch_candidate_record(
            ordinary_job_binding=active_candidate_record.ordinary_job_binding,
            candidate=failed,
            source=f"service:controller:candidate-held-out:{trace_id}",
            updated_at=recorded_at,
        )
        candidate_store.write_merge_train_batch_candidate_record(updated_record)
        record_id = updated_record.record_id
        _supersede_active_merge_train_batch_candidate_records(
            record_store=candidate_store,
            repository=repository,
            base_branch=base_branch,
            batch_id=failed.batch_id,
            replacement_record_id=record_id,
        )
        if active_candidate_record.status == "active":
            superseded_record_id = active_candidate_record.record_id
    result: dict[str, object] = {
        "repository": repository,
        "base_branch": base_branch,
        "mode": "dry-run",
        "controller_action": "candidate_failed",
        "reason_code": "unchanged_batch_after_conflict_probe",
        **(stopped_result or {}),
        "merge_train_batch_candidate_record_id": record_id,
        "candidate": failed.model_dump(mode="json"),
    }
    if superseded_record_id is not None:
        result["superseded_merge_train_batch_candidate_record_id"] = superseded_record_id
    else:
        result.pop("superseded_merge_train_batch_candidate_record_id", None)
    details = result.get("details")
    result["details"] = {
        **(details if isinstance(details, dict) else {}),
        "recovery_source_record_id": active_candidate_record.record_id,
    }
    if probe.report is not None:
        result["conflict_probe"] = probe.report
    return result


def _entries_ahead_of(
    *, candidate: MergeTrainBatchCandidate, pull_request_number: int
) -> tuple[int, ...]:
    ahead: list[int] = []
    for entry in candidate.entries:
        if entry.pull_request_number == pull_request_number:
            break
        ahead.append(entry.pull_request_number)
    return tuple(ahead)


def _held_out_diagnostic(entry: MergeTrainBatchHeldOutEntry) -> dict[str, object]:
    return entry.model_dump(
        mode="json", include={"pull_request_number", "head_sha", "reason", "conflicts_with"}
    )


def _expose_persisted_conflict_holds(result: dict[str, object]) -> None:
    candidate = result.get("candidate")
    if not isinstance(candidate, dict) or not candidate.get("held_out"):
        return
    holds = tuple(
        MergeTrainBatchHeldOutEntry.model_validate(entry) for entry in candidate["held_out"]
    )
    probe = result.get("conflict_probe")
    if isinstance(probe, dict):
        probe["held_out"] = [_held_out_diagnostic(entry) for entry in holds]
    else:
        result["conflict_probe"] = {
            "status": "persisted",
            "pull_request_numbers": [],
            "held_out": [_held_out_diagnostic(entry) for entry in holds],
        }


def _surviving_held_out_entries(
    *,
    policy: MergeTrainPolicy,
    snapshot: MergeTrainDryRunSnapshot,
    held_out: tuple[MergeTrainBatchHeldOutEntry, ...],
    batch_landing: bool = False,
    isolating_entries: tuple[MergeTrainBatchEntry, ...] = (),
) -> tuple[MergeTrainBatchHeldOutEntry, ...]:
    """Return the holds that still apply to the current queue.

    A conflict hold lasts while its base, head and accepted preceding heads are
    unchanged. A failure-isolation hold also needs its base and head, and lasts
    only while every member of the candidate retesting without it
    (``isolating_entries``) is still queued at the same head; planning without
    that candidate releases it. A dependency-not-ready hold lasts while its base
    and head are unchanged, whether or not the head has settled yet, so a head
    that settles mid-candidate waits for the next base instead of replacing the
    candidate.
    """
    if not held_out:
        return ()
    queue = build_merge_train_dry_run_result(
        policy=policy, snapshot=snapshot, batch_landing=batch_landing
    ).queue
    eligible_heads = {(entry.number, entry.head_sha) for entry in queue if entry.eligible}
    isolation_active = bool(isolating_entries) and all(
        (entry.pull_request_number, entry.head_sha) in eligible_heads for entry in isolating_entries
    )
    holds = {entry.pull_request_number: entry for entry in held_out}
    preceding: list[tuple[int, str]] = []
    surviving: list[MergeTrainBatchHeldOutEntry] = []
    for entry in queue:
        hold = holds.get(entry.number)
        unchanged = (
            hold is not None
            and hold.head_sha == entry.head_sha
            and hold.probe_base_sha == snapshot.base_sha
        )
        if not entry.eligible:
            # A dependency still waiting on its own head stays held, so later
            # rewrites of the hold list cannot drop it before it settles.
            if unchanged and hold is not None and hold.reason == "dependency_not_ready":
                surviving.append(hold)
            continue
        if (
            unchanged
            and hold is not None
            and (
                isolation_active
                if hold.reason == "failure_isolation"
                else hold.reason == "dependency_not_ready"
                or tuple(zip(hold.conflicts_with, hold.conflicts_with_head_shas))
                == tuple(preceding)
            )
        ):
            surviving.append(hold)
        else:
            preceding.append((entry.number, entry.head_sha))
    return tuple(surviving)


def _without_held_out_pull_requests(
    *,
    snapshot: MergeTrainDryRunSnapshot,
    held_out: tuple[MergeTrainBatchHeldOutEntry, ...],
) -> MergeTrainDryRunSnapshot:
    """Remove the already-applicable holds from a planning snapshot."""
    held = {(entry.pull_request_number, entry.head_sha) for entry in held_out}
    if not held:
        return snapshot
    return snapshot.model_copy(
        update={
            "pull_requests": tuple(
                pull_request
                for pull_request in snapshot.pull_requests
                if (pull_request.number, pull_request.head_sha) not in held
            )
        }
    )


def _merge_train_candidate_matches_dry_run_queue(
    *, candidate: MergeTrainBatchCandidate, dry_run_result: MergeTrainDryRunResult, base_sha: str
) -> bool:
    # Candidates store the repository lowercased; dry-run results keep the policy's casing.
    if candidate.repository.casefold() != dry_run_result.repository.casefold():
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
        "retired_merge_train_batch_candidate_record_id",
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
        result_detail = result.get("reconciliation_detail")
        if isinstance(result_detail, str) and result_detail.strip():
            return result_detail.strip()
        return current_detail
    if result.get("candidate_ref_cleanup_status") == "failed":
        return "retryable:candidate_ref_cleanup_failed"
    return "operator_required:controller_step_blocked"


def _controller_exception_reconciliation_detail(error: Exception) -> str:
    description = ""
    if isinstance(error, MergeTrainGitHubError):
        description = error.request_description
        if isinstance(error.__cause__, MergeTrainGitHubError) and not description:
            description = error.__cause__.request_description
    suffix = f"; request:{description}" if description else ""
    if isinstance(error, MergeTrainGitHubMergeRejectedError):
        return {
            "head_behind_base": "operator_required:pull_request_head_behind_base",
            "merge_blocked": "operator_required:pull_request_merge_blocked",
        }.get(error.refusal_diagnosis, "operator_required:github_merge_rejected") + suffix
    if isinstance(error, MergeTrainGitHubError):
        quota_error = error
        if isinstance(error.__cause__, MergeTrainGitHubError) and error.__cause__.rate_limited:
            quota_error = error.__cause__
        if quota_error.rate_limited:
            reset = (
                f"; reset_at:{quota_error.rate_limit_reset}"
                if quota_error.rate_limit_reset is not None
                else ""
            )
            retry_after = (
                f"; retry_after_seconds:{quota_error.retry_after_seconds}"
                if quota_error.retry_after_seconds is not None
                else ""
            )
            primary_exhausted = (
                f"; primary_exhausted:{str(quota_error.primary_quota_exhausted).lower()}"
                if quota_error.primary_quota_exhausted is not None
                else ""
            )
            return (
                "retryable:github_rate_limited" + suffix + reset + retry_after + primary_exhausted
            )
        if error.status_code is None or error.status_code >= 500:
            return "retryable:github_request_failed" + suffix
        return "operator_required:github_request_rejected" + suffix
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
