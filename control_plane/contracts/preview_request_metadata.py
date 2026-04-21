from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

LAUNCHPLANE_PREVIEW_REQUEST_BLOCK_INFO_STRING = "launchplane-preview"
LAUNCHPLANE_ALLOWED_COMPANION_REPOS: tuple[str, ...] = ("shared-addons",)
LaunchplanePreviewRequestParseStatus = Literal["missing", "valid", "invalid"]


class LaunchplaneCompanionPullRequestReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    pr_number: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_reference(self) -> "LaunchplaneCompanionPullRequestReference":
        normalized_repo = self.repo.strip()
        if not normalized_repo:
            raise ValueError("Launchplane companion pull request reference requires repo")
        if normalized_repo not in LAUNCHPLANE_ALLOWED_COMPANION_REPOS:
            raise ValueError(
                f"Launchplane companion repo {normalized_repo!r} is not allowlisted for v1 preview metadata"
            )
        return self


class LaunchplanePreviewRequestMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    baseline_channel: str = "testing"
    companions: tuple[LaunchplaneCompanionPullRequestReference, ...] = ()

    @model_validator(mode="after")
    def _validate_metadata(self) -> "LaunchplanePreviewRequestMetadata":
        if not self.baseline_channel.strip():
            raise ValueError("Launchplane preview request metadata requires baseline_channel")
        seen_repos: set[str] = set()
        for companion in self.companions:
            normalized_repo = companion.repo.strip()
            if normalized_repo in seen_repos:
                raise ValueError(
                    f"Launchplane preview request metadata contains duplicate companion repo {normalized_repo!r}"
                )
            seen_repos.add(normalized_repo)
        return self


class LaunchplanePreviewRequestParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: LaunchplanePreviewRequestParseStatus
    metadata: LaunchplanePreviewRequestMetadata | None = None
    error: str = ""

    @model_validator(mode="after")
    def _validate_result(self) -> "LaunchplanePreviewRequestParseResult":
        if self.status == "valid":
            if self.metadata is None:
                raise ValueError("valid Launchplane preview request parse result requires metadata")
            if self.error.strip():
                raise ValueError("valid Launchplane preview request parse result cannot include error")
            return self
        if self.status == "missing":
            if self.metadata is not None:
                raise ValueError("missing Launchplane preview request parse result cannot include metadata")
            if self.error.strip():
                raise ValueError("missing Launchplane preview request parse result cannot include error")
            return self
        if self.metadata is not None:
            raise ValueError("invalid Launchplane preview request parse result cannot include metadata")
        if not self.error.strip():
            raise ValueError("invalid Launchplane preview request parse result requires error")
        return self
