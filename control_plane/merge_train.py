from typing import Callable, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field, PositiveInt, model_validator

from control_plane.contracts.merge_train_policy import MergeTrainMergeMethod
from control_plane.contracts.merge_train_policy import MergeTrainPolicy
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy
from control_plane.merge_train_dependency_updates import DependencyUpdateClass


MergeTrainCheckStatus = Literal["pass", "fail", "pending", "unknown"]
MergeTrainDryRunAction = Literal["idle", "block", "merge", "update_branch", "wait_for_checks"]
MergeTrainMergeableState = Literal["mergeable", "conflicting", "unknown"]
MergeTrainPullRequestState = Literal["open", "closed", "merged"]
MergeTrainStackDiscoveryStatus = Literal["ready_for_collapse", "not_stacked", "unsupported"]


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


class MergeTrainPullRequestReader(Protocol):
    def read_pull_request_snapshot(
        self, *, repository: str, pull_request_number: int
    ) -> "MergeTrainPullRequestSnapshot": ...


class MergeTrainSnapshotReader(Protocol):
    def read_merge_train_snapshot(
        self, *, repository: str, base_branch: str
    ) -> "MergeTrainDryRunSnapshot": ...


class MergeTrainLabelActor(BaseModel):
    """Who last applied a label the pull request carries now."""

    model_config = ConfigDict(extra="forbid")

    label: str
    actor_id: PositiveInt | None = None
    actor_login: str = ""
    actor_role: str = "unknown"
    # A GitHub App that applied the label with this actor's user token.
    on_behalf_via_app: str = Field(default="", exclude_if=lambda value: not value)


# GitHub's code-scanning App opens review threads for its findings.
CODE_SCANNING_REVIEW_AUTHOR = "github-advanced-security"


class MergeTrainReviewConversations(BaseModel):
    """Unresolved review threads that can stop GitHub from merging a pull request.

    Recorded only when the base branch requires conversation resolution, or
    when that rule could not be read and the threads might still block.
    """

    model_config = ConfigDict(extra="forbid")

    rule: Literal["required", "unreadable"]
    unresolved_count: PositiveInt
    code_scanning_count: int = Field(default=0, ge=0)


def review_conversations_reason(conversations: MergeTrainReviewConversations) -> str:
    # Thread paths stay out: reasons are public summaries with a length limit.
    count = conversations.unresolved_count
    noun = "conversation" if count == 1 else "conversations"
    rule = (
        "the base branch requires conversation resolution"
        if conversations.rule == "required"
        else "the base branch's conversation-resolution rule could not be read"
    )
    reason = f"{count} unresolved review {noun}; {rule}"
    if conversations.code_scanning_count:
        reason += "; fix the code-scanning finding in the code instead of resolving its thread"
    return reason


class MergeTrainPullRequestSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int = Field(gt=0)
    url: str = ""
    title: str = ""
    state: MergeTrainPullRequestState = "open"
    is_draft: bool = False
    created_at: str
    labels: tuple[str, ...] = ()
    # The enqueue label admits a pull request only when its labeler may enqueue.
    label_actors: tuple[MergeTrainLabelActor, ...] = Field(
        default=(), exclude_if=lambda value: not value
    )
    actor_id: PositiveInt | None = None
    actor_role: str = "unknown"
    head_sha: str
    head_ref: str = ""
    head_repository: str = ""
    base_sha: str = ""
    base_ref: str = ""
    base_repository: str = ""
    mergeable: MergeTrainMergeableState = "unknown"
    requires_individual_landing: bool = Field(default=False, exclude_if=lambda value: not value)
    owner_review_required: bool = Field(default=False, exclude_if=lambda value: not value)
    required_checks_status: MergeTrainCheckStatus = "unknown"
    branch_update_required: bool = False
    # Set only for bot-authored pull requests; see merge_train_dependency_updates.
    dependency_update_class: DependencyUpdateClass | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    review_conversations: MergeTrainReviewConversations | None = Field(
        default=None, exclude_if=lambda value: value is None
    )

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
        self.head_ref = self.head_ref.strip()
        self.head_repository = self.head_repository.strip()
        self.base_sha = self.base_sha.strip()
        self.base_ref = self.base_ref.strip()
        self.base_repository = self.base_repository.strip()
        return self


class MergeTrainDryRunSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    base_branch: str
    base_sha: str = ""
    pull_requests: tuple[MergeTrainPullRequestSnapshot, ...]

    @model_validator(mode="after")
    def _validate_snapshot(self) -> "MergeTrainDryRunSnapshot":
        self.repository = _normalize_required_value(
            self.repository, "merge train dry-run snapshot requires repository"
        )
        self.base_branch = _normalize_required_value(
            self.base_branch, "merge train dry-run snapshot requires base_branch"
        )
        self.base_sha = self.base_sha.strip()
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
    requires_individual_landing: bool = Field(default=False, exclude_if=lambda value: not value)
    owner_review_required: bool = Field(default=False, exclude_if=lambda value: not value)
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


class MergeTrainStackEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    number: int
    head_sha: str
    head_ref: str
    base_sha: str = ""
    base_ref: str


class MergeTrainStackDiscoveryResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: MergeTrainStackDiscoveryStatus
    repository: str
    base_branch: str
    root_pull_request_number: int
    stack_order: tuple[int, ...] = ()
    entries: tuple[MergeTrainStackEntry, ...] = ()
    unsupported_reasons: tuple[str, ...] = ()


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
    *,
    policy: MergeTrainPolicy,
    snapshot: MergeTrainDryRunSnapshot,
    batch_landing: bool = False,
) -> MergeTrainDryRunResult:
    repository_policy = policy.find_repository_policy(
        repository=snapshot.repository, base_branch=snapshot.base_branch
    )
    base_pull_requests = tuple(
        pull_request
        for pull_request in snapshot.pull_requests
        if _targets_merge_train_base(pull_request=pull_request, base_branch=snapshot.base_branch)
    )
    queue = tuple(
        _build_queue_entry(repository_policy, pull_request, skip_blocked=batch_landing)
        for pull_request in sorted(
            base_pull_requests, key=lambda item: (item.created_at, item.number)
        )
    )
    eligible_entries = tuple(entry for entry in queue if entry.eligible)
    if batch_landing:
        batch_entries: list[MergeTrainQueueEntry] = []
        for entry in eligible_entries:
            if entry.requires_individual_landing:
                if not batch_entries:
                    batch_entries.append(entry)
                break
            batch_entries.append(entry)
        eligible_entries = tuple(batch_entries)
    selected_pr = next(iter(eligible_entries), None)
    intended_next_action, next_action_detail = _next_action_for_selected_pr(
        repository_policy,
        selected_pr,
        skip_branch_update=(
            batch_landing
            and repository_policy.merge_method == "merge"
            and len(eligible_entries) > 1
        ),
    )
    if batch_landing and intended_next_action == "merge":
        # Candidate CI does not include constituent Client statuses. Every
        # labelled member must be ready, even when the oldest entry passed.
        waiting_review = next(
            (
                entry
                for entry in eligible_entries
                if entry.owner_review_required and entry.required_checks_status != "pass"
            ),
            None,
        )
        if waiting_review is not None:
            selected_pr = waiting_review
            intended_next_action, next_action_detail = _next_action_for_selected_pr(
                repository_policy, selected_pr, skip_branch_update=True
            )
    return MergeTrainDryRunResult(
        repository=snapshot.repository,
        base_branch=snapshot.base_branch,
        policy_key=repository_policy.policy_key,
        merge_method=repository_policy.merge_method,
        failure_policy=repository_policy.failure_policy,
        enqueue_label=repository_policy.enqueue_label,
        blocked_label=repository_policy.blocked_label,
        queue_order=tuple(entry.number for entry in eligible_entries),
        queue=queue,
        selected_pr=selected_pr,
        intended_next_action=intended_next_action,
        next_action_detail=next_action_detail,
    )


