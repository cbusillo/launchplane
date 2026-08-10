import hashlib
import json
from pathlib import Path
import re
from typing import Protocol
from urllib.parse import quote_plus

import click

from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.contracts.every_code_pr_feedback_record import (
    EveryCodePrFeedbackRecord,
    build_every_code_pr_feedback_id,
)
from control_plane.contracts.preview_pr_feedback_record import (
    PreviewPrFeedbackDeliveryStatus,
    PreviewPrFeedbackRecord,
    PreviewPrFeedbackStatus,
    build_preview_pr_feedback_id,
)
from control_plane.contracts.preview_pr_feedback_remediation import (
    PreviewPrFeedbackRemediationPlan,
    PreviewPrFeedbackTerminalStatus,
)
from control_plane.contracts.preview_record import PreviewRecord
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

DEFAULT_PREVIEW_FEEDBACK_MARKER = "<!-- launchplane-preview-control -->"
LEGACY_PREVIEW_FEEDBACK_MARKERS = ("<!-- verireel-preview-control -->",)
DEFAULT_EVERY_CODE_PREVIEW_READY_MARKER_PREFIX = "<!-- launchplane-every-code-preview-ready"
DEFAULT_EVERY_CODE_READY_TO_MERGE_MARKER_PREFIX = "<!-- launchplane-every-code-ready-to-merge"
EVERY_CODE_PREVIEW_READY_LABEL = "preview-ready"
EVERY_CODE_PREVIEW_APPROVED_LABEL = "preview-approved"
EVERY_CODE_PREVIEW_CHANGES_REQUESTED_LABEL = "preview-changes-requested"
EVERY_CODE_READY_TO_MERGE_LABEL = "ready-to-merge"
_PREVIEW_COMMAND_PATTERN = re.compile(
    r"^\s*/preview\s+(?P<command>ok|approve|changes)\b(?P<body>.*)$",
    re.IGNORECASE | re.DOTALL,
)


class EveryCodePreviewValidationResult(dict[str, object]):
    pass


class EveryCodeWorkRequestReadStore(Protocol):
    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...


class EveryCodePreviewValidationStore(EveryCodeWorkRequestReadStore, Protocol):
    def write_every_code_pr_feedback_record(self, record: EveryCodePrFeedbackRecord) -> object: ...

    def list_every_code_pr_feedback_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePrFeedbackRecord, ...]: ...


class PreviewPrFeedbackPreviewReadStore(Protocol):
    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]: ...


class PreviewPrFeedbackRemediationStore(Protocol):
    def list_preview_pr_feedback_records(
        self,
        *,
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackRecord, ...]: ...


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


