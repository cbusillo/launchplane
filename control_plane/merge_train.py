from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_train_policy import MergeTrainMergeMethod
from control_plane.contracts.merge_train_policy import MergeTrainPolicy
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy


MergeTrainCheckStatus = Literal["pass", "fail", "pending", "unknown"]
MergeTrainDryRunAction = Literal[
    "idle", "block", "merge", "update_branch", "wait_for_checks"
]
MergeTrainMergeableState = Literal["mergeable", "conflicting", "unknown"]
MergeTrainPullRequestState = Literal["open", "closed", "merged"]


class MergeTrainLabelClient(Protocol):
    def add_pull_request_label(
        self, *, repository: str, pull_request_number: int, label: str
    ) -> None: ...


class MergeTrainBranchClient(Protocol):
    def update_pull_request_branch(
        self, *, repository: str, pull_request_number: int, expected_head_sha: str
    ) -> None: ...


class MergeTrainMergeClient(Protocol):
    def merge_pull_request(
        self,
        *,
        repository: str,
        pull_request_number: int,
        head_sha: str,
        merge_method: MergeTrainMergeMethod,
    ) -> str: ...


class MergeTrainSnapshotReader(Protocol):
    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> "MergeTrainDryRunSnapshot": ...


class MergeTrainPullRequestSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = Field(gt=0)
    url: str = ""
    title: str = ""
    state: MergeTrainPullRequestState = "open"
    is_draft: bool = False
    created_at: str
    labels: tuple[str, ...] = ()
    actor_role: str = "unknown"
    head_sha: str
    base_sha: str = ""
    mergeable: MergeTrainMergeableState = "unknown"
    required_checks_status: MergeTrainCheckStatus = "unknown"
    branch_update_required: bool = False

    @model_validator(mode="after")
    def _validate_snapshot(self) -> "MergeTrainPullRequestSnapshot":
        self.url = self.url.strip()
        self.title = self.title.strip()
        self.created_at = _normalize_required_value(
            self.created_at, "merge train pull request snapshot requires created_at"
        )
        self.labels = _normalize_unique_values(self.labels)
        self.actor_role = _normalize_required_value(
            self.actor_role, "merge train pull request snapshot requires actor_role"
        )
        self.head_sha = _normalize_required_value(
            self.head_sha, "merge train pull request snapshot requires head_sha"
        )
        self.base_sha = self.base_sha.strip()
        return self


class MergeTrainDryRunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    base_branch: str
    pull_requests: tuple[MergeTrainPullRequestSnapshot, ...]

    @model_validator(mode="after")
    def _validate_snapshot(self) -> "MergeTrainDryRunSnapshot":
        self.repository = _normalize_required_value(
            self.repository, "merge train dry-run snapshot requires repository"
        )
        self.base_branch = _normalize_required_value(
            self.base_branch, "merge train dry-run snapshot requires base_branch"
        )
        return self


class MergeTrainQueueEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    url: str = ""
    title: str = ""
    created_at: str
    head_sha: str
    labels: tuple[str, ...]
    actor_role: str
    mergeable: MergeTrainMergeableState
    required_checks_status: MergeTrainCheckStatus
    branch_update_required: bool
    eligible: bool
    ineligible_reasons: tuple[str, ...] = ()


class MergeTrainDryRunResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry-run"] = "dry-run"
    repository: str
    base_branch: str
    policy_key: str
    merge_method: MergeTrainMergeMethod
    failure_policy: str
    enqueue_label: str
    blocked_label: str
    queue_order: tuple[int, ...]
    queue: tuple[MergeTrainQueueEntry, ...]
    selected_pr: MergeTrainQueueEntry | None = None
    intended_next_action: MergeTrainDryRunAction
    next_action_detail: str


class MergeTrainBlockResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["blocked", "skipped"]
    repository: str
    base_branch: str
    pull_request_number: int | None = None
    blocked_label: str
    train_should_continue: bool
    detail: str


class MergeTrainBranchUpdateResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["updated", "skipped"]
    repository: str
    base_branch: str
    pull_request_number: int | None = None
    expected_head_sha: str = ""
    reread_required: bool
    detail: str


class MergeTrainRereadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["reread", "skipped"]
    repository: str
    base_branch: str
    refreshed_result: MergeTrainDryRunResult | None = None
    detail: str


class MergeTrainWaitResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["waiting", "skipped"]
    repository: str
    base_branch: str
    pull_request_number: int | None = None
    head_sha: str = ""
    mergeable: MergeTrainMergeableState | None = None
    required_checks_status: MergeTrainCheckStatus | None = None
    poll_required: bool
    detail: str


class MergeTrainMergeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["merged", "skipped"]
    repository: str
    base_branch: str
    pull_request_number: int | None = None
    head_sha: str = ""
    merge_method: MergeTrainMergeMethod | None = None
    merge_commit_sha: str = ""
    reread_required: bool
    detail: str


