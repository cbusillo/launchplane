from __future__ import annotations

from collections.abc import Callable
import hashlib
import json

from control_plane.advisory_check_projection import write_advisory_check_projection
from control_plane.contracts.advisory_check_projection import (
    AdvisoryCheckProjection,
    AdvisoryCheckProjectionResult,
    OWNER_ACCEPTANCE_CHECK_NAME,
)
from control_plane.contracts.change_impact import ChangeImpactTarget
from control_plane.contracts.owner_acceptance import OwnerAcceptanceDecision
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.workflows.launchplane import github_api_request


GitHubApiRequest = Callable[..., object]


def project_owner_acceptance_decision(
    *,
    decision: OwnerAcceptanceDecision,
    target: ChangeImpactTarget,
    installation_token: GitHubAppInstallationToken,
    api_request: GitHubApiRequest = github_api_request,
) -> AdvisoryCheckProjectionResult:
    external_id = owner_acceptance_projection_sha256(decision)
    details_url = f"https://github.com/{target.repository}/pull/{target.pull_request_number}"
    return write_advisory_check_projection(
        projection=AdvisoryCheckProjection(
            name=OWNER_ACCEPTANCE_CHECK_NAME,
            repository=target.repository,
            repository_id=target.repository_id,
            head_sha=target.head_sha,
            external_id=external_id,
            details_url=details_url,
            title=f"Owner acceptance: {decision.status.replace('_', ' ')}",
            summary=_summary(decision),
        ),
        installation_token=installation_token,
        api_request=api_request,
    )


def owner_acceptance_projection_sha256(decision: OwnerAcceptanceDecision) -> str:
    payload = decision.model_dump(
        mode="json",
        exclude={"evaluated_at"},
        exclude_none=True,
    )
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def _summary(decision: OwnerAcceptanceDecision) -> str:
    lines = [
        "Launchplane advisory projection; mode=shadow, authoritative=false, "
        "enforcement_effect=none.",
        "",
        f"Aggregate decision: **{decision.status.replace('_', ' ')}**",
        f"Reason code: `{decision.reason_code}`",
    ]
    if decision.products:
        lines.extend(("", "Per-product decisions:"))
        for product in decision.products:
            binding = (
                product.binding.binding_sha256 if product.binding is not None else "unavailable"
            )
            lines.append(
                f"- `{product.product}`: **{product.status.replace('_', ' ')}** "
                f"(`{product.reason_code}`; binding `{binding}`)"
            )
    else:
        lines.extend(("", "No product-specific Owner decision is currently required."))
    return "\n".join(lines)
