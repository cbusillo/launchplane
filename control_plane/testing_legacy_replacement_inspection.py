"""Observe retained legacy evidence without manufacturing dispatch or release proof."""

from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from time import monotonic
from typing import TYPE_CHECKING

from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
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

    @property
    def description(self) -> str:
        if not self.provider_inspected:
            return f"Legacy inspection: {self.detail}."
        return (
            f"Legacy inspection: retained deployment candidate; target history {self.target_history}; "
            f"schedule history {self.schedule_history}; timers {self.schedule_timers}; "
            "original data effects unbound."
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
    records = store.list_deployment_records(
        context_name=operation.context, instance_name=operation.instance, limit=51
    )
    if len(records) >= 51:
        return LegacyReplacementInspection(detail="retained deployment inventory truncated")
    started = datetime.fromisoformat(operation.started_at)
    fenced = datetime.fromisoformat(operation.updated_at)
    candidates = [
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
    ]
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
    if (
        not isinstance(schedules, list)
        or len(schedules) > 50
        or any(not isinstance(item, dict) for item in schedules)
    ):
        return LegacyReplacementInspection(detail="provider schedule inventory incomplete")
    # Reuse the executor's naming rule. Matching a mutable name is only a
    # candidate for observation, never attribution to the historical attempt.
    app_name = post_deploy._build_dokploy_data_workflow_schedule_app_name(
        context_name=operation.context, instance_name=operation.instance
    )
    schedule_states = []
    timer_states = []
    for schedule in schedules:
        assert isinstance(schedule, dict)
        if schedule.get("appName") != app_name:
            continue
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
    return LegacyReplacementInspection(
        provider_inspected=True,
        target_history=target_state,
        schedule_history=schedule_state,
        schedule_timers=timer_state,
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
