from __future__ import annotations

from collections.abc import Callable
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator


RunnerHostHygieneStatus = Literal["healthy", "attention"]
RunnerHostHygieneApplyAction = Literal[
    "prune_docker_cache",
    "remove_buildkit_state_volumes",
    "prune_runner_workdir",
    "restart_runner_service",
]
RunnerHostHygieneApplyPlanStatus = Literal["ready", "blocked"]
RunnerHostHygieneApplyAuditStatus = Literal["planned", "completed", "failed"]
RunnerHostHygieneAuditDeliveryState = Literal["pending", "delivered"]
RunnerHostHygieneAuditExecutionState = Literal[
    "planned",
    "blocked",
    "action_started",
    "terminal_recorded",
]
RunnerHostHygieneAdapterType = Literal[
    "github_actions_runner",
    "launchplane_worker",
    "remote_host_executor",
]
RunnerHostHygieneAdapterBoundaryStatus = Literal["ready", "blocked"]
RunnerHostHygieneApplyBlockerCode = Literal[
    "action_not_enabled",
    "approved_host_mismatch",
    "audit_record_required",
    "host_needs_attention",
    "mutate_not_requested",
    "report_host_mismatch",
    "target_volume_active",
    "target_volume_missing_from_report",
    "target_volume_multiple_requested",
    "target_volume_not_allowlisted",
    "target_volume_not_buildkit_state",
    "target_volume_not_requested",
    "retained_warm_builder_missing_from_report",
    "warm_builder_not_retained",
]
RunnerHostHygieneFindingCode = Literal[
    "docker_reclaimable_above_limit",
    "free_disk_below_minimum",
    "orphan_buildkit_present",
    "required_warm_builder_missing",
    "runner_workdir_above_limit",
]
RunnerHostHygienePrivilegedScope = Literal[
    "docker_cache",
    "docker_volume",
    "runner_service",
    "runner_workdir",
]
RunnerHostHygieneAdapterBoundaryBlockerCode = Literal[
    "adapter_type_not_allowed",
    "apply_plan_not_ready",
    "audit_record_key_mismatch",
    "audit_record_key_prefix_missing",
    "execution_lane_not_allowed",
    "host_not_approved",
    "host_plan_mismatch",
    "post_apply_evidence_missing",
    "pre_apply_evidence_missing",
    "privileged_scope_forbidden",
    "privileged_scope_missing",
    "privileged_scope_overbroad",
    "repository_scope_not_allowed",
    "repository_scope_required",
    "rollback_plan_required",
    "service_user_not_allowed",
]


class RunnerHostHygienePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    minimum_free_disk_bytes: int = Field(default=0, ge=0)
    maximum_docker_reclaimable_bytes: int | None = Field(default=None, ge=0)
    maximum_runner_workdir_bytes: int | None = Field(default=None, ge=0)
    required_warm_builders: tuple[str, ...] = ()
    allow_orphan_buildkit: bool = False

    @model_validator(mode="after")
    def _normalize_policy(self) -> "RunnerHostHygienePolicy":
        self.required_warm_builders = _normalized_tokens(self.required_warm_builders)
        return self


class RunnerHostHygieneDockerReclaimableBreakdown(BaseModel):
    model_config = ConfigDict(extra="forbid")

    images_bytes: int = Field(default=0, ge=0)
    containers_bytes: int = Field(default=0, ge=0)
    local_volumes_bytes: int = Field(default=0, ge=0)
    build_cache_bytes: int = Field(default=0, ge=0)

    @property
    def total_bytes(self) -> int:
        return (
            self.images_bytes
            + self.containers_bytes
            + self.local_volumes_bytes
            + self.build_cache_bytes
        )


class RunnerHostHygieneRunnerWorkdirUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    root_key: str
    apparent_bytes: int = Field(default=0, ge=0)
    allocated_bytes: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _normalize_usage(self) -> "RunnerHostHygieneRunnerWorkdirUsage":
        self.root_key = _required_text(
            _normalized_token(self.root_key),
            "runner host hygiene workdir usage requires root_key",
        )
        return self


class RunnerHostHygieneObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_name: str
    observed_at: str
    free_disk_bytes: int = Field(ge=0)
    docker_reclaimable_bytes: int = Field(default=0, ge=0)
    docker_reclaimable_breakdown: RunnerHostHygieneDockerReclaimableBreakdown = Field(
        default_factory=RunnerHostHygieneDockerReclaimableBreakdown
    )
    runner_workdir_bytes: int = Field(default=0, ge=0)
    runner_workdir_allocated_bytes: int = Field(default=0, ge=0)
    runner_workdir_usage: tuple[RunnerHostHygieneRunnerWorkdirUsage, ...] = ()
    docker_toolchain: "RunnerHostDockerToolchainObservation | None" = None
    warm_builders: tuple[str, ...] = ()
    image_inventory: tuple["RunnerHostHygieneImageInventoryItem", ...] = ()
    volume_inventory: tuple["RunnerHostHygieneVolumeInventoryItem", ...] = ()
    orphan_buildkit_containers: int = Field(default=0, ge=0)
    orphan_buildkit_volumes: int = Field(default=0, ge=0)
    notes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_observation(self) -> "RunnerHostHygieneObservation":
        self.host_name = _required_text(
            self.host_name, "runner host hygiene observation requires host_name"
        )
        self.observed_at = _required_text(
            self.observed_at, "runner host hygiene observation requires observed_at"
        )
        self.warm_builders = _normalized_tokens(self.warm_builders)
        self.runner_workdir_usage = tuple(
            sorted(self.runner_workdir_usage, key=lambda item: item.root_key)
        )
        self.image_inventory = _sorted_image_inventory(self.image_inventory)
        self.volume_inventory = _sorted_volume_inventory(self.volume_inventory)
        self.notes = tuple(note.strip() for note in self.notes if note.strip())
        return self