def discover_merge_train_stack(
    *,
    policy: MergeTrainPolicy,
    snapshot: MergeTrainDryRunSnapshot,
    root_pull_request_number: int,
) -> MergeTrainStackDiscoveryResult:
    repository_policy = policy.find_repository_policy(
        repository=snapshot.repository, base_branch=snapshot.base_branch
    )
    pull_requests_by_number = {
        pull_request.number: pull_request for pull_request in snapshot.pull_requests
    }
    root_pull_request = pull_requests_by_number.get(root_pull_request_number)
    if root_pull_request is None:
        return MergeTrainStackDiscoveryResult(
            status="unsupported",
            repository=snapshot.repository,
            base_branch=snapshot.base_branch,
            root_pull_request_number=root_pull_request_number,
            unsupported_reasons=("root pull request was not present in the snapshot",),
        )
    unsupported_reasons = _stack_pull_request_reasons(
        snapshot=snapshot, pull_request=root_pull_request
    )
    if root_pull_request.base_ref != snapshot.base_branch:
        unsupported_reasons = (
            *unsupported_reasons,
            "root pull request does not target base branch",
        )
    if unsupported_reasons:
        return _unsupported_stack_result(
            snapshot=snapshot,
            root_pull_request_number=root_pull_request_number,
            unsupported_reasons=unsupported_reasons,
        )

    children_by_base_ref = _stack_children_by_base_ref(snapshot=snapshot)
    chain = [root_pull_request]
    seen_numbers = {root_pull_request.number}
    current_head_ref = root_pull_request.head_ref
    while current_head_ref:
        children = tuple(
            child
            for child in children_by_base_ref.get(current_head_ref, ())
            if child.number not in seen_numbers
        )
        if not children:
            break
        if len(children) > 1:
            return _unsupported_stack_result(
                snapshot=snapshot,
                root_pull_request_number=root_pull_request_number,
                unsupported_reasons=(
                    f"branch {current_head_ref} has multiple stacked child pull requests",
                ),
            )
        child = children[0]
        child_reasons = _stack_pull_request_reasons(
            snapshot=snapshot, pull_request=child
        ) or merge_train_stack_child_readiness_reasons(
            repository_policy=repository_policy, pull_request=child
        )
        if child_reasons:
            return _unsupported_stack_result(
                snapshot=snapshot,
                root_pull_request_number=root_pull_request_number,
                unsupported_reasons=child_reasons,
            )
        chain.append(child)
        seen_numbers.add(child.number)
        current_head_ref = child.head_ref

    if any(pr.requires_individual_landing for pr in chain):
        # Land the original root first. Its children remain open on their own
        # PRs; after their base dependency lands they can target the train base.
        chain = [root_pull_request]
    if len(chain) == 1:
        return MergeTrainStackDiscoveryResult(
            status="not_stacked",
            repository=snapshot.repository,
            base_branch=snapshot.base_branch,
            root_pull_request_number=root_pull_request_number,
            stack_order=(root_pull_request.number,),
            entries=(_stack_entry(root_pull_request),),
        )
    return MergeTrainStackDiscoveryResult(
        status="ready_for_collapse",
        repository=snapshot.repository,
        base_branch=snapshot.base_branch,
        root_pull_request_number=root_pull_request_number,
        stack_order=tuple(pull_request.number for pull_request in chain),
        entries=tuple(_stack_entry(pull_request) for pull_request in chain),
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
    if any(
        label.casefold() == dry_run_result.blocked_label.casefold()
        for label in dry_run_result.selected_pr.labels
    ):
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
    if dry_run_result.intended_next_action != "update_branch" or dry_run_result.selected_pr is None:
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
    *,
    skip_blocked: bool = False,
) -> MergeTrainQueueEntry:
    ineligible_reasons: list[str] = []
    is_trusted_automation = (
        pull_request.actor_id is not None
        and pull_request.actor_id in repository_policy.enqueue.trusted_automation_github_user_ids
    )
    actor_role = "trusted_automation" if is_trusted_automation else pull_request.actor_role
    if pull_request.state != "open":
        ineligible_reasons.append("pull request is not open")
    if pull_request.is_draft:
        ineligible_reasons.append("draft pull request")
    if skip_blocked and any(
        label.casefold() == repository_policy.blocked_label.casefold()
        for label in pull_request.labels
    ):
        ineligible_reasons.append(f"held by {repository_policy.blocked_label} label")
    is_dependency_update = (
        pull_request.actor_id is not None
        and pull_request.actor_id in repository_policy.enqueue.dependency_update_github_user_ids
    )
    label_refusal = _enqueue_label_refusal(repository_policy, pull_request)
    if (
        skip_blocked
        and pull_request.state == "open"
        and repository_policy.enqueue.label_required
        and is_dependency_update
        and pull_request.dependency_update_class == "patch_or_minor"
        and label_refusal
        and (pull_request.required_checks_status == "fail" or pull_request.mergeable != "mergeable")
    ):
        # A failed automatically admitted head is a current-check hold, not a
        # persistent label. The next snapshot requalifies it without clearing
        # any manual hold or guessing who applied an existing block label.
        ineligible_reasons.append(
            "dependency update current-head merge conflicts"
            if pull_request.mergeable == "conflicting"
            else "dependency update current-head mergeability unknown"
            if pull_request.mergeable == "unknown"
            else "dependency update current-head checks failed"
        )
    if repository_policy.enqueue.label_required and label_refusal:
        if not is_dependency_update:
            ineligible_reasons.append(label_refusal)
        elif pull_request.dependency_update_class != "patch_or_minor":
            ineligible_reasons.append("dependency update needs agent review")
    if (
        not is_trusted_automation
        and actor_role not in repository_policy.enqueue.allowed_actor_roles
    ):
        ineligible_reasons.append("actor role is not allowed to enqueue")
    if pull_request.review_conversations is not None:
        ineligible_reasons.append(review_conversations_reason(pull_request.review_conversations))
    return MergeTrainQueueEntry(
        number=pull_request.number,
        url=pull_request.url,
        title=pull_request.title,
        created_at=pull_request.created_at,
        head_sha=pull_request.head_sha,
        labels=pull_request.labels,
        actor_role=actor_role,
        mergeable=pull_request.mergeable,
        requires_individual_landing=pull_request.requires_individual_landing,
        owner_review_required=pull_request.owner_review_required,
        required_checks_status=pull_request.required_checks_status,
        branch_update_required=pull_request.branch_update_required,
        eligible=not ineligible_reasons,
        ineligible_reasons=tuple(ineligible_reasons),
    )


