from typing import Literal, Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.merge_train_pr_feedback_record import (
    MergeTrainPrFeedbackEvent,
    MergeTrainPrFeedbackRecord,
    build_merge_train_pr_feedback_id,
    merge_train_pr_feedback_marker,
)
from control_plane.workflows.launchplane import (
    _github_comment_url,
    create_github_issue_comment,
    find_github_issue_comment_by_marker,
    update_github_issue_comment,
)


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
    ) -> tuple[MergeTrainPrFeedbackRecord, ...]: ...


def require_merge_train_pr_feedback_record_store(
    record_store: object,
) -> MergeTrainPrFeedbackRecordStore:
    if hasattr(record_store, "write_merge_train_pr_feedback_record") and hasattr(
        record_store, "list_merge_train_pr_feedback_records"
    ):
        return cast(MergeTrainPrFeedbackRecordStore, record_store)
    raise TypeError("record store does not support merge train PR feedback records")


def build_merge_train_pr_feedback_record(
    *,
    request: MergeTrainPrFeedbackEnvelope,
    policy_key: str,
    policy_sha256: str,
    token: str,
    recorded_at: str,
    response_trace_id: str,
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
    delivery_status: Literal["delivered", "skipped", "failed"] = "skipped"
    delivery_action = ""
    comment_id = 0
    comment_url = ""
    error_message = ""
    owner, repo = request.repository.split("/", 1)
    if not token:
        error_message = "Configured merge train GitHub token is not available."
    else:
        try:
            existing_comment = find_github_issue_comment_by_marker(
                owner=owner,
                repo=repo,
                issue_number=request.pull_request_number,
                token=token,
                marker=marker,
            )
            if existing_comment is not None:
                existing_comment_id = existing_comment.get("id")
                if not isinstance(existing_comment_id, int):
                    raise click.ClickException(
                        "Existing merge train feedback comment is missing a numeric id."
                    )
                updated_comment = update_github_issue_comment(
                    owner=owner,
                    repo=repo,
                    comment_id=existing_comment_id,
                    token=token,
                    body=comment_markdown,
                )
                delivery_action = "updated_comment"
                comment_id = existing_comment_id
                comment_url = _github_comment_url(updated_comment)
            else:
                created_comment = create_github_issue_comment(
                    owner=owner,
                    repo=repo,
                    issue_number=request.pull_request_number,
                    token=token,
                    body=comment_markdown,
                )
                created_comment_id = created_comment.get("id")
                delivery_action = "created_comment"
                comment_id = created_comment_id if isinstance(created_comment_id, int) else 0
                comment_url = _github_comment_url(created_comment)
            delivery_status = "delivered"
        except click.ClickException as exc:
            delivery_status = "failed"
            error_message = str(exc)
    return MergeTrainPrFeedbackRecord(
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
        policy_key=policy_key,
        policy_sha256=policy_sha256,
        controller_action=request.controller_action,
        controller_record_id=request.controller_record_id,
        delivery_status=delivery_status,
        delivery_action=delivery_action,
        comment_id=comment_id,
        comment_url=comment_url,
        error_message=error_message,
    )


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