class RunnerHostDockerToolchainObservation(BaseModel):
    model_config = ConfigDict(extra="forbid")

    docker_engine_version: str = ""
    docker_cli_version: str = ""
    docker_buildx_version: str = ""
    docker_buildx_plugin_path: str = ""
    docker_buildx_package: str = ""
    docker_buildx_source: str = ""
    buildkit_version: str = ""

    @model_validator(mode="after")
    def _normalize_toolchain(self) -> "RunnerHostDockerToolchainObservation":
        self.docker_engine_version = self.docker_engine_version.strip().removeprefix("v")
        self.docker_cli_version = self.docker_cli_version.strip().removeprefix("v")
        self.docker_buildx_version = self.docker_buildx_version.strip().removeprefix("v")
        self.docker_buildx_plugin_path = self.docker_buildx_plugin_path.strip()
        self.docker_buildx_package = self.docker_buildx_package.strip()
        self.docker_buildx_source = self.docker_buildx_source.strip()
        self.buildkit_version = self.buildkit_version.strip().removeprefix("v")
        return self

    def has_evidence(self) -> bool:
        return any(
            (
                self.docker_engine_version,
                self.docker_cli_version,
                self.docker_buildx_version,
                self.docker_buildx_plugin_path,
                self.docker_buildx_package,
                self.docker_buildx_source,
                self.buildkit_version,
            )
        )


class RunnerHostHygieneImageInventoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    image_id: str
    repository: str
    tag: str
    size_bytes: int = Field(default=0, ge=0)
    created_at: str = ""
    dangling: bool = False
    in_use: bool = False
    is_warm_builder: bool = False

    @model_validator(mode="after")
    def _normalize_item(self) -> "RunnerHostHygieneImageInventoryItem":
        self.image_id = _required_text(
            self.image_id, "runner host hygiene image inventory item requires image_id"
        )
        self.repository = self.repository.strip().lower()
        self.tag = self.tag.strip().lower()
        self.created_at = self.created_at.strip()
        return self


class RunnerHostHygieneVolumeInventoryItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str
    driver: str
    mountpoint: str = ""
    labels: tuple[str, ...] = ()
    size_bytes: int = Field(default=0, ge=0)
    referenced_by_containers: int = Field(default=0, ge=0)
    dangling: bool = False

    @model_validator(mode="after")
    def _normalize_item(self) -> "RunnerHostHygieneVolumeInventoryItem":
        self.name = _required_text(
            self.name, "runner host hygiene volume inventory item requires name"
        )
        self.driver = _required_text(
            self.driver, "runner host hygiene volume inventory item requires driver"
        )
        self.mountpoint = self.mountpoint.strip()
        self.labels = tuple(sorted(label.strip() for label in self.labels if label.strip()))
        return self


class RunnerHostHygieneFinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: RunnerHostHygieneFindingCode
    message: str

    @model_validator(mode="after")
    def _normalize_finding(self) -> "RunnerHostHygieneFinding":
        self.message = _required_text(self.message, "runner host hygiene finding requires message")
        return self


class RunnerHostHygieneReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: RunnerHostHygieneStatus
    host_name: str
    free_disk_bytes: int = Field(default=0, ge=0)
    docker_reclaimable_bytes: int = Field(default=0, ge=0)
    docker_reclaimable_breakdown: RunnerHostHygieneDockerReclaimableBreakdown = Field(
        default_factory=RunnerHostHygieneDockerReclaimableBreakdown
    )
    runner_workdir_bytes: int = Field(default=0, ge=0)
    runner_workdir_allocated_bytes: int = Field(default=0, ge=0)
    runner_workdir_usage: tuple[RunnerHostHygieneRunnerWorkdirUsage, ...] = ()
    docker_toolchain: RunnerHostDockerToolchainObservation | None = None
    warm_builders: tuple[str, ...] = ()
    image_inventory: tuple[RunnerHostHygieneImageInventoryItem, ...] = ()
    volume_inventory: tuple[RunnerHostHygieneVolumeInventoryItem, ...] = ()
    orphan_buildkit_containers: int = Field(default=0, ge=0)
    orphan_buildkit_volumes: int = Field(default=0, ge=0)
    findings: tuple[RunnerHostHygieneFinding, ...] = ()
    summary: str
    next_steps: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_report(self) -> "RunnerHostHygieneReport":
        self.host_name = _required_text(
            self.host_name, "runner host hygiene report requires host_name"
        )
        self.warm_builders = _normalized_tokens(self.warm_builders)
        self.runner_workdir_usage = tuple(
            sorted(self.runner_workdir_usage, key=lambda item: item.root_key)
        )
        self.image_inventory = _sorted_image_inventory(self.image_inventory)
        self.volume_inventory = _sorted_volume_inventory(self.volume_inventory)
        self.findings = tuple(sorted(self.findings, key=lambda finding: finding.code))
        self.summary = _required_text(self.summary, "runner host hygiene report requires summary")
        self.next_steps = tuple(step.strip() for step in self.next_steps if step.strip())
        if self.status == "healthy" and self.findings:
            raise ValueError("healthy runner host hygiene report cannot include findings")
        if self.status == "attention" and not self.findings:
            raise ValueError("attention runner host hygiene report requires findings")
        return self


class RunnerHostHygieneApplyPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    approved_hosts: tuple[str, ...] = ()
    required_retained_warm_builders: tuple[str, ...] = ()
    require_healthy_report: bool = True
    require_audit_record: bool = True
    allow_docker_cache_prune: bool = False
    allow_buildkit_state_volume_remove: bool = False
    allowed_buildkit_state_volumes: tuple[str, ...] = ()
    allow_runner_workdir_prune: bool = False
    allow_runner_service_restart: bool = False

    @model_validator(mode="after")
    def _normalize_policy(self) -> "RunnerHostHygieneApplyPolicy":
        self.approved_hosts = _normalized_host_names(self.approved_hosts)
        self.required_retained_warm_builders = _normalized_tokens(
            self.required_retained_warm_builders
        )
        self.allowed_buildkit_state_volumes = _normalized_volume_names(
            self.allowed_buildkit_state_volumes
        )
        return self


class RunnerHostHygieneApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    action: RunnerHostHygieneApplyAction
    host_name: str
    mutate: bool = False
    retained_warm_builders: tuple[str, ...] = ()
    target_buildkit_state_volumes: tuple[str, ...] = ()
    audit_record_key: str = ""

    @model_validator(mode="after")
    def _normalize_request(self) -> "RunnerHostHygieneApplyRequest":
        self.host_name = _normalized_host_name(self.host_name)
        if not self.host_name:
            raise ValueError("runner host hygiene apply requires host_name")
        self.retained_warm_builders = _normalized_tokens(self.retained_warm_builders)
        self.target_buildkit_state_volumes = _normalized_volume_name_entries(
            self.target_buildkit_state_volumes
        )
        self.audit_record_key = self.audit_record_key.strip()
        return self


class RunnerHostHygieneApplyBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: RunnerHostHygieneApplyBlockerCode
    message: str

    @model_validator(mode="after")
    def _normalize_blocker(self) -> "RunnerHostHygieneApplyBlocker":
        self.message = _required_text(
            self.message, "runner host hygiene apply blocker requires message"
        )
        return self


class RunnerHostHygieneApplyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: RunnerHostHygieneApplyPlanStatus
    action: RunnerHostHygieneApplyAction
    host_name: str
    mutate: bool
    audit_record_key: str = ""
    blockers: tuple[RunnerHostHygieneApplyBlocker, ...] = ()
    next_steps: tuple[str, ...] = ()
    summary: str

    @model_validator(mode="after")
    def _normalize_plan(self) -> "RunnerHostHygieneApplyPlan":
        self.host_name = _required_text(
            self.host_name, "runner host hygiene apply plan requires host_name"
        )
        self.audit_record_key = self.audit_record_key.strip()
        self.blockers = tuple(sorted(self.blockers, key=lambda blocker: blocker.code))
        self.next_steps = tuple(step.strip() for step in self.next_steps if step.strip())
        self.summary = _required_text(
            self.summary, "runner host hygiene apply plan requires summary"
        )
        if self.status == "ready" and self.blockers:
            raise ValueError("ready runner host hygiene apply plan cannot include blockers")
        if self.status == "blocked" and not self.blockers:
            raise ValueError("blocked runner host hygiene apply plan requires blockers")
        return self


class RunnerHostHygieneApplyAuditRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    audit_record_key: str
    status: RunnerHostHygieneApplyAuditStatus
    request: RunnerHostHygieneApplyRequest
    plan: RunnerHostHygieneApplyPlan
    pre_apply_report: RunnerHostHygieneReport
    post_apply_report: RunnerHostHygieneReport | None = None
    message: str = ""

    @model_validator(mode="after")
    def _normalize_record(self) -> "RunnerHostHygieneApplyAuditRecord":
        self.audit_record_key = _required_text(
            self.audit_record_key, "runner host hygiene apply audit record requires key"
        )
        self.message = self.message.strip()
        if self.audit_record_key != self.request.audit_record_key:
            raise ValueError("runner host hygiene audit record key must match request")
        if self.plan.audit_record_key != self.request.audit_record_key:
            raise ValueError("runner host hygiene audit record plan key must match request")
        if _normalized_host_name(self.request.host_name) != _normalized_host_name(
            self.plan.host_name
        ):
            raise ValueError("runner host hygiene audit record plan must match request host")
        if self.plan.action != self.request.action:
            raise ValueError("runner host hygiene audit record plan must match request action")
        if self.plan.mutate != self.request.mutate:
            raise ValueError(
                "runner host hygiene audit record plan must match request mutate intent"
            )
        if _normalized_host_name(self.request.host_name) != _normalized_host_name(
            self.pre_apply_report.host_name
        ):
            raise ValueError(
                "runner host hygiene audit record pre-apply report must match request host"
            )
        if self.post_apply_report is not None and _normalized_host_name(
            self.request.host_name
        ) != _normalized_host_name(self.post_apply_report.host_name):
            raise ValueError(
                "runner host hygiene audit record post-apply report must match request host"
            )
        if self.status == "planned" and self.post_apply_report is not None:
            raise ValueError("planned runner host hygiene audit record cannot include post report")
        if self.status == "completed" and self.post_apply_report is None:
            raise ValueError(
                "completed runner host hygiene audit record requires post-apply report"
            )
        if self.status == "failed" and self.post_apply_report is None and not self.message:
            raise ValueError(
                "failed runner host hygiene audit record without post report requires a message"
            )
        if self.status in {"completed", "failed"} and self.plan.status != "ready":
            raise ValueError("terminal runner host hygiene audit record requires a ready plan")
        return self


class RunnerHostHygieneAuditDeliveryEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    host_name: str
    action: RunnerHostHygieneApplyAction
    audit_record_key: str
    execution_state: RunnerHostHygieneAuditExecutionState = "planned"
    planned_audit: RunnerHostHygieneApplyAuditRecord
    planned_idempotency_key: str
    planned_delivery_state: RunnerHostHygieneAuditDeliveryState = "pending"
    terminal_audit: RunnerHostHygieneApplyAuditRecord | None = None
    terminal_idempotency_key: str = ""
    terminal_delivery_state: RunnerHostHygieneAuditDeliveryState | None = None
    created_at: str
    updated_at: str
    delivery_attempts: int = Field(default=0, ge=0)
    last_error: str = ""
    last_error_retryable: bool | None = None

    @model_validator(mode="after")
    def _normalize_envelope(self) -> "RunnerHostHygieneAuditDeliveryEnvelope":
        self.host_name = _required_text(
            _normalized_host_name(self.host_name),
            "runner host hygiene audit delivery envelope requires host_name",
        )
        self.audit_record_key = _required_text(
            self.audit_record_key,
            "runner host hygiene audit delivery envelope requires audit_record_key",
        )
        self.planned_idempotency_key = _required_text(
            self.planned_idempotency_key,
            "runner host hygiene audit delivery envelope requires planned idempotency key",
        )
        self.created_at = _required_text(
            self.created_at,
            "runner host hygiene audit delivery envelope requires created_at",
        )
        self.updated_at = _required_text(
            self.updated_at,
            "runner host hygiene audit delivery envelope requires updated_at",
        )
        self.last_error = self.last_error.strip()[:500]
        if self.audit_record_key != self.planned_audit.audit_record_key:
            raise ValueError("runner host hygiene audit delivery key must match planned audit")
        if self.host_name != _normalized_host_name(self.planned_audit.request.host_name):
            raise ValueError("runner host hygiene audit delivery host must match planned audit")
        if self.action != self.planned_audit.request.action:
            raise ValueError("runner host hygiene audit delivery action must match planned audit")
        if self.planned_audit.status != "planned":
            raise ValueError("runner host hygiene audit delivery planned audit must be planned")
        if self.terminal_audit is None:
            if self.terminal_idempotency_key or self.terminal_delivery_state is not None:
                raise ValueError(
                    "runner host hygiene audit delivery terminal metadata requires terminal audit"
                )
            if self.execution_state == "terminal_recorded":
                raise ValueError(
                    "runner host hygiene terminal-recorded delivery requires terminal audit"
                )
        else:
            if self.terminal_audit.status not in {"completed", "failed"}:
                raise ValueError("runner host hygiene terminal audit must be completed or failed")
            if self.terminal_audit.audit_record_key != self.audit_record_key:
                raise ValueError("runner host hygiene terminal audit key must match envelope")
            self.terminal_idempotency_key = _required_text(
                self.terminal_idempotency_key,
                "runner host hygiene terminal audit requires idempotency key",
            )
            if self.terminal_delivery_state is None:
                raise ValueError("runner host hygiene terminal audit requires delivery state")
            if self.execution_state != "terminal_recorded":
                raise ValueError(
                    "runner host hygiene terminal audit requires terminal-recorded execution state"
                )
        return self


class RunnerHostHygieneAdapterPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    approved_hosts: tuple[str, ...] = ()
    allowed_adapter_types: tuple[RunnerHostHygieneAdapterType, ...] = ()
    allowed_execution_lanes: tuple[str, ...] = ()
    allowed_service_users: tuple[str, ...] = ()
    allowed_repository_scopes: tuple[str, ...] = ()
    allowed_privileged_scopes: tuple[RunnerHostHygienePrivilegedScope, ...] = ()
    required_pre_apply_evidence: tuple[str, ...] = ()
    required_post_apply_evidence: tuple[str, ...] = ()
    audit_record_key_prefix: str = "runner-host-hygiene/"
    require_rollback_plan: bool = True

    @model_validator(mode="after")
    def _normalize_policy(self) -> "RunnerHostHygieneAdapterPolicy":
        self.approved_hosts = _normalized_host_names(self.approved_hosts)
        self.allowed_execution_lanes = _normalized_tokens(self.allowed_execution_lanes)
        self.allowed_service_users = _normalized_tokens(self.allowed_service_users)
        self.allowed_repository_scopes = _normalized_repositories(self.allowed_repository_scopes)
        self.allowed_privileged_scopes = cast(
            tuple[RunnerHostHygienePrivilegedScope, ...],
            _normalized_values(self.allowed_privileged_scopes, _normalized_token),
        )
        self.required_pre_apply_evidence = _normalized_tokens(self.required_pre_apply_evidence)
        self.required_post_apply_evidence = _normalized_tokens(self.required_post_apply_evidence)
        self.audit_record_key_prefix = _required_text(
            self.audit_record_key_prefix,
            "runner host hygiene adapter policy requires audit record key prefix",
        )
        return self


