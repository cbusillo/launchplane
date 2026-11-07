import json
from collections.abc import Callable
from urllib.error import HTTPError, URLError
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
        except (OSError, HTTPError, URLError, ValueError) as error:
            raise MergeTrainGitHubError("Runner lifecycle service operation failed.") from error
        if result.repository != request.repository or result.operation != request.operation:
            raise MergeTrainGitHubError("Runner lifecycle response belongs to another operation.")
        return result.payload

    def read_runner_lane_inventory(self, *, repository: str) -> RunnerLaneInventory:
        return RunnerLaneInventory.model_validate(
            self.operation(RunnerLaneServiceRequest(repository=repository, operation="inventory"))
        )

    def fetch_registration_token(
        self, *, repository: str
    ) -> tuple[str, RunnerLaneRegistrationTokenRecord]:
        payload = self.operation(
            RunnerLaneServiceRequest(repository=repository, operation="registration_token")
        )
        token = payload.get("token")
        record = RunnerLaneRegistrationTokenRecord.model_validate(payload.get("record"))
        if not isinstance(token, str) or not token or record.repository != repository:
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
