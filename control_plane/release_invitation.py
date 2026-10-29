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
from typing import TYPE_CHECKING, cast
from urllib.parse import quote
from zoneinfo import ZoneInfo
from control_plane.client_test_notes import _markdown_lines
from control_plane.github_payload import github_app_authored
from control_plane.launchplane_github_delivery import resolve_delivery_github_app_id

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import (
    ReleaseChecklist,
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

if TYPE_CHECKING:
    from control_plane.client_release import ClientReleaseRunView

WEBSITE_SETTINGS_REVIEW_NOTE = (
    "- Website settings: review the prepared search indexing, analytics and cookie "
    "consent settings on the release page. They apply only after you accept."
)
_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ReleaseInvitationBackoff:
    next_reads: dict[str, tuple[str, float]] = field(default_factory=dict)
    delivered: dict[tuple[str, str], tuple[str, float]] = field(default_factory=dict)
    diagnostics: dict[str, tuple[type[Exception], float]] = field(default_factory=dict)
    release_records: dict[str, float] = field(default_factory=dict)
    release_waits: dict[str, tuple[str, float]] = field(default_factory=dict)
    legacy_retirements: dict[str, tuple[str, float]] = field(default_factory=dict)


@dataclass(frozen=True)
class LegacyReleaseRequest:
    issue: dict[str, object]
    request: dict[str, object] | None
    due: datetime | None
    reminded: bool


def _client_release_requests_enabled(
    profile: LaunchplaneProductProfileRecord, *, include_director_standing: bool = False
) -> bool:
    return (
        profile.is_active
        and profile.production_use != "prelaunch"
        and profile.owner.is_set
        and (include_director_standing or profile.release_on_acceptance != "director_standing")
    )


def advance_release_invitations(
    *,
    store: object,
    control_plane_root: Path,
    stop_event: Event | None = None,
    backoff: ReleaseInvitationBackoff | None = None,
) -> None:
    """Cold notification reads never occupy the accepted-release worker."""
    from control_plane.storage.postgres import PostgresRecordStore

    if not isinstance(store, PostgresRecordStore):
        return
    for profile in store.list_product_profile_records():
        if stop_event is not None and stop_event.is_set():
            break
        try:
            _settle_release_records(
                store=store,
                control_plane_root=control_plane_root,
                profile=profile,
                backoff=backoff,
                stop_event=stop_event,
            )
            from control_plane.release_event_waits import advance_product_release_waits

            try:
                advance_product_release_waits(
                    store=store,
                    control_plane_root=control_plane_root,
                    profile=profile,
                    next_checks=backoff.release_waits if backoff is not None else None,
                    should_stop=stop_event.is_set if stop_event is not None else lambda: False,
                )
            except Exception as error:
                _LOGGER.warning(
                    "release waits unavailable product=%s error_type=%s",
                    profile.product,
                    type(error).__name__,
                )
            if _client_release_requests_enabled(profile):
                if profile.release_request is not None:
                    publish_release_invitation(
                        store=store,
                        control_plane_root=control_plane_root,
                        profile=profile,
                        backoff=backoff,
                    )
                _retire_decided_legacy_requests(
                    store=cast(ReleaseReviewStore, cast(object, store)),
                    control_plane_root=control_plane_root,
                    profile=profile,
                    backoff=backoff,
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


def _settings_invitation_key(checklist: ReleaseChecklist) -> str:
    settings = checklist.candidate.runtime_settings
    if settings is not None:
        if settings != checklist.production.runtime_settings:
            return hashlib.sha256(
                json.dumps(settings.model_dump(), sort_keys=True).encode()
            ).hexdigest()
    if (
        checklist.website_settings is None
        or checklist.candidate.website_settings_sha256
        == checklist.production.website_settings_sha256
    ):
        return ""
    return hashlib.sha256(
        f"website-settings:{checklist.candidate.website_settings_sha256}".encode()
    ).hexdigest()


def _settings_review_note(checklist: ReleaseChecklist) -> str:
    if checklist.candidate.runtime_settings is not None:
        return "Review the prepared production settings on the release page. Rollback restores the previous settings with the previous version."
    return WEBSITE_SETTINGS_REVIEW_NOTE


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


REPLACED_MARKER = "<!-- launchplane:release-invitation-replaced -->"
COMPLETED_PREFIX = "<!-- launchplane:release-invitation-completed:"
WHY_NOW = "**Why now:** "
ADOPTED_ORIGINAL_PREFIX = "<!-- launchplane:release-adopted-original:"


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


def _accepted_release_in_progress(
    store: ReleaseReviewStore,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord | None,
    production: ReleaseVersion,
    candidate: ReleaseVersion,
) -> bool:
    if decision is None or decision.decision != "accepted" or not decision.release_start:
        return False
    from control_plane.client_release import read_client_release_run

    run = read_client_release_run(store=store, profile=profile, decision=decision)
    return run is not None and (
        any(step.status in {"pending", "running", "reconciliation_required"} for step in run.steps)
        or (
            run.state == "waiting"
            and (
                any(step.status == "pass" for step in run.steps)
                or (
                    production == decision.checklist.production
                    and candidate == decision.checklist.candidate
                )
            )
        )
    )


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
    identity: tuple[str, ...] = (
        product,
        candidate.artifact_id,
        candidate.source_commit,
        candidate.shared_addons_digest,
    )
    if candidate.website_settings_sha256:
        identity += (candidate.website_settings_sha256,)
    key = hashlib.sha256(json.dumps(identity, separators=(",", ":")).encode()).hexdigest()
    return f"<!-- launchplane:release-invitation:{key} -->"


def _release_invitation_markers(product: str, candidate: ReleaseVersion) -> set[str]:
    return {
        release_invitation_marker(product, candidate),
        release_invitation_marker(
            product, candidate.model_copy(update={"website_settings_sha256": ""})
        ),
    }


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
        and not checklist_blockers(checklist, profile=profile)
        and review.blockers == (CLIENT_APPROVAL_REQUIRED,)
    )


def _pages(
    path: str,
    token: str,
    *,
    app_id: int | None = None,
    marker: str = "",
    issues_only: bool = False,
    created_after: datetime | None = None,
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
                or (
                    isinstance(item.get("body"), str)
                    and marker in (_comment_lines(item) if app_id is None else _receipt_lines(item))
                )
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
        if len(result) < 100 or (
            created_after is not None
            and any(
                isinstance(item.get("created_at"), str)
                and _timestamp(item["created_at"]) < created_after.replace(microsecond=0)
                for item in result
            )
        ):
            return records
    raise ValueError("Release invitation lookup exceeds the supported size.")


def _close_issue(*, path: str, token: str, number: int) -> None:
    result = github_api_request(path=path, token=token, method="PATCH", body={"state": "closed"})
    if (
        not isinstance(result, dict)
        or result.get("number") != number
        or result.get("state") != "closed"
    ):
        raise ValueError("Release issue closure was not confirmed.")


def _finished(run: "ClientReleaseRunView") -> bool:
    if run.state == "passed":
        return True
    return (
        run.state == "stopped"
        and any(step.status in {"fail", "cancelled"} for step in run.steps)
        and not any(
            step.status in {"pending", "running", "reconciliation_required"} for step in run.steps
        )
    )


def _settle_release_records(
    *,
    store: ReleaseReviewStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    backoff: ReleaseInvitationBackoff | None,
    stop_event: Event | None,
) -> None:
    for decision in store.list_release_review_decision_records(product=profile.product):
        if stop_event is not None and stop_event.is_set():
            break
        if (
            decision.decision == "temporary_hotfix"
            or not decision.release_start
            or not decision.release_issue_url
            or decision.checklist.repository != profile.repository
        ):
            continue
        if backoff is not None:
            if monotonic() < backoff.release_records.get(decision.record_id, 0):
                continue
            backoff.release_records[decision.record_id] = monotonic() + 300
        try:
            settled = _settle_release_record(
                store=store,
                control_plane_root=control_plane_root,
                profile=profile,
                decision=decision,
            )
        except Exception as error:
            _LOGGER.warning(
                "release record completion unavailable product=%s record=%s error_type=%s",
                profile.product,
                decision.record_id,
                type(error).__name__,
            )
            continue
        if settled and backoff is not None:
            backoff.release_records[decision.record_id] = float("inf")


def _stopped_release_outcome(
    *,
    store: ReleaseReviewStore,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
    run: "ClientReleaseRunView",
) -> str:
    names = {
        "backup": "verified backup",
        "promote": "deployment of the reviewed version",
        "rollback": "rollback safety check"
        if run.rollback_drill
        else "rollback to the last accepted release",
        "recovery": "automatic recovery",
    }
    failures = [
        f"The {names[step.kind]} step ({step.step}) stopped: "
        + (step.failure.reason if step.failure else "No failure reason was recorded for this step.")
        for step in run.steps
        if step.status in {"fail", "cancelled"}
    ]
    try:
        production = release_version(store=store, profile=profile, instance="prod")
    except (FileNotFoundError, ValueError):
        live = "The version currently recorded in production could not be verified."
    else:
        version = (
            ("rejected" if decision.release_start == "rollback" else "reviewed")
            if production == decision.checklist.candidate
            else "previous"
            if production == decision.checklist.production
            else "another"
        )
        live = (
            f"Production is currently recorded as running the {version} version "
            f"({production.source_commit[:7]})."
        )
    return "\n\n".join([*failures, live]).replace("@", "@\u200b")


def _settle_release_record(
    *,
    store: ReleaseReviewStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    decision: ReleaseReviewDecisionRecord,
) -> bool:
    from control_plane.client_release import read_client_release_run
    from control_plane.release_review_record import release_decision_marker

    settled = False
    with store.release_review_publication_lock(record_id=decision.record_id):
        run = read_client_release_run(store=store, profile=profile, decision=decision)
        if run is None or run.decision_record_id != decision.record_id or not _finished(run):
            return False
        outcome = (
            ("Rolled back" if decision.release_start == "rollback" else "Released")
            if run.state == "passed"
            else "Failed"
        )
        match = re.fullmatch(
            rf"https://github\.com/{re.escape(profile.repository)}/issues/([1-9][0-9]*)",
            decision.release_issue_url,
        )
        if match is None:
            raise ValueError("Release record destination does not match its repository.")
        number = int(match[1])
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
                raise ValueError("Release record source-control access is unavailable.")
            path = f"/repos/{quote(profile.repository)}/issues/{number}"
            issue = github_api_request(path=path, token=token)
            if (
                not isinstance(issue, dict)
                or issue.get("number") != number
                or "pull_request" in issue
                or not isinstance(issue.get("body"), str)
            ):
                raise ValueError("Release record identity was not confirmed.")
            app_id = resolve_delivery_github_app_id(control_plane_root=control_plane_root)
            if release_decision_marker(decision.record_id) not in _receipt_lines(issue):
                decisions = _pages(
                    f"{path}/comments",
                    token,
                    app_id=app_id,
                    marker=release_decision_marker(decision.record_id),
                )
                if not decisions:
                    raise ValueError("Release record identity was not confirmed.")
            if issue.get("state") == "closed":
                return True
            if (
                store.read_product_profile_record(profile.product) != profile
                or store.read_release_review_decision_record(
                    product=profile.product, record_id=decision.record_id
                )
                != decision
                or read_client_release_run(store=store, profile=profile, decision=decision) != run
            ):
                return False
            marker = f"<!-- launchplane:release-outcome:{decision.record_id} -->"
            comments = _pages(
                f"{path}/comments",
                token,
                app_id=resolve_delivery_github_app_id(control_plane_root=control_plane_root),
                marker=marker,
            )
            if not any(marker in _receipt_lines(comment) for comment in comments):
                details = (
                    "\n\n"
                    + _stopped_release_outcome(
                        store=store, profile=profile, decision=decision, run=run
                    )
                    if run.state == "stopped"
                    else ""
                )
                result = github_api_request(
                    path=f"{path}/comments",
                    token=token,
                    method="POST",
                    body={"body": f"{marker}\n{outcome}: this release has finished.{details}\n"},
                )
                if not isinstance(result, dict) or not isinstance(result.get("id"), int):
                    raise ValueError("Release outcome publication was not confirmed.")
            active = False
            saved_decisions = store.list_release_review_decision_records(product=profile.product)
            for other in saved_decisions:
                if (
                    other.record_id == decision.record_id
                    or other.release_issue_url != decision.release_issue_url
                    or not other.release_start
                ):
                    continue
                if other.decision == "temporary_hotfix" and not any(
                    item.decision in {"accepted", "changes_requested"}
                    and item.checklist_digest == other.checklist_digest
                    and (_timestamp(item.decided_at), item.record_id)
                    > (_timestamp(other.decided_at), other.record_id)
                    for item in saved_decisions
                ):
                    active = True
                    break
                other_run = read_client_release_run(store=store, profile=profile, decision=other)
                if (
                    other_run is None
                    or other_run.decision_record_id != other.record_id
                    or not _finished(other_run)
                ):
                    active = True
                    break
            if not active:
                _close_issue(path=path, token=token, number=number)
            settled = True
    return settled


def publish_release_invitation(
    *,
    store: ReleaseReviewStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    backoff: ReleaseInvitationBackoff | None = None,
    send_reminder: bool = True,
) -> bool:
    origin = launchplane_public_origin_from_env()
    if not origin or not _client_release_requests_enabled(profile):
        return False
    production = release_version(store=store, profile=profile, instance="prod")
    candidate = release_version(store=store, profile=profile, instance="testing")
    request_decision = profile.release_request
    if request_decision is None or production == candidate:
        return False
    last_decision = _last_client_decision(store, profile)
    if (
        request_decision.product != profile.product
        or request_decision.repository != profile.repository
        or request_decision.owner_github_id != profile.owner.github_id
        or request_decision.production != production
        or request_decision.candidate != candidate
        or request_decision.client_decision_record_id
        != (last_decision.record_id if last_decision else "")
    ):
        return False
    request_marker = _request_marker(profile, last_decision)
    decision_marker = f"<!-- launchplane:release-request-decision:{request_decision.record_id} -->"
    receipt = (
        profile.product,
        profile.repository,
        request_marker
        + request_decision.record_id
        + release_invitation_marker(profile.product, candidate),
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
    if _accepted_release_in_progress(store, profile, last_decision, production, candidate):
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
        if review.checklist_digest != request_decision.checklist_digest:
            return False
        assert review.checklist is not None
        client_changes = client_invitation_changes(store, profile, review.checklist)
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
            marker = release_candidate_issue_marker(request_decision.record_id)
            product_marker = release_issue_product_marker(profile.product)
            app_id = resolve_delivery_github_app_id(control_plane_root=control_plane_root)
            issues = _pages(
                f"{path}?state=open&sort=updated&direction=desc",
                token,
                marker=release_request_issue_marker(profile.product),
                issues_only=True,
            )
            legacy = _legacy_release_requests(
                issues=issues, profile=profile, path=path, token=token, last_decision=last_decision
            )
            carried = next(
                (
                    item
                    for item in legacy
                    if item.request is not None
                    and _release_invitation_markers(profile.product, candidate).intersection(
                        _comment_lines(item.request)
                    )
                ),
                None,
            )
            if store.read_product_profile_record(profile.product) != profile:
                return False
            latest = current_release_review(
                control_plane_root=control_plane_root, record_store=store, profile=profile
            )
            if (
                not _ready(latest, profile)
                or latest.checklist_digest != review.checklist_digest
                or _last_client_decision(store, profile) != last_decision
                or _accepted_release_in_progress(
                    store, profile, last_decision, production, candidate
                )
            ):
                return False
            identity = release_candidate_identity(profile, review.checklist_digest)
            with store.release_review_publication_lock(
                record_id=f"candidate:{profile.product}:{identity}"
            ):
                candidates = _pages(
                    f"{path}?state=all&sort=created&direction=desc",
                    token,
                    marker=marker,
                    app_id=app_id,
                    issues_only=True,
                    created_after=_timestamp(request_decision.decided_at),
                )
                if len(candidates) > 1 or any(
                    _receipt_lines(item)[:1] != [marker] for item in candidates
                ):
                    raise ValueError("Release candidate issue identity is ambiguous.")
                if (
                    store.read_product_profile_record(profile.product) != profile
                    or _last_client_decision(store, profile) != last_decision
                    or _accepted_release_in_progress(
                        store, profile, last_decision, production, candidate
                    )
                ):
                    return False
                issue = candidates[0] if candidates else None
                body = _release_invitation_body(
                    store=store,
                    profile=profile,
                    checklist=review.checklist,
                    changes=client_changes,
                    origin=origin,
                    digest=request_decision.record_id,
                )
                if issue is None:
                    if carried is not None:
                        assert carried.request is not None and carried.due is not None
                        due = carried.due
                        reminder_marker = marker.replace("release-candidate:", "release-reminder:")
                        legacy_number = _release_issue_number(carried.issue)
                        body = (
                            f"{marker}\n{product_marker}\n{decision_marker}\n"
                            + f"<!-- launchplane:release-reminder-due:{due.isoformat()} -->\n"
                            + (f"{reminder_marker}\n" if carried.reminded else "")
                            + f"\nExisting [review request](https://github.com/{profile.repository}/issues/{legacy_number}#issuecomment-{_release_comment_id(carried.request)}) carried over. Use the same release page; no second decision is needed.\n\n"
                            + "\n".join(_comment_lines(carried.request)).replace("@", "@\u200b")
                        )
                    created = github_api_request(
                        path=path,
                        token=token,
                        method="POST",
                        body={
                            "title": f"Release {candidate.source_commit[:7]}: review requested",
                            "body": body,
                        },
                    )
                    if not isinstance(created, dict) or not isinstance(created.get("number"), int):
                        raise ValueError("Release candidate issue creation was not confirmed.")
                    issue = created
            number = _release_issue_number(issue)
            if (
                issue.get("state") == "open"
                and issue.get("body") != body
                and not any(
                    line.startswith(
                        (
                            "<!-- launchplane:release-reminder-due:",
                            "<!-- launchplane:release-decision:",
                        )
                    )
                    for line in _receipt_lines(issue)
                )
            ):
                result = github_api_request(
                    path=f"{path}/{number}", token=token, method="PATCH", body={"body": body}
                )
                if not isinstance(result, dict) or result.get("number") != number:
                    raise ValueError("Release invitation update was not confirmed.")
                issue = result
            url = f"https://github.com/{profile.repository}/issues/{number}"
            open_issues = _pages(f"{path}?state=open&sort=created&direction=desc", token)
            decided_urls = {
                item.release_issue_url
                for item in store.list_release_review_decision_records(product=profile.product)
                if item.release_issue_url
            }
            _retire_older_release_issues(
                issues=open_issues,
                decided_urls=decided_urls,
                current=issue,
                product_marker=product_marker,
                legacy=legacy,
                url=url,
                path=path,
                token=token,
                app_id=app_id,
            )
            if issue.get("state") == "closed":
                return True
            due_line = next(
                (
                    line
                    for line in _receipt_lines(issue)
                    if line.startswith("<!-- launchplane:release-reminder-due:")
                ),
                "",
            )
            due = (
                _timestamp(
                    due_line.removeprefix("<!-- launchplane:release-reminder-due:").removesuffix(
                        " -->"
                    )
                )
                if due_line
                else _timestamp(issue.get("created_at")) + timedelta(days=3)
            )
            reminder_marker = marker.replace("release-candidate:", "release-reminder:")
            comments = (
                _pages(f"{path}/{number}/comments", token, app_id=app_id, marker=reminder_marker)
                if _now() >= due and send_reminder
                else []
            )
            reminded = reminder_marker in _receipt_lines(issue) or bool(comments)
            if _now() >= due and send_reminder and not reminded:
                result = github_api_request(
                    path=f"{path}/{number}/comments",
                    token=token,
                    method="POST",
                    body={
                        "body": f"{reminder_marker}\nA reminder: hi @{profile.owner.github_login}, [open the release page]({origin}/ui/owner-review?product={quote(profile.product, safe='')}) to Accept or Request changes on this release."
                    },
                )
                if not isinstance(result, dict) or not isinstance(result.get("id"), int):
                    raise ValueError("Release invitation reminder was not confirmed.")
                reminded = True
            _remember_delivery(backoff, receipt, due, reminded)
            return True
    return False


def _release_invitation_body(
    *,
    store: ReleaseReviewStore,
    profile: LaunchplaneProductProfileRecord,
    checklist: ReleaseChecklist,
    changes: dict[str, InvitationChange],
    origin: str,
    digest: str,
) -> str:
    request_decision = profile.release_request
    assert request_decision is not None
    settings_key = _settings_invitation_key(checklist)
    change_links = [_change_link(change) for change in changes.values()]
    if settings_key:
        change_links.append(_settings_review_note(checklist))
    from control_plane.client_release import release_start_for_acceptance

    mode = release_start_for_acceptance(store=store, profile=profile)
    effect = "Accepting records your approval; an admin starts the release."
    if mode:
        effect = "Accepting starts the release to the production site, with a verified backup, checks and automatic rollback."
        if mode == "promote_with_rollback_drill":
            effect = "Accepting starts the release to the production site and its rollback-and-re-release drill, with verified backups, checks and automatic rollback."
    review_link = f"{origin}/ui/owner-review?product={quote(profile.product, safe='')}"
    notes = change_links or ["- Engineering updates only; no new Client change needs checking."]
    action = (
        "check the changes on the testing site"
        if changes
        else "there is nothing new to test. Read the release summary"
    )
    if settings_key:
        action = "review the prepared production settings on the release page" + (
            " and " + action if changes else ""
        )
    return (
        f"{release_candidate_issue_marker(digest)}\n{release_issue_product_marker(profile.product)}\n"
        + f"<!-- launchplane:release-request-decision:{request_decision.record_id} -->\n"
        + release_invitation_marker(profile.product, checklist.candidate)
        + "\n\n**Release review** — is the site working with these changes, so they can go live?\n\n"
        + f"Hi @{profile.owner.github_login},\n\n"
        + WHY_NOW
        + " ".join(request_decision.reason.split()).replace("@", "@\u200b")
        + "\n\nWhat to check in this release:\n"
        + "\n".join(notes)
        + f"\n\nStill one thing to do: [open the release page]({review_link}), {action}, then press **Accept** or **Request changes**. The release page lists every included change."
        + f"\n\n{effect}\n\nUpdated at: {_timestamp(request_decision.decided_at).astimezone(ZoneInfo('America/New_York')).strftime('%B %-d, %Y at %-I:%M %p ET')}"
    )


def release_candidate_identity(profile: LaunchplaneProductProfileRecord, digest: str) -> str:
    request = profile.release_request
    return (
        request.record_id if request is not None and request.checklist_digest == digest else digest
    )


def release_candidate_issue_marker(digest: str) -> str:
    return f"<!-- launchplane:release-candidate:{digest} -->"


def release_issue_product_marker(product: str) -> str:
    return f"<!-- launchplane:release-product:{hashlib.sha256(product.encode()).hexdigest()} -->"


def _release_comment_id(comment: dict[str, object]) -> int:
    comment_id = comment.get("id")
    if not isinstance(comment_id, int) or comment_id < 1:
        raise ValueError("Release invitation comment identity is unavailable.")
    return comment_id


def _release_issue_number(issue: dict[str, object]) -> int:
    number = issue.get("number")
    if not isinstance(number, int) or number < 1:
        raise ValueError("Release candidate issue number is unavailable.")
    return number


def _legacy_release_requests(
    *,
    issues: list[dict[str, object]],
    profile: LaunchplaneProductProfileRecord,
    path: str,
    token: str,
    last_decision: ReleaseReviewDecisionRecord | None,
) -> list[LegacyReleaseRequest]:
    legacy = []
    for issue in issues:
        if not isinstance(issue.get("body"), str):
            continue
        lines = _receipt_lines(issue)
        if (
            "pull_request" in issue
            or any(line.startswith("<!-- launchplane:release-candidate:") for line in lines[:1])
            or "<!-- launchplane:release-requests-retired -->" in lines
            or release_request_issue_marker(profile.product) not in lines
        ):
            continue
        number = _release_issue_number(issue)
        comments = [
            comment
            for comment in _pages(f"{path}/{number}/comments", token)
            if REPLACED_MARKER not in _comment_lines(comment)
            and not any(line.startswith(COMPLETED_PREFIX) for line in _comment_lines(comment))
        ]
        request_marker = _request_marker(profile, last_decision)
        request = _open_request(comments, request_marker, last_decision)
        current = [
            comment
            for comment in comments
            if request_marker in _comment_lines(comment)
            or (request is not None and comment.get("id") == request.get("id"))
        ]
        due = min((_timestamp(comment.get("created_at")) for comment in current), default=None)
        reminder_marker = request_marker.replace("release-request:", "release-reminder:")
        reminded = any(reminder_marker in _receipt_lines(comment) for comment in current)
        legacy.append(
            LegacyReleaseRequest(issue, request, due + timedelta(days=3) if due else None, reminded)
        )
    return legacy


def _retire_older_release_issues(
    *,
    issues: list[dict[str, object]],
    current: dict[str, object],
    product_marker: str,
    legacy: list[LegacyReleaseRequest],
    decided_urls: set[str],
    url: str,
    path: str,
    token: str,
    app_id: int,
) -> None:
    older = [
        issue
        for issue in issues
        if "pull_request" not in issue
        and issue.get("number") != current.get("number")
        and issue.get("state") == "open"
        and isinstance(issue.get("body"), str)
        and product_marker in _receipt_lines(issue)
        and f"https://github.com{path.removeprefix('/repos')}/{_release_issue_number(issue)}"
        not in decided_urls
        and github_app_authored(
            issue,
            app_id,
            lookup_app=lambda slug: github_api_request(path=f"/apps/{quote(slug)}", token=token),
        )
    ]
    older.extend(item.issue for item in legacy)
    for issue in older:
        _retire_release_issue(
            issue=issue,
            url=url,
            path=path,
            token=token,
            legacy=any(item.issue == issue for item in legacy),
        )


def _retire_release_issue(
    *, issue: dict[str, object], url: str, path: str, token: str, legacy: bool
) -> None:
    number = _release_issue_number(issue)
    updated_body = (
        "\n".join(_comment_lines(issue))
        + ("\n<!-- launchplane:release-requests-retired -->" if legacy else "")
        + f"\n\nContinue in the release review at {url}. Use that issue and the same release page; no second decision is needed."
    )
    result = github_api_request(
        path=f"{path}/{number}",
        token=token,
        method="PATCH",
        body={"body": updated_body, "state": "closed"},
    )
    if (
        not isinstance(result, dict)
        or result.get("number") != number
        or result.get("state") != "closed"
    ):
        raise ValueError("Replaced release issue closure was not confirmed.")


def _retire_decided_legacy_requests(
    *,
    store: ReleaseReviewStore,
    control_plane_root: Path,
    profile: LaunchplaneProductProfileRecord,
    backoff: ReleaseInvitationBackoff | None,
) -> None:
    decision = _last_client_decision(store, profile)
    if decision is None or not decision.release_issue_url:
        return
    fingerprint = profile.model_dump_json() + decision.model_dump_json()
    if backoff is not None:
        previous, next_read = backoff.legacy_retirements.get(profile.product, ("", 0))
        if previous == fingerprint and monotonic() < next_read:
            return
        backoff.legacy_retirements[profile.product] = (fingerprint, monotonic() + 300)
    with store.release_review_publication_lock(record_id=f"invitation:{profile.product}"):
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
                raise ValueError("Release record source-control access is unavailable.")
            path = f"/repos/{quote(profile.repository)}/issues"
            issues = _pages(
                f"{path}?state=all&sort=updated&direction=desc",
                token,
                marker=release_request_issue_marker(profile.product),
                issues_only=True,
            )
            legacy = _legacy_release_requests(
                issues=issues, profile=profile, path=path, token=token, last_decision=decision
            )
            if (
                store.read_product_profile_record(profile.product) != profile
                or _last_client_decision(store, profile) != decision
            ):
                return
            for item in legacy:
                if item.request is None:
                    _retire_release_issue(
                        issue=item.issue,
                        url=decision.release_issue_url,
                        path=path,
                        token=token,
                        legacy=True,
                    )
        if backoff is not None:
            backoff.legacy_retirements[profile.product] = (fingerprint, float("inf"))


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
        notification_keys = {
            key for key, change in changes.items() if change.client_review_requested
        }
        if settings_key := _settings_invitation_key(review.checklist):
            notification_keys.add(settings_key)
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
                or not _release_invitation_markers(
                    profile.product, review.checklist.candidate
                ).intersection(_receipt_lines(original))
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
            candidates = _pages(
                f"{path}?state=all&sort=updated&direction=desc",
                token,
                app_id=app_id,
                marker=release_candidate_issue_marker(
                    release_candidate_identity(profile, checklist_digest)
                ),
                issues_only=True,
            )
            if candidates:
                return False
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
                        for key in sorted(notification_keys)
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
