from dataclasses import dataclass
import logging
from typing import Callable, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchLandingPlan,
    MergeTrainBatchLandingPlanRecord,
    build_merge_train_batch_landing_plan,
    build_merge_train_batch_landing_plan_record,
)
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
    build_merge_train_stack_collapse_plan_record,
    reconcile_merge_train_stack_children_after_root_landing,
)
from control_plane.merge_train_batch_candidate import (
    MergeTrainBatchCandidateRecordStore,
    read_merge_train_batch_candidate_record,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    MergeTrainGitHubError,
    MergeTrainGitHubStaleHeadError,
    UrllibMergeTrainGitHubTransport,
)
from control_plane.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecordStore,
    read_merge_train_stack_collapse_plan_record,
    stack_collapse_expected_root_head_sha,
)


_LOGGER = logging.getLogger(__name__)


class MergeTrainBatchLandingRunOnceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str = "main"
    mode: Literal["plan", "land"] = "plan"
    candidate_record_id: str = ""
    landing_plan_record_id: str = ""
    stack_collapse_plan_record_id: str = ""
    github_api_base_url: str = "https://api.github.com"

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainBatchLandingRunOnceEnvelope":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        self.candidate_record_id = self.candidate_record_id.strip()
        self.landing_plan_record_id = self.landing_plan_record_id.strip()
        self.stack_collapse_plan_record_id = self.stack_collapse_plan_record_id.strip()
        self.github_api_base_url = self.github_api_base_url.strip() or "https://api.github.com"
        if not self.repository:
            raise ValueError("merge train batch landing requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train batch landing requires base_branch")
        if self.mode == "plan" and not self.candidate_record_id:
            raise ValueError("landing plan mode requires candidate_record_id")
        if self.mode == "land" and not self.landing_plan_record_id:
            raise ValueError("landing land mode requires landing_plan_record_id")
        return self


class MergeTrainBatchLandingPlanRecordNotFoundError(ValueError):
    """Raised when a requested batch landing plan record is absent."""


class MergeTrainBatchLandingPlanRecordStore(Protocol):
    def write_merge_train_batch_landing_plan_record(
        self, record: MergeTrainBatchLandingPlanRecord
    ) -> object: ...

    def list_merge_train_batch_landing_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchLandingPlanRecord, ...]: ...


def require_merge_train_batch_landing_plan_record_store(
    record_store: object,
) -> MergeTrainBatchLandingPlanRecordStore:
    if hasattr(record_store, "write_merge_train_batch_landing_plan_record") and hasattr(
        record_store, "list_merge_train_batch_landing_plan_records"
    ):
        return cast(MergeTrainBatchLandingPlanRecordStore, record_store)
    raise TypeError("record store does not support merge train batch landing plan records")


@dataclass(frozen=True)
class MergeTrainBatchLandingRunOnceResult:
    accepted_result: dict[str, object]
    records: dict[str, str]


def execute_merge_train_batch_landing_run_once(
    *,
    request: MergeTrainBatchLandingRunOnceEnvelope,
    repository_policy: MergeTrainRepositoryPolicy,
    policy_sha256: str,
    token: str,
    trace_id: str,
    recorded_at: str,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    mutation_checkpoint: Callable[[str, int | None], None] | None = None,
) -> MergeTrainBatchLandingRunOnceResult:
    if request.mode == "plan":
        return _execute_plan_mode(
            request=request,
            repository_policy=repository_policy,
            trace_id=trace_id,
            recorded_at=recorded_at,
            candidate_store=candidate_store,
            landing_store=landing_store,
        )
    return _execute_land_mode(
        request=request,
        repository_policy=repository_policy,
        policy_sha256=policy_sha256,
        token=token,
        trace_id=trace_id,
        recorded_at=recorded_at,
        landing_store=landing_store,
        stack_collapse_store=stack_collapse_store,
        mutation_checkpoint=mutation_checkpoint,
    )


def read_merge_train_batch_landing_plan_record(
    *,
    record_store: MergeTrainBatchLandingPlanRecordStore,
    repository: str,
    base_branch: str,
    record_id: str,
) -> MergeTrainBatchLandingPlanRecord:
    records = record_store.list_merge_train_batch_landing_plan_records(
        repository=repository, base_branch=base_branch
    )
    for record in records:
        if record.record_id == record_id:
            return record
    raise MergeTrainBatchLandingPlanRecordNotFoundError(
        "merge train batch landing plan record not found"
    )


