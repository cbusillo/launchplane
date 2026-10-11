"""Capped source changes for a new updater pull request; no caller-selected ref."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

UPDATER_PROPOSAL_ROUTE = "/v1/source-control/updater-proposals"
UPDATER_PROPOSAL_ACTION = "updater_proposal.create"
UPDATER_PROPOSAL_MAX_BODY_BYTES = 512 * 1024
UPDATER_PROPOSAL_MAX_CHANGE_BYTES = 256 * 1024
UPDATER_PROPOSAL_MAX_FILES = 32


class UpdaterFileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    content: str | None
    mode: Literal["100644", "100755"] = "100644"

    @model_validator(mode="after")
    def validate_path(self) -> "UpdaterFileChange":
        parts = self.path.split("/")
        if (
            any(part in ("", ".", "..", ".git") for part in parts)
            or "\\" in self.path
            or any(ord(character) < 32 for character in self.path)
            or self.path.casefold() in (".github", ".github/workflows")
            or self.path.casefold().startswith(".github/workflows/")
        ):
            raise ValueError("Updater changes require ordinary relative file paths.")
        return self


class UpdaterProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=16384)
    files: tuple[UpdaterFileChange, ...] = Field(
        min_length=1, max_length=UPDATER_PROPOSAL_MAX_FILES
    )

    @model_validator(mode="after")
    def validate_changes(self) -> "UpdaterProposalRequest":
        paths = [change.path for change in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("Updater file paths must be unique.")
        if any(other.startswith(path + "/") for path in paths for other in paths):
            raise ValueError("Updater file paths cannot overlap directories.")
        if sum(len((change.content or "").encode("utf-8")) for change in self.files) > (
            UPDATER_PROPOSAL_MAX_CHANGE_BYTES
        ):
            raise ValueError("Updater change exceeds the content size limit.")
        if not self.title.strip() or re.search(r"[\r\n]", self.title):
            raise ValueError("Updater title requires one nonempty line.")
        return self


class UpdaterProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    repository: str
    branch: str
    head_sha: str
    pull_request_number: int = Field(gt=0)
    pull_request_url: str
