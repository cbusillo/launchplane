from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselineReadiness
from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory


RunnerLaneControlAction = Literal["create", "drain", "restart", "remove"]
RunnerLaneControlPlanStatus = Literal["ready", "blocked"]
RunnerLaneControlBlockerCode = Literal[
    "action_not_enabled",
    "baseline_not_ready",
    "busy_lane_requires_drain_confirmation",
    "inventory_repository_mismatch",
    "lane_already_exists",
    "lane_name_ambiguous",
    "lane_not_found",
    "managed_label_missing",
    "mutate_not_requested",
    "repository_not_allowed",
]


class RunnerLaneControlPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    allowed_repositories: tuple[str, ...] = ()
    required_managed_label: str = "launchplane-managed"
    allow_create: bool = False
    allow_drain: bool = False
    allow_restart: bool = False
    allow_remove: bool = False

    @model_validator(mode="after")
    def _normalize_policy(self) -> "RunnerLaneControlPolicy":
        self.allowed_repositories = _normalized_repositories(self.allowed_repositories)
        self.required_managed_label = _normalized_token(self.required_managed_label)
        if not self.required_managed_label:
            raise ValueError("runner lane control policy requires managed label")
        return self


class RunnerLaneControlRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    action: RunnerLaneControlAction
    repository: str
    lane_name: str
    mutate: bool = False
    drain_busy_lane: bool = False

    @model_validator(mode="after")
    def _normalize_request(self) -> "RunnerLaneControlRequest":
        self.repository = _normalized_repository(self.repository)
        self.lane_name = _required_text(self.lane_name, "runner lane control requires lane_name")
        if not self.repository:
            raise ValueError("runner lane control requires repository")
        return self


class RunnerLaneControlBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: RunnerLaneControlBlockerCode
    message: str

    @model_validator(mode="after")
    def _normalize_blocker(self) -> "RunnerLaneControlBlocker":
        self.message = _required_text(self.message, "runner lane control blocker requires message")
        return self


class RunnerLaneControlPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: RunnerLaneControlPlanStatus
    action: RunnerLaneControlAction
    repository: str
    lane_name: str
    mutate: bool
    blockers: tuple[RunnerLaneControlBlocker, ...] = ()
    next_steps: tuple[str, ...] = ()
    summary: str

    @model_validator(mode="after")
    def _normalize_plan(self) -> "RunnerLaneControlPlan":
        self.repository = _required_text(
            self.repository, "runner lane control plan requires repository"
        )
        self.lane_name = _required_text(
            self.lane_name, "runner lane control plan requires lane_name"
        )
        self.blockers = tuple(sorted(self.blockers, key=lambda blocker: blocker.code))
        self.next_steps = tuple(step.strip() for step in self.next_steps if step.strip())
        self.summary = _required_text(self.summary, "runner lane control plan requires summary")
        if self.status == "ready" and self.blockers:
            raise ValueError("ready runner lane control plan cannot include blockers")
        if self.status == "blocked" and not self.blockers:
            raise ValueError("blocked runner lane control plan requires blockers")
        return self


