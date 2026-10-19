from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


IssueInboxProjectStatus = Literal["present", "missing", "stale", "closed", "unconfigured"]


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