def _enqueue_label_refusal(
    repository_policy: MergeTrainRepositoryPolicy,
    pull_request: MergeTrainPullRequestSnapshot,
) -> str:
    """Return why the enqueue label does not admit the pull request, or ""."""
    enqueue_label = repository_policy.enqueue_label
    if enqueue_label not in pull_request.labels:
        return f"missing {enqueue_label} label"
    # Pull-request write access includes labels, so the label counts only when
    # the actor who applied it may enqueue, whoever authored the pull request.
    labeler = next(
        (actor for actor in pull_request.label_actors if actor.label == enqueue_label), None
    )
    if labeler is None or labeler.actor_id is None:
        return f"{enqueue_label} label ignored: could not read who applied it"
    if labeler.on_behalf_via_app:
        return (
            f"{enqueue_label} label ignored: applied by the {labeler.on_behalf_via_app} "
            f"GitHub App acting as {labeler.actor_login}, which is not allowed to enqueue"
        )
    if labeler.actor_id in repository_policy.enqueue.trusted_automation_github_user_ids:
        return ""
    if labeler.actor_role in repository_policy.enqueue.allowed_actor_roles:
        return ""
    login = labeler.actor_login or f"user id {labeler.actor_id}"
    return (
        f"{enqueue_label} label ignored: applied by {login} "
        f"({labeler.actor_role}), who is not allowed to enqueue"
    )


def _targets_merge_train_base(
    *, pull_request: MergeTrainPullRequestSnapshot, base_branch: str
) -> bool:
    return not pull_request.base_ref or pull_request.base_ref == base_branch


def _stack_children_by_base_ref(
    *, snapshot: MergeTrainDryRunSnapshot
) -> dict[str, tuple[MergeTrainPullRequestSnapshot, ...]]:
    children_by_base_ref: dict[str, list[MergeTrainPullRequestSnapshot]] = {}
    for pull_request in snapshot.pull_requests:
        if pull_request.base_ref:
            children_by_base_ref.setdefault(pull_request.base_ref, []).append(pull_request)
    return {
        base_ref: tuple(sorted(children, key=lambda item: (item.created_at, item.number)))
        for base_ref, children in children_by_base_ref.items()
    }


