from __future__ import annotations

from collections.abc import Callable
from typing import Literal
from urllib.parse import quote

from control_plane.contracts.engineering_review_decision import (
    ENGINEERING_REVIEW_STATUS_CONTEXT,
    EngineeringReviewDecisionProjectionResult,
    EngineeringReviewDecisionRecord,
)
from control_plane.github_payload import json_object, required_string_text
from control_plane.workflows.launchplane import github_api_request


GitHubApiRequest = Callable[..., object]
EngineeringReviewGitHubState = Literal["pending", "success", "failure", "error"]


class EngineeringReviewDecisionProjectionError(ValueError):
    pass


def project_engineering_review_decision(
    *,
    record: EngineeringReviewDecisionRecord,
    token: str,
    api_request: GitHubApiRequest = github_api_request,
) -> EngineeringReviewDecisionProjectionResult:
    state, description = _projection_state(record)
    owner, repo = record.target.repository.split("/", 1)
    encoded_owner = quote(owner, safe="")
    encoded_repo = quote(repo, safe="")
    encoded_sha = quote(record.target.head_sha, safe="")
    target_url = (
        f"https://github.com/{record.target.repository}/pull/"
        f"{record.target.pull_request_number}"
    )
    current_payload = json_object(
        api_request(
            path=f"/repos/{encoded_owner}/{encoded_repo}/commits/{encoded_sha}/status",
            token=token,
        ),
        "GitHub combined status response",
        error_type=EngineeringReviewDecisionProjectionError,
    )
    statuses = current_payload.get("statuses")
    if not isinstance(statuses, list):
        raise EngineeringReviewDecisionProjectionError(
            "GitHub combined status response must include statuses."
        )
    current = next(
        (
            item
            for item in statuses
            if isinstance(item, dict)
            and str(item.get("context") or "") == ENGINEERING_REVIEW_STATUS_CONTEXT
        ),
        None,
    )
    if current is not None and (
        str(current.get("state") or "") == state
        and str(current.get("description") or "") == description
        and str(current.get("target_url") or "") == target_url
    ):
        return EngineeringReviewDecisionProjectionResult(
            status="replayed", state=state, head_sha=record.target.head_sha
        )
    response = json_object(
        api_request(
            path=f"/repos/{encoded_owner}/{encoded_repo}/statuses/{encoded_sha}",
            token=token,
            method="POST",
            body={
                "state": state,
                "target_url": target_url,
                "description": description,
                "context": ENGINEERING_REVIEW_STATUS_CONTEXT,
            },
        ),
        "GitHub engineering review status response",
        error_type=EngineeringReviewDecisionProjectionError,
    )
    if (
        required_string_text(
            response.get("context"),
            "GitHub engineering review status response requires context.",
            error_type=EngineeringReviewDecisionProjectionError,
        )
        != ENGINEERING_REVIEW_STATUS_CONTEXT
        or required_string_text(
            response.get("state"),
            "GitHub engineering review status response requires state.",
            error_type=EngineeringReviewDecisionProjectionError,
        )
        != state
        or required_string_text(
            response.get("sha"),
            "GitHub engineering review status response requires sha.",
            error_type=EngineeringReviewDecisionProjectionError,
        ).lower()
        != record.target.head_sha
    ):
        raise EngineeringReviewDecisionProjectionError(
            "GitHub engineering review status response did not match the decision."
        )
    return EngineeringReviewDecisionProjectionResult(
        status="projected", state=state, head_sha=record.target.head_sha
    )


def _projection_state(
    record: EngineeringReviewDecisionRecord,
) -> tuple[EngineeringReviewGitHubState, str]:
    if record.status == "approved":
        return "success", (
            f"Shadow review approved by {len(record.qualifying_run_ids)}/"
            f"{record.required_review_count} required run(s)."
        )
    if record.status == "pending":
        return "pending", (
            f"Shadow review awaiting {record.required_review_count} approved run(s)."
        )
    if record.status == "changes_requested":
        return "failure", "Shadow engineering review requested changes."
    if record.status == "blocked":
        return "failure", "Shadow engineering review reported a blocking finding."
    if record.status == "stale":
        return "error", "Shadow engineering review evidence is stale."
    return "error", "Shadow engineering review authority is unavailable."
