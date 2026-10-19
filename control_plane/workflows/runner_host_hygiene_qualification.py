"""Offline acceptance of existing hygiene configuration and retention evidence.

Inputs are review artifacts, never runtime configuration authority. The future
record-backed projection can call this same evaluator without another store.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneAuditDeliveryEnvelope
from control_plane.workflows.runner_host_hygiene_executor import (
    RunnerHostGitHubBinding,
    RunnerHostHygieneExecutorRequest,
)


class BuilderMeasurement(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    builder: str
    availability: Literal["complete", "partial", "unavailable"]
    logical_bytes: int | None = Field(default=None, ge=0)
    # Explicit evidence explains an excess; it does not authorize removal or
    # pretend the target is a physical-volume quota.
    constraint: Literal["", "protected", "in_use", "recent_cache", "maintenance_blocked"] = ""
    evidence: str = ""
    recovery_owner: str = ""


class HygieneQualificationEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host_name: str
    observed_at: datetime
    services_complete: bool = False
    registrations_complete: bool = False
    images_complete: bool = False
    builders_complete: bool = False
    service_units: tuple[str, ...] = ()
    registrations: tuple[RunnerHostGitHubBinding, ...] = ()
    image_references: tuple[str, ...] = ()
    builders: tuple[BuilderMeasurement, ...] = ()
    audits: tuple[RunnerHostHygieneAuditDeliveryEnvelope, ...] = ()


class BuilderQualification(BaseModel):
    builder: str
    target_bytes: int
    logical_bytes: int | None
    status: Literal["within_target", "explained", "blocked", "unknown"]
    reason: str
    recovery_owner: str
    accepted_terminal_completion: bool = False


class HygieneQualification(BaseModel):
    mode: Literal["report-only"] = "report-only"
    configuration_qualified: bool
    retention_accounted_for: bool
    blockers: tuple[str, ...]
    builders: tuple[BuilderQualification, ...]


def qualify_runner_host_hygiene(
    *,
    requests: tuple[RunnerHostHygieneExecutorRequest, ...],
    evidence: HygieneQualificationEvidence,
    now: datetime,
) -> HygieneQualification:
    """Check a proposed projection using complete independent host observations.

    Each request is an existing configured named-builder schedule entry. No
    extra builder receives a budget, grant or cleanup permission here.
    """
    blockers: list[str] = []
    if not requests:
        blockers.append("configured_named_builder_requests_missing")
    for request in requests:
        if request.host_name != evidence.host_name:
            blockers.append("host_mismatch")
        if (
            now.tzinfo is None
            or evidence.observed_at.tzinfo is None
            or not 0 <= (now - evidence.observed_at).total_seconds() <= request.idle_max_age_seconds
        ):
            blockers.append("source_stale_or_future")
        if not evidence.services_complete or not evidence.registrations_complete:
            blockers.append("fleet_source_incomplete")
        configured = set(request.github_idle_bindings)
        registered = set(evidence.registrations)
        units = [binding.service_unit for binding in evidence.registrations]
        if (
            not configured
            or configured != registered
            or len(units) != len(set(units))
            or len(evidence.service_units) != len(set(evidence.service_units))
            or set(units) != set(evidence.service_units)
        ):
            blockers.append("fleet_binding_mismatch")
        if not evidence.images_complete:
            blockers.append("retained_image_source_incomplete")
        if not set(request.retained_warm_builders).issubset(evidence.image_references):
            blockers.append("retained_image_missing")
        if (
            request.action != "prune_docker_cache"
            or not request.target_buildkit_builder
            or request.target_buildkit_builder not in request.allowed_buildkit_builders
            or request.max_used_space_bytes <= 0
        ):
            blockers.append("named_builder_request_invalid")
    targets = [request.target_buildkit_builder for request in requests]
    if len(targets) != len(set(targets)):
        blockers.append("duplicate_builder_target")
    measurements = [item.builder for item in evidence.builders]
    if not evidence.builders_complete or len(measurements) != len(set(measurements)):
        blockers.append("builder_source_incomplete_or_ambiguous")
    if set(measurements) != set(targets):
        # Includes live builders without a configured budget. Never borrow
        # another builder's budget or silently leave their ownership unqualified.
        blockers.append("builder_ownership_unqualified")
    rows: list[BuilderQualification] = []
    for request in requests:
        matching = [
            item for item in evidence.builders if item.builder == request.target_buildkit_builder
        ]
        measurement = matching[0] if len(matching) == 1 else None
        status: Literal["within_target", "explained", "blocked", "unknown"] = "unknown"
        reason = "named_builder_measurement_incomplete"
        if (
            measurement
            and measurement.availability == "complete"
            and measurement.logical_bytes is not None
        ):
            if measurement.logical_bytes <= request.max_used_space_bytes:
                status, reason = "within_target", "measured_within_target"
            elif (
                measurement.constraint
                and measurement.evidence.strip()
                and measurement.recovery_owner.strip()
            ):
                status = (
                    "blocked" if measurement.constraint == "maintenance_blocked" else "explained"
                )
                reason = measurement.constraint
            else:
                status, reason = "blocked", "above_target_without_owned_explanation"
        rows.append(
            BuilderQualification(
                builder=request.target_buildkit_builder,
                target_bytes=request.max_used_space_bytes,
                logical_bytes=measurement.logical_bytes if measurement else None,
                status=status,
                reason=reason,
                recovery_owner=measurement.recovery_owner if measurement else "",
                accepted_terminal_completion=any(
                    _accepted_completion(request, audit, now) for audit in evidence.audits
                ),
            )
        )
    return HygieneQualification(
        configuration_qualified=not blockers,
        retention_accounted_for=not blockers
        and bool(rows)
        and all(row.status in {"within_target", "explained"} for row in rows),
        blockers=tuple(sorted(set(blockers))),
        builders=tuple(rows),
    )


def _accepted_completion(
    request: RunnerHostHygieneExecutorRequest,
    envelope: RunnerHostHygieneAuditDeliveryEnvelope,
    now: datetime,
) -> bool:
    terminal = envelope.terminal_audit
    if terminal is None or terminal.post_apply_report is None:
        return False
    try:
        observed_at = datetime.fromisoformat(terminal.post_apply_report.observed_at)
        if (
            observed_at.tzinfo is None
            or not 0 <= (now - observed_at).total_seconds() <= request.idle_max_age_seconds
        ):
            return False
    except (ValueError, TypeError):
        return False
    planned_targets = envelope.planned_audit.request.model_dump()
    terminal_targets = terminal.request.model_dump()
    planned_targets.pop("idle_decision_at")
    terminal_targets.pop("idle_decision_at")
    return (
        envelope.host_name == request.host_name
        and envelope.audit_record_key == request.audit_record_key
        and planned_targets == terminal_targets
        and envelope.planned_delivery_state == "delivered"
        and envelope.terminal_delivery_state == "delivered"
        and terminal.status == "completed"
        and terminal.request.mutate
        and terminal.request.action == request.action
        and terminal.request.host_name == request.host_name
        and terminal.request.target_buildkit_builder == request.target_buildkit_builder
        and terminal.request.max_used_space_bytes == request.max_used_space_bytes
        and terminal.request.retained_warm_builders == request.retained_warm_builders
    )
