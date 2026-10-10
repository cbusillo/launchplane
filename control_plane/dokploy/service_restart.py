"""Dokploy's container restart API, with strict inspected Compose membership."""

import hashlib
import json
from typing import cast

from control_plane.contracts.lane_service_restart import RestartContainerIdentity
from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    compare_runtime_identity,
    parse_runtime_identity_payload,
    RUNTIME_IDENTITY_ENV_KEY,
)
from control_plane.dokploy import api
from control_plane.dokploy.runtime_evidence import normalize_expected_image_reference


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def inspect_service(
    *,
    host: str,
    token: str,
    app_name: str,
    server_id: str,
    service: str,
    expected: RuntimeIdentity,
) -> RestartContainerIdentity:
    query: dict[str, str | int] = {"appName": app_name, "appType": "docker-compose"}
    if server_id:
        query["serverId"] = server_id
    raw = api.dokploy_request(
        host=host, token=token, path="/api/docker.getContainersByAppNameMatch", query=query
    )
    if not isinstance(raw, list):
        raise ValueError("Container inventory is unavailable.")
    selected: list[RestartContainerIdentity] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError("Container inventory is ambiguous.")
        container_id = str(item.get("containerId") or "")
        if not container_id or container_id in seen:
            raise ValueError("Container inventory is ambiguous.")
        seen.add(container_id)
        config_query: dict[str, str | int] = {"containerId": container_id}
        if server_id:
            config_query["serverId"] = server_id
        config_raw = api.dokploy_request(
            host=host, token=token, path="/api/docker.getConfig", query=config_query
        )
        if isinstance(config_raw, str):
            config_raw = json.loads(config_raw)
        if isinstance(config_raw, list) and len(config_raw) == 1:
            config_raw = config_raw[0]
        if not isinstance(config_raw, dict):
            raise ValueError("Container inspection is unavailable.")
        config = config_raw.get("Config")
        if not isinstance(config, dict) or not isinstance(config.get("Labels"), dict):
            raise ValueError("Container membership is ambiguous.")
        labels = cast(dict[str, object], config["Labels"])
        # Never fall back to a name substring, list metadata, or a guessed index.
        if labels.get("com.docker.compose.project") != app_name:
            continue
        if labels.get("com.docker.compose.service") != service:
            continue
        if str(labels.get("com.docker.compose.oneoff", "false")).lower() != "false":
            continue
        state = config_raw.get("State")
        if not isinstance(state, dict) or any(
            state.get(key) for key in ("Paused", "Restarting", "Dead")
        ):
            raise ValueError("Container state is ambiguous or cannot be restarted safely.")
        full_id = str(config_raw.get("Id") or "")
        if not full_id.startswith(container_id) or len(container_id) < 12:
            raise ValueError("Container identity changed during inspection.")
        env = config.get("Env")
        if not isinstance(env, list):
            raise ValueError("Runtime identity is missing.")
        identities = [
            value.split("=", 1)[1]
            for value in env
            if isinstance(value, str) and value.startswith(RUNTIME_IDENTITY_ENV_KEY + "=")
        ]
        observed = parse_runtime_identity_payload(identities[0]) if len(identities) == 1 else None
        if compare_runtime_identity(expected=expected, observed=observed)[0] != "match":
            raise ValueError("Container does not run the recorded current artifact.")
        image = str(config.get("Image") or "")
        if normalize_expected_image_reference(image) != expected.image_reference:
            raise ValueError("Container image differs from the recorded current artifact.")
        health = state.get("Health")
        selected.append(
            RestartContainerIdentity(
                container_id=full_id,
                image_id=str(config_raw.get("Image") or ""),
                image_reference=image,
                configuration_sha256=_digest(
                    {
                        "Config": config,
                        "HostConfig": config_raw.get("HostConfig"),
                        "Mounts": config_raw.get("Mounts"),
                    }
                ),
                started_at=str(state.get("StartedAt") or ""),
                running=state.get("Running") is True,
                health=str(health.get("Status") or "unknown")
                if isinstance(health, dict)
                else "unavailable",
                runtime_identity_sha256=_digest(
                    observed.model_dump(mode="json") if observed else {}
                ),
            )
        )
    if len(selected) != 1:
        raise ValueError("Restart requires exactly one inspected service container.")
    return selected[0]


def restart_container(*, host: str, token: str, container_id: str, server_id: str) -> None:
    payload: api.JsonObject = {"containerId": container_id}
    if server_id:
        payload["serverId"] = server_id
    # A timeout is an unknown effect. Do not retry this POST.
    api.dokploy_request(
        host=host, token=token, path="/api/docker.restartContainer", method="POST", payload=payload
    )
