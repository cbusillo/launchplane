"""Carry a Client's acceptance across unchanged patches or declared invisible commits.

The Client accepts a change, seen on its preview. When the merge train only merges
the base branch into the pull request, the change is the same, so asking again
protects nothing. The acceptance carries to the new head only when all hold:

1. The pull request still targets the base branch recorded on the acceptance.
   Train refresh records supply an immutable base when available; other updates,
   including rebases, use a pinned commit read from that same base branch.
2. The pull request's change against its base is the same at both heads: every
   changed file has the same name, status, and added and removed lines, in order.
   Outside the train, context, section names and file modes must also match, and
   each hunk's position must follow the base's own line shifts. Recorded train
   refreshes can incorporate base context edits.
3. The carry is saved as its own decision record naming the decision and head it
   came from and the comparison base, so it reads as carried, not re-decided.

A linear descendant instead carries when every commit after the acceptance has
one `Client-impact: none` trailer. Save those commits and trailers as evidence.
Visible or missing trailers and unproven merges need a new decision, even if the
final feature patch matches. Recorded train-only refreshes retain the patch proof
above. Rewritten history still needs the strict unchanged-patch rebase proof.
A different base, a new Client, or unreadable or truncated evidence never carries.
"""

from collections.abc import Callable
from datetime import datetime, timezone
import logging
import re
from typing import Final
from urllib.parse import quote
from uuid import uuid4

from control_plane.contracts.merge_train_branch_refresh_record import (
    MergeTrainBranchRefreshRecord,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.product_review import (
    ProductReviewCarry,
    ProductReviewCarryReason,
    ProductReviewCarryCommit,
    ProductReviewDecisionRecord,
)
from control_plane.merge_train_branch_refresh import (
    optional_merge_train_branch_refresh_read_store,
)
from control_plane.product_review import ProductReviewStore
from control_plane.source_control_change import change_fingerprint as _change_fingerprint
from control_plane.source_control_change import patch_positions_match_base_update

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
    if not base_branch.strip():
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
    refreshes = (
        refresh_store.list_merge_train_branch_refresh_records(
            repository=profile.repository, pull_request_number=pull_request_number
        )
        if refresh_store is not None
        else ()
    )
    carry = _base_only_refresh(
        repository=profile.repository,
        accepted_head=accepted.head_sha.strip().lower(),
        head=head,
        base_branch=base_branch.strip(),
        refreshes=refreshes,
        read=read,
    )
    reason: ProductReviewCarryReason
    commits: tuple[ProductReviewCarryCommit, ...] = ()
    if carry is not None:
        refresh_record_ids, base_commit = carry
        reason = "merge_train_base_refresh"
    else:
        branch = quote(base_branch.strip(), safe="")
        base_commit = _text(
            _object(read(f"/repos/{_path(profile.repository)}/branches/{branch}"), "commit").get(
                "sha"
            )
        ).lower()
        if re.fullmatch(r"[0-9a-f]{40}", base_commit) is None:
            return None
        comparison = _object(
            read(f"/repos/{_path(profile.repository)}/compare/{accepted.head_sha}...{head}")
        )
        if comparison.get("status") == "ahead":
            commits = _client_impact_commits(comparison, accepted.head_sha, head)
            if not commits:
                return None
        elif comparison.get("status") != "diverged":
            return None
        refresh_record_ids = ()
        reason = "client_impact_none" if commits else "unchanged_patch_base_update"
    if reason != "client_impact_none":
        responses: dict[str, object] = {}

        def comparison_read(path: str) -> object:
            # These comparisons and trees are bound to immutable commits. Reuse the
            # comparisons; trees are needed only after text and positions match.
            if path not in responses:
                responses[path] = read(path)
            return responses[path]

        accepted_change = _change_fingerprint(
            repository=profile.repository,
            base=base_commit,
            head=accepted.head_sha,
            read=comparison_read,
            include_blobs=False,
            changed_lines_only=reason == "merge_train_base_refresh",
            ignore_hunk_positions=reason == "unchanged_patch_base_update",
        )
        if not accepted_change or accepted_change != _change_fingerprint(
            repository=profile.repository,
            base=base_commit,
            head=head,
            read=comparison_read,
            include_blobs=False,
            changed_lines_only=reason == "merge_train_base_refresh",
            ignore_hunk_positions=reason == "unchanged_patch_base_update",
        ):
            return None
        if reason == "unchanged_patch_base_update":
            if not patch_positions_match_base_update(
                repository=profile.repository,
                base=base_commit,
                accepted_head=accepted.head_sha,
                head=head,
                read=comparison_read,
            ):
                return None
            accepted_modes = _change_fingerprint(
                repository=profile.repository,
                base=base_commit,
                head=accepted.head_sha,
                read=comparison_read,
                include_blobs=False,
                ignore_hunk_positions=True,
                include_file_modes=True,
            )
            if accepted_modes is None or accepted_modes != _change_fingerprint(
                repository=profile.repository,
                base=base_commit,
                head=head,
                read=comparison_read,
                include_blobs=False,
                ignore_hunk_positions=True,
                include_file_modes=True,
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
                    reason=reason,
                    refresh_record_ids=refresh_record_ids,
                    comparison_base_sha=base_commit,
                    commits=commits,
                ),
            }
        )
        store.write_product_review_decision_record(record)
    _LOGGER.info(
        "Carried a Client acceptance to a later change head.",
        extra={
            "repository": profile.repository,
            "pull_request_number": pull_request_number,
            "from_head": accepted.head_sha,
            "to_head": head,
        },
    )
    return record


