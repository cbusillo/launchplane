from __future__ import annotations

from typing import Literal
import re

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runner_lane_inventory import RunnerLaneInventory


RunnerLaneRegistrationPlanStatus = Literal["ready", "blocked"]
RunnerLaneRegistrationAuditStatus = Literal["planned", "completed", "failed"]
RunnerLaneLifecycleOperation = Literal["register", "retire"]
RunnerLaneRegistrationBlockerCode = Literal[
    "audit_record_key_missing",
    "host_not_allowed",
    "inventory_repository_mismatch",
    "label_missing",
    "lane_already_exists",
    "lane_ambiguous",
    "lane_busy",
    "lane_missing",
    "mutate_not_requested",
    "registration_root_not_allowed",
    "repository_runs_active",
    "repository_not_allowed",
    "target_worker_active",
]


class RunnerLaneRegistrationPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    allowed_repositories: tuple[str, ...] = ()
    approved_hosts: tuple[str, ...] = ()
    allowed_registration_roots: tuple[str, ...] = ()
    required_labels: tuple[str, ...] = ("self-hosted", "launchplane", "launchplane-managed")
    require_audit_record: bool = True

    @model_validator(mode="after")
    def _normalize_policy(self) -> "RunnerLaneRegistrationPolicy":
        self.allowed_repositories = _normalized_repositories(self.allowed_repositories)
        self.approved_hosts = _normalized_tokens(self.approved_hosts)
        self.allowed_registration_roots = tuple(
            sorted(
                {_normalized_path(root) for root in self.allowed_registration_roots if root.strip()}
            )
        )
        self.required_labels = _normalized_labels(self.required_labels)
        if not self.required_labels:
            raise ValueError("runner lane registration policy requires at least one label")
        return self


class RunnerLaneRegistrationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    operation: RunnerLaneLifecycleOperation = "register"
    repository: str
    host_name: str
    lane_name: str
    registration_root: str
    labels: tuple[str, ...]
    active_run_ids: tuple[int, ...] = ()
    target_worker_active: bool = False
    mutate: bool = False
    audit_record_key: str = ""

    @model_validator(mode="after")
    def _normalize_request(self) -> "RunnerLaneRegistrationRequest":
        self.repository = _normalized_repository(self.repository)
        self.host_name = _required_text(
            self.host_name, "runner lane registration requires host_name"
        ).lower()
        self.lane_name = _required_text(
            self.lane_name, "runner lane registration requires lane_name"
        ).lower()
        _validate_slug(
            self.host_name,
            "runner lane registration host_name must use letters, numbers, dots, underscores, or hyphens",
        )
        _validate_slug(
            self.lane_name,
            "runner lane registration lane_name must use letters, numbers, dots, underscores, or hyphens",
        )
        self.registration_root = _normalized_path(
            _required_text(
                self.registration_root,
                "runner lane registration requires registration_root",
            )
        )
        self.labels = _normalized_labels(self.labels)
        self.active_run_ids = tuple(sorted(set(self.active_run_ids)))
        if any(run_id <= 0 for run_id in self.active_run_ids):
            raise ValueError("runner lane lifecycle active run ids must be positive")
        self.audit_record_key = self.audit_record_key.strip()
        if not self.repository:
            raise ValueError("runner lane registration requires repository")
        if self.operation == "register" and not self.labels:
            raise ValueError("runner lane registration requires labels")
        return self


class RunnerLaneRegistrationBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: RunnerLaneRegistrationBlockerCode
    message: str

    @model_validator(mode="after")
    def _normalize_blocker(self) -> "RunnerLaneRegistrationBlocker":
        self.message = _required_text(
            self.message, "runner lane registration blocker requires message"
        )
        return self


class RunnerLaneRegistrationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    operation: RunnerLaneLifecycleOperation = "register"
    status: RunnerLaneRegistrationPlanStatus
    repository: str
    host_name: str
    lane_name: str
    registration_root: str
    labels: tuple[str, ...]
    active_run_ids: tuple[int, ...] = ()
    target_worker_active: bool = False
    mutate: bool
    audit_record_key: str
    blockers: tuple[RunnerLaneRegistrationBlocker, ...] = ()
    next_steps: tuple[str, ...] = ()
    summary: str

    @model_validator(mode="after")
    def _normalize_plan(self) -> "RunnerLaneRegistrationPlan":
        self.repository = _required_text(
            self.repository, "runner lane registration plan requires repository"
        )
        self.host_name = _required_text(
            self.host_name, "runner lane registration plan requires host_name"
        )
        self.lane_name = _required_text(
            self.lane_name, "runner lane registration plan requires lane_name"
        )
        self.registration_root = _required_text(
            self.registration_root,
            "runner lane registration plan requires registration_root",
        )
        self.labels = _normalized_labels(self.labels)
        self.active_run_ids = tuple(sorted(set(self.active_run_ids)))
        self.audit_record_key = self.audit_record_key.strip()
        self.blockers = tuple(sorted(self.blockers, key=lambda blocker: blocker.code))
        self.next_steps = tuple(step.strip() for step in self.next_steps if step.strip())
        self.summary = _required_text(
            self.summary, "runner lane registration plan requires summary"
        )
        if self.status == "ready" and self.blockers:
            raise ValueError("ready runner lane registration plan cannot include blockers")
        if self.status == "blocked" and not self.blockers:
            raise ValueError("blocked runner lane registration plan requires blockers")
        return self


class RunnerLaneRegistrationAuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    audit_record_key: str
    status: RunnerLaneRegistrationAuditStatus
    request: RunnerLaneRegistrationRequest
    plan: RunnerLaneRegistrationPlan
    pre_inventory: RunnerLaneInventory
    post_inventory: RunnerLaneInventory | None = None
    message: str

    @model_validator(mode="after")
    def _normalize_record(self) -> "RunnerLaneRegistrationAuditRecord":
        self.audit_record_key = _required_text(
            self.audit_record_key,
            "runner lane registration audit record requires key",
        )
        if self.audit_record_key != self.request.audit_record_key:
            raise ValueError("runner lane registration audit key must match request")
        if self.audit_record_key != self.plan.audit_record_key:
            raise ValueError("runner lane registration audit key must match plan")
        if self.request.repository != self.plan.repository:
            raise ValueError("runner lane registration audit request must match plan repository")
        if self.request.operation != self.plan.operation:
            raise ValueError("runner lane registration audit request must match plan operation")
        if self.request.lane_name != self.plan.lane_name:
            raise ValueError("runner lane registration audit request must match plan lane")
        if self.pre_inventory.repository != self.request.repository:
            raise ValueError("runner lane registration pre-inventory must match request repository")
        if (
            self.post_inventory is not None
            and self.post_inventory.repository != self.request.repository
        ):
            raise ValueError(
                "runner lane registration post-inventory must match request repository"
            )
        self.message = _required_text(
            self.message, "runner lane registration audit record requires message"
        )
        return self


class RunnerLaneRegistrationTokenRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    repository: str
    expires_at: str
    fetched_at: str

    @model_validator(mode="after")
    def _normalize_record(self) -> "RunnerLaneRegistrationTokenRecord":
        self.repository = _normalized_repository(self.repository)
        self.expires_at = _required_text(
            self.expires_at, "runner lane registration token record requires expires_at"
        )
        self.fetched_at = _required_text(
            self.fetched_at, "runner lane registration token record requires fetched_at"
        )
        return self


