"""Observe retained legacy evidence without manufacturing dispatch or release proof."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.dokploy import api, post_deploy, source

if TYPE_CHECKING:
    from control_plane.testing_replacement_recovery import TestingRecoveryStore


@dataclass(frozen=True)
class LegacyReplacementInspection:
    provider_inspected: bool = False
    detail: str = ""
    target_history: str = "unknown"
    schedule_history: str = "unknown"
    schedule_timers: str = "unknown"
    retained_schedule_run: str = "unavailable"
    retained_schedule_history: str = "unavailable"
    retained_schedule_timer: str = "unavailable"

    @property
    def description(self) -> str:
        if not self.provider_inspected:
            return f"Legacy inspection: {self.detail}."
        return (
            f"Legacy inspection: retained deployment candidate; target history {self.target_history}; "
            f"schedule history {self.schedule_history}; timers {self.schedule_timers}; "
            f"retained schedule run {self.retained_schedule_run}; "
            f"retained schedule history {self.retained_schedule_history}; "
            f"retained schedule timer {self.retained_schedule_timer}; "
            "original data-effect coverage incomplete."
        )


def inspect_legacy_replacement(
    store: "TestingRecoveryStore",
    operation: OdooStableTargetReplacementOperationRecord,
    root: Path,
    *,
    deadline_at: float,
) -> LegacyReplacementInspection:
    """A candidate's current provider history never proves the old effects complete.

    The caller owns execution exclusion, authority and before/after record checks.
    Only independently retained deployment records supply provider coordinates;
    current runtime identity and current lane coordinates do not supply a target.
    This function has no settlement path and emits only fixed diagnostic text.
    """
    if not operation.request.artifact_id or not operation.request.source_git_ref:
        return LegacyReplacementInspection(detail="original build request unavailable")
    candidates = _retained_deployment_candidates(store, operation)
    if candidates is None:
        return LegacyReplacementInspection(detail="retained deployment inventory truncated")
    if len(candidates) != 1:
        return LegacyReplacementInspection(
            detail="retained deployment attribution unavailable or ambiguous"
        )
    retained = candidates[0]
    assert retained.resolved_target is not None
    target_id = retained.resolved_target.target_id
    current_target = store.read_dokploy_target_id_record(
        context_name=operation.context, instance_name=operation.instance
    )
    if current_target.target_id != target_id:
        return LegacyReplacementInspection(detail="retained provider target differs from the lane")
    host, token = source.read_dokploy_config(control_plane_root=root)

    def read(path: str, query: dict[str, str | int]) -> api.JsonValue:
        timeout = min(5, deadline_at - monotonic())
        if timeout <= 0:
            raise TimeoutError("Legacy inspection budget expired.")
        return api.dokploy_request(
            host=host, token=token, path=path, query=query, timeout_seconds=timeout
        )

    target = read("/api/compose.one", {"composeId": target_id})
    if not isinstance(target, dict) or target.get("composeId") != target_id:
        return LegacyReplacementInspection(detail="retained provider target unreadable")
    history = read("/api/deployment.allByType", {"id": target_id, "type": "compose"})
    target_state = _history_state(history)
    server_id = str(target.get("serverId") or "").strip()
    if server_id:
        schedule_type, lookup_id = "server", server_id
    else:
        session = read("/api/user.session", {})
        user = session.get("user") if isinstance(session, dict) else None
        lookup_id = str(user.get("id") or "").strip() if isinstance(user, dict) else ""
        schedule_type = "dokploy-server"
    if not lookup_id:
        return LegacyReplacementInspection(detail="provider schedule inventory unavailable")
    schedules = read("/api/schedule.list", {"id": lookup_id, "scheduleType": schedule_type})
    if isinstance(schedules, dict):
        wrappers = [
            schedules[key] for key in ("data", "schedules", "items", "result") if key in schedules
        ]
        schedules = wrappers[0] if len(wrappers) == 1 else None
    if not isinstance(schedules, list) or any(not isinstance(item, dict) for item in schedules):
        return LegacyReplacementInspection(detail="provider schedule inventory incomplete")
    # Reuse the executor's naming rule. Matching a mutable name is only a
    # candidate for observation, never attribution to the historical attempt.
    app_name = post_deploy._build_dokploy_data_workflow_schedule_app_name(
        context_name=operation.context, instance_name=operation.instance
    )
    schedules = [
        schedule
        for schedule in schedules
        if isinstance(schedule, dict) and schedule.get("appName") == app_name
    ]
    if len(schedules) > 50:
        return LegacyReplacementInspection(
            detail="lane schedule inventory exceeds inspection bound"
        )
    schedule_states = []
    timer_states = []
    for schedule in schedules:
        assert isinstance(schedule, dict)
        enabled = schedule.get("enabled")
        timer_states.append(
            "enabled" if enabled is True else "disabled" if enabled is False else "unknown"
        )
        schedule_id = api.schedule_key(schedule)
        if not schedule_id:
            schedule_states.append("unknown")
            continue
        schedule_history = read(
            "/api/deployment.allByType", {"id": schedule_id, "type": "schedule"}
        )
        schedule_states.append(_history_state(schedule_history))
    schedule_state = (
        "unavailable"
        if not schedule_states
        else "unknown"
        if "unknown" in schedule_states
        else "active"
        if "active" in schedule_states
        else "empty"
        if "empty" in schedule_states
        else "terminal"
    )
    timer_state = (
        "unavailable"
        if not timer_states
        else "unknown"
        if "unknown" in timer_states
        else "enabled"
        if "enabled" in timer_states
        else "disabled"
    )
    # Independently retained references can identify a run even if the schedule's
    # mutable name changed. They do not enumerate every historical dispatch.
    retained_run = retained_history = retained_timer = "unavailable"
    evidence = retained.post_deploy_update.evidence
    schedule_reference = evidence.get("post_deploy_readback_schedule_id", "")
    run_reference = evidence.get("post_deploy_readback_schedule_deployment_id", "")
    if schedule_reference or run_reference:
        retained_run = retained_history = retained_timer = "unknown"
        retained_schedule_id = post_deploy._bounded_dokploy_evidence_id(schedule_reference)
        retained_run_id = post_deploy._bounded_dokploy_evidence_id(run_reference)
        if retained_schedule_id and retained_run_id:
            schedule = read("/api/schedule.one", {"scheduleId": retained_schedule_id})
            if isinstance(schedule, dict) and api.schedule_key(schedule) == retained_schedule_id:
                enabled = schedule.get("enabled")
                retained_timer = (
                    "enabled" if enabled is True else "disabled" if enabled is False else "unknown"
                )
                history = read(
                    "/api/deployment.allByType", {"id": retained_schedule_id, "type": "schedule"}
                )
                retained_history = _history_state(history)
                if retained_history in {"terminal", "active"} and isinstance(history, list):
                    matches = [
                        item
                        for item in history
                        if isinstance(item, dict) and api.deployment_key(item) == retained_run_id
                    ]
                    if len(matches) == 1:
                        retained_run = (
                            "terminal"
                            if api.deployment_status(matches[0]) in {"done", "error"}
                            else "active"
                        )
    if _retained_deployment_candidates(store, operation) != (retained,):
        return LegacyReplacementInspection(detail="retained deployment evidence changed")
    return LegacyReplacementInspection(
        provider_inspected=True,
        target_history=target_state,
        schedule_history=schedule_state,
        schedule_timers=timer_state,
        retained_schedule_run=retained_run,
        retained_schedule_history=retained_history,
        retained_schedule_timer=retained_timer,
    )


def _retained_deployment_candidates(
    store: "TestingRecoveryStore", operation: OdooStableTargetReplacementOperationRecord
) -> tuple[DeploymentRecord, ...] | None:
    records = store.list_deployment_records(
        context_name=operation.context,
        instance_name=operation.instance,
        artifact_id=operation.request.artifact_id,
        source_git_ref=operation.request.source_git_ref,
        limit=51,
    )
    if len(records) >= 51:
        return None
    started = datetime.fromisoformat(operation.started_at)
    fenced = datetime.fromisoformat(operation.updated_at)
    return tuple(
        record
        for record in records
        if record.context == operation.context
        and record.instance == operation.instance
        and record.source_git_ref == operation.request.source_git_ref
        and record.artifact_identity is not None
        and record.artifact_identity.artifact_id == operation.request.artifact_id
        and record.resolved_target is not None
        and record.resolved_target.target_type == "compose"
        and started <= datetime.fromisoformat(record.deploy.started_at) <= fenced
    )


def _history_state(history: api.JsonValue) -> str:
    if not isinstance(history, list) or any(not isinstance(item, dict) for item in history):
        return "unknown"
    if not history:
        return "empty"
    ids = [api.deployment_key(item) for item in history if isinstance(item, dict)]
    if not all(ids) or len(set(ids)) != len(ids):
        return "unknown"
    statuses = [api.deployment_status(item) for item in history if isinstance(item, dict)]
    if any(status not in {"done", "error", "running", "queued"} for status in statuses):
        return "unknown"
    return "terminal" if all(status in {"done", "error"} for status in statuses) else "active"
