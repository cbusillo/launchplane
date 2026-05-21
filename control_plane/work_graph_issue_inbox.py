from __future__ import annotations

import json
import re
import subprocess
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

import click
from pydantic import BaseModel, ConfigDict, Field

from control_plane.work_graph_github_projects import (
    GitHubProjectPlanningFactsConfig,
    build_github_project_issue_keys,
    github_as_object,
    github_labels,
    github_positive_int,
    github_string,
)


IssueInboxProjectStatus = Literal["present", "missing", "unconfigured"]
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
    repositories: tuple[GitHubIssueInboxRepositoryGroup, ...] = ()


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
    project_issue_keys = _project_issue_keys(config.project_config)
    project_configured = config.project_config is not None
    groups = tuple(
        _repository_group(
            config=config,
            repository=repository,
            project_configured=project_configured,
            project_issue_keys=project_issue_keys,
        )
        for repository in config.repositories
    )
    return GitHubIssueInboxReadModel(
        generated_at=generated_at,
        project_configured=project_configured,
        repository_count=len(groups),
        issue_count=sum(group.issue_count for group in groups),
        repositories=groups,
    )


def _repository_group(
    *,
    config: GitHubIssueInboxConfig,
    repository: str,
    project_configured: bool,
    project_issue_keys: frozenset[str],
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
    return GitHubIssueInboxRepositoryGroup(
        repository=repository,
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


def _run_gh_json_array(command: Sequence[str]) -> list[object]:
    try:
        result = subprocess.run(
            command,
            check=True,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as error:
        raise click.ClickException("GitHub CLI is required for work graph issue inbox.") from error
    except subprocess.CalledProcessError as error:
        detail = (error.stderr or error.stdout or str(error)).strip()
        raise click.ClickException(f"GitHub issue inbox could not be loaded: {detail}") from error
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
