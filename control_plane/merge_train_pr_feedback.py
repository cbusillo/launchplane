from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from time import time_ns
from urllib.error import HTTPError, URLError
from typing import ContextManager, Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_train_pr_feedback_record import (
    MergeTrainPrFeedbackEvent,
    MergeTrainPrFeedbackRecord,
    build_merge_train_pr_feedback_id,
    merge_train_pr_feedback_marker,
)
from control_plane.workflows.launchplane import upsert_github_issue_comment


class MergeTrainPrFeedbackEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    base_branch: str = "main"
    pull_request_number: int = Field(gt=0)
    event: MergeTrainPrFeedbackEvent
    source: str = ""
    controller_action: str = ""
    controller_record_id: str = ""
    message: str = ""

    @model_validator(mode="after")
    def _validate_envelope(self) -> "MergeTrainPrFeedbackEnvelope":
        self.repository = self.repository.strip()
        self.base_branch = self.base_branch.strip()
        self.source = self.source.strip()
        self.controller_action = self.controller_action.strip()
        self.controller_record_id = self.controller_record_id.strip()
        self.message = self.message.strip()
        if not self.repository:
            raise ValueError("merge train PR feedback requires repository")
        if "/" not in self.repository:
            raise ValueError("merge train repository must be owner/name")
        if not self.base_branch:
            raise ValueError("merge train PR feedback requires base_branch")
        return self


class MergeTrainPrFeedbackRecordStore(Protocol):
    def merge_train_feedback_delivery_lock(
        self, *, repository: str, pull_request_number: int
    ) -> ContextManager[None]: ...

    def write_merge_train_pr_feedback_record(
        self, record: MergeTrainPrFeedbackRecord
    ) -> object: ...

    def list_merge_train_pr_feedback_records(
        self,
        *,
        repository: str = "",
        base_branch: str = "",
        pr_number: int | None = None,
        limit: int | None = None,
        latest_per_pr: bool = False,
        delivery_status: str = "",
        terminal_retry_candidates: bool = False,
        provider_backoff_only: bool = False,
    ) -> tuple[MergeTrainPrFeedbackRecord, ...]: ...


def require_merge_train_pr_feedback_record_store(
    record_store: object,
) -> MergeTrainPrFeedbackRecordStore:
    if hasattr(record_store, "write_merge_train_pr_feedback_record") and hasattr(
        record_store, "list_merge_train_pr_feedback_records"
    ):
        return cast(MergeTrainPrFeedbackRecordStore, record_store)
    raise TypeError("record store does not support merge train PR feedback records")


def write_merge_train_pr_feedback_record(
    *,
    store: MergeTrainPrFeedbackRecordStore,
    request: MergeTrainPrFeedbackEnvelope,
    policy_key: str,
    policy_sha256: str,
    token: str,
    recorded_at: str,
    response_trace_id: str,
    defer_until: str = "",
) -> MergeTrainPrFeedbackRecord:
    with store.merge_train_feedback_delivery_lock(
        repository=request.repository, pull_request_number=request.pull_request_number
    ):
        record = build_merge_train_pr_feedback_record(
            request=request,
            policy_key=policy_key,
            policy_sha256=policy_sha256,
            token=token,
            recorded_at=recorded_at,
            response_trace_id=response_trace_id,
            defer_until=defer_until,
        )
        store.write_merge_train_pr_feedback_record(record)
        return record


def build_merge_train_pr_feedback_record(
    *,
    request: MergeTrainPrFeedbackEnvelope,
    policy_key: str,
    policy_sha256: str,
    token: str,
    recorded_at: str,
    response_trace_id: str,
    defer_until: str = "",
) -> MergeTrainPrFeedbackRecord:
    marker = merge_train_pr_feedback_marker(
        repository=request.repository,
        base_branch=request.base_branch,
        pull_request_number=request.pull_request_number,
    )
    comment_markdown = render_merge_train_pr_feedback_markdown(
        marker=marker,
        request=request,
    )
    record = MergeTrainPrFeedbackRecord(
        feedback_id=build_merge_train_pr_feedback_id(
            repository=request.repository,
            base_branch=request.base_branch,
            pull_request_number=request.pull_request_number,
            event=request.event,
            marker=marker,
            recorded_at=recorded_at,
            response_trace_id=response_trace_id,
        ),
        repository=request.repository,
        base_branch=request.base_branch,
        pull_request_number=request.pull_request_number,
        pull_request_url=(
            f"https://github.com/{request.repository}/pull/{request.pull_request_number}"
        ),
        event=request.event,
        marker=marker,
        comment_markdown=comment_markdown,
        source=request.source or "service:merge-train-pr-feedback",
        recorded_at=recorded_at,
        created_at_ns=time_ns(),
        policy_key=policy_key,
        policy_sha256=policy_sha256,
        controller_action=request.controller_action,
        controller_record_id=request.controller_record_id,
        delivery_status="skipped",
    )

    if defer_until:
        return record.model_copy(
            update={
                "delivery_status": "failed",
                "retry_at": defer_until,
                "provider_retry_at": defer_until,
                "error_message": "Comment delivery deferred by the provider quota deadline.",
            }
        )
    return deliver_merge_train_pr_feedback_record(
        record=record, token=token, attempted_at=recorded_at
    )


