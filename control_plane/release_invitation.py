"""Ask the Client to review a release when there is something new to check."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import argparse
import hashlib
import json
import re
from pathlib import Path
from time import monotonic
from urllib.parse import quote
from zoneinfo import ZoneInfo

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import (
    ReleaseReviewDecisionRecord,
    ReleaseReviewItem,
    ReleaseReviewStatus,
    ReleaseVersion,
)
from control_plane.release_invitation_changes import client_invitation_changes
from control_plane.release_review import (
    CLIENT_APPROVAL_REQUIRED,
    ReleaseReviewStore,
    checklist_blockers,
    current_release_review,
    release_version,
)
from control_plane.service_human_auth import launchplane_public_origin_from_env
from control_plane.workflows.launchplane import (
    github_api_request,
    resolve_launchplane_github_token,
    launchplane_github_token,
)


@dataclass(slots=True)
class ReleaseInvitationBackoff:
    next_reads: dict[str, tuple[str, float]] = field(default_factory=dict)
    delivered: dict[tuple[str, str], tuple[str, float]] = field(default_factory=dict)


def _now() -> datetime:
    return datetime.now(UTC)


def _display_time() -> str:
    return _now().astimezone(ZoneInfo("America/New_York")).strftime("%B %-d, %Y at %-I:%M %p ET")


def _remember_delivery(
    backoff: ReleaseInvitationBackoff | None,
    receipt: tuple[str, str, str],
    due: datetime,
    reminded: bool,
) -> None:
    if backoff is not None:
        deadline = (
            float("inf") if reminded else monotonic() + max(0, (due - _now()).total_seconds())
        )
        backoff.delivered[receipt[:2]] = (receipt[2], deadline)


def _timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Release invitation timestamp is unavailable.")
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as error:
        raise ValueError("Release invitation timestamp is unavailable.") from error
    if parsed.tzinfo is None:
        raise ValueError("Release invitation timestamp needs a timezone.")
    return parsed.astimezone(UTC)


def _change_link(item: ReleaseReviewItem) -> str:
    # Use the instructions written for the Client, rather than engineering titles.
    notes = " ".join(item.owner_test_notes.splitlines()).replace("@", "@\u200b")
    return f"- [#{item.pull_request_number}]({item.url}): {notes}"


def _announced_changes(
    request: dict[str, object] | None, changes: dict[str, ReleaseReviewItem]
) -> set[str]:
    if request is None:
        return set()
    lines = _comment_lines(request)
    keys = {
        match[1]
        for line in lines
        if (
            match := re.fullmatch(
                r"<!-- launchplane:release-client-change:([0-9a-f]{64}) -->", line
            )
        )
    }
    # Adopt existing invitations using their exact PR links. Subsequent receipts
    # keep announcement history even when a candidate temporarily removes a PR.
    if not keys:
        links = set(re.findall(r"https://github\.com/[^\s)<>]+/pull/[0-9]+", "\n".join(lines)))
        keys.update(key for key, item in changes.items() if item.url in links)
    return keys


REPLACED_MARKER = "<!-- launchplane:release-invitation-replaced -->"


def _replace_older_invitations(
    *, comments: list[dict[str, object]], current: dict[str, object], path: str, token: str
) -> None:
    current_id = current.get("id")
    if not isinstance(current_id, int) or current_id < 1:
        raise ValueError("Release invitation comment identity is unavailable.")
    for comment in comments:
        lines = _comment_lines(comment)
        if comment.get("id") == current_id or REPLACED_MARKER in lines:
            continue
        if not any(
            re.fullmatch(r"<!-- launchplane:release-(?:invitation|reminder):[0-9a-f]{64} -->", line)
            for line in lines
        ):
            continue
        comment_id = comment.get("id")
        if not isinstance(comment_id, int) or comment_id < 1:
            raise ValueError("Release invitation comment identity is unavailable.")
        receipts = [
            line
            for line in lines
            if re.fullmatch(r"<!-- launchplane:[a-z-]+:[0-9a-f]{64} -->", line)
        ]
        body = "\n".join(
            receipts
            + [
                REPLACED_MARKER,
                "",
                "Replaced by the current release invitation below. No action is needed on this older invitation.",
                "",
                "<details><summary>Earlier invitation</summary>",
                "",
                "\n".join(line for line in lines if line not in receipts)
                .replace("@", "@\u200b")
                .strip(),
                "",
                "</details>",
            ]
        )
        result = github_api_request(
            path=f"{path}/comments/{comment_id}", token=token, method="PATCH", body={"body": body}
        )
        if not isinstance(result, dict) or result.get("id") != comment_id:
            raise ValueError("Release invitation replacement was not confirmed.")


def _comment_lines(comment: dict[str, object]) -> list[str]:
    body = comment.get("body")
    if not isinstance(body, str):
        raise ValueError("Release invitation comment body is unavailable.")
    return body.splitlines()


def _last_client_decision(
    store: ReleaseReviewStore, profile: LaunchplaneProductProfileRecord
) -> ReleaseReviewDecisionRecord | None:
    return max(
        [
            decision
            for decision in store.list_release_review_decision_records(product=profile.product)
            if decision.checklist.repository == profile.repository
            and decision.checklist.owner_github_id == profile.owner.github_id
            and decision.decision in ("accepted", "changes_requested")
        ],
        key=lambda decision: (_timestamp(decision.decided_at), decision.record_id),
        default=None,
    )


def _request_marker(
    profile: LaunchplaneProductProfileRecord, decision: ReleaseReviewDecisionRecord | None
) -> str:
    identity = (
        profile.product,
        profile.repository,
        profile.owner.github_id,
        decision.record_id if decision else "",
    )
    key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return f"<!-- launchplane:release-request:{key} -->"


def _open_request(
    comments: list[dict[str, object]],
    marker: str,
    decision: ReleaseReviewDecisionRecord | None,
) -> dict[str, object] | None:
    matching = [
        comment
        for comment in comments
        if marker in _comment_lines(comment) and REPLACED_MARKER not in _comment_lines(comment)
    ]
    if matching:
        # A lost post/replacement response may leave two current receipts. The
        # newest is authoritative; the replacement pass repairs the older one.
        return matching[-1]
    # Adopt the newest legacy/manual receipt still awaiting a Client decision.
    # Candidate markers alone cannot distinguish an open request from a decided one.
    legacy = []
    for comment in comments:
        lines = _comment_lines(comment)
        if REPLACED_MARKER in lines:
            continue
        if any(line.startswith("<!-- launchplane:release-request:") for line in lines):
            continue
        if not any(
            re.fullmatch(r"<!-- launchplane:release-invitation:[0-9a-f]{64} -->", line)
            for line in lines
        ):
            continue
        if decision and _timestamp(comment.get("created_at")) <= _timestamp(decision.decided_at):
            continue
        legacy.append(comment)
    return legacy[-1] if legacy else None


def release_request_issue_marker(product: str) -> str:
    return f"<!-- launchplane:release-requests:{hashlib.sha256(product.encode()).hexdigest()} -->"


def release_invitation_marker(product: str, candidate: ReleaseVersion) -> str:
    identity = (
        product,
        candidate.artifact_id,
        candidate.source_commit,
        candidate.shared_addons_digest,
    )
    key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return f"<!-- launchplane:release-invitation:{key} -->"


def _ready(review: ReleaseReviewStatus, profile: LaunchplaneProductProfileRecord) -> bool:
    checklist = review.checklist
    return bool(
        review.required
        and not review.approved
        and review.latest_decision is None
        and review.unavailable_reason is None
        and review.checklist_complete
        and checklist is not None
        and checklist.repository == profile.repository
        and checklist.owner_github_id == profile.owner.github_id
        and checklist.production != checklist.candidate
        and not checklist_blockers(checklist)
        and review.blockers == (CLIENT_APPROVAL_REQUIRED,)
    )


def _pages(
    path: str, token: str, *, marker: str = "", issues_only: bool = False
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    separator = "&" if "?" in path else "?"
    for page in range(1, 11):
        result = github_api_request(path=f"{path}{separator}per_page=100&page={page}", token=token)
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise ValueError("Release invitation lookup is incomplete.")
        matching = [
            item
            for item in result
            if (not issues_only or "pull_request" not in item)
            and (not marker or marker in str(item.get("body", "")).splitlines())
        ]
        records.extend(matching)
        if marker and matching:
            return records
        if len(result) < 100:
            return records
    raise ValueError("Release invitation lookup exceeds the supported size.")


def publish_release_invitation(
    *,
    store: ReleaseReviewStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    backoff: ReleaseInvitationBackoff | None = None,
) -> None:
    origin = launchplane_public_origin_from_env()
    if (
        not origin
        or not profile.is_active
        or profile.production_use == "prelaunch"
        or profile.release_on_acceptance == "director_standing"
        or not profile.owner.is_set
    ):
        return
    production = release_version(store=store, profile=profile, instance="prod")
    candidate = release_version(store=store, profile=profile, instance="testing")
    if production == candidate:
        return
    last_decision = _last_client_decision(store, profile)
    request_marker = _request_marker(profile, last_decision)
    receipt = (
        profile.product,
        profile.repository,
        request_marker + release_invitation_marker(profile.product, candidate),
    )
    receipt_due = False
    if backoff is not None:
        previous_marker, next_read = backoff.delivered.get(receipt[:2], ("", 0))
        if previous_marker == receipt[2]:
            if monotonic() < next_read:
                return
            receipt_due = True
            backoff.delivered.pop(receipt[:2])
    # A person already decided about these versions. This avoids polling GitHub
    # for settled products without letting a cached result supply acceptance.
    if any(
        decision.checklist.production == production
        and decision.checklist.candidate == candidate
        and decision.checklist.repository == profile.repository
        and decision.checklist.owner_github_id == profile.owner.github_id
        for decision in store.list_release_review_decision_records(product=profile.product, limit=1)
    ):
        return
    # Do not compile a large checklist on every worker poll. This is a read
    # throttle only; GitHub's candidate marker remains the durable receipt.
    fingerprint = (
        profile.model_dump_json() + production.model_dump_json() + candidate.model_dump_json()
    )
    if backoff is not None:
        previous_fingerprint, next_read = backoff.next_reads.get(profile.product, ("", 0))
        if not receipt_due and previous_fingerprint == fingerprint and monotonic() < next_read:
            return
        backoff.next_reads[profile.product] = (fingerprint, monotonic() + 300)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}", profile.owner.github_login):
        raise ValueError("Release invitation requires a GitHub login.")
    # Product-wide serialization also prevents competing candidates creating
    # separate destination issues. The existing lock spans all worker replicas.
    with store.release_review_publication_lock(record_id=f"invitation:{profile.product}"):
        current = store.read_product_profile_record(profile.product)
        if current != profile:
            return
        review = current_release_review(
            control_plane_root=control_plane_root, record_store=store, profile=profile
        )
        if not _ready(review, profile):
            return
        assert review.checklist is not None
        marker = release_invitation_marker(profile.product, review.checklist.candidate)
        issue_marker = release_request_issue_marker(profile.product)
        lane = next(lane for lane in profile.lanes if lane.instance == "testing")
        with launchplane_github_token(
            token_resolver=resolve_launchplane_github_token,
            api_request=github_api_request,
            control_plane_root=control_plane_root,
            context_name=lane.context,
            repository=profile.repository,
            purpose="release_record",
        ) as token:
            if not token:
                raise ValueError("Release invitation source-control access is unavailable.")
            path = f"/repos/{quote(profile.repository)}/issues"
            issues = _pages(
                f"{path}?state=all&sort=updated&direction=desc",
                token,
                marker=issue_marker,
                issues_only=True,
            )
            if len(issues) > 1:
                raise ValueError("Release invitation destination is ambiguous.")
            if not issues:
                created = github_api_request(
                    path=path,
                    token=token,
                    method="POST",
                    body={"title": "Release review requests", "body": issue_marker},
                )
                if not isinstance(created, dict):
                    raise ValueError("Release invitation issue creation was not confirmed.")
                issues = [created]
            number = issues[0].get("number")
            if not isinstance(number, int) or number < 1:
                raise ValueError("Release invitation issue number is unavailable.")
            comments_path = f"{path}/{number}/comments"
            comments = _pages(comments_path, token)
            request = _open_request(comments, request_marker, last_decision)
            reminder_marker = request_marker.replace("release-request:", "release-reminder:")
            reminded = any(reminder_marker in _comment_lines(comment) for comment in comments)
            # Recompile after destination lookup: a concurrent acceptance or testing
            # deploy must not receive an invitation for the obsolete snapshot.
            if store.read_product_profile_record(profile.product) != profile:
                return
            latest = current_release_review(
                control_plane_root=control_plane_root, record_store=store, profile=profile
            )
            if (
                not _ready(latest, profile)
                or latest.checklist_digest != review.checklist_digest
                or _last_client_decision(store, profile) != last_decision
            ):
                return
            # Reuse the review page's authority calculation, including unsupported
            # drivers and the one-time rollback drill. Import here to avoid the
            # client-release module's publisher import creating a module cycle.
            from control_plane.client_release import release_start_for_acceptance

            mode = release_start_for_acceptance(store=store, profile=profile)
            effect = "Accepting records your approval; an admin starts the release."
            if mode:
                effect = "Accepting starts the release to the production site, with a verified backup, checks and automatic rollback."
                if mode == "promote_with_rollback_drill":
                    effect = "Accepting starts the release to the production site and its rollback-and-re-release drill, with verified backups, checks and automatic rollback."
            review_link = f"{origin}/ui/owner-review?product={quote(profile.product, safe='')}"
            due = (
                _timestamp(request.get("created_at")) + timedelta(days=3)
                if request
                else _now() + timedelta(days=3)
            )
            client_changes = client_invitation_changes(store, profile, review.checklist)
            announced = _announced_changes(request, client_changes)
            added = {key: item for key, item in client_changes.items() if key not in announced}
            # A matching candidate receipt still needs the one-time migration
            # to plain wording and cleanup of older duplicate invitations.
            formatted = request and any(
                line.startswith("Updated at: ") for line in _comment_lines(request)
            )
            if request and marker in _comment_lines(request) and formatted and not added:
                _replace_older_invitations(
                    comments=comments, current=request, path=path, token=token
                )
                if reminded or _now() < due:
                    _remember_delivery(backoff, receipt, due, reminded)
                    return
                # One durable reminder per open request. A lost response is
                # recovered from this marker, including after replica restart.
                body = f"{reminder_marker}\n" + str(request["body"]).replace(
                    f"Hi {profile.owner.github_login},", f"Hi @{profile.owner.github_login},"
                ).replace("Hi @", "A reminder: hi @")
                body = re.sub(r"(?m)^Updated at: .*", f"Updated at: {_display_time()}", body)
                result = github_api_request(
                    path=comments_path, token=token, method="POST", body={"body": body}
                )
                if not isinstance(result, dict) or not isinstance(result.get("id"), int):
                    raise ValueError("Release invitation reminder was not confirmed.")
                _replace_older_invitations(
                    comments=comments, current=result, path=path, token=token
                )
                _remember_delivery(backoff, receipt, due, True)
                return
            notify = not request or bool(added)
            changes = [
                _change_link(item)
                for item in (added if request and added else client_changes).values()
            ]
            if not changes:
                changes = ["- Engineering updates only; no new Client change needs checking."]
            client = f"@{profile.owner.github_login}" if notify else profile.owner.github_login
            heading = (
                "Added to this release" if request and added else "What to check in this release"
            )
            receipts = "\n".join(
                f"<!-- launchplane:release-client-change:{key} -->"
                for key in sorted(announced | client_changes.keys())
            )
            if reminded:
                receipts += f"\n{reminder_marker}"
            display_time = _display_time()
            action = (
                "check the changes on the testing site, then press **Accept** or **Request changes**."
                if client_changes
                else "there is nothing new to test. Read the release summary, then press **Accept** or **Request changes**."
            )
            body = (
                f"{request_marker}\n{marker}\n{receipts}\n\nHi {client},\n\n"
                f"{heading}:\n"
                + "\n".join(changes)
                + f"\n\nStill one thing to do: [open the release page]({review_link}), "
                + action
                + f"\n\n{effect}\n\nUpdated at: {display_time}"
            )
            destination = comments_path
            method = "POST"
            if request and not notify:
                comment_id = request.get("id")
                if not isinstance(comment_id, int) or comment_id < 1:
                    raise ValueError("Release invitation comment identity is unavailable.")
                destination = f"{path}/comments/{comment_id}"
                method = "PATCH"
            result = github_api_request(
                path=destination, token=token, method=method, body={"body": body}
            )
            if not isinstance(result, dict) or not isinstance(result.get("id"), int):
                raise ValueError("Release invitation publication was not confirmed.")
            _replace_older_invitations(comments=comments, current=result, path=path, token=token)
            if notify:
                due = _timestamp(result.get("created_at")) + timedelta(days=3)
            _remember_delivery(backoff, receipt, due, reminded)


def main() -> None:
    """Print adoption markers from an exact candidate read; never publish."""
    parser = argparse.ArgumentParser(description=main.__doc__)
    parser.add_argument("--product", required=True)
    parser.add_argument("--candidate-file", required=True, type=Path)
    args = parser.parse_args()
    candidate = ReleaseVersion.model_validate_json(args.candidate_file.read_text())
    print(
        json.dumps(
            {
                "issue_marker": release_request_issue_marker(args.product),
                "comment_marker": release_invitation_marker(args.product, candidate),
            }
        )
    )


if __name__ == "__main__":
    main()
