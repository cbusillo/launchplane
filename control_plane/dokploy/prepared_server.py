import json
from typing import Any

from control_plane.contracts.cutover_host import OdooCutoverHostRuntime
from control_plane.dokploy.slot_maintenance import ContainerCommand


PREPARED_COMMAND = (
    "/app/.venv/bin/uvicorn",
    "control_plane.dokploy.prepared_host:prepared_service_app",
    "--factory",
    "--host",
    "0.0.0.0",
    "--port",
    "8080",
)


def require_prepared_server(
    runtime: OdooCutoverHostRuntime, reader: ContainerCommand, *, require_running: bool = True
) -> None:
    values = json.loads(reader(("docker", "inspect", runtime.prepared_container), None))
    if len(values) != 1:
        raise ValueError("prepared server inventory is ambiguous")
    value: dict[str, Any] = values[0]
    config, host, state = value["Config"], value["HostConfig"], value["State"]
    images = json.loads(reader(("docker", "image", "inspect", runtime.prepared_image), None))
    if len(images) != 1 or images[0]["Id"] != value["Image"]:
        raise ValueError("prepared server does not run its retained immutable component image")
    expected_env = dict(item.split("=", 1) for item in images[0]["Config"].get("Env", []))
    expected_env.update(HOME="/tmp", LAUNCHPLANE_PREPARED_PUBLIC_ROOT="/prepared")
    mounts = value.get("Mounts", [])
    if (
        config["Image"] != runtime.prepared_image
        or config.get("Cmd") != list(PREPARED_COMMAND)
        or config.get("User") != "launchplane-browser"
        or dict(item.split("=", 1) for item in config.get("Env", [])) != expected_env
        or host.get("ReadonlyRootfs") is not True
        or host.get("Privileged") is not False
        or host.get("CapAdd")
        or set(host.get("CapDrop") or []) != {"ALL"}
        or "no-new-privileges:true" not in (host.get("SecurityOpt") or [])
        or host.get("PortBindings")
        or host.get("NetworkMode") == "host"
        or set(value["NetworkSettings"]["Networks"]) != {runtime.private_network}
        or len(mounts) != 1
        or mounts[0].get("Type") != "volume"
        or mounts[0].get("Name") != runtime.prepared_volume
        or mounts[0].get("Destination") != "/prepared"
        or mounts[0].get("RW") is not False
        or require_running
        and (state.get("Running") is not True or state.get("Restarting") is not False)
    ):
        raise ValueError("prepared server lacks its isolated, read-only retained lifecycle")
