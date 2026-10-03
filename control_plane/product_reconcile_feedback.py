"""Say on the pull request what an event reconcile did.

A preview reconcile edits one comment on its PR: the preview is ready (with its
URL), destroyed, waiting for a verified build, or failed. A testing reconcile
edits one comment on the PR merged as the desired commit: the deploy is queued,
testing runs it, the lane is held for staff testing, or the deploy failed.

The comment is posted with the product repository's merge-train GitHub App and
no other identity. Posting never changes what the reconcile did: its outcome,
including a failure to post, is kept on the reconcile's plan as ``pr_feedback``.
A reconcile that would say the same thing again posts nothing, so the sweep
does not rewrite comments every half hour.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import logging
from typing import Literal, Protocol, cast
from urllib.parse import quote

import click

from control_plane.child_process_errors import redact_untrusted_text
from control_plane.contracts.preview_pr_feedback_record import (
    PreviewPrFeedbackDeliveryStatus,
    PreviewPrFeedbackRecord,
    PreviewPrFeedbackStatus,
    build_preview_pr_feedback_id,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_reconcile import ProductReconcileRequestRecord
from control_plane.preview_pr_feedback_notifications import (
    deliver_preview_pr_feedback_notifications,
)
from control_plane.product_review_status import OwnerReviewStatus, owner_review_reference_url
from control_plane.testing_lane_hold import STAFF_TESTING_HOLD_REASON
from control_plane.workflows.launchplane import github_api_request, upsert_github_issue_comment
from control_plane.workflows.preview_pr_feedback import render_preview_pr_feedback_markdown

PREVIEW_FEEDBACK_MARKER = "<!-- launchplane-reconcile-preview -->"
TESTING_FEEDBACK_MARKER = "<!-- launchplane-reconcile-testing -->"
PR_FEEDBACK_PLAN_KEY = "pr_feedback"
_FAILURE_SUMMARY_LENGTH = 300
_LOGGER = logging.getLogger(__name__)

TestingFeedbackStatus = Literal["queued", "deployed", "waiting", "failed"]
FeedbackTokenFactory = Callable[[object, LaunchplaneProductProfileRecord], str]
# Writes the pull request's Owner-review status (carrying an acceptance across a
# merge train base refresh first); best-effort, returns the status written.
OwnerReviewStatusWriter = Callable[[LaunchplaneProductProfileRecord, int], OwnerReviewStatus | None]


class ReconcileFeedbackStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...


@dataclass(frozen=True)
class _Feedback:
    status: str
    marker: str
    body: str
    preview_url: str = ""
    revision: str = ""
    failure_summary: str = ""
    commit: str = ""
    owner_review: str = ""


def post_reconcile_feedback(
    *,
    record_store: object,
    request: ProductReconcileRequestRecord,
    plan: dict[str, object],
    error: str,
    feedback_token: FeedbackTokenFactory,
    public_origin: Callable[[], str],
    source: str,
    recorded_at: str,
    owner_review_status: OwnerReviewStatusWriter | None = None,
) -> dict[str, object] | None:
    """Post this reconcile's result on its PR; return what to keep as the plan's ``pr_feedback``.

    Returns the previous entry unchanged when there is nothing new to say, and
    None when there has never been anything to say.
    """
    previous = request.last_plan.get(PR_FEEDBACK_PLAN_KEY)
    previous_entry = cast(dict[str, object], previous) if isinstance(previous, dict) else None
    try:
        profile = cast(ReconcileFeedbackStore, record_store).read_product_profile_record(
            request.product
        )
        feedback = _decide_feedback(
            request=request,
            profile=profile,
            plan=plan,
            error=error,
            public_origin=public_origin,
            owner_review_status=owner_review_status,
        )
        if feedback is None:
            return previous_entry
        body_sha256 = f"sha256:{hashlib.sha256(feedback.body.encode()).hexdigest()}"
        if (
            previous_entry is not None
            and previous_entry.get("body_sha256") == body_sha256
            and (
                previous_entry.get("delivery_status") == "delivered"
                or previous_entry.get("delivery_action") == "no_merged_pull_request"
            )
        ):
            return previous_entry
        return _deliver(
            record_store=record_store,
            request=request,
            profile=profile,
            feedback=feedback,
            body_sha256=body_sha256,
            feedback_token=feedback_token,
            source=source,
            recorded_at=recorded_at,
        )
    except Exception as feedback_error:
        # Feedback never fails or undoes the reconcile it reports.
        _LOGGER.warning(
            "Reconcile PR feedback for %s failed: %s", request.target_key, feedback_error
        )
        return {
            "delivery_status": "failed",
            "error": f"Unexpected {type(feedback_error).__name__} while posting PR feedback.",
        }


def _decide_feedback(
    *,
    request: ProductReconcileRequestRecord,
    profile: LaunchplaneProductProfileRecord,
    plan: dict[str, object],
    error: str,
    public_origin: Callable[[], str],
    owner_review_status: OwnerReviewStatusWriter | None = None,
) -> _Feedback | None:
    if plan.get("deferred"):
        # A busy lane or a moved PR runs again and says what it did then.
        return None
    if request.target_kind == "preview":
        return _preview_feedback(
            request=request,
            profile=profile,
            plan=plan,
            error=error,
            public_origin=public_origin,
            owner_review_status=owner_review_status,
        )
    return _testing_feedback(plan=plan, error=error)


def _preview_feedback(
    *,
    request: ProductReconcileRequestRecord,
    profile: LaunchplaneProductProfileRecord,
    plan: dict[str, object],
    error: str,
    public_origin: Callable[[], str],
    owner_review_status: OwnerReviewStatusWriter | None = None,
) -> _Feedback | None:
    action = plan.get("action")
    revision = _text(plan.get("head_sha"))
    preview_url = ""
    failure_summary = ""
    waiting_for = ""
    status: PreviewPrFeedbackStatus
    if action == "wait" and plan.get("reason") == "no_verified_build":
        status = "pending"
        waiting_for = "a verified build of this commit"
    elif action == "apply" and error:
        status, failure_summary = "failed", _failure_summary(error)
    elif action == "destroy" and error:
        status, failure_summary = "cleanup_failed", _failure_summary(error)
    elif action == "apply" and plan.get("preview_result_status") == "pass":
        status, preview_url = "ready", _text(plan.get("preview_url"))
    elif (
        action == "none"
        and plan.get("reason") == "already_serving"
        and plan.get("current_state") == "active"
        and _text(plan.get("current_preview_url"))
    ):
        # Said again from the live preview, so a later Owner-review label, a fixed
        # public origin, or a post that failed reaches the PR; an unchanged body is
        # not posted again.
        status, preview_url = "ready", _text(plan.get("current_preview_url"))
    elif action == "destroy" and plan.get("preview_result_status") == "pass":
        status, revision = "destroyed", ""
    else:
        return None
    assert request.pull_request_number is not None
    owner_review = ""
    owner_review_url = ""
    owner_review_accepted = ""
    if status == "ready" and plan.get("owner_review_requested") is True:
        owner_review, owner_review_url = _owner_review(
            profile=profile,
            pull_request_number=request.pull_request_number,
            public_origin=public_origin,
        )
        written = (
            owner_review_status(profile, request.pull_request_number)
            if owner_review_status is not None and owner_review == "mentioned"
            else None
        )
        if written is not None and written.state == "success":
            # Said without the @, so an accepted change does not page the Owner again.
            owner_review = "accepted"
            owner_review_accepted = written.description.replace("@", "")
    body = render_preview_pr_feedback_markdown(
        marker=PREVIEW_FEEDBACK_MARKER,
        status=status,
        anchor_pr_number=request.pull_request_number,
        preview_url=preview_url,
        revision=revision,
        failure_summary=failure_summary,
        waiting_for=waiting_for,
        owner_review_requested=owner_review in {"mentioned", "owner_not_set", "accepted"},
        owner_login=profile.owner.github_login,
        owner_review_url=owner_review_url,
        owner_review_accepted=owner_review_accepted,
    )
    return _Feedback(
        status=status,
        marker=PREVIEW_FEEDBACK_MARKER,
        body=body,
        preview_url=preview_url,
        revision=revision,
        failure_summary=failure_summary,
        owner_review=owner_review,
    )


def _owner_review(
    *,
    profile: LaunchplaneProductProfileRecord,
    pull_request_number: int,
    public_origin: Callable[[], str],
) -> tuple[str, str]:
    """How a PR marked for Owner review is answered, and the Owner's review link.

    As on the feedback route: the Owner is mentioned with a link to record the
    decision, or the comment says no Owner is set. Without Launchplane's public
    origin there is no link, so there is no mention, and the plan says why.
    """
    if not profile.owner.is_set:
        return "owner_not_set", ""
    origin = public_origin()
    if not origin:
        return "no_public_origin", ""
    try:
        return "mentioned", owner_review_reference_url(
            public_origin=origin,
            repository=profile.repository,
            pull_request_number=pull_request_number,
        )
    except ValueError:
        return "invalid_public_origin", ""


def _testing_feedback(*, plan: dict[str, object], error: str) -> _Feedback | None:
    commit = _text(plan.get("desired_commit"))
    if not commit:
        return None
    action = plan.get("action")
    reason = plan.get("reason")
    status: TestingFeedbackStatus
    if action == "deploy" and error:
        status = "failed"
    elif action == "wait" and reason == STAFF_TESTING_HOLD_REASON:
        status = "waiting"
    elif action == "deploy" and plan.get("queued_operation_id"):
        status = "queued"
    elif (action == "none" and reason == "already_deployed") or (
        # A generic-web testing deploy runs in the reconcile and records its deployment.
        action == "deploy" and plan.get("deployment_record_id")
    ):
        status = "deployed"
    else:
        return None
    return _Feedback(
        status=status,
        marker=TESTING_FEEDBACK_MARKER,
        body=render_testing_feedback_markdown(
            status=status,
            commit=commit,
            hold_reason=_text(plan.get("hold_reason")),
            failure_summary=_failure_summary(error) if error else "",
        ),
        commit=commit,
    )


def render_testing_feedback_markdown(
    *, status: TestingFeedbackStatus, commit: str, hold_reason: str = "", failure_summary: str = ""
) -> str:
    titles: dict[TestingFeedbackStatus, str] = {
        "queued": "Launchplane queued the testing deploy of this change.",
        "deployed": "The testing lane runs this change.",
        "waiting": "Waiting: the testing lane is held for staff testing.",
        "failed": "Launchplane's testing deploy of this change failed.",
    }
    lines = [TESTING_FEEDBACK_MARKER, titles[status], "", f"- Commit: `{commit}`"]
    if status == "waiting":
        if hold_reason:
            # Operator free text: redacted like a failure summary before it is public.
            lines.append(f"- Hold: {_failure_summary(hold_reason)}")
        lines.extend(["", "Launchplane deploys the newest verified build when the hold is lifted."])
    elif status == "failed":
        if failure_summary:
            lines.append(f"- Failure summary: {failure_summary}")
        lines.extend(["", "Launchplane deploys again when a newer build is verified."])
    lines.extend(["", "Launchplane manages this comment and updates it as the deploy moves."])
    return "\n".join(lines)


@dataclass(frozen=True)
class _Delivery:
    pull_request_number: int
    status: PreviewPrFeedbackDeliveryStatus = "skipped"
    action: str = ""
    comment_id: int = 0
    error: str = ""


class _PreviewFeedbackStore(Protocol):
    def write_preview_pr_feedback_record(self, record: PreviewPrFeedbackRecord) -> object: ...


def _deliver(
    *,
    record_store: object,
    request: ProductReconcileRequestRecord,
    profile: LaunchplaneProductProfileRecord,
    feedback: _Feedback,
    body_sha256: str,
    feedback_token: FeedbackTokenFactory,
    source: str,
    recorded_at: str,
) -> dict[str, object]:
    delivery = _post(
        record_store=record_store,
        request=request,
        profile=profile,
        feedback=feedback,
        feedback_token=feedback_token,
    )
    entry: dict[str, object] = {
        "status": feedback.status,
        "pull_request_number": delivery.pull_request_number,
        "body_sha256": body_sha256,
        "delivery_status": delivery.status,
        "delivery_action": delivery.action,
        "comment_id": delivery.comment_id,
        "error": delivery.error,
    }
    if feedback.owner_review:
        entry["owner_review"] = feedback.owner_review
    if request.target_kind == "preview":
        entry["feedback_id"] = _record_preview_feedback(
            record_store=record_store,
            profile=profile,
            feedback=feedback,
            delivery=delivery,
            source=source,
            recorded_at=recorded_at,
        )
    return entry


def _post(
    *,
    record_store: object,
    request: ProductReconcileRequestRecord,
    profile: LaunchplaneProductProfileRecord,
    feedback: _Feedback,
    feedback_token: FeedbackTokenFactory,
) -> _Delivery:
    pull_request_number = request.pull_request_number or 0
    owner, _, repo = profile.repository.strip().partition("/")
    try:
        # Only the merge-train App posts; a missing or unusable App fails closed.
        token = feedback_token(record_store, profile)
        if not pull_request_number:
            pull_request_number = _merged_pull_request(
                owner=owner, repo=repo, commit=feedback.commit, token=token
            )
            if not pull_request_number:
                return _Delivery(pull_request_number=0, action="no_merged_pull_request")
        comment = upsert_github_issue_comment(
            owner=owner,
            repo=repo,
            issue_number=pull_request_number,
            token=token,
            marker=feedback.marker,
            body=feedback.body,
        )
    except Exception as error:
        # Recorded on the plan, never raised into the reconcile.
        _LOGGER.warning("Reconcile PR feedback for %s failed: %s", request.target_key, error)
        return _Delivery(
            pull_request_number=pull_request_number,
            status="failed",
            error=_delivery_error(error),
        )
    return _Delivery(
        pull_request_number=pull_request_number,
        status="delivered",
        action=comment["action"],
        comment_id=comment["comment_id"],
    )


def _merged_pull_request(*, owner: str, repo: str, commit: str, token: str) -> int:
    """The PR GitHub merged as exactly this commit, or 0 (a direct push)."""
    payload = github_api_request(
        path=f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/commits/{commit}/pulls",
        token=token,
    )
    if not isinstance(payload, list):
        raise click.ClickException("GitHub returned an unexpected list of pull requests.")
    for item in payload:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        if (
            item.get("merged_at")
            and _text(item.get("merge_commit_sha")).lower() == commit.lower()
            and isinstance(number, int)
        ):
            return number
    return 0


def _record_preview_feedback(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    feedback: _Feedback,
    delivery: _Delivery,
    source: str,
    recorded_at: str,
) -> str:
    """Keep the preview's feedback where the route's feedback is kept, and alert on a failure."""
    context = profile.preview.context.strip()
    number = delivery.pull_request_number
    if not hasattr(record_store, "write_preview_pr_feedback_record") or not number or not context:
        return ""
    record = PreviewPrFeedbackRecord(
        feedback_id=build_preview_pr_feedback_id(
            context_name=context, anchor_pr_number=number, requested_at=recorded_at
        ),
        product=profile.product,
        context=context,
        source=source,
        requested_at=recorded_at,
        repository=profile.repository,
        anchor_repo=profile.repository.partition("/")[2],
        anchor_pr_number=number,
        anchor_pr_url=f"https://github.com/{profile.repository}/pull/{number}",
        status=cast(PreviewPrFeedbackStatus, feedback.status),
        marker=feedback.marker,
        comment_markdown=feedback.body,
        preview_url=feedback.preview_url,
        revision=feedback.revision,
        failure_summary=feedback.failure_summary,
        delivery_status=delivery.status,
        delivery_action=delivery.action,
        comment_id=delivery.comment_id,
        error_message=delivery.error,
    )
    cast(_PreviewFeedbackStore, record_store).write_preview_pr_feedback_record(record)
    deliver_preview_pr_feedback_notifications(
        record_store=record_store, feedback=record, attempted_at=recorded_at
    )
    return record.feedback_id


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _failure_summary(error: str) -> str:
    return redact_untrusted_text(
        error,
        fallback="See Launchplane's reconcile record.",
        maximum_length=_FAILURE_SUMMARY_LENGTH,
    )


def _delivery_error(error: Exception) -> str:
    message = error.format_message() if isinstance(error, click.ClickException) else str(error)
    if not message.strip():
        return f"Unexpected {type(error).__name__} while posting PR feedback."
    return _failure_summary(message)
