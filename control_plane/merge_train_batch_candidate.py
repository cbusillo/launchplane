from dataclasses import dataclass
from typing import Callable, Literal, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_train_batch import (
    MergeTrainBatchCandidate,
    MergeTrainBatchCandidateRecord,
    build_merge_train_batch_candidate,
    build_merge_train_batch_candidate_record,
)
from control_plane.contracts.merge_train_policy import MergeTrainPolicy
from control_plane.contracts.merge_train_stack_collapse import (
    MergeTrainStackCollapsePlanRecord,
    build_merge_train_stack_collapse_plan,
    build_merge_train_stack_collapse_plan_record,
)
from control_plane.merge_train import (
    MergeTrainDryRunResult,
    MergeTrainDryRunSnapshot,
    build_merge_train_dry_run_result,
    discover_merge_train_stack,
)
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    GitHubMergeTrainSnapshotReader,
    MergeTrainGitHubTransport,
    UrllibMergeTrainGitHubTransport,
)


class MergeTrainBatchCandidateRunOnceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str = "main"
    mode: Literal["plan", "build", "observe"] = "plan"
    candidate_record_id: str = ""
    github_api_base_url: str = "https://api.github.com"

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainBatchCandidateRunOnceEnvelope":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        self.candidate_record_id = self.candidate_record_id.strip()
        self.github_api_base_url = self.github_api_base_url.strip() or "https://api.github.com"
        if not self.repository:
            raise ValueError("merge train batch candidate requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train batch candidate requires base_branch")
        if self.mode in {"build", "observe"} and not self.candidate_record_id:
            raise ValueError("build and observe require candidate_record_id")
        return self


class MergeTrainBatchCandidateRecordNotFoundError(ValueError):
    """Raised when a requested batch candidate record is absent."""


class MergeTrainBatchCandidateRecordStore(Protocol):
    def write_merge_train_batch_candidate_record(
        self, record: MergeTrainBatchCandidateRecord
    ) -> object: ...

    def list_merge_train_batch_candidate_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[MergeTrainBatchCandidateRecord, ...]: ...


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


def require_merge_train_batch_candidate_record_store(
    record_store: object,
) -> MergeTrainBatchCandidateRecordStore:
    if hasattr(record_store, "write_merge_train_batch_candidate_record") and hasattr(
        record_store, "list_merge_train_batch_candidate_records"
    ):
        return cast(MergeTrainBatchCandidateRecordStore, record_store)
    raise TypeError("record store does not support merge train batch candidate records")


def require_merge_train_stack_collapse_plan_record_store(
    record_store: object,
) -> MergeTrainStackCollapsePlanRecordStore:
    if hasattr(record_store, "write_merge_train_stack_collapse_plan_record") and hasattr(
        record_store, "list_merge_train_stack_collapse_plan_records"
    ):
        return cast(MergeTrainStackCollapsePlanRecordStore, record_store)
    raise TypeError("record store does not support merge train stack collapse plans")


@dataclass(frozen=True)
class MergeTrainBatchCandidateRunOnceResult:
    accepted_result: dict[str, object]
    records: dict[str, str]


def execute_merge_train_batch_candidate_run_once(
    *,
    request: MergeTrainBatchCandidateRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    token: str,
    trace_id: str,
    recorded_at: str,
    batch_store: MergeTrainBatchCandidateRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
    mutation_checkpoint: Callable[[str, int | None], None] | None = None,
) -> MergeTrainBatchCandidateRunOnceResult:
    transport = UrllibMergeTrainGitHubTransport(
        token=token,
        api_base_url=request.github_api_base_url,
    )
    if request.mode == "plan":
        return _execute_plan_mode(
            request=request,
            policy=policy,
            policy_sha256=policy_sha256,
            transport=transport,
            trace_id=trace_id,
            recorded_at=recorded_at,
            batch_store=batch_store,
            stack_collapse_store=stack_collapse_store,
        )

    existing_record = read_merge_train_batch_candidate_record(
        record_store=batch_store,
        repository=request.repository,
        base_branch=request.base_branch,
        record_id=request.candidate_record_id,
    )
    candidate = existing_record.candidate
    github_client = GitHubMergeTrainClient(transport=transport)
    if request.mode == "build":
        candidate = github_client.build_batch_candidate(
            candidate=candidate,
            checkpoint=(
                lambda progress_candidate, entry, phase: (
                    mutation_checkpoint(
                        phase,
                        entry.pull_request_number if entry is not None else None,
                    )
                    if mutation_checkpoint is not None
                    else None
                )
            ),
        )
    else:
        if mutation_checkpoint is not None:
            mutation_checkpoint("observe_required_checks", None)
        candidate = github_client.observe_batch_candidate_checks(candidate=candidate)
    return _persist_candidate_result(
        candidate=candidate,
        mode=request.mode,
        trace_id=trace_id,
        recorded_at=recorded_at,
        batch_store=batch_store,
    )


