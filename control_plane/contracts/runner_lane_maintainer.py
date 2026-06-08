from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runner_lane_baseline import RunnerLaneBaselineReadiness
from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory
from control_plane.contracts.runner_lane_inventory import RunnerLaneRecord


RunnerLaneMaintainerPlanStatus = Literal["ready", "blocked"]
RunnerLaneMaintainerDecision = Literal[
    "recommend_create",
    "recommend_verify_adoption",
    "recommend_remove_recreate",
    "blocked",
]
RunnerLaneMaintainerBlockerKind = Literal["policy", "capability"]
RunnerLaneMaintainerBlockerCode = Literal[
    "approved_host_missing",
    "baseline_not_ready",
    "desired_label_missing",
    "existing_lane_not_managed",
    "host_not_allowed",
    "inventory_repository_mismatch",
    "lane_name_ambiguous",
    "repository_not_allowed",
    "runner_directory_not_allowed",
    "service_user_not_allowed",
    "supervised_maintainer_required",
]


class RunnerLaneMaintainerPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    allowed_repositories: tuple[str, ...] = ()
    approved_hosts: tuple[str, ...] = ()
    allowed_registration_roots: tuple[str, ...] = ()
    allowed_service_users: tuple[str, ...] = ()
    required_labels: tuple[str, ...] = (
        "self-hosted",
        "launchplane",
        "launchplane-managed",
    )
    required_managed_label: str = "launchplane-managed"
    require_baseline_readiness: bool = True

    @model_validator(mode="after")
    def _normalize_policy(self) -> "RunnerLaneMaintainerPolicy":
        self.allowed_repositories = _normalized_repositories(self.allowed_repositories)
        self.approved_hosts = _normalized_identifiers(self.approved_hosts, "approved host")
        self.allowed_registration_roots = tuple(
            sorted(
                {_normalized_path(root) for root in self.allowed_registration_roots if root.strip()}
            )
        )
        self.allowed_service_users = _normalized_identifiers(
            self.allowed_service_users, "service user"
        )
        self.required_labels = _normalized_identifiers(self.required_labels, "label")
        self.required_managed_label = _normalized_identifier(
            self.required_managed_label, "managed label"
        )
        if not self.required_labels:
            raise ValueError("runner lane maintainer policy requires labels")
        return self


class RunnerLaneDesiredState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    host_name: str
    lane_name: str
    runner_directory: str
    service_user: str
    systemd_unit_name: str
    labels: tuple[str, ...]
    runner_version: str = "latest-approved"

    @model_validator(mode="after")
    def _normalize_state(self) -> "RunnerLaneDesiredState":
        self.repository = _normalized_repository(self.repository)
        self.host_name = _normalized_identifier(self.host_name, "host_name")
        self.lane_name = _normalized_identifier(self.lane_name, "lane_name")
        self.runner_directory = _normalized_path(self.runner_directory)
        self.service_user = _normalized_identifier(self.service_user, "service_user")
        self.systemd_unit_name = _normalized_systemd_unit_name(self.systemd_unit_name)
        self.labels = _normalized_identifiers(self.labels, "label")
        self.runner_version = _required_text(
            self.runner_version, "runner lane maintainer desired state requires runner_version"
        )
        if not self.labels:
            raise ValueError("runner lane maintainer desired state requires labels")
        if self.runner_directory.rsplit("/", 1)[-1] != self.lane_name:
            raise ValueError("runner lane maintainer runner_directory must be the lane directory")
        expected_unit_name = f"launchplane-runner@{self.lane_name}.service"
        if self.systemd_unit_name != expected_unit_name:
            raise ValueError("runner lane maintainer systemd_unit_name must match the lane name")
        return self


class RunnerLaneMaintainerBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: RunnerLaneMaintainerBlockerCode
    message: str
    kind: RunnerLaneMaintainerBlockerKind = "policy"

    @model_validator(mode="after")
    def _normalize_blocker(self) -> "RunnerLaneMaintainerBlocker":
        self.message = _required_text(
            self.message, "runner lane maintainer blocker requires message"
        )
        return self


class RunnerLaneMaintainerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: RunnerLaneMaintainerPlanStatus
    decision: RunnerLaneMaintainerDecision
    policy_ready: bool
    capability_ready: bool
    desired_state: RunnerLaneDesiredState
    matching_lanes: tuple[RunnerLaneRecord, ...] = ()
    blockers: tuple[RunnerLaneMaintainerBlocker, ...] = ()
    next_steps: tuple[str, ...] = ()
    summary: str

    @model_validator(mode="after")
    def _normalize_plan(self) -> "RunnerLaneMaintainerPlan":
        self.matching_lanes = tuple(
            sorted(self.matching_lanes, key=lambda lane: (lane.name.lower(), lane.github_id))
        )
        self.blockers = tuple(sorted(self.blockers, key=lambda blocker: blocker.code))
        self.next_steps = tuple(step.strip() for step in self.next_steps if step.strip())
        self.summary = _required_text(self.summary, "runner lane maintainer plan requires summary")
        if self.status == "ready" and self.blockers:
            raise ValueError("ready runner lane maintainer plan cannot include blockers")
        if self.status == "blocked" and not self.blockers:
            raise ValueError("blocked runner lane maintainer plan requires blockers")
        return self


def plan_runner_lane_maintainer(
    *,
    policy: RunnerLaneMaintainerPolicy,
    desired_state: RunnerLaneDesiredState,
    inventory: RunnerLaneInventory,
    baseline_readiness: RunnerLaneBaselineReadiness,
) -> RunnerLaneMaintainerPlan:
    blockers: list[RunnerLaneMaintainerBlocker] = []
    inventory_repository = _normalized_repository(inventory.repository)
    if (
        not policy.allowed_repositories
        or desired_state.repository not in policy.allowed_repositories
    ):
        blockers.append(
            _blocker(
                "repository_not_allowed",
                f"repository is not opted into runner lane maintenance: {desired_state.repository}",
            )
        )
    if inventory_repository != desired_state.repository:
        blockers.append(
            _blocker(
                "inventory_repository_mismatch",
                "runner lane inventory repository does not match desired state: "
                f"{inventory_repository}",
            )
        )
    if not policy.approved_hosts:
        blockers.append(
            _blocker(
                "approved_host_missing", "runner lane maintainer policy requires approved hosts"
            )
        )
    elif desired_state.host_name not in policy.approved_hosts:
        blockers.append(
            _blocker(
                "host_not_allowed",
                f"runner host is not approved for maintenance: {desired_state.host_name}",
            )
        )
    if not _path_under_allowed_roots(
        path=desired_state.runner_directory,
        allowed_roots=policy.allowed_registration_roots,
    ):
        blockers.append(
            _blocker(
                "runner_directory_not_allowed",
                "runner directory is not under an allowed registration root: "
                f"{desired_state.runner_directory}",
            )
        )
    if (
        not policy.allowed_service_users
        or desired_state.service_user not in policy.allowed_service_users
    ):
        blockers.append(
            _blocker(
                "service_user_not_allowed",
                f"runner service user is not approved: {desired_state.service_user}",
            )
        )
    for label in policy.required_labels:
        if label not in desired_state.labels:
            blockers.append(
                _blocker(
                    "desired_label_missing",
                    f"runner desired state is missing required label: {label}",
                )
            )
    if policy.require_baseline_readiness and not baseline_readiness.ready:
        blockers.append(
            _blocker(
                "baseline_not_ready",
                f"runner lane baseline is not ready: {baseline_readiness.summary}",
            )
        )

    matching_lanes = tuple(
        lane for lane in inventory.lanes if lane.name.strip().lower() == desired_state.lane_name
    )
    if len(matching_lanes) > 1:
        blockers.append(
            _blocker(
                "lane_name_ambiguous",
                f"runner lane name matches multiple inventory records: {desired_state.lane_name}",
            )
        )
    if len(matching_lanes) == 1:
        lane = matching_lanes[0]
        lane_labels = set(_normalized_identifiers(lane.labels, "label"))
        if policy.required_managed_label not in lane_labels:
            blockers.append(
                _blocker(
                    "existing_lane_not_managed",
                    "existing runner lane is not marked as Launchplane-managed: "
                    f"{desired_state.lane_name}",
                )
            )

    decision = _decision(blockers=tuple(blockers), matching_lanes=matching_lanes)
    if decision != "blocked":
        blockers.append(
            _blocker(
                "supervised_maintainer_required",
                "runner lane maintainer host mutation requires the supervised maintainer capability",
                kind="capability",
            )
        )
    status: RunnerLaneMaintainerPlanStatus = "blocked" if blockers else "ready"
    return RunnerLaneMaintainerPlan(
        status=status,
        decision=decision,
        policy_ready=not any(blocker.kind == "policy" for blocker in blockers),
        capability_ready=not any(blocker.kind == "capability" for blocker in blockers),
        desired_state=desired_state,
        matching_lanes=matching_lanes,
        blockers=tuple(blockers),
        next_steps=_next_steps(decision=decision),
        summary=_summary(decision=decision, status=status),
    )


