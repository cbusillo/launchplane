import json
from collections.abc import Callable
from urllib.error import HTTPError
from http.client import HTTPException
import logging
import re
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from typing import Any, cast

from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.contracts.runner_lane_registration import RunnerLaneRegistrationTokenRecord
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.runner_lane_service import (
    RUNNER_LANE_SERVICE_ROUTE,
    RunnerLaneServiceRequest,
    RunnerLaneServiceResponse,
)
from control_plane.workflows.runner_lane_ephemeral import AddonRunnerRequest

_LOGGER = logging.getLogger(__name__)


def _service_error(error: HTTPError) -> MergeTrainGitHubError:
    diagnostics: list[str] = []
    try:
        payload = json.loads(error.read(65536))
        detail = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(detail, dict) and isinstance(payload, dict):
            stage = detail.get("reason")
            for value in (detail.get("code"), payload.get("trace_id"), stage):
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value):
                    diagnostics.append(value)
    except (OSError, ValueError, HTTPException):
        diagnostics = []
    suffix = f" ({'; '.join(diagnostics)})" if diagnostics else ""
    return MergeTrainGitHubError(
        f"Runner lifecycle service operation failed{suffix}.",
        status_code=error.code,
    )


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


class ServiceRunnerLaneClient:
    def __init__(self, *, service_url: str, bearer_token_provider: Callable[[], str]) -> None:
        self.base = service_url.rstrip("/")
        parsed = urlsplit(self.base)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
        ):
            raise ValueError("Runner lifecycle requires a trusted HTTPS service URL.")
        self.bearer_token_provider = bearer_token_provider

    def operation(self, request: RunnerLaneServiceRequest) -> dict[str, object]:
        http_request = Request(
            self.base + RUNNER_LANE_SERVICE_ROUTE,
            data=request.model_dump_json().encode(),
            headers={
                "Authorization": f"Bearer {self.bearer_token_provider()}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with build_opener(_NoRedirect()).open(http_request, timeout=120) as response:
                result = RunnerLaneServiceResponse.model_validate(json.load(response))
        except HTTPError as error:
            raise _service_error(error) from error
        except (OSError, ValueError, HTTPException) as error:
            raise MergeTrainGitHubError("Runner lifecycle service operation failed.") from error
        if result.repository != request.repository or result.operation != request.operation:
            raise MergeTrainGitHubError("Runner lifecycle response belongs to another operation.")
        if not result.credential_revoked:
            _LOGGER.warning(
                "Runner lifecycle operation completed; installation credential revocation is unconfirmed."
            )
        return result.payload

    def read_runner_lane_inventory(self, *, repository: str) -> RunnerLaneInventory:
        return RunnerLaneInventory.model_validate(
            self.operation(RunnerLaneServiceRequest(repository=repository, operation="inventory"))
        )

    def read_addon_job(self, request: AddonRunnerRequest) -> dict[str, object]:
        return self.operation(
            RunnerLaneServiceRequest(
                repository=request.repository,
                operation="addon_job",
                run_id=request.run_id,
                run_attempt=request.run_attempt,
                job_id=request.job_id,
            )
        )

    def fetch_registration_token(
        self, *, repository: str
    ) -> tuple[str, RunnerLaneRegistrationTokenRecord]:
        payload = self.operation(
            RunnerLaneServiceRequest(repository=repository, operation="registration_token")
        )
        token = payload.get("token")
        record = RunnerLaneRegistrationTokenRecord.model_validate(payload.get("record"))
        if (
            not isinstance(token, str)
            or not token
            or record.repository.casefold() != repository.strip().casefold()
        ):
            raise MergeTrainGitHubError("Runner registration configuration is unavailable.")
        return token, record

    def read_active_run_ids(self, *, repository: str) -> tuple[int, ...]:
        payload = self.operation(
            RunnerLaneServiceRequest(repository=repository, operation="active_runs")
        )
        run_ids = payload.get("run_ids")
        if not isinstance(run_ids, list) or any(
            type(value) is not int or value <= 0 for value in run_ids
        ):
            raise MergeTrainGitHubError("Runner active-run evidence is unavailable.")
        return tuple(cast(list[int], run_ids))

    def delete_runner(self, *, repository: str, runner_id: int) -> None:
        self.operation(
            RunnerLaneServiceRequest(repository=repository, operation="retire", runner_id=runner_id)
        )
