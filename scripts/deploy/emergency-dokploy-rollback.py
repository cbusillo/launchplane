from __future__ import annotations

import json
import os
from typing import Any

from control_plane import dokploy as control_plane_dokploy


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _required_env(name: str) -> str:
    value = _env(name)
    if not value:
        raise SystemExit(f"Missing required environment variable: {name}")
    return value


def _require_break_glass_allowed() -> None:
    if _env("LAUNCHPLANE_ALLOW_DIRECT_DOKPLOY_FALLBACK").lower() != "true":
        raise SystemExit(
            "Direct Dokploy rollback fallback requires "
            "LAUNCHPLANE_ALLOW_DIRECT_DOKPLOY_FALLBACK=true."
        )


def _deploy_timeout_seconds() -> int:
    raw_timeout = _env("LAUNCHPLANE_DOKPLOY_DEPLOY_TIMEOUT_SECONDS") or "600"
    try:
        timeout_seconds = int(raw_timeout)
    except ValueError as error:
        raise SystemExit(
            "LAUNCHPLANE_DOKPLOY_DEPLOY_TIMEOUT_SECONDS must be an integer."
        ) from error
    if timeout_seconds <= 0:
        raise SystemExit("LAUNCHPLANE_DOKPLOY_DEPLOY_TIMEOUT_SECONDS must be positive.")
    return timeout_seconds


def run_break_glass_rollback() -> dict[str, Any]:
    _require_break_glass_allowed()

    host = _required_env("LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST")
    token = _required_env("LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN")
    target_type = _required_env("LAUNCHPLANE_DOKPLOY_TARGET_TYPE")
    target_id = _required_env("LAUNCHPLANE_DOKPLOY_TARGET_ID")
    image_reference = _required_env("LAUNCHPLANE_ROLLBACK_IMAGE_REFERENCE")
    rollback_request_status = _required_env("LAUNCHPLANE_ROLLBACK_REQUEST_STATUS")
    deploy_timeout_seconds = _deploy_timeout_seconds()

    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )
    raw_env_text = str(target_payload.get("env") or "")
    updated_env_text = control_plane_dokploy.render_dokploy_env_text_with_overrides(
        raw_env_text,
        updates={"DOCKER_IMAGE_REFERENCE": image_reference},
    )
    image_reference_changed = updated_env_text != raw_env_text
    if image_reference_changed:
        control_plane_dokploy.update_dokploy_target_env(
            host=host,
            token=token,
            target_type=target_type,
            target_id=target_id,
            target_payload=target_payload,
            env_text=updated_env_text,
        )

    latest_before = control_plane_dokploy.latest_deployment_for_target(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
    )
    control_plane_dokploy.trigger_deployment(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
        no_cache=True,
    )
    deployment_result = control_plane_dokploy.wait_for_target_deployment(
        host=host,
        token=token,
        target_type=target_type,
        target_id=target_id,
        before_key=control_plane_dokploy.deployment_key(latest_before),
        timeout_seconds=deploy_timeout_seconds,
    )

    return {
        "schema_version": 1,
        "event": "launchplane_break_glass_rollback",
        "fallback_kind": "direct_dokploy",
        "reason": "Launchplane self-deploy rollback service request failed.",
        "service_rollback_http_status": rollback_request_status,
        "target_type": target_type,
        "target_id": target_id,
        "image_reference": image_reference,
        "image_reference_changed": image_reference_changed,
        "deployment_result": deployment_result,
    }


def main() -> int:
    print(json.dumps(run_break_glass_rollback(), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