def deliver_merge_train_pr_feedback_record(
    *, record: MergeTrainPrFeedbackRecord, token: str, attempted_at: str
) -> MergeTrainPrFeedbackRecord:
    """Deliver the saved body; retry metadata never changes its evidence chronology."""
    attempts = record.delivery_attempts + (record.delivery_status == "failed")
    changes: dict[str, object] = {
        "delivery_attempts": attempts,
        "provider_retry_at": "",
        "retry_at": "",
        "error_message": "",
    }
    if not token:
        changes.update(
            delivery_status="skipped",
            retryable=False,
            error_message="Configured merge train GitHub token is not available.",
        )
    else:
        owner, repo = record.repository.split("/", 1)
        try:
            comment = upsert_github_issue_comment(
                owner=owner,
                repo=repo,
                issue_number=record.pull_request_number,
                token=token,
                marker=record.marker,
                body=record.comment_markdown,
                skip_unchanged=True,
            )
            changes.update(
                delivery_status="delivered",
                delivery_action=comment["action"],
                comment_id=comment["comment_id"],
                comment_url=comment["comment_url"],
                retryable=False,
            )
        except click.ClickException as exc:
            retryable, retry_at, provider_retry_at = _delivery_retry(
                exc, attempted_at=attempted_at, attempts=attempts
            )
            changes.update(
                delivery_status="failed",
                error_message=str(exc),
                retryable=retryable,
                retry_at=retry_at,
                provider_retry_at=provider_retry_at,
            )
    return record.model_copy(update=changes)


def feedback_retry_is_due(record: MergeTrainPrFeedbackRecord, *, now: str) -> bool:
    if record.delivery_status != "failed" or not record.retryable:
        return False
    try:
        deadline = (
            record.retry_at or (_timestamp(record.recorded_at) + timedelta(seconds=60)).isoformat()
        )
        return _timestamp(now) >= _timestamp(deadline)
    except ValueError:
        return False


def _timestamp(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("feedback retry requires an aware timestamp")
    return parsed.astimezone(timezone.utc)


def _delivery_retry(
    error: click.ClickException, *, attempted_at: str, attempts: int
) -> tuple[bool, str, str]:
    cause = error.__cause__
    transient = isinstance(cause, (URLError, OSError))
    rate_limited = False
    deadlines = [
        _timestamp(attempted_at) + timedelta(seconds=min(60 * 2 ** min(attempts - 1, 4), 900))
    ]
    if isinstance(cause, HTTPError):
        headers = cause.headers
        remaining = headers.get("x-ratelimit-remaining", "") if headers else ""
        retry_after = headers.get("retry-after", "") if headers else ""
        rate_limited = cause.code == 429 or (
            cause.code == 403 and (remaining == "0" or bool(retry_after))
        )
        transient = (
            cause.code == 429
            or cause.code >= 500
            or (cause.code == 403 and (remaining == "0" or bool(retry_after)))
        )
        if transient:
            if retry_after.isdigit():
                deadlines.append(_timestamp(attempted_at) + timedelta(seconds=int(retry_after)))
            elif retry_after:
                try:
                    deadlines.append(parsedate_to_datetime(retry_after))
                except (ValueError, TypeError):
                    pass
            reset = headers.get("x-ratelimit-reset", "") if headers else ""
            if remaining == "0" and reset.isdigit():
                deadlines.append(datetime.fromtimestamp(int(reset), tz=timezone.utc))
    retry_at = max(deadlines).isoformat() if transient else ""
    return transient, retry_at, retry_at if rate_limited else ""


def render_merge_train_pr_feedback_markdown(
    *, marker: str, request: MergeTrainPrFeedbackEnvelope
) -> str:
    event_titles = {
        "queued": "Launchplane queued this pull request in the merge train.",
        "building": "Launchplane is building a merge-train candidate.",
        "waiting": "Launchplane is waiting before the next merge-train step.",
        "blocked": "Launchplane blocked the merge-train step for this pull request.",
        "stale_policy": "Launchplane parked this merge-train record because policy changed.",
        "completed": "Launchplane completed the merge-train step for this pull request.",
    }
    lines = [
        marker,
        event_titles[request.event],
        "",
        f"- Repository: `{request.repository}`",
        f"- Base branch: `{request.base_branch}`",
        f"- Pull request: #{request.pull_request_number}",
    ]
    if request.controller_action:
        lines.append(f"- Controller action: `{request.controller_action}`")
    if request.controller_record_id:
        lines.append(f"- Controller record: `{request.controller_record_id}`")
    if request.message:
        lines.extend(["", request.message])
    lines.extend(
        [
            "",
            "Launchplane manages this comment and will update it as the train moves.",
        ]
    )
    return "\n".join(lines)
