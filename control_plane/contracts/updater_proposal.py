"""Capped source changes for a new updater pull request; no caller-selected ref."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

UPDATER_PROPOSAL_ROUTE = "/v1/source-control/updater-proposals"
UPDATER_PROPOSAL_ACTION = "updater_proposal.create"
UPDATER_PROPOSAL_MAX_BODY_BYTES = 1024 * 1024
UPDATER_PROPOSAL_MAX_CHANGE_BYTES = 512 * 1024
UPDATER_PROPOSAL_MAX_FILES = 32
UPDATER_PROPOSAL_MAX_FILE_BYTES = 1024 * 1024
UPDATER_PROPOSAL_MAX_MATERIALIZED_BYTES = 4 * 1024 * 1024
UPDATER_PROPOSAL_MAX_EDITS = 256


def _validate_path(path: str) -> None:
    parts = path.split("/")
    if (
        any(part in ("", ".", "..", ".git") for part in parts)
        or "\\" in path
        or any(ord(character) < 32 for character in path)
        or path.casefold() in (".github", ".github/workflows")
        or path.casefold().startswith(".github/workflows/")
    ):
        raise ValueError("Updater changes require ordinary relative file paths.")


class UpdaterFileChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    content: str | None
    mode: Literal["100644", "100755"] | None = None

    @model_validator(mode="after")
    def validate_path(self) -> "UpdaterFileChange":
        _validate_path(self.path)
        return self

    @property
    def submitted_change_bytes(self) -> int:
        return len((self.content or "").encode("utf-8"))


class UpdaterTextEdit(BaseModel):
    """Replace exact UTF-8 text at a byte offset in the original base blob."""

    model_config = ConfigDict(extra="forbid")

    start_byte: int = Field(strict=True, ge=0, le=UPDATER_PROPOSAL_MAX_FILE_BYTES)
    old_text: str = Field(max_length=UPDATER_PROPOSAL_MAX_CHANGE_BYTES)
    new_text: str = Field(max_length=UPDATER_PROPOSAL_MAX_CHANGE_BYTES)


class UpdaterFileEdit(BaseModel):
    """Bounded exact edits to an existing regular file, preserving its mode."""

    model_config = ConfigDict(extra="forbid")

    path: str = Field(min_length=1, max_length=512)
    base_blob_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    edits: tuple[UpdaterTextEdit, ...] = Field(min_length=1, max_length=UPDATER_PROPOSAL_MAX_EDITS)

    @model_validator(mode="after")
    def validate_edits(self) -> "UpdaterFileEdit":
        _validate_path(self.path)
        previous_start = -1
        previous_end = 0
        for edit in self.edits:
            if edit.old_text == edit.new_text:
                raise ValueError("Updater text edits must change content.")
            if edit.start_byte <= previous_start or edit.start_byte < previous_end:
                raise ValueError("Updater text edits must be ordered and nonoverlapping.")
            previous_start = edit.start_byte
            previous_end = edit.start_byte + len(edit.old_text.encode("utf-8"))
            edit.new_text.encode("utf-8")
            if previous_end > UPDATER_PROPOSAL_MAX_FILE_BYTES:
                raise ValueError("Updater text edit exceeds the file size limit.")
        return self

    @property
    def submitted_change_bytes(self) -> int:
        return sum(
            len(edit.old_text.encode("utf-8")) + len(edit.new_text.encode("utf-8"))
            for edit in self.edits
        )

    @property
    def size_delta(self) -> int:
        return sum(
            len(edit.new_text.encode("utf-8")) - len(edit.old_text.encode("utf-8"))
            for edit in self.edits
        )


class UpdaterProposalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    repository: str = Field(pattern=r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
    base_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=16384)
    files: tuple[UpdaterFileChange | UpdaterFileEdit, ...] = Field(
        min_length=1, max_length=UPDATER_PROPOSAL_MAX_FILES
    )

    @model_validator(mode="after")
    def validate_changes(self) -> "UpdaterProposalRequest":
        paths = [change.path for change in self.files]
        if len(set(paths)) != len(paths):
            raise ValueError("Updater file paths must be unique.")
        if any(other.startswith(path + "/") for path in paths for other in paths):
            raise ValueError("Updater file paths cannot overlap directories.")
        if sum(change.submitted_change_bytes for change in self.files) > (
            UPDATER_PROPOSAL_MAX_CHANGE_BYTES
        ):
            raise ValueError("Updater change exceeds the content size limit.")
        if (
            sum(len(change.edits) for change in self.files if isinstance(change, UpdaterFileEdit))
            > UPDATER_PROPOSAL_MAX_EDITS
        ):
            raise ValueError("Updater change exceeds the text edit count limit.")
        if not self.title.strip() or re.search(r"[\r\n]", self.title):
            raise ValueError("Updater title requires one nonempty line.")
        return self


class UpdaterProposalResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    disposition: Literal["created", "replayed", "pending"] = "created"
    repository: str
    branch: str
    head_sha: str
    pull_request_number: int = Field(gt=0)
    pull_request_url: str
