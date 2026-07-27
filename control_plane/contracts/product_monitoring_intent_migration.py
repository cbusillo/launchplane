from __future__ import annotations

from copy import deepcopy


def migrate_product_profile_monitoring_intent_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    migrated_payload = deepcopy(payload)
    lanes = migrated_payload.get("lanes")
    if not isinstance(lanes, list):
        return migrated_payload
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        health_monitoring = lane.get("health_monitoring")
        if not isinstance(health_monitoring, dict):
            continue
        if "monitoring_intent" in health_monitoring:
            continue
        health_monitoring["monitoring_intent"] = _inferred_monitoring_intent(
            health_monitoring.get("checks")
        )
    return migrated_payload


def downgrade_product_profile_monitoring_intent_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    downgraded_payload = deepcopy(payload)
    lanes = downgraded_payload.get("lanes")
    if not isinstance(lanes, list):
        return downgraded_payload
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        health_monitoring = lane.get("health_monitoring")
        if isinstance(health_monitoring, dict):
            health_monitoring.pop("monitoring_intent", None)
    return downgraded_payload


def _inferred_monitoring_intent(checks: object) -> str:
    if not isinstance(checks, list):
        return "prelaunch"
    enabled_kinds = {
        str(check.get("kind") or "public_http")
        for check in checks
        if isinstance(check, dict) and bool(check.get("enabled", True))
    }
    if "public_http" in enabled_kinds:
        return "public"
    if "private_http" in enabled_kinds:
        return "private"
    return "prelaunch"