def _preview_url_from_latest_record(
    *,
    record_store: PreviewPrFeedbackPreviewReadStore | None,
    context: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> str:
    if record_store is None:
        return ""
    for preview in record_store.list_preview_records(
        context_name=context,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
        limit=5,
    ):
        if preview.state != "active":
            continue
        preview_url = preview.canonical_url.strip()
        if preview_url:
            return preview_url
    return ""


def _every_code_preview_ready_marker(*, repository: str, pr_number: int) -> str:
    return f"{DEFAULT_EVERY_CODE_PREVIEW_READY_MARKER_PREFIX}:{repository}#{pr_number} -->"


def _every_code_ready_to_merge_marker(*, repository: str, pr_number: int) -> str:
    return f"{DEFAULT_EVERY_CODE_READY_TO_MERGE_MARKER_PREFIX}:{repository}#{pr_number} -->"


def _github_search_url(query: str) -> str:
    return f"https://github.com/issues?q={quote_plus(query)}"


def _github_pulls_url(query: str) -> str:
    return f"https://github.com/pulls?q={quote_plus(query)}"


def _preview_validation_queue_url(*, issue_author: str) -> str:
    return _github_search_url(
        f"is:open is:issue author:{issue_author} label:{EVERY_CODE_PREVIEW_READY_LABEL}"
    )


def _merge_queue_url(*, merge_owner: str) -> str:
    return _github_pulls_url(
        f"is:open is:pr assignee:{merge_owner} label:{EVERY_CODE_READY_TO_MERGE_LABEL}"
    )


def _render_preview_checklist(record: EveryCodeWorkRequestRecord) -> list[str]:
    title = record.issue_title.strip()
    checklist = []
    if title:
        checklist.append(f"Confirm the preview resolves: {title}")
    else:
        checklist.append("Confirm the preview resolves the issue you opened.")
    checklist.extend(
        [
            "Try the affected workflow in the preview, not only the page load.",
            "Look for obvious regressions around the changed area.",
        ]
    )
    return checklist


def _render_every_code_preview_ready_issue_comment(
    *,
    marker: str,
    issue_author: str,
    record: EveryCodeWorkRequestRecord,
    pr_number: int,
    anchor_pr_url: str,
    preview_url: str,
    merge_owner: str,
) -> str:
    lines = [
        marker,
        f"@{issue_author} your Every Code preview is ready.",
        "",
        f"- Preview URL: {preview_url}",
        f"- Pull request: {anchor_pr_url}",
        "",
        "Please check:",
    ]
    lines.extend(f"- {item}" for item in _render_preview_checklist(record))
    lines.extend(
        [
            "",
            "When it looks right, comment `/preview ok` on this issue.",
            "If it needs changes, comment `/preview changes <what needs to change>`.",
            "",
            f"Your preview queue: {_preview_validation_queue_url(issue_author=issue_author)}",
        ]
    )
    if merge_owner:
        lines.append(f"Merge queue: {_merge_queue_url(merge_owner=merge_owner)}")
    return "\n".join(lines)


def _render_every_code_ready_to_merge_pr_comment(
    *,
    marker: str,
    merge_owner: str,
    issue_author: str,
    issue_url: str,
    preview_url: str,
) -> str:
    mention = f"@{merge_owner} " if merge_owner else ""
    lines = [
        marker,
        f"{mention}the issue author approved the preview.",
        "",
        f"- Approved by: @{issue_author}",
        f"- Source issue: {issue_url}",
    ]
    if preview_url.strip():
        lines.append(f"- Preview URL: {preview_url.strip()}")
    if merge_owner:
        lines.extend(
            [
                "",
                f"Merge queue: {_merge_queue_url(merge_owner=merge_owner)}",
            ]
        )
    return "\n".join(lines)


def _github_issue_author_login(*, owner: str, repo: str, issue_number: int, token: str) -> str:
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


def _github_repository_user_owner_login(*, owner: str, repo: str, token: str) -> str:
    payload = github_api_request(
        path=f"/repos/{owner}/{repo}",
        token=token,
    )
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"GitHub repository response for {owner}/{repo} must be an object."
        )
    owner_payload = payload.get("owner")
    if not isinstance(owner_payload, dict):
        return ""
    owner_type = owner_payload.get("type")
    login = owner_payload.get("login")
    if owner_type != "User" or not isinstance(login, str):
        return ""
    return login.strip()


def _github_add_labels(
    *, owner: str, repo: str, issue_number: int, labels: list[str], token: str
) -> None:
    if not labels:
        return
    payload = github_api_request(
        path=f"/repos/{owner}/{repo}/issues/{issue_number}/labels",
        token=token,
        method="POST",
        body={"labels": labels},
    )
    if not isinstance(payload, list):
        raise click.ClickException(
            f"GitHub label response for {owner}/{repo}#{issue_number} must be a list."
        )


def _github_remove_label(
    *, owner: str, repo: str, issue_number: int, label: str, token: str
) -> None:
    if not label.strip():
        return
    try:
        github_api_request(
            path=f"/repos/{owner}/{repo}/issues/{issue_number}/labels/{label}",
            token=token,
            method="DELETE",
        )
    except click.ClickException:
        return


