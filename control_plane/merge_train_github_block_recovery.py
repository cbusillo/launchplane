"""GitHub evidence for recovery of a service-recorded candidate block."""

from __future__ import annotations

import logging
import re
from datetime import datetime
from collections.abc import Callable
from typing import TYPE_CHECKING
from urllib.parse import quote

from control_plane.contracts.merge_train_batch import MergeTrainBatchCandidateRecord
from control_plane.contracts.merge_train_policy import MergeTrainRepositoryPolicy
from control_plane.contracts.merge_train_pr_feedback_record import MergeTrainPrFeedbackRecord
from control_plane.merge_train_handoff import handoff_actor_allowed, handoff_timestamp

if TYPE_CHECKING:
    from control_plane.merge_train_github import GitHubMergeTrainClient

_LOGGER = logging.getLogger(__name__)
# Producer contract: gh-pr.py comment --reviewed-head. Free prose is never an outcome.
_RECEIPT = re.compile(r"\AReview result: weighed\nHead: ([0-9a-f]{40})\n\n(?=\S)")
_REQUEST = re.compile(
    r"(?i)^review (?:prompt|request)\b|^review of record (?:still )?(?:needed|requested)\b"
)
_REQUEST_CONTEXT = re.compile(
    r"(?i)\breview of record\b|\banother model\b|\bmodel[- ]review\b"
    r"|\bprovider\s*:\s*`?(?:openai|anthropic|google)\b"
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


def block_label_events(
    client: GitHubMergeTrainClient, *, repository: str, number: int, label: str
) -> list[dict[str, object]]:
    return [
        event
        for event in _pages(client, f"/repos/{repository}/issues/{number}/events")
        if isinstance(named := event.get("label"), dict)
        and str(named.get("name") or "").casefold() == label.casefold()
        and event.get("event") in {"labeled", "unlabeled"}
    ]


def candidate_block_application(
    client: GitHubMergeTrainClient,
    *,
    candidate: MergeTrainBatchCandidateRecord,
    policy: MergeTrainRepositoryPolicy,
) -> dict[str, object]:
    intent = candidate.block_intent
    if intent is None:
        raise ValueError("Candidate block application requires its saved decision")
    number = candidate.candidate.entries[0].pull_request_number
    events = block_label_events(
        client, repository=policy.repository, number=number, label=intent.label
    )
    changes = [
        event
        for event in events
        if type(event.get("id")) is int and int(str(event["id"])) > intent.prior_label_event_id
    ]
    if any(type(event.get("id")) is not int for event in events):
        return {"status": "pending"}
    if (
        len(changes) == 1
        and changes[0].get("event") == "labeled"
        and _actor_id(changes[0], "actor") in policy.enqueue.trusted_automation_github_user_ids
    ):
        return {"status": "applied", "event_id": changes[0]["id"]}
    if changes:
        return {"status": "other_hold"}
    detail = client._pull_request_detail(repository=policy.repository, pull_request_number=number)
    labels = detail.get("labels")
    if not isinstance(labels, list):
        return {"status": "pending"}
    return {
        "status": "pending"
        if any(
            isinstance(label, dict)
            and str(label.get("name") or "").casefold() == intent.label.casefold()
            for label in labels
        )
        else "absent"
    }


def _review_weighed(
    comments: list[dict[str, object]],
    *,
    head: str,
    actor_allowed: Callable[[dict[str, object]], bool],
) -> bool:
    latest: tuple[datetime, int, bool] | None = None
    for comment in comments:
        body = str(comment.get("body") or "")
        receipt = _RECEIPT.match(body)
        lines = body.strip().splitlines()
        first = re.sub(r"^[\s#>*_]+", "", lines[0]) if lines else ""
        request = bool(_REQUEST.search(first) and _REQUEST_CONTEXT.search(first)) or any(
            _SUPERVISOR.fullmatch(line.strip().replace("**", ""))
            for line in lines
            if not line.lstrip().startswith((">", "`"))
        )
        if not (receipt or request) or (receipt and receipt[1] != head):
            continue
        if not actor_allowed(comment):
            continue
        comment_id = comment.get("id")
        if type(comment_id) is not int:
            return False
        # The catalog orders newly posted attestations/requests by creation, and
        # accepts edited receipt bodies. An old request edit is not a new request.
        row = (handoff_timestamp(str(comment.get("created_at") or "")), comment_id, bool(receipt))
        if latest is None or row[:2] > latest[:2]:
            latest = row
    return latest is not None and latest[2]


def recover_candidate_block(
    *,
    client: GitHubMergeTrainClient,
    candidate: MergeTrainBatchCandidateRecord,
    feedback: MergeTrainPrFeedbackRecord | None,
    policy: MergeTrainRepositoryPolicy,
    expected_head_sha: str,
    mutate: bool,
    before_remove: Callable[[], None],
) -> bool:
    from control_plane.merge_train_github import (
        GitHubMergeTrainSnapshotReader,
        MergeTrainGitHubError,
    )
    from control_plane.merge_train_batch_pull_request import bound_closed_batch_pull_request

    repository = policy.repository
    number = candidate.candidate.entries[0].pull_request_number
    prefix = f"/repos/{repository}"
    label = policy.blocked_label

    def review_actor_allowed(
        comment: dict[str, object], author_id: int | None, reader: GitHubMergeTrainSnapshotReader
    ) -> bool:
        actor_id = _actor_id(comment, "user")
        if actor_id is None:
            return False
        if actor_id == author_id or actor_id in policy.enqueue.trusted_automation_github_user_ids:
            return True
        user = comment.get("user")
        login = str(user.get("login") or "") if isinstance(user, dict) else ""
        if not login:
            return False
        # Use the same current repository authority as Ready handoff writers.
        # A permitted helper may post the receipt on another author's PR.
        role = reader._actor_role_for_user(repository_path=repository, username=login)
        return handoff_actor_allowed(policy, actor_id=actor_id, actor_role=role)

    def label_event() -> dict[str, object] | None:
        events = block_label_events(client, repository=repository, number=number, label=label)
        if any(type(event.get("id")) is not int for event in events):
            return None
        events.sort(key=lambda event: int(str(event["id"])))
        if candidate.block_intent is not None:
            intent = candidate.block_intent
            if intent.label.casefold() != label.casefold() or intent.applied_label_event_id is None:
                return None
            changes = [
                event for event in events if int(str(event["id"])) > intent.prior_label_event_id
            ]
        else:
            # Legacy block feedback is a durable controller outcome even when
            # comment delivery failed or came through the workflow runner. Bind
            # its label sequence to the provider's exact closed candidate PR,
            # without comparing the service clock to GitHub's clock.
            if feedback is None:
                return None
            closed = bound_closed_batch_pull_request(client=client, candidate=candidate.candidate)
            if closed is None:
                return None
            closed_at = handoff_timestamp(str(closed.get("closed_at") or ""))
            changes = [
                event
                for event in events
                if handoff_timestamp(str(event.get("created_at") or "")) >= closed_at
            ]
        if len(changes) != 1 or changes[0].get("event") != "labeled":
            return None
        event = changes[0]
        if (
            candidate.block_intent is not None
            and event["id"] != candidate.block_intent.applied_label_event_id
        ):
            return None
        return (
            event
            if _actor_id(event, "actor") in policy.enqueue.trusted_automation_github_user_ids
            else None
        )

    def qualified() -> bool:
        pr = client.read_pull_request_snapshot(repository=repository, pull_request_number=number)
        reader = GitHubMergeTrainSnapshotReader(transport=client.transport)
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
                actor_allowed=lambda comment: review_actor_allowed(comment, pr.actor_id, reader),
            )
        )

    try:
        if not qualified() or (event := label_event()) is None:
            return False
        if not mutate:
            return True
        before_remove()
        if not qualified() or label_event() != event:
            return False
    except MergeTrainGitHubError as error:
        if error.rate_limited or error.status_code in {401, 403}:
            raise
        _LOGGER.warning(
            "Candidate block recovery evidence unavailable; preserving hold",
            extra={
                "repository": repository,
                "pull_request_number": number,
                "github_status_code": error.status_code,
            },
        )
        return False
    except (OSError, ValueError, TypeError, KeyError) as error:
        _LOGGER.warning(
            "Candidate block recovery proof unavailable; preserving hold",
            extra={
                "repository": repository,
                "pull_request_number": number,
                "error_type": type(error).__name__,
            },
        )
        return False
    # Mutation errors propagate into the ordinary reconciliation fence. A resume
    # observes removal and emits the waiting feedback without replaying DELETE.
    client.transport.request(
        method="DELETE", path=f"{prefix}/issues/{number}/labels/{quote(label, safe='')}"
    )
    observed = client.read_pull_request_snapshot(repository=repository, pull_request_number=number)
    if label.casefold() in {name.casefold() for name in observed.labels}:
        raise MergeTrainGitHubError("Candidate block removal could not be confirmed.")
    return True
