from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from decimal import InvalidOperation
import json
import os
import posixpath
import pwd
import re
import socket
import subprocess
import time
from typing import Literal, cast
from urllib.error import HTTPError
from urllib.error import URLError
from urllib.parse import quote
from urllib.request import Request, urlopen

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAction
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditStatus
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyPlan
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyRequest
from control_plane.contracts.runner_host_hygiene import RunnerHostDockerToolchainObservation
from control_plane.contracts.runner_host_hygiene import (
    RunnerHostHygieneAuditDeliveryEnvelope,
)
from control_plane.contracts.runner_host_hygiene import (
    RunnerHostHygieneDockerReclaimableBreakdown,
)
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneImageInventoryItem
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneGeneratedCacheAgeBucket
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneGeneratedCacheBudget
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneGeneratedCacheEntry
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneGeneratedCacheUsage
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneObservation
from control_plane.contracts.runner_host_hygiene import RunnerHostHygienePolicy
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneReport
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneRunnerWorkdirUsage
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneVolumeInventoryItem
from control_plane.contracts.runner_host_hygiene import evaluate_runner_host_hygiene
from control_plane.contracts.runner_host_hygiene import plan_runner_host_hygiene_apply
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.runner_host_hygiene_audit_spool import (
    RunnerHostHygieneAuditSpool,
)


AUDIT_ROUTE_PATH = "/v1/evidence/runner-host-hygiene/audits"
DEFAULT_PRUNE_UNTIL = "168h"
MINIMUM_PRUNE_UNTIL_HOURS = 168
MINIMUM_DEFAULT_CACHE_KEEP_STORAGE_BYTES = 8 * 1024**3
HOST_LOCK_PATH = "/tmp/launchplane-runner-host-hygiene.lock"
_BUILDCTL_STORAGE_MB_BYTES = 1_000_000
_RUNNER_WORKDIR_USAGE_HELPER = "/usr/local/sbin/launchplane-runner-workdir-usage"
_GENERATED_RUN_CACHE_HELPER = "/usr/local/sbin/launchplane-generated-run-cache"
_DOCKER_DISK_USAGE_COMMAND = (
    "curl",
    "--silent",
    "--show-error",
    "--fail",
    "--unix-socket",
    "/var/run/docker.sock",
    "http://localhost/v1.45/system/df?verbose=1",
)
_ACTIVE_WORK_COMMAND = (
    "command -v pgrep >/dev/null 2>&1 || exit 127; "
    "command -v ps >/dev/null 2>&1 || exit 127; "
    "ancestor_pids=''; pid=$PPID; "
    'while [ -n "$pid" ] && [ "$pid" -gt 1 ]; do '
    'ancestor_pids="$ancestor_pids $pid"; '
    "pid=$(ps -o ppid= -p \"$pid\" | tr -d ' '); "
    "done; printf 'ancestor_pids=%s\\n' \"$ancestor_pids\"; "
    "status=0; output=$(pgrep -af "
    "'[d]ocker buildx|[d]ocker build|[b]uildctl|[R]unner.Worker') || status=$?; "
    'if [ "$status" -gt 1 ]; then exit "$status"; fi; '
    "printf '%s\\n' \"$output\""
)
_DOCKER_SIZE_UNITS = {
    "b": Decimal(1),
    "kb": Decimal(1000),
    "mb": Decimal(1000**2),
    "gb": Decimal(1000**3),
    "tb": Decimal(1000**4),
    "kib": Decimal(1024),
    "mib": Decimal(1024**2),
    "gib": Decimal(1024**3),
    "tib": Decimal(1024**4),
}
DOCKER_BUILDX_PLUGIN_PATH_COMMAND = (
    "docker info --format "
    '\'{{range .ClientInfo.Plugins}}{{if eq .Name "buildx"}}'
    '{{.Path}}{{"\\n"}}{{end}}{{end}}\' 2>/dev/null | '
    "awk 'NF { print; found=1; exit } END { exit found ? 0 : 1 }' || "
    "command -v docker-buildx 2>/dev/null || "
    "for path in $HOME/.docker/cli-plugins/docker-buildx "
    "/usr/local/lib/docker/cli-plugins/docker-buildx "
    "/usr/local/libexec/docker/cli-plugins/docker-buildx "
    "/usr/lib/docker/cli-plugins/docker-buildx "
    "/usr/libexec/docker/cli-plugins/docker-buildx; do "
    '[ -x "$path" ] && { printf \'%s\\n\' "$path"; break; }; '
    "done"
)


@dataclass(frozen=True)
class RemoteCommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class _DockerDiskUsageSnapshot:
    reclaimable_breakdown: RunnerHostHygieneDockerReclaimableBreakdown
    volume_inventory: tuple[RunnerHostHygieneVolumeInventoryItem, ...]


RemoteCommandRunner = Callable[[Sequence[str], int], RemoteCommandResult]
AuditPoster = Callable[[RunnerHostHygieneApplyAuditRecord, str], dict[str, object]]
BearerTokenProvider = Callable[[], str]
AuditDeliverySleeper = Callable[[float], None]
GitHubRunStateReader = Callable[[str, tuple[int, ...]], Mapping[int, str]]


@dataclass(frozen=True)
class RunnerWorkdirRoot:
    key: str
    path: str


@dataclass(frozen=True)
class GeneratedRunCacheRoot:
    key: str
    path: str
    source_repository: str
    high_water_bytes: int
    low_water_bytes: int
    minimum_age_hours: int
    cooldown_hours: int


