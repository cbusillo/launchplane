from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.data_provenance import agent_safe_host_label
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.product_environment_read_model import ProductSiteOverview
from control_plane.contracts.repo_product_mapping_read_model import RepoProductMapping


RepoClassification = Literal[
    "managed_runtime",
    "active_awareness",
    "support_dependency",
    "out_of_scope",
]
WorkItemFocus = Literal["Now", "Next", "Waiting", "Later", "Done", "Unknown"]
WorkItemState = Literal["ready", "waiting", "blocked", "done", "hidden"]
WorkItemRecommendation = Literal[
    "quick_win",
    "deep_work",
    "switch_projects",
    "blocked_cleanup",
    "attention_needed",
    "watch",
]


class WorkGraphRepoSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    classification: RepoClassification
    product: str = ""
    display_name: str = ""

    @model_validator(mode="after")
    def _validate_repo(self) -> "WorkGraphRepoSnapshot":
        if not self.repository.strip() or "/" not in self.repository.strip():
            raise ValueError("work graph repo snapshot requires owner/repo repository")
        if self.classification == "managed_runtime" and not self.product.strip():
            raise ValueError("managed runtime repo snapshot requires product")
        return self


class WorkGraphIssueSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    number: int = Field(ge=1)
    title: str
    url: str
    state: Literal["open", "closed"] = "open"
    focus: WorkItemFocus = "Unknown"
    manager: str = ""
    finish_line: str = ""
    labels: tuple[str, ...] = ()
    blocked_by: int = Field(default=0, ge=0)
    blocking: int = Field(default=0, ge=0)
    subissues_total: int = Field(default=0, ge=0)
    subissues_completed: int = Field(default=0, ge=0)
    updated_at: str = ""
    is_pull_request: bool = False
    check_state: Literal["success", "pending", "failure", "unknown"] = "unknown"
    deploy_state: Literal["success", "pending", "failure", "unknown"] = "unknown"

    @model_validator(mode="after")
    def _validate_issue(self) -> "WorkGraphIssueSnapshot":
        if not self.repository.strip() or "/" not in self.repository.strip():
            raise ValueError("work graph issue snapshot requires owner/repo repository")
        if not self.title.strip():
            raise ValueError("work graph issue snapshot requires title")
        if not self.url.strip():
            raise ValueError("work graph issue snapshot requires url")
        if self.subissues_completed > self.subissues_total:
            raise ValueError("completed subissue count cannot exceed total subissue count")
        return self


class WorkGraphPlanningIssueFacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    number: int = Field(ge=1)
    title: str = ""
    url: str = ""
    state: Literal["open", "closed"] | None = None
    focus: WorkItemFocus | None = None
    manager: str = ""
    finish_line: str = ""
    labels: tuple[str, ...] = ()
    blocked_by: int | None = Field(default=None, ge=0)
    blocking: int | None = Field(default=None, ge=0)
    subissues_total: int | None = Field(default=None, ge=0)
    subissues_completed: int | None = Field(default=None, ge=0)
    updated_at: str = ""
    is_pull_request: bool | None = None
    check_state: Literal["success", "pending", "failure", "unknown"] | None = None
    deploy_state: Literal["success", "pending", "failure", "unknown"] | None = None

    @model_validator(mode="after")
    def _validate_planning_facts(self) -> "WorkGraphPlanningIssueFacts":
        if not self.repository.strip() or "/" not in self.repository.strip():
            raise ValueError("work graph planning facts require owner/repo repository")
        if (
            self.subissues_total is not None
            and self.subissues_completed is not None
            and self.subissues_completed > self.subissues_total
        ):
            raise ValueError("completed subissue count cannot exceed total subissue count")
        return self


class WorkGraphSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    generated_at: str
    repos: tuple[WorkGraphRepoSnapshot, ...] = ()
    issues: tuple[WorkGraphIssueSnapshot, ...]

    @model_validator(mode="after")
    def _validate_snapshot(self) -> "WorkGraphSnapshot":
        if not self.generated_at.strip():
            raise ValueError("work graph snapshot requires generated_at")
        repositories = {repo.repository.strip().lower() for repo in self.repos}
        missing = sorted(
            {
                issue.repository.strip().lower()
                for issue in self.issues
                if issue.repository.strip().lower() not in repositories
            }
        )
        if missing:
            raise ValueError(
                "work graph snapshot is missing repo classifications: " + ", ".join(missing)
            )
        return self