def _client_impact_commits(
    comparison: dict[str, object], accepted_head: str, head: str
) -> tuple[ProductReviewCarryCommit, ...]:
    """Require the complete, ordered single-parent chain, not a truncated compare.

    A merge's message cannot prove it did not resolve conflicts. Only the existing
    recorded train refresh and patch proof can carry such a base merge.
    """

    commits = comparison.get("commits")
    if not isinstance(commits, list) or not 0 < len(commits) <= 250:
        return ()
    if (
        any(
            type(comparison.get(key)) is not int or comparison[key] != expected
            for key, expected in (
                ("total_commits", len(commits)),
                ("ahead_by", len(commits)),
                ("behind_by", 0),
            )
        )
        or _object(comparison.get("merge_base_commit")).get("sha") != accepted_head
    ):
        return ()
    previous = accepted_head
    evidence: list[ProductReviewCarryCommit] = []
    seen = {accepted_head}
    for value in commits:
        commit = _object(value)
        sha = _text(commit.get("sha"))
        parents = commit.get("parents")
        message = _object(commit.get("commit")).get("message")
        if (
            re.fullmatch(r"[0-9a-f]{40}", sha) is None
            or sha in seen
            or not isinstance(parents, list)
            or len(parents) != 1
            or _object(parents[0]).get("sha") != previous
            or not _has_none_trailer(message)
        ):
            return ()
        evidence.append(ProductReviewCarryCommit(sha=sha, client_impact="none"))
        seen.add(sha)
        previous = sha
    return tuple(evidence) if previous == head else ()


def _has_none_trailer(message: object) -> bool:
    if not isinstance(message, str):
        return False
    # Trailers are the final paragraph, not a matching phrase in the body.
    paragraphs = message.rstrip().split("\n\n")
    if len(paragraphs) < 2:
        return False
    lines = paragraphs[-1].splitlines()
    trailers = [re.fullmatch(r"([A-Za-z][A-Za-z0-9-]*):[ \t]*(.+)", line) for line in lines]
    if not trailers or any(trailer is None for trailer in trailers):
        return False
    impacts = [
        trailer.group(2).strip()
        for trailer in trailers
        if trailer is not None and trailer.group(1).lower() == "client-impact"
    ]
    return impacts == ["none"]


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
