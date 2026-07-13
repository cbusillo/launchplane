from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import click
from pydantic import BaseModel, ConfigDict, Field

from control_plane.child_process_errors import normalize_child_process_failure
from control_plane.contracts.work_graph_read_model import WorkGraphPlanningIssueFacts
from control_plane.work_graph_github_projects import (
    GitHubProjectPlanningFactsConfig,
    build_github_project_item_facts,
    build_github_project_issue_keys,
    github_as_object,
    github_labels,
    github_positive_int,
    github_string,
)


IssueInboxProjectStatus = Literal["present", "missing", "stale", "closed", "unconfigured"]
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubIssueInboxIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    repository: str
    number: int = Field(ge=1)
    title: str = ""
    url: str = ""
    state: str = "open"
    labels: tuple[str, ...] = ()
    author: str = ""
    created_at: str = ""
    updated_at: str = ""
    project_status: IssueInboxProjectStatus = "unconfigured"
    present_in_project: bool | None = None


class GitHubIssueInboxRepositoryGroup(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str
    issue_count: int = Field(ge=0)
    present_in_project_count: int = Field(ge=0)
    missing_from_project_count: int = Field(ge=0)
    issues: tuple[GitHubIssueInboxIssue, ...] = ()


class GitHubIssueInboxReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    generated_at: str
    project_configured: bool = False
    repository_count: int = Field(ge=0)
    issue_count: int = Field(ge=0)
    stale_project_item_count: int = Field(default=0, ge=0)
    repositories: tuple[GitHubIssueInboxRepositoryGroup, ...] = ()


class GitHubIssueInboxReconcileRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry_run", "apply"] = "dry_run"


class GitHubIssueInboxReconcileItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str
    repository: str
    number: int = Field(ge=1)
    title: str = ""
    url: str = ""
    action: Literal["would_add", "added", "already_present", "failed", "skipped"]
    detail: str = ""
    error_code: str = ""
    error_correlation_id: str = ""


class GitHubIssueInboxReconcileResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    generated_at: str
    mode: Literal["dry_run", "apply"]
    repository_count: int = Field(ge=0)
    issue_count: int = Field(ge=0)
    added_count: int = Field(default=0, ge=0)
    already_present_count: int = Field(default=0, ge=0)
    skipped_count: int = Field(default=0, ge=0)
    failed_count: int = Field(default=0, ge=0)
    would_add_count: int = Field(default=0, ge=0)
    items: tuple[GitHubIssueInboxReconcileItem, ...] = ()


@dataclass(frozen=True)
class GitHubIssueInboxConfig:
    repositories: tuple[str, ...]
    limit_per_repo: int = 100
    gh_binary: str = "gh"
    project_config: GitHubProjectPlanningFactsConfig | None = None

    def __post_init__(self) -> None:
        if not self.repositories:
            raise ValueError("GitHub issue inbox requires at least one repository")
        for repository in self.repositories:
            if _normalize_repository(repository) != repository:
                raise ValueError("GitHub issue inbox repositories must be owner/repo values")
        if self.limit_per_repo < 1:
            raise ValueError("GitHub issue inbox requires a positive per-repo limit")
        if not self.gh_binary.strip():
            raise ValueError("GitHub issue inbox requires a gh binary")


def load_github_issue_inbox_config_from_env(
    environ: dict[str, str],
    *,
    project_config: GitHubProjectPlanningFactsConfig | None = None,
) -> GitHubIssueInboxConfig | None:
    raw_repositories = environ.get("LAUNCHPLANE_WORK_GRAPH_ISSUE_INBOX_REPOSITORIES", "")
    repositories = _parse_repository_inventory(raw_repositories)
    if not repositories:
        return None
    return GitHubIssueInboxConfig(
        repositories=repositories,
        limit_per_repo=_int_env_value(
            environ,
            name="LAUNCHPLANE_WORK_GRAPH_ISSUE_INBOX_LIMIT",
            default=100,
        ),
        gh_binary=environ.get("LAUNCHPLANE_WORK_GRAPH_GH_BINARY", "gh").strip() or "gh",
        project_config=project_config,
    )


def build_github_issue_inbox_read_model(
    *,
    generated_at: str,
    config: GitHubIssueInboxConfig,
) -> GitHubIssueInboxReadModel:
    project_facts = _project_facts(config.project_config)
    project_issue_keys = _project_issue_keys_from_facts(project_facts)
    open_issue_keys: set[str] = set()
    project_configured = config.project_config is not None
    groups = tuple(
        _repository_group(
            config=config,
            repository=repository,
            project_configured=project_configured,
            project_issue_keys=project_issue_keys,
            open_issue_keys=open_issue_keys,
        )
        for repository in config.repositories
    )
    groups = _append_stale_project_items(
        groups=groups,
        project_facts=project_facts,
        open_issue_keys=frozenset(open_issue_keys),
    )
    return GitHubIssueInboxReadModel(
        generated_at=generated_at,
        project_configured=project_configured,
        repository_count=len(groups),
        issue_count=sum(group.issue_count for group in groups),
        stale_project_item_count=sum(
            1
            for group in groups
            for issue in group.issues
            if issue.project_status in {"stale", "closed"}
        ),
        repositories=groups,
    )


def reconcile_github_issue_inbox(
    *,
    generated_at: str,
    config: GitHubIssueInboxConfig,
    request: GitHubIssueInboxReconcileRequest,
) -> GitHubIssueInboxReconcileResult:
    if config.project_config is None:
        raise click.ClickException(
            "Configure LAUNCHPLANE_WORK_GRAPH_PROJECT_OWNER and "
            "LAUNCHPLANE_WORK_GRAPH_PROJECT_NUMBER before reconciling the issue inbox."
        )
    inbox = build_github_issue_inbox_read_model(generated_at=generated_at, config=config)
    open_project_issues = tuple(
        issue
        for group in inbox.repositories
        for issue in group.issues
        if issue.present_in_project is not None
    )
    if request.mode == "dry_run":
        items = tuple(
            _reconcile_item(issue=issue, action="would_add")
            for issue in open_project_issues
            if issue.present_in_project is False
        )
    else:
        current_issue_keys = _project_issue_keys(config.project_config)
        items = tuple(
            _apply_issue_reconcile_item(
                config=config,
                issue=issue,
                current_issue_keys=current_issue_keys,
            )
            for issue in open_project_issues
        )
    return GitHubIssueInboxReconcileResult(
        generated_at=generated_at,
        mode=request.mode,
        repository_count=inbox.repository_count,
        issue_count=inbox.issue_count,
        added_count=sum(1 for item in items if item.action == "added"),
        already_present_count=sum(1 for item in items if item.action == "already_present"),
        skipped_count=sum(1 for item in items if item.action == "skipped"),
        failed_count=sum(1 for item in items if item.action == "failed"),
        would_add_count=sum(1 for item in items if item.action == "would_add"),
        items=items,
    )


def _repository_group(
    *,
    config: GitHubIssueInboxConfig,
    repository: str,
    project_configured: bool,
    project_issue_keys: frozenset[str],
    open_issue_keys: set[str],
) -> GitHubIssueInboxRepositoryGroup:
    issues = tuple(
        sorted(
            (
                _issue_from_gh_payload(
                    repository=repository,
                    raw_issue=raw_issue,
                    project_configured=project_configured,
                    project_issue_keys=project_issue_keys,
                )
                for raw_issue in _list_open_issues(config=config, repository=repository)
            ),
            key=lambda issue: issue.number,
        )
    )
    open_issue_keys.update(issue.key.lower() for issue in issues)
    return GitHubIssueInboxRepositoryGroup(
        repository=repository,
        issue_count=len(issues),
        present_in_project_count=sum(1 for issue in issues if issue.present_in_project is True),
        missing_from_project_count=sum(1 for issue in issues if issue.present_in_project is False),
        issues=issues,
    )


def _append_stale_project_items(
    *,
    groups: tuple[GitHubIssueInboxRepositoryGroup, ...],
    project_facts: tuple[WorkGraphPlanningIssueFacts, ...],
    open_issue_keys: frozenset[str],
) -> tuple[GitHubIssueInboxRepositoryGroup, ...]:
    if not project_facts:
        return groups
    stale_by_repository: dict[str, list[GitHubIssueInboxIssue]] = {}
    for fact in project_facts:
        key = f"{fact.repository}#{fact.number}"
        if fact.is_pull_request or key.lower() in open_issue_keys:
            continue
        state = str(fact.state or "closed")
        project_status: IssueInboxProjectStatus = "closed" if fact.state == "closed" else "stale"
        stale_by_repository.setdefault(fact.repository, []).append(
            GitHubIssueInboxIssue(
                key=key,
                repository=fact.repository,
                number=fact.number,
                title=fact.title,
                url=fact.url,
                state=state,
                labels=fact.labels,
                updated_at=fact.updated_at,
                project_status=project_status,
                present_in_project=True,
            )
        )
    if not stale_by_repository:
        return groups
    configured_repositories = {group.repository.lower() for group in groups}
    updated_groups = [
        _group_with_stale_items(group, stale_by_repository.pop(group.repository, []))
        for group in groups
    ]
    for repository in sorted(stale_by_repository):
        if repository.lower() in configured_repositories:
            continue
        updated_groups.append(
            _group_with_stale_items(
                GitHubIssueInboxRepositoryGroup(
                    repository=repository,
                    issue_count=0,
                    present_in_project_count=0,
                    missing_from_project_count=0,
                ),
                stale_by_repository[repository],
            )
        )
    return tuple(updated_groups)


def _group_with_stale_items(
    group: GitHubIssueInboxRepositoryGroup,
    stale_items: list[GitHubIssueInboxIssue],
) -> GitHubIssueInboxRepositoryGroup:
    if not stale_items:
        return group
    issues = tuple(sorted((*group.issues, *stale_items), key=lambda issue: issue.number))
    return GitHubIssueInboxRepositoryGroup(
        repository=group.repository,
        issue_count=len(issues),
        present_in_project_count=sum(1 for issue in issues if issue.present_in_project is True),
        missing_from_project_count=sum(1 for issue in issues if issue.present_in_project is False),
        issues=issues,
    )


def _list_open_issues(
    *, config: GitHubIssueInboxConfig, repository: str
) -> tuple[dict[str, Any], ...]:
    payload = _run_gh_json_array(
        (
            config.gh_binary,
            "issue",
            "list",
            "--repo",
            repository,
            "--state",
            "open",
            "--limit",
            str(config.limit_per_repo),
            "--json",
            "number,title,url,state,labels,updatedAt,createdAt,author",
        )
    )
    return tuple(
        raw_issue for raw_issue in (github_as_object(item) for item in payload) if raw_issue
    )


def _issue_from_gh_payload(
    *,
    repository: str,
    raw_issue: dict[str, Any],
    project_configured: bool,
    project_issue_keys: frozenset[str],
) -> GitHubIssueInboxIssue:
    number = github_positive_int(raw_issue.get("number"))
    if number is None:
        raise click.ClickException(f"GitHub issue list returned an invalid issue for {repository}.")
    key = f"{repository}#{number}"
    present_in_project: bool | None = None
    project_status: IssueInboxProjectStatus = "unconfigured"
    if project_configured:
        present_in_project = key.lower() in project_issue_keys
        project_status = "present" if present_in_project else "missing"
    author = github_as_object(raw_issue.get("author"))
    return GitHubIssueInboxIssue(
        key=key,
        repository=repository,
        number=number,
        title=github_string(raw_issue.get("title")),
        url=github_string(raw_issue.get("url")),
        state=github_string(raw_issue.get("state")) or "open",
        labels=github_labels(raw_issue.get("labels")),
        author=github_string(author.get("login")) if author is not None else "",
        created_at=github_string(raw_issue.get("createdAt") or raw_issue.get("created_at")),
        updated_at=github_string(raw_issue.get("updatedAt") or raw_issue.get("updated_at")),
        project_status=project_status,
        present_in_project=present_in_project,
    )


def _project_issue_keys(
    project_config: GitHubProjectPlanningFactsConfig | None,
) -> frozenset[str]:
    if project_config is None:
        return frozenset()
    return frozenset(key.lower() for key in build_github_project_issue_keys(project_config))


def _project_facts(
    project_config: GitHubProjectPlanningFactsConfig | None,
) -> tuple[WorkGraphPlanningIssueFacts, ...]:
    if project_config is None:
        return ()
    return build_github_project_item_facts(project_config)


def _project_issue_keys_from_facts(
    project_facts: tuple[WorkGraphPlanningIssueFacts, ...],
) -> frozenset[str]:
    return frozenset(
        f"{fact.repository}#{fact.number}".lower()
        for fact in project_facts
        if not fact.is_pull_request
    )


def _apply_issue_reconcile_item(
    *,
    config: GitHubIssueInboxConfig,
    issue: GitHubIssueInboxIssue,
    current_issue_keys: frozenset[str],
) -> GitHubIssueInboxReconcileItem:
    if issue.key.lower() in current_issue_keys:
        return _reconcile_item(issue=issue, action="already_present")
    if not issue.url.strip():
        return _reconcile_item(
            issue=issue,
            action="skipped",
            detail="GitHub issue has no URL to add to the configured Project.",
        )
    try:
        _project_item_add(config=config, issue=issue)
    except FileNotFoundError as error:
        failure = normalize_child_process_failure(
            operation="Add GitHub issue inbox item to Project",
            tool="github_cli",
            exception=error,
        )
        raise click.ClickException(failure.operator_message()) from error
    except subprocess.CalledProcessError as error:
        failure = normalize_child_process_failure(
            operation="Add GitHub issue inbox item to Project",
            tool="github_cli",
            exception=error,
        )
        return _reconcile_item(
            issue=issue,
            action="failed",
            detail=failure.detail,
            error_code=failure.code,
            error_correlation_id=failure.correlation_id,
        )
    return _reconcile_item(issue=issue, action="added")


def _project_item_add(*, config: GitHubIssueInboxConfig, issue: GitHubIssueInboxIssue) -> None:
    assert config.project_config is not None
    subprocess.run(
        (
            config.gh_binary,
            "project",
            "item-add",
            str(config.project_config.project_number),
            "--owner",
            config.project_config.owner,
            "--url",
            issue.url,
            "--format",
            "json",
        ),
        check=True,
        capture_output=True,
        text=True,
    )


def _reconcile_item(
    *,
    issue: GitHubIssueInboxIssue,
    action: Literal["would_add", "added", "already_present", "failed", "skipped"],
    detail: str = "",
    error_code: str = "",
    error_correlation_id: str = "",
) -> GitHubIssueInboxReconcileItem:
    return GitHubIssueInboxReconcileItem(
        key=issue.key,
        repository=issue.repository,
        number=issue.number,
        title=issue.title,
        url=issue.url,
        action=action,
        detail=detail,
        error_code=error_code,
        error_correlation_id=error_correlation_id,
    )


def _run_gh_json_array(command: Sequence[str]) -> list[object]:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        failure = normalize_child_process_failure(
            operation="Load GitHub issue inbox",
            tool="github_cli",
            exception=error,
        )
        raise click.ClickException(failure.operator_message()) from error
    except subprocess.CalledProcessError as error:
        failure = normalize_child_process_failure(
            operation="Load GitHub issue inbox",
            tool="github_cli",
            exception=error,
        )
        raise click.ClickException(failure.operator_message()) from error
    try:
        payload: object = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise click.ClickException("GitHub issue list returned invalid JSON.") from error
    if not isinstance(payload, list):
        raise click.ClickException("GitHub issue list did not return an array.")
    return payload


def _parse_repository_inventory(value: str) -> tuple[str, ...]:
    repositories: list[str] = []
    seen: set[str] = set()
    for raw_repository in re.split(r"[,\n]", value):
        repository = _normalize_repository(raw_repository)
        if not repository:
            continue
        if repository.lower() in seen:
            continue
        if not _REPOSITORY_PATTERN.fullmatch(repository):
            raise click.ClickException(
                "LAUNCHPLANE_WORK_GRAPH_ISSUE_INBOX_REPOSITORIES must contain "
                "comma or newline separated owner/repo values."
            )
        seen.add(repository.lower())
        repositories.append(repository)
    return tuple(repositories)


def _int_env_value(environ: dict[str, str], *, name: str, default: int) -> int:
    value = environ.get(name, "").strip()
    if not value:
        return default
    try:
        parsed = int(value)
    except ValueError as error:
        raise click.ClickException(f"{name} must be a positive integer.") from error
    if parsed < 1:
        raise click.ClickException(f"{name} must be a positive integer.")
    return parsed


def _normalize_repository(value: str) -> str:
    return value.strip().strip("/")
