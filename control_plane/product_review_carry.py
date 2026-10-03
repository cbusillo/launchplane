"""Carry a Client's acceptance across a base-only refresh made by the merge train.

The Client accepts a change, seen on its preview. When the merge train only merges
the base branch into the pull request, the change is the same, so asking again
protects nothing. The acceptance carries to the new head only when all hold:

1. Every commit between the accepted head and the new head is the train's own merge
   of the base branch: exactly two parents, the first is the previous head, the
   second is on the pull request's current base branch, and the train recorded that
   it asked the provider to refresh this pull request from that previous head on
   that base branch, no later than the commit was made.
2. The pull request's change against its base is byte-identical at both heads:
   every changed file has the same name, status, resulting blob, and patch.
3. The carry is saved as its own decision record naming the decision and head it
   came from and the train's refresh records, so it reads as carried, not re-decided.

Anything else (another commit, a conflict resolution, a different base, a new
Client, an unreadable or truncated diff) leaves the new head waiting for a decision.
"""

from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import logging
from typing import Final, cast
from urllib.parse import quote
from uuid import uuid4

from control_plane.contracts.merge_train_branch_refresh_record import (
    MergeTrainBranchRefreshRecord,
    requested_at_datetime,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_review import ProductReviewCarry, ProductReviewDecisionRecord
from control_plane.merge_train_branch_refresh import MergeTrainBranchRefreshReadStore
from control_plane.product_review import ProductReviewStore

# Clock difference allowed between Launchplane and the provider that made the commit.
CLOCK_SKEW: Final = timedelta(seconds=60)
# A busy train may refresh a pull request more than once before anyone looks.
MAX_REFRESHES: Final = 10
# The provider lists at most this many files in a comparison; more is not exact.
_COMPARE_FILE_LIMIT: Final = 300

SourceControlRead = Callable[[str], object]
ChangeFingerprint = tuple[tuple[str, str, str, str, str], ...]

_LOGGER = logging.getLogger(__name__)


def carry_owner_acceptance(
    *,
    store: ProductReviewStore,
    profile: LaunchplaneProductProfileRecord,
    pull_request_number: int,
    head_sha: str,
    base_branch: str,
    read: SourceControlRead,
    now: Callable[[], datetime] = lambda: datetime.now(timezone.utc),
) -> ProductReviewDecisionRecord | None:
    """Save and return the carried acceptance for `head_sha`, or None when it does not carry."""

    if not callable(getattr(store, "list_merge_train_branch_refresh_records", None)):
        return None
    if not base_branch.strip():
        return None
    head = head_sha.strip().lower()
    decisions = store.list_product_review_decision_records(
        repository=profile.repository, pull_request_number=pull_request_number
    )
    if not decisions or any(record.head_sha.strip().lower() == head for record in decisions):
        return None
    accepted = decisions[0]
    if not _carryable(accepted, profile):
        return None
    refreshes = cast(
        MergeTrainBranchRefreshReadStore, store
    ).list_merge_train_branch_refresh_records(
        repository=profile.repository, pull_request_number=pull_request_number
    )
    carry = _base_only_refresh(
        repository=profile.repository,
        accepted_head=accepted.head_sha.strip().lower(),
        head=head,
        base_branch=base_branch.strip(),
        refreshes=refreshes,
        read=read,
    )
    if carry is None:
        return None
    refresh_record_ids, base_commit = carry
    accepted_change = _change_fingerprint(
        repository=profile.repository, base=base_commit, head=accepted.head_sha, read=read
    )
    if accepted_change is None or accepted_change != _change_fingerprint(
        repository=profile.repository, base=base_commit, head=head, read=read
    ):
        return None
    with store.product_review_lock(
        repository=profile.repository, pull_request_number=pull_request_number
    ):
        latest = store.list_product_review_decision_records(
            repository=profile.repository, pull_request_number=pull_request_number, limit=1
        )
        if not latest or latest[0].record_id != accepted.record_id:
            # The Client decided again while this was checked; that decision stands.
            return None
        record = ProductReviewDecisionRecord.model_validate(
            {
                **accepted.model_dump(),
                "record_id": (
                    f"product-review-{profile.product}-pr-{pull_request_number}-{uuid4().hex}"
                ),
                "head_sha": head,
                "decided_at": now()
                .astimezone(timezone.utc)
                .isoformat(timespec="milliseconds")
                .replace("+00:00", "Z"),
                "carried_from": ProductReviewCarry(
                    record_id=accepted.record_id,
                    head_sha=accepted.head_sha.strip().lower(),
                    refresh_record_ids=refresh_record_ids,
                ),
            }
        )
        store.write_product_review_decision_record(record)
    _LOGGER.info(
        "Carried a Client acceptance across a merge train base refresh.",
        extra={
            "repository": profile.repository,
            "pull_request_number": pull_request_number,
            "from_head": accepted.head_sha,
            "to_head": head,
        },
    )
    return record


def _carryable(
    decision: ProductReviewDecisionRecord, profile: LaunchplaneProductProfileRecord
) -> bool:
    return (
        decision.decision == "accepted"
        and decision.product == profile.product
        and profile.owner.is_set
        # A new Client decides for themselves.
        and decision.owner_github_id == profile.owner.github_id
        and bool(decision.head_sha.strip())
        and not (decision.feedback_requested and not decision.feedback_url)
    )


def _base_only_refresh(
    *,
    repository: str,
    accepted_head: str,
    head: str,
    base_branch: str,
    refreshes: tuple[MergeTrainBranchRefreshRecord, ...],
    read: SourceControlRead,
) -> tuple[tuple[str, ...], str] | None:
    """The train's refresh records from `accepted_head` to `head`, oldest first, and
    the base commit the newest refresh merged; None unless every step is one."""

    record_ids: list[str] = []
    newest_base_commit = ""
    current = head
    for _ in range(MAX_REFRESHES):
        commit = _object(read(f"/repos/{_path(repository)}/git/commits/{current}"))
        parents = commit.get("parents")
        if not isinstance(parents, list) or len(parents) != 2:
            return None
        previous_head = _text(_object(parents[0]).get("sha")).lower()
        base_commit = _text(_object(parents[1]).get("sha")).lower()
        committed_at = _timestamp(_text(_object(commit, "committer").get("date")))
        if not previous_head or not base_commit or committed_at is None:
            return None
        refresh = next(
            (
                record
                for record in refreshes
                if record.expected_head_sha == previous_head
                and record.base_branch == base_branch
                and requested_at_datetime(record) - CLOCK_SKEW <= committed_at
            ),
            None,
        )
        if refresh is None or not _on_base_branch(
            repository=repository, commit=base_commit, base_branch=base_branch, read=read
        ):
            return None
        record_ids.insert(0, refresh.record_id)
        newest_base_commit = newest_base_commit or base_commit
        if previous_head == accepted_head:
            return tuple(record_ids), newest_base_commit
        current = previous_head
    return None


def _on_base_branch(
    *, repository: str, commit: str, base_branch: str, read: SourceControlRead
) -> bool:
    branch = quote(base_branch, safe="")
    comparison = _object(read(f"/repos/{_path(repository)}/compare/{commit}...{branch}"))
    return comparison.get("status") in {"ahead", "identical"}


def _change_fingerprint(
    *, repository: str, base: str, head: str, read: SourceControlRead
) -> ChangeFingerprint | None:
    """The pull request's change against `base`: from their merge base to `head`.

    Equal fingerprints mean equal resulting blobs and equal patches, so the change
    is byte-identical. A file the provider gives no patch for (binary, or too large)
    is not exact, and neither is a truncated file list.
    """

    comparison = _object(
        read(f"/repos/{_path(repository)}/compare/{base}...{head.strip().lower()}")
    )
    files = comparison.get("files")
    if not isinstance(files, list) or len(files) >= _COMPARE_FILE_LIMIT:
        return None
    fingerprint: list[tuple[str, str, str, str, str]] = []
    for item in files:
        entry = _object(item)
        status = _text(entry.get("status"))
        patch = entry.get("patch")
        pure_rename = status == "renamed" and entry.get("changes") == 0
        if not isinstance(patch, str) and not pure_rename:
            return None
        fingerprint.append(
            (
                _text(entry.get("filename")),
                _text(entry.get("previous_filename")),
                status,
                _text(entry.get("sha")),
                patch if isinstance(patch, str) else "",
            )
        )
    return tuple(sorted(fingerprint))


def _object(value: object, key: str = "") -> dict[str, object]:
    if key:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _timestamp(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.astimezone(timezone.utc) if parsed.tzinfo is not None else None


def _path(repository: str) -> str:
    owner, name = repository.strip().split("/", 1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"
