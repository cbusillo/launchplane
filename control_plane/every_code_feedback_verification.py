"""Compare a signed delivery with independently read canonical GitHub objects.

The caller owns webhook authentication and managed GitHub API reads. This module
does not accept URLs as authority or turn transport authentication into a grant.
No existing webhook route calls it until the resume rollout is implemented.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC
import hashlib
from typing import cast

from control_plane.contracts.every_code_feedback_resume import (
    EveryCodeVerifiedFeedbackRevision,
    FeedbackKind,
    parse_every_code_feedback_timestamp,
    validate_every_code_feedback_revision_time,
)


def _object(value: object) -> Mapping[str, object]:
    if not isinstance(value, dict):
        raise ValueError("canonical feedback object is absent or malformed")
    return cast(dict[str, object], value)


def _id(value: object) -> int:
    if type(value) is not int or value < 1:
        raise ValueError("feedback identity must be a positive immutable numeric ID")
    return value


def _text(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("canonical feedback field is absent or malformed")
    return value


def _canonical_time(value: object) -> str:
    return (
        parse_every_code_feedback_timestamp(_text(value))
        .astimezone(UTC)
        .isoformat(timespec="microseconds")
        .replace("+00:00", "Z")
    )


def verify_every_code_feedback_revision(
    *,
    event_name: str,
    delivery: Mapping[str, object],
    canonical_repository: Mapping[str, object],
    canonical_pull_request: Mapping[str, object],
    canonical_feedback: Mapping[str, object],
    observed_at: str,
) -> EveryCodeVerifiedFeedbackRevision:
    """Fail closed unless current GitHub identity, PR binding, revision and body agree.

    Canonical inputs must come from successful independent managed API reads,
    never from the delivery or caller-supplied authorization evidence.
    """
    supported = {
        "issue_comment": {"created", "edited"},
        "pull_request_review": {"submitted", "edited"},
        "pull_request_review_comment": {"created", "edited"},
    }
    action = delivery.get("action")
    if (
        event_name not in supported
        or not isinstance(action, str)
        or action not in supported[event_name]
    ):
        raise ValueError("unsupported feedback event or action")
    repository = _object(delivery.get("repository"))
    repository_id = _id(repository.get("id"))
    owner_id = _id(_object(repository.get("owner")).get("id"))
    if (
        repository_id != _id(canonical_repository.get("id"))
        or owner_id != _id(_object(canonical_repository.get("owner")).get("id"))
        or _text(repository.get("full_name")).lower()
        != _text(canonical_repository.get("full_name")).lower()
    ):
        raise ValueError("canonical repository binding differs from delivery")
    pull_request = _object(
        delivery.get("issue") if event_name == "issue_comment" else delivery.get("pull_request")
    )
    if event_name == "issue_comment" and not isinstance(pull_request.get("pull_request"), dict):
        raise ValueError("issue feedback does not identify a pull request")
    if event_name == "issue_comment" and _text(
        _object(pull_request.get("pull_request")).get("url")
    ) != _text(canonical_pull_request.get("url")):
        raise ValueError("delivery issue references a different canonical pull request")
    pr_number = _id(pull_request.get("number"))
    pr_node_id = _text(pull_request.get("node_id"))
    base_repository = _object(_object(canonical_pull_request.get("base")).get("repo"))
    if (
        pr_number != _id(canonical_pull_request.get("number"))
        or pr_node_id != _text(canonical_pull_request.get("node_id"))
        or repository_id != _id(base_repository.get("id"))
        or owner_id != _id(_object(base_repository.get("owner")).get("id"))
        or canonical_pull_request.get("state") != "open"
    ):
        raise ValueError("canonical pull request is closed or has a different binding")
    feedback = _object(
        delivery.get("review") if event_name == "pull_request_review" else delivery.get("comment")
    )
    sender = _object(delivery.get("sender"))
    author = _object(feedback.get("user"))
    canonical_author = _object(canonical_feedback.get("user"))
    actor_id = _id(sender.get("id"))
    object_id = _id(feedback.get("id"))
    object_node_id = _text(feedback.get("node_id"))
    if (
        sender.get("type") != "User"
        or author.get("type") != "User"
        or canonical_author.get("type") != "User"
        or actor_id != _id(author.get("id"))
        or actor_id != _id(canonical_author.get("id"))
        or object_id != _id(canonical_feedback.get("id"))
        or object_node_id != _text(canonical_feedback.get("node_id"))
    ):
        raise ValueError("feedback author or immutable object identity does not match")
    binding_key = "issue_url" if event_name == "issue_comment" else "pull_request_url"
    expected_url = _text(
        canonical_pull_request.get("issue_url" if event_name == "issue_comment" else "url")
    )
    if _text(canonical_feedback.get(binding_key)) != expected_url:
        raise ValueError("canonical feedback belongs to a different pull request")
    timestamp_key = "submitted_at" if event_name == "pull_request_review" else "updated_at"
    provider_updated_at = _canonical_time(canonical_feedback.get(timestamp_key))
    if _canonical_time(feedback.get(timestamp_key)) != provider_updated_at or _text(
        feedback.get("body")
    ) != _text(canonical_feedback.get("body")):
        raise ValueError("canonical feedback revision or body differs from delivery")
    validate_every_code_feedback_revision_time(
        provider_updated_at=provider_updated_at, observed_at=observed_at
    )
    return EveryCodeVerifiedFeedbackRevision(
        repository_owner_id=owner_id,
        repository_id=repository_id,
        repository=_text(canonical_repository.get("full_name")).lower(),
        pull_request_number=pr_number,
        pull_request_node_id=pr_node_id,
        feedback_id=f"feedback-{repository_id}-{event_name}-{object_id}",
        feedback_kind=cast(FeedbackKind, event_name),
        object_node_id=object_node_id,
        object_id=object_id,
        actor_github_id=actor_id,
        actor_login=_text(canonical_author.get("login")),
        provider_updated_at=provider_updated_at,
        body_sha256=hashlib.sha256(_text(canonical_feedback.get("body")).encode()).hexdigest(),
    )
