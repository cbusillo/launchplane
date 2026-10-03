from datetime import datetime, timezone
import re
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

_COMMIT_SHA = re.compile(r"^[0-9a-f]{40}$")


class MergeTrainBranchRefreshRecord(BaseModel):
    """The merge train asked the source-control provider to merge a pull request's
    base branch into it, while the pull request's head was ``expected_head_sha``.

    A Client's acceptance of that head may be carried to the commit the refresh
    produced; this record is the proof that the train, not a person, asked for it.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    record_id: str
    repository: str
    base_branch: str
    pull_request_number: int = Field(ge=1)
    expected_head_sha: str
    requested_at: str
    trace_id: str = ""

    @model_validator(mode="before")
    @classmethod
    def _normalize(cls, data: object) -> object:
        if isinstance(data, dict) and isinstance(data.get("expected_head_sha"), str):
            data = {**data, "expected_head_sha": data["expected_head_sha"].strip().lower()}
        return data

    @model_validator(mode="after")
    def _validate_record(self) -> "MergeTrainBranchRefreshRecord":
        for name in ("record_id", "repository", "base_branch", "requested_at"):
            if not getattr(self, name).strip():
                raise ValueError(f"merge train branch refresh requires {name}")
        if "/" not in self.repository:
            raise ValueError("merge train branch refresh repository must be owner/name")
        if not _COMMIT_SHA.fullmatch(self.expected_head_sha):
            raise ValueError("merge train branch refresh requires a full expected_head_sha")
        requested_at_datetime(self)
        return self


def requested_at_datetime(record: MergeTrainBranchRefreshRecord) -> datetime:
    parsed = datetime.fromisoformat(record.requested_at.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("merge train branch refresh requested_at needs a time zone")
    return parsed.astimezone(timezone.utc)


def build_merge_train_branch_refresh_record(
    *,
    repository: str,
    base_branch: str,
    pull_request_number: int,
    expected_head_sha: str,
    requested_at: datetime,
    trace_id: str = "",
) -> MergeTrainBranchRefreshRecord:
    return MergeTrainBranchRefreshRecord(
        record_id=f"merge-train-branch-refresh-{uuid4().hex}",
        repository=repository,
        base_branch=base_branch,
        pull_request_number=pull_request_number,
        expected_head_sha=expected_head_sha,
        requested_at=requested_at.astimezone(timezone.utc)
        .isoformat(timespec="milliseconds")
        .replace("+00:00", "Z"),
        trace_id=trace_id,
    )
