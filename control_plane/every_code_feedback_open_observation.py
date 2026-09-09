"""Canonical managed-provider comparison for an inert open-state observation.

The future adapter must read both objects independently through managed GitHub
transport, then capture observed_at after the response completes. This helper
performs no transport and must never receive webhook/request-owned copies.
Closed/merged or mismatched repository evidence returns None. Malformed required
PR identifiers or timestamps raise ValidationError; the future transport adapter
must classify malformed provider data explicitly rather than treating it as open.
"""

from __future__ import annotations

from collections.abc import Mapping

from control_plane.contracts.every_code_feedback_resume import (
    EveryCodeFeedbackPullRequestOpenObservation,
)


def _verified_open_observation(
    *,
    repository: Mapping[str, object],
    pull_request: Mapping[str, object],
    observed_at: str,
) -> EveryCodeFeedbackPullRequestOpenObservation | None:
    owner = repository.get("owner")
    base = pull_request.get("base")
    base_repo = base.get("repo") if isinstance(base, Mapping) else None
    base_owner = base_repo.get("owner") if isinstance(base_repo, Mapping) else None
    repository_id = repository.get("id")
    owner_id = owner.get("id") if isinstance(owner, Mapping) else None
    if (
        pull_request.get("state") != "open"
        or pull_request.get("merged") is not False
        or type(repository_id) is not int
        or repository_id < 1
        or type(owner_id) is not int
        or owner_id < 1
        or not isinstance(base_repo, Mapping)
        or not isinstance(base_owner, Mapping)
        or type(base_repo.get("id")) is not int
        or type(base_owner.get("id")) is not int
        or base_repo.get("id") != repository_id
        or base_owner.get("id") != owner_id
    ):
        return None
    return EveryCodeFeedbackPullRequestOpenObservation.model_validate(
        {
            "repository_id": repository_id,
            "repository_owner_id": owner_id,
            "pull_request_number": pull_request.get("number"),
            "pull_request_node_id": pull_request.get("node_id"),
            "observed_at": observed_at,
        }
    )