class AuditDeliveryError(click.ClickException):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class RunnerHostHygieneExecutorRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    action: RunnerHostHygieneApplyAction
    host_name: str
    execution_lane: str
    service_user: str
    repository_scope: str
    audit_record_key: str
    retained_warm_builders: tuple[str, ...]
    mutate: bool = False
    minimum_free_disk_bytes: int = Field(default=0, ge=0)
    timeout_seconds: int = Field(default=120, ge=1)
    prune_until: str = DEFAULT_PRUNE_UNTIL
    runner_workdir_roots: tuple[RunnerWorkdirRoot, ...] = ()
    generated_run_cache_roots: tuple[GeneratedRunCacheRoot, ...] = ()
    target_generated_cache_key: str = ""
    target_generated_cache_run_ids: tuple[int, ...] = ()
    idle_observation_count: int = Field(default=2, ge=2, le=10)
    idle_observation_interval_seconds: int = Field(default=5, ge=0, le=60)
    resolve_action_started: bool = False
    target_buildkit_builder: str = ""
    allowed_buildkit_builders: tuple[str, ...] = ()
    max_used_space_bytes: int = Field(default=0, ge=0)
    target_buildkit_state_volumes: tuple[str, ...] = ()
    allowed_buildkit_state_volumes: tuple[str, ...] = ()

    @model_validator(mode="after")
    def _validate_request(self) -> "RunnerHostHygieneExecutorRequest":
        if self.action not in {
            "prune_docker_cache",
            "prune_dangling_images",
            "prune_generated_run_cache",
            "remove_buildkit_state_volumes",
        }:
            raise ValueError(
                "runner host hygiene executor only supports prune_docker_cache "
                "prune_dangling_images, prune_generated_run_cache, and "
                "remove_buildkit_state_volumes"
            )
        (
            self.host_name,
            self.execution_lane,
            self.service_user,
            self.repository_scope,
            self.audit_record_key,
            self.prune_until,
        ) = _strip_text_fields(
            self.host_name,
            self.execution_lane,
            self.service_user,
            self.repository_scope,
            self.audit_record_key,
            self.prune_until,
        )
        self.retained_warm_builders = tuple(
            token.strip().lower() for token in self.retained_warm_builders if token.strip()
        )
        normalized_roots: list[RunnerWorkdirRoot] = []
        seen_root_keys: set[str] = set()
        seen_root_paths: set[str] = set()
        for root in self.runner_workdir_roots:
            key = root.key.strip().lower()
            path = root.path.strip().rstrip("/")
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", key):
                raise ValueError("runner workdir root key must be a public-safe token")
            if (
                not path
                or not path.startswith("/")
                or "/../" in f"{path}/"
                or re.search(r"\s", path)
                or posixpath.normpath(path) != path
            ):
                raise ValueError("runner workdir root path must be a scoped absolute path")
            if key in seen_root_keys or path in seen_root_paths:
                raise ValueError("runner workdir roots must be unique")
            seen_root_keys.add(key)
            seen_root_paths.add(path)
            normalized_roots.append(RunnerWorkdirRoot(key=key, path=path))
        self.runner_workdir_roots = tuple(sorted(normalized_roots, key=lambda item: item.key))
        normalized_generated_roots: list[GeneratedRunCacheRoot] = []
        seen_generated_keys: set[str] = set()
        seen_generated_paths: set[str] = set()
        for generated_root in self.generated_run_cache_roots:
            key = generated_root.key.strip().lower()
            path = generated_root.path.strip().rstrip("/")
            source_repository = generated_root.source_repository.strip().lower()
            if not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,63}", key):
                raise ValueError("generated run cache key must be a public-safe token")
            if (
                not path
                or not path.startswith("/")
                or "/../" in f"{path}/"
                or re.search(r"\s", path)
                or posixpath.normpath(path) != path
            ):
                raise ValueError("generated run cache path must be a scoped absolute path")
            if not re.fullmatch(r"[a-z0-9_.-]+/[a-z0-9_.-]+", source_repository):
                raise ValueError("generated run cache repository must use owner/name")
            if (
                generated_root.high_water_bytes <= 0
                or generated_root.low_water_bytes >= generated_root.high_water_bytes
            ):
                raise ValueError("generated run cache requires positive high water above low water")
            if generated_root.minimum_age_hours <= 0:
                raise ValueError("generated run cache minimum age must be positive hours")
            if generated_root.cooldown_hours <= 0:
                raise ValueError("generated run cache cooldown must be positive hours")
            if key in seen_generated_keys or path in seen_generated_paths:
                raise ValueError("generated run cache roots must be unique")
            seen_generated_keys.add(key)
            seen_generated_paths.add(path)
            normalized_generated_roots.append(
                GeneratedRunCacheRoot(
                    key=key,
                    path=path,
                    source_repository=source_repository,
                    high_water_bytes=generated_root.high_water_bytes,
                    low_water_bytes=generated_root.low_water_bytes,
                    minimum_age_hours=generated_root.minimum_age_hours,
                    cooldown_hours=generated_root.cooldown_hours,
                )
            )
        self.generated_run_cache_roots = tuple(
            sorted(normalized_generated_roots, key=lambda item: item.key)
        )
        self.target_generated_cache_key = self.target_generated_cache_key.strip().lower()
        if self.target_generated_cache_key and not re.fullmatch(
            r"[a-z0-9][a-z0-9._-]{0,63}", self.target_generated_cache_key
        ):
            raise ValueError("target generated cache key must be a public-safe token")
        self.target_generated_cache_run_ids = tuple(
            sorted(set(self.target_generated_cache_run_ids))
        )
        if any(run_id <= 0 for run_id in self.target_generated_cache_run_ids):
            raise ValueError("target generated cache run ids must be positive")
        self.target_buildkit_builder = self.target_buildkit_builder.strip().lower()
        if self.target_buildkit_builder and not re.fullmatch(
            r"[a-z0-9][a-z0-9._-]{0,127}", self.target_buildkit_builder
        ):
            raise ValueError("target BuildKit builder must be a public-safe token")
        self.allowed_buildkit_builders = tuple(
            sorted(
                {
                    builder.strip().lower()
                    for builder in self.allowed_buildkit_builders
                    if builder.strip()
                }
            )
        )
        if any(
            not re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", builder)
            for builder in self.allowed_buildkit_builders
        ):
            raise ValueError("allowed BuildKit builders must be public-safe tokens")
        self.target_buildkit_state_volumes = tuple(
            sorted(
                {
                    volume_name.strip()
                    for volume_name in self.target_buildkit_state_volumes
                    if volume_name.strip()
                }
            )
        )
        self.allowed_buildkit_state_volumes = tuple(
            sorted(
                {
                    volume_name.strip()
                    for volume_name in self.allowed_buildkit_state_volumes
                    if volume_name.strip()
                }
            )
        )
        if self.action != "remove_buildkit_state_volumes" and self.target_buildkit_state_volumes:
            raise ValueError("Docker cache and image prune requests cannot include target volumes")
        if self.action != "remove_buildkit_state_volumes" and self.allowed_buildkit_state_volumes:
            raise ValueError("Docker cache and image prune requests cannot include allowed volumes")
        if self.action != "prune_generated_run_cache" and self.target_generated_cache_key:
            raise ValueError("non-generated-cache requests cannot include a generated cache key")
        if self.action != "prune_generated_run_cache" and self.target_generated_cache_run_ids:
            raise ValueError("non-generated-cache requests cannot include generated cache run ids")
        if self.action == "prune_dangling_images" and (
            self.target_buildkit_builder
            or self.allowed_buildkit_builders
            or self.max_used_space_bytes
        ):
            raise ValueError("dangling image prune requests cannot include BuildKit builder inputs")
        if self.action == "remove_buildkit_state_volumes" and (
            self.target_buildkit_builder
            or self.allowed_buildkit_builders
            or self.max_used_space_bytes
        ):
            raise ValueError("BuildKit volume removal requests cannot include builder cache inputs")
        if self.action == "prune_generated_run_cache" and (
            self.target_buildkit_builder
            or self.allowed_buildkit_builders
            or self.max_used_space_bytes
            or self.target_buildkit_state_volumes
            or self.allowed_buildkit_state_volumes
        ):
            raise ValueError("generated cache requests cannot include Docker mutation targets")
        if self.action == "prune_generated_run_cache":
            if not self.target_generated_cache_key:
                raise ValueError("generated cache requests require a target cache key")
            if self.target_generated_cache_key not in {
                root.key for root in self.generated_run_cache_roots
            }:
                raise ValueError("target generated cache key is not configured")
        if (
            not self.target_buildkit_builder
            and self.max_used_space_bytes
            and self.max_used_space_bytes < MINIMUM_DEFAULT_CACHE_KEEP_STORAGE_BYTES
        ):
            raise ValueError("default Docker cache budget must retain at least 8 GiB")
        if not self.target_buildkit_builder and self.allowed_buildkit_builders:
            raise ValueError("allowed BuildKit builders require a target BuildKit builder")
        if not self.host_name:
            raise ValueError("runner host hygiene executor requires host_name")
        if not self.execution_lane:
            raise ValueError("runner host hygiene executor requires execution_lane")
        if not self.service_user:
            raise ValueError("runner host hygiene executor requires service_user")
        if "/" not in self.repository_scope:
            raise ValueError("runner host hygiene executor requires repository_scope as owner/name")
        if not self.audit_record_key:
            raise ValueError("runner host hygiene executor requires audit_record_key")
        if not self.retained_warm_builders:
            raise ValueError("runner host hygiene executor requires retained_warm_builders")
        if not self.prune_until:
            raise ValueError("runner host hygiene executor requires prune_until")
        prune_until_match = re.fullmatch(r"([1-9][0-9]*)h", self.prune_until.lower())
        if prune_until_match is None:
            raise ValueError("runner host hygiene prune_until must be a whole-hour duration")
        prune_until_hours = int(prune_until_match.group(1))
        if prune_until_hours < MINIMUM_PRUNE_UNTIL_HOURS:
            raise ValueError(
                "runner host hygiene prune_until must retain at least 168 hours of cache"
            )
        self.prune_until = f"{prune_until_hours}h"
        if not self.runner_workdir_roots:
            raise ValueError("runner host hygiene executor requires runner_workdir_roots")
        return self


class RunnerHostHygieneExecutorResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    audit_record_key: str
    planned_response: dict[str, object]
    terminal_response: dict[str, object] | None = None
    audit_delivery_pending: bool = False
    reconciled: bool = False
    message: str


def _strip_text_fields(*values: str) -> tuple[str, ...]:
    return tuple(value.strip() for value in values)


