from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

HARBOR_PREVIEW_REQUEST_BLOCK_INFO_STRING = "harbor-preview"
HARBOR_ALLOWED_COMPANION_REPOS: tuple[str, ...] = ("shared-addons",)
HarborPreviewRequestParseStatus = Literal["missing", "valid", "invalid"]


class HarborCompanionPullRequestReference(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repo: str
    pr_number: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_reference(self) -> "HarborCompanionPullRequestReference":
        normalized_repo = self.repo.strip()
        if not normalized_repo:
            raise ValueError("Harbor companion pull request reference requires repo")
        if normalized_repo not in HARBOR_ALLOWED_COMPANION_REPOS:
            raise ValueError(
                f"Harbor companion repo {normalized_repo!r} is not allowlisted for v1 preview metadata"
            )
        return self


class HarborPreviewRequestMetadata(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    companions: tuple[HarborCompanionPullRequestReference, ...] = ()

    @model_validator(mode="after")
    def _validate_metadata(self) -> "HarborPreviewRequestMetadata":
        seen_repos: set[str] = set()
        for companion in self.companions:
            normalized_repo = companion.repo.strip()
            if normalized_repo in seen_repos:
                raise ValueError(
                    f"Harbor preview request metadata contains duplicate companion repo {normalized_repo!r}"
                )
            seen_repos.add(normalized_repo)
        return self


class HarborPreviewRequestParseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: HarborPreviewRequestParseStatus
    metadata: HarborPreviewRequestMetadata | None = None
    error: str = ""

    @model_validator(mode="after")
    def _validate_result(self) -> "HarborPreviewRequestParseResult":
        if self.status == "valid":
            if self.metadata is None:
                raise ValueError("valid Harbor preview request parse result requires metadata")
            if self.error.strip():
                raise ValueError("valid Harbor preview request parse result cannot include error")
            return self
        if self.status == "missing":
            if self.metadata is not None:
                raise ValueError("missing Harbor preview request parse result cannot include metadata")
            if self.error.strip():
                raise ValueError("missing Harbor preview request parse result cannot include error")
            return self
        if self.metadata is not None:
            raise ValueError("invalid Harbor preview request parse result cannot include metadata")
        if not self.error.strip():
            raise ValueError("invalid Harbor preview request parse result requires error")
        return self