class WorkGraphRecommendationReason(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    detail: str


class WorkGraphQueueEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    state: Literal["verified", "recorded", "stale", "missing", "unsupported"]
    detail: str
    source_url: str = ""


class WorkGraphQueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    repo_classification: RepoClassification
    product: str = ""
    product_display_name: str = ""
    number: int
    title: str
    url: str
    focus: WorkItemFocus
    manager: str = ""
    finish_line: str = ""
    state: WorkItemState
    recommendation: WorkItemRecommendation
    score: int
    updated_at: str = ""
    safe_to_start: bool
    next_action: str
    why_now: str
    blocked_by_count: int = Field(default=0, ge=0)
    source_of_truth_url: str
    handoff_url: str = ""
    evidence: tuple[WorkGraphQueueEvidence, ...] = ()
    reasons: tuple[WorkGraphRecommendationReason, ...]


class WorkGraphQueue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    generated_at: str
    items: tuple[WorkGraphQueueItem, ...]
    hidden_count: int = Field(default=0, ge=0)


def build_work_graph_snapshot_from_records(
    *,
    generated_at: str,
    product_overviews: tuple[ProductSiteOverview, ...],
    work_requests: tuple[EveryCodeWorkRequestRecord, ...],
    planning_issue_facts: tuple[WorkGraphPlanningIssueFacts, ...] = (),
) -> WorkGraphSnapshot:
    repo_snapshots: dict[str, WorkGraphRepoSnapshot] = {}
    for product in product_overviews:
        repository = product.repository.strip()
        if not repository or "/" not in repository:
            continue
        repo_snapshots[repository.lower()] = WorkGraphRepoSnapshot(
            repository=repository,
            classification="managed_runtime",
            product=product.product,
            display_name=product.display_name or product.product,
        )
    for request in work_requests:
        repository = request.repository.strip()
        if not repository or "/" not in repository:
            continue
        repo_snapshots.setdefault(
            repository.lower(),
            WorkGraphRepoSnapshot(
                repository=repository,
                classification="active_awareness",
                display_name=repository.rsplit("/", maxsplit=1)[-1],
            ),
        )
    planning_facts_by_issue = {
        (facts.repository.strip().lower(), facts.number): facts for facts in planning_issue_facts
    }
    request_issue_keys: set[tuple[str, int]] = set()
    issues: list[WorkGraphIssueSnapshot] = []
    for request in work_requests:
        key = (request.repository.strip().lower(), request.issue_number)
        request_issue_keys.add(key)
        issues.append(
            _apply_planning_issue_facts(
                _work_request_issue_snapshot(request),
                planning_facts_by_issue.get(key),
            )
        )
    for facts in planning_issue_facts:
        repository_key = facts.repository.strip().lower()
        key = (repository_key, facts.number)
        if key in request_issue_keys or repository_key not in repo_snapshots:
            continue
        issue = _planning_facts_issue_snapshot(facts)
        if issue is not None:
            issues.append(issue)
    return WorkGraphSnapshot(
        generated_at=generated_at,
        repos=tuple(repo_snapshots.values()),
        issues=tuple(issues),
    )


def build_work_graph_snapshot_from_repo_mapping(
    *,
    generated_at: str,
    repo_mapping: RepoProductMapping,
    work_requests: tuple[EveryCodeWorkRequestRecord, ...],
    planning_issue_facts: tuple[WorkGraphPlanningIssueFacts, ...] = (),
) -> WorkGraphSnapshot:
    repo_snapshots = {
        entry.repository.strip().lower(): WorkGraphRepoSnapshot(
            repository=entry.repository,
            classification=entry.classification,
            product=entry.product,
            display_name=entry.display_name,
        )
        for entry in repo_mapping.repositories
    }
    planning_facts_by_issue = {
        (facts.repository.strip().lower(), facts.number): facts for facts in planning_issue_facts
    }
    request_issue_keys: set[tuple[str, int]] = set()
    issues: list[WorkGraphIssueSnapshot] = []
    for request in work_requests:
        key = (request.repository.strip().lower(), request.issue_number)
        request_issue_keys.add(key)
        if key[0] not in repo_snapshots:
            continue
        issues.append(
            _apply_planning_issue_facts(
                _work_request_issue_snapshot(request),
                planning_facts_by_issue.get(key),
            )
        )
    for facts in planning_issue_facts:
        repository_key = facts.repository.strip().lower()
        key = (repository_key, facts.number)
        if key in request_issue_keys or repository_key not in repo_snapshots:
            continue
        issue = _planning_facts_issue_snapshot(facts)
        if issue is not None:
            issues.append(issue)
    return WorkGraphSnapshot(
        generated_at=generated_at,
        repos=tuple(repo_snapshots.values()),
        issues=tuple(issues),
    )


def build_work_graph_queue(snapshot: WorkGraphSnapshot, *, limit: int = 25) -> WorkGraphQueue:
    if limit < 1:
        raise ValueError("work graph queue limit must be positive")
    repos = {repo.repository.strip().lower(): repo for repo in snapshot.repos}
    ranked_items: list[WorkGraphQueueItem] = []
    hidden_count = 0
    for issue in snapshot.issues:
        repo = repos[issue.repository.strip().lower()]
        item = _build_queue_item(issue=issue, repo=repo)
        if item.state == "hidden":
            hidden_count += 1
            continue
        ranked_items.append(item)
    ranked_items.sort(key=_queue_sort_key)
    return WorkGraphQueue(
        generated_at=snapshot.generated_at,
        items=tuple(ranked_items[:limit]),
        hidden_count=hidden_count + max(0, len(ranked_items) - limit),
    )


def _work_request_issue_snapshot(request: EveryCodeWorkRequestRecord) -> WorkGraphIssueSnapshot:
    return WorkGraphIssueSnapshot(
        repository=request.repository,
        number=request.issue_number,
        title=request.issue_title or f"Issue #{request.issue_number}",
        url=request.issue_url,
        state="closed" if request.state == "done" else "open",
        focus=_work_request_focus(request),
        manager=agent_safe_host_label(request.claimed_by_host) or request.trigger_actor or "Code",
        finish_line=request.result_summary,
        labels=tuple(label for label in (request.trigger_label,) if label),
        blocked_by=1 if request.state == "blocked" else 0,
        updated_at=request.updated_at or request.queued_at,
        check_state="failure" if request.state == "blocked" else "unknown",
    )


def _planning_facts_issue_snapshot(
    facts: WorkGraphPlanningIssueFacts,
) -> WorkGraphIssueSnapshot | None:
    title = facts.title.strip()
    url = facts.url.strip()
    if not title or not url:
        return None
    return _apply_planning_issue_facts(
        WorkGraphIssueSnapshot(
            repository=facts.repository,
            number=facts.number,
            title=title,
            url=url,
        ),
        facts,
    )


def _apply_planning_issue_facts(
    issue: WorkGraphIssueSnapshot, facts: WorkGraphPlanningIssueFacts | None
) -> WorkGraphIssueSnapshot:
    if facts is None:
        return issue
    updates: dict[str, object] = {}
    if facts.state is not None:
        updates["state"] = facts.state
    if facts.focus is not None:
        updates["focus"] = facts.focus
    if facts.manager.strip():
        updates["manager"] = facts.manager
    if facts.finish_line.strip():
        updates["finish_line"] = facts.finish_line
    if facts.labels:
        updates["labels"] = _merge_labels(issue.labels, facts.labels)
    for field_name in (
        "blocked_by",
        "blocking",
        "subissues_total",
        "is_pull_request",
        "check_state",
        "deploy_state",
    ):
        value = getattr(facts, field_name)
        if value is not None:
            updates[field_name] = value
    if facts.subissues_completed is not None:
        next_total = (
            facts.subissues_total if facts.subissues_total is not None else issue.subissues_total
        )
        if next_total:
            updates["subissues_completed"] = facts.subissues_completed
    if facts.updated_at.strip():
        updates["updated_at"] = facts.updated_at
    if not updates:
        return issue
    return WorkGraphIssueSnapshot.model_validate(issue.model_dump(mode="python") | updates)


def _merge_labels(
    existing_labels: tuple[str, ...], incoming_labels: tuple[str, ...]
) -> tuple[str, ...]:
    labels: list[str] = []
    seen: set[str] = set()
    for label in (*existing_labels, *incoming_labels):
        normalized = label.strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        labels.append(normalized)
        seen.add(key)
    return tuple(labels)


def _work_request_focus(request: EveryCodeWorkRequestRecord) -> WorkItemFocus:
    if request.state == "blocked":
        return "Waiting"
    if request.state == "done":
        return "Done"
    if request.state in {"claimed", "running"}:
        return "Now"
    return "Next"


def _build_queue_item(
    *, issue: WorkGraphIssueSnapshot, repo: WorkGraphRepoSnapshot
) -> WorkGraphQueueItem:
    state = _work_item_state(issue)
    recommendation = _recommendation(issue=issue, state=state)
    reasons = _recommendation_reasons(issue=issue, repo=repo, state=state)
    return WorkGraphQueueItem(
        repository=issue.repository,
        repo_classification=repo.classification,
        product=repo.product,
        product_display_name=repo.display_name,
        number=issue.number,
        title=issue.title,
        url=issue.url,
        focus=issue.focus,
        manager=issue.manager,
        finish_line=issue.finish_line,
        state=state,
        recommendation=recommendation,
        score=_score(issue=issue, repo=repo, state=state, recommendation=recommendation),
        updated_at=issue.updated_at,
        safe_to_start=_safe_to_start(issue=issue, state=state),
        next_action=_next_action(issue=issue, state=state, recommendation=recommendation),
        why_now=_why_now(issue=issue, repo=repo, state=state, recommendation=recommendation),
        blocked_by_count=issue.blocked_by,
        source_of_truth_url=issue.url,
        handoff_url=issue.url,
        evidence=_queue_evidence(issue=issue),
        reasons=reasons,
    )


def _work_item_state(issue: WorkGraphIssueSnapshot) -> WorkItemState:
    if issue.state == "closed" or issue.focus == "Done":
        return "hidden"
    if "plan:blocked" in issue.labels or issue.blocked_by > 0:
        return "blocked"
    if issue.focus == "Waiting":
        return "waiting"
    if issue.check_state == "failure" or issue.deploy_state == "failure":
        return "ready"
    if issue.focus in {"Now", "Next"}:
        return "ready"
    return "waiting"


def _recommendation(
    *, issue: WorkGraphIssueSnapshot, state: WorkItemState
) -> WorkItemRecommendation:
    if state == "blocked":
        return "blocked_cleanup"
    if issue.check_state == "failure" or issue.deploy_state == "failure":
        return "attention_needed"
    if issue.focus == "Now":
        return "deep_work"
    if issue.focus == "Next" and issue.subissues_total == 0:
        return "quick_win"
    if issue.focus == "Next":
        return "deep_work"
    if state == "waiting":
        return "watch"
    return "switch_projects"


def _score(
    *,
    issue: WorkGraphIssueSnapshot,
    repo: WorkGraphRepoSnapshot,
    state: WorkItemState,
    recommendation: WorkItemRecommendation,
) -> int:
    if state == "hidden":
        return 0
    score = 0
    score += {
        "ready": 70,
        "waiting": 30,
        "blocked": 20,
        "done": 0,
        "hidden": 0,
    }[state]
    score += {"Now": 30, "Next": 22, "Waiting": 0, "Later": -8, "Done": -100, "Unknown": -2}[
        issue.focus
    ]
    score += {
        "attention_needed": 24,
        "quick_win": 18,
        "deep_work": 12,
        "switch_projects": 8,
        "blocked_cleanup": 4,
        "watch": 0,
    }[recommendation]
    if issue.check_state == "failure" or issue.deploy_state == "failure":
        score += 36
    if repo.classification == "managed_runtime":
        score += 8
    if issue.blocking:
        score += min(issue.blocking * 3, 12)
    if issue.subissues_total:
        remaining = issue.subissues_total - issue.subissues_completed
        score -= min(remaining * 2, 12)
    return score


def _recommendation_reasons(
    *, issue: WorkGraphIssueSnapshot, repo: WorkGraphRepoSnapshot, state: WorkItemState
) -> tuple[WorkGraphRecommendationReason, ...]:
    reasons: list[WorkGraphRecommendationReason] = [
        WorkGraphRecommendationReason(
            code="repo_classification",
            detail=f"{repo.repository} is {repo.classification.replace('_', ' ')}.",
        ),
        WorkGraphRecommendationReason(
            code="state",
            detail=f"Work item is {state}.",
        ),
    ]
    if issue.focus != "Unknown":
        reasons.append(
            WorkGraphRecommendationReason(
                code="focus", detail=f"Code Plans focus is {issue.focus}."
            )
        )
    if issue.check_state == "failure" or issue.deploy_state == "failure":
        reasons.append(
            WorkGraphRecommendationReason(
                code="failed_signal",
                detail="A check or deploy signal is failing and needs operator attention.",
            )
        )
    if issue.blocked_by:
        reasons.append(
            WorkGraphRecommendationReason(
                code="blocked_by", detail=f"Blocked by {issue.blocked_by} dependency item(s)."
            )
        )
    if issue.blocking:
        reasons.append(
            WorkGraphRecommendationReason(
                code="blocking", detail=f"Unblocks {issue.blocking} other item(s)."
            )
        )
    if issue.finish_line.strip():
        reasons.append(WorkGraphRecommendationReason(code="finish_line", detail=issue.finish_line))
    return tuple(reasons)


def _safe_to_start(*, issue: WorkGraphIssueSnapshot, state: WorkItemState) -> bool:
    if state != "ready":
        return False
    return issue.focus in {"Now", "Next"} or issue.check_state == "failure" or issue.deploy_state == "failure"


def _next_action(
    *,
    issue: WorkGraphIssueSnapshot,
    state: WorkItemState,
    recommendation: WorkItemRecommendation,
) -> str:
    if state == "blocked":
        return "Inspect the blocking dependency before starting implementation."
    if state == "waiting":
        return "Wait for the next external signal or move the item into a ready lane."
    if issue.check_state == "failure" or issue.deploy_state == "failure":
        return "Open the failing check or deploy source and fix the current regression."
    if issue.focus == "Now":
        return "Continue the active implementation path from the linked source of truth."
    if recommendation == "quick_win":
        return "Start a focused branch from the linked source of truth."
    if recommendation == "deep_work":
        return "Confirm the next slice, then start a focused branch from the linked source of truth."
    return "Review the linked source of truth before changing code."


def _why_now(
    *,
    issue: WorkGraphIssueSnapshot,
    repo: WorkGraphRepoSnapshot,
    state: WorkItemState,
    recommendation: WorkItemRecommendation,
) -> str:
    if issue.check_state == "failure" or issue.deploy_state == "failure":
        return "A failing check or deploy signal needs operator attention."
    if state == "blocked":
        return "The item is visible because dependency cleanup may unblock later work."
    if issue.blocking:
        return f"It unblocks {issue.blocking} other item(s)."
    if recommendation == "quick_win":
        return "It is ready, focused, and has no tracked subissues."
    if issue.focus == "Now":
        return "It is marked as the active focus item."
    if repo.classification == "managed_runtime":
        return "It belongs to a Launchplane-managed runtime repo."
    return "It is ranked from compact planning and operational signals."


def _queue_evidence(issue: WorkGraphIssueSnapshot) -> tuple[WorkGraphQueueEvidence, ...]:
    evidence = [
        WorkGraphQueueEvidence(
            code="source_of_truth",
            state="verified",
            detail="Linked GitHub issue or pull request is the source of truth.",
            source_url=issue.url,
        )
    ]
    if issue.focus != "Unknown":
        evidence.append(
            WorkGraphQueueEvidence(
                code="focus",
                state="recorded",
                detail=f"Focus lane is {issue.focus}.",
                source_url=issue.url,
            )
        )
    if issue.check_state != "unknown":
        evidence.append(
            WorkGraphQueueEvidence(
                code="checks",
                state="verified",
                detail=f"Check state is {issue.check_state}.",
                source_url=issue.url,
            )
        )
    if issue.deploy_state != "unknown":
        evidence.append(
            WorkGraphQueueEvidence(
                code="deploy",
                state="verified",
                detail=f"Deploy state is {issue.deploy_state}.",
                source_url=issue.url,
            )
        )
    if issue.blocked_by:
        evidence.append(
            WorkGraphQueueEvidence(
                code="blocked_by",
                state="verified",
                detail=f"Blocked by {issue.blocked_by} dependency item(s).",
                source_url=issue.url,
            )
        )
    return tuple(evidence)


def _queue_sort_key(item: WorkGraphQueueItem) -> tuple[int, str, str, int]:
    return (-item.score, item.repository, item.updated_at, item.number)