def _stack_pull_request_reasons(
    *, snapshot: MergeTrainDryRunSnapshot, pull_request: MergeTrainPullRequestSnapshot
) -> tuple[str, ...]:
    reasons: list[str] = []
    if pull_request.state != "open":
        reasons.append(f"pull request #{pull_request.number} is not open")
    if not pull_request.head_ref:
        reasons.append(f"pull request #{pull_request.number} is missing head ref")
    if not pull_request.base_ref:
        reasons.append(f"pull request #{pull_request.number} is missing base ref")
    if not pull_request.head_repository or not pull_request.base_repository:
        reasons.append(f"pull request #{pull_request.number} is missing repository identity")
    # GitHub repository names are case-insensitive, and the adapter lowercases them.
    train_repository = snapshot.repository.casefold()
    if pull_request.head_repository.casefold() != train_repository:
        reasons.append(f"pull request #{pull_request.number} is not from the train repository")
    if pull_request.base_repository.casefold() != train_repository:
        reasons.append(f"pull request #{pull_request.number} does not target the train repository")
    if pull_request.head_ref == pull_request.base_ref:
        reasons.append(f"pull request #{pull_request.number} has identical head and base refs")
    return tuple(reasons)


def merge_train_stack_child_readiness_reasons(
    *,
    repository_policy: MergeTrainRepositoryPolicy,
    pull_request: MergeTrainPullRequestSnapshot,
) -> tuple[str, ...]:
    # Collapsing merges the child into the root, so the child must be ready to land on its own.
    queue_entry = _build_queue_entry(repository_policy, pull_request)
    return tuple(
        f"stacked pull request #{pull_request.number} is not ready for the train: {reason}"
        for reason in queue_entry.ineligible_reasons
    )


def merge_train_stack_child_readiness_check(
    *,
    reader: MergeTrainPullRequestReader,
    repository: str,
    repository_policy: MergeTrainRepositoryPolicy,
) -> Callable[[int], tuple[str, ...]]:
    def readiness_reasons(pull_request_number: int) -> tuple[str, ...]:
        return merge_train_stack_child_readiness_reasons(
            repository_policy=repository_policy,
            pull_request=reader.read_pull_request_snapshot(
                repository=repository, pull_request_number=pull_request_number
            ),
        )

    return readiness_reasons


def _unsupported_stack_result(
    *,
    snapshot: MergeTrainDryRunSnapshot,
    root_pull_request_number: int,
    unsupported_reasons: tuple[str, ...],
) -> MergeTrainStackDiscoveryResult:
    return MergeTrainStackDiscoveryResult(
        status="unsupported",
        repository=snapshot.repository,
        base_branch=snapshot.base_branch,
        root_pull_request_number=root_pull_request_number,
        unsupported_reasons=unsupported_reasons,
    )


def _stack_entry(pull_request: MergeTrainPullRequestSnapshot) -> MergeTrainStackEntry:
    return MergeTrainStackEntry(
        number=pull_request.number,
        head_sha=pull_request.head_sha,
        head_ref=pull_request.head_ref,
        base_sha=pull_request.base_sha,
        base_ref=pull_request.base_ref,
    )


def _next_action_for_selected_pr(
    repository_policy: MergeTrainRepositoryPolicy,
    selected_pr: MergeTrainQueueEntry | None,
    *,
    skip_branch_update: bool = False,
) -> tuple[MergeTrainDryRunAction, str]:
    if selected_pr is None:
        return "idle", "No eligible pull requests are queued."
    if selected_pr.mergeable == "conflicting":
        return (
            "block",
            f"Add {repository_policy.blocked_label}; pull request has merge conflicts.",
        )
    if selected_pr.required_checks_status == "fail":
        if selected_pr.owner_review_required:
            return (
                "block",
                "Current-head review or required checks failed on pull request "
                f"#{selected_pr.number}.",
            )
        return (
            "block",
            f"Add {repository_policy.blocked_label}; required checks failed.",
        )
    if selected_pr.branch_update_required and not skip_branch_update:
        return "update_branch", "Refresh the pull request against the current base branch."
    if selected_pr.owner_review_required and selected_pr.required_checks_status != "pass":
        return (
            "wait_for_checks",
            "Wait for current-head Client review and required checks on pull request "
            f"#{selected_pr.number}.",
        )
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