def plan_runner_lane_registration(
    *,
    policy: RunnerLaneRegistrationPolicy,
    request: RunnerLaneRegistrationRequest,
    inventory: RunnerLaneInventory,
) -> RunnerLaneRegistrationPlan:
    if request.operation != "register":
        raise ValueError("runner lane registration planner requires register operation")
    blockers: list[RunnerLaneRegistrationBlocker] = []
    inventory_repository = _normalized_repository(inventory.repository)
    if not policy.allowed_repositories or request.repository not in policy.allowed_repositories:
        blockers.append(
            _blocker(
                "repository_not_allowed",
                f"repository is not opted into runner lane registration: {request.repository}",
            )
        )
    if not policy.approved_hosts or request.host_name.lower() not in policy.approved_hosts:
        blockers.append(
            _blocker(
                "host_not_allowed",
                f"runner host is not approved for lane registration: {request.host_name}",
            )
        )
    if not _path_under_allowed_roots(
        path=request.registration_root,
        allowed_roots=policy.allowed_registration_roots,
    ):
        blockers.append(
            _blocker(
                "registration_root_not_allowed",
                "runner registration root is not under an allowed root: "
                f"{request.registration_root}",
            )
        )
    if policy.require_audit_record and not request.audit_record_key:
        blockers.append(
            _blocker(
                "audit_record_key_missing",
                "runner lane registration requires an audit record key",
            )
        )
    if not request.mutate:
        blockers.append(
            _blocker(
                "mutate_not_requested",
                "runner lane registration is dry-run only until mutate is explicitly requested",
            )
        )
    if inventory_repository != request.repository:
        blockers.append(
            _blocker(
                "inventory_repository_mismatch",
                f"runner lane inventory repository does not match request: {inventory_repository}",
            )
        )
    existing_lanes = tuple(
        lane for lane in inventory.lanes if lane.name.strip().lower() == request.lane_name
    )
    if existing_lanes:
        blockers.append(
            _blocker(
                "lane_already_exists",
                f"runner lane already exists: {request.lane_name}",
            )
        )
    request_labels = set(request.labels)
    for label in policy.required_labels:
        if label not in request_labels:
            blockers.append(
                _blocker(
                    "label_missing",
                    f"runner lane registration is missing required label: {label}",
                )
            )

    status: RunnerLaneRegistrationPlanStatus = "blocked" if blockers else "ready"
    return RunnerLaneRegistrationPlan(
        operation="register",
        status=status,
        repository=request.repository,
        host_name=request.host_name,
        lane_name=request.lane_name,
        registration_root=request.registration_root,
        labels=request.labels,
        active_run_ids=request.active_run_ids,
        target_worker_active=request.target_worker_active,
        mutate=request.mutate,
        audit_record_key=request.audit_record_key,
        blockers=tuple(blockers),
        next_steps=_next_steps(status=status),
        summary=(
            "runner lane registration plan is ready"
            if status == "ready"
            else "runner lane registration plan is blocked"
        ),
    )


def plan_runner_lane_retirement(
    *,
    policy: RunnerLaneRegistrationPolicy,
    request: RunnerLaneRegistrationRequest,
    inventory: RunnerLaneInventory,
) -> RunnerLaneRegistrationPlan:
    if request.operation != "retire":
        raise ValueError("runner lane retirement planner requires retire operation")
    blockers: list[RunnerLaneRegistrationBlocker] = []
    inventory_repository = _normalized_repository(inventory.repository)
    if not policy.allowed_repositories or request.repository not in policy.allowed_repositories:
        blockers.append(
            _blocker(
                "repository_not_allowed",
                f"repository is not opted into runner lane retirement: {request.repository}",
            )
        )
    if not policy.approved_hosts or request.host_name.lower() not in policy.approved_hosts:
        blockers.append(
            _blocker(
                "host_not_allowed",
                f"runner host is not approved for lane retirement: {request.host_name}",
            )
        )
    if not _path_under_allowed_roots(
        path=request.registration_root,
        allowed_roots=policy.allowed_registration_roots,
    ):
        blockers.append(
            _blocker(
                "registration_root_not_allowed",
                "runner registration root is not under an allowed root: "
                f"{request.registration_root}",
            )
        )
    if policy.require_audit_record and not request.audit_record_key:
        blockers.append(
            _blocker(
                "audit_record_key_missing",
                "runner lane retirement requires an audit record key",
            )
        )
    if not request.mutate:
        blockers.append(
            _blocker(
                "mutate_not_requested",
                "runner lane retirement is dry-run only until mutate is explicitly requested",
            )
        )
    if inventory_repository != request.repository:
        blockers.append(
            _blocker(
                "inventory_repository_mismatch",
                f"runner lane inventory repository does not match request: {inventory_repository}",
            )
        )
    matching_lanes = tuple(
        lane for lane in inventory.lanes if lane.name.strip().lower() == request.lane_name
    )
    if not matching_lanes:
        blockers.append(
            _blocker("lane_missing", f"runner lane does not exist: {request.lane_name}")
        )
    elif len(matching_lanes) > 1:
        blockers.append(
            _blocker(
                "lane_ambiguous",
                f"runner lane inventory contains duplicate lane name: {request.lane_name}",
            )
        )
    else:
        lane = matching_lanes[0]
        if lane.busy:
            blockers.append(_blocker("lane_busy", f"runner lane is busy: {request.lane_name}"))
        observed_labels = set(_normalized_labels(lane.labels))
        for label in policy.required_labels:
            if label not in observed_labels:
                blockers.append(
                    _blocker(
                        "label_missing",
                        f"runner lane retirement requires managed label: {label}",
                    )
                )
    if request.active_run_ids:
        blockers.append(
            _blocker(
                "repository_runs_active",
                "runner lane retirement requires no active repository runs: "
                + ", ".join(str(run_id) for run_id in request.active_run_ids),
            )
        )
    if request.target_worker_active:
        blockers.append(
            _blocker(
                "target_worker_active",
                f"runner lane still has an active worker process: {request.lane_name}",
            )
        )

    status: RunnerLaneRegistrationPlanStatus = "blocked" if blockers else "ready"
    plan_labels = matching_lanes[0].labels if len(matching_lanes) == 1 else request.labels
    return RunnerLaneRegistrationPlan(
        operation="retire",
        status=status,
        repository=request.repository,
        host_name=request.host_name,
        lane_name=request.lane_name,
        registration_root=request.registration_root,
        labels=plan_labels,
        active_run_ids=request.active_run_ids,
        target_worker_active=request.target_worker_active,
        mutate=request.mutate,
        audit_record_key=request.audit_record_key,
        blockers=tuple(blockers),
        next_steps=_retirement_next_steps(status=status),
        summary=(
            "runner lane retirement plan is ready"
            if status == "ready"
            else "runner lane retirement plan is blocked"
        ),
    )


