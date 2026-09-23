"""Project a saved release decision into a durable tenant repository issue."""

from datetime import UTC, datetime
from pathlib import Path
import re
from urllib.parse import quote

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import ReleaseReviewDecisionRecord
from control_plane.workflows.launchplane import github_api_request, resolve_launchplane_github_token


def _literal(value: str) -> str:
    # Notes and reasons are data, including Markdown fences and incidental mentions.
    fence = "`" * max(3, 1 + max((len(run) for run in re.findall(r"`+", value)), default=0))
    return f"{fence}\n{value.replace('@', '@\u200b')}\n{fence}"


def release_decision_issue_body(decision: ReleaseReviewDecisionRecord) -> str:
    checklist = decision.checklist
    lines = [
        f"<!-- launchplane:release-decision:{decision.record_id} -->",
        "# Release decision",
        f"Decision: **{decision.decision.replace('_', ' ')}**",
        f"Recorded by `{decision.actor_github_login}` (GitHub ID `{decision.actor_github_id}`)",
        f"Recorded at: {decision.decided_at}",
        "",
        "This records a decision in Launchplane. It does not publish the site, and editing this issue does not change approval.",
        "",
        "## Release",
        _literal(
            f"Product: {checklist.product}\n"
            f"Production: {checklist.production.source_commit} ({checklist.production.artifact_id})\n"
            f"Testing: {checklist.candidate.source_commit} ({checklist.candidate.artifact_id})\n"
            f"Production shared inputs: {checklist.production.shared_addons_digest}\n"
            f"Testing shared inputs: {checklist.candidate.shared_addons_digest}\n"
            f"Testing site: {checklist.testing_url}\n"
            f"Checklist digest: {decision.checklist_digest}"
        ),
        "",
        "## Owner checklist",
    ]
    for item in checklist.items:
        lines.extend(
            [
                f"### Pull request #{item.pull_request_number}",
                _literal(item.title),
                item.url,
                "Accepted in preview." if item.already_reviewed else "Not accepted in preview.",
                _literal(item.owner_test_notes or "Owner test notes are missing."),
            ]
        )
    if not checklist.items:
        lines.append("No merged pull request changes between these versions.")
    if checklist.untracked_commits:
        lines.extend(
            [
                "## Commits without pull request coverage",
                _literal("\n".join(checklist.untracked_commits)),
            ]
        )
    if checklist.additional_changes:
        lines.extend(["## Additional changes", _literal("\n".join(checklist.additional_changes))])
    if decision.reason:
        lines.extend(["## Decision reason", _literal(decision.reason)])
    return "\n\n".join(lines)


def _record_parts(decision: ReleaseReviewDecisionRecord) -> tuple[str, tuple[str, ...]]:
    body = release_decision_issue_body(decision)
    if len(body.encode()) <= 60000:
        return body, ()
    # UTF-8 uses at most four bytes per character. Literal blocks keep each
    # piece below the provider limit even when notes contain Unicode or fences.
    chunks = tuple(body[offset : offset + 12000] for offset in range(0, len(body), 12000))
    marker = body.splitlines()[0]
    issue = (
        f"{marker}\n\n# Release decision\n\n"
        f"Decision: **{decision.decision.replace('_', ' ')}**\n\n"
        f"Checklist digest: `{decision.checklist_digest}`\n\n"
        f"The complete saved decision and checklist are recorded in {len(chunks)} numbered comments."
    )
    comments = tuple(
        f"{marker}\n\n## Release record part {index + 1} of {len(chunks)}\n\n{_literal(chunk)}"
        for index, chunk in enumerate(chunks)
    )
    return issue, comments


def _publish_record_parts(*, path: str, token: str, parts: tuple[str, ...]) -> None:
    if not parts:
        return
    existing: set[str] = set()
    for page in range(1, 101):
        comments = github_api_request(path=f"{path}/comments?per_page=100&page={page}", token=token)
        if not isinstance(comments, list):
            raise ValueError("Release record comments are unavailable.")
        for comment in comments:
            if not isinstance(comment, dict) or not isinstance(comment.get("body"), str):
                raise ValueError("Release record comments are incomplete.")
            existing.add(comment["body"])
        if len(comments) < 100:
            break
    else:
        raise ValueError("Release record comment lookup exceeds the supported size.")
    for part in parts:
        if part in existing:
            continue
        created = github_api_request(
            path=f"{path}/comments", token=token, method="POST", body={"body": part}
        )
        if not isinstance(created, dict) or created.get("body") != part:
            raise ValueError("Release record comment creation was not confirmed.")


def publish_release_decision(
    *,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
) -> str:
    """Return the issue URL; recover a successful but unacknowledged prior write."""
    if profile.product != decision.product or profile.repository != decision.checklist.repository:
        raise ValueError("The release record must belong to the product repository.")
    body, parts = _record_parts(decision)
    lane = next(lane for lane in profile.lanes if lane.instance == "testing")
    token = resolve_launchplane_github_token(
        control_plane_root=control_plane_root, context_name=lane.context
    )
    if not token:
        raise ValueError("Release record source-control access is unavailable.")
    path = f"/repos/{quote(profile.repository)}/issues"
    decision_time = (
        datetime.fromisoformat(decision.decided_at).astimezone(UTC).replace(microsecond=0)
    )
    issue_number = None
    # A retry reuses the saved decision ID. Do not duplicate an issue when the
    # first POST succeeded but its response or the following DB write was lost.
    for page in range(1, 11):
        issues = github_api_request(
            path=f"{path}?state=all&sort=created&direction=desc&per_page=100&page={page}",
            token=token,
        )
        if not isinstance(issues, list):
            raise ValueError("Release record lookup is unavailable.")
        reached_older_issue = False
        for issue in issues:
            if not isinstance(issue, dict):
                raise ValueError("Release record lookup is incomplete.")
            created_at = issue.get("created_at")
            if isinstance(created_at, str):
                created_time = datetime.fromisoformat(created_at).astimezone(UTC)
                reached_older_issue |= created_time < decision_time
            if "pull_request" in issue or issue.get("body") != body:
                continue
            number = issue.get("number")
            if not isinstance(number, int) or number < 1:
                raise ValueError("Release record number is unavailable.")
            issue_number = number
            break
        if issue_number is not None or len(issues) < 100 or reached_older_issue:
            break
    else:
        raise ValueError("Release record lookup exceeds the supported size.")
    if issue_number is None:
        created = github_api_request(
            path=path,
            token=token,
            method="POST",
            body={
                "title": f"Release {decision.checklist.candidate.source_commit[:7]}: {decision.decision.replace('_', ' ')}",
                "body": body,
            },
        )
        number = created.get("number") if isinstance(created, dict) else None
        if not isinstance(number, int) or number < 1:
            raise ValueError("Release record creation was not confirmed.")
        issue_number = number
    _publish_record_parts(path=f"{path}/{issue_number}", token=token, parts=parts)
    return f"https://github.com/{profile.repository}/issues/{issue_number}"
