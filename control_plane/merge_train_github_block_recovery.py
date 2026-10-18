"""GitHub evidence for recovery of a service-recorded candidate block."""

from __future__ import annotations

import re
from datetime import datetime
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import quote

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidateRecord
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy
from control_plane.contracts.merge_train_pr_feedback_record import MergeTrainPrFeedbackRecord
from control_plane.merge_train_handoff import handoff_kind, handoff_timestamp

if TYPE_CHECKING:
    from control_plane.merge_train_github import GitHubMergeTrainClient


_RECEIPT = re.compile(r"\AReview result: weighed\nHead: ([0-9a-f]{40})\n\n(?=\S)")
_REQUEST = re.compile(
    r"(?i)^review (?:prompt|request)\b|^review of record (?:still )?(?:needed|requested)\b"
)
_SUPERVISOR = re.compile(r"(?i)^(?:.*?\bso\s+)?supervisor runs the review[.:]?\s*$")


def _pages(client: GitHubMergeTrainClient, path: str) -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for page in range(1, 101):
        payload = client.transport.request(method="GET", path=f"{path}?per_page=100&page={page}")
        if not isinstance(payload, list) or any(not isinstance(item, dict) for item in payload):
            raise ValueError("Candidate block recovery evidence is incomplete.")
        items.extend(payload)
        if len(payload) < 100:
            return items
    raise ValueError("Candidate block recovery evidence exceeds its page bound.")


def _actor_id(item: dict[str, object], field: str) -> int | None:
    actor = item.get(field)
    if not isinstance(actor, dict) or (
        item.get("performed_via_github_app") and actor.get("type") != "Bot"
    ):
        return None
    value = actor.get("id")
    return value if type(value) is int and value > 0 else None


def _review_weighed(comments: list[dict[str, object]], *, head: str, trusted: set[int]) -> bool:
    latest: tuple[datetime, int, bool] | None = None
    for comment in comments:
        if _actor_id(comment, "user") not in trusted:
            continue
        body = str(comment.get("body") or "")
        receipt = _RECEIPT.match(body)
        lines = body.strip().splitlines()
        first = re.sub(r"^[\s#>*_]+", "", lines[0]) if lines else ""
        request = bool(_REQUEST.search(first)) or any(
            _SUPERVISOR.fullmatch(line.strip().replace("**", ""))
            for line in lines
            if not line.lstrip().startswith((">", "`"))
        )
        waiting = handoff_kind(body) == "waiting"
        if not (receipt or request or waiting):
            continue
        if receipt and receipt[1] != head:
            continue
        created = str(comment.get("created_at") or "")
        updated = str(comment.get("updated_at") or created)
        comment_id = comment.get("id")
        if type(comment_id) is not int:
            return False
        stamp = max(handoff_timestamp(created), handoff_timestamp(updated))
        valid = bool(receipt) and created == updated
        row = (stamp, comment_id, valid)
        if latest is None or row[:2] > latest[:2]:
            latest = row
    return latest is not None and latest[2]


def recover_candidate_block(
    *,
    client: GitHubMergeTrainClient,
    candidate: MergeTrainBatchCandidateRecord,
    feedback: MergeTrainPrFeedbackRecord,
    policy: MergeTrainRepositoryPolicy,
    expected_head_sha: str,
    mutate: bool,
    before_remove: Callable[[], None],
) -> bool:
    from control_plane.merge_train_github import MergeTrainGitHubError

    repository = policy.repository
    number = feedback.pull_request_number
    prefix = f"/repos/{repository}"
    label = policy.blocked_label

    def label_event() -> dict[str, object] | None:
        latest = None
        changes_since_failure = 0
        for event in _pages(client, f"{prefix}/issues/{number}/events"):
            named = event.get("label")
            if (
                not isinstance(named, dict)
                or str(named.get("name") or "").casefold() != label.casefold()
            ):
                continue
            if event.get("event") not in {"labeled", "unlabeled"}:
                continue
            if handoff_timestamp(str(event.get("created_at") or "")) >= handoff_timestamp(
                candidate.updated_at
            ):
                changes_since_failure += 1
            if event.get("event") == "unlabeled":
                latest = None
            else:
                latest = event
        # GitHub timestamps have second precision. A removal/reapplication can
        # share the original stamp; its separate event must preserve the new hold.
        return latest if changes_since_failure == 1 else None

    def qualified() -> bool:
        pr = client.read_pull_request_snapshot(repository=repository, pull_request_number=number)
        return (
            pr.head_sha == expected_head_sha
            and pr.head_sha != candidate.candidate.entries[0].head_sha
            and pr.state == "open"
            and not pr.is_draft
            and pr.base_repository == repository
            and pr.base_ref == policy.base_branch
            and pr.head_repository == repository
            and pr.required_checks_status == "pass"
            and pr.mergeable == "mergeable"
            and not pr.client_test_notes_error
            and pr.review_conversations is None
            and (
                pr.actor_id in policy.enqueue.trusted_automation_github_user_ids
                or pr.actor_role in policy.enqueue.allowed_actor_roles
            )
            and _review_weighed(
                _pages(client, f"{prefix}/issues/{number}/comments"),
                head=pr.head_sha,
                trusted=set(policy.enqueue.trusted_automation_github_user_ids) | {pr.actor_id or 0},
            )
        )

    try:
        if not qualified():
            return False
        comment = client.transport.request(
            method="GET", path=f"{prefix}/issues/comments/{feedback.comment_id}"
        )
        event = label_event()
        if not isinstance(comment, dict) or event is None:
            return False
        actor = _actor_id(event, "actor")
        if (
            actor not in policy.enqueue.trusted_automation_github_user_ids
            or actor != _actor_id(comment, "user")
            or comment.get("id") != feedback.comment_id
            or comment.get("body") != feedback.comment_markdown
            or type(event.get("id")) is not int
            or not (
                handoff_timestamp(candidate.updated_at)
                <= handoff_timestamp(str(event.get("created_at") or ""))
                <= handoff_timestamp(feedback.recorded_at)
            )
        ):
            return False
        if not mutate:
            return True
        # Checkpoint before the provider effect. Unknown effects retain the
        # controller's reconciliation fence; a later pass observes the label.
        before_remove()
        if not qualified() or label_event() != event:
            return False
    except (ValueError, TypeError, KeyError):
        return False
    # Provider refusals and uncertain writes propagate. Never retry a removal
    # under a different key/identity or restore a label over concurrent work.
    client.transport.request(
        method="DELETE", path=f"{prefix}/issues/{number}/labels/{quote(label, safe='')}"
    )
    observed = client.read_pull_request_snapshot(repository=repository, pull_request_number=number)
    if label.casefold() in {name.casefold() for name in observed.labels}:
        raise MergeTrainGitHubError("Candidate block removal could not be confirmed.")
    return True