class RunnerHostHygieneAdapterProposal(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    adapter_type: RunnerHostHygieneAdapterType
    host_name: str
    execution_lane: str
    service_user: str
    repository_scopes: tuple[str, ...] = ()
    privileged_scopes: tuple[RunnerHostHygienePrivilegedScope, ...] = ()
    audit_record_key: str
    rollback_plan: str = ""
    pre_apply_evidence: tuple[str, ...] = ()
    post_apply_evidence: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _normalize_proposal(self) -> "RunnerHostHygieneAdapterProposal":
        self.host_name = _normalized_host_name(self.host_name)
        if not self.host_name:
            raise ValueError("runner host hygiene adapter proposal requires host_name")
        self.execution_lane = _normalized_token(self.execution_lane)
        if not self.execution_lane:
            raise ValueError("runner host hygiene adapter proposal requires execution_lane")
        self.service_user = _normalized_token(self.service_user)
        if not self.service_user:
            raise ValueError("runner host hygiene adapter proposal requires service_user")
        self.repository_scopes = _normalized_repositories(self.repository_scopes)
        self.privileged_scopes = cast(
            tuple[RunnerHostHygienePrivilegedScope, ...],
            _normalized_values(self.privileged_scopes, _normalized_token),
        )
        self.audit_record_key = _required_text(
            self.audit_record_key, "runner host hygiene adapter proposal requires audit_record_key"
        )
        self.rollback_plan = self.rollback_plan.strip()
        self.pre_apply_evidence = _normalized_tokens(self.pre_apply_evidence)
        self.post_apply_evidence = _normalized_tokens(self.post_apply_evidence)
        return self


class RunnerHostHygieneAdapterBoundaryBlocker(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: RunnerHostHygieneAdapterBoundaryBlockerCode
    message: str

    @model_validator(mode="after")
    def _normalize_blocker(self) -> "RunnerHostHygieneAdapterBoundaryBlocker":
        self.message = _required_text(
            self.message, "runner host hygiene adapter boundary blocker requires message"
        )
        return self


class RunnerHostHygieneAdapterBoundaryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    status: RunnerHostHygieneAdapterBoundaryStatus
    adapter_type: RunnerHostHygieneAdapterType
    host_name: str
    execution_lane: str
    service_user: str
    audit_record_key: str
    blockers: tuple[RunnerHostHygieneAdapterBoundaryBlocker, ...] = ()
    next_steps: tuple[str, ...] = ()
    summary: str

    @model_validator(mode="after")
    def _normalize_plan(self) -> "RunnerHostHygieneAdapterBoundaryPlan":
        self.host_name = _required_text(
            self.host_name, "runner host hygiene adapter boundary plan requires host_name"
        )
        self.execution_lane = _required_text(
            self.execution_lane,
            "runner host hygiene adapter boundary plan requires execution_lane",
        )
        self.service_user = _required_text(
            self.service_user,
            "runner host hygiene adapter boundary plan requires service_user",
        )
        self.audit_record_key = _required_text(
            self.audit_record_key,
            "runner host hygiene adapter boundary plan requires audit_record_key",
        )
        self.blockers = tuple(sorted(self.blockers, key=lambda blocker: blocker.code))
        self.next_steps = tuple(step.strip() for step in self.next_steps if step.strip())
        self.summary = _required_text(
            self.summary, "runner host hygiene adapter boundary plan requires summary"
        )
        if self.status == "ready" and self.blockers:
            raise ValueError("ready runner host hygiene adapter boundary cannot include blockers")
        if self.status == "blocked" and not self.blockers:
            raise ValueError("blocked runner host hygiene adapter boundary requires blockers")
        return self


def evaluate_runner_host_hygiene(
    *,
    policy: RunnerHostHygienePolicy,
    observation: RunnerHostHygieneObservation,
) -> RunnerHostHygieneReport:
    findings: list[RunnerHostHygieneFinding] = []
    if observation.free_disk_bytes < policy.minimum_free_disk_bytes:
        findings.append(
            _finding(
                "free_disk_below_minimum",
                (
                    "runner host free disk is below the configured minimum: "
                    f"{observation.free_disk_bytes} < {policy.minimum_free_disk_bytes}"
                ),
            )
        )
    if (
        policy.maximum_docker_reclaimable_bytes is not None
        and observation.docker_reclaimable_bytes > policy.maximum_docker_reclaimable_bytes
    ):
        findings.append(
            _finding(
                "docker_reclaimable_above_limit",
                (
                    "runner host Docker reclaimable bytes exceed the configured limit: "
                    f"{observation.docker_reclaimable_bytes} > "
                    f"{policy.maximum_docker_reclaimable_bytes}"
                ),
            )
        )
    if (
        policy.maximum_runner_workdir_bytes is not None
        and observation.runner_workdir_bytes > policy.maximum_runner_workdir_bytes
    ):
        findings.append(
            _finding(
                "runner_workdir_above_limit",
                (
                    "runner host work directory bytes exceed the configured limit: "
                    f"{observation.runner_workdir_bytes} > "
                    f"{policy.maximum_runner_workdir_bytes}"
                ),
            )
        )
    missing_builders = tuple(
        builder
        for builder in policy.required_warm_builders
        if builder not in observation.warm_builders
    )
    if missing_builders:
        findings.append(
            _finding(
                "required_warm_builder_missing",
                "runner host is missing required warm builders: " + ", ".join(missing_builders),
            )
        )
    if not policy.allow_orphan_buildkit and (
        observation.orphan_buildkit_containers > 0 or observation.orphan_buildkit_volumes > 0
    ):
        findings.append(
            _finding(
                "orphan_buildkit_present",
                (
                    "runner host has orphan BuildKit artifacts: "
                    f"{observation.orphan_buildkit_containers} containers, "
                    f"{observation.orphan_buildkit_volumes} volumes"
                ),
            )
        )

    status: RunnerHostHygieneStatus = "attention" if findings else "healthy"
    return RunnerHostHygieneReport(
        status=status,
        host_name=observation.host_name,
        free_disk_bytes=observation.free_disk_bytes,
        docker_reclaimable_bytes=observation.docker_reclaimable_bytes,
        docker_reclaimable_breakdown=observation.docker_reclaimable_breakdown,
        runner_workdir_bytes=observation.runner_workdir_bytes,
        runner_workdir_allocated_bytes=observation.runner_workdir_allocated_bytes,
        runner_workdir_usage=observation.runner_workdir_usage,
        docker_toolchain=observation.docker_toolchain,
        warm_builders=observation.warm_builders,
        image_inventory=observation.image_inventory,
        volume_inventory=observation.volume_inventory,
        orphan_buildkit_containers=observation.orphan_buildkit_containers,
        orphan_buildkit_volumes=observation.orphan_buildkit_volumes,
        findings=tuple(findings),
        summary=(
            "runner host hygiene satisfies report-only policy"
            if status == "healthy"
            else "runner host hygiene needs operator attention"
        ),
        next_steps=_next_steps(status),
    )


def plan_runner_host_hygiene_apply(
    *,
    policy: RunnerHostHygieneApplyPolicy,
    request: RunnerHostHygieneApplyRequest,
    report: RunnerHostHygieneReport,
) -> RunnerHostHygieneApplyPlan:
    blockers: list[RunnerHostHygieneApplyBlocker] = []
    if not policy.approved_hosts or request.host_name not in policy.approved_hosts:
        blockers.append(
            _apply_blocker(
                "approved_host_mismatch",
                f"runner host is not approved for hygiene apply: {request.host_name}",
            )
        )
    if not _apply_action_allowed(policy=policy, action=request.action):
        blockers.append(
            _apply_blocker(
                "action_not_enabled",
                f"runner host hygiene apply action is not enabled: {request.action}",
            )
        )
    if not request.mutate:
        blockers.append(
            _apply_blocker(
                "mutate_not_requested",
                "runner host hygiene apply is dry-run only until mutate is explicitly requested",
            )
        )
    if policy.require_audit_record and not request.audit_record_key:
        blockers.append(
            _apply_blocker(
                "audit_record_required",
                "runner host hygiene apply requires an audit record key",
            )
        )
    if policy.require_healthy_report and report.status != "healthy":
        blockers.append(
            _apply_blocker(
                "host_needs_attention",
                f"runner host hygiene report is not healthy: {report.summary}",
            )
        )
    if _normalized_host_name(report.host_name) != request.host_name:
        blockers.append(
            _apply_blocker(
                "report_host_mismatch",
                (
                    "runner host hygiene report host does not match requested host: "
                    f"{report.host_name} != {request.host_name}"
                ),
            )
        )
    missing_retained_builders = tuple(
        builder
        for builder in policy.required_retained_warm_builders
        if builder not in request.retained_warm_builders
    )
    if missing_retained_builders:
        blockers.append(
            _apply_blocker(
                "warm_builder_not_retained",
                (
                    "runner host hygiene apply request does not retain required warm builders: "
                    + ", ".join(missing_retained_builders)
                ),
            )
        )
    missing_observed_retained_builders = tuple(
        builder for builder in request.retained_warm_builders if builder not in report.warm_builders
    )
    if missing_observed_retained_builders:
        blockers.append(
            _apply_blocker(
                "retained_warm_builder_missing_from_report",
                (
                    "runner host hygiene report did not observe requested retained warm builders: "
                    + ", ".join(missing_observed_retained_builders)
                ),
            )
        )
    blockers.extend(_target_volume_blockers(policy=policy, request=request, report=report))

    status: RunnerHostHygieneApplyPlanStatus = "blocked" if blockers else "ready"
    return RunnerHostHygieneApplyPlan(
        status=status,
        action=request.action,
        host_name=request.host_name,
        mutate=request.mutate,
        audit_record_key=request.audit_record_key,
        blockers=tuple(blockers),
        next_steps=_apply_next_steps(action=request.action, status=status),
        summary=(
            "runner host hygiene apply plan is ready"
            if status == "ready"
            else "runner host hygiene apply plan is blocked"
        ),
    )


def plan_runner_host_hygiene_adapter_boundary(
    *,
    policy: RunnerHostHygieneAdapterPolicy,
    proposal: RunnerHostHygieneAdapterProposal,
    apply_plan: RunnerHostHygieneApplyPlan,
) -> RunnerHostHygieneAdapterBoundaryPlan:
    blockers: list[RunnerHostHygieneAdapterBoundaryBlocker] = []
    required_scope = _required_privileged_scope(apply_plan.action)
    if apply_plan.status != "ready":
        blockers.append(
            _adapter_blocker(
                "apply_plan_not_ready",
                f"runner host adapter boundary requires a ready apply plan: {apply_plan.summary}",
            )
        )
    if not policy.approved_hosts or proposal.host_name not in policy.approved_hosts:
        blockers.append(
            _adapter_blocker(
                "host_not_approved",
                f"runner host is not approved for adapter execution: {proposal.host_name}",
            )
        )
    if proposal.host_name != _normalized_host_name(apply_plan.host_name):
        blockers.append(
            _adapter_blocker(
                "host_plan_mismatch",
                (
                    "runner host adapter proposal does not match apply plan host: "
                    f"{proposal.host_name} != {apply_plan.host_name}"
                ),
            )
        )
    if proposal.audit_record_key != apply_plan.audit_record_key:
        blockers.append(
            _adapter_blocker(
                "audit_record_key_mismatch",
                "runner host adapter proposal audit key must match the apply plan",
            )
        )
    if not proposal.audit_record_key.startswith(policy.audit_record_key_prefix):
        blockers.append(
            _adapter_blocker(
                "audit_record_key_prefix_missing",
                (
                    "runner host adapter audit key must use prefix: "
                    f"{policy.audit_record_key_prefix}"
                ),
            )
        )
    if (
        not policy.allowed_adapter_types
        or proposal.adapter_type not in policy.allowed_adapter_types
    ):
        blockers.append(
            _adapter_blocker(
                "adapter_type_not_allowed",
                f"runner host adapter type is not allowed: {proposal.adapter_type}",
            )
        )
    if (
        not policy.allowed_execution_lanes
        or proposal.execution_lane not in policy.allowed_execution_lanes
    ):
        blockers.append(
            _adapter_blocker(
                "execution_lane_not_allowed",
                f"runner host execution lane is not allowed: {proposal.execution_lane}",
            )
        )
    if (
        not policy.allowed_service_users
        or proposal.service_user not in policy.allowed_service_users
    ):
        blockers.append(
            _adapter_blocker(
                "service_user_not_allowed",
                f"runner host service user is not allowed: {proposal.service_user}",
            )
        )
    if not proposal.repository_scopes:
        blockers.append(
            _adapter_blocker(
                "repository_scope_required",
                "runner host adapter proposal requires explicit repository scope",
            )
        )
    forbidden_repositories = tuple(
        repository
        for repository in proposal.repository_scopes
        if repository not in policy.allowed_repository_scopes
    )
    if forbidden_repositories:
        blockers.append(
            _adapter_blocker(
                "repository_scope_not_allowed",
                "runner host adapter repository scope is not allowed: "
                + ", ".join(forbidden_repositories),
            )
        )
    if required_scope not in proposal.privileged_scopes:
        blockers.append(
            _adapter_blocker(
                "privileged_scope_missing",
                f"runner host adapter proposal is missing required scope: {required_scope}",
            )
        )
    forbidden_scopes = tuple(
        scope
        for scope in proposal.privileged_scopes
        if scope not in policy.allowed_privileged_scopes
    )
    if forbidden_scopes:
        blockers.append(
            _adapter_blocker(
                "privileged_scope_forbidden",
                "runner host adapter privileged scope is not allowed: "
                + ", ".join(forbidden_scopes),
            )
        )
    extra_scopes = tuple(scope for scope in proposal.privileged_scopes if scope != required_scope)
    if extra_scopes:
        blockers.append(
            _adapter_blocker(
                "privileged_scope_overbroad",
                "runner host adapter privileged scope is broader than the apply action: "
                + ", ".join(extra_scopes),
            )
        )
    missing_pre_apply_evidence = tuple(
        evidence
        for evidence in policy.required_pre_apply_evidence
        if evidence not in proposal.pre_apply_evidence
    )
    if missing_pre_apply_evidence:
        blockers.append(
            _adapter_blocker(
                "pre_apply_evidence_missing",
                "runner host adapter proposal is missing pre-apply evidence: "
                + ", ".join(missing_pre_apply_evidence),
            )
        )
    missing_post_apply_evidence = tuple(
        evidence
        for evidence in policy.required_post_apply_evidence
        if evidence not in proposal.post_apply_evidence
    )
    if missing_post_apply_evidence:
        blockers.append(
            _adapter_blocker(
                "post_apply_evidence_missing",
                "runner host adapter proposal is missing post-apply evidence: "
                + ", ".join(missing_post_apply_evidence),
            )
        )
    if policy.require_rollback_plan and not proposal.rollback_plan:
        blockers.append(
            _adapter_blocker(
                "rollback_plan_required",
                "runner host adapter proposal requires a rollback or stop condition",
            )
        )

    status: RunnerHostHygieneAdapterBoundaryStatus = "blocked" if blockers else "ready"
    return RunnerHostHygieneAdapterBoundaryPlan(
        status=status,
        adapter_type=proposal.adapter_type,
        host_name=proposal.host_name,
        execution_lane=proposal.execution_lane,
        service_user=proposal.service_user,
        audit_record_key=proposal.audit_record_key,
        blockers=tuple(blockers),
        next_steps=_adapter_next_steps(status=status),
        summary=(
            "runner host hygiene adapter boundary is ready"
            if status == "ready"
            else "runner host hygiene adapter boundary is blocked"
        ),
    )


def _apply_action_allowed(
    *, policy: RunnerHostHygieneApplyPolicy, action: RunnerHostHygieneApplyAction
) -> bool:
    if action == "prune_docker_cache":
        return policy.allow_docker_cache_prune
    if action == "remove_buildkit_state_volumes":
        return policy.allow_buildkit_state_volume_remove
    if action == "prune_runner_workdir":
        return policy.allow_runner_workdir_prune
    return policy.allow_runner_service_restart


def _target_volume_blockers(
    *,
    policy: RunnerHostHygieneApplyPolicy,
    request: RunnerHostHygieneApplyRequest,
    report: RunnerHostHygieneReport,
) -> tuple[RunnerHostHygieneApplyBlocker, ...]:
    if request.action != "remove_buildkit_state_volumes":
        return ()
    blockers: list[RunnerHostHygieneApplyBlocker] = []
    if not request.target_buildkit_state_volumes:
        blockers.append(
            _apply_blocker(
                "target_volume_not_requested",
                "runner host hygiene volume cleanup requires at least one target volume",
            )
        )
    if len(request.target_buildkit_state_volumes) > 1:
        blockers.append(
            _apply_blocker(
                "target_volume_multiple_requested",
                "runner host hygiene volume cleanup accepts one target volume per mutation run",
            )
        )
    volume_by_name = {volume.name: volume for volume in report.volume_inventory}
    for volume_name in request.target_buildkit_state_volumes:
        if volume_name not in policy.allowed_buildkit_state_volumes:
            blockers.append(
                _apply_blocker(
                    "target_volume_not_allowlisted",
                    f"runner host hygiene target volume is not allowlisted: {volume_name}",
                )
            )
        if not _is_buildkit_state_volume(volume_name):
            blockers.append(
                _apply_blocker(
                    "target_volume_not_buildkit_state",
                    f"runner host hygiene target volume is not a BuildKit state volume: {volume_name}",
                )
            )
        volume = volume_by_name.get(volume_name)
        if volume is None:
            blockers.append(
                _apply_blocker(
                    "target_volume_missing_from_report",
                    f"runner host hygiene target volume is missing from report: {volume_name}",
                )
            )
            continue
        if not volume.dangling or volume.referenced_by_containers > 0:
            blockers.append(
                _apply_blocker(
                    "target_volume_active",
                    (
                        "runner host hygiene target volume is still referenced: "
                        f"{volume_name} links={volume.referenced_by_containers}"
                    ),
                )
            )
    return tuple(blockers)


def _apply_next_steps(
    *, action: RunnerHostHygieneApplyAction, status: RunnerHostHygieneApplyPlanStatus
) -> tuple[str, ...]:
    if status == "blocked":
        return ("resolve blockers before invoking any host adapter",)
    if action == "restart_runner_service":
        return (
            "capture pre-apply host hygiene evidence",
            "restart only the approved runner service through the host adapter",
            "capture post-apply host hygiene evidence and write the audit record",
        )
    if action == "prune_runner_workdir":
        return (
            "capture pre-apply host hygiene evidence",
            "prune only approved runner work directories through the host adapter",
            "capture post-apply host hygiene evidence and write the audit record",
        )
    if action == "remove_buildkit_state_volumes":
        return (
            "capture pre-apply host hygiene evidence",
            "remove only explicitly allowlisted zero-link BuildKit state volumes",
            "capture post-apply host hygiene evidence and write the audit record",
        )
    return (
        "capture pre-apply host hygiene evidence",
        "prune Docker cache without removing retained warm builders",
        "capture post-apply host hygiene evidence and write the audit record",
    )


def _next_steps(status: RunnerHostHygieneStatus) -> tuple[str, ...]:
    if status == "healthy":
        return ("keep collecting read-only host hygiene evidence before enabling apply",)
    return (
        "review findings before scheduling any host cleanup",
        "use a Launchplane-owned apply workflow before mutating runner hosts",
    )


def _finding(code: RunnerHostHygieneFindingCode, message: str) -> RunnerHostHygieneFinding:
    return RunnerHostHygieneFinding(code=code, message=message)


def _apply_blocker(
    code: RunnerHostHygieneApplyBlockerCode, message: str
) -> RunnerHostHygieneApplyBlocker:
    return RunnerHostHygieneApplyBlocker(code=code, message=message)


def _adapter_blocker(
    code: RunnerHostHygieneAdapterBoundaryBlockerCode, message: str
) -> RunnerHostHygieneAdapterBoundaryBlocker:
    return RunnerHostHygieneAdapterBoundaryBlocker(code=code, message=message)


def _required_privileged_scope(
    action: RunnerHostHygieneApplyAction,
) -> RunnerHostHygienePrivilegedScope:
    if action == "prune_docker_cache":
        return "docker_cache"
    if action == "remove_buildkit_state_volumes":
        return "docker_volume"
    if action == "prune_runner_workdir":
        return "runner_workdir"
    return "runner_service"


def _adapter_next_steps(*, status: RunnerHostHygieneAdapterBoundaryStatus) -> tuple[str, ...]:
    if status == "blocked":
        return ("resolve adapter boundary blockers before wiring a host mutation path",)
    return (
        "review the adapter proposal with operators before implementation",
        "implement the narrow host adapter behind the approved execution lane",
        "write planned, completed, or failed audit records to Launchplane-owned storage",
    )


def _normalized_host_names(values: tuple[str, ...]) -> tuple[str, ...]:
    return _normalized_values(values, _normalized_host_name)


def _normalized_host_name(value: str) -> str:
    return value.strip().lower()


def _normalized_tokens(values: tuple[str, ...]) -> tuple[str, ...]:
    return _normalized_values(values, _normalized_token)


def _normalized_volume_names(values: tuple[str, ...]) -> tuple[str, ...]:
    return _normalized_values(values, _normalized_volume_name)


def _normalized_volume_name_entries(values: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(normalized for value in values if (normalized := _normalized_volume_name(value)))


def _normalized_repositories(values: tuple[str, ...]) -> tuple[str, ...]:
    return _normalized_values(values, _normalized_repository)


def _sorted_image_inventory(
    values: tuple[RunnerHostHygieneImageInventoryItem, ...],
) -> tuple[RunnerHostHygieneImageInventoryItem, ...]:
    return tuple(sorted(values, key=lambda item: (item.repository, item.tag, item.image_id)))


def _sorted_volume_inventory(
    values: tuple[RunnerHostHygieneVolumeInventoryItem, ...],
) -> tuple[RunnerHostHygieneVolumeInventoryItem, ...]:
    return tuple(sorted(values, key=lambda item: (item.name, item.driver)))


def _normalized_values(values: tuple[str, ...], normalize: Callable[[str], str]) -> tuple[str, ...]:
    return tuple(sorted({normalized for value in values if (normalized := normalize(value))}))


def _normalized_token(value: str) -> str:
    return value.strip().lower()


def _normalized_volume_name(value: str) -> str:
    return value.strip()


def _is_buildkit_state_volume(value: str) -> bool:
    return value.startswith("buildx_buildkit_") and value.endswith("_state")


def _normalized_repository(value: str) -> str:
    normalized_value = value.strip().lower()
    if not normalized_value:
        return ""
    normalized_repository = "/".join(part.strip() for part in normalized_value.split("/"))
    if normalized_repository.count("/") != 1:
        raise ValueError("runner host hygiene adapter requires repository formatted as owner/name")
    owner, name = normalized_repository.split("/", 1)
    if not owner or not name:
        raise ValueError("runner host hygiene adapter requires repository formatted as owner/name")
    return normalized_repository


def _required_text(value: str, message: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(message)
    return normalized_value
