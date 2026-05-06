from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


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
    reasons: tuple[WorkGraphRecommendationReason, ...]


class WorkGraphQueue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    generated_at: str
    items: tuple[WorkGraphQueueItem, ...]
    hidden_count: int = Field(default=0, ge=0)


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


def _queue_sort_key(item: WorkGraphQueueItem) -> tuple[int, str, str, int]:
    return (-item.score, item.repository, item.updated_at, item.number)