def build_merge_train_dry_run_result(
    *, policy: MergeTrainPolicy, snapshot: MergeTrainDryRunSnapshot
) -> MergeTrainDryRunResult:
    repository_policy = policy.find_repository_policy(
        repository=snapshot.repository, base_branch=snapshot.base_branch
    )
    queue = tuple(
        _build_queue_entry(repository_policy, pull_request)
        for pull_request in sorted(
            snapshot.pull_requests, key=lambda item: (item.created_at, item.number)
        )
    )
    selected_pr = next((entry for entry in queue if entry.eligible), None)
    intended_next_action, next_action_detail = _next_action_for_selected_pr(
        repository_policy, selected_pr
    )
    return MergeTrainDryRunResult(
        repository=snapshot.repository,
        base_branch=snapshot.base_branch,
        policy_key=repository_policy.policy_key,
        merge_method=repository_policy.merge_method,
        failure_policy=repository_policy.failure_policy,
        enqueue_label=repository_policy.enqueue_label,
        blocked_label=repository_policy.blocked_label,
        queue_order=tuple(entry.number for entry in queue if entry.eligible),
        queue=queue,
        selected_pr=selected_pr,
        intended_next_action=intended_next_action,
        next_action_detail=next_action_detail,
    )


def apply_merge_train_block_intent(
    *,
    dry_run_result: MergeTrainDryRunResult,
    label_client: MergeTrainLabelClient,
) -> MergeTrainBlockResult:
    if dry_run_result.intended_next_action != "block" or dry_run_result.selected_pr is None:
        return MergeTrainBlockResult(
            status="skipped",
            repository=dry_run_result.repository,
            base_branch=dry_run_result.base_branch,
            blocked_label=dry_run_result.blocked_label,
            train_should_continue=True,
            detail="Dry-run result does not require a block label.",
        )
    train_should_continue = dry_run_result.failure_policy == "continue_after_blocking_pr"
    if dry_run_result.blocked_label in dry_run_result.selected_pr.labels:
        return MergeTrainBlockResult(
            status="blocked",
            repository=dry_run_result.repository,
            base_branch=dry_run_result.base_branch,
            pull_request_number=dry_run_result.selected_pr.number,
            blocked_label=dry_run_result.blocked_label,
            train_should_continue=train_should_continue,
            detail=(
                f"Pull request #{dry_run_result.selected_pr.number} already has "
                f"{dry_run_result.blocked_label}."
            ),
        )
    label_client.add_pull_request_label(
        repository=dry_run_result.repository,
        pull_request_number=dry_run_result.selected_pr.number,
        label=dry_run_result.blocked_label,
    )
    return MergeTrainBlockResult(
        status="blocked",
        repository=dry_run_result.repository,
        base_branch=dry_run_result.base_branch,
        pull_request_number=dry_run_result.selected_pr.number,
        blocked_label=dry_run_result.blocked_label,
        train_should_continue=train_should_continue,
        detail=(
            f"Applied {dry_run_result.blocked_label} to pull request "
            f"#{dry_run_result.selected_pr.number}."
        ),
    )


def apply_merge_train_branch_update_intent(
    *,
    dry_run_result: MergeTrainDryRunResult,
    branch_client: MergeTrainBranchClient,
) -> MergeTrainBranchUpdateResult:
    if (
        dry_run_result.intended_next_action != "update_branch"
        or dry_run_result.selected_pr is None
    ):
        return MergeTrainBranchUpdateResult(
            status="skipped",
            repository=dry_run_result.repository,
            base_branch=dry_run_result.base_branch,
            reread_required=False,
            detail="Dry-run result does not require a branch update.",
        )
    branch_client.update_pull_request_branch(
        repository=dry_run_result.repository,
        pull_request_number=dry_run_result.selected_pr.number,
        expected_head_sha=dry_run_result.selected_pr.head_sha,
    )
    return MergeTrainBranchUpdateResult(
        status="updated",
        repository=dry_run_result.repository,
        base_branch=dry_run_result.base_branch,
        pull_request_number=dry_run_result.selected_pr.number,
        expected_head_sha=dry_run_result.selected_pr.head_sha,
        reread_required=True,
        detail=(
            "Updated pull request branch; re-read mergeability and required checks "
            "before continuing."
        ),
    )


def reread_merge_train_after_branch_update(
    *,
    branch_update_result: MergeTrainBranchUpdateResult,
    policy: MergeTrainPolicy,
    snapshot_reader: MergeTrainSnapshotReader,
) -> MergeTrainRereadResult:
    if not branch_update_result.reread_required:
        return MergeTrainRereadResult(
            status="skipped",
            repository=branch_update_result.repository,
            base_branch=branch_update_result.base_branch,
            detail="Branch update result does not require a reread.",
        )
    snapshot = snapshot_reader.read_merge_train_snapshot(
        repository=branch_update_result.repository,
        base_branch=branch_update_result.base_branch,
    )
    refreshed_result = build_merge_train_dry_run_result(policy=policy, snapshot=snapshot)
    return MergeTrainRereadResult(
        status="reread",
        repository=branch_update_result.repository,
        base_branch=branch_update_result.base_branch,
        refreshed_result=refreshed_result,
        detail="Re-read mergeability and required checks after branch update.",
    )


