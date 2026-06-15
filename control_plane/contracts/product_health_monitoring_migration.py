from __future__ import annotations

from copy import deepcopy


def migrate_product_profile_health_monitoring_payload(payload: object) -> object:
    """Return a profile payload using lane health_monitoring checks.

    This is used by both the database migration and the file-backed store so
    persisted legacy records receive the same one-time shape conversion.
    """
    if not isinstance(payload, dict):
        return payload
    migrated_payload = deepcopy(payload)
    lanes = migrated_payload.get("lanes")
    if not isinstance(lanes, list):
        return migrated_payload
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        if "health_monitoring" in lane:
            lane.pop("public_ingress_monitoring", None)
            continue
        old_policy = lane.pop("public_ingress_monitoring", None)
        if isinstance(old_policy, dict):
            enabled = bool(old_policy.get("enabled", True))
            lane["health_monitoring"] = {
                "checks": [
                    _public_http_check(
                        enabled=enabled,
                        require_runtime_identity=bool(
                            old_policy.get("require_runtime_identity", False)
                        ),
                        alert_issue_url=str(old_policy.get("alert_issue_url") or ""),
                    )
                ]
            }
        elif _lane_has_public_http_surface(lane=lane, profile=migrated_payload):
            lane["health_monitoring"] = {"checks": [_public_http_check()]}
        else:
            lane["health_monitoring"] = {"checks": []}
    return migrated_payload


def downgrade_product_profile_health_monitoring_payload(payload: object) -> object:
    if not isinstance(payload, dict):
        return payload
    downgraded_payload = deepcopy(payload)
    lanes = downgraded_payload.get("lanes")
    if not isinstance(lanes, list):
        return downgraded_payload
    for lane in lanes:
        if not isinstance(lane, dict):
            continue
        if "public_ingress_monitoring" in lane:
            lane.pop("health_monitoring", None)
            continue
        health_monitoring = lane.pop("health_monitoring", None)
        public_check = None
        if isinstance(health_monitoring, dict):
            checks = health_monitoring.get("checks")
            if isinstance(checks, list):
                public_check = next(
                    (
                        check
                        for check in checks
                        if isinstance(check, dict) and check.get("kind") == "public_http"
                    ),
                    None,
                )
        if isinstance(public_check, dict):
            lane["public_ingress_monitoring"] = {
                "enabled": bool(public_check.get("enabled", True)),
                "require_runtime_identity": bool(
                    public_check.get("require_runtime_identity", False)
                ),
                "alert_issue_url": str(public_check.get("alert_issue_url") or ""),
            }
        else:
            lane["public_ingress_monitoring"] = {"enabled": False}
    return downgraded_payload


def canonical_health_check_record_token(check_name: str) -> str:
    normalized_name = health_check_record_token(check_name)
    if normalized_name in {"", "public-ingress"}:
        return ""
    return normalized_name


def health_check_record_token(check_name: str) -> str:
    return "".join(
        character if character.isalnum() else "-" for character in check_name.strip().lower()
    ).strip("-")


def _public_http_check(
    *,
    enabled: bool = True,
    require_runtime_identity: bool = False,
    alert_issue_url: str = "",
) -> dict[str, object]:
    return {
        "name": "public-ingress",
        "kind": "public_http",
        "enabled": enabled,
        "url": "",
        "require_runtime_identity": require_runtime_identity,
        "alert_issue_url": alert_issue_url,
        "provider": "",
        "provider_check": "",
    }


def _lane_has_public_http_surface(*, lane: dict[str, object], profile: dict[str, object]) -> bool:
    if str(lane.get("health_url") or "").strip():
        return True
    if str(lane.get("base_url") or "").strip() and str(profile.get("health_path") or "").strip():
        return True
    return False
