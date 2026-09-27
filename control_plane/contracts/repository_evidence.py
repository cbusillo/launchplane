"""Provider-resolved Git identities and evidence, independent of approval policy."""

from __future__ import annotations

import re
from typing import Literal, Protocol
from pydantic import BaseModel, ConfigDict, Field, model_validator

RepositoryChangeKind = Literal["added", "modified", "removed", "renamed", "unknown"]
RepositoryAuthorshipResolution = Literal["resolved", "unresolved", "conflicting"]
_GIT_SHA_PATTERN = re.compile(r"^[0-9a-f]{40}(?:[0-9a-f]{24})?$")
_REPOSITORY_PATTERN = re.compile(r"^[^/\s]+/[^/\s]+$")
_GIT_REF_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-/]{0,254}$")


def _required_token(value: str, field_name: str) -> str:
    normalized = value.strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty")
    return normalized


def _normalize_decimal_id(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name)
    if not normalized.isdecimal() or int(normalized) < 1:
        raise ValueError(f"{field_name} must be a positive decimal identity")
    return str(int(normalized))


def _normalize_repository(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name).lower()
    if _REPOSITORY_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be owner/name")
    return normalized


def _normalize_git_sha(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name).lower()
    if _GIT_SHA_PATTERN.fullmatch(normalized) is None:
        raise ValueError(f"{field_name} must be a Git SHA")
    return normalized


def _normalize_path(value: str, field_name: str) -> str:
    normalized = _required_token(value, field_name).lstrip("/")
    if ".." in normalized.split("/"):
        raise ValueError(f"{field_name} cannot contain '..'")
    return normalized


class RepositoryTarget(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    repository_id: str
    repository_owner_id: str
    repository: str
    pull_request_number: int = Field(ge=1)
    head_sha: str
    tree_sha: str

    @model_validator(mode="after")
    def _validate_target(self) -> "RepositoryTarget":
        if self.schema_version != 1:
            raise ValueError("Unsupported repository target schema version.")
        object.__setattr__(
            self,
            "repository_id",
            _normalize_decimal_id(self.repository_id, "repository_id"),
        )
        object.__setattr__(
            self,
            "repository_owner_id",
            _normalize_decimal_id(self.repository_owner_id, "repository_owner_id"),
        )
        object.__setattr__(
            self,
            "repository",
            _normalize_repository(self.repository, "repository"),
        )
        object.__setattr__(self, "head_sha", _normalize_git_sha(self.head_sha, "head_sha"))
        object.__setattr__(self, "tree_sha", _normalize_git_sha(self.tree_sha, "tree_sha"))
        return self


class RepositoryChangedFileEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    path: str
    change_kind: RepositoryChangeKind = "unknown"
    previous_path: str | None = None
    source: Literal["server_diff"] = "server_diff"

    @model_validator(mode="after")
    def _validate_file(self) -> "RepositoryChangedFileEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported repository file evidence schema version.")
        object.__setattr__(self, "path", _normalize_path(self.path, "path"))
        if self.previous_path is not None:
            previous_path = _normalize_path(self.previous_path, "previous_path")
            if self.change_kind != "renamed" or previous_path == self.path:
                raise ValueError("previous_path requires a rename from a distinct path")
            object.__setattr__(self, "previous_path", previous_path)
        return self


class RepositoryTargetReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    repository: str
    pull_request_number: int = Field(ge=1)

    @model_validator(mode="after")
    def _validate_reference(self) -> "RepositoryTargetReference":
        if self.schema_version != 1:
            raise ValueError("Unsupported repository target reference schema version.")
        object.__setattr__(
            self,
            "repository",
            _normalize_repository(self.repository, "repository"),
        )
        return self


class RepositoryBaseEvidence(BaseModel):
    """Server-resolved base ref and SHA the reviewed change was compared against."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    base_ref: str
    base_sha: str

    @model_validator(mode="after")
    def _validate_base(self) -> "RepositoryBaseEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported repository base evidence schema version.")
        normalized_ref = _required_token(self.base_ref, "base_ref")
        if _GIT_REF_PATTERN.fullmatch(normalized_ref) is None:
            raise ValueError("base_ref must be a canonical Git ref name")
        object.__setattr__(self, "base_ref", normalized_ref)
        object.__setattr__(self, "base_sha", _normalize_git_sha(self.base_sha, "base_sha"))
        return self


class RepositoryAuthorshipEvidence(BaseModel):
    """Server-resolved numeric GitHub contributing identities over the reviewed range.

    ``resolution`` is ``resolved`` only when every reviewed commit and the pull
    request itself carry a consistent GitHub-linked numeric identity. Missing,
    incomplete, or contradictory identity evidence is never repaired here; it is
    reported without inferring approval requirements.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    resolution: RepositoryAuthorshipResolution
    contributor_github_ids: tuple[int, ...] = ()
    commit_count: int = Field(default=0, ge=0)
    reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _validate_authorship(self) -> "RepositoryAuthorshipEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported repository authorship evidence schema version.")
        for github_id in self.contributor_github_ids:
            if github_id < 1:
                raise ValueError("contributor_github_ids must be positive numeric GitHub IDs")
        object.__setattr__(
            self,
            "contributor_github_ids",
            tuple(sorted(set(self.contributor_github_ids))),
        )
        object.__setattr__(self, "reason", self.reason.strip())
        if self.resolution == "resolved" and not self.contributor_github_ids:
            raise ValueError("resolved repository authorship requires contributing identities")
        if self.resolution != "resolved" and not self.reason:
            raise ValueError("unresolved repository authorship requires a reason")
        return self


class RepositoryEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    target: RepositoryTarget
    merge_commit_sha: str = ""
    changed_files: tuple[RepositoryChangedFileEvidence, ...]
    base: RepositoryBaseEvidence | None = None
    authorship: RepositoryAuthorshipEvidence | None = None

    @model_validator(mode="after")
    def _validate_evidence(self) -> "RepositoryEvidence":
        if self.schema_version != 1:
            raise ValueError("Unsupported repository repository evidence schema version.")
        if self.merge_commit_sha:
            object.__setattr__(
                self,
                "merge_commit_sha",
                _normalize_git_sha(self.merge_commit_sha, "merge_commit_sha"),
            )
        if not self.changed_files:
            raise ValueError("repository repository evidence requires changed files")
        paths = tuple(file.path for file in self.changed_files)
        if len(paths) != len(set(paths)):
            raise ValueError("repository repository evidence paths must be unique")
        object.__setattr__(
            self,
            "changed_files",
            tuple(sorted(self.changed_files, key=lambda changed_file: changed_file.path)),
        )
        return self


class RepositoryEvidenceProvider(Protocol):
    def resolve(self, target: RepositoryTargetReference) -> RepositoryEvidence: ...