def plan_runner_lane_control(
    *,
    policy: RunnerLaneControlPolicy,
    request: RunnerLaneControlRequest,
    inventory: RunnerLaneInventory,
    baseline_readiness: RunnerLaneBaselineReadiness,
) -> RunnerLaneControlPlan:
    blockers: list[RunnerLaneControlBlocker] = []
    inventory_repository = _normalized_repository(inventory.repository)
    if not policy.allowed_repositories or request.repository not in policy.allowed_repositories:
        blockers.append(
            _blocker(
                "repository_not_allowed",
                f"repository is not opted into runner lane control: {request.repository}",
            )
        )
    if not _action_allowed(policy=policy, action=request.action):
        blockers.append(
            _blocker(
                "action_not_enabled",
                f"runner lane control action is not enabled by policy: {request.action}",
            )
        )
    if not request.mutate:
        blockers.append(
            _blocker(
                "mutate_not_requested",
                "runner lane control is dry-run only until mutate is explicitly requested",
            )
        )
    if not baseline_readiness.ready:
        blockers.append(
            _blocker(
                "baseline_not_ready",
                f"runner lane baseline is not ready: {baseline_readiness.summary}",
            )
        )

    if inventory_repository != request.repository:
        blockers.append(
            _blocker(
                "inventory_repository_mismatch",
                (
                    "runner lane inventory repository does not match request: "
                    f"{inventory_repository}"
                ),
            )
        )
    matching_lanes = tuple(item for item in inventory.lanes if item.name == request.lane_name)
    lane = matching_lanes[0] if len(matching_lanes) == 1 else None
    if len(matching_lanes) > 1:
        blockers.append(
            _blocker(
                "lane_name_ambiguous",
                f"runner lane name matches multiple inventory records: {request.lane_name}",
            )
        )
    if request.action == "create" and lane is not None:
        blockers.append(
            _blocker("lane_already_exists", f"runner lane already exists: {request.lane_name}")
        )
    if request.action != "create" and lane is None and len(matching_lanes) <= 1:
        blockers.append(_blocker("lane_not_found", f"runner lane not found: {request.lane_name}"))
    if request.action in {"drain", "restart", "remove"} and lane is not None:
        if policy.required_managed_label not in _normalized_tokens(lane.labels):
            blockers.append(
                _blocker(
                    "managed_label_missing",
                    f"runner lane is missing managed label: {policy.required_managed_label}",
                )
            )
        if (
            lane.busy
            and request.action in {"drain", "restart", "remove"}
            and not request.drain_busy_lane
        ):
            blockers.append(
                _blocker(
                    "busy_lane_requires_drain_confirmation",
                    "busy runner lane requires explicit drain confirmation",
                )
            )

    status: RunnerLaneControlPlanStatus = "blocked" if blockers else "ready"
    return RunnerLaneControlPlan(
        status=status,
        action=request.action,
        repository=request.repository,
        lane_name=request.lane_name,
        mutate=request.mutate,
        blockers=tuple(blockers),
        next_steps=_next_steps(action=request.action, status=status),
        summary=(
            "runner lane control plan is ready"
            if status == "ready"
            else "runner lane control plan is blocked"
        ),
    )


def _action_allowed(*, policy: RunnerLaneControlPolicy, action: RunnerLaneControlAction) -> bool:
    if action == "create":
        return policy.allow_create
    if action == "drain":
        return policy.allow_drain
    if action == "restart":
        return policy.allow_restart
    return policy.allow_remove


def _next_steps(
    *, action: RunnerLaneControlAction, status: RunnerLaneControlPlanStatus
) -> tuple[str, ...]:
    if status == "blocked":
        return ("resolve blockers before running any host or GitHub runner mutation",)
    if action == "create":
        return (
            "create runner lane through an approved host adapter",
            "verify GitHub sees the lane online",
        )
    if action == "drain":
        return ("stop admitting new jobs to the managed lane", "wait for active jobs to finish")
    if action == "restart":
        return (
            "drain the managed lane",
            "restart the approved runner service",
            "verify the lane returns online",
        )
    return ("drain the managed lane", "remove only the Launchplane-owned runner registration")


def _blocker(code: RunnerLaneControlBlockerCode, message: str) -> RunnerLaneControlBlocker:
    return RunnerLaneControlBlocker(code=code, message=message)


def _normalized_repositories(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted({normalized for value in values if (normalized := _normalized_repository(value))})
    )


def _normalized_tokens(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted({normalized for value in values if (normalized := _normalized_token(value))})
    )


def _normalized_repository(value: str) -> str:
    normalized_value = value.strip().lower()
    if not normalized_value:
        return ""
    normalized_repository = "/".join(part.strip() for part in normalized_value.split("/"))
    if normalized_repository.count("/") != 1:
        raise ValueError("runner lane control requires repository formatted as owner/name")
    owner, name = normalized_repository.split("/", 1)
    if not owner or not name:
        raise ValueError("runner lane control requires repository formatted as owner/name")
    return normalized_repository


def _normalized_token(value: str) -> str:
    return value.strip().lower()


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value