def _decision(
    *,
    blockers: tuple[RunnerLaneMaintainerBlocker, ...],
    matching_lanes: tuple[RunnerLaneRecord, ...],
) -> RunnerLaneMaintainerDecision:
    if blockers:
        return "blocked"
    if not matching_lanes:
        return "recommend_create"
    lane = matching_lanes[0]
    if lane.status == "online":
        return "recommend_verify_adoption"
    return "recommend_remove_recreate"


def _next_steps(*, decision: RunnerLaneMaintainerDecision) -> tuple[str, ...]:
    if decision == "blocked":
        return ("resolve blockers before any runner maintainer host mutation",)
    if decision == "recommend_create":
        return (
            "future maintainer should prepare a supervised systemd-backed runner service under the approved root",
            "future maintainer should request a short-lived GitHub registration token inside the maintainer executor",
            "future maintainer should verify systemd active state, GitHub online inventory, and baseline readiness",
        )
    if decision == "recommend_verify_adoption":
        return (
            "future maintainer should verify the existing online runner has a matching Launchplane-owned systemd unit",
            "future maintainer should verify service user, runner directory, labels, and baseline evidence match desired state",
        )
    return (
        "future maintainer should remove the stale GitHub runner registration through an audited maintainer path",
        "future maintainer should recreate the runner only through a supervised systemd-backed service",
        "future maintainer should verify post-recreate inventory and baseline evidence before admitting product jobs",
    )


def _summary(
    *, decision: RunnerLaneMaintainerDecision, status: RunnerLaneMaintainerPlanStatus
) -> str:
    if status == "blocked":
        return "runner lane maintainer plan is blocked"
    if decision == "recommend_create":
        return "runner lane maintainer should create a supervised runner service"
    if decision == "recommend_verify_adoption":
        return "runner lane maintainer should verify existing runner adoption evidence"
    return "runner lane maintainer should remove and recreate the stale runner lane"


def _blocker(
    code: RunnerLaneMaintainerBlockerCode,
    message: str,
    *,
    kind: RunnerLaneMaintainerBlockerKind = "policy",
) -> RunnerLaneMaintainerBlocker:
    return RunnerLaneMaintainerBlocker(code=code, message=message, kind=kind)


def _path_under_allowed_roots(*, path: str, allowed_roots: tuple[str, ...]) -> bool:
    if not allowed_roots:
        return False
    normalized_path = _normalized_path(path)
    for root in allowed_roots:
        normalized_root = _normalized_path(root)
        if normalized_path == normalized_root or normalized_path.startswith(f"{normalized_root}/"):
            return True
    return False


def _normalized_repositories(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        sorted({normalized for value in values if (normalized := _normalized_repository(value))})
    )


def _normalized_repository(value: str) -> str:
    normalized_value = value.strip().lower()
    if not normalized_value:
        return ""
    normalized_repository = "/".join(part.strip() for part in normalized_value.split("/"))
    if normalized_repository.count("/") != 1:
        raise ValueError("runner lane maintainer requires repository formatted as owner/name")
    owner, name = normalized_repository.split("/", 1)
    if not owner or not name:
        raise ValueError("runner lane maintainer requires repository formatted as owner/name")
    return normalized_repository


def _normalized_identifiers(values: tuple[str, ...], label: str) -> tuple[str, ...]:
    return tuple(
        sorted({_normalized_identifier(value, label) for value in values if value.strip()})
    )


def _normalized_identifier(value: str, label: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", normalized):
        raise ValueError(
            f"runner lane maintainer {label} must use letters, numbers, dots, underscores, or hyphens"
        )
    return normalized


def _normalized_systemd_unit_name(value: str) -> str:
    normalized = value.strip().lower()
    if not re.fullmatch(r"launchplane-runner@[a-z0-9][a-z0-9._-]{0,127}\.service", normalized):
        raise ValueError(
            "runner lane maintainer systemd_unit_name must match launchplane-runner@<lane>.service"
        )
    return normalized


def _normalized_path(value: str) -> str:
    normalized = value.strip().rstrip("/")
    if not normalized.startswith("/"):
        raise ValueError("runner lane maintainer paths must be absolute")
    segments: list[str] = []
    for segment in normalized.split("/"):
        if segment in {"", "."}:
            continue
        if segment == "..":
            if segments:
                segments.pop()
            continue
        segments.append(segment)
    return "/" + "/".join(segments)


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value