def execute_runner_host_hygiene_executor(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
    audit_poster: AuditPoster,
    audit_spool: RunnerHostHygieneAuditSpool | None = None,
    audit_delivery_sleeper: AuditDeliverySleeper = time.sleep,
    github_run_state_reader: GitHubRunStateReader | None = None,
) -> RunnerHostHygieneExecutorResult:
    if request.action == "prune_generated_run_cache" and request.mutate and audit_spool is None:
        raise click.ClickException(
            "generated run cache mutation requires durable host-local audit delivery state"
        )
    if audit_spool is not None:
        reconciliation_result = _reconcile_audit_spool(
            request=request,
            remote_runner=remote_runner,
            audit_spool=audit_spool,
            audit_poster=audit_poster,
            audit_delivery_sleeper=audit_delivery_sleeper,
            github_run_state_reader=github_run_state_reader,
        )
        if reconciliation_result is not None:
            return reconciliation_result

    pre_report = collect_runner_host_hygiene_report(
        request=request,
        remote_runner=remote_runner,
        github_run_state_reader=github_run_state_reader,
    )
    selected_generated_run_ids = _select_generated_cache_run_ids(
        request=request,
        report=pre_report,
    )
    action_request = request.model_copy(
        update={"target_generated_cache_run_ids": selected_generated_run_ids}
    )
    target_generated_cache = _target_generated_run_cache_root(action_request)
    apply_request = RunnerHostHygieneApplyRequest(
        action=action_request.action,
        host_name=action_request.host_name,
        mutate=action_request.mutate,
        retained_warm_builders=action_request.retained_warm_builders,
        target_buildkit_builder=action_request.target_buildkit_builder,
        max_used_space_bytes=action_request.max_used_space_bytes,
        target_buildkit_state_volumes=action_request.target_buildkit_state_volumes,
        target_generated_cache_key=action_request.target_generated_cache_key,
        target_generated_cache_run_ids=action_request.target_generated_cache_run_ids,
        generated_cache_minimum_age_hours=(
            target_generated_cache.minimum_age_hours if target_generated_cache else 0
        ),
        generated_cache_high_water_bytes=(
            target_generated_cache.high_water_bytes if target_generated_cache else 0
        ),
        generated_cache_low_water_bytes=(
            target_generated_cache.low_water_bytes if target_generated_cache else 0
        ),
        generated_cache_cooldown_hours=(
            target_generated_cache.cooldown_hours if target_generated_cache else 0
        ),
        audit_record_key=action_request.audit_record_key,
    )
    apply_plan = plan_runner_host_hygiene_apply(
        policy=RunnerHostHygieneApplyPolicy(
            approved_hosts=(action_request.host_name,),
            required_retained_warm_builders=action_request.retained_warm_builders,
            require_healthy_report=False,
            allow_docker_cache_prune=action_request.action == "prune_docker_cache",
            allow_dangling_image_prune=action_request.action == "prune_dangling_images",
            allow_generated_run_cache_prune=(action_request.action == "prune_generated_run_cache"),
            allowed_generated_cache_keys=tuple(
                root.key for root in action_request.generated_run_cache_roots
            ),
            allowed_buildkit_builders=action_request.allowed_buildkit_builders,
            allow_buildkit_state_volume_remove=(
                action_request.action == "remove_buildkit_state_volumes"
            ),
            allowed_buildkit_state_volumes=action_request.allowed_buildkit_state_volumes,
        ),
        request=apply_request,
        report=pre_report,
    )
    planned_audit = RunnerHostHygieneApplyAuditRecord(
        audit_record_key=request.audit_record_key,
        status="planned",
        request=apply_request,
        plan=apply_plan,
        pre_apply_report=pre_report,
        message="planned runner host hygiene apply; no host mutation was executed yet",
    )
    _assert_generated_cache_audit_redacted(audit=planned_audit, request=action_request)
    planned_idempotency_key = f"runner-host-hygiene:{request.audit_record_key}:planned"
    audit_envelope: RunnerHostHygieneAuditDeliveryEnvelope | None = None
    if audit_spool is None:
        planned_response = audit_poster(planned_audit, planned_idempotency_key)
    else:
        audit_envelope = audit_spool.create(
            planned_audit=planned_audit,
            planned_idempotency_key=planned_idempotency_key,
        )
        audit_envelope, delivery_responses = _deliver_audit_envelope(
            envelope=audit_envelope,
            audit_spool=audit_spool,
            audit_poster=audit_poster,
            audit_delivery_sleeper=audit_delivery_sleeper,
        )
        planned_response = delivery_responses.get("planned", {})
        if (
            action_request.action == "prune_generated_run_cache"
            and apply_plan.status == "ready"
            and audit_envelope.planned_delivery_state != "delivered"
        ):
            audit_envelope = audit_spool.write(
                audit_envelope.model_copy(update={"execution_state": "blocked"})
            )
            return RunnerHostHygieneExecutorResult(
                status="blocked",
                audit_record_key=request.audit_record_key,
                planned_response=planned_response,
                audit_delivery_pending=True,
                message=(
                    "generated run cache mutation requires accepted planned audit delivery; "
                    "host mutation was not executed"
                ),
            )
        if (
            apply_plan.status == "ready"
            and audit_envelope.planned_delivery_state == "pending"
            and audit_envelope.last_error_retryable is False
        ):
            audit_envelope = audit_spool.write(
                audit_envelope.model_copy(update={"execution_state": "blocked"})
            )
            return RunnerHostHygieneExecutorResult(
                status="blocked",
                audit_record_key=request.audit_record_key,
                planned_response=planned_response,
                audit_delivery_pending=True,
                message=(
                    "runner host hygiene planned audit was permanently rejected; "
                    "host mutation was not executed"
                ),
            )
    if apply_plan.status != "ready":
        generated_cache_not_needed = _generated_cache_apply_not_needed(apply_plan)
        audit_delivery_pending = (
            audit_envelope is not None and audit_envelope.planned_delivery_state != "delivered"
        )
        if audit_envelope is not None and audit_spool is not None:
            audit_envelope = audit_spool.write(
                audit_envelope.model_copy(update={"execution_state": "blocked"})
            )
        return RunnerHostHygieneExecutorResult(
            status="not_needed" if generated_cache_not_needed else "blocked",
            audit_record_key=request.audit_record_key,
            planned_response=planned_response,
            audit_delivery_pending=audit_delivery_pending,
            message=(
                f"{apply_plan.summary}; planned audit delivery remains pending"
                if audit_delivery_pending
                else (
                    "runner host generated cache cleanup is not needed under current policy"
                    if generated_cache_not_needed
                    else apply_plan.summary
                )
            ),
        )

    idle_result = None
    if action_request.action != "prune_generated_run_cache":
        idle_result = _check_host_idle(
            request=action_request,
            remote_runner=remote_runner,
            sleeper=audit_delivery_sleeper,
        )
    if idle_result is not None:
        post_report = _collect_terminal_report(
            request=action_request,
            remote_runner=remote_runner,
            github_run_state_reader=github_run_state_reader,
        )
        terminal_message = (
            f"runner host hygiene apply blocked by active runner or build processes: {idle_result}"
        )
        if post_report is None:
            terminal_message += "; terminal evidence collection failed"
        terminal_audit = _terminal_audit(
            request=apply_request,
            apply_plan=apply_plan,
            pre_report=pre_report,
            post_report=post_report,
            status="failed",
            message=terminal_message,
        )
        _assert_generated_cache_audit_redacted(audit=terminal_audit, request=action_request)
        terminal_response, audit_delivery_pending = _deliver_terminal_audit(
            terminal_audit=terminal_audit,
            audit_envelope=audit_envelope,
            audit_spool=audit_spool,
            audit_poster=audit_poster,
            audit_delivery_sleeper=audit_delivery_sleeper,
        )
        return RunnerHostHygieneExecutorResult(
            status="audit_delivery_pending" if audit_delivery_pending else "failed",
            audit_record_key=request.audit_record_key,
            planned_response=planned_response,
            terminal_response=terminal_response,
            audit_delivery_pending=audit_delivery_pending,
            message=terminal_message,
        )

    if audit_envelope is not None and audit_spool is not None:
        audit_envelope = audit_spool.write(
            audit_envelope.model_copy(update={"execution_state": "action_started"})
        )
    action_result = _execute_apply_action(request=action_request, remote_runner=remote_runner)
    post_report = _collect_terminal_report(
        request=action_request,
        remote_runner=remote_runner,
        github_run_state_reader=github_run_state_reader,
    )
    if post_report is None:
        terminal_status: RunnerHostHygieneApplyAuditStatus = "failed"
        action_outcome = "succeeded" if action_result.returncode == 0 else "failed"
        terminal_message = (
            f"runner host hygiene {action_request.action} {action_outcome}; "
            "terminal evidence collection failed"
        )
    else:
        post_evidence_failure = _post_apply_evidence_failure(
            request=action_request,
            post_report=post_report,
        )
        terminal_status = (
            "completed" if action_result.returncode == 0 and not post_evidence_failure else "failed"
        )
        terminal_message = _terminal_message(
            action=action_request.action,
            action_result=action_result,
            post_report=post_report,
            post_evidence_failure=post_evidence_failure,
        )
    terminal_audit = _terminal_audit(
        request=apply_request,
        apply_plan=apply_plan,
        pre_report=pre_report,
        post_report=post_report,
        status=terminal_status,
        message=terminal_message,
    )
    _assert_generated_cache_audit_redacted(audit=terminal_audit, request=action_request)
    terminal_response, audit_delivery_pending = _deliver_terminal_audit(
        terminal_audit=terminal_audit,
        audit_envelope=audit_envelope,
        audit_spool=audit_spool,
        audit_poster=audit_poster,
        audit_delivery_sleeper=audit_delivery_sleeper,
    )
    return RunnerHostHygieneExecutorResult(
        status="audit_delivery_pending" if audit_delivery_pending else terminal_status,
        audit_record_key=request.audit_record_key,
        planned_response=planned_response,
        terminal_response=terminal_response,
        audit_delivery_pending=audit_delivery_pending,
        message=terminal_message,
    )


def _reconcile_audit_spool(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
    audit_spool: RunnerHostHygieneAuditSpool,
    audit_poster: AuditPoster,
    audit_delivery_sleeper: AuditDeliverySleeper,
    github_run_state_reader: GitHubRunStateReader | None,
) -> RunnerHostHygieneExecutorResult | None:
    for original_envelope in audit_spool.list_for(
        host_name=request.host_name,
        action=request.action,
    ):
        envelope, delivery_responses = _deliver_audit_envelope(
            envelope=original_envelope,
            audit_spool=audit_spool,
            audit_poster=audit_poster,
            audit_delivery_sleeper=audit_delivery_sleeper,
        )
        is_current_request = envelope.audit_record_key == request.audit_record_key
        if is_current_request and envelope.execution_state == "terminal_recorded":
            terminal_audit = envelope.terminal_audit
            assert terminal_audit is not None
            delivery_pending = envelope.terminal_delivery_state != "delivered"
            return RunnerHostHygieneExecutorResult(
                status=("audit_delivery_pending" if delivery_pending else terminal_audit.status),
                audit_record_key=request.audit_record_key,
                planned_response=delivery_responses.get("planned", {}),
                terminal_response=delivery_responses.get("terminal"),
                audit_delivery_pending=delivery_pending,
                reconciled=not delivery_pending,
                message=(
                    "runner host hygiene terminal audit delivery remains pending"
                    if delivery_pending
                    else "runner host hygiene terminal audit delivery reconciled; action not repeated"
                ),
            )
        if (
            is_current_request
            and envelope.execution_state == "action_started"
            and request.resolve_action_started
        ):
            post_report = _collect_terminal_report(
                request=request,
                remote_runner=remote_runner,
                github_run_state_reader=github_run_state_reader,
            )
            if post_report is None:
                terminal_message = (
                    "runner host hygiene previous execution stopped after action_started; "
                    "terminal evidence collection failed and the action was not repeated"
                )
            else:
                terminal_message = (
                    "runner host hygiene previous execution stopped after action_started; "
                    "current post evidence was captured and the action was not repeated"
                )
            terminal_audit = _terminal_audit(
                request=envelope.planned_audit.request,
                apply_plan=envelope.planned_audit.plan,
                pre_report=envelope.planned_audit.pre_apply_report,
                post_report=post_report,
                status="failed",
                message=terminal_message,
            )
            _assert_generated_cache_audit_redacted(audit=terminal_audit, request=request)
            terminal_response, delivery_pending = _deliver_terminal_audit(
                terminal_audit=terminal_audit,
                audit_envelope=envelope,
                audit_spool=audit_spool,
                audit_poster=audit_poster,
                audit_delivery_sleeper=audit_delivery_sleeper,
            )
            return RunnerHostHygieneExecutorResult(
                status="audit_delivery_pending" if delivery_pending else "failed",
                audit_record_key=request.audit_record_key,
                planned_response=delivery_responses.get("planned", {}),
                terminal_response=terminal_response,
                audit_delivery_pending=delivery_pending,
                reconciled=not delivery_pending,
                message=terminal_message,
            )
        if RunnerHostHygieneAuditSpool.is_unresolved(envelope):
            return RunnerHostHygieneExecutorResult(
                status="blocked",
                audit_record_key=request.audit_record_key,
                planned_response=delivery_responses.get("planned", {}),
                terminal_response=delivery_responses.get("terminal"),
                audit_delivery_pending=True,
                message=(
                    "runner host hygiene blocked by unresolved audit delivery or "
                    f"execution evidence for {envelope.audit_record_key}"
                ),
            )
        if is_current_request and envelope.execution_state == "blocked":
            return RunnerHostHygieneExecutorResult(
                status="blocked",
                audit_record_key=request.audit_record_key,
                planned_response=delivery_responses.get("planned", {}),
                message="runner host hygiene request was already recorded as blocked",
            )
    return None


def _generated_cache_apply_not_needed(apply_plan: RunnerHostHygieneApplyPlan) -> bool:
    if apply_plan.action != "prune_generated_run_cache" or not apply_plan.mutate:
        return False
    blocker_codes = {blocker.code for blocker in apply_plan.blockers}
    benign_codes = {
        "target_generated_cache_below_high_water",
        "target_generated_cache_cooldown_active",
        "target_generated_cache_runs_missing",
    }
    return "target_generated_cache_below_high_water" in blocker_codes and blocker_codes.issubset(
        benign_codes
    )