def validate_stack_collapse_record_for_landing(
    *,
    collapse_record: MergeTrainStackCollapsePlanRecord,
    landing_plan: MergeTrainBatchLandingPlan,
    policy_sha256: str,
) -> None:
    stack_collapse_plan = collapse_record.plan
    if stack_collapse_plan.repository != landing_plan.repository:
        raise ValueError("merge train stack collapse repository does not match landing plan")
    if stack_collapse_plan.base_branch != landing_plan.base_branch:
        raise ValueError("merge train stack collapse base branch does not match landing plan")
    if stack_collapse_plan.policy_key != landing_plan.policy_key:
        raise ValueError("merge train stack collapse policy key does not match landing plan")
    if stack_collapse_plan.policy_sha256 != policy_sha256:
        raise ValueError("merge train stack collapse policy digest no longer matches")
    if stack_collapse_plan.policy_sha256 != landing_plan.policy_sha256:
        raise ValueError("merge train stack collapse policy digest does not match landing plan")
    if stack_collapse_plan.status != "waiting_for_root_checks":
        raise ValueError("merge train stack collapse plan is not ready for landing")
    root_entry = next(
        (
            entry
            for entry in landing_plan.entries
            if entry.pull_request_number == stack_collapse_plan.root_pull_request_number
        ),
        None,
    )
    if root_entry is None:
        raise ValueError("merge train stack collapse root PR is missing from landing plan")
    if root_entry.expected_head_sha != stack_collapse_expected_root_head_sha(stack_collapse_plan):
        raise ValueError("merge train stack collapse root PR head no longer matches")


def _execute_plan_mode(
    *,
    request: MergeTrainBatchLandingRunOnceEnvelope,
    repository_policy: MergeTrainRepositoryPolicy,
    trace_id: str,
    recorded_at: str,
    candidate_store: MergeTrainBatchCandidateRecordStore,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
) -> MergeTrainBatchLandingRunOnceResult:
    candidate_record = read_merge_train_batch_candidate_record(
        record_store=candidate_store,
        repository=request.repository,
        base_branch=request.base_branch,
        record_id=request.candidate_record_id,
    )
    landing_plan = build_merge_train_batch_landing_plan(
        candidate=candidate_record.candidate,
        merge_method=repository_policy.merge_method,
        created_at=recorded_at,
    )
    landing_record = build_merge_train_batch_landing_plan_record(
        landing_plan=landing_plan,
        source=f"service:{request.mode}:{trace_id}",
        updated_at=recorded_at,
    )
    landing_store.write_merge_train_batch_landing_plan_record(landing_record)
    return MergeTrainBatchLandingRunOnceResult(
        accepted_result={
            "mode": request.mode,
            "landing_plan": landing_plan.model_dump(mode="json"),
        },
        records={"merge_train_batch_landing_plan_record_id": landing_record.record_id},
    )


