from __future__ import annotations

import re
from collections.abc import Callable
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, model_validator


RunnerHostHygieneStatus = Literal["healthy", "attention"]
RunnerHostHygieneEvidenceAvailability = Literal[
    "available",
    "partial",
    "unavailable",
    "stale",
]
RunnerHostHygieneMeasurementStatus = Literal[
    "complete",
    "partial",
    "unavailable",
    "stale",
]
RunnerHostHygieneCacheClass = Literal[
    "runner_workdir",
    "generated_filesystem",
    "docker_images",
    "docker_containers",
    "docker_volumes",
    "docker_build_cache",
]
RunnerHostHygieneCacheScope = Literal["host", "runner_root", "generated_cache"]
RunnerHostHygieneMeasurementBasis = Literal[
    "filesystem_apparent_allocated",
    "docker_engine",
]
RUNNER_HOST_HYGIENE_MAX_DOCKER_INVENTORY_ITEMS = 128
RunnerHostHygieneApplyAction = Literal[
    "prune_docker_cache",
    "prune_dangling_images",
    "prune_generated_run_cache",
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
    "target_builder_budget_missing",
    "target_builder_not_allowlisted",
    "target_generated_cache_below_high_water",
    "target_generated_cache_budget_invalid",
    "target_generated_cache_cooldown_active",
    "target_generated_cache_key_missing",
    "target_generated_cache_missing_from_report",
    "target_generated_cache_not_allowlisted",
    "target_generated_cache_owner_busy",
    "target_generated_cache_run_active",
    "target_generated_cache_run_missing",
    "target_generated_cache_run_not_idle",
    "target_generated_cache_run_too_recent",
    "target_generated_cache_runs_missing",
    "target_generated_cache_safety_failed",
    "retained_warm_builder_missing_from_report",
    "warm_builder_not_retained",
]
RunnerHostHygieneFindingCode = Literal[
    "docker_reclaimable_above_limit",
    "free_disk_below_minimum",
    "generated_cache_above_limit",
    "generated_cache_measurement_partial",
    "generated_cache_safety_failed",
    "orphan_buildkit_present",
    "required_warm_builder_missing",
    "runner_workdir_above_limit",
    "runner_workdir_measurement_partial",
]
RunnerHostHygienePrivilegedScope = Literal[
    "docker_cache",
    "docker_volume",
    "generated_cache",
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


class RunnerHostHygieneGeneratedCacheBudget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_key: str
    high_water_bytes: int = Field(ge=1)

    @model_validator(mode="after")
    def _normalize_budget(self) -> "RunnerHostHygieneGeneratedCacheBudget":
        self.cache_key = _required_public_token(
            self.cache_key,
            "runner host hygiene generated cache budget requires cache_key",
        )
        return self


class RunnerHostHygienePolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    minimum_free_disk_bytes: int = Field(default=0, ge=0)
    maximum_docker_reclaimable_bytes: int | None = Field(default=None, ge=0)
    maximum_runner_workdir_bytes: int | None = Field(default=None, ge=0)
    generated_cache_budgets: tuple[RunnerHostHygieneGeneratedCacheBudget, ...] = ()
    required_warm_builders: tuple[str, ...] = ()
    allow_orphan_buildkit: bool = False

    @model_validator(mode="after")
    def _normalize_policy(self) -> "RunnerHostHygienePolicy":
        self.required_warm_builders = _normalized_tokens(self.required_warm_builders)
        self.generated_cache_budgets = tuple(
            sorted(self.generated_cache_budgets, key=lambda item: item.cache_key)
        )
        if len({item.cache_key for item in self.generated_cache_budgets}) != len(
            self.generated_cache_budgets
        ):
            raise ValueError("runner host hygiene generated cache budgets must be unique")
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
    measurement_status: RunnerHostHygieneMeasurementStatus = "complete"
    reason_code: str = ""

    @model_validator(mode="after")
    def _normalize_usage(self) -> "RunnerHostHygieneRunnerWorkdirUsage":
        self.root_key = _required_text(
            _normalized_token(self.root_key),
            "runner host hygiene workdir usage requires root_key",
        )
        self.reason_code = _normalized_public_token(self.reason_code)
        if self.measurement_status == "complete" and self.reason_code:
            raise ValueError("complete runner workdir measurement cannot include reason_code")
        if self.measurement_status != "complete" and not self.reason_code:
            self.reason_code = "legacy_incomplete"
        if self.measurement_status in {"unavailable", "stale"} and (
            self.apparent_bytes or self.allocated_bytes
        ):
            raise ValueError("unavailable runner workdir measurement cannot include bytes")
        return self


class RunnerHostHygieneGeneratedCacheAgeBucket(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket: Literal["under_1h", "1h_to_24h", "1d_to_7d", "7d_to_30d", "30d_or_more"]
    entry_count: int = Field(default=0, ge=0)
    apparent_bytes: int = Field(default=0, ge=0)
    allocated_bytes: int = Field(default=0, ge=0)


class RunnerHostHygieneGeneratedCacheEntry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    run_id: int = Field(ge=1)
    apparent_bytes: int = Field(default=0, ge=0)
    allocated_bytes: int = Field(default=0, ge=0)
    age_seconds: int = Field(default=0, ge=0)
    open_handle_observations: tuple[int, ...] = ()
    github_run_state: Literal["completed", "active", "unknown"] = "unknown"

    @model_validator(mode="after")
    def _normalize_entry(self) -> "RunnerHostHygieneGeneratedCacheEntry":
        if any(value < 0 for value in self.open_handle_observations):
            raise ValueError("generated cache open-handle observations cannot be negative")
        return self


class RunnerHostHygieneGeneratedCacheUsage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_key: str
    apparent_bytes: int = Field(default=0, ge=0)
    allocated_bytes: int = Field(default=0, ge=0)
    entry_count: int = Field(default=0, ge=0)
    measurement_status: RunnerHostHygieneMeasurementStatus = "complete"
    reason_code: str = ""
    owner_valid: bool = False
    mode_valid: bool = False
    symlink_safe: bool = False
    owner_worker_observations: tuple[int, ...] = ()
    age_buckets: tuple[RunnerHostHygieneGeneratedCacheAgeBucket, ...] = ()
    entries: tuple[RunnerHostHygieneGeneratedCacheEntry, ...] = ()
    entries_truncated: bool = False
    last_cleanup_epoch_seconds: int = Field(default=0, ge=0)
    last_reclaimed_bytes: int = Field(default=0, ge=0)
    last_post_cleanup_allocated_bytes: int = Field(default=0, ge=0)
    last_removed_run_ids: tuple[int, ...] = ()
    estimated_daily_growth_bytes: int = Field(default=0, ge=0)
    cooldown_remaining_seconds: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def _normalize_usage(self) -> "RunnerHostHygieneGeneratedCacheUsage":
        self.cache_key = _required_public_token(
            self.cache_key,
            "runner host hygiene generated cache usage requires cache_key",
        )
        self.reason_code = _normalized_public_token(self.reason_code)
        if self.measurement_status == "complete" and self.reason_code:
            raise ValueError("complete generated cache measurement cannot include reason_code")
        if self.measurement_status != "complete" and not self.reason_code:
            self.reason_code = "legacy_incomplete"
        if self.measurement_status in {"unavailable", "stale"} and (
            self.apparent_bytes
            or self.allocated_bytes
            or self.entry_count
            or self.age_buckets
            or self.entries
        ):
            raise ValueError("unavailable generated cache measurement cannot include usage")
        if any(value < 0 for value in self.owner_worker_observations):
            raise ValueError("generated cache worker observations cannot be negative")
        self.age_buckets = tuple(sorted(self.age_buckets, key=_cache_age_bucket_sort_key))
        self.entries = tuple(sorted(self.entries, key=lambda item: item.run_id))
        self.last_removed_run_ids = tuple(sorted(set(self.last_removed_run_ids)))
        if any(run_id <= 0 for run_id in self.last_removed_run_ids):
            raise ValueError("generated cache cleanup history run ids must be positive")
        if self.entry_count < len(self.entries):
            raise ValueError("generated cache entry_count cannot be smaller than entries")
        return self


class RunnerHostHygieneCacheClassTelemetry(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cache_key: str
    cache_class: RunnerHostHygieneCacheClass
    scope: RunnerHostHygieneCacheScope
    availability: RunnerHostHygieneEvidenceAvailability
    measurement_basis: RunnerHostHygieneMeasurementBasis
    source: str
    observed_at: str
    unavailable_reason: str = ""
    apparent_bytes: int | None = Field(default=None, ge=0)
    allocated_bytes: int | None = Field(default=None, ge=0)
    logical_bytes: int | None = Field(default=None, ge=0)
    reclaimable_bytes: int | None = Field(default=None, ge=0)
    entry_count: int | None = Field(default=None, ge=0)
    age_buckets: tuple[RunnerHostHygieneGeneratedCacheAgeBucket, ...] = ()
    estimated_daily_growth_bytes: int | None = Field(default=None, ge=0)
    last_cleanup_epoch_seconds: int | None = Field(default=None, ge=0)
    last_reclaimed_bytes: int | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def _normalize_telemetry(self) -> "RunnerHostHygieneCacheClassTelemetry":
        self.cache_key = _required_public_token(
            self.cache_key,
            "runner host hygiene cache-class telemetry requires cache_key",
        )
        self.source = _required_public_token(
            self.source,
            "runner host hygiene cache-class telemetry requires source",
        )
        self.observed_at = _required_text(
            self.observed_at,
            "runner host hygiene cache-class telemetry requires observed_at",
        )
        self.unavailable_reason = self.unavailable_reason.strip()
        self.age_buckets = tuple(sorted(self.age_buckets, key=_cache_age_bucket_sort_key))
        filesystem_measurements = (self.apparent_bytes, self.allocated_bytes)
        docker_measurements = (self.logical_bytes, self.reclaimable_bytes)
        if self.measurement_basis == "filesystem_apparent_allocated" and any(
            value is not None for value in docker_measurements
        ):
            raise ValueError("filesystem cache telemetry cannot include Docker byte measurements")
        if self.measurement_basis == "docker_engine" and any(
            value is not None for value in filesystem_measurements
        ):
            raise ValueError("Docker cache telemetry cannot include filesystem byte measurements")
        if self.cache_class in {"runner_workdir", "generated_filesystem"}:
            if self.measurement_basis != "filesystem_apparent_allocated":
                raise ValueError("filesystem cache classes require filesystem measurement basis")
        elif self.measurement_basis != "docker_engine":
            raise ValueError("Docker cache classes require Docker measurement basis")
        measurements = (
            *filesystem_measurements,
            *docker_measurements,
            self.entry_count,
            self.estimated_daily_growth_bytes,
            self.last_cleanup_epoch_seconds,
            self.last_reclaimed_bytes,
        )
        if self.availability in {"unavailable", "stale"}:
            if any(value is not None for value in measurements) or self.age_buckets:
                raise ValueError("unavailable cache telemetry cannot include measurements")
            if not self.unavailable_reason:
                raise ValueError("unavailable cache telemetry requires unavailable_reason")
        else:
            if not any(value is not None for value in measurements) and not self.age_buckets:
                raise ValueError("available cache telemetry requires measurement evidence")
            if self.availability == "partial" and not self.unavailable_reason:
                raise ValueError("partial cache telemetry requires unavailable_reason")
            if self.availability == "available" and self.unavailable_reason:
                raise ValueError("available cache telemetry cannot include unavailable_reason")
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
    generated_cache_apparent_bytes: int = Field(default=0, ge=0)
    generated_cache_allocated_bytes: int = Field(default=0, ge=0)
    generated_cache_usage: tuple[RunnerHostHygieneGeneratedCacheUsage, ...] = ()
    cache_class_telemetry: tuple[RunnerHostHygieneCacheClassTelemetry, ...] = ()
    docker_toolchain: "RunnerHostDockerToolchainObservation | None" = None
    warm_builders: tuple[str, ...] = ()
    image_inventory: tuple["RunnerHostHygieneImageInventoryItem", ...] = ()
    image_inventory_total_count: int = Field(default=0, ge=0)
    image_inventory_truncated: bool = False
    volume_inventory: tuple["RunnerHostHygieneVolumeInventoryItem", ...] = ()
    volume_inventory_total_count: int = Field(default=0, ge=0)
    volume_inventory_truncated: bool = False
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
        self.generated_cache_usage = tuple(
            sorted(self.generated_cache_usage, key=lambda item: item.cache_key)
        )
        self.cache_class_telemetry = _sorted_cache_class_telemetry(self.cache_class_telemetry)
        self.image_inventory = _sorted_image_inventory(self.image_inventory)
        self.volume_inventory = _sorted_volume_inventory(self.volume_inventory)
        if not self.image_inventory_total_count:
            self.image_inventory_total_count = len(self.image_inventory)
        if not self.volume_inventory_total_count:
            self.volume_inventory_total_count = len(self.volume_inventory)
        if self.image_inventory_total_count < len(self.image_inventory):
            raise ValueError("image inventory total count cannot be smaller than retained rows")
        if self.volume_inventory_total_count < len(self.volume_inventory):
            raise ValueError("volume inventory total count cannot be smaller than retained rows")
        if self.image_inventory_truncated != (
            self.image_inventory_total_count > len(self.image_inventory)
        ):
            raise ValueError("image inventory truncation must match total count")
        if self.volume_inventory_truncated != (
            self.volume_inventory_total_count > len(self.volume_inventory)
        ):
            raise ValueError("volume inventory truncation must match total count")
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
    size_bytes: int | None = Field(default=None, ge=0)
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
    size_bytes: int | None = Field(default=None, ge=0)
    referenced_by_containers: int | None = Field(default=None, ge=0)
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
    observed_at: str = ""
    free_disk_bytes: int = Field(default=0, ge=0)
    docker_reclaimable_bytes: int = Field(default=0, ge=0)
    docker_reclaimable_breakdown: RunnerHostHygieneDockerReclaimableBreakdown = Field(
        default_factory=RunnerHostHygieneDockerReclaimableBreakdown
    )
    runner_workdir_bytes: int = Field(default=0, ge=0)
    runner_workdir_allocated_bytes: int = Field(default=0, ge=0)
    runner_workdir_usage: tuple[RunnerHostHygieneRunnerWorkdirUsage, ...] = ()
    generated_cache_apparent_bytes: int = Field(default=0, ge=0)
    generated_cache_allocated_bytes: int = Field(default=0, ge=0)
    generated_cache_usage: tuple[RunnerHostHygieneGeneratedCacheUsage, ...] = ()
    cache_class_telemetry: tuple[RunnerHostHygieneCacheClassTelemetry, ...] = ()
    docker_toolchain: RunnerHostDockerToolchainObservation | None = None
    warm_builders: tuple[str, ...] = ()
    image_inventory: tuple[RunnerHostHygieneImageInventoryItem, ...] = ()
    image_inventory_total_count: int = Field(default=0, ge=0)
    image_inventory_truncated: bool = False
    volume_inventory: tuple[RunnerHostHygieneVolumeInventoryItem, ...] = ()
    volume_inventory_total_count: int = Field(default=0, ge=0)
    volume_inventory_truncated: bool = False
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
        self.observed_at = self.observed_at.strip()
        self.warm_builders = _normalized_tokens(self.warm_builders)
        self.runner_workdir_usage = tuple(
            sorted(self.runner_workdir_usage, key=lambda item: item.root_key)
        )
        self.generated_cache_usage = tuple(
            sorted(self.generated_cache_usage, key=lambda item: item.cache_key)
        )
        self.cache_class_telemetry = _sorted_cache_class_telemetry(self.cache_class_telemetry)
        self.image_inventory = _sorted_image_inventory(self.image_inventory)
        self.volume_inventory = _sorted_volume_inventory(self.volume_inventory)
        if not self.image_inventory_total_count:
            self.image_inventory_total_count = len(self.image_inventory)
        if not self.volume_inventory_total_count:
            self.volume_inventory_total_count = len(self.volume_inventory)
        if self.image_inventory_total_count < len(self.image_inventory):
            raise ValueError("image inventory total count cannot be smaller than retained rows")
        if self.volume_inventory_total_count < len(self.volume_inventory):
            raise ValueError("volume inventory total count cannot be smaller than retained rows")
        if self.image_inventory_truncated != (
            self.image_inventory_total_count > len(self.image_inventory)
        ):
            raise ValueError("image inventory truncation must match total count")
        if self.volume_inventory_truncated != (
            self.volume_inventory_total_count > len(self.volume_inventory)
        ):
            raise ValueError("volume inventory truncation must match total count")
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
    allow_dangling_image_prune: bool = False
    allow_generated_run_cache_prune: bool = False
    allowed_generated_cache_keys: tuple[str, ...] = ()
    allowed_buildkit_builders: tuple[str, ...] = ()
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
        self.allowed_buildkit_builders = _normalized_buildkit_builder_names(
            self.allowed_buildkit_builders
        )
        self.allowed_generated_cache_keys = tuple(
            sorted(
                {
                    _required_public_token(
                        value,
                        "runner host hygiene allowed generated cache key must be public-safe",
                    )
                    for value in self.allowed_generated_cache_keys
                }
            )
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
    target_buildkit_builder: str = ""
    max_used_space_bytes: int = Field(default=0, ge=0)
    target_buildkit_state_volumes: tuple[str, ...] = ()
    target_generated_cache_key: str = ""
    target_generated_cache_run_ids: tuple[int, ...] = ()
    generated_cache_minimum_age_hours: int = Field(default=0, ge=0)
    generated_cache_high_water_bytes: int = Field(default=0, ge=0)
    generated_cache_low_water_bytes: int = Field(default=0, ge=0)
    generated_cache_cooldown_hours: int = Field(default=0, ge=0)
    audit_record_key: str = ""

    @model_validator(mode="after")
    def _normalize_request(self) -> "RunnerHostHygieneApplyRequest":
        self.host_name = _normalized_host_name(self.host_name)
        if not self.host_name:
            raise ValueError("runner host hygiene apply requires host_name")
        self.retained_warm_builders = _normalized_tokens(self.retained_warm_builders)
        self.target_buildkit_builder = _normalized_buildkit_builder_name(
            self.target_buildkit_builder
        )
        self.target_buildkit_state_volumes = _normalized_volume_name_entries(
            self.target_buildkit_state_volumes
        )
        self.target_generated_cache_key = _normalized_public_token(self.target_generated_cache_key)
        self.target_generated_cache_run_ids = tuple(
            sorted(set(self.target_generated_cache_run_ids))
        )
        if any(run_id <= 0 for run_id in self.target_generated_cache_run_ids):
            raise ValueError("generated cache run ids must be positive")
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


def sanitize_runner_host_hygiene_audit_record_for_persistence(
    audit: RunnerHostHygieneApplyAuditRecord,
) -> RunnerHostHygieneApplyAuditRecord:
    return RunnerHostHygieneApplyAuditRecord.model_validate(
        {
            **audit.model_dump(mode="python"),
            "pre_apply_report": _sanitize_runner_host_hygiene_report_for_persistence(
                audit.pre_apply_report
            ).model_dump(mode="python"),
            "post_apply_report": (
                _sanitize_runner_host_hygiene_report_for_persistence(
                    audit.post_apply_report
                ).model_dump(mode="python")
                if audit.post_apply_report is not None
                else None
            ),
        }
    )


def _sanitize_runner_host_hygiene_report_for_persistence(
    report: RunnerHostHygieneReport,
) -> RunnerHostHygieneReport:
    image_inventory = tuple(
        item.model_copy(
            update={
                "repository": "",
                "tag": "",
                "created_at": "",
            }
        )
        for item in report.image_inventory[:RUNNER_HOST_HYGIENE_MAX_DOCKER_INVENTORY_ITEMS]
    )
    volume_inventory = tuple(
        item.model_copy(update={"mountpoint": "", "labels": ()})
        for item in report.volume_inventory[:RUNNER_HOST_HYGIENE_MAX_DOCKER_INVENTORY_ITEMS]
    )
    image_inventory_total_count = max(
        report.image_inventory_total_count,
        len(report.image_inventory),
    )
    volume_inventory_total_count = max(
        report.volume_inventory_total_count,
        len(report.volume_inventory),
    )
    cache_class_telemetry = tuple(
        item.model_copy(
            update={"unavailable_reason": _sanitized_unavailable_reason(item.unavailable_reason)}
        )
        for item in report.cache_class_telemetry
    )
    return RunnerHostHygieneReport.model_validate(
        {
            **report.model_dump(mode="python"),
            "cache_class_telemetry": tuple(
                item.model_dump(mode="python") for item in cache_class_telemetry
            ),
            "image_inventory": tuple(item.model_dump(mode="python") for item in image_inventory),
            "image_inventory_total_count": image_inventory_total_count,
            "image_inventory_truncated": image_inventory_total_count > len(image_inventory),
            "volume_inventory": tuple(item.model_dump(mode="python") for item in volume_inventory),
            "volume_inventory_total_count": volume_inventory_total_count,
            "volume_inventory_truncated": volume_inventory_total_count > len(volume_inventory),
        }
    )


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
    partial_workdir_roots = tuple(
        item.root_key
        for item in observation.runner_workdir_usage
        if item.measurement_status != "complete"
    )
    if partial_workdir_roots:
        findings.append(
            _finding(
                "runner_workdir_measurement_partial",
                (
                    "runner host work directory measurement was partial for roots: "
                    + ", ".join(partial_workdir_roots)
                ),
            )
        )
    generated_cache_budgets = {
        item.cache_key: item.high_water_bytes for item in policy.generated_cache_budgets
    }
    over_budget_generated_caches = tuple(
        item.cache_key
        for item in observation.generated_cache_usage
        if item.cache_key in generated_cache_budgets
        and item.allocated_bytes > generated_cache_budgets[item.cache_key]
    )
    if over_budget_generated_caches:
        findings.append(
            _finding(
                "generated_cache_above_limit",
                "runner host generated caches exceed configured high-water limits: "
                + ", ".join(over_budget_generated_caches),
            )
        )
    partial_generated_caches = tuple(
        item.cache_key
        for item in observation.generated_cache_usage
        if item.measurement_status != "complete"
    )
    if partial_generated_caches:
        findings.append(
            _finding(
                "generated_cache_measurement_partial",
                "runner host generated cache measurement was partial for keys: "
                + ", ".join(partial_generated_caches),
            )
        )
    unsafe_generated_caches = tuple(
        item.cache_key
        for item in observation.generated_cache_usage
        if not item.owner_valid or not item.mode_valid or not item.symlink_safe
    )
    if unsafe_generated_caches:
        findings.append(
            _finding(
                "generated_cache_safety_failed",
                "runner host generated cache safety checks failed for keys: "
                + ", ".join(unsafe_generated_caches),
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
    cache_class_telemetry = _merged_cache_class_telemetry(
        _cache_class_telemetry_from_observation(observation),
        observation.cache_class_telemetry,
    )
    return RunnerHostHygieneReport(
        status=status,
        host_name=observation.host_name,
        observed_at=observation.observed_at,
        free_disk_bytes=observation.free_disk_bytes,
        docker_reclaimable_bytes=observation.docker_reclaimable_bytes,
        docker_reclaimable_breakdown=observation.docker_reclaimable_breakdown,
        runner_workdir_bytes=observation.runner_workdir_bytes,
        runner_workdir_allocated_bytes=observation.runner_workdir_allocated_bytes,
        runner_workdir_usage=observation.runner_workdir_usage,
        generated_cache_apparent_bytes=observation.generated_cache_apparent_bytes,
        generated_cache_allocated_bytes=observation.generated_cache_allocated_bytes,
        generated_cache_usage=observation.generated_cache_usage,
        cache_class_telemetry=cache_class_telemetry,
        docker_toolchain=observation.docker_toolchain,
        warm_builders=observation.warm_builders,
        image_inventory=observation.image_inventory,
        image_inventory_total_count=observation.image_inventory_total_count,
        image_inventory_truncated=observation.image_inventory_truncated,
        volume_inventory=observation.volume_inventory,
        volume_inventory_total_count=observation.volume_inventory_total_count,
        volume_inventory_truncated=observation.volume_inventory_truncated,
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


def _cache_class_telemetry_from_observation(
    observation: RunnerHostHygieneObservation,
) -> tuple[RunnerHostHygieneCacheClassTelemetry, ...]:
    telemetry: list[RunnerHostHygieneCacheClassTelemetry] = []
    for workdir_usage in observation.runner_workdir_usage:
        availability = _telemetry_availability(workdir_usage.measurement_status)
        unavailable_reason = workdir_usage.reason_code if availability != "available" else ""
        telemetry.append(
            RunnerHostHygieneCacheClassTelemetry(
                cache_key=workdir_usage.root_key,
                cache_class="runner_workdir",
                scope="runner_root",
                availability=availability,
                measurement_basis="filesystem_apparent_allocated",
                source="runner_workdir_probe",
                observed_at=observation.observed_at,
                unavailable_reason=unavailable_reason,
                apparent_bytes=(
                    workdir_usage.apparent_bytes
                    if availability in {"available", "partial"}
                    else None
                ),
                allocated_bytes=(
                    workdir_usage.allocated_bytes
                    if availability in {"available", "partial"}
                    else None
                ),
            )
        )
    for generated_usage in observation.generated_cache_usage:
        unavailable_reasons: list[str] = []
        availability = _telemetry_availability(generated_usage.measurement_status)
        if availability != "available":
            unavailable_reasons.append(generated_usage.reason_code)
        if generated_usage.entries_truncated:
            unavailable_reasons.append("entry_history_truncated")
            if availability == "available":
                availability = "partial"
        has_cleanup_history = generated_usage.last_cleanup_epoch_seconds > 0
        has_measurement = availability in {"available", "partial"}
        telemetry.append(
            RunnerHostHygieneCacheClassTelemetry(
                cache_key=generated_usage.cache_key,
                cache_class="generated_filesystem",
                scope="generated_cache",
                availability=availability,
                measurement_basis="filesystem_apparent_allocated",
                source="generated_cache_probe",
                observed_at=observation.observed_at,
                unavailable_reason=",".join(unavailable_reasons),
                apparent_bytes=generated_usage.apparent_bytes if has_measurement else None,
                allocated_bytes=generated_usage.allocated_bytes if has_measurement else None,
                entry_count=generated_usage.entry_count if has_measurement else None,
                age_buckets=generated_usage.age_buckets if has_measurement else (),
                estimated_daily_growth_bytes=(
                    generated_usage.estimated_daily_growth_bytes
                    if has_measurement and has_cleanup_history
                    else None
                ),
                last_cleanup_epoch_seconds=(
                    generated_usage.last_cleanup_epoch_seconds
                    if has_measurement and has_cleanup_history
                    else None
                ),
                last_reclaimed_bytes=(
                    generated_usage.last_reclaimed_bytes
                    if has_measurement and has_cleanup_history
                    else None
                ),
            )
        )
    image_logical_bytes = _inventory_size_total(
        observation.image_inventory,
        truncated=observation.image_inventory_truncated,
    )
    image_unavailable_reasons = _inventory_unavailable_reasons(
        observation.image_inventory,
        truncated=observation.image_inventory_truncated,
    )
    telemetry.append(
        RunnerHostHygieneCacheClassTelemetry(
            cache_key="images",
            cache_class="docker_images",
            scope="host",
            availability="partial" if image_unavailable_reasons else "available",
            measurement_basis="docker_engine",
            source="docker_engine",
            observed_at=observation.observed_at,
            unavailable_reason=",".join(image_unavailable_reasons),
            logical_bytes=image_logical_bytes,
            reclaimable_bytes=observation.docker_reclaimable_breakdown.images_bytes,
            entry_count=observation.image_inventory_total_count,
        )
    )
    telemetry.append(
        RunnerHostHygieneCacheClassTelemetry(
            cache_key="containers",
            cache_class="docker_containers",
            scope="host",
            availability="available",
            measurement_basis="docker_engine",
            source="docker_engine",
            observed_at=observation.observed_at,
            reclaimable_bytes=observation.docker_reclaimable_breakdown.containers_bytes,
        )
    )
    volume_logical_bytes = _inventory_size_total(
        observation.volume_inventory,
        truncated=observation.volume_inventory_truncated,
    )
    volume_unavailable_reasons = _inventory_unavailable_reasons(
        observation.volume_inventory,
        truncated=observation.volume_inventory_truncated,
    )
    telemetry.append(
        RunnerHostHygieneCacheClassTelemetry(
            cache_key="volumes",
            cache_class="docker_volumes",
            scope="host",
            availability="partial" if volume_unavailable_reasons else "available",
            measurement_basis="docker_engine",
            source="docker_engine",
            observed_at=observation.observed_at,
            unavailable_reason=",".join(volume_unavailable_reasons),
            logical_bytes=volume_logical_bytes,
            reclaimable_bytes=observation.docker_reclaimable_breakdown.local_volumes_bytes,
            entry_count=observation.volume_inventory_total_count,
        )
    )
    telemetry.append(
        RunnerHostHygieneCacheClassTelemetry(
            cache_key="build-cache",
            cache_class="docker_build_cache",
            scope="host",
            availability="available",
            measurement_basis="docker_engine",
            source="docker_engine",
            observed_at=observation.observed_at,
            reclaimable_bytes=observation.docker_reclaimable_breakdown.build_cache_bytes,
        )
    )
    return _sorted_cache_class_telemetry(tuple(telemetry))


def _inventory_size_total(
    inventory: tuple[
        RunnerHostHygieneImageInventoryItem | RunnerHostHygieneVolumeInventoryItem, ...
    ],
    *,
    truncated: bool,
) -> int | None:
    if truncated or any(item.size_bytes is None for item in inventory):
        return None
    return sum(cast(int, item.size_bytes) for item in inventory)


def _inventory_unavailable_reasons(
    inventory: tuple[
        RunnerHostHygieneImageInventoryItem | RunnerHostHygieneVolumeInventoryItem, ...
    ],
    *,
    truncated: bool,
) -> tuple[str, ...]:
    reasons: list[str] = []
    if truncated:
        reasons.append("inventory_truncated")
    if any(item.size_bytes is None for item in inventory):
        reasons.append("inventory_size_unavailable")
    return tuple(reasons)


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
    blockers.extend(_target_builder_blockers(policy=policy, request=request))
    blockers.extend(_target_generated_cache_blockers(policy=policy, request=request, report=report))

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
    if action == "prune_dangling_images":
        return policy.allow_dangling_image_prune
    if action == "prune_generated_run_cache":
        return policy.allow_generated_run_cache_prune
    if action == "remove_buildkit_state_volumes":
        return policy.allow_buildkit_state_volume_remove
    if action == "prune_runner_workdir":
        return policy.allow_runner_workdir_prune
    return policy.allow_runner_service_restart


def _target_generated_cache_blockers(
    *,
    policy: RunnerHostHygieneApplyPolicy,
    request: RunnerHostHygieneApplyRequest,
    report: RunnerHostHygieneReport,
) -> tuple[RunnerHostHygieneApplyBlocker, ...]:
    if request.action != "prune_generated_run_cache":
        return ()
    blockers: list[RunnerHostHygieneApplyBlocker] = []
    cache_key = request.target_generated_cache_key
    if not cache_key:
        blockers.append(
            _apply_blocker(
                "target_generated_cache_key_missing",
                "runner host generated cache cleanup requires a target cache key",
            )
        )
        return tuple(blockers)
    if cache_key not in policy.allowed_generated_cache_keys:
        blockers.append(
            _apply_blocker(
                "target_generated_cache_not_allowlisted",
                f"runner host generated cache key is not allowlisted: {cache_key}",
            )
        )
    cache_by_key = {item.cache_key: item for item in report.generated_cache_usage}
    cache = cache_by_key.get(cache_key)
    if cache is None:
        blockers.append(
            _apply_blocker(
                "target_generated_cache_missing_from_report",
                f"runner host generated cache is missing from report: {cache_key}",
            )
        )
        return tuple(blockers)
    if (
        request.generated_cache_high_water_bytes <= 0
        or request.generated_cache_low_water_bytes >= request.generated_cache_high_water_bytes
        or request.generated_cache_minimum_age_hours <= 0
        or request.generated_cache_cooldown_hours <= 0
    ):
        blockers.append(
            _apply_blocker(
                "target_generated_cache_budget_invalid",
                "runner host generated cache cleanup requires positive age/high-water inputs "
                "and a lower low-water target",
            )
        )
    if cache.allocated_bytes <= request.generated_cache_high_water_bytes:
        blockers.append(
            _apply_blocker(
                "target_generated_cache_below_high_water",
                (
                    "runner host generated cache is not above its high-water limit: "
                    f"{cache.allocated_bytes} <= {request.generated_cache_high_water_bytes}"
                ),
            )
        )
    if cache.cooldown_remaining_seconds > 0:
        blockers.append(
            _apply_blocker(
                "target_generated_cache_cooldown_active",
                (
                    "runner host generated cache cleanup cooldown remains active for "
                    f"{cache.cooldown_remaining_seconds} seconds"
                ),
            )
        )
    if cache.measurement_status != "complete" or not (
        cache.owner_valid and cache.mode_valid and cache.symlink_safe
    ):
        blockers.append(
            _apply_blocker(
                "target_generated_cache_safety_failed",
                f"runner host generated cache safety evidence is incomplete: {cache_key}",
            )
        )
    if len(cache.owner_worker_observations) < 2 or any(
        value > 0 for value in cache.owner_worker_observations
    ):
        blockers.append(
            _apply_blocker(
                "target_generated_cache_owner_busy",
                (
                    "runner host generated cache owner lacks two idle worker observations: "
                    f"{cache_key}"
                ),
            )
        )
    if not request.target_generated_cache_run_ids:
        blockers.append(
            _apply_blocker(
                "target_generated_cache_runs_missing",
                "runner host generated cache cleanup requires completed run targets",
            )
        )
        return tuple(blockers)
    entries_by_run_id = {item.run_id: item for item in cache.entries}
    minimum_age_seconds = request.generated_cache_minimum_age_hours * 3600
    for run_id in request.target_generated_cache_run_ids:
        entry = entries_by_run_id.get(run_id)
        if entry is None:
            blockers.append(
                _apply_blocker(
                    "target_generated_cache_run_missing",
                    f"runner host generated cache run is missing from report: {run_id}",
                )
            )
            continue
        if entry.github_run_state != "completed":
            blockers.append(
                _apply_blocker(
                    "target_generated_cache_run_active",
                    (
                        "runner host generated cache run is not proven completed: "
                        f"{run_id} state={entry.github_run_state}"
                    ),
                )
            )
        if entry.age_seconds < minimum_age_seconds:
            blockers.append(
                _apply_blocker(
                    "target_generated_cache_run_too_recent",
                    (
                        "runner host generated cache run is newer than the configured age: "
                        f"{run_id} age_seconds={entry.age_seconds}"
                    ),
                )
            )
        if len(entry.open_handle_observations) < 2 or any(
            value > 0 for value in entry.open_handle_observations
        ):
            blockers.append(
                _apply_blocker(
                    "target_generated_cache_run_not_idle",
                    f"runner host generated cache run lacks two zero-handle observations: {run_id}",
                )
            )
    return tuple(blockers)


def _target_builder_blockers(
    *,
    policy: RunnerHostHygieneApplyPolicy,
    request: RunnerHostHygieneApplyRequest,
) -> tuple[RunnerHostHygieneApplyBlocker, ...]:
    if request.action != "prune_docker_cache" or not request.target_buildkit_builder:
        return ()
    blockers: list[RunnerHostHygieneApplyBlocker] = []
    if request.target_buildkit_builder not in policy.allowed_buildkit_builders:
        blockers.append(
            _apply_blocker(
                "target_builder_not_allowlisted",
                (
                    "runner host hygiene target BuildKit builder is not allowlisted: "
                    f"{request.target_buildkit_builder}"
                ),
            )
        )
    if request.max_used_space_bytes <= 0:
        blockers.append(
            _apply_blocker(
                "target_builder_budget_missing",
                "runner host hygiene target BuildKit builder requires a positive cache budget",
            )
        )
    return tuple(blockers)


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
        if (
            volume.referenced_by_containers is None
            or not volume.dangling
            or volume.referenced_by_containers > 0
        ):
            reference_summary = (
                str(volume.referenced_by_containers)
                if volume.referenced_by_containers is not None
                else "unknown"
            )
            blockers.append(
                _apply_blocker(
                    "target_volume_active",
                    (
                        "runner host hygiene target volume is still referenced: "
                        f"{volume_name} links={reference_summary}"
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
    if action == "prune_dangling_images":
        return (
            "capture pre-apply host hygiene evidence",
            "prune only dangling Docker images older than the approved age",
            "capture post-apply host hygiene evidence and write the audit record",
        )
    if action == "prune_generated_run_cache":
        return (
            "capture completed-run, ownership, symlink, and open-handle evidence",
            "remove only exact allowlisted completed-run cache directories above high water",
            "capture post-apply generated cache evidence and write the audit record",
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
    if action in {"prune_docker_cache", "prune_dangling_images"}:
        return "docker_cache"
    if action == "remove_buildkit_state_volumes":
        return "docker_volume"
    if action == "prune_generated_run_cache":
        return "generated_cache"
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


def _normalized_buildkit_builder_names(values: tuple[str, ...]) -> tuple[str, ...]:
    return _normalized_values(values, _normalized_buildkit_builder_name)


def _normalized_buildkit_builder_name(value: str) -> str:
    normalized = value.strip().lower()
    if normalized and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", normalized):
        raise ValueError("BuildKit builder name must be a public-safe token")
    return normalized


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


def _sorted_cache_class_telemetry(
    values: tuple[RunnerHostHygieneCacheClassTelemetry, ...],
) -> tuple[RunnerHostHygieneCacheClassTelemetry, ...]:
    sorted_values = tuple(sorted(values, key=lambda item: (item.cache_class, item.cache_key)))
    keys = tuple((item.cache_class, item.cache_key) for item in sorted_values)
    if len(set(keys)) != len(keys):
        raise ValueError("runner host hygiene cache-class telemetry must be unique")
    return sorted_values


def _merged_cache_class_telemetry(
    derived: tuple[RunnerHostHygieneCacheClassTelemetry, ...],
    explicit: tuple[RunnerHostHygieneCacheClassTelemetry, ...],
) -> tuple[RunnerHostHygieneCacheClassTelemetry, ...]:
    telemetry_by_key = {(item.cache_class, item.cache_key): item for item in derived}
    telemetry_by_key.update({(item.cache_class, item.cache_key): item for item in explicit})
    return _sorted_cache_class_telemetry(tuple(telemetry_by_key.values()))


def _telemetry_availability(
    measurement_status: RunnerHostHygieneMeasurementStatus,
) -> RunnerHostHygieneEvidenceAvailability:
    if measurement_status == "complete":
        return "available"
    if measurement_status == "partial":
        return "partial"
    if measurement_status == "unavailable":
        return "unavailable"
    return "stale"


def _normalized_values(values: tuple[str, ...], normalize: Callable[[str], str]) -> tuple[str, ...]:
    return tuple(sorted({normalized for value in values if (normalized := normalize(value))}))


def _normalized_token(value: str) -> str:
    return value.strip().lower()


def _cache_age_bucket_sort_key(item: RunnerHostHygieneGeneratedCacheAgeBucket) -> int:
    return {
        "under_1h": 0,
        "1h_to_24h": 1,
        "1d_to_7d": 2,
        "7d_to_30d": 3,
        "30d_or_more": 4,
    }[item.bucket]


def _normalized_public_token(value: str) -> str:
    normalized = value.strip().lower()
    if normalized and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", normalized):
        raise ValueError("value must be a public-safe token")
    return normalized


def _required_public_token(value: str, message: str) -> str:
    return _required_text(_normalized_public_token(value), message)


def _sanitized_unavailable_reason(value: str) -> str:
    if not value.strip():
        return ""
    try:
        normalized_reasons = tuple(
            _required_public_token(
                reason,
                "runner host hygiene unavailable reason must be public-safe",
            )
            for reason in value.split(",")
        )
    except ValueError:
        return "redacted_reason"
    summary = ",".join(normalized_reasons)
    return summary if len(summary) <= 64 else "redacted_reason"


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
