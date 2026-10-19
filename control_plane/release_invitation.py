"""Ask the Client to review a release when there is something new to check."""

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
import argparse
import hashlib
import json
import logging
import re
from pathlib import Path
from time import monotonic
from threading import Event
from urllib.parse import quote
from zoneinfo import ZoneInfo
from control_plane.client_test_notes import _markdown_lines
from control_plane.github_payload import github_app_authored
from control_plane.launchplane_github_delivery import resolve_delivery_github_app_id

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import (
    ReleaseReviewDecisionRecord,
    ReleaseReviewStatus,
    ReleaseVersion,
)
from control_plane.release_invitation_changes import InvitationChange, client_invitation_changes
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


# How often a worker confirms that a decision on unchanged lane versions still covers
# the current checklist. Changed test notes can invalidate it without a new candidate.
SETTLED_DECISION_RECHECK_SECONDS = 1800
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ReleaseInvitationBackoff:
    next_reads: dict[str, tuple[str, float]] = field(default_factory=dict)
    delivered: dict[tuple[str, str], tuple[str, float]] = field(default_factory=dict)
    settled: dict[str, tuple[str, float]] = field(default_factory=dict)
    completions: dict[str, tuple[str, float]] = field(default_factory=dict)
    diagnostics: dict[str, tuple[type[Exception], float]] = field(default_factory=dict)


def advance_release_invitations(
    *,
    store: object,
    control_plane_root: Path,
    stop_event: Event | None = None,
    backoff: ReleaseInvitationBackoff | None = None,
) -> None:
    """Cold notification reads never occupy the accepted-release worker."""
    from control_plane.storage.postgres import PostgresRecordStore
    from control_plane.release_invitation_changes import ReleaseInvitationNotesUnavailable

    if not isinstance(store, PostgresRecordStore):
        return
    for profile in store.list_product_profile_records():
        if stop_event is not None and stop_event.is_set():
            break
        try:
            publish_release_invitation(
                store=store, control_plane_root=control_plane_root, profile=profile, backoff=backoff
            )
        except Exception as error:
            now = monotonic()
            previous, next_log = (
                backoff.diagnostics.get(profile.product, (None, 0))
                if backoff is not None
                else (None, 0)
            )
            if type(error) is previous and now < next_log:
                continue
            if backoff is not None:
                backoff.diagnostics[profile.product] = (type(error), now + 300)
            if isinstance(error, ReleaseInvitationNotesUnavailable):
                _LOGGER.warning(
                    "release invitation unavailable product=%s error_type=%s reason=%s",
                    profile.product,
                    type(error).__name__,
                    error,
                )
            else:
                _LOGGER.warning(
                    "release invitation unavailable product=%s error_type=%s",
                    profile.product,
                    type(error).__name__,
                )
        else:
            if backoff is not None:
                backoff.diagnostics.pop(profile.product, None)


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


def _change_link(change: InvitationChange) -> str:
    # Keep Client instructions; name constituents instead of their batch PR.
    item = change.item
    name = change.feature_name.replace("@", "@\u200b")
    notes = " ".join((change.release_test_notes or item.owner_test_notes).splitlines()).replace(
        "@", "@\u200b"
    )
    return f"- [#{item.pull_request_number}]({item.url}): " + (
        f"{name} — {notes}" if name else notes
    )


def _announced_changes(
    request: dict[str, object] | None, changes: dict[str, InvitationChange], candidate_marker: str
) -> set[str]:
    if request is None:
        return set()
    lines = _comment_lines(request)
    if candidate_marker in lines and not any(
        line.startswith("<!-- launchplane:release-request:") for line in lines
    ):
        # A manually adopted receipt for this exact candidate already requested
        # every included change, even when its prose has no individual PR links.
        return set(changes)
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
        keys.update(
            key
            for key, change in changes.items()
            if change.item.url in links or change.legacy_url in links
        )
    return keys


REPLACED_MARKER = "<!-- launchplane:release-invitation-replaced -->"
COMPLETED_PREFIX = "<!-- launchplane:release-invitation-completed:"
WHY_NOW = "**Why now:** "
ADOPTED_ORIGINAL_PREFIX = "<!-- launchplane:release-adopted-original:"