def _execute_land_mode(
    *,
    request: MergeTrainBatchLandingRunOnceEnvelope,
    repository_policy: MergeTrainRepositoryPolicy,
    policy_sha256: str,
    token: str,
    trace_id: str,
    recorded_at: str,
    landing_store: MergeTrainBatchLandingPlanRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    mutation_checkpoint: Callable[[str, int | None], None] | None,
) -> MergeTrainBatchLandingRunOnceResult:
    landing_record = read_merge_train_batch_landing_plan_record(
        record_store=landing_store,
        repository=request.repository,
        base_branch=request.base_branch,
        record_id=request.landing_plan_record_id,
    )
    collapse_existing_record = _read_and_validate_stack_record(
        request=request,
        repository_policy=repository_policy,
        policy_sha256=policy_sha256,
        landing_plan=landing_record.landing_plan,
        stack_collapse_store=stack_collapse_store,
    )
    github_client = GitHubMergeTrainClient(
        transport=UrllibMergeTrainGitHubTransport(
            token=token,
            api_base_url=request.github_api_base_url,
        )
    )
    try:
        landing_plan = github_client.land_batch_candidate(
            landing_plan=landing_record.landing_plan,
            checkpoint=(
                lambda progress_plan, entry, phase: (
                    mutation_checkpoint(
                        phase,
                        entry.pull_request_number,
                    )
                    if mutation_checkpoint is not None
                    else None
                )
            ),
        )
    except MergeTrainGitHubStaleHeadError:
        stale_plan = _stale_merge_train_landing_plan(landing_record.landing_plan)
        stale_record = build_merge_train_batch_landing_plan_record(
            landing_plan=stale_plan,
            source=f"service:{request.mode}:stale-landing:{trace_id}",
            updated_at=recorded_at,
        )
        landing_store.write_merge_train_batch_landing_plan_record(stale_record)
        raise
    landing_record = build_merge_train_batch_landing_plan_record(
        landing_plan=landing_plan,
        source=f"service:{request.mode}:{trace_id}",
        updated_at=recorded_at,
    )
    landing_store.write_merge_train_batch_landing_plan_record(landing_record)
    if mutation_checkpoint is not None:
        mutation_checkpoint("cleanup_candidate_ref", None)
    candidate_ref_cleanup_result = _cleanup_merge_train_batch_candidate_ref(
        github_client=github_client,
        landing_plan=landing_plan,
        request_trace_id=trace_id,
    )
    collapse_record: MergeTrainStackCollapsePlanRecord | None = None
    reconciled_collapse_plan: MergeTrainStackCollapsePlan | None = None
    if collapse_existing_record is not None:
        root_entry = next(
            (
                entry
                for entry in landing_plan.entries
                if entry.pull_request_number
                == collapse_existing_record.plan.root_pull_request_number
            ),
            None,
        )
        if root_entry is None or root_entry.status != "merged":
            raise ValueError("merge train stack child disposition requires merged root PR")
        if mutation_checkpoint is not None:
            mutation_checkpoint(
                "reconcile_stack_children",
                collapse_existing_record.plan.root_pull_request_number,
            )
        reconciled_collapse_plan = reconcile_merge_train_stack_children_after_root_landing(
            plan=collapse_existing_record.plan,
            disposition_client=github_client,
            root_merge_commit_sha=root_entry.merge_commit_sha,
            label=repository_policy.stack_child_disposition_label,
            updated_at=recorded_at,
            checkpoint=(
                lambda progress_plan: (
                    mutation_checkpoint(
                        "reconcile_stack_children",
                        next(
                            (
                                disposition.pull_request_number
                                for disposition in progress_plan.child_dispositions
                                if disposition.status != "closed"
                            ),
                            None,
                        ),
                    )
                    if mutation_checkpoint is not None
                    else None
                )
            ),
        )
        collapse_record = build_merge_train_stack_collapse_plan_record(
            plan=reconciled_collapse_plan,
            source=f"service:child-disposition:{trace_id}",
            updated_at=recorded_at,
        )
        stack_collapse_store.write_merge_train_stack_collapse_plan_record(collapse_record)

    accepted_result: dict[str, object] = {
        "mode": request.mode,
        "landing_plan": landing_plan.model_dump(mode="json"),
        **candidate_ref_cleanup_result,
    }
    records = {"merge_train_batch_landing_plan_record_id": landing_record.record_id}
    if request.stack_collapse_plan_record_id:
        if collapse_record is None or reconciled_collapse_plan is None:
            raise ValueError("merge train stack child disposition record missing")
        records["merge_train_stack_collapse_plan_record_id"] = collapse_record.record_id
        accepted_result["stack_collapse_plan"] = reconciled_collapse_plan.model_dump(mode="json")
    return MergeTrainBatchLandingRunOnceResult(
        accepted_result=accepted_result,
        records=records,
    )


def _stale_merge_train_landing_plan(
    landing_plan: MergeTrainBatchLandingPlan,
) -> MergeTrainBatchLandingPlan:
    return landing_plan.model_copy(
        update={
            "entries": tuple(
                entry.model_copy(update={"status": "stale"})
                if entry.status in {"planned", "merging"}
                else entry
                for entry in landing_plan.entries
            )
        }
    )


def _read_and_validate_stack_record(
    *,
    request: MergeTrainBatchLandingRunOnceEnvelope,
    repository_policy: MergeTrainRepositoryPolicy,
    policy_sha256: str,
    landing_plan: MergeTrainBatchLandingPlan,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
) -> MergeTrainStackCollapsePlanRecord | None:
    if not request.stack_collapse_plan_record_id:
        return None
    collapse_existing_record = read_merge_train_stack_collapse_plan_record(
        record_store=stack_collapse_store,
        repository=request.repository,
        base_branch=request.base_branch,
        record_id=request.stack_collapse_plan_record_id,
    )
    validate_stack_collapse_record_for_landing(
        collapse_record=collapse_existing_record,
        landing_plan=landing_plan,
        policy_sha256=policy_sha256,
    )
    if not repository_policy.stack_child_disposition_label:
        raise ValueError(
            "merge train stack child disposition requires stack_child_disposition_label policy"
        )
    return collapse_existing_record


def _cleanup_merge_train_batch_candidate_ref(
    *,
    github_client: GitHubMergeTrainClient,
    landing_plan: MergeTrainBatchLandingPlan,
    request_trace_id: str,
) -> dict[str, object]:
    try:
        deleted = github_client.cleanup_batch_candidate_ref(landing_plan=landing_plan)
    except MergeTrainGitHubError as error:
        message = str(error).strip() or "GitHub candidate ref cleanup failed."
        _LOGGER.warning(
            "Merge train candidate ref cleanup failed after landing persistence",
            extra={
                "trace_id": request_trace_id,
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
    return {
        "candidate_ref_cleanup_status": "deleted" if deleted else "already_missing",
    }