def _deliver_terminal_audit(
    *,
    terminal_audit: RunnerHostHygieneApplyAuditRecord,
    audit_envelope: RunnerHostHygieneAuditDeliveryEnvelope | None,
    audit_spool: RunnerHostHygieneAuditSpool | None,
    audit_poster: AuditPoster,
    audit_delivery_sleeper: AuditDeliverySleeper,
) -> tuple[dict[str, object] | None, bool]:
    idempotency_key = (
        f"runner-host-hygiene:{terminal_audit.audit_record_key}:{terminal_audit.status}"
    )
    if audit_envelope is None or audit_spool is None:
        return audit_poster(terminal_audit, idempotency_key), False
    audit_envelope = audit_spool.write(
        audit_envelope.model_copy(
            update={
                "execution_state": "terminal_recorded",
                "terminal_audit": terminal_audit,
                "terminal_idempotency_key": idempotency_key,
                "terminal_delivery_state": "pending",
            }
        )
    )
    audit_envelope, delivery_responses = _deliver_audit_envelope(
        envelope=audit_envelope,
        audit_spool=audit_spool,
        audit_poster=audit_poster,
        audit_delivery_sleeper=audit_delivery_sleeper,
    )
    return (
        delivery_responses.get("terminal"),
        audit_envelope.terminal_delivery_state != "delivered",
    )


def _deliver_audit_envelope(
    *,
    envelope: RunnerHostHygieneAuditDeliveryEnvelope,
    audit_spool: RunnerHostHygieneAuditSpool,
    audit_poster: AuditPoster,
    audit_delivery_sleeper: AuditDeliverySleeper,
) -> tuple[RunnerHostHygieneAuditDeliveryEnvelope, dict[str, dict[str, object]]]:
    responses: dict[str, dict[str, object]] = {}
    for audit, idempotency_key, phase in audit_spool.pending_delivery_phases(envelope):
        response, error_message, attempts, retryable_error = _post_audit_with_retry(
            audit=audit,
            idempotency_key=idempotency_key,
            audit_poster=audit_poster,
            sleeper=audit_delivery_sleeper,
        )
        updates: dict[str, object] = {
            "delivery_attempts": envelope.delivery_attempts + attempts,
            "last_error": error_message,
            "last_error_retryable": retryable_error,
        }
        if response is not None:
            responses[phase] = response
            updates[f"{phase}_delivery_state"] = "delivered"
        envelope = audit_spool.write(envelope.model_copy(update=updates))
        if response is None:
            break
    return envelope, responses


def _post_audit_with_retry(
    *,
    audit: RunnerHostHygieneApplyAuditRecord,
    idempotency_key: str,
    audit_poster: AuditPoster,
    sleeper: AuditDeliverySleeper,
    max_attempts: int = 3,
) -> tuple[dict[str, object] | None, str, int, bool | None]:
    last_error = ""
    for attempt in range(1, max_attempts + 1):
        try:
            return audit_poster(audit, idempotency_key), "", attempt, None
        except AuditDeliveryError as error:
            last_error = _redact_sensitive_text(error.message)
            if not error.retryable:
                return None, last_error, attempt, False
        except click.ClickException as error:
            return None, _redact_sensitive_text(error.message), attempt, False
        except OSError as error:
            last_error = _redact_sensitive_text(str(error))
        if attempt < max_attempts:
            sleeper(float(2 ** (attempt - 1)))
    return (
        None,
        last_error or "runner host hygiene audit delivery failed",
        max_attempts,
        True,
    )


def _execute_apply_action(
    *, request: RunnerHostHygieneExecutorRequest, remote_runner: RemoteCommandRunner
) -> RemoteCommandResult:
    if request.action == "prune_generated_run_cache":
        root = _target_generated_run_cache_root(request)
        if root is None or not request.target_generated_cache_run_ids:
            return RemoteCommandResult(
                returncode=1,
                stderr="generated cache action lacks an exact configured target",
            )
        return remote_runner(
            (
                "flock",
                "-n",
                HOST_LOCK_PATH,
                "/usr/bin/sudo",
                "-n",
                _GENERATED_RUN_CACHE_HELPER,
                "prune",
                f"{root.key}={root.path}",
                str(request.idle_observation_count),
                str(request.idle_observation_interval_seconds),
                ",".join(str(run_id) for run_id in request.target_generated_cache_run_ids),
            ),
            request.timeout_seconds,
        )
    if request.action == "remove_buildkit_state_volumes":
        target_volume = request.target_buildkit_state_volumes[0]
        return remote_runner(
            (
                "flock",
                "-n",
                HOST_LOCK_PATH,
                "docker",
                "volume",
                "rm",
                target_volume,
            ),
            request.timeout_seconds,
        )
    if request.action == "prune_dangling_images":
        return remote_runner(
            (
                "flock",
                "-n",
                HOST_LOCK_PATH,
                "docker",
                "image",
                "prune",
                "--force",
                "--filter",
                f"until={request.prune_until}",
            ),
            request.timeout_seconds,
        )
    if request.target_buildkit_builder:
        buildkit_container = f"buildx_buildkit_{request.target_buildkit_builder}0"
        container_state = remote_runner(
            ("docker", "inspect", "--format", "{{.State.Running}}", buildkit_container),
            request.timeout_seconds,
        )
        if container_state.returncode != 0 or container_state.stdout.strip() != "true":
            return RemoteCommandResult(
                returncode=1,
                stderr="allowlisted BuildKit cache container is not running",
            )
        buildctl_help = remote_runner(
            ("docker", "exec", buildkit_container, "buildctl", "prune", "--help"),
            request.timeout_seconds,
        )
        if (
            buildctl_help.returncode != 0
            or "--keep-duration" not in buildctl_help.stdout
            or "--keep-storage" not in buildctl_help.stdout
        ):
            return RemoteCommandResult(
                returncode=1,
                stderr="BuildKit does not support bounded duration and storage pruning",
            )
        keep_storage_mb = (
            request.max_used_space_bytes + _BUILDCTL_STORAGE_MB_BYTES - 1
        ) // _BUILDCTL_STORAGE_MB_BYTES
        return remote_runner(
            (
                "flock",
                "-n",
                HOST_LOCK_PATH,
                "docker",
                "exec",
                buildkit_container,
                "buildctl",
                "prune",
                "--all",
                "--keep-duration",
                request.prune_until,
                "--keep-storage",
                str(keep_storage_mb),
            ),
            request.timeout_seconds,
        )
    if request.max_used_space_bytes:
        prune_url = (
            "http://localhost/v1.45/build/prune?all=true&keep-storage="
            f"{request.max_used_space_bytes}"
        )
    else:
        prune_filters = quote(
            json.dumps({"until": [request.prune_until]}, separators=(",", ":")),
            safe="",
        )
        prune_url = f"http://localhost/v1.45/build/prune?all=true&filters={prune_filters}"
    return remote_runner(
        (
            "flock",
            "-n",
            HOST_LOCK_PATH,
            "curl",
            "--silent",
            "--show-error",
            "--fail",
            "--request",
            "POST",
            "--unix-socket",
            "/var/run/docker.sock",
            prune_url,
        ),
        request.timeout_seconds,
    )


def collect_runner_host_hygiene_report(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
    github_run_state_reader: GitHubRunStateReader | None = None,
) -> RunnerHostHygieneReport:
    df_result = _require_remote_success(
        remote_runner(("df", "-B1", "-P", "/"), request.timeout_seconds),
        evidence_name="df",
    )
    docker_disk_usage = _collect_docker_disk_usage(
        request=request,
        remote_runner=remote_runner,
    )
    warm_builders = tuple(
        builder
        for builder in request.retained_warm_builders
        if remote_runner(
            ("docker", "image", "inspect", builder), request.timeout_seconds
        ).returncode
        == 0
    )
    image_inventory = _collect_image_inventory(
        request=request,
        remote_runner=remote_runner,
    )
    container_inventory = _collect_container_inventory(
        request=request,
        remote_runner=remote_runner,
    )
    volume_inventory = docker_disk_usage.volume_inventory
    docker_reclaimable_breakdown = docker_disk_usage.reclaimable_breakdown
    runner_workdir_usage = _collect_runner_workdir_usage(
        request=request,
        remote_runner=remote_runner,
    )
    generated_cache_usage = _collect_generated_run_cache_usage(
        request=request,
        remote_runner=remote_runner,
        github_run_state_reader=github_run_state_reader,
    )
    observation = RunnerHostHygieneObservation(
        host_name=request.host_name,
        observed_at=utc_now_timestamp(),
        free_disk_bytes=_parse_df_available_bytes(df_result.stdout),
        docker_reclaimable_bytes=docker_reclaimable_breakdown.total_bytes,
        docker_reclaimable_breakdown=docker_reclaimable_breakdown,
        runner_workdir_bytes=sum(item.apparent_bytes for item in runner_workdir_usage),
        runner_workdir_allocated_bytes=sum(item.allocated_bytes for item in runner_workdir_usage),
        runner_workdir_usage=runner_workdir_usage,
        generated_cache_apparent_bytes=sum(item.apparent_bytes for item in generated_cache_usage),
        generated_cache_allocated_bytes=sum(item.allocated_bytes for item in generated_cache_usage),
        generated_cache_usage=generated_cache_usage,
        docker_toolchain=_collect_docker_toolchain(
            request=request,
            remote_runner=remote_runner,
        ),
        warm_builders=warm_builders,
        image_inventory=image_inventory,
        volume_inventory=volume_inventory,
        orphan_buildkit_containers=_count_orphan_buildkit_containers(container_inventory),
        orphan_buildkit_volumes=_count_orphan_buildkit_volumes(volume_inventory),
        notes=(
            f"execution_lane={request.execution_lane}",
            f"service_user={request.service_user}",
            f"repository_scope={request.repository_scope}",
            "docker_summary=engine_api_v1.45",
        ),
    )
    return evaluate_runner_host_hygiene(
        policy=RunnerHostHygienePolicy(
            minimum_free_disk_bytes=request.minimum_free_disk_bytes,
            generated_cache_budgets=tuple(
                RunnerHostHygieneGeneratedCacheBudget(
                    cache_key=root.key,
                    high_water_bytes=root.high_water_bytes,
                )
                for root in request.generated_run_cache_roots
            ),
            required_warm_builders=request.retained_warm_builders,
        ),
        observation=observation,
    )


