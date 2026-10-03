"""Carry a Client's acceptance across a base-only refresh made by the merge train.

The Client accepts a change, seen on its preview. When the merge train only merges
the base branch into the pull request, the change is the same, so asking again
protects nothing. The acceptance carries to the new head only when all hold:

1. Every commit between the accepted head and the new head is exactly the merge
   commit the train's own refresh produced: the train read the provider's new head
   back after asking for the refresh and recorded it, the previous head, and the
   base commit it merged. That base commit is on the pull request's current base
   branch, which is the base the acceptance was given on.
2. The pull request's change against its base is the same at both heads: every
   changed file has the same name, status, and added and removed lines, in order.
   Hunk positions and context lines are not compared, so a base edit that moves or
   surrounds the change in a file it also changes does not stop the carry.
3. The carry is saved as its own decision record naming the decision and head it
   came from and the train's refresh records, so it reads as carried, not re-decided.

Anything else (another commit, another merge with the same parents, a conflict
resolution, a different base, a new Client, an unreadable or truncated diff) leaves
the new head waiting for a decision.
"""

from collections.abc import Callable
from datetime import datetime, timezone
import logging
from typing import Final
from urllib.parse import quote
from uuid import uuid4

from control_plane.contracts.merge_train_branch_refresh_record import (
    MergeTrainBranchRefreshRecord,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_review import ProductReviewCarry, ProductReviewDecisionRecord
from control_plane.merge_train_branch_refresh import (
    optional_merge_train_branch_refresh_read_store,
)
from control_plane.product_review import ProductReviewStore
from control_plane.source_control_change import change_fingerprint as _change_fingerprint

# A busy train may refresh a pull request more than once before anyone looks.
MAX_REFRESHES: Final = 10

SourceControlRead = Callable[[str], object]

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

    refresh_store = optional_merge_train_branch_refresh_read_store(store)
    if refresh_store is None or not base_branch.strip():
        return None
    head = head_sha.strip().lower()
    decisions = store.list_product_review_decision_records(
        repository=profile.repository, pull_request_number=pull_request_number
    )
    if not decisions or any(record.head_sha.strip().lower() == head for record in decisions):
        return None
    accepted = decisions[0]
    if not _carryable(accepted, profile) or accepted.base_branch != base_branch.strip():
        # Accepted on another base (or one never recorded): the change is not the same.
        return None
    refreshes = refresh_store.list_merge_train_branch_refresh_records(
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
        repository=profile.repository,
        base=base_commit,
        head=accepted.head_sha,
        read=read,
        changed_lines_only=True,
    )
    if accepted_change is None or accepted_change != _change_fingerprint(
        repository=profile.repository,
        base=base_commit,
        head=head,
        read=read,
        changed_lines_only=True,
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
    the base commit the newest refresh merged; None unless every step is one.

    Each step is bound to the exact commit the train saw the provider make, so a
    different merge of the same parents never matches a record.
    """

    record_ids: list[str] = []
    newest_base_commit = ""
    current = head
    for _ in range(MAX_REFRESHES):
        refresh = next(
            (
                record
                for record in refreshes
                if record.result_head_sha == current and record.base_branch == base_branch
            ),
            None,
        )
        if refresh is None or not _on_base_branch(
            repository=repository,
            commit=refresh.merged_base_sha,
            base_branch=base_branch,
            read=read,
        ):
            return None
        record_ids.insert(0, refresh.record_id)
        newest_base_commit = newest_base_commit or refresh.merged_base_sha
        if refresh.expected_head_sha == accepted_head:
            return tuple(record_ids), newest_base_commit
        current = refresh.expected_head_sha
    return None


def _on_base_branch(
    *, repository: str, commit: str, base_branch: str, read: SourceControlRead
) -> bool:
    branch = quote(base_branch, safe="")
    comparison = _object(read(f"/repos/{_path(repository)}/compare/{commit}...{branch}"))
    return comparison.get("status") in {"ahead", "identical"}


def _object(value: object, key: str = "") -> dict[str, object]:
    if key:
        value = value.get(key) if isinstance(value, dict) else None
    return value if isinstance(value, dict) else {}


def _text(value: object) -> str:
    return value.strip() if isinstance(value, str) else ""


def _path(repository: str) -> str:
    owner, name = repository.strip().split("/", 1)
    return f"{quote(owner, safe='')}/{quote(name, safe='')}"


def record_decision_base(
    *,
    store: ProductReviewStore,
    profile: LaunchplaneProductProfileRecord,
    pull_request_number: int,
    head_sha: str,
    base_branch: str,
) -> None:
    """Keep the base branch on the newest decision for this head the first time it is
    shown on the pull request, so an acceptance carries only on that base."""

    head = head_sha.strip().lower()
    if not base_branch.strip():
        return
    with store.product_review_lock(
        repository=profile.repository, pull_request_number=pull_request_number
    ):
        latest = store.list_product_review_decision_records(
            repository=profile.repository, pull_request_number=pull_request_number, limit=1
        )
        if not latest or latest[0].head_sha.strip().lower() != head or latest[0].base_branch:
            return
        store.write_product_review_decision_record(
            latest[0].model_copy(update={"base_branch": base_branch.strip()})
        )
