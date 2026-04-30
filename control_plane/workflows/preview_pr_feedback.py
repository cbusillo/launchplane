from pathlib import Path

import click

from control_plane.contracts.preview_pr_feedback_record import (
    PreviewPrFeedbackRecord,
    PreviewPrFeedbackStatus,
    build_preview_pr_feedback_id,
)
from control_plane.workflows.launchplane import (
    create_github_issue_comment,
    delete_github_issue_comment,
    find_github_issue_comment_by_marker,
    github_pull_request_reference,
    resolve_launchplane_github_token,
    update_github_issue_comment,
)

DEFAULT_PREVIEW_FEEDBACK_MARKER = "<!-- verireel-preview-control -->"


def _comment_url(payload: dict[str, object]) -> str:
    html_url = payload.get("html_url")
    return html_url if isinstance(html_url, str) else ""


def _render_preview_pr_feedback_markdown(
    *,
    marker: str,
    status: PreviewPrFeedbackStatus,
    anchor_pr_number: int,
    preview_url: str,
    immutable_image_reference: str,
    refresh_image_reference: str,
    revision: str,
    run_url: str,
    failure_summary: str,
) -> str:
    lines = [marker]
    if status == "ready":
        lines.extend(
            [
                f"Launchplane preview is ready for PR #{anchor_pr_number}.",
                "",
            ]
        )
    elif status == "destroyed":
        lines.extend(
            [
                f"Launchplane retired the preview for PR #{anchor_pr_number}.",
                "",
            ]
        )
    elif status == "cleanup_failed":
        lines.extend(
            [
                f"Launchplane preview cleanup failed for PR #{anchor_pr_number}.",
                "",
            ]
        )
    elif status == "unsupported":
        lines.extend(
            [
                f"Launchplane preview automation is unavailable for PR #{anchor_pr_number}.",
                "",
            ]
        )
    elif status == "cleared":
        lines.extend(
            [
                f"Launchplane cleared preview feedback for PR #{anchor_pr_number}.",
                "",
            ]
        )
    else:
        lines.extend(
            [
                f"Launchplane preview refresh failed for PR #{anchor_pr_number}.",
                "",
            ]
        )
    if preview_url:
        lines.append(f"- Preview URL: {preview_url}")
    if immutable_image_reference:
        lines.append(f"- Immutable image: `{immutable_image_reference}`")
    if refresh_image_reference:
        lines.append(f"- Refresh tag: `{refresh_image_reference}`")
    if revision:
        lines.append(f"- Revision: `{revision}`")
    if run_url:
        lines.append(f"- Workflow run: {run_url}")
    if failure_summary:
        lines.append(f"- Failure summary: {failure_summary}")

    if status == "ready":
        lines.extend(
            [
                "",
                "The preview passed the remote creator/public verification gate.",
                "",
                "Controls:",
                "- Push new commits while the `preview` label stays applied to refresh this preview.",
                "- Remove the `preview` label or close the PR to destroy it.",
                "- Preview inventory, lifecycle plans, and cleanup evidence are recorded in Launchplane.",
            ]
        )
    elif status == "destroyed":
        lines.extend(
            [
                "",
                "Launchplane recorded the cleanup result for this preview lifecycle.",
            ]
        )
    elif status == "cleanup_failed":
        lines.extend(
            [
                "",
                "The preview may still exist. Check the Launchplane cleanup record before retrying.",
            ]
        )
    elif status == "unsupported":
        lines.extend(
            [
                "",
                "No preview environment was requested because this pull request cannot use the protected preview provisioning path.",
            ]
        )
    elif status == "cleared":
        lines.extend(
            [
                "",
                "Launchplane keeps this record as evidence of the cleared PR feedback request.",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "No fresh preview link is being advertised from this run. Treat any older preview as stale until a later refresh succeeds.",
            ]
        )
    return "\n".join(line for line in lines if line is not None)


