import json
import re
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from time import time_ns
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
from control_plane.merge_train_provider_failure import provider_failure
from control_plane.merge_train_planning_scope import PlanningScopeResolver
from control_plane.merge_train_planning_completion import (
    PlanningCompletionFailure,
    PlanningCompletionPending,
    PlanningCompletionProviderFailure,
    PlanningCompletionSourceReview,
    PlanningCompletionReview,
    reconcile_completed_change,
)
from control_plane.merge_train_controller_feedback import build_feedback_payloads
from control_plane.contracts.merge_train_batch import MergeTrainBatchLandingPlanRecord
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy


PLANNING_RECEIPT_PREFIX = "<!-- launchplane-planning-reconciliation "
_PLANNING_NOTE_START = "<!-- launchplane-planning-review:start -->"
_PLANNING_NOTE_END = "<!-- launchplane-planning-review:end -->"


def _planning_comment(body: str, receipt: object, *, review: str = "") -> str:
    controller_body = re.sub(
        r"(?m)^" + re.escape(PLANNING_RECEIPT_PREFIX) + r"[^\n]* -->\n?", "", body
    ).rstrip()
    controller_body = re.sub(
        re.escape(_PLANNING_NOTE_START) + r".*?" + re.escape(_PLANNING_NOTE_END),
        "",
        controller_body,
        flags=re.DOTALL,
    ).rstrip()
    if review:
        controller_body += (
            "\n\n"
            + _PLANNING_NOTE_START
            + "\nPlanning reconciliation is incomplete and needs owning-agent review. "
            + "The merge landed; source planning metadata has not all been verified.\n\n"
            + review
            + "\n"
            + _PLANNING_NOTE_END
        )
    return (
        controller_body
        + "\n\n"
        + PLANNING_RECEIPT_PREFIX
        + json.dumps(receipt, sort_keys=True)
        + " -->"
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


def queue_landing_planning_feedback(
    *,
    store: object,
    landing_record: MergeTrainBatchLandingPlanRecord,
    repository_policy: MergeTrainRepositoryPolicy,
    trace_id: str,
) -> None:
    """Persist native completion before returning to a caller; do no provider IO.

    The existing terminal-feedback recovery uses current policy and its normal
    delivery lock. No workstation, caller comeback, or new watcher owns this job.
    """
    if not repository_policy.enqueue.trusted_automation_github_user_ids:
        return
    if not landing_record.landing_plan.entries or any(
        entry.status != "merged" for entry in landing_record.landing_plan.entries
    ):
        return
    feedback_store = require_merge_train_pr_feedback_record_store(store)
    payloads = build_feedback_payloads(
        response={
            "result": {
                "mode": "land",
                "landing_plan": landing_record.landing_plan.model_dump(mode="json"),
            },
            "records": {"merge_train_batch_landing_plan_record_id": landing_record.record_id},
        },
        source="service:native-landing-completion",
        phase="batch-landing",
    )
    for payload in payloads:
        request = MergeTrainPrFeedbackEnvelope.model_validate(payload)
        if request.event != "completed":
            continue
        with feedback_store.merge_train_feedback_delivery_lock(
            repository=request.repository, pull_request_number=request.pull_request_number
        ):
            latest = feedback_store.list_merge_train_pr_feedback_records(
                repository=request.repository,
                base_branch=request.base_branch,
                pr_number=request.pull_request_number,
                limit=1,
            )
            queued_at = landing_record.updated_at
            if latest:
                previous = latest[0]
                same_landing = previous.controller_record_id == landing_record.record_id
                if same_landing:
                    if (
                        previous.event == "completed"
                        and PLANNING_RECEIPT_PREFIX in previous.comment_markdown
                    ):
                        continue
                    # Preserve both saved evidence records. Completion is observed
                    # after this building record even when its clock is ahead.
                    if _timestamp(previous.recorded_at) >= _timestamp(queued_at):
                        queued_at = previous.recorded_at
                elif _timestamp(previous.recorded_at) > _timestamp(landing_record.updated_at):
                    continue
            queued = build_merge_train_pr_feedback_record(
                request=request,
                policy_key=repository_policy.policy_key,
                policy_sha256=landing_record.landing_plan.policy_sha256,
                token="",
                recorded_at=queued_at,
                response_trace_id=trace_id,
                defer_until=queued_at,
            ).model_copy(
                update={
                    "provider_retry_at": "",
                    "error_message": "Native landing planning completion queued for delivery.",
                }
            )
            queued = queued.model_copy(
                update={
                    "comment_markdown": _planning_comment(
                        queued.comment_markdown, {"ok": False, "status": "queued"}
                    )
                }
            )
            feedback_store.write_merge_train_pr_feedback_record(queued)


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
    planning_author_ids: tuple[int, ...] = (),
    planning_scope_resolver: PlanningScopeResolver | None = None,
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
            planning_author_ids=planning_author_ids,
            planning_scope_resolver=planning_scope_resolver,
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
    planning_author_ids: tuple[int, ...] = (),
    planning_scope_resolver: PlanningScopeResolver | None = None,
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
                "delivery_attempts": 0,
                "retryable": record.event in {"completed", "stale_policy"},
                "retry_at": defer_until,
                "provider_retry_at": defer_until,
                "error_message": "Comment delivery deferred by the provider quota deadline.",
            }
        )
    return deliver_merge_train_pr_feedback_record(
        record=record,
        token=token,
        attempted_at=recorded_at,
        planning_author_ids=planning_author_ids,
        planning_scope_resolver=planning_scope_resolver,
    )


