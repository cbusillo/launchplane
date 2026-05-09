from __future__ import annotations

from datetime import datetime, timezone
from typing import Protocol
from urllib.parse import urlencode

from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.contracts.runner_lane_inventory import RunnerLaneRecord
from control_plane.contracts.runner_lane_inventory import build_runner_lane_inventory
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.merge_train_github import MergeTrainGitHubTransport


class RunnerLaneClock(Protocol):
    def now(self) -> datetime: ...


class SystemRunnerLaneClock:
    def now(self) -> datetime:
        return datetime.now(timezone.utc).replace(microsecond=0)


class GitHubRunnerLaneInventoryReader:
    def __init__(
        self,
        *,
        transport: MergeTrainGitHubTransport,
        clock: RunnerLaneClock | None = None,
    ) -> None:
        self.transport = transport
        self.clock = clock or SystemRunnerLaneClock()

    def read_runner_lane_inventory(self, *, repository: str) -> RunnerLaneInventory:
        repository_path = _repository_path(repository)
        observed_at = self.clock.now().isoformat().replace("+00:00", "Z")
        lanes: list[RunnerLaneRecord] = []
        page = 1
        while True:
            query = urlencode({"per_page": "100", "page": str(page)})
            payload = self.transport.request(
                method="GET", path=f"/repos/{repository_path}/actions/runners?{query}"
            )
            payload_object = _json_object(payload, "GitHub runner list response")
            runners_payload = payload_object.get("runners")
            if not isinstance(runners_payload, list):
                raise MergeTrainGitHubError(
                    "GitHub runner list response must include runners."
                )
            page_lanes = tuple(
                _runner_lane_record(
                    repository=repository,
                    observed_at=observed_at,
                    payload=_json_object(runner, "GitHub runner entry"),
                )
                for runner in runners_payload
            )
            lanes.extend(page_lanes)
            if len(page_lanes) < 100:
                return build_runner_lane_inventory(
                    repository=repository,
                    observed_at=observed_at,
                    lanes=tuple(lanes),
                )
            page += 1


def _runner_lane_record(
    *, repository: str, observed_at: str, payload: dict[str, object]
) -> RunnerLaneRecord:
    return RunnerLaneRecord(
        github_id=_required_int(payload.get("id"), "GitHub runner entry requires id."),
        name=_required_text(payload.get("name"), "GitHub runner entry requires name."),
        repository=repository,
        status=_required_text(payload.get("status"), "GitHub runner entry requires status."),
        busy=bool(payload.get("busy")),
        labels=_runner_labels(payload.get("labels")),
        host_hint=_host_hint_from_name(
            _required_text(payload.get("name"), "GitHub runner entry requires name.")
        ),
        observed_at=observed_at,
    )


def _runner_labels(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    if not isinstance(value, list):
        raise MergeTrainGitHubError("GitHub runner labels must be a JSON array.")
    labels: list[str] = []
    for item in value:
        label = _json_object(item, "GitHub runner label")
        labels.append(_required_text(label.get("name"), "GitHub runner label requires name."))
    return tuple(labels)


def _host_hint_from_name(name: str) -> str:
    normalized_name = name.strip()
    if not normalized_name:
        return ""
    for separator in (":", "/"):
        if separator in normalized_name:
            return normalized_name.split(separator, 1)[0].strip()
    segments = [segment for segment in normalized_name.split("-") if segment]
    if len(segments) >= 3:
        return "-".join(segments[:2])
    return normalized_name


def _repository_path(repository: str) -> str:
    normalized_repository = repository.strip()
    if normalized_repository.count("/") != 1:
        raise ValueError("GitHub repository must be formatted as owner/name.")
    owner, repo = normalized_repository.split("/", 1)
    if not owner or not repo:
        raise ValueError("GitHub repository must be formatted as owner/name.")
    return normalized_repository


def _json_object(value: object, label: str) -> dict[str, object]:
    if not isinstance(value, dict):
        raise MergeTrainGitHubError(f"{label} must be a JSON object.")
    return value


def _required_text(value: object, message: str) -> str:
    normalized_value = str(value or "").strip()
    if not normalized_value:
        raise MergeTrainGitHubError(message)
    return normalized_value


def _required_int(value: object, message: str) -> int:
    if not isinstance(value, int) or isinstance(value, bool):
        raise MergeTrainGitHubError(message)
    return value
