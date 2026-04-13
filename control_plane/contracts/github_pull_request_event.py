from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

PullRequestAction = Literal[
    "opened",
    "reopened",
    "synchronize",
    "edited",
    "labeled",
    "unlabeled",
    "closed",
]
PullRequestState = Literal["open", "closed"]


class GitHubPullRequestEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    action: PullRequestAction
    repo: str
    pr_number: int = Field(ge=1)
    pr_url: str
    occurred_at: str = ""
    pr_body: str = ""
    state: PullRequestState
    merged: bool = False
    head_sha: str
    label_names: tuple[str, ...] = ()
    action_label: str = ""

    @model_validator(mode="after")
    def _validate_event(self) -> "GitHubPullRequestEvent":
        if not self.repo.strip():
            raise ValueError("github pull request event requires repo")
        if not self.pr_url.strip():
            raise ValueError("github pull request event requires pr_url")
        if not self.head_sha.strip():
            raise ValueError("github pull request event requires head_sha")
        if self.action == "labeled" and not self.action_label.strip():
            raise ValueError("labeled github pull request event requires action_label")
        return self