def read_merge_train_batch_candidate_record(
    *,
    record_store: MergeTrainBatchCandidateRecordStore,
    repository: str,
    base_branch: str,
    record_id: str,
) -> MergeTrainBatchCandidateRecord:
    records = record_store.list_merge_train_batch_candidate_records(
        repository=repository, base_branch=base_branch
    )
    for record in records:
        if record.record_id == record_id:
            return record
    raise MergeTrainBatchCandidateRecordNotFoundError(
        "merge train batch candidate record not found"
    )


def merge_train_snapshot_has_stack_topology(
    *, snapshot: MergeTrainDryRunSnapshot, dry_run_result: MergeTrainDryRunResult
) -> bool:
    selected_pr = dry_run_result.selected_pr
    if selected_pr is None:
        return False
    for pull_request in snapshot.pull_requests:
        if pull_request.number != selected_pr.number:
            continue
        return all(
            (
                pull_request.head_ref,
                pull_request.head_repository,
                pull_request.base_ref,
                pull_request.base_repository,
            )
        )
    return False


def _execute_plan_mode(
    *,
    request: MergeTrainBatchCandidateRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    transport: MergeTrainGitHubTransport,
    trace_id: str,
    recorded_at: str,
    batch_store: MergeTrainBatchCandidateRecordStore,
    stack_collapse_store: MergeTrainStackCollapsePlanRecordStore,
) -> MergeTrainBatchCandidateRunOnceResult:
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
        stack_collapse_plan = build_merge_train_stack_collapse_plan(
            discovery_result=stack_discovery,
            policy_key=dry_run_result.policy_key,
            policy_sha256=policy_sha256,
            created_at=recorded_at,
        )
        stack_collapse_record = build_merge_train_stack_collapse_plan_record(
            plan=stack_collapse_plan,
            source=f"service:{request.mode}:{trace_id}",
            updated_at=recorded_at,
        )
        stack_collapse_store.write_merge_train_stack_collapse_plan_record(stack_collapse_record)
        return MergeTrainBatchCandidateRunOnceResult(
            accepted_result={
                "mode": request.mode,
                "dry_run_result": dry_run_result.model_dump(mode="json"),
                "stack_discovery": stack_discovery.model_dump(mode="json"),
                "stack_collapse_plan": stack_collapse_plan.model_dump(mode="json"),
            },
            records={"merge_train_stack_collapse_plan_record_id": stack_collapse_record.record_id},
        )

    if stack_discovery is not None and stack_discovery.status == "unsupported":
        return MergeTrainBatchCandidateRunOnceResult(
            accepted_result={
                "mode": request.mode,
                "dry_run_result": dry_run_result.model_dump(mode="json"),
                "stack_discovery": stack_discovery.model_dump(mode="json"),
                "next_action": "stack_unsupported",
            },
            records={},
        )

    candidate = build_merge_train_batch_candidate(
        dry_run_result=dry_run_result,
        base_sha=snapshot.base_sha,
        policy_sha256=policy_sha256,
        created_at=recorded_at,
    )
    return _persist_candidate_result(
        candidate=candidate,
        mode=request.mode,
        trace_id=trace_id,
        recorded_at=recorded_at,
        batch_store=batch_store,
        dry_run_result=dry_run_result,
    )


def _persist_candidate_result(
    *,
    candidate: MergeTrainBatchCandidate,
    mode: str,
    trace_id: str,
    recorded_at: str,
    batch_store: MergeTrainBatchCandidateRecordStore,
    dry_run_result: MergeTrainDryRunResult | None = None,
) -> MergeTrainBatchCandidateRunOnceResult:
    candidate_record = build_merge_train_batch_candidate_record(
        candidate=candidate,
        source=f"service:{mode}:{trace_id}",
        updated_at=recorded_at,
    )
    batch_store.write_merge_train_batch_candidate_record(candidate_record)
    accepted_result: dict[str, object] = {
        "mode": mode,
        "candidate": candidate.model_dump(mode="json"),
    }
    if dry_run_result is not None:
        accepted_result["dry_run_result"] = dry_run_result.model_dump(mode="json")
    return MergeTrainBatchCandidateRunOnceResult(
        accepted_result=accepted_result,
        records={"merge_train_batch_candidate_record_id": candidate_record.record_id},
    )
