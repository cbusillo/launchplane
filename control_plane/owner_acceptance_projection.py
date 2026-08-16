from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
from urllib.parse import quote, urlsplit

from control_plane.advisory_check_projection import write_advisory_check_projection
from control_plane.contracts.advisory_check_projection import (
    AdvisoryCheckProjection,
    AdvisoryCheckConclusion,
    AdvisoryCheckProjectionResult,
    OWNER_ACCEPTANCE_CHECK_NAME,
)
from control_plane.contracts.change_impact import ChangeImpactTarget
from control_plane.contracts.owner_acceptance import OwnerAcceptanceDecision
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.workflows.launchplane import github_api_request


GitHubApiRequest = Callable[..., object]
OWNER_ACCEPTANCE_WORKBENCH_PATH = "/ui/engineering/owner-acceptance"


def project_owner_acceptance_decision(
    *,
    decision: OwnerAcceptanceDecision,
    target: ChangeImpactTarget,
    public_origin: str,
    installation_token: GitHubAppInstallationToken,
    api_request: GitHubApiRequest = github_api_request,
) -> AdvisoryCheckProjectionResult:
    external_id = owner_acceptance_projection_sha256(decision)
    details_url = owner_acceptance_workbench_url(
        public_origin=public_origin,
        target=target,
    )
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
            conclusion=_conclusion(decision),
        ),
        installation_token=installation_token,
        api_request=api_request,
    )


def owner_acceptance_workbench_url(
    *,
    public_origin: str,
    target: ChangeImpactTarget,
) -> str:
    return owner_acceptance_workbench_reference_url(
        public_origin=public_origin,
        repository=target.repository,
        pull_request_number=target.pull_request_number,
    )


def owner_acceptance_workbench_reference_url(
    *,
    public_origin: str,
    repository: str,
    pull_request_number: int,
) -> str:
    origin = public_origin.strip()
    try:
        parsed_origin = urlsplit(origin)
    except ValueError as error:
        raise ValueError(
            "Owner acceptance projection requires a valid browser public origin."
        ) from error
    if (
        parsed_origin.scheme not in {"http", "https"}
        or not parsed_origin.netloc
        or parsed_origin.path not in {"", "/"}
        or parsed_origin.query
        or parsed_origin.fragment
        or parsed_origin.username is not None
        or parsed_origin.password is not None
    ):
        raise ValueError("Owner acceptance projection requires a valid browser public origin.")
    try:
        if parsed_origin.port is not None and not 1 <= parsed_origin.port <= 65535:
            raise ValueError
    except ValueError as error:
        raise ValueError(
            "Owner acceptance projection requires a valid browser public origin."
        ) from error
    if any(character.isspace() or ord(character) < 32 for character in origin):
        raise ValueError("Owner acceptance projection requires a valid browser public origin.")
    if repository.count("/") != 1 or any(
        not part or part != part.strip() for part in repository.split("/", 1)
    ):
        raise ValueError("Owner acceptance projection requires a valid repository target.")
    if pull_request_number < 1:
        raise ValueError("Owner acceptance projection requires a positive pull request number.")
    return (
        f"{origin.rstrip('/')}{OWNER_ACCEPTANCE_WORKBENCH_PATH}"
        f"?repository={quote(repository, safe='')}&pull_request={pull_request_number}"
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
        "Launchplane is the authoritative Owner-review system. This GitHub check is a "
        "routing and status projection only; record decisions in Launchplane.",
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


def _conclusion(decision: OwnerAcceptanceDecision) -> AdvisoryCheckConclusion:
    if decision.status in {"accepted", "not_required"}:
        return "success"
    if decision.status == "unavailable":
        return "failure"
    return "action_required"