def build_merge_train_wait_result(
    *, dry_run_result: MergeTrainDryRunResult
) -> MergeTrainWaitResult:
    if (
        dry_run_result.intended_next_action != "wait_for_checks"
        or dry_run_result.selected_pr is None
    ):
        return MergeTrainWaitResult(
            status="skipped",
            repository=dry_run_result.repository,
            base_branch=dry_run_result.base_branch,
            poll_required=False,
            detail="Dry-run result does not require check polling.",
        )
    return MergeTrainWaitResult(
        status="waiting",
        repository=dry_run_result.repository,
        base_branch=dry_run_result.base_branch,
        pull_request_number=dry_run_result.selected_pr.number,
        head_sha=dry_run_result.selected_pr.head_sha,
        mergeable=dry_run_result.selected_pr.mergeable,
        required_checks_status=dry_run_result.selected_pr.required_checks_status,
        poll_required=True,
        detail=dry_run_result.next_action_detail,
    )


def apply_merge_train_merge_intent(
    *,
    dry_run_result: MergeTrainDryRunResult,
    merge_client: MergeTrainMergeClient,
) -> MergeTrainMergeResult:
    if dry_run_result.intended_next_action != "merge" or dry_run_result.selected_pr is None:
        return MergeTrainMergeResult(
            status="skipped",
            repository=dry_run_result.repository,
            base_branch=dry_run_result.base_branch,
            reread_required=False,
            detail="Dry-run result does not allow a merge.",
        )
    merge_commit_sha = merge_client.merge_pull_request(
        repository=dry_run_result.repository,
        pull_request_number=dry_run_result.selected_pr.number,
        head_sha=dry_run_result.selected_pr.head_sha,
        merge_method=dry_run_result.merge_method,
    )
    return MergeTrainMergeResult(
        status="merged",
        repository=dry_run_result.repository,
        base_branch=dry_run_result.base_branch,
        pull_request_number=dry_run_result.selected_pr.number,
        head_sha=dry_run_result.selected_pr.head_sha,
        merge_method=dry_run_result.merge_method,
        merge_commit_sha=merge_commit_sha,
        reread_required=True,
        detail=(
            f"Merged pull request #{dry_run_result.selected_pr.number}; "
            "re-read the merge train before selecting the next queued entry."
        ),
    )


def _build_queue_entry(
    repository_policy: MergeTrainRepositoryPolicy,
    pull_request: MergeTrainPullRequestSnapshot,
) -> MergeTrainQueueEntry:
    ineligible_reasons: list[str] = []
    if pull_request.state != "open":
        ineligible_reasons.append("pull request is not open")
    if pull_request.is_draft:
        ineligible_reasons.append("draft pull request")
    if (
        repository_policy.enqueue.label_required
        and repository_policy.enqueue_label not in pull_request.labels
    ):
        ineligible_reasons.append(f"missing {repository_policy.enqueue_label} label")
    if pull_request.actor_role not in repository_policy.enqueue.allowed_actor_roles:
        ineligible_reasons.append("actor role is not allowed to enqueue")
    return MergeTrainQueueEntry(
        number=pull_request.number,
        url=pull_request.url,
        title=pull_request.title,
        created_at=pull_request.created_at,
        head_sha=pull_request.head_sha,
        labels=pull_request.labels,
        actor_role=pull_request.actor_role,
        mergeable=pull_request.mergeable,
        required_checks_status=pull_request.required_checks_status,
        branch_update_required=pull_request.branch_update_required,
        eligible=not ineligible_reasons,
        ineligible_reasons=tuple(ineligible_reasons),
    )


def _next_action_for_selected_pr(
    repository_policy: MergeTrainRepositoryPolicy,
    selected_pr: MergeTrainQueueEntry | None,
) -> tuple[MergeTrainDryRunAction, str]:
    if selected_pr is None:
        return "idle", "No eligible pull requests are queued."
    if selected_pr.mergeable == "conflicting":
        return (
            "block",
            f"Add {repository_policy.blocked_label}; pull request has merge conflicts.",
        )
    if selected_pr.required_checks_status == "fail":
        return (
            "block",
            f"Add {repository_policy.blocked_label}; required checks failed.",
        )
    if selected_pr.branch_update_required:
        return "update_branch", "Refresh the pull request against the current base branch."
    if selected_pr.mergeable == "unknown" or selected_pr.required_checks_status in {
        "pending",
        "unknown",
    }:
        return "wait_for_checks", "Wait for mergeability and required checks on the head SHA."
    return (
        "merge",
        f"Merge with {repository_policy.merge_method} after confirming current head checks.",
    )


def _normalize_required_value(value: str, message: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(message)
    return normalized


def _normalize_unique_values(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized: list[str] = []
    for raw_value in values:
        value = raw_value.strip()
        if not value:
            raise ValueError("merge train labels must be non-empty")
        if value not in normalized:
            normalized.append(value)
    return tuple(normalized)