def _next_steps(*, status: RunnerLaneRegistrationPlanStatus) -> tuple[str, ...]:
    if status == "blocked":
        return ("resolve blockers before registering any GitHub runner lane",)
    return (
        "request a short-lived GitHub runner registration token",
        "configure the runner under the approved registration root",
        "verify GitHub sees the lane online",
    )


def _retirement_next_steps(*, status: RunnerLaneRegistrationPlanStatus) -> tuple[str, ...]:
    if status == "blocked":
        return ("resolve blockers before retiring any GitHub runner lane",)
    return (
        "stop and disable the exact supervised runner service",
        "delete the exact GitHub runner registration",
        "remove the verified retired runner directory",
    )


def _blocker(
    code: RunnerLaneRegistrationBlockerCode, message: str
) -> RunnerLaneRegistrationBlocker:
    return RunnerLaneRegistrationBlocker(code=code, message=message)


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
        raise ValueError("runner lane registration requires repository formatted as owner/name")
    owner, name = normalized_repository.split("/", 1)
    if not owner or not name:
        raise ValueError("runner lane registration requires repository formatted as owner/name")
    return normalized_repository


def _normalized_labels(values: tuple[str, ...]) -> tuple[str, ...]:
    return _normalized_tokens(values)


def _normalized_tokens(values: tuple[str, ...]) -> tuple[str, ...]:
    normalized_tokens = tuple(sorted({value.strip().lower() for value in values if value.strip()}))
    for token in normalized_tokens:
        _validate_slug(
            token,
            "runner lane registration labels must use letters, numbers, dots, underscores, or hyphens",
        )
    return normalized_tokens


def _normalized_path(value: str) -> str:
    normalized = value.strip()
    if ".." in normalized.split("/"):
        raise ValueError(
            "runner lane registration paths must not contain parent-directory components"
        )
    normalized = normalized.rstrip("/")
    if not normalized.startswith("/"):
        raise ValueError("runner lane registration paths must be absolute")
    segments: list[str] = []
    for segment in normalized.split("/"):
        if segment in {"", "."}:
            continue
        segments.append(segment)
    if not segments:
        raise ValueError("runner lane registration paths must be scoped absolute paths")
    return "/" + "/".join(segments)


def _validate_slug(value: str, message: str) -> None:
    if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", value):
        raise ValueError(message)


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value
