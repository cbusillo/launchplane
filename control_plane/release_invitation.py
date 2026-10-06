"""Ask the Client to accept a complete release, once per testing candidate."""

from dataclasses import dataclass, field
import argparse
import hashlib
import json
import logging
import re
from pathlib import Path
from threading import Event
from time import monotonic
from urllib.parse import quote

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.release_review import ReleaseReviewStatus, ReleaseVersion
from control_plane.github_payload import github_app_authored
from control_plane.launchplane_github_delivery import resolve_delivery_github_app_id
from control_plane.release_review import (
    CLIENT_APPROVAL_REQUIRED,
    ReleaseReviewStore,
    checklist_blockers,
    current_release_review,
    release_version,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.service_human_auth import launchplane_public_origin_from_env
from control_plane.workflows.launchplane import github_api_request, resolve_launchplane_github_token

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class ReleaseInvitationBackoff:
    next_reads: dict[str, tuple[str, float]] = field(default_factory=dict)
    delivered: set[tuple[str, str, str]] = field(default_factory=set)
    diagnostics: dict[str, tuple[type[Exception], float]] = field(default_factory=dict)


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
    path: str, token: str, *, marker: str, app_id: int, issues_only: bool = False
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
            and (not marker or marker in str(item.get("body", "")).splitlines())
        ]
        matching = []
        for item in marked:
            if github_app_authored(item, app_id):
                matching.append(item)
            elif issues_only:
                # Manual issues may have a human author. Adoption requires an
                # App-authored attestation on that issue, without a new mention.
                number = item.get("number")
                if (
                    isinstance(number, int)
                    and number > 0
                    and _pages(
                        f"{path.split('?')[0]}/{number}/comments",
                        token,
                        marker=marker,
                        app_id=app_id,
                    )
                ):
                    matching.append(item)
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
    receipt = (
        profile.product,
        profile.repository,
        release_invitation_marker(profile.product, candidate),
    )
    if backoff is not None and receipt in backoff.delivered:
        return
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
        if previous_fingerprint == fingerprint and monotonic() < next_read:
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
        token = resolve_launchplane_github_token(
            control_plane_root=control_plane_root,
            context_name=lane.context,
            repository=profile.repository,
            purpose="release_record",
        )
        if not token:
            raise ValueError("Release invitation source-control access is unavailable.")
        app_id = resolve_delivery_github_app_id(control_plane_root=control_plane_root)
        path = f"/repos/{quote(profile.repository)}/issues"
        issues = _pages(
            f"{path}?state=all&sort=updated&direction=desc",
            token,
            marker=issue_marker,
            app_id=app_id,
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
        if _pages(comments_path, token, marker=marker, app_id=app_id):
            if backoff is not None:
                backoff.delivered.add((profile.product, profile.repository, marker))
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
        # Reuse the review page's authority calculation, including unsupported
        # drivers and the one-time rollback drill. Resolve it only after the
        # final review check, immediately before composing the request.
        from control_plane.client_release import release_start_for_acceptance

        mode = release_start_for_acceptance(store=store, profile=profile)
        effect = "Accepting records your approval; an admin starts the release."
        if mode:
            effect = "Accepting starts the release to the production site, with a verified backup, checks and automatic rollback."
            if mode == "promote_with_rollback_drill":
                effect = "Accepting starts the release to the production site and its rollback-and-re-release drill, with verified backups, checks and automatic rollback."
        body = (
            f"{marker}\n\n@{profile.owner.github_login} this release is ready for you to review:\n\n"
            f"{origin}/ui/owner-review?product={quote(profile.product, safe='')}\n\n{effect}"
        )
        result = github_api_request(
            path=comments_path, token=token, method="POST", body={"body": body}
        )
        if not isinstance(result, dict) or not isinstance(result.get("id"), int):
            raise ValueError("Release invitation publication was not confirmed.")
        if backoff is not None:
            backoff.delivered.add((profile.product, profile.repository, marker))


def advance_release_invitations(
    *,
    store: object,
    control_plane_root: Path,
    backoff: ReleaseInvitationBackoff,
    stop_event: Event | None = None,
) -> None:
    """Deliver on a separate worker thread; isolate and bound repeated diagnostics."""
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
            previous_type, next_warning = backoff.diagnostics.get(profile.product, (Exception, 0))
            now = monotonic()
            if type(error) is not previous_type or now >= next_warning:
                _LOGGER.warning(
                    "release invitation unavailable product=%s error_type=%s",
                    profile.product,
                    type(error).__name__,
                )
                backoff.diagnostics[profile.product] = (type(error), now + 300)
        else:
            backoff.diagnostics.pop(profile.product, None)


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