def deliver_merge_train_pr_feedback_record(
    *,
    record: MergeTrainPrFeedbackRecord,
    token: str,
    attempted_at: str,
    planning_author_ids: tuple[int, ...] = (),
    planning_scope_resolver: PlanningScopeResolver | None = None,
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
        source_review = None
        try:
            # Completion is persisted/retried by this same native feedback path,
            # including scheduler landings with no surviving workstation driver.
            if record.event == "completed" and (
                planning_author_ids or PLANNING_RECEIPT_PREFIX in record.comment_markdown
            ):
                # Keep the existing persisted record schema readable by older
                # workers during a rolling deploy or rollback. The receipt is
                # evidence in its saved comment, never an authorization input.
                changes["comment_markdown"] = _planning_comment(
                    record.comment_markdown, {"ok": False, "status": "incomplete"}
                )
                try:
                    receipt = reconcile_completed_change(
                        repository=record.repository,
                        base_branch=record.base_branch,
                        number=record.pull_request_number,
                        token=token,
                        trusted_author_ids=planning_author_ids,
                        attempted_at=attempted_at,
                        scope_resolver=planning_scope_resolver,
                    )
                except PlanningCompletionSourceReview as exc:
                    if exc.receipt is None:
                        raise PlanningCompletionReview(
                            "Planning completion source review is missing its receipt."
                        ) from exc
                    source_review = exc
                    receipt = exc.receipt
                except (KeyError, TypeError, ValueError) as exc:
                    raise PlanningCompletionReview(
                        "Planning completion received malformed provider evidence; owning-agent review required."
                    ) from exc
                changes["comment_markdown"] = _planning_comment(
                    record.comment_markdown,
                    receipt,
                    review=source_review.message if source_review else "",
                )
            comment = upsert_github_issue_comment(
                owner=owner,
                repo=repo,
                issue_number=record.pull_request_number,
                token=token,
                marker=record.marker,
                body=str(changes.get("comment_markdown", record.comment_markdown)),
                skip_unchanged=True,
            )
            changes.update(
                delivery_status="failed" if source_review else "delivered",
                delivery_action=comment["action"],
                comment_id=comment["comment_id"],
                comment_url=comment["comment_url"],
                retryable=False,
                error_message=source_review.message if source_review else "",
            )
        except click.ClickException as exc:
            if isinstance(exc, PlanningCompletionFailure) and exc.receipt is not None:
                changes["comment_markdown"] = _planning_comment(
                    record.comment_markdown, exc.receipt
                )
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


def feedback_provider_backoff_is_active(*, record: MergeTrainPrFeedbackRecord, now: str) -> bool:
    return bool(record.provider_retry_at) and _timestamp(now) < _timestamp(record.provider_retry_at)


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
    evidence = provider_failure(error)
    transient = evidence.retryable or isinstance(error, PlanningCompletionPending)
    rate_limited = evidence.rate_limited
    if isinstance(error, PlanningCompletionProviderFailure):
        transient = error.retryable
        rate_limited = error.rate_limited
    deadlines = [
        _timestamp(attempted_at) + timedelta(seconds=min(60 * 2 ** min(attempts - 1, 4), 900))
    ]
    if transient:
        retry_after = evidence.retry_after
        if retry_after.isdigit():
            deadlines.append(_timestamp(attempted_at) + timedelta(seconds=int(retry_after)))
        elif retry_after:
            try:
                parsed = parsedate_to_datetime(retry_after)
                if parsed.tzinfo is not None:
                    deadlines.append(parsed)
            except (ValueError, TypeError):
                pass
        if evidence.remaining == "0" and evidence.reset.isdigit():
            deadlines.append(datetime.fromtimestamp(int(evidence.reset), tz=timezone.utc))
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
