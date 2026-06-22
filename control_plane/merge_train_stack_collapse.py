from dataclasses import dataclass
from typing import Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_train_batch import (
    build_merge_train_batch_candidate,
    build_merge_train_batch_candidate_record,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicy
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlan,
    MergeTrainStackCollapsePlanRecord,
    build_merge_train_stack_collapse_plan_record,
    execute_merge_train_stack_collapse_plan,
)
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train_batch_candidate import MergeTrainBatchCandidateRecordStore
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    GitHubMergeTrainSnapshotReader,
    MergeTrainGitHubTransport,
    UrllibMergeTrainGitHubTransport,
)


class MergeTrainStackCollapseRunOnceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str = "main"
    mode: Literal["execute", "admit"] = "execute"
    stack_collapse_plan_record_id: str
    github_api_base_url: str = "https://api.github.com"

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainStackCollapseRunOnceEnvelope":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        self.stack_collapse_plan_record_id = self.stack_collapse_plan_record_id.strip()
        self.github_api_base_url = self.github_api_base_url.strip() or "https://api.github.com"
        if not self.repository:
            raise ValueError("merge train stack collapse requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train stack collapse requires base_branch")
        if not self.stack_collapse_plan_record_id:
            raise ValueError("merge train stack collapse requires stack_collapse_plan_record_id")
        return self


class MergeTrainStackCollapsePlanRecordNotFoundError(ValueError):
    """Raised when a requested stack-collapse plan record is absent."""


class MergeTrainStackCollapseBatchCandidateStoreMissingError(RuntimeError):
    """Raised when admit mode lacks batch-candidate persistence."""


class MergeTrainStackCollapsePlanRecordStore(Protocol):
    def write_merge_train_stack_collapse_plan_record(
        self, record: MergeTrainStackCollapsePlanRecord
    ) -> object: ...

    def list_merge_train_stack_collapse_plan_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainStackCollapsePlanRecord, ...]: ...


def require_merge_train_stack_collapse_plan_record_store(
    record_store: object,
) -> MergeTrainStackCollapsePlanRecordStore:
    if hasattr(record_store, "write_merge_train_stack_collapse_plan_record") and hasattr(
        record_store, "list_merge_train_stack_collapse_plan_records"
    ):
        return cast(MergeTrainStackCollapsePlanRecordStore, record_store)
    raise TypeError("record store does not support merge train stack collapse plans")


@dataclass(frozen=True)
class MergeTrainStackCollapseRunOnceResult:
    accepted_result: dict[str, object]
    records: dict[str, str]


def execute_merge_train_stack_collapse_run_once(
    *,
    request: MergeTrainStackCollapseRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    token: str,
    trace_id: str,
    recorded_at: str,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    batch_candidate_store: MergeTrainBatchCandidateRecordStore | None = None,
) -> MergeTrainStackCollapseRunOnceResult:
    existing_record = read_merge_train_stack_collapse_plan_record(
        record_store=stack_collapse_store,
        repository=request.repository,
        base_branch=request.base_branch,
        record_id=request.stack_collapse_plan_record_id,
    )
    transport = UrllibMergeTrainGitHubTransport(
        token=token,
        api_base_url=request.github_api_base_url,
    )
    if request.mode == "execute":
        executed_plan = execute_merge_train_stack_collapse_plan(
            plan=existing_record.plan,
            branch_client=GitHubMergeTrainClient(transport=transport),
            updated_at=recorded_at,
        )
        collapse_record = build_merge_train_stack_collapse_plan_record(
            plan=executed_plan,
            source=f"service:execute:{trace_id}",
            updated_at=recorded_at,
        )
        stack_collapse_store.write_merge_train_stack_collapse_plan_record(collapse_record)
        return MergeTrainStackCollapseRunOnceResult(
            accepted_result={
                "mode": request.mode,
                "stack_collapse_plan": executed_plan.model_dump(mode="json"),
            },
            records={"merge_train_stack_collapse_plan_record_id": collapse_record.record_id},
        )
    return _execute_admit_mode(
        request=request,
        policy=policy,
        policy_sha256=policy_sha256,
        trace_id=trace_id,
        recorded_at=recorded_at,
        existing_plan=existing_record.plan,
        transport=transport,
        batch_candidate_store=batch_candidate_store,
    )


def read_merge_train_stack_collapse_plan_record(
    *,
    record_store: MergeTrainStackCollapsePlanRecordStore,
    repository: str,
    base_branch: str,
    record_id: str,
) -> MergeTrainStackCollapsePlanRecord:
    records = record_store.list_merge_train_stack_collapse_plan_records(
        repository=repository, base_branch=base_branch
    )
    for record in records:
        if record.record_id == record_id:
            return record
    raise MergeTrainStackCollapsePlanRecordNotFoundError(
        "merge train stack collapse plan record not found"
    )


def stack_collapse_expected_root_head_sha(plan: MergeTrainStackCollapsePlan) -> str:
    for mutation in plan.mutations:
        if mutation.parent_pull_request_number == plan.root_pull_request_number:
            return mutation.merge_commit_sha or plan.root_initial_head_sha
    return plan.root_initial_head_sha


def _execute_admit_mode(
    *,
    request: MergeTrainStackCollapseRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    trace_id: str,
    recorded_at: str,
    existing_plan: MergeTrainStackCollapsePlan,
    transport: MergeTrainGitHubTransport,
    batch_candidate_store: MergeTrainBatchCandidateRecordStore | None,
) -> MergeTrainStackCollapseRunOnceResult:
    if batch_candidate_store is None:
        raise MergeTrainStackCollapseBatchCandidateStoreMissingError(
            "record store does not support merge train batch candidate records"
        )
    if existing_plan.status != "waiting_for_root_checks":
        raise ValueError("merge train stack collapse plan is not ready for train admission")
    if existing_plan.policy_sha256 != policy_sha256:
        raise ValueError("merge train stack collapse policy digest no longer matches")
    snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
        repository=request.repository,
        base_branch=request.base_branch,
    )
    root_pull_request = next(
        (
            pull_request
            for pull_request in snapshot.pull_requests
            if pull_request.number == existing_plan.root_pull_request_number
        ),
        None,
    )
    if root_pull_request is None:
        raise ValueError("merge train stack collapse root PR is missing")
    if root_pull_request.head_sha != stack_collapse_expected_root_head_sha(existing_plan):
        raise ValueError("merge train stack collapse root PR head no longer matches")
    snapshot = snapshot.model_copy(update={"pull_requests": (root_pull_request,)})
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    candidate = build_merge_train_batch_candidate(
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
        policy_sha256=policy_sha256,
        created_at=recorded_at,
    )
    candidate_record = build_merge_train_batch_candidate_record(
        candidate=candidate,
        source=f"service:stack-collapse-admit:{trace_id}",
        updated_at=recorded_at,
    )
    batch_candidate_store.write_merge_train_batch_candidate_record(candidate_record)
    return MergeTrainStackCollapseRunOnceResult(
        accepted_result={
            "mode": request.mode,
            "dry_run_result": dry_run_result.model_dump(mode="json"),
            "candidate": candidate.model_dump(mode="json"),
        },
        records={"merge_train_batch_candidate_record_id": candidate_record.record_id},
    )
