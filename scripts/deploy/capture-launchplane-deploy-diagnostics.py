from __future__ import annotations

import json
import os
from collections.abc import Mapping
from typing import Any

from click import ClickException

from control_plane import dokploy as control_plane_dokploy


def _env(name: str) -> str:
    return os.environ.get(name, "").strip()


def _redact_text(raw_text: str) -> str:
    return control_plane_dokploy.redact_dokploy_log_line(raw_text)


def _request_json(
    *,
    host: str,
    token: str,
    path: str,
    query: Mapping[str, str | int] | None = None,
) -> Any:
    try:
        return control_plane_dokploy.dokploy_request(
            host=host,
            token=token,
            path=path,
            query=dict(query or {}),
        )
    except ClickException as error:
        return {"error": _redact_text(str(error))[:2000]}


def _target_env_summary(raw_env: str, *, expected_image_reference: str) -> dict[str, object]:
    env_map = control_plane_dokploy.parse_dokploy_env_text(raw_env)
    return {
        "env_keys": sorted(env_map),
        "docker_image_matches_expected": env_map.get("DOCKER_IMAGE_REFERENCE", "")
        == expected_image_reference,
    }


def _compact_target_payload(
    *,
    target_type: str,
    target_id: str,
    target_payload: Mapping[str, Any],
    expected_image_reference: str,
) -> dict[str, object]:
    deployments = [
        {
            "deploymentId": deployment.get("deploymentId"),
            "status": deployment.get("status"),
            "title": deployment.get("title"),
            "createdAt": deployment.get("createdAt"),
        }
        for deployment in target_payload.get("deployments") or []
        if isinstance(deployment, dict)
    ][-8:]
    domains = [
        {
            "host": domain.get("host"),
            "port": domain.get("port"),
            "path": domain.get("path"),
            "serviceName": domain.get("serviceName"),
        }
        for domain in target_payload.get("domains") or []
        if isinstance(domain, dict)
    ]
    env_summary = _target_env_summary(
        str(target_payload.get("env") or ""),
        expected_image_reference=expected_image_reference,
    )
    return {
        "diagnostic": "dokploy-target",
        "target_type": target_type,
        "target_id": target_id,
        "appName": target_payload.get("appName"),
        "composeStatus": target_payload.get("composeStatus"),
        "domains": domains,
        "deployments": deployments,
        **env_summary,
    }


def _target_containers(*, containers_payload: Any, app_name: str) -> list[dict[str, object]]:
    containers = containers_payload if isinstance(containers_payload, list) else []
    target_containers: list[dict[str, object]] = []
    for container in containers:
        if not isinstance(container, dict):
            continue
        container_name = str(container.get("name") or "")
        if app_name and app_name not in container_name:
            continue
        target_containers.append(
            {
                "containerId": container.get("containerId")
                or container.get("id")
                or container.get("Id"),
                "name": container_name,
                "image": container.get("image") or container.get("Image"),
                "state": container.get("state") or container.get("State"),
                "status": container.get("status") or container.get("Status"),
                "ports": container.get("ports") or container.get("Ports"),
            }
        )
    return target_containers


def _container_id(container: Mapping[str, object]) -> str:
    return str(container.get("containerId") or container.get("id") or container.get("Id") or "").strip()


def _container_config_summary(config_payload: Any) -> dict[str, object]:
    if not isinstance(config_payload, dict):
        return {"config_present": False}
    labels = ((config_payload.get("Config") or {}).get("Labels") or {})
    networks = ((config_payload.get("NetworkSettings") or {}).get("Networks") or {})
    label_map = labels if isinstance(labels, dict) else {}
    network_map = networks if isinstance(networks, dict) else {}
    return {
        "config_present": True,
        "label_count": len(label_map),
        "traefik_label_count": len(
            [key for key in label_map if str(key).startswith("traefik.")]
        ),
        "traefik_enable": label_map.get("traefik.enable"),
        "traefik_docker_network": label_map.get("traefik.docker.network"),
        "network_names": sorted(str(key) for key in network_map),
        "has_dokploy_network": "dokploy-network" in network_map,
    }


def _print_json(payload: Mapping[str, object]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _print_container_logs(
    *, host: str, token: str, target_id: str, container: Mapping[str, object]
) -> None:
    container_id = str(container.get("containerId") or "").strip()
    if not container_id:
        return
    log_payload = _request_json(
        host=host,
        token=token,
        path="/api/compose.readLogs",
        query={"composeId": target_id, "containerId": container_id, "tail": 300, "since": "1h"},
    )
    log_text = log_payload if isinstance(log_payload, str) else json.dumps(log_payload)
    print(f"--- Dokploy logs for {container.get('name')} ({container_id}) ---")
    for line in log_text.splitlines()[-120:]:
        print(_redact_text(line)[:1200])


def main() -> int:
    host = _env("LAUNCHPLANE_EMERGENCY_DOKPLOY_HOST")
    token = _env("LAUNCHPLANE_EMERGENCY_DOKPLOY_TOKEN")
    if not host or not token:
        print("Skipping Dokploy diagnostics because emergency Dokploy credentials are unavailable.")
        return 0

    target_type = _env("LAUNCHPLANE_DOKPLOY_TARGET_TYPE")
    target_id = _env("LAUNCHPLANE_DOKPLOY_TARGET_ID")
    expected_image_reference = _env("LAUNCHPLANE_EXPECTED_IMAGE_REFERENCE")
    if target_type != "compose":
        print(
            f"Dokploy diagnostics currently support compose targets only; target_type={target_type}."
        )
        return 0
    if not target_id:
        print("Skipping Dokploy diagnostics because LAUNCHPLANE_DOKPLOY_TARGET_ID is empty.")
        return 0

    target_payload = _request_json(
        host=host,
        token=token,
        path="/api/compose.one",
        query={"composeId": target_id},
    )
    if not isinstance(target_payload, dict) or "error" in target_payload:
        _print_json(
            {"diagnostic": "dokploy-target", "target_id": target_id, "target": target_payload}
        )
        return 0

    _print_json(
        _compact_target_payload(
            target_type=target_type,
            target_id=target_id,
            target_payload=target_payload,
            expected_image_reference=expected_image_reference,
        )
    )

    app_name = str(target_payload.get("appName") or "")
    server_id = str(target_payload.get("serverId") or "").strip()
    container_query = {"appName": app_name, "appType": "docker-compose"}
    if server_id:
        container_query["serverId"] = server_id
    containers_payload = _request_json(
        host=host,
        token=token,
        path="/api/docker.getContainersByAppNameMatch",
        query=container_query,
    )
    target_containers = _target_containers(containers_payload=containers_payload, app_name=app_name)
    _print_json({"diagnostic": "dokploy-containers", "containers": target_containers})
    for container in target_containers:
        container_id = _container_id(container)
        if container_id:
            config_query = {"containerId": container_id}
            if server_id:
                config_query["serverId"] = server_id
            config_payload = _request_json(
                host=host,
                token=token,
                path="/api/docker.getConfig",
                query=config_query,
            )
            _print_json(
                {
                    "diagnostic": "dokploy-container-config",
                    "containerId": container_id,
                    "name": container.get("name"),
                    **_container_config_summary(config_payload),
                }
            )
        _print_container_logs(host=host, token=token, target_id=target_id, container=container)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