def build_preview_pr_feedback_record(
    *,
    control_plane_root: Path,
    product: str,
    context: str,
    source: str,
    requested_at: str,
    repository: str,
    anchor_repo: str,
    anchor_pr_number: int,
    anchor_pr_url: str,
    status: PreviewPrFeedbackStatus,
    marker: str = DEFAULT_PREVIEW_FEEDBACK_MARKER,
    preview_url: str = "",
    immutable_image_reference: str = "",
    refresh_image_reference: str = "",
    revision: str = "",
    run_url: str = "",
    failure_summary: str = "",
) -> PreviewPrFeedbackRecord:
    comment_markdown = _render_preview_pr_feedback_markdown(
        marker=marker,
        status=status,
        anchor_pr_number=anchor_pr_number,
        preview_url=preview_url.strip(),
        immutable_image_reference=immutable_image_reference.strip(),
        refresh_image_reference=refresh_image_reference.strip(),
        revision=revision.strip(),
        run_url=run_url.strip(),
        failure_summary=failure_summary.strip(),
    )
    delivery_status = "skipped"
    delivery_action = ""
    comment_id = 0
    comment_url = ""
    error_message = ""
    github_reference = github_pull_request_reference(pr_url=anchor_pr_url)
    github_token = resolve_launchplane_github_token(
        control_plane_root=control_plane_root,
        context_name=context,
    )
    if github_reference is None:
        error_message = "anchor_pr_url must be a GitHub pull request URL"
    elif not github_token:
        error_message = "Launchplane runtime records do not expose GITHUB_TOKEN for this context"
    else:
        try:
            existing_comment = find_github_issue_comment_by_marker(
                owner=github_reference["owner"],
                repo=github_reference["repo"],
                issue_number=github_reference["pr_number"],
                token=github_token,
                marker=marker,
            )
            if existing_comment is not None:
                existing_comment_id = existing_comment.get("id")
                if not isinstance(existing_comment_id, int):
                    raise click.ClickException("Existing preview feedback comment is missing a numeric id.")
                if status == "cleared":
                    delete_github_issue_comment(
                        owner=github_reference["owner"],
                        repo=github_reference["repo"],
                        comment_id=existing_comment_id,
                        token=github_token,
                    )
                    delivery_status = "delivered"
                    delivery_action = "deleted_comment"
                    comment_id = existing_comment_id
                else:
                    updated_comment = update_github_issue_comment(
                        owner=github_reference["owner"],
                        repo=github_reference["repo"],
                        comment_id=existing_comment_id,
                        token=github_token,
                        body=comment_markdown,
                    )
                    delivery_status = "delivered"
                    delivery_action = "updated_comment"
                    comment_id = existing_comment_id
                    comment_url = _comment_url(updated_comment)
            elif status == "cleared":
                delivery_action = "no_existing_comment"
            else:
                created_comment = create_github_issue_comment(
                    owner=github_reference["owner"],
                    repo=github_reference["repo"],
                    issue_number=github_reference["pr_number"],
                    token=github_token,
                    body=comment_markdown,
                )
                created_comment_id = created_comment.get("id")
                delivery_status = "delivered"
                delivery_action = "created_comment"
                comment_id = created_comment_id if isinstance(created_comment_id, int) else 0
                comment_url = _comment_url(created_comment)
        except click.ClickException as exc:
            delivery_status = "failed"
            error_message = str(exc)

    return PreviewPrFeedbackRecord(
        feedback_id=build_preview_pr_feedback_id(
            context_name=context,
            anchor_pr_number=anchor_pr_number,
            requested_at=requested_at,
        ),
        product=product,
        context=context,
        source=source,
        requested_at=requested_at,
        repository=repository,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
        anchor_pr_url=anchor_pr_url,
        status=status,
        marker=marker,
        comment_markdown=comment_markdown,
        preview_url=preview_url,
        immutable_image_reference=immutable_image_reference,
        refresh_image_reference=refresh_image_reference,
        revision=revision,
        run_url=run_url,
        failure_summary=failure_summary,
        delivery_status=delivery_status,
        delivery_action=delivery_action,
        comment_id=comment_id,
        comment_url=comment_url,
        error_message=error_message,
    )
