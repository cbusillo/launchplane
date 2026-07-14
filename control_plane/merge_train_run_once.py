from dataclasses import dataclass
from typing import Callable, Protocol, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_train_policy import MergeTrainPolicy
from control_plane.contracts.merge_train_run_record import (
    MergeTrainRunRecord,
    build_merge_train_run_record,
)
from control_plane.merge_train import build_merge_train_dry_run_result
from control_plane.merge_train_github import (
    GitHubMergeTrainClient,
    GitHubMergeTrainSnapshotReader,
    UrllibMergeTrainGitHubTransport,
)
from control_plane.workflows.merge_train_worker import (
    MergeTrainWorkerClients,
    run_merge_train_worker_step,
)


class MergeTrainRunOnceEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str = "main"
    mutate: bool = False
    github_api_base_url: str = "https://api.github.com"

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainRunOnceEnvelope":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        self.github_api_base_url = self.github_api_base_url.strip() or "https://api.github.com"
        if not self.repository:
            raise ValueError("merge train run-once requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train run-once requires base_branch")
        return self


class MergeTrainRunRecordStore(Protocol):
    def write_merge_train_run_record(self, record: MergeTrainRunRecord) -> object: ...


def require_merge_train_run_record_store(record_store: object) -> MergeTrainRunRecordStore:
    if hasattr(record_store, "write_merge_train_run_record"):
        return cast(MergeTrainRunRecordStore, record_store)
    raise TypeError("record store does not support merge train run records")


@dataclass(frozen=True)
class MergeTrainRunOnceResult:
    accepted_result: dict[str, object]
    records: dict[str, str]
    run_record: MergeTrainRunRecord


def execute_merge_train_run_once(
    *,
    request: MergeTrainRunOnceEnvelope,
    policy: MergeTrainPolicy,
    policy_sha256: str,
    token: str,
    trace_id: str,
    recorded_at: str,
    mutation_checkpoint: Callable[[], None] | None = None,
) -> MergeTrainRunOnceResult:
    transport = UrllibMergeTrainGitHubTransport(
        token=token,
        api_base_url=request.github_api_base_url,
    )
    snapshot = GitHubMergeTrainSnapshotReader(transport=transport).read_merge_train_snapshot(
        repository=request.repository,
        base_branch=request.base_branch,
    )
    dry_run_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    route_result: dict[str, object] = {
        "repository": request.repository,
        "base_branch": request.base_branch,
        "mode": "mutate" if request.mutate else "dry-run",
        "dry_run_result": dry_run_result.model_dump(mode="json"),
    }
    worker_step_result = None
    if request.mutate:
        if mutation_checkpoint is not None:
            mutation_checkpoint()
        github_client = GitHubMergeTrainClient(transport=transport)
        worker_step_result = run_merge_train_worker_step(
            policy=policy,
            snapshot=snapshot,
            clients=MergeTrainWorkerClients(
                label_client=github_client,
                branch_client=github_client,
                merge_client=github_client,
            ),
        )
        route_result["worker_step_result"] = worker_step_result.model_dump(mode="json")
    run_record = build_merge_train_run_record(
        recorded_at=recorded_at,
        trace_id=trace_id,
        policy_sha256=policy_sha256,
        snapshot=snapshot,
        dry_run_result=dry_run_result,
        worker_step_result=worker_step_result,
    )
    route_result["merge_train_run_id"] = run_record.run_id
    if worker_step_result is not None:
        accepted_result = worker_step_result.model_dump(mode="json")
    else:
        accepted_result = route_result
    return MergeTrainRunOnceResult(
        accepted_result=accepted_result,
        records={"merge_train_run_id": run_record.run_id},
        run_record=run_record,
    )
