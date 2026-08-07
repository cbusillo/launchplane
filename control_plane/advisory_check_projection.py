from __future__ import annotations

from collections.abc import Callable
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
    current = next(
        (
            json_object(
                item,
                "GitHub advisory check run",
                error_type=AdvisoryCheckProjectionError,
            )
            for item in raw_check_runs
            if isinstance(item, dict)
            and str(item.get("name") or "") == projection.name
            and str(item.get("head_sha") or "").lower() == projection.head_sha
            and _app_id(item.get("app")) == installation_token.app_id
        ),
        None,
    )
    if current is not None and _matches_projection(current, projection):
        return _result(
            status="replayed",
            projection=projection,
            installation_token=installation_token,
            check_run=current,
        )
    body = {
        "name": projection.name,
        "status": "completed",
        "conclusion": "neutral",
        "external_id": projection.external_id,
        "details_url": projection.details_url,
        "output": {"title": projection.title, "summary": projection.summary},
    }
    status: AdvisoryCheckProjectionStatus
    if current is None:
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
        str(check_run.get("status") or "") == "completed"
        and str(check_run.get("conclusion") or "") == "neutral"
        and str(check_run.get("external_id") or "") == projection.external_id
        and str(check_run.get("details_url") or "") == projection.details_url
        and isinstance(output, dict)
        and str(output.get("title") or "") == projection.title
        and str(output.get("summary") or "") == projection.summary
    )


def _result(
    *,
    status: AdvisoryCheckProjectionStatus,
    projection: AdvisoryCheckProjection,
    installation_token: GitHubAppInstallationToken,
    check_run: dict[str, object],
) -> AdvisoryCheckProjectionResult:
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
        or str(check_run.get("status") or "") != "completed"
        or str(check_run.get("conclusion") or "") != "neutral"
        or str(check_run.get("details_url") or "") != projection.details_url
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
    )


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
