from pathlib import Path
from typing import Protocol

import click

from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.preview_pr_feedback_record import (
    PreviewPrFeedbackDeliveryStatus,
    PreviewPrFeedbackRecord,
    PreviewPrFeedbackStatus,
    build_preview_pr_feedback_id,
)
from control_plane.every_code_worker import every_code_worktree_branch
from control_plane.workflows.launchplane import (
    create_github_issue_comment,
    delete_github_issue_comment,
    find_github_issue_comment_by_marker,
    github_api_request,
    github_pull_request_reference,
    resolve_launchplane_github_token,
    update_github_issue_comment,
)

DEFAULT_PREVIEW_FEEDBACK_MARKER = "<!-- verireel-preview-control -->"
DEFAULT_EVERY_CODE_PREVIEW_READY_MARKER_PREFIX = "<!-- launchplane-every-code-preview-ready"


class EveryCodeWorkRequestReadStore(Protocol):
    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...


def _comment_url(payload: dict[str, object]) -> str:
    html_url = payload.get("html_url")
    return html_url if isinstance(html_url, str) else ""


def _find_every_code_work_request_for_preview(
    *,
    record_store: EveryCodeWorkRequestReadStore | None,
    repository: str,
    anchor_pr_url: str,
    anchor_pr_head_branch: str = "",
) -> EveryCodeWorkRequestRecord | None:
    if record_store is None:
        return None
    normalized_repository = repository.strip()
    normalized_pr_url = anchor_pr_url.strip()
    if not normalized_repository or not normalized_pr_url:
        return None

    records: list[EveryCodeWorkRequestRecord] = []
    for state in ("running", "done"):
        records.extend(
            record_store.list_every_code_work_request_records(
                state=state,
                repository=normalized_repository,
                limit=100,
            )
        )
    for record in records:
        if record.result_pr_url.strip() == normalized_pr_url:
            return record

    normalized_head_branch = anchor_pr_head_branch.strip()
    if not normalized_head_branch:
        return None
    for record in records:
        if record.repository.strip().casefold() != normalized_repository.casefold():
            continue
        expected_branch = every_code_worktree_branch(record)
        if expected_branch == normalized_head_branch:
            return record
    return None


def _every_code_preview_ready_marker(*, repository: str, pr_number: int) -> str:
    return f"{DEFAULT_EVERY_CODE_PREVIEW_READY_MARKER_PREFIX}:{repository}#{pr_number} -->"


def _render_every_code_preview_ready_issue_comment(
    *,
    marker: str,
    issue_author: str,
    pr_number: int,
    anchor_pr_url: str,
    preview_url: str,
    reviewer_action: str,
) -> str:
    lines = [
        marker,
        f"@{issue_author} your Every Code preview is ready for PR #{pr_number}.",
        "",
        f"- Preview URL: {preview_url}",
        f"- Pull request: {anchor_pr_url}",
    ]
    if reviewer_action == "requested":
        lines.append("- Review request: requested you as a reviewer on the pull request.")
    elif reviewer_action == "already_requested":
        lines.append("- Review request: you were already requested as a reviewer.")
    elif reviewer_action == "skipped_pr_author":
        lines.append("- Review request: skipped because you opened the pull request.")
    elif reviewer_action.startswith("failed"):
        lines.append(
            "- Review request: GitHub did not accept the automatic reviewer request; "
            "this comment is the fallback notification."
        )
    return "\n".join(lines)


def _github_issue_author_login(
    *, owner: str, repo: str, issue_number: int, token: str
) -> str:
    payload = github_api_request(
        path=f"/repos/{owner}/{repo}/issues/{issue_number}",
        token=token,
    )
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"GitHub issue response for {owner}/{repo}#{issue_number} must be an object."
        )
    user = payload.get("user")
    if not isinstance(user, dict):
        return ""
    login = user.get("login")
    return login.strip() if isinstance(login, str) else ""


def _github_pull_request_author_and_head_branch(
    *, owner: str, repo: str, pr_number: int, token: str
) -> tuple[str, str]:
    payload = github_api_request(
        path=f"/repos/{owner}/{repo}/pulls/{pr_number}",
        token=token,
    )
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"GitHub pull request response for {owner}/{repo}#{pr_number} must be an object."
        )
    user = payload.get("user")
    pr_author = ""
    if isinstance(user, dict):
        login = user.get("login")
        pr_author = login.strip() if isinstance(login, str) else ""
    head = payload.get("head")
    if not isinstance(head, dict):
        return pr_author, ""
    reference = head.get("ref")
    return pr_author, reference.strip() if isinstance(reference, str) else ""


