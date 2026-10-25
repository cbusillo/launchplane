from collections.abc import Mapping
from time import monotonic
import re

import click

from control_plane.dokploy import api
from control_plane.dokploy.runtime_evidence import DokployEvidenceProviderError

MAX_AUTOMATION_SCHEDULES = 20
MAX_AUTOMATION_HISTORY = 100
AUTOMATION_READ_SECONDS = 30
DEPLOYMENT_STATUSES = frozenset(
    {
        "idle",
        "queued",
        "running",
        "pending",
        "building",
        "done",
        "success",
        "failed",
        "error",
        "cancelled",
        "canceled",
    }
)


def automation_flag(value: object) -> str:
    return "enabled" if value is True else "disabled" if value is False else "unknown"


def inspect_dokploy_automation(
    *, host: str, token: str, target_type: str, target_id: str, payload: Mapping[str, object]
) -> dict[str, object]:
    if payload.get(target_type + "Id") != target_id:
        raise DokployEvidenceProviderError("automation-target-identity")
    deadline = monotonic() + AUTOMATION_READ_SECONDS

    def read(path: str, query: dict[str, str | int]) -> api.JsonValue:
        remaining = deadline - monotonic()
        if remaining <= 0:
            raise DokployEvidenceProviderError("automation-read-budget")
        try:
            return api.dokploy_request(
                host=host,
                token=token,
                path=path,
                query=query,
                timeout_seconds=min(5, remaining),
            )
        except click.ClickException as error:
            raise DokployEvidenceProviderError("automation-provider-read") from error

    raw = read("/api/schedule.list", {"id": target_id, "scheduleType": target_type})
    schedules = _object_collection(
        raw, ("data", "schedules", "items", "result"), MAX_AUTOMATION_SCHEDULES
    )
    complete = schedules is not None
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for schedule in schedules or []:
        schedule_id = _identity(schedule, ("scheduleId", "schedule_id", "id", "uuid"))
        if (
            not schedule_id
            or schedule_id in seen
            or schedule.get(target_type + "Id") != target_id
            or schedule.get("scheduleType") != target_type
        ):
            complete = False
            continue
        seen.add(schedule_id)
        history = read("/api/deployment.allByType", {"id": schedule_id, "type": "schedule"})
        rows.append(
            {
                "schedule_id": schedule_id,
                "enabled": automation_flag(schedule.get("enabled")),
                "history": _history_summary(history),
            }
        )
    return {
        "auto_deploy": automation_flag(payload.get("autoDeploy")),
        "schedule_inventory_complete": complete,
        "scope": "provider_credential_visible_target_schedules",
        "qualification": "not_evaluated",
        "schedules": rows,
    }


def _object_collection(
    payload: api.JsonValue, wrappers: tuple[str, ...], limit: int
) -> list[api.JsonObject] | None:
    if isinstance(payload, dict):
        values = [payload[key] for key in wrappers if key in payload]
        payload = values[0] if len(values) == 1 else None
    if not isinstance(payload, list) or len(payload) > limit:
        return None
    items = api.collect_dokploy_object_items(payload)
    return items if len(items) == len(payload) else None


def _identity(payload: api.JsonObject, keys: tuple[str, ...]) -> str:
    for key in keys:
        if key in payload:
            value = payload[key]
            return (
                value
                if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9_-]{1,160}", value)
                else ""
            )
    return ""


def _history_summary(payload: api.JsonValue) -> dict[str, object]:
    history = _object_collection(
        payload, ("data", "deployments", "items", "result"), MAX_AUTOMATION_HISTORY
    )
    if history is None:
        return {"state": "unknown", "entries": []}
    rows: list[dict[str, object]] = []
    seen: set[str] = set()
    for deployment in history:
        deployment_id = _identity(deployment, ("deploymentId", "deployment_id", "id", "uuid"))
        if not deployment_id or deployment_id in seen:
            return {"state": "unknown", "entries": []}
        seen.add(deployment_id)
        status = api.deployment_status(deployment)
        rows.append(
            {
                "deployment_id": deployment_id,
                "status": status if status in DEPLOYMENT_STATUSES else "unknown",
            }
        )
    return {"state": "present" if rows else "no_history", "entries": rows}
