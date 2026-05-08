from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_train_policy import MergeTrainPolicy
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy


MergeTrainCheckStatus = Literal["pass", "fail", "pending", "unknown"]
MergeTrainDryRunAction = Literal[
    "idle", "block", "merge", "update_branch", "wait_for_checks"
]
MergeTrainMergeableState = Literal["mergeable", "conflicting", "unknown"]
MergeTrainPullRequestState = Literal["open", "closed", "merged"]


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
    merge_method: str
    failure_policy: str
    enqueue_label: str
    blocked_label: str
    queue_order: tuple[int, ...]
    queue: tuple[MergeTrainQueueEntry, ...]
    selected_pr: MergeTrainQueueEntry | None = None
    intended_next_action: MergeTrainDryRunAction
    next_action_detail: str


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