def _adopted_originals(
    comments: list[dict[str, object]], *, token: str, control_plane_root: Path
) -> set[int]:
    return {
        int(match[1])
        for comment in comments
        for line in _receipt_lines(comment)
        if (match := re.fullmatch(re.escape(ADOPTED_ORIGINAL_PREFIX) + r"([1-9][0-9]*) -->", line))
        and github_app_authored(
            comment,
            resolve_delivery_github_app_id(control_plane_root=control_plane_root),
            lookup_app=lambda slug: github_api_request(path=f"/apps/{quote(slug)}", token=token),
        )
    }


def _replace_older_invitations(
    *,
    comments: list[dict[str, object]],
    current: dict[str, object],
    path: str,
    token: str,
    request_marker: str,
    last_decision: ReleaseReviewDecisionRecord | None,
    control_plane_root: Path,
) -> None:
    current_id = current.get("id")
    if not isinstance(current_id, int) or current_id < 1:
        raise ValueError("Release invitation comment identity is unavailable.")
    preserved = _adopted_originals(comments, token=token, control_plane_root=control_plane_root)
    for comment in comments:
        lines = _comment_lines(comment)
        if (
            comment.get("id") in preserved
            or comment.get("id") == current_id
            or REPLACED_MARKER in lines
        ):
            continue
        if not any(
            re.fullmatch(r"<!-- launchplane:release-(?:invitation|reminder):[0-9a-f]{64} -->", line)
            for line in lines
        ):
            continue
        # Completed reviews keep their historical invitations. Only the current
        # request and its undecided legacy duplicates are replacement targets.
        if (
            any(line.startswith("<!-- launchplane:release-request:") for line in lines)
            and request_marker not in lines
        ):
            continue
        if last_decision and _timestamp(comment.get("created_at")) <= _timestamp(
            last_decision.decided_at
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
                "\n".join(line for line in _comment_lines(comment) if line not in receipts)
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


def _receipt_lines(comment: dict[str, object]) -> list[str]:
    return [
        line
        for line, _heading, fence in _markdown_lines("\n".join(_comment_lines(comment)))
        if not fence
    ]


def _last_client_decision(
    store: ReleaseReviewStore,
    profile: LaunchplaneProductProfileRecord,
    *,
    candidate: ReleaseVersion | None = None,
    before: ReleaseReviewDecisionRecord | None = None,
) -> ReleaseReviewDecisionRecord | None:
    return max(
        [
            decision
            for decision in store.list_release_review_decision_records(product=profile.product)
            if decision.checklist.repository == profile.repository
            and decision.checklist.owner_github_id == profile.owner.github_id
            and decision.decision in ("accepted", "changes_requested")
            and (candidate is None or decision.checklist.candidate == candidate)
            and (
                before is None
                or (_timestamp(decision.decided_at), decision.record_id)
                < (_timestamp(before.decided_at), before.record_id)
            )
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
    path: str, token: str, *, app_id: int | None = None, marker: str = "", issues_only: bool = False
) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    separator = "&" if "?" in path else "?"
    for page in range(1, 11):
        result = github_api_request(path=f"{path}{separator}per_page=100&page={page}", token=token)
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise ValueError("Release invitation lookup is incomplete.")
        marked = [
            item
            for item in result
            if (not issues_only or "pull_request" not in item)
            and (
                not marker
                or marker in (_comment_lines(item) if app_id is None else _receipt_lines(item))
            )
        ]
        matching = []
        for item in marked:
            if app_id is None or github_app_authored(
                item,
                app_id,
                lookup_app=lambda slug: github_api_request(
                    path=f"/apps/{quote(slug)}", token=token
                ),
            ):
                matching.append(item)
            elif issues_only:
                number = item.get("number")
                if isinstance(number, int) and number > 0:
                    receipts = _pages(
                        f"{path.split('?')[0]}/{number}/comments",
                        token,
                        app_id=app_id,
                        marker=marker,
                    )
                    if receipts:
                        matching.append(item)
        records.extend(matching)
        if marker and matching:
            return records
        if len(result) < 100:
            return records
    raise ValueError("Release invitation lookup exceeds the supported size.")


def _settle_completed_invitation(
    *,
    store: ReleaseReviewStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    production: ReleaseVersion,
    candidate: ReleaseVersion,
    backoff: ReleaseInvitationBackoff | None,
) -> None:
    # Equal lane versions alone are not evidence of an accepted, successful release.
    from control_plane.client_release import read_client_release_run

    fingerprint = (
        profile.model_dump_json() + production.model_dump_json() + candidate.model_dump_json()
    )
    if backoff is not None:
        previous, next_read = backoff.completions.get(profile.product, ("", 0))
        if previous == fingerprint and monotonic() < next_read:
            return
        backoff.completions[profile.product] = (fingerprint, monotonic() + 300)
    with store.release_review_publication_lock(record_id=f"invitation:{profile.product}"):
        last_decision = _last_client_decision(store, profile)
        decision = _last_client_decision(store, profile, candidate=production)
        if (
            store.read_product_profile_record(profile.product) != profile
            or decision is None
            or decision.decision != "accepted"
            or decision.release_start not in ("promote", "promote_with_rollback_drill")
            or not decision.release_issue_url
        ):
            return

        def completed() -> bool:
            run = read_client_release_run(store=store, profile=profile, decision=decision)
            return bool(
                run is not None
                and run.decision_record_id == decision.record_id
                and run.state == "passed"
                and store.read_product_profile_record(profile.product) == profile
                and _last_client_decision(store, profile) == last_decision
                and store.read_release_review_decision_record(
                    product=profile.product, record_id=decision.record_id
                )
                == decision
                and release_version(store=store, profile=profile, instance="prod") == production
                and release_version(store=store, profile=profile, instance="testing") == candidate
            )

        if not completed():
            return
        previous_decision = _last_client_decision(store, profile, before=decision)
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
                marker=release_request_issue_marker(profile.product),
                issues_only=True,
            )
            if len(issues) > 1:
                raise ValueError("Release invitation destination is ambiguous.")
            if not issues:
                if backoff is not None:
                    backoff.completions[profile.product] = (fingerprint, float("inf"))
                return
            number = issues[0].get("number")
            if not isinstance(number, int) or number < 1:
                raise ValueError("Release invitation issue number is unavailable.")
            comments = _pages(f"{path}/{number}/comments", token)
            preserved = _adopted_originals(
                comments, token=token, control_plane_root=control_plane_root
            )
            comments = [comment for comment in comments if comment.get("id") not in preserved]
            marker = release_invitation_marker(profile.product, production)
            completion = (
                COMPLETED_PREFIX + hashlib.sha256(decision.record_id.encode()).hexdigest() + " -->"
            )
            continued_request = None
            if (
                previous_decision is not None
                and previous_decision.checklist_digest == decision.checklist_digest
            ):
                # A changed decision or release retry can use the same checklist
                # without another invitation. Settle only its newest open receipt.
                for comment in reversed(comments):
                    lines = _comment_lines(comment)
                    if marker not in lines or REPLACED_MARKER in lines:
                        continue
                    if any(line.startswith(COMPLETED_PREFIX) for line in lines):
                        continue
                    if _timestamp(comment.get("created_at")) <= _timestamp(
                        previous_decision.decided_at
                    ):
                        continued_request = comment
                    break
            matched = False
            pending = False
            for comment in comments:
                lines = _comment_lines(comment)
                if REPLACED_MARKER in lines:
                    continue
                if completion in lines:
                    matched = True
                    continue
                if any(line.startswith(COMPLETED_PREFIX) for line in lines):
                    continue
                if not any(
                    re.fullmatch(r"<!-- launchplane:release-invitation:[0-9a-f]{64} -->", line)
                    for line in lines
                ):
                    continue
                created_at = _timestamp(comment.get("created_at"))
                if marker not in lines or (
                    previous_decision is not None
                    and created_at <= _timestamp(previous_decision.decided_at)
                    and comment is not continued_request
                ):
                    # Preserve earlier rejected/decided requests as history.
                    if last_decision is None or created_at > _timestamp(last_decision.decided_at):
                        pending = True
                    continue
                if not completed():
                    return
                comment_id = comment.get("id")
                if not isinstance(comment_id, int) or comment_id < 1:
                    raise ValueError("Release invitation comment identity is unavailable.")
                # Keep all original receipts and prose, including adopted manual requests.
                # Neutralize historical mentions instead of notifying the Client again.
                body = (
                    f"{completion}\n{marker}\n\n"
                    "**Released** — this version reached production through its accepted release. "
                    "No action is needed on this invitation.\n\n"
                    f"[Saved acceptance]({decision.release_issue_url})\n\n"
                    "<details><summary>Original invitation and delivery receipts</summary>\n\n"
                    + "\n".join(lines).replace("@", "@\u200b")
                    + "\n\n</details>"
                )
                result = github_api_request(
                    path=f"{path}/comments/{comment_id}",
                    token=token,
                    method="PATCH",
                    body={"body": body},
                )
                if not isinstance(result, dict) or result.get("id") != comment_id:
                    raise ValueError("Release invitation completion was not confirmed.")
                matched = True
            if matched and not pending and production == candidate:
                if not completed():
                    return
                if issues[0].get("state") != "closed":
                    result = github_api_request(
                        path=f"{path}/{number}",
                        token=token,
                        method="PATCH",
                        body={"state": "closed"},
                    )
                    if (
                        not isinstance(result, dict)
                        or result.get("number") != number
                        or result.get("state") != "closed"
                    ):
                        raise ValueError("Release invitation issue closure was not confirmed.")
            # Nothing remains to settle for this production snapshot, even when
            # a different candidate waits for its own decision.
            if backoff is not None:
                backoff.completions[profile.product] = (fingerprint, float("inf"))


def publish_release_invitation(
    *,
    store: ReleaseReviewStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    backoff: ReleaseInvitationBackoff | None = None,
    urgent_reason: str = "",
    urgent_checklist_digest: str = "",
) -> bool:
    """Keep the Client's release request current; return whether one is open.

    Only a Client-facing change opens a request. An engineering-only candidate
    waits for the next Client-facing release unless an admin deliberately opens
    it with ``urgent_reason`` for the exact checklist the admin reviewed
    (``urgent_checklist_digest``). The Client still has to accept it either way.
    """
    origin = launchplane_public_origin_from_env()
    if (
        not origin
        or not profile.is_active
        or profile.production_use == "prelaunch"
        or profile.release_on_acceptance == "director_standing"
        or not profile.owner.is_set
    ):
        return False
    production = release_version(store=store, profile=profile, instance="prod")
    candidate = release_version(store=store, profile=profile, instance="testing")
    if not urgent_reason:
        try:
            _settle_completed_invitation(
                store=store,
                control_plane_root=control_plane_root,
                profile=profile,
                production=production,
                candidate=candidate,
                backoff=backoff,
            )
        except Exception as error:
            if production == candidate:
                raise
            # Historical completion must not prevent the next real review.
            # As in the worker, log only bounded identities, not provider text.
            _LOGGER.warning(
                "release invitation completion unavailable product=%s error_type=%s",
                profile.product,
                type(error).__name__,
            )
    if production == candidate:
        return False
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
                return True
            receipt_due = True
            backoff.delivered.pop(receipt[:2])
    # A live temporary hotfix awaits the Client's review first; its release record
    # already asked them, and their review page shows that hotfix until they decide.
    from control_plane.client_release import live_temporary_hotfix

    if live_temporary_hotfix(store, profile) is not None:
        return False
    # Do not compile a large checklist on every worker poll. This is a read
    # throttle only; GitHub's candidate marker remains the durable receipt.
    fingerprint = (
        profile.model_dump_json() + production.model_dump_json() + candidate.model_dump_json()
    )
    if backoff is not None:
        previous_fingerprint, next_read = backoff.next_reads.get(profile.product, ("", 0))
        if not receipt_due and previous_fingerprint == fingerprint and monotonic() < next_read:
            return False
        backoff.next_reads[profile.product] = (fingerprint, monotonic() + 300)
    # A person already decided about these versions. The decision still settles the
    # review only while it covers the current checklist; edited test notes change the
    # checklist without a new candidate, and the Client then needs a fresh request.
    settled = next(
        (
            decision
            for decision in store.list_release_review_decision_records(
                product=profile.product, limit=1
            )
            if decision.checklist.production == production
            and decision.checklist.candidate == candidate
            and decision.checklist.repository == profile.repository
            and decision.checklist.owner_github_id == profile.owner.github_id
        ),
        None,
    )
    if settled is not None:
        if backoff is not None:
            checked, next_check = backoff.settled.get(profile.product, ("", 0))
            if checked == settled.record_id and monotonic() < next_check:
                return False
        current_review = current_release_review(
            control_plane_root=control_plane_root, record_store=store, profile=profile
        )
        if current_review.unavailable_reason is not None or current_review.checklist_digest in (
            "",
            settled.checklist_digest,
        ):
            if backoff is not None:
                backoff.settled[profile.product] = (
                    settled.record_id,
                    monotonic() + SETTLED_DECISION_RECHECK_SECONDS,
                )
            return False
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,38}", profile.owner.github_login):
        raise ValueError("Release invitation requires a GitHub login.")
    # Product-wide serialization also prevents competing candidates creating
    # separate destination issues. The existing lock spans all worker replicas.
    with store.release_review_publication_lock(record_id=f"invitation:{profile.product}"):
        current = store.read_product_profile_record(profile.product)
        if current != profile:
            return False
        review = current_release_review(
            control_plane_root=control_plane_root, record_store=store, profile=profile
        )
        if not _ready(review, profile):
            return False
        # The admin's reason applies only to the checklist they reviewed.
        if urgent_reason and review.checklist_digest != urgent_checklist_digest:
            return False
        assert review.checklist is not None
        client_changes = client_invitation_changes(store, profile, review.checklist)
        notification_changes = {
            key: change for key, change in client_changes.items() if change.client_review_requested
        }
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
                if not notification_changes and not urgent_reason:
                    return False
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
            # Completion preserves original receipts as history, not active requests
            # or reminders for a later candidate under the same saved decision.
            comments = [
                comment
                for comment in _pages(comments_path, token)
                if REPLACED_MARKER not in _comment_lines(comment)
                and not any(line.startswith(COMPLETED_PREFIX) for line in _comment_lines(comment))
            ]
            request = _open_request(comments, request_marker, last_decision)
            # Engineering-only changes never open a release review on their own;
            # they ride with the next Client-facing release or an urgent opening.
            if request is None and not notification_changes and not urgent_reason:
                return False
            reminder_marker = request_marker.replace("release-request:", "release-reminder:")
            reminded = any(reminder_marker in _comment_lines(comment) for comment in comments)
            # Recompile after destination lookup: a concurrent acceptance or testing
            # deploy must not receive an invitation for the obsolete snapshot.
            if store.read_product_profile_record(profile.product) != profile:
                return False
            latest = current_release_review(
                control_plane_root=control_plane_root, record_store=store, profile=profile
            )
            if (
                not _ready(latest, profile)
                or latest.checklist_digest != review.checklist_digest
                or _last_client_decision(store, profile) != last_decision
            ):
                return False
            if issues[0].get("state") == "closed":
                result = github_api_request(
                    path=f"{path}/{number}", token=token, method="PATCH", body={"state": "open"}
                )
                if (
                    not isinstance(result, dict)
                    or result.get("number") != number
                    or result.get("state") != "open"
                ):
                    raise ValueError("Release invitation issue reopening was not confirmed.")
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
            announced = _announced_changes(request, notification_changes, marker)
            added = {
                key: item for key, item in notification_changes.items() if key not in announced
            }
            # A matching candidate receipt still needs the one-time migration
            # to plain wording and cleanup of older duplicate invitations.
            formatted = (
                request
                and any(line.startswith("Updated at: ") for line in _comment_lines(request))
                and any(line.startswith("**Release review**") for line in _comment_lines(request))
            )
            # An unchanged candidate can still have a request from before release
            # steps were displayed, or corrected notes on its source PR. Refresh
            # that content silently before trusting or reminding from the receipt.
            release_steps_changed = request is not None and any(
                _change_link(change) not in _comment_lines(request)
                for change in client_changes.values()
                if change.release_test_notes
            )
            if (
                request
                and marker in _comment_lines(request)
                and formatted
                and not added
                and not release_steps_changed
            ):
                _replace_older_invitations(
                    comments=comments,
                    current=request,
                    path=path,
                    token=token,
                    request_marker=request_marker,
                    last_decision=last_decision,
                    control_plane_root=control_plane_root,
                )
                # An explicit urgent-open retry only confirms the existing request;
                # routine workers retain the scheduled reminder behavior.
                if reminded or _now() < due or urgent_reason:
                    _remember_delivery(backoff, receipt, due, reminded)
                    return True
                # One durable reminder per open request. A lost response is
                # recovered from this marker, including after replica restart.
                body = f"{reminder_marker}\n" + "\n".join(_comment_lines(request)).replace(
                    f"Hi {profile.owner.github_login},", f"Hi @{profile.owner.github_login},"
                )
                body = re.sub(r"(?m)^Hi @", "A reminder: hi @", body)
                body = re.sub(r"(?m)^Updated at: .*", f"Updated at: {_display_time()}", body)
                result = github_api_request(
                    path=comments_path, token=token, method="POST", body={"body": body}
                )
                if not isinstance(result, dict) or not isinstance(result.get("id"), int):
                    raise ValueError("Release invitation reminder was not confirmed.")
                _replace_older_invitations(
                    comments=comments,
                    current=result,
                    path=path,
                    token=token,
                    request_marker=request_marker,
                    last_decision=last_decision,
                    control_plane_root=control_plane_root,
                )
                _remember_delivery(backoff, receipt, due, True)
                return True
            notify = not request or bool(added)
            changes = [
                _change_link(change)
                for change in (added if request and added else client_changes).values()
            ]
            if not changes:
                changes = ["- Engineering updates only; no new Client change needs checking."]
            client = f"@{profile.owner.github_login}" if notify else profile.owner.github_login
            heading = (
                "Added to this release" if request and added else "What to check in this release"
            )
            receipts = "\n".join(
                f"<!-- launchplane:release-client-change:{key} -->"
                for key in sorted(announced | notification_changes.keys())
            )
            if reminded:
                receipts += f"\n{reminder_marker}"
            if request and _adopted_originals(
                [request], token=token, control_plane_root=control_plane_root
            ):
                # Keep the adoption identity and original-comment custody through
                # every ordinary rewrite, including later reminders/completion.
                receipts += "\n" + "\n".join(
                    line
                    for line in _comment_lines(request)
                    if line.startswith(
                        (
                            ADOPTED_ORIGINAL_PREFIX,
                            "<!-- launchplane:release-requests:",
                            "<!-- launchplane:release-adoption:",
                            "Adoption reason: ",
                        )
                    )
                )
            # The admin's reason for opening early stays on the request through
            # later silent updates; neutralize mentions in it like Client notes.
            why_now = (
                next((line for line in _comment_lines(request) if line.startswith(WHY_NOW)), "")
                if request
                else ""
            )
            if not request and urgent_reason:
                why_now = WHY_NOW + " ".join(urgent_reason.split()).replace("@", "@\u200b")
            display_time = _display_time()
            action = (
                "check the changes on the testing site, then press **Accept** or **Request changes**."
                if client_changes
                else "there is nothing new to test. Read the release summary, then press **Accept** or **Request changes**."
            )
            body = (
                f"{request_marker}\n{marker}\n{receipts}\n\n"
                "**Release review** — is the site working with these changes, so they can go live?\n\n"
                f"Hi {client},\n\n"
                + (f"{why_now}\n\n" if why_now else "")
                + f"{heading}:\n"
                + "\n".join(changes)
                + f"\n\nStill one thing to do: [open the release page]({review_link}), "
                + action
                + " The release page lists every included change."
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
            _replace_older_invitations(
                comments=comments,
                current=result,
                path=path,
                token=token,
                request_marker=request_marker,
                last_decision=last_decision,
                control_plane_root=control_plane_root,
            )
            if notify:
                due = _timestamp(result.get("created_at")) + timedelta(days=3)
            _remember_delivery(backoff, receipt, due, reminded)
            return True
    # The lock's context-manager type allows it to suppress an error.
    return False


def adopt_release_invitation(
    *,
    store: ReleaseReviewStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    checklist_digest: str,
    issue_number: int,
    comment_id: int,
    dry_run: bool,
    reason: str = "",
) -> bool:
    """Attest an admin-selected existing request with no mention or original edit."""
    with store.release_review_publication_lock(record_id=f"invitation:{profile.product}"):
        review = current_release_review(
            control_plane_root=control_plane_root, record_store=store, profile=profile
        )
        if (
            not _ready(review, profile)
            or review.checklist is None
            or review.checklist_digest != checklist_digest
            or store.read_product_profile_record(profile.product) != profile
        ):
            return False
        last_decision = _last_client_decision(store, profile)
        candidate_marker = release_invitation_marker(profile.product, review.checklist.candidate)
        request_marker = _request_marker(profile, last_decision)
        issue_marker = release_request_issue_marker(profile.product)
        identity = (profile.product, checklist_digest, issue_number, comment_id, request_marker)
        adoption_marker = (
            "<!-- launchplane:release-adoption:"
            + hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
            + " -->"
        )
        changes = client_invitation_changes(store, profile, review.checklist)
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
            app_id = resolve_delivery_github_app_id(control_plane_root=control_plane_root)
            path = f"/repos/{quote(profile.repository)}/issues"
            issue = github_api_request(path=f"{path}/{issue_number}", token=token)
            original = github_api_request(path=f"{path}/comments/{comment_id}", token=token)
            if (
                not isinstance(issue, dict)
                or "pull_request" in issue
                or issue.get("number") != issue_number
                or issue.get("state") != "open"
                or not isinstance(original, dict)
                or original.get("id") != comment_id
                or original.get("issue_url") != f"https://api.github.com{path}/{issue_number}"
                or issue_marker not in _receipt_lines(issue)
                or candidate_marker not in _receipt_lines(original)
                or REPLACED_MARKER in _receipt_lines(original)
                or any(line.startswith(COMPLETED_PREFIX) for line in _receipt_lines(original))
                or (
                    last_decision is not None
                    and _timestamp(original.get("created_at"))
                    <= _timestamp(last_decision.decided_at)
                )
            ):
                return False
            comments_path = f"{path}/{issue_number}/comments"
            destinations = _pages(
                f"{path}?state=all&sort=updated&direction=desc",
                token,
                app_id=app_id,
                marker=issue_marker,
                issues_only=True,
            )
            if any(destination.get("number") != issue_number for destination in destinations):
                return False
            existing = _pages(comments_path, token, app_id=app_id, marker=adoption_marker)
            if existing:
                return True
            latest = current_release_review(
                control_plane_root=control_plane_root, record_store=store, profile=profile
            )
            if (
                not _ready(latest, profile)
                or latest.checklist_digest != checklist_digest
                or store.read_product_profile_record(profile.product) != profile
                or _last_client_decision(store, profile) != last_decision
            ):
                return False
            if dry_run:
                return True
            body = "\n".join(
                (
                    adoption_marker,
                    issue_marker,
                    candidate_marker,
                    request_marker,
                    ADOPTED_ORIGINAL_PREFIX + str(comment_id) + " -->",
                    *(
                        f"<!-- launchplane:release-client-change:{key} -->"
                        for key, change in sorted(changes.items())
                        if change.client_review_requested
                    ),
                    "",
                    "Existing release request adopted; no new Client action or decision.",
                    "Adoption reason: "
                    + json.dumps(reason).replace("<", "\\u003c").replace("@", "@\u200b"),
                    f"https://github.com/{profile.repository}/issues/{issue_number}#issuecomment-{comment_id}",
                )
            )
            result = github_api_request(
                path=comments_path, token=token, method="POST", body={"body": body}
            )
            return bool(
                isinstance(result, dict)
                and isinstance(result.get("id"), int)
                and github_app_authored(
                    result,
                    app_id,
                    lookup_app=lambda slug: github_api_request(
                        path=f"/apps/{quote(slug)}", token=token
                    ),
                )
            )
    return False


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
