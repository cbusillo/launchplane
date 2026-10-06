"""Ask the Client to accept a complete release, once per testing candidate."""

from dataclasses import dataclass, field
import hashlib
import re
from pathlib import Path
from time import monotonic
from urllib.parse import quote

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import ReleaseReviewStatus, ReleaseVersion
from control_plane.release_review import (
    CLIENT_APPROVAL_REQUIRED,
    ReleaseReviewStore,
    checklist_blockers,
    current_release_review,
)
from control_plane.service_human_auth import launchplane_public_origin_from_env
from control_plane.workflows.launchplane import github_api_request, resolve_launchplane_github_token


@dataclass(slots=True)
class ReleaseInvitationBackoff:
    next_reads: dict[str, tuple[str, float]] = field(default_factory=dict)


def release_request_issue_marker(product: str) -> str:
    return f"<!-- launchplane:release-requests:{hashlib.sha256(product.encode()).hexdigest()} -->"


def release_invitation_marker(product: str, candidate: ReleaseVersion) -> str:
    key = hashlib.sha256((product + candidate.model_dump_json()).encode()).hexdigest()
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


def _pages(path: str, token: str) -> list[dict[str, object]]:
    records: list[dict[str, object]] = []
    separator = "&" if "?" in path else "?"
    for page in range(1, 11):
        result = github_api_request(path=f"{path}{separator}per_page=100&page={page}", token=token)
        if not isinstance(result, list) or any(not isinstance(item, dict) for item in result):
            raise ValueError("Release invitation lookup is incomplete.")
        records.extend(result)
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
    if not re.fullmatch(
        r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?", profile.owner.github_login
    ):
        raise ValueError("Release invitation requires a GitHub login.")
    # Do not compile a large checklist on every worker poll. This is a read
    # throttle only; GitHub's candidate marker remains the durable receipt.
    fingerprint = profile.model_dump_json()
    if backoff is not None:
        previous = backoff.next_reads.get(profile.product)
        if previous is not None and previous[0] == fingerprint and monotonic() < previous[1]:
            return
        backoff.next_reads[profile.product] = (fingerprint, monotonic() + 300)
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
        token = resolve_launchplane_github_token(
            control_plane_root=control_plane_root,
            context_name=lane.context,
            repository=profile.repository,
            purpose="release_record",
        )
        if not token:
            raise ValueError("Release invitation source-control access is unavailable.")
        path = f"/repos/{quote(profile.repository)}/issues"
        issues = [
            issue
            for issue in _pages(f"{path}?state=all&sort=created&direction=desc", token)
            if "pull_request" not in issue
            and issue_marker in str(issue.get("body", "")).splitlines()
        ]
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
        if any(
            marker in str(comment.get("body", "")).splitlines()
            for comment in _pages(comments_path, token)
        ):
            return
        # Recompile after destination lookup: a concurrent acceptance or testing
        # deploy must not receive an invitation for the obsolete snapshot.
        if store.read_product_profile_record(profile.product) != profile:
            return
        latest = current_release_review(
            control_plane_root=control_plane_root, record_store=store, profile=profile
        )
        if not _ready(latest, profile) or latest.checklist_digest != review.checklist_digest:
            return
        effect = (
            "Accepting records your approval; releases are held until an admin releases the hold."
            if profile.release_on_acceptance == "held"
            else "Accepting starts the release to the production site, with a verified backup, checks and automatic rollback."
        )
        body = (
            f"{marker}\n\n@{profile.owner.github_login} this release is ready for you to review:\n\n"
            f"{origin}/ui/owner-review?product={quote(profile.product, safe='')}\n\n{effect}"
        )
        result = github_api_request(
            path=comments_path, token=token, method="POST", body={"body": body}
        )
        if not isinstance(result, dict) or not isinstance(result.get("id"), int):
            raise ValueError("Release invitation publication was not confirmed.")