def _github_assign_user(
    *, owner: str, repo: str, issue_number: int, assignee: str, token: str
) -> None:
    if not assignee.strip():
        return
    payload = github_api_request(
        path=f"/repos/{owner}/{repo}/issues/{issue_number}/assignees",
        token=token,
        method="POST",
        body={"assignees": [assignee]},
    )
    if not isinstance(payload, dict):
        raise click.ClickException(
            f"GitHub assignee response for {owner}/{repo}#{issue_number} must be an object."
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
    _pr_author, pr_head_branch = _github_pull_request_author_and_head_branch(
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

    merge_owner = _github_repository_user_owner_login(owner=owner, repo=repo, token=token)
    _github_add_labels(
        owner=owner,
        repo=repo,
        issue_number=record.issue_number,
        labels=[EVERY_CODE_PREVIEW_READY_LABEL],
        token=token,
    )
    _github_remove_label(
        owner=owner,
        repo=repo,
        issue_number=record.issue_number,
        label=EVERY_CODE_PREVIEW_APPROVED_LABEL,
        token=token,
    )
    _github_remove_label(
        owner=owner,
        repo=repo,
        issue_number=record.issue_number,
        label=EVERY_CODE_PREVIEW_CHANGES_REQUESTED_LABEL,
        token=token,
    )

    marker = _every_code_preview_ready_marker(repository=repository, pr_number=pr_number)
    comment_markdown = _render_every_code_preview_ready_issue_comment(
        marker=marker,
        issue_author=issue_author,
        record=record,
        pr_number=pr_number,
        anchor_pr_url=anchor_pr_url,
        preview_url=preview_url.strip(),
        merge_owner=merge_owner,
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
        return "updated_source_issue_comment"
    create_github_issue_comment(
        owner=owner,
        repo=repo,
        issue_number=record.issue_number,
        token=token,
        body=comment_markdown,
    )
    return "created_source_issue_comment"


def _parse_preview_validation_command(body: str) -> tuple[str, str] | None:
    match = _PREVIEW_COMMAND_PATTERN.match(body)
    if match is None:
        return None
    command = match.group("command").strip().casefold()
    details = match.group("body").strip()
    if command == "approve":
        command = "ok"
    return command, details


def _find_every_code_work_request_for_source_issue(
    *,
    record_store: EveryCodeWorkRequestReadStore,
    repository: str,
    issue_number: int,
) -> EveryCodeWorkRequestRecord | None:
    for state in ("running", "done"):
        for record in record_store.list_every_code_work_request_records(
            state=state,
            repository=repository,
            limit=100,
        ):
            if record.issue_number == issue_number and record.result_pr_url.strip():
                return record
    return None


def _preview_command_feedback_body(*, details: str, issue_url: str) -> str:
    return "\n".join(
        [
            "The source issue author requested preview changes.",
            "",
            details.strip(),
            "",
            f"Source issue: {issue_url}",
        ]
    ).strip()


def handle_every_code_preview_validation_comment(
    *,
    record_store: EveryCodePreviewValidationStore,
    owner: str,
    repo: str,
    issue_number: int,
    issue_url: str,
    issue_author: str,
    actor: str,
    comment_body: str,
    comment_id: str,
    comment_node_id: str,
    comment_url: str,
    delivery_id: str,
    token: str,
    received_at: str,
) -> EveryCodePreviewValidationResult:
    parsed_command = _parse_preview_validation_command(comment_body)
    if parsed_command is None:
        return EveryCodePreviewValidationResult(handled=False, reason="not_preview_command")
    command, details = parsed_command
    if actor.casefold() != issue_author.casefold():
        return EveryCodePreviewValidationResult(
            handled=True,
            skipped=True,
            reason="actor_not_issue_author",
            command=command,
        )
    repository = f"{owner}/{repo}"
    record = _find_every_code_work_request_for_source_issue(
        record_store=record_store,
        repository=repository,
        issue_number=issue_number,
    )
    if record is None:
        return EveryCodePreviewValidationResult(
            handled=True,
            skipped=True,
            reason="linked_every_code_request_not_found",
            command=command,
        )
    pr_reference = github_pull_request_reference(record.result_pr_url)
    if pr_reference is None:
        return EveryCodePreviewValidationResult(
            handled=True,
            skipped=True,
            reason="linked_every_code_pull_request_invalid",
            command=command,
            request_id=record.request_id,
        )
    pr_number = pr_reference["pr_number"]
    pr_url = record.result_pr_url.strip()

    if command == "changes":
        if not details.strip():
            return EveryCodePreviewValidationResult(
                handled=True,
                skipped=True,
                reason="changes_details_required",
                command=command,
                request_id=record.request_id,
            )
        _github_add_labels(
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            labels=[EVERY_CODE_PREVIEW_CHANGES_REQUESTED_LABEL],
            token=token,
        )
        _github_remove_label(
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            label=EVERY_CODE_PREVIEW_READY_LABEL,
            token=token,
        )
        _github_remove_label(
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            label=EVERY_CODE_PREVIEW_APPROVED_LABEL,
            token=token,
        )
        _github_remove_label(
            owner=owner,
            repo=repo,
            issue_number=pr_number,
            label=EVERY_CODE_READY_TO_MERGE_LABEL,
            token=token,
        )
        feedback_id = build_every_code_pr_feedback_id(
            repository=repository,
            pr_number=pr_number,
            github_delivery_id=delivery_id,
            github_node_id=comment_node_id,
            github_id=comment_id,
        )
        for existing_feedback in record_store.list_every_code_pr_feedback_records(
            request_id=record.request_id,
            repository=repository,
            pr_number=pr_number,
            limit=100,
        ):
            if existing_feedback.feedback_id == feedback_id:
                return EveryCodePreviewValidationResult(
                    handled=True,
                    deduped=True,
                    command=command,
                    request_id=record.request_id,
                    feedback_id=existing_feedback.feedback_id,
                )
        feedback_record = EveryCodePrFeedbackRecord(
            feedback_id=feedback_id,
            request_id=record.request_id,
            repository=repository,
            pr_number=pr_number,
            pr_url=pr_url,
            feedback_kind="issue_comment",
            github_delivery_id=delivery_id,
            github_node_id=comment_node_id,
            github_id=comment_id,
            actor=actor,
            author_association="SOURCE_ISSUE_AUTHOR",
            body=_preview_command_feedback_body(details=details, issue_url=issue_url),
            html_url=comment_url,
            received_at=received_at,
        )
        record_store.write_every_code_pr_feedback_record(feedback_record)
        return EveryCodePreviewValidationResult(
            handled=True,
            command=command,
            request_id=record.request_id,
            feedback_id=feedback_record.feedback_id,
            status=feedback_record.status,
        )

    _github_add_labels(
        owner=owner,
        repo=repo,
        issue_number=issue_number,
        labels=[EVERY_CODE_PREVIEW_APPROVED_LABEL],
        token=token,
    )
    _github_remove_label(
        owner=owner,
        repo=repo,
        issue_number=issue_number,
        label=EVERY_CODE_PREVIEW_READY_LABEL,
        token=token,
    )
    _github_remove_label(
        owner=owner,
        repo=repo,
        issue_number=issue_number,
        label=EVERY_CODE_PREVIEW_CHANGES_REQUESTED_LABEL,
        token=token,
    )
    _github_add_labels(
        owner=owner,
        repo=repo,
        issue_number=pr_number,
        labels=[EVERY_CODE_READY_TO_MERGE_LABEL],
        token=token,
    )
    merge_owner = _github_repository_user_owner_login(owner=owner, repo=repo, token=token)
    if merge_owner:
        _github_assign_user(
            owner=owner,
            repo=repo,
            issue_number=pr_number,
            assignee=merge_owner,
            token=token,
        )
    marker = _every_code_ready_to_merge_marker(repository=repository, pr_number=pr_number)
    comment_markdown = _render_every_code_ready_to_merge_pr_comment(
        marker=marker,
        merge_owner=merge_owner,
        issue_author=issue_author,
        issue_url=issue_url,
        preview_url="",
    )
    existing_comment = find_github_issue_comment_by_marker(
        owner=owner,
        repo=repo,
        issue_number=pr_number,
        token=token,
        marker=marker,
    )
    if existing_comment is not None:
        existing_comment_id = existing_comment.get("id")
        if isinstance(existing_comment_id, int):
            update_github_issue_comment(
                owner=owner,
                repo=repo,
                comment_id=existing_comment_id,
                token=token,
                body=comment_markdown,
            )
    else:
        create_github_issue_comment(
            owner=owner,
            repo=repo,
            issue_number=pr_number,
            token=token,
            body=comment_markdown,
        )
    return EveryCodePreviewValidationResult(
        handled=True,
        command=command,
        request_id=record.request_id,
        pr_number=pr_number,
        merge_owner=merge_owner,
    )


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
        has_resource_evidence = bool(
            preview_url or immutable_image_reference or refresh_image_reference
        )
        lines.extend(
            [
                "",
                (
                    "The preview may still exist. Check the Launchplane cleanup record before retrying."
                    if has_resource_evidence
                    else "Launchplane could not confirm cleanup for this preview lifecycle. Check the workflow run and cleanup record before retrying."
                ),
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


def _find_preview_pr_feedback_comment(
    *,
    owner: str,
    repo: str,
    issue_number: int,
    token: str,
    marker: str,
) -> dict[str, object] | None:
    existing_comment = find_github_issue_comment_by_marker(
        owner=owner,
        repo=repo,
        issue_number=issue_number,
        token=token,
        marker=marker,
    )
    if existing_comment is not None:
        return existing_comment
    if marker != DEFAULT_PREVIEW_FEEDBACK_MARKER:
        return None
    for legacy_marker in LEGACY_PREVIEW_FEEDBACK_MARKERS:
        if legacy_marker == marker:
            continue
        existing_comment = find_github_issue_comment_by_marker(
            owner=owner,
            repo=repo,
            issue_number=issue_number,
            token=token,
            marker=legacy_marker,
        )
        if existing_comment is not None:
            return existing_comment
    return None


def _preview_pr_feedback_comment_body_sha256(comment: dict[str, object]) -> str:
    body = comment.get("body")
    if not isinstance(body, str) or not body.strip():
        raise click.ClickException("Managed preview feedback comment has no body.")
    return hashlib.sha256(body.encode("utf-8")).hexdigest()


def _preview_pr_feedback_remediation_plan_sha256(payload: dict[str, object]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def build_preview_pr_feedback_remediation_plan(
    *,
    control_plane_root: Path,
    record_store: PreviewPrFeedbackRemediationStore,
    product: str,
    context: str,
    repository: str,
    anchor_pr_number: int,
    desired_status: PreviewPrFeedbackTerminalStatus,
    reason: str,
    issue_reference: str,
) -> PreviewPrFeedbackRemediationPlan:
    normalized_repository = repository.strip()
    repository_parts = normalized_repository.split("/", maxsplit=1)
    if len(repository_parts) != 2 or not all(repository_parts):
        raise click.ClickException("Preview feedback remediation repository must use owner/name.")
    owner, repo = repository_parts
    anchor_pr_url = f"https://github.com/{normalized_repository}/pull/{anchor_pr_number}"
    current_feedback = next(
        (
            record
            for record in record_store.list_preview_pr_feedback_records(
                context_name=context,
                limit=None,
            )
            if record.product == product
            and record.repository.casefold() == normalized_repository.casefold()
            and record.anchor_repo.casefold() == repo.casefold()
            and record.anchor_pr_number == anchor_pr_number
            and record.anchor_pr_url == anchor_pr_url
            and record.marker == DEFAULT_PREVIEW_FEEDBACK_MARKER
            and record.delivery_status == "delivered"
            and record.delivery_action in {"created_comment", "updated_comment"}
            and record.comment_id > 0
            and bool(record.comment_url.strip())
        ),
        None,
    )
    if current_feedback is None:
        raise click.ClickException(
            "Launchplane has no delivered managed preview feedback ownership record for this PR."
        )

    github_token = resolve_launchplane_github_token(
        control_plane_root=control_plane_root,
        context_name=context,
    )
    if not github_token:
        raise click.ClickException(
            "Launchplane runtime records do not expose GITHUB_TOKEN for this context."
        )
    current_comment = _find_preview_pr_feedback_comment(
        owner=owner,
        repo=repo,
        issue_number=anchor_pr_number,
        token=github_token,
        marker=DEFAULT_PREVIEW_FEEDBACK_MARKER,
    )
    if current_comment is None:
        raise click.ClickException("The managed preview feedback comment no longer exists.")
    current_comment_id = current_comment.get("id")
    if not isinstance(current_comment_id, int) or current_comment_id != current_feedback.comment_id:
        raise click.ClickException(
            "The current managed preview feedback comment does not match Launchplane ownership evidence."
        )
    current_comment_url = _comment_url(current_comment)
    if not current_comment_url:
        raise click.ClickException("The managed preview feedback comment has no GitHub URL.")
    if current_feedback.comment_url != current_comment_url:
        raise click.ClickException(
            "The current managed preview feedback comment URL does not match Launchplane evidence."
        )
    current_comment_body_sha256 = _preview_pr_feedback_comment_body_sha256(current_comment)
    recorded_comment_body_sha256 = hashlib.sha256(
        current_feedback.comment_markdown.encode("utf-8")
    ).hexdigest()
    if current_comment_body_sha256 != recorded_comment_body_sha256:
        raise click.ClickException(
            "The current managed preview feedback comment body does not match Launchplane evidence."
        )

    plan_payload: dict[str, object] = {
        "schema_version": 1,
        "product": product,
        "context": context,
        "repository": normalized_repository,
        "anchor_pr_number": anchor_pr_number,
        "anchor_pr_url": anchor_pr_url,
        "desired_status": desired_status,
        "reason": reason.strip(),
        "issue_reference": issue_reference.strip(),
        "current_feedback_id": current_feedback.feedback_id,
        "current_feedback_status": current_feedback.status,
        "current_comment_id": current_comment_id,
        "current_comment_url": current_comment_url,
        "current_comment_body_sha256": current_comment_body_sha256,
        "planned_delivery_action": (
            "delete_comment" if desired_status == "cleared" else "update_comment"
        ),
    }
    return PreviewPrFeedbackRemediationPlan(
        product=product,
        context=context,
        repository=normalized_repository,
        anchor_pr_number=anchor_pr_number,
        anchor_pr_url=anchor_pr_url,
        desired_status=desired_status,
        reason=reason.strip(),
        issue_reference=issue_reference.strip(),
        current_feedback_id=current_feedback.feedback_id,
        current_feedback_status=current_feedback.status,
        current_comment_id=current_comment_id,
        current_comment_url=current_comment_url,
        current_comment_body_sha256=str(plan_payload["current_comment_body_sha256"]),
        planned_delivery_action=(
            "delete_comment" if desired_status == "cleared" else "update_comment"
        ),
        plan_sha256=_preview_pr_feedback_remediation_plan_sha256(plan_payload),
    )


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
    expected_existing_comment_id: int = 0,
    expected_existing_comment_body_sha256: str = "",
    remediates_feedback_id: str = "",
    remediation_reason: str = "",
    remediation_issue_reference: str = "",
    remediation_plan_sha256: str = "",
    remediation_actor: str = "",
    feedback_id: str = "",
    every_code_record_store: EveryCodeWorkRequestReadStore | None = None,
    preview_record_store: PreviewPrFeedbackPreviewReadStore | None = None,
) -> PreviewPrFeedbackRecord:
    resolved_preview_url = preview_url.strip()
    if status in {"ready", "cleanup_failed"} and not resolved_preview_url:
        resolved_preview_url = _preview_url_from_latest_record(
            record_store=preview_record_store,
            context=context,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
        )
    if status == "ready" and not resolved_preview_url:
        raise click.ClickException(
            "Ready preview feedback requires an explicit preview URL or an active "
            "Launchplane preview record."
        )
    comment_markdown = _render_preview_pr_feedback_markdown(
        marker=marker,
        status=status,
        anchor_pr_number=anchor_pr_number,
        preview_url=resolved_preview_url,
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
            existing_comment = _find_preview_pr_feedback_comment(
                owner=github_reference["owner"],
                repo=github_reference["repo"],
                issue_number=github_reference["pr_number"],
                token=github_token,
                marker=marker,
            )
            if expected_existing_comment_id:
                if existing_comment is None:
                    raise click.ClickException(
                        "Expected managed preview feedback comment no longer exists."
                    )
                existing_comment_id = existing_comment.get("id")
                if existing_comment_id != expected_existing_comment_id:
                    raise click.ClickException(
                        "Managed preview feedback comment changed after remediation planning."
                    )
                if expected_existing_comment_body_sha256 and (
                    _preview_pr_feedback_comment_body_sha256(existing_comment)
                    != expected_existing_comment_body_sha256
                ):
                    raise click.ClickException(
                        "Managed preview feedback comment body changed after remediation planning."
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
            if status == "ready" and resolved_preview_url:
                source_issue_action = _notify_every_code_preview_ready_source_issue(
                    record_store=every_code_record_store,
                    owner=github_reference["owner"],
                    repo=github_reference["repo"],
                    pr_number=github_reference["pr_number"],
                    anchor_pr_url=anchor_pr_url,
                    repository=repository,
                    preview_url=resolved_preview_url,
                    token=github_token,
                )
                if not delivery_action:
                    delivery_action = source_issue_action
        except click.ClickException as exc:
            delivery_status = "failed"
            error_message = str(exc)

    return PreviewPrFeedbackRecord(
        feedback_id=(
            feedback_id.strip()
            or build_preview_pr_feedback_id(
                context_name=context,
                anchor_pr_number=anchor_pr_number,
                requested_at=requested_at,
            )
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
        preview_url=resolved_preview_url,
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
        remediates_feedback_id=remediates_feedback_id,
        remediation_reason=remediation_reason,
        remediation_issue_reference=remediation_issue_reference,
        remediation_plan_sha256=remediation_plan_sha256,
        remediation_actor=remediation_actor,
    )