def build_local_command_runner() -> RemoteCommandRunner:
    def run(command_args: Sequence[str], timeout_seconds: int) -> RemoteCommandResult:
        bounded_timeout = max(timeout_seconds, 1)
        try:
            completed = subprocess.run(
                tuple(command_args),
                capture_output=True,
                text=True,
                timeout=bounded_timeout,
            )
        except subprocess.TimeoutExpired as error:
            stdout = _timeout_output_text(error.stdout)
            stderr = _timeout_output_text(error.stderr)
            detail = stderr or f"command timed out after {bounded_timeout} seconds"
            return RemoteCommandResult(returncode=124, stdout=stdout, stderr=detail)
        return RemoteCommandResult(
            returncode=completed.returncode,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )

    return run


def _timeout_output_text(output: str | bytes | bytearray | memoryview | None) -> str:
    if output is None:
        return ""
    if isinstance(output, bytes):
        return output.decode(errors="replace")
    if isinstance(output, bytearray):
        return bytes(output).decode(errors="replace")
    if isinstance(output, memoryview):
        return output.tobytes().decode(errors="replace")
    return output


def validate_local_executor_environment(
    *,
    request: RunnerHostHygieneExecutorRequest,
    env: Mapping[str, str] | None = None,
    current_user: str | None = None,
    current_host: str | None = None,
) -> None:
    resolved_env = os.environ if env is None else env
    resolved_current_user = current_user or pwd.getpwuid(os.geteuid()).pw_name
    resolved_current_host = current_host or socket.gethostname()
    if resolved_current_user != request.service_user:
        raise click.ClickException(
            "Runner host hygiene executor user must match the approved service user."
        )
    if (
        resolved_current_host.strip().lower().split(".", maxsplit=1)[0]
        != (request.host_name.lower().split(".", maxsplit=1)[0])
    ):
        raise click.ClickException(
            "Runner host hygiene executor host must match the approved host."
        )
    github_repository = resolved_env.get("GITHUB_REPOSITORY", "").strip()
    if request.action == "prune_generated_run_cache" and request.mutate and not github_repository:
        raise click.ClickException(
            "Generated run cache mutation requires GITHUB_REPOSITORY scope evidence."
        )
    if github_repository and github_repository != request.repository_scope:
        raise click.ClickException(
            "Runner host hygiene repository scope must match GITHUB_REPOSITORY."
        )


def build_service_audit_poster(*, service_url: str, bearer_token: str) -> AuditPoster:
    normalized_bearer_token = bearer_token.strip()
    if not normalized_bearer_token:
        raise click.ClickException("runner host hygiene executor requires bearer token")
    return build_refreshing_service_audit_poster(
        service_url=service_url,
        bearer_token_provider=lambda: normalized_bearer_token,
    )


def build_github_run_state_reader(
    *,
    bearer_token: str,
    api_base_url: str = "https://api.github.com",
) -> GitHubRunStateReader:
    normalized_token = bearer_token.strip()
    normalized_api_base_url = api_base_url.strip().rstrip("/")
    if not normalized_api_base_url:
        raise click.ClickException("generated cache GitHub run evidence requires API base URL")

    def read(repository: str, run_ids: tuple[int, ...]) -> Mapping[int, str]:
        owner, name = repository.split("/", maxsplit=1)
        states: dict[int, str] = {}
        for run_id in run_ids:
            headers = {
                "Accept": "application/vnd.github+json",
                "User-Agent": "launchplane-runner-host-hygiene",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            if normalized_token:
                headers["Authorization"] = f"Bearer {normalized_token}"
            request = Request(
                (
                    f"{normalized_api_base_url}/repos/{quote(owner, safe='')}"
                    f"/{quote(name, safe='')}/actions/runs/{run_id}"
                ),
                headers=headers,
                method="GET",
            )
            try:
                with urlopen(request, timeout=30) as response:
                    payload = json.loads(response.read().decode("utf-8"))
            except (HTTPError, URLError, TimeoutError, socket.timeout, json.JSONDecodeError):
                states[run_id] = "unknown"
                continue
            if not isinstance(payload, dict):
                states[run_id] = "unknown"
                continue
            states[run_id] = "completed" if payload.get("status") == "completed" else "active"
        return states

    return read


def build_refreshing_service_audit_poster(
    *, service_url: str, bearer_token_provider: BearerTokenProvider
) -> AuditPoster:
    normalized_service_url = service_url.strip().rstrip("/")
    if not normalized_service_url:
        raise click.ClickException("runner host hygiene executor requires service_url")

    def post(audit: RunnerHostHygieneApplyAuditRecord, idempotency_key: str) -> dict[str, object]:
        bearer_token = bearer_token_provider().strip()
        if not bearer_token:
            raise click.ClickException("runner host hygiene executor requires bearer token")
        body = json.dumps(_audit_route_payload(audit)).encode()
        request = Request(
            f"{normalized_service_url}{AUDIT_ROUTE_PATH}",
            data=body,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {bearer_token}",
                "Content-Type": "application/json",
                "Idempotency-Key": idempotency_key,
            },
            method="POST",
        )
        try:
            with urlopen(request, timeout=30) as response:
                response_payload = json.loads(response.read().decode("utf-8"))
        except HTTPError as error:
            response_text = error.read().decode(errors="replace")
            message = _redact_sensitive_text(
                response_text.strip() or f"Launchplane service returned HTTP {error.code}."
            )
            raise AuditDeliveryError(
                message,
                retryable=error.code in {429, 500, 502, 503, 504},
            ) from error
        except (URLError, TimeoutError, socket.timeout) as error:
            raise AuditDeliveryError(
                _redact_sensitive_text(str(error) or "Launchplane service request failed"),
                retryable=True,
            ) from error
        if not isinstance(response_payload, dict):
            raise AuditDeliveryError(
                "Launchplane service returned a non-object response.",
                retryable=False,
            )
        return response_payload

    return post


def _audit_route_payload(audit: RunnerHostHygieneApplyAuditRecord) -> dict[str, object]:
    return {
        "schema_version": 1,
        "product": "launchplane",
        "audit": audit.model_dump(mode="json"),
    }


def _required_env(env: Mapping[str, str], name: str) -> str:
    value = env.get(name, "").strip()
    if not value:
        raise click.ClickException(f"Missing required env var: {name}")
    return value


def _require_remote_success(
    result: RemoteCommandResult, *, evidence_name: str
) -> RemoteCommandResult:
    if result.returncode == 0:
        return result
    detail = result.stderr.strip() or result.stdout.strip() or "unknown error"
    raise click.ClickException(f"runner host hygiene {evidence_name} evidence failed: {detail}")


def _parse_df_available_bytes(output: str) -> int:
    lines = [line.split() for line in output.splitlines() if line.strip()]
    if len(lines) < 2 or len(lines[1]) < 4:
        raise click.ClickException(
            "runner host hygiene df evidence did not include available bytes"
        )
    available = lines[1][3]
    if not available.isdigit():
        raise click.ClickException("runner host hygiene df available bytes was not numeric")
    return int(available)


