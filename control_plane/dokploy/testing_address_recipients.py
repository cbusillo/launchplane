from typing import Any

from control_plane.dokploy.route_rules import host_rule_may_match


def testing_address_tcp_may_match(rule: object, hostname: str) -> bool:
    import re

    if not isinstance(rule, str):
        return True
    literal = re.fullmatch(r"HostSNI\(`([a-zA-Z0-9.-]+)`\)", rule.strip())
    return literal is None or literal.group(1).casefold() == hostname.casefold()


def qualify_testing_address_tcp_routes(
    raw: dict[str, Any], hostname: str, entrypoints: tuple[str, ...]
) -> None:
    routers = raw.get("tcpRouters", {})
    if not isinstance(routers, dict):
        raise ValueError("Applied TCP router inventory is incomplete")
    for router in routers.values():
        if not isinstance(router, dict):
            raise ValueError("Applied TCP router ownership is incomplete")
        if not testing_address_tcp_may_match(router.get("rule"), hostname):
            continue
        points = router.get("entryPoints")
        if (
            not isinstance(points, list)
            or not points
            or any(not isinstance(point, str) for point in points)
        ):
            raise ValueError("Applied TCP router listeners are unavailable")
        if set(points) & set(entrypoints):
            raise ValueError("A TCP router can intercept the testing hostname")


def qualify_testing_address_recipients(
    containers: list[dict[str, Any]], spec: dict[str, Any]
) -> str:
    import hashlib
    import json

    hostname = spec["hostname"]
    projects = set(spec["projects"])
    points = set(spec["entrypoints"])
    maximum_priority = spec["priority"]
    members: list[dict[str, Any]] = []
    found: set[str] = set()
    for container in containers:
        labels = container.get("Config", {}).get("Labels") or {}
        if not isinstance(labels, dict):
            raise ValueError("Address recipient labels are unavailable")
        project = labels.get("com.docker.compose.project")
        if isinstance(project, str) and project in projects:
            found.add(project)
            state = container.get("State", {}).get("Status")
            restart = container.get("HostConfig", {}).get("RestartPolicy", {}).get("Name")
            if state not in {"exited", "created"} or restart not in {"no", "unless-stopped"}:
                raise ValueError("A retained hostname recipient can still start automatically")
            members.append(
                {
                    "id": container["Id"],
                    "project": project,
                    "state": state,
                    "restart": restart,
                    "routing": {
                        key: value for key, value in labels.items() if key.startswith("traefik.")
                    },
                }
            )
        for key, rule in labels.items():
            if key.startswith("traefik.tcp.routers.") and key.endswith(".rule"):
                listeners = labels.get(key.removesuffix(".rule") + ".entrypoints")
                if (
                    not isinstance(listeners, str)
                    or not listeners
                    or set(listeners.split(",")) & points
                ) and testing_address_tcp_may_match(rule, hostname):
                    raise ValueError("A latent TCP router can intercept the testing hostname")
            if not key.startswith("traefik.http.routers.") or not key.endswith(".rule"):
                continue
            if not isinstance(rule, str):
                raise ValueError("A latent hostname router rule is unavailable")
            if not host_rule_may_match(rule, hostname):
                continue
            prefix = key.removesuffix(".rule")
            priority = labels.get(prefix + ".priority", "0")
            listeners = labels.get(prefix + ".entrypoints")
            if (
                project not in projects
                or not isinstance(listeners, str)
                or not listeners
                or not set(listeners.split(",")) <= points
                or not isinstance(priority, str)
                or not priority.isdecimal()
                or (int(priority) or len(rule.encode())) >= maximum_priority
            ):
                raise ValueError("A latent hostname router has no strict testing-owned winner")
    if found != projects or not projects:
        raise ValueError("The original retained hostname recipient inventory is incomplete")
    return hashlib.sha256(
        json.dumps(
            sorted(members, key=lambda item: item["id"]), sort_keys=True, separators=(",", ":")
        ).encode()
    ).hexdigest()
