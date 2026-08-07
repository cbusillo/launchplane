from __future__ import annotations

from collections.abc import Callable
from typing import Literal

from control_plane.advisory_check_projection import write_advisory_check_projection
from control_plane.contracts.advisory_check_projection import AdvisoryCheckProjection
from control_plane.contracts.engineering_review_decision import (
    ENGINEERING_REVIEW_STATUS_CONTEXT,
    EngineeringReviewDecisionProjectionResult,
    EngineeringReviewDecisionRecord,
)
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.workflows.launchplane import github_api_request


GitHubApiRequest = Callable[..., object]
EngineeringReviewGitHubState = Literal["pending", "success", "failure", "error"]


class EngineeringReviewDecisionProjectionError(ValueError):
    pass


def project_engineering_review_decision(
    *,
    record: EngineeringReviewDecisionRecord,
    installation_token: GitHubAppInstallationToken,
    api_request: GitHubApiRequest = github_api_request,
) -> EngineeringReviewDecisionProjectionResult:
    state, description = _projection_state(record)
    target_url = (
        f"https://github.com/{record.target.repository}/pull/{record.target.pull_request_number}"
    )
    summary = "\n".join(
        (
            "Launchplane advisory projection; mode=shadow, authoritative=false, "
            "enforcement_effect=none.",
            "",
            description,
            f"Decision binding: `{record.decision_binding_sha256}`",
        )
    )
    try:
        result = write_advisory_check_projection(
            projection=AdvisoryCheckProjection(
                name=ENGINEERING_REVIEW_STATUS_CONTEXT,
                repository=record.target.repository,
                repository_id=record.target.repository_id,
                head_sha=record.target.head_sha,
                external_id=record.decision_binding_sha256,
                details_url=target_url,
                title=f"Engineering review: {record.status.replace('_', ' ')}",
                summary=summary,
            ),
            installation_token=installation_token,
            api_request=api_request,
        )
    except ValueError as error:
        raise EngineeringReviewDecisionProjectionError(str(error)) from error
    return EngineeringReviewDecisionProjectionResult(
        status=result.status,
        state=state,
        head_sha=result.head_sha,
        external_id=result.external_id,
        app_id=result.app_id,
        installation_id=result.installation_id,
        check_run_id=result.check_run_id,
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