def _github_requested_reviewer_logins(
    *, owner: str, repo: str, pr_number: int, token: str
) -> frozenset[str]:
    payload = github_api_request(
        path=f"/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers",
        token=token,
    )
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"GitHub requested reviewers response for {owner}/{repo}#{pr_number} must be an object."
        )
    users = payload.get("users")
    if not isinstance(users, list):
        return frozenset()
    logins: set[str] = set()
    for item in users:
        if not isinstance(item, dict):
            continue
        login = item.get("login")
        if isinstance(login, str) and login.strip():
            logins.add(login.strip().casefold())
    return frozenset(logins)


def _request_github_pull_request_reviewer(
    *, owner: str, repo: str, pr_number: int, reviewer: str, token: str
) -> None:
    payload = github_api_request(
        path=f"/repos/{owner}/{repo}/pulls/{pr_number}/requested_reviewers",
        token=token,
        method="POST",
        body={"reviewers": [reviewer]},
    )
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"GitHub reviewer request response for {owner}/{repo}#{pr_number} must be an object."
        )


def _notify_every_code_preview_ready_source_issue(
    *,
    record_store: EveryCodeWorkRequestReadStore | None,
    owner: str,
    repo: str,
    pr_number: int,
    anchor_pr_url: str,
    repository: str,
    preview_url: str,
    token: str,
) -> str:
    if not preview_url.strip():
        return "skipped_no_preview_url"
    pr_author, pr_head_branch = _github_pull_request_author_and_head_branch(
        owner=owner,
        repo=repo,
        pr_number=pr_number,
        token=token,
    )
    record = _find_every_code_work_request_for_preview(
        record_store=record_store,
        repository=repository,
        anchor_pr_url=anchor_pr_url,
        anchor_pr_head_branch=pr_head_branch,
    )
    if record is None:
        return "skipped_no_every_code_request"

    issue_author = _github_issue_author_login(
        owner=owner,
        repo=repo,
        issue_number=record.issue_number,
        token=token,
    )
    if not issue_author:
        return "skipped_no_issue_author"

    reviewer_action = "requested"
    if pr_author.casefold() == issue_author.casefold():
        reviewer_action = "skipped_pr_author"
    else:
        requested_reviewers = _github_requested_reviewer_logins(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
            token=token,
        )
        if issue_author.casefold() in requested_reviewers:
            reviewer_action = "already_requested"
        else:
            try:
                _request_github_pull_request_reviewer(
                    owner=owner,
                    repo=repo,
                    pr_number=pr_number,
                    reviewer=issue_author,
                    token=token,
                )
            except click.ClickException:
                reviewer_action = "failed"

    marker = _every_code_preview_ready_marker(repository=repository, pr_number=pr_number)
    comment_markdown = _render_every_code_preview_ready_issue_comment(
        marker=marker,
        issue_author=issue_author,
        pr_number=pr_number,
        anchor_pr_url=anchor_pr_url,
        preview_url=preview_url.strip(),
        reviewer_action=reviewer_action,
    )
    existing_comment = find_github_issue_comment_by_marker(
        owner=owner,
        repo=repo,
        issue_number=record.issue_number,
        token=token,
        marker=marker,
    )
    if existing_comment is not None:
        existing_comment_id = existing_comment.get("id")
        if not isinstance(existing_comment_id, int):
            raise click.ClickException(
                "Existing Every Code preview ready comment is missing a numeric id."
            )
        update_github_issue_comment(
            owner=owner,
            repo=repo,
            comment_id=existing_comment_id,
            token=token,
            body=comment_markdown,
        )
        return f"updated_source_issue_comment:{reviewer_action}"
    create_github_issue_comment(
        owner=owner,
        repo=repo,
        issue_number=record.issue_number,
        token=token,
        body=comment_markdown,
    )
    return f"created_source_issue_comment:{reviewer_action}"


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
    if status == "pending":
        lines.extend(
            [
                f"Launchplane preview is waiting for PR #{anchor_pr_number}.",
                "",
            ]
        )
    elif status == "ready":
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

    if status == "pending":
        lines.extend(
            [
                "",
                "Preview prerequisites are still in flight. Launchplane will replace this note "
                "once the preview is ready or an actual preview failure is known.",
            ]
        )
    elif status == "ready":
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
    every_code_record_store: EveryCodeWorkRequestReadStore | None = None,
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
    delivery_status: PreviewPrFeedbackDeliveryStatus = "skipped"
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
                    raise click.ClickException(
                        "Existing preview feedback comment is missing a numeric id."
                    )
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
            if status == "ready" and preview_url.strip():
                source_issue_action = _notify_every_code_preview_ready_source_issue(
                    record_store=every_code_record_store,
                    owner=github_reference["owner"],
                    repo=github_reference["repo"],
                    pr_number=github_reference["pr_number"],
                    anchor_pr_url=anchor_pr_url,
                    repository=repository,
                    preview_url=preview_url,
                    token=github_token,
                )
                if not delivery_action:
                    delivery_action = source_issue_action
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
