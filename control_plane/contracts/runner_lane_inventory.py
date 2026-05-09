from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field, model_validator


class RunnerLaneRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    github_id: int = Field(ge=1)
    name: str
    repository: str
    status: str
    busy: bool
    labels: tuple[str, ...] = ()
    host_hint: str = ""
    observed_at: str

    @model_validator(mode="after")
    def _normalize_record(self) -> "RunnerLaneRecord":
        self.name = _required_text(self.name, "runner lane requires name")
        self.repository = _required_text(
            self.repository, "runner lane requires repository"
        )
        self.status = _required_text(self.status, "runner lane requires status").lower()
        self.labels = tuple(sorted({label.strip() for label in self.labels if label.strip()}))
        self.host_hint = self.host_hint.strip()
        self.observed_at = _required_text(
            self.observed_at, "runner lane requires observed_at"
        )
        return self


class RunnerLaneInventory(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    observed_at: str
    lanes: tuple[RunnerLaneRecord, ...]
    total_lanes: int = Field(ge=0)
    online_lanes: int = Field(ge=0)
    busy_lanes: int = Field(ge=0)
    idle_lanes: int = Field(ge=0)
    offline_lanes: int = Field(ge=0)
    capacity_constrained: bool
    capacity_reason: str

    @model_validator(mode="after")
    def _normalize_inventory(self) -> "RunnerLaneInventory":
        self.repository = _required_text(
            self.repository, "runner lane inventory requires repository"
        )
        self.observed_at = _required_text(
            self.observed_at, "runner lane inventory requires observed_at"
        )
        self.lanes = tuple(sorted(self.lanes, key=lambda lane: (lane.name, lane.github_id)))
        self.capacity_reason = self.capacity_reason.strip()
        return self


def build_runner_lane_inventory(
    *, repository: str, observed_at: str, lanes: tuple[RunnerLaneRecord, ...]
) -> RunnerLaneInventory:
    online_lanes = tuple(lane for lane in lanes if lane.status == "online")
    busy_lanes = tuple(lane for lane in online_lanes if lane.busy)
    idle_lanes = tuple(lane for lane in online_lanes if not lane.busy)
    offline_lanes = tuple(lane for lane in lanes if lane.status != "online")
    capacity_constrained = not idle_lanes
    capacity_reason = "idle lane available"
    if not lanes:
        capacity_reason = "no self-hosted runner lanes discovered"
    elif not online_lanes:
        capacity_reason = "no online self-hosted runner lanes discovered"
    elif capacity_constrained:
        capacity_reason = "all online self-hosted runner lanes are busy"
    return RunnerLaneInventory(
        repository=repository,
        observed_at=observed_at,
        lanes=lanes,
        total_lanes=len(lanes),
        online_lanes=len(online_lanes),
        busy_lanes=len(busy_lanes),
        idle_lanes=len(idle_lanes),
        offline_lanes=len(offline_lanes),
        capacity_constrained=capacity_constrained,
        capacity_reason=capacity_reason,
    )


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value