def _collect_docker_toolchain(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> RunnerHostDockerToolchainObservation | None:
    buildx_plugin_path = _first_existing_path(
        remote_runner(
            (
                "sh",
                "-c",
                DOCKER_BUILDX_PLUGIN_PATH_COMMAND,
            ),
            request.timeout_seconds,
        ).stdout
    )
    toolchain = RunnerHostDockerToolchainObservation(
        docker_engine_version=_command_output(
            remote_runner,
            ("docker", "version", "--format", "{{.Server.Version}}"),
            request.timeout_seconds,
        ),
        docker_cli_version=_command_output(
            remote_runner,
            ("docker", "version", "--format", "{{.Client.Version}}"),
            request.timeout_seconds,
        ),
        docker_buildx_version=_first_version(
            _command_output(
                remote_runner,
                ("docker", "buildx", "version"),
                request.timeout_seconds,
            )
        ),
        docker_buildx_plugin_path=buildx_plugin_path,
        docker_buildx_package=_docker_buildx_package(
            remote_runner=remote_runner,
            timeout_seconds=request.timeout_seconds,
        ),
        docker_buildx_source=_docker_buildx_source(buildx_plugin_path),
        buildkit_version=_first_version(
            _command_output(
                remote_runner,
                ("docker", "buildx", "inspect"),
                request.timeout_seconds,
            )
        ),
    )
    return toolchain if toolchain.has_evidence() else None


def _command_output(
    remote_runner: RemoteCommandRunner,
    command_args: tuple[str, ...],
    timeout_seconds: int,
) -> str:
    result = remote_runner(command_args, timeout_seconds)
    if result.returncode != 0:
        return ""
    return result.stdout.strip()


def _first_existing_path(output: str) -> str:
    for line in output.splitlines():
        value = line.strip()
        if value:
            return value
    return ""


def _docker_buildx_package(*, remote_runner: RemoteCommandRunner, timeout_seconds: int) -> str:
    package = _command_output(
        remote_runner,
        ("sh", "-c", "dpkg-query -W -f='${Package} ${Version}' docker-buildx 2>/dev/null"),
        timeout_seconds,
    )
    if package:
        return package
    return _command_output(
        remote_runner,
        ("sh", "-c", "rpm -q docker-buildx 2>/dev/null"),
        timeout_seconds,
    )


def _docker_buildx_source(plugin_path: str) -> str:
    if plugin_path.startswith("/usr/libexec/") or plugin_path.startswith("/usr/lib/"):
        return "system package"
    if plugin_path.startswith("/usr/local/"):
        return "managed plugin"
    if "/.docker/cli-plugins/" in plugin_path:
        return "user plugin"
    return ""


def _first_version(value: str) -> str:
    for token in value.replace(",", " ").split():
        normalized = token.strip().removeprefix("v")
        if normalized and normalized[0].isdigit() and "." in normalized:
            return normalized
    return ""


def _collect_image_inventory(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> tuple[RunnerHostHygieneImageInventoryItem, ...]:
    image_result = _require_remote_success(
        remote_runner(
            (
                "docker",
                "image",
                "ls",
                "--all",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ),
            request.timeout_seconds,
        ),
        evidence_name="image_inventory",
    )
    container_images = _collect_container_image_references(
        request=request,
        remote_runner=remote_runner,
    )
    inventory: list[RunnerHostHygieneImageInventoryItem] = []
    retained_builders = set(request.retained_warm_builders)
    for row in _parse_json_lines(image_result.stdout, evidence_name="image_inventory"):
        image_id = _docker_json_text(row, "ID").removeprefix("sha256:")
        repository = _docker_json_text(row, "Repository").lower()
        tag = _docker_json_text(row, "Tag").lower()
        if not image_id:
            raise click.ClickException(
                "runner host hygiene image inventory evidence did not include image IDs"
            )
        reference = _image_reference(repository=repository, tag=tag)
        inventory.append(
            RunnerHostHygieneImageInventoryItem(
                image_id=image_id,
                repository=repository,
                tag=tag,
                size_bytes=_parse_optional_docker_size_bytes(_docker_json_text(row, "Size")),
                created_at=_docker_json_text(row, "CreatedAt"),
                dangling=_is_dangling_image(repository=repository, tag=tag),
                in_use=reference in container_images or image_id in container_images,
                is_warm_builder=_is_warm_builder_image(
                    repository=repository,
                    reference=reference,
                    retained_builders=retained_builders,
                ),
            )
        )
    return tuple(inventory)


def _collect_container_inventory(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> tuple[dict[str, object], ...]:
    result = _require_remote_success(
        remote_runner(
            (
                "docker",
                "ps",
                "--all",
                "--no-trunc",
                "--format",
                "{{json .}}",
            ),
            request.timeout_seconds,
        ),
        evidence_name="container_inventory",
    )
    return _parse_json_lines(result.stdout, evidence_name="container_inventory")


def _collect_container_image_references(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> set[str]:
    return _container_image_references(
        _collect_container_inventory(request=request, remote_runner=remote_runner)
    )


def _container_image_references(rows: tuple[dict[str, object], ...]) -> set[str]:
    references: set[str] = set()
    for row in rows:
        image = _docker_json_text(row, "Image").lower()
        image_id = _docker_json_text(row, "ImageID").removeprefix("sha256:").lower()
        if image:
            references.add(image)
        if image_id:
            references.add(image_id)
    return references


def _count_orphan_buildkit_containers(rows: tuple[dict[str, object], ...]) -> int:
    return sum(1 for row in rows if _is_orphan_buildkit_container(row))


def _is_orphan_buildkit_container(row: Mapping[str, object]) -> bool:
    name = _docker_json_text(row, "Names").lower()
    image = _docker_json_text(row, "Image").lower()
    state = _docker_json_text(row, "State").lower()
    status = _docker_json_text(row, "Status").lower()
    is_buildkit = name.startswith("buildx_buildkit_") or "buildkit" in image
    if not is_buildkit:
        return False
    if state:
        return state not in {"running", "restarting"}
    return not status.startswith(("up ", "restarting"))


def _count_orphan_buildkit_volumes(
    volume_inventory: tuple[RunnerHostHygieneVolumeInventoryItem, ...],
) -> int:
    return sum(
        1
        for volume in volume_inventory
        if _is_buildkit_state_volume_name(volume.name)
        and (volume.dangling or volume.referenced_by_containers == 0)
    )


def _collect_runner_workdir_usage(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> tuple[RunnerHostHygieneRunnerWorkdirUsage, ...]:
    usage: list[RunnerHostHygieneRunnerWorkdirUsage] = []
    for root in request.runner_workdir_roots:
        result = remote_runner(
            (
                "/usr/bin/sudo",
                "-n",
                _RUNNER_WORKDIR_USAGE_HELPER,
                f"{root.key}={root.path}",
            ),
            request.timeout_seconds,
        )
        if result.returncode != 0:
            raise click.ClickException(
                f"runner host hygiene runner_workdir_bytes:{root.key} evidence failed"
            )
        lines = tuple(line.strip() for line in result.stdout.splitlines() if line.strip())
        if len(lines) not in {2, 3}:
            raise click.ClickException("runner host hygiene runner workdir evidence was incomplete")
        raw_measurement_status = lines[2] if len(lines) == 3 else "complete"
        measurement_status: Literal["complete", "partial"]
        if raw_measurement_status == "complete":
            measurement_status = "complete"
        elif raw_measurement_status == "partial":
            measurement_status = "partial"
        else:
            raise click.ClickException(
                "runner host hygiene runner workdir evidence had an invalid status"
            )
        usage.append(
            RunnerHostHygieneRunnerWorkdirUsage(
                root_key=root.key,
                apparent_bytes=_parse_non_negative_int_evidence(
                    lines[0], evidence_name=f"runner_workdir_apparent_bytes:{root.key}"
                ),
                allocated_bytes=_parse_non_negative_int_evidence(
                    lines[1], evidence_name=f"runner_workdir_allocated_bytes:{root.key}"
                ),
                measurement_status=measurement_status,
            )
        )
    return tuple(usage)


def _collect_generated_run_cache_usage(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
    github_run_state_reader: GitHubRunStateReader | None,
) -> tuple[RunnerHostHygieneGeneratedCacheUsage, ...]:
    usage: list[RunnerHostHygieneGeneratedCacheUsage] = []
    effective_github_run_state_reader = (
        github_run_state_reader
        if request.action == "prune_generated_run_cache" or not request.mutate
        else None
    )
    for root in request.generated_run_cache_roots:
        result = remote_runner(
            (
                "/usr/bin/sudo",
                "-n",
                _GENERATED_RUN_CACHE_HELPER,
                "observe",
                f"{root.key}={root.path}",
                str(request.idle_observation_count),
                str(request.idle_observation_interval_seconds),
            ),
            request.timeout_seconds,
        )
        if result.returncode != 0:
            usage.append(
                RunnerHostHygieneGeneratedCacheUsage(
                    cache_key=root.key,
                    measurement_status="partial",
                )
            )
            continue
        try:
            usage.append(
                _parse_generated_run_cache_usage(
                    output=result.stdout,
                    root=root,
                    github_run_state_reader=effective_github_run_state_reader,
                )
            )
        except (click.ClickException, ValueError):
            usage.append(
                RunnerHostHygieneGeneratedCacheUsage(
                    cache_key=root.key,
                    measurement_status="partial",
                )
            )
    return tuple(usage)


def _parse_generated_run_cache_usage(
    *,
    output: str,
    root: GeneratedRunCacheRoot,
    github_run_state_reader: GitHubRunStateReader | None,
) -> RunnerHostHygieneGeneratedCacheUsage:
    rows = _parse_json_lines(output, evidence_name=f"generated_cache:{root.key}")
    summary_rows = tuple(row for row in rows if row.get("record_type") == "summary")
    entry_rows = tuple(row for row in rows if row.get("record_type") == "entry")
    if len(summary_rows) != 1:
        raise click.ClickException("runner host generated cache evidence requires one summary")
    summary = summary_rows[0]
    if _docker_json_text(summary, "cache_key") != root.key:
        raise click.ClickException("runner host generated cache evidence key mismatch")
    configured_policy = (
        ("minimum_age_hours", root.minimum_age_hours),
        ("high_water_bytes", root.high_water_bytes),
        ("low_water_bytes", root.low_water_bytes),
        ("cooldown_hours", root.cooldown_hours),
    )
    for field_name, expected_value in configured_policy:
        if (
            _strict_non_negative_int(
                summary.get(field_name),
                evidence_name=f"generated cache {field_name}",
            )
            != expected_value
        ):
            raise click.ClickException(
                f"runner host generated cache {field_name} does not match root-owned policy"
            )
    run_ids = tuple(
        sorted(
            {
                _strict_positive_int(row.get("run_id"), evidence_name="generated cache run id")
                for row in entry_rows
            }
        )
    )
    github_states = (
        github_run_state_reader(root.source_repository, run_ids)
        if github_run_state_reader is not None and run_ids
        else {}
    )
    entries = tuple(
        RunnerHostHygieneGeneratedCacheEntry(
            run_id=_strict_positive_int(row.get("run_id"), evidence_name="generated cache run id"),
            apparent_bytes=_strict_non_negative_int(
                row.get("apparent_bytes"), evidence_name="generated cache apparent bytes"
            ),
            allocated_bytes=_strict_non_negative_int(
                row.get("allocated_bytes"), evidence_name="generated cache allocated bytes"
            ),
            age_seconds=_strict_non_negative_int(
                row.get("age_seconds"), evidence_name="generated cache age seconds"
            ),
            open_handle_observations=_strict_non_negative_int_tuple(
                row.get("open_handle_observations"),
                evidence_name="generated cache open-handle observations",
            ),
            github_run_state=_github_run_state(github_states.get(run_id)),
        )
        for row in entry_rows
        for run_id in (
            _strict_positive_int(row.get("run_id"), evidence_name="generated cache run id"),
        )
    )
    observed_epoch_seconds = _strict_non_negative_int(
        summary.get("observed_epoch_seconds"),
        evidence_name="generated cache observed epoch",
    )
    last_cleanup_epoch_seconds = _strict_non_negative_int(
        summary.get("last_cleanup_epoch_seconds", 0),
        evidence_name="generated cache last cleanup epoch",
    )
    last_post_cleanup_allocated_bytes = _strict_non_negative_int(
        summary.get("last_post_cleanup_allocated_bytes", 0),
        evidence_name="generated cache last post-cleanup bytes",
    )
    allocated_bytes = _strict_non_negative_int(
        summary.get("allocated_bytes"),
        evidence_name="generated cache allocated bytes",
    )
    cooldown_remaining_seconds = max(
        last_cleanup_epoch_seconds + root.cooldown_hours * 3600 - observed_epoch_seconds,
        0,
    )
    elapsed_since_cleanup = max(observed_epoch_seconds - last_cleanup_epoch_seconds, 0)
    estimated_daily_growth_bytes = 0
    if (
        last_cleanup_epoch_seconds > 0
        and elapsed_since_cleanup > 0
        and allocated_bytes > last_post_cleanup_allocated_bytes
    ):
        estimated_daily_growth_bytes = (
            (allocated_bytes - last_post_cleanup_allocated_bytes) * 86_400 // elapsed_since_cleanup
        )
    return RunnerHostHygieneGeneratedCacheUsage(
        cache_key=root.key,
        apparent_bytes=_strict_non_negative_int(
            summary.get("apparent_bytes"),
            evidence_name="generated cache apparent bytes",
        ),
        allocated_bytes=allocated_bytes,
        entry_count=_strict_non_negative_int(
            summary.get("entry_count"), evidence_name="generated cache entry count"
        ),
        measurement_status=_generated_cache_measurement_status(summary.get("measurement_status")),
        owner_valid=_strict_bool(summary.get("owner_valid"), evidence_name="owner_valid"),
        mode_valid=_strict_bool(summary.get("mode_valid"), evidence_name="mode_valid"),
        symlink_safe=_strict_bool(summary.get("symlink_safe"), evidence_name="symlink_safe"),
        owner_worker_observations=_strict_non_negative_int_tuple(
            summary.get("owner_worker_observations"),
            evidence_name="generated cache owner-worker observations",
        ),
        age_buckets=_generated_cache_age_buckets(entries),
        entries=entries,
        entries_truncated=_strict_bool(
            summary.get("entries_truncated"), evidence_name="entries_truncated"
        ),
        last_cleanup_epoch_seconds=last_cleanup_epoch_seconds,
        last_reclaimed_bytes=_strict_non_negative_int(
            summary.get("last_reclaimed_bytes", 0),
            evidence_name="generated cache last reclaimed bytes",
        ),
        last_post_cleanup_allocated_bytes=last_post_cleanup_allocated_bytes,
        last_removed_run_ids=_strict_non_negative_int_tuple(
            summary.get("last_removed_run_ids"),
            evidence_name="generated cache last removed run ids",
        ),
        estimated_daily_growth_bytes=estimated_daily_growth_bytes,
        cooldown_remaining_seconds=cooldown_remaining_seconds,
    )


def _generated_cache_age_buckets(
    entries: tuple[RunnerHostHygieneGeneratedCacheEntry, ...],
) -> tuple[RunnerHostHygieneGeneratedCacheAgeBucket, ...]:
    bucket_names = (
        "under_1h",
        "1h_to_24h",
        "1d_to_7d",
        "7d_to_30d",
        "30d_or_more",
    )
    bucket_values: dict[str, list[int]] = {name: [0, 0, 0] for name in bucket_names}
    for entry in entries:
        if entry.age_seconds < 3600:
            bucket = "under_1h"
        elif entry.age_seconds < 86_400:
            bucket = "1h_to_24h"
        elif entry.age_seconds < 7 * 86_400:
            bucket = "1d_to_7d"
        elif entry.age_seconds < 30 * 86_400:
            bucket = "7d_to_30d"
        else:
            bucket = "30d_or_more"
        bucket_values[bucket][0] += 1
        bucket_values[bucket][1] += entry.apparent_bytes
        bucket_values[bucket][2] += entry.allocated_bytes
    return tuple(
        RunnerHostHygieneGeneratedCacheAgeBucket(
            bucket=cast(
                Literal[
                    "under_1h",
                    "1h_to_24h",
                    "1d_to_7d",
                    "7d_to_30d",
                    "30d_or_more",
                ],
                bucket_name,
            ),
            entry_count=bucket_values[bucket_name][0],
            apparent_bytes=bucket_values[bucket_name][1],
            allocated_bytes=bucket_values[bucket_name][2],
        )
        for bucket_name in bucket_names
    )


def _select_generated_cache_run_ids(
    *,
    request: RunnerHostHygieneExecutorRequest,
    report: RunnerHostHygieneReport,
) -> tuple[int, ...]:
    if request.action != "prune_generated_run_cache":
        return ()
    root = _target_generated_run_cache_root(request)
    if root is None:
        return ()
    usage = next(
        (
            item
            for item in report.generated_cache_usage
            if item.cache_key == request.target_generated_cache_key
        ),
        None,
    )
    if (
        usage is None
        or usage.allocated_bytes <= root.high_water_bytes
        or usage.cooldown_remaining_seconds > 0
        or usage.measurement_status != "complete"
        or usage.entries_truncated
        or not (usage.owner_valid and usage.mode_valid and usage.symlink_safe)
        or len(usage.owner_worker_observations) < request.idle_observation_count
        or any(value > 0 for value in usage.owner_worker_observations)
    ):
        return ()
    projected_allocated_bytes = usage.allocated_bytes
    selected: list[int] = []
    for entry in sorted(usage.entries, key=lambda item: (-item.age_seconds, item.run_id)):
        if projected_allocated_bytes <= root.low_water_bytes:
            break
        if (
            entry.github_run_state != "completed"
            or entry.age_seconds < root.minimum_age_hours * 3600
            or len(entry.open_handle_observations) < request.idle_observation_count
            or any(value > 0 for value in entry.open_handle_observations)
        ):
            continue
        selected.append(entry.run_id)
        projected_allocated_bytes = max(
            projected_allocated_bytes - entry.allocated_bytes,
            0,
        )
    if projected_allocated_bytes > root.low_water_bytes:
        return ()
    return tuple(sorted(selected))


def _target_generated_run_cache_root(
    request: RunnerHostHygieneExecutorRequest,
) -> GeneratedRunCacheRoot | None:
    if not request.target_generated_cache_key:
        return None
    return next(
        (
            root
            for root in request.generated_run_cache_roots
            if root.key == request.target_generated_cache_key
        ),
        None,
    )


def _generated_cache_measurement_status(value: object) -> Literal["complete", "partial"]:
    if value == "complete":
        return "complete"
    if value == "partial":
        return "partial"
    raise click.ClickException("runner host generated cache measurement status was invalid")


def _github_run_state(value: object) -> Literal["completed", "active", "unknown"]:
    if value == "completed":
        return "completed"
    if value == "active":
        return "active"
    return "unknown"


def _strict_bool(value: object, *, evidence_name: str) -> bool:
    if isinstance(value, bool):
        return value
    raise click.ClickException(f"runner host hygiene {evidence_name} evidence was not boolean")


def _strict_positive_int(value: object, *, evidence_name: str) -> int:
    parsed = _strict_non_negative_int(value, evidence_name=evidence_name)
    if parsed <= 0:
        raise click.ClickException(f"runner host hygiene {evidence_name} was not positive")
    return parsed


def _strict_non_negative_int(value: object, *, evidence_name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise click.ClickException(
            f"runner host hygiene {evidence_name} evidence was not a non-negative integer"
        )
    return value


def _strict_non_negative_int_tuple(
    value: object,
    *,
    evidence_name: str,
) -> tuple[int, ...]:
    if not isinstance(value, list):
        raise click.ClickException(f"runner host hygiene {evidence_name} was not a list")
    return tuple(_strict_non_negative_int(item, evidence_name=evidence_name) for item in value)


def _is_buildkit_state_volume_name(value: str) -> bool:
    return value.startswith("buildx_buildkit_") and value.endswith("_state")


def _parse_non_negative_int_evidence(output: str, *, evidence_name: str) -> int:
    stripped_output = output.strip()
    if not stripped_output:
        raise click.ClickException(f"runner host hygiene {evidence_name} evidence was empty")
    first_line = stripped_output.splitlines()[0].strip()
    try:
        value = int(first_line)
    except ValueError as error:
        raise click.ClickException(
            f"runner host hygiene {evidence_name} evidence was not an integer"
        ) from error
    if value < 0:
        raise click.ClickException(f"runner host hygiene {evidence_name} evidence was negative")
    return value


def _collect_docker_disk_usage(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
) -> _DockerDiskUsageSnapshot:
    result = _require_remote_success(
        remote_runner(_DOCKER_DISK_USAGE_COMMAND, request.timeout_seconds),
        evidence_name="docker_summary",
    )
    return _parse_docker_disk_usage(result.stdout)


def _parse_docker_disk_usage(output: str) -> _DockerDiskUsageSnapshot:
    try:
        payload = json.loads(output)
    except json.JSONDecodeError as error:
        raise click.ClickException(
            "runner host hygiene Docker disk usage evidence was not valid JSON"
        ) from error
    if not isinstance(payload, dict):
        raise click.ClickException(
            "runner host hygiene Docker disk usage evidence was not a JSON object"
        )

    image_reclaimable = 0
    for row in _docker_disk_usage_rows(payload, "Images"):
        containers = _optional_int(row.get("Containers"))
        size = _optional_int(row.get("Size"))
        shared_size = _optional_int(row.get("SharedSize"))
        if containers == 0 and size is not None and shared_size is not None:
            image_reclaimable += max(size - shared_size, 0)

    container_total = 0
    container_used = 0
    for row in _docker_disk_usage_rows(payload, "Containers"):
        size = max(_optional_int(row.get("SizeRw")) or 0, 0)
        container_total += size
        if _docker_json_text(row, "State").lower() in {"running", "paused", "restarting"}:
            container_used += size

    volume_total = 0
    volume_used = 0
    volume_inventory: list[RunnerHostHygieneVolumeInventoryItem] = []
    for row in _docker_disk_usage_rows(payload, "Volumes"):
        usage_data = row.get("UsageData")
        if not isinstance(usage_data, dict):
            usage_data = {}
        ref_count = _non_negative_int(usage_data.get("RefCount"))
        size = _non_negative_int(usage_data.get("Size"))
        if size > 0:
            volume_total += size
            if ref_count > 0:
                volume_used += size
        volume_inventory.append(
            RunnerHostHygieneVolumeInventoryItem(
                name=_docker_json_text(row, "Name"),
                driver=_docker_json_text(row, "Driver"),
                mountpoint=_docker_json_text(row, "Mountpoint"),
                labels=_docker_labels(row.get("Labels")),
                size_bytes=size,
                referenced_by_containers=ref_count,
                dangling=ref_count == 0,
            )
        )

    build_cache_total = 0
    build_cache_used = 0
    for row in _docker_disk_usage_rows(payload, "BuildCache"):
        if row.get("Shared") is True:
            continue
        size = max(_optional_int(row.get("Size")) or 0, 0)
        build_cache_total += size
        if row.get("InUse") is True:
            build_cache_used += size

    return _DockerDiskUsageSnapshot(
        reclaimable_breakdown=RunnerHostHygieneDockerReclaimableBreakdown(
            images_bytes=image_reclaimable,
            containers_bytes=max(container_total - container_used, 0),
            local_volumes_bytes=max(volume_total - volume_used, 0),
            build_cache_bytes=max(build_cache_total - build_cache_used, 0),
        ),
        volume_inventory=tuple(volume_inventory),
    )


def _docker_disk_usage_rows(
    payload: Mapping[str, object], key: str
) -> tuple[dict[str, object], ...]:
    value = payload.get(key)
    if value is None:
        return ()
    if not isinstance(value, list) or any(not isinstance(row, dict) for row in value):
        raise click.ClickException(
            f"runner host hygiene Docker disk usage {key} evidence was malformed"
        )
    return tuple(value)


def _optional_int(value: object) -> int | None:
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value


def _parse_json_lines(output: str, *, evidence_name: str) -> tuple[dict[str, object], ...]:
    rows: list[dict[str, object]] = []
    for line in output.splitlines():
        stripped_line = line.strip()
        if not stripped_line:
            continue
        try:
            payload = json.loads(stripped_line)
        except json.JSONDecodeError as error:
            raise click.ClickException(
                f"runner host hygiene {evidence_name} evidence was not valid JSON"
            ) from error
        if not isinstance(payload, dict):
            raise click.ClickException(
                f"runner host hygiene {evidence_name} evidence contained a non-object row"
            )
        rows.append(payload)
    return tuple(rows)


def _docker_json_text(row: Mapping[str, object], key: str) -> str:
    value = row.get(key)
    return _optional_scalar_text(value)


def _docker_labels(value: object) -> tuple[str, ...]:
    if not isinstance(value, dict):
        return ()
    labels = []
    for label_key, label_value in value.items():
        normalized_key = _optional_scalar_text(label_key)
        if not normalized_key:
            continue
        labels.append(f"{normalized_key}={_optional_scalar_text(label_value)}")
    return tuple(labels)


def _non_negative_int(value: object) -> int:
    if isinstance(value, bool) or value is None:
        return 0
    if isinstance(value, int):
        return max(value, 0)
    try:
        parsed_value = int(_optional_scalar_text(value))
    except ValueError:
        return 0
    return max(parsed_value, 0)


def _optional_scalar_text(value: object) -> str:
    if value is None or isinstance(value, bool | dict | list | tuple):
        return ""
    if isinstance(value, str):
        return value.strip()
    if isinstance(value, int | float | Decimal):
        return f"{value}".strip()
    return ""


def _parse_optional_docker_size_bytes(value: str) -> int:
    if not value.strip() or value.strip() in {"N/A", "-"}:
        return 0
    return _parse_docker_size_bytes(value)


def _image_reference(*, repository: str, tag: str) -> str:
    if _is_dangling_image(repository=repository, tag=tag):
        return ""
    return f"{repository}:{tag}"


def _is_warm_builder_image(*, repository: str, reference: str, retained_builders: set[str]) -> bool:
    return repository in retained_builders or reference in retained_builders


def _is_dangling_image(*, repository: str, tag: str) -> bool:
    return repository in {"", "<none>"} or tag in {"", "<none>"}


def _parse_docker_size_bytes(value: str) -> int:
    size = value.split("(", 1)[0].strip()
    if not size:
        raise click.ClickException("runner host hygiene Docker size was empty")
    numeric_text = "".join(
        character for character in size if character.isdigit() or character == "."
    )
    unit_text = size[len(numeric_text) :].strip().lower()
    if not numeric_text:
        raise click.ClickException("runner host hygiene Docker size was not numeric")
    try:
        numeric_value = Decimal(numeric_text)
    except InvalidOperation as error:
        raise click.ClickException("runner host hygiene Docker size was invalid") from error
    multiplier = _DOCKER_SIZE_UNITS.get(unit_text or "b")
    if multiplier is None:
        raise click.ClickException(
            f"runner host hygiene Docker size used unsupported unit: {unit_text}"
        )
    return int(numeric_value * multiplier)


def _compact_evidence(output: str) -> str:
    compact = " | ".join(line.strip() for line in output.splitlines() if line.strip())
    return compact[:500]


def _check_host_idle(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
    sleeper: AuditDeliverySleeper,
) -> str | None:
    for sample_index in range(request.idle_observation_count):
        active_processes = _require_remote_success(
            remote_runner(
                ("bash", "-lc", _ACTIVE_WORK_COMMAND),
                request.timeout_seconds,
            ),
            evidence_name="active_runner_or_build_processes",
        )
        raw_lines = tuple(
            line.strip() for line in active_processes.stdout.splitlines() if line.strip()
        )
        ancestor_pids: set[str] = set()
        lines: list[str] = []
        for line in raw_lines:
            if line.startswith("ancestor_pids="):
                ancestor_pids.update(line.partition("=")[2].split())
                continue
            process_id = line.split(maxsplit=1)[0]
            if "Runner.Worker" in line and process_id in ancestor_pids:
                continue
            if "runner-host-hygiene" not in line:
                lines.append(line)
        if lines:
            return _compact_evidence(_redact_sensitive_text("\n".join(lines)))
        if sample_index + 1 < request.idle_observation_count:
            sleeper(float(request.idle_observation_interval_seconds))
    return None


def _terminal_audit(
    *,
    request: RunnerHostHygieneApplyRequest,
    apply_plan: RunnerHostHygieneApplyPlan,
    pre_report: RunnerHostHygieneReport,
    post_report: RunnerHostHygieneReport | None,
    status: RunnerHostHygieneApplyAuditStatus,
    message: str,
) -> RunnerHostHygieneApplyAuditRecord:
    return RunnerHostHygieneApplyAuditRecord(
        audit_record_key=request.audit_record_key,
        status=status,
        request=request,
        plan=apply_plan,
        pre_apply_report=pre_report,
        post_apply_report=post_report,
        message=message,
    )


def _collect_terminal_report(
    *,
    request: RunnerHostHygieneExecutorRequest,
    remote_runner: RemoteCommandRunner,
    github_run_state_reader: GitHubRunStateReader | None,
) -> RunnerHostHygieneReport | None:
    try:
        return collect_runner_host_hygiene_report(
            request=request,
            remote_runner=remote_runner,
            github_run_state_reader=github_run_state_reader,
        )
    except click.ClickException:
        return None


def _redact_sensitive_text(value: str) -> str:
    redacted = re.sub(
        r"\b(?:ghp_|ghs_|github_pat_|sk-)[A-Za-z0-9_-]{12,}\b",
        "[REDACTED]",
        value,
    )
    redacted = re.sub(r"(?i)\bbearer\s+\S+", "Bearer [REDACTED]", redacted)
    redacted = re.sub(
        r"(?i)\b(token|secret|password|authorization)\s*([=:]|\s)\s*[^\s,;]+",
        r"\1\2[REDACTED]",
        redacted,
    )
    redacted = re.sub(r"(?i)(https?://)[^/@\s]+@", r"\1[REDACTED]@", redacted)
    return redacted.strip()[:500]


def _assert_generated_cache_audit_redacted(
    *,
    audit: RunnerHostHygieneApplyAuditRecord,
    request: RunnerHostHygieneExecutorRequest,
) -> None:
    serialized_audit = json.dumps(audit.model_dump(mode="json"), sort_keys=True)
    forbidden_values = {
        value
        for root in request.generated_run_cache_roots
        for value in (root.path, root.source_repository)
        if value
    }
    leaked_values = tuple(sorted(value for value in forbidden_values if value in serialized_audit))
    if leaked_values:
        raise click.ClickException(
            "runner host generated cache audit contained forbidden runtime topology"
        )


def _terminal_message(
    *,
    action: RunnerHostHygieneApplyAction,
    action_result: RemoteCommandResult,
    post_report: RunnerHostHygieneReport,
    post_evidence_failure: str,
) -> str:
    action_label = {
        "prune_docker_cache": "Docker cache prune",
        "prune_dangling_images": "dangling Docker image prune",
        "prune_generated_run_cache": "generated completed-run cache prune",
        "remove_buildkit_state_volumes": "BuildKit state volume removal",
        "prune_runner_workdir": "runner workdir prune",
        "restart_runner_service": "runner service restart",
    }[action]
    if action_result.returncode != 0:
        detail = action_result.stderr.strip() or action_result.stdout.strip() or "unknown error"
        return f"runner host {action_label} failed: {_redact_sensitive_text(detail)}"
    if post_evidence_failure:
        return f"runner host {action_label} completed but post evidence failed: {post_evidence_failure}"
    if post_report.status != "healthy":
        return (
            f"runner host {action_label} completed; post evidence still needs attention: "
            f"{post_report.summary}"
        )
    return f"runner host {action_label} completed and post evidence is healthy"


def _post_apply_evidence_failure(
    *,
    request: RunnerHostHygieneExecutorRequest,
    post_report: RunnerHostHygieneReport,
) -> str:
    missing_warm_builders = tuple(
        builder
        for builder in request.retained_warm_builders
        if builder not in post_report.warm_builders
    )
    if missing_warm_builders:
        return "retained warm builders are missing: " + ", ".join(missing_warm_builders)
    if request.action == "remove_buildkit_state_volumes":
        remaining_volumes = {volume.name for volume in post_report.volume_inventory}
        remaining_targets = tuple(
            volume
            for volume in request.target_buildkit_state_volumes
            if volume in remaining_volumes
        )
        if remaining_targets:
            return "target BuildKit state volumes remain present: " + ", ".join(remaining_targets)
    if request.action == "prune_generated_run_cache":
        root = _target_generated_run_cache_root(request)
        if root is None:
            return "target generated cache policy is missing from post evidence"
        cache = next(
            (
                item
                for item in post_report.generated_cache_usage
                if item.cache_key == request.target_generated_cache_key
            ),
            None,
        )
        if cache is None:
            return "target generated cache is missing from post evidence"
        remaining_run_ids = {entry.run_id for entry in cache.entries}.intersection(
            request.target_generated_cache_run_ids
        )
        if remaining_run_ids:
            return "target generated cache runs remain present: " + ", ".join(
                str(run_id) for run_id in sorted(remaining_run_ids)
            )
        if cache.allocated_bytes > root.low_water_bytes:
            return (
                "target generated cache remains above its low-water target: "
                f"{cache.allocated_bytes} > {root.low_water_bytes}"
            )
        if cache.measurement_status != "complete" or not (
            cache.owner_valid and cache.mode_valid and cache.symlink_safe
        ):
            return "target generated cache post evidence failed safety checks"
    return ""
