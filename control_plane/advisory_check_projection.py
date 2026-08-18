from __future__ import annotations

from collections.abc import Callable
import logging
from urllib.parse import quote, urlencode

import click

from control_plane.contracts.advisory_check_projection import (
    AdvisoryCheckProjection,
    AdvisoryCheckProjectionResult,
    AdvisoryCheckProjectionStatus,
)
from control_plane.github_app_identity import GitHubAppInstallationToken
from control_plane.github_payload import json_object, required_positive_int, required_string_text
from control_plane.workflows.launchplane import github_api_request


GitHubApiRequest = Callable[..., object]
logger = logging.getLogger(__name__)


class AdvisoryCheckProjectionError(ValueError):
    pass


def write_advisory_check_projection(
    *,
    projection: AdvisoryCheckProjection,
    installation_token: GitHubAppInstallationToken,
    api_request: GitHubApiRequest = github_api_request,
) -> AdvisoryCheckProjectionResult:
    if installation_token.repository_id != int(projection.repository_id):
        raise AdvisoryCheckProjectionError(
            "GitHub App installation token does not match projection repository id."
        )
    if installation_token.repository.casefold() != projection.repository.casefold():
        raise AdvisoryCheckProjectionError(
            "GitHub App installation token does not match projection repository name."
        )
    owner, repo = projection.repository.split("/", 1)
    repository_path = f"{quote(owner, safe='')}/{quote(repo, safe='')}"
    query = urlencode(
        {
            "check_name": projection.name,
            "app_id": str(installation_token.app_id),
            "filter": "latest",
            "per_page": "100",
        }
    )
    current_payload = json_object(
        _github_api_request(
            api_request,
            path=(
                f"/repos/{repository_path}/commits/"
                f"{quote(projection.head_sha, safe='')}/check-runs?{query}"
            ),
            token=installation_token.token,
        ),
        "GitHub advisory check runs response",
        error_type=AdvisoryCheckProjectionError,
    )
    raw_check_runs = current_payload.get("check_runs")
    if not isinstance(raw_check_runs, list):
        raise AdvisoryCheckProjectionError(
            "GitHub advisory check runs response must include check_runs."
        )
    matching_runs = tuple(
        json_object(
            item,
            "GitHub advisory check run",
            error_type=AdvisoryCheckProjectionError,
        )
        for item in raw_check_runs
        if isinstance(item, dict)
        and item.get("name") == projection.name
        and isinstance(item.get("head_sha"), str)
        and item["head_sha"].lower() == projection.head_sha
        and _app_id(item.get("app")) == installation_token.app_id
    )
    exact_match = next(
        (check_run for check_run in matching_runs if _matches_projection(check_run, projection)),
        None,
    )
    if exact_match is not None:
        return _result(
            status="replayed",
            projection=projection,
            installation_token=installation_token,
            check_run=exact_match,
        )
    current = matching_runs[0] if matching_runs else None
    body: dict[str, object] = {
        "name": projection.name,
        "status": projection.check_status,
        "external_id": projection.external_id,
        "details_url": projection.details_url,
        "output": {"title": projection.title, "summary": projection.summary},
    }
    if projection.conclusion is not None:
        body["conclusion"] = projection.conclusion
    status: AdvisoryCheckProjectionStatus
    recreate_incomplete = (
        current is not None
        and current.get("status") == "completed"
        and projection.check_status == "in_progress"
    )
    if current is None or recreate_incomplete:
        if recreate_incomplete:
            logger.info(
                "Creating a fresh in-progress advisory check after GitHub completed the prior run.",
                extra={
                    "advisory_check_name": projection.name,
                    "advisory_check_head_sha": projection.head_sha,
                },
            )
        response = _github_api_request(
            api_request,
            path=f"/repos/{repository_path}/check-runs",
            token=installation_token.token,
            method="POST",
            body={
                "name": projection.name,
                "head_sha": projection.head_sha,
                **body,
            },
        )
        status = "projected"
    else:
        check_run_id = required_positive_int(
            current.get("id"),
            "GitHub advisory check run requires id.",
            error_type=AdvisoryCheckProjectionError,
        )
        response = _github_api_request(
            api_request,
            path=f"/repos/{repository_path}/check-runs/{check_run_id}",
            token=installation_token.token,
            method="PATCH",
            body=body,
        )
        status = "updated"
    check_run = json_object(
        response,
        "GitHub advisory check run response",
        error_type=AdvisoryCheckProjectionError,
    )
    return _result(
        status=status,
        projection=projection,
        installation_token=installation_token,
        check_run=check_run,
    )


def _matches_projection(check_run: dict[str, object], projection: AdvisoryCheckProjection) -> bool:
    output = check_run.get("output")
    return (
        check_run.get("status") == projection.check_status
        and _conclusion_matches(check_run, projection)
        and check_run.get("external_id") == projection.external_id
        and check_run.get("details_url") == projection.details_url
        and isinstance(output, dict)
        and output.get("title") == projection.title
        and output.get("summary") == projection.summary
    )


def _result(
    *,
    status: AdvisoryCheckProjectionStatus,
    projection: AdvisoryCheckProjection,
    installation_token: GitHubAppInstallationToken,
    check_run: dict[str, object],
) -> AdvisoryCheckProjectionResult:
    output = check_run.get("output")
    if (
        required_string_text(
            check_run.get("name"),
            "GitHub advisory check run response requires name.",
            error_type=AdvisoryCheckProjectionError,
        )
        != projection.name
        or required_string_text(
            check_run.get("head_sha"),
            "GitHub advisory check run response requires head_sha.",
            error_type=AdvisoryCheckProjectionError,
        ).lower()
        != projection.head_sha
        or required_string_text(
            check_run.get("external_id"),
            "GitHub advisory check run response requires external_id.",
            error_type=AdvisoryCheckProjectionError,
        )
        != projection.external_id
        or _app_id(check_run.get("app")) != installation_token.app_id
        or check_run.get("status") != projection.check_status
        or not _conclusion_matches(check_run, projection)
        or check_run.get("details_url") != projection.details_url
        or not isinstance(output, dict)
        or output.get("title") != projection.title
        or output.get("summary") != projection.summary
    ):
        raise AdvisoryCheckProjectionError(
            "GitHub advisory check run response did not match exact projection identity."
        )
    return AdvisoryCheckProjectionResult(
        status=status,
        name=projection.name,
        head_sha=projection.head_sha,
        external_id=projection.external_id,
        app_id=installation_token.app_id,
        installation_id=installation_token.installation_id,
        check_run_id=required_positive_int(
            check_run.get("id"),
            "GitHub advisory check run response requires id.",
            error_type=AdvisoryCheckProjectionError,
        ),
        check_status=projection.check_status,
        conclusion=projection.conclusion,
    )


def _conclusion_matches(check_run: dict[str, object], projection: AdvisoryCheckProjection) -> bool:
    if projection.conclusion is None:
        return check_run.get("conclusion") is None
    return check_run.get("conclusion") == projection.conclusion


def _app_id(value: object) -> int:
    if not isinstance(value, dict):
        return 0
    raw_id = value.get("id")
    if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0:
        return raw_id
    return 0


def _github_api_request(
    api_request: GitHubApiRequest,
    **kwargs: object,
) -> object:
    try:
        return api_request(**kwargs)
    except click.ClickException as error:
        raise AdvisoryCheckProjectionError(str(error)) from error
