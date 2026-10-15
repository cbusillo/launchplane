"""Qualify the fixed connection-removal effect without declaring a copy disposable."""

from control_plane.disposable_lanes import (
    DisposableLaneSafetyUnknown,
    DisposableLaneStore,
    disposable_lane_binding_sha256,
    disposable_lane_configuration,
)
from control_plane.dokploy.compose import ODOO_DATABASE_DATA_DIRECTORY


def require_connection_isolation_lane(
    store: DisposableLaneStore, *, product: str, context: str, instance: str
) -> str:
    profile = store.read_product_profile_record(product)
    if (
        instance != "testing"
        or sum((lane.context, lane.instance) == (context, instance) for lane in profile.lanes) != 1
    ):
        raise DisposableLaneSafetyUnknown("Connection isolation requires the exact testing lane.")
    target = store.read_dokploy_target_record(context_name=context, instance_name=instance)
    target_id = store.read_dokploy_target_id_record(context_name=context, instance_name=instance)
    if target.target_type != "compose" or not target_id.target_id:
        raise DisposableLaneSafetyUnknown("Connection isolation requires a tracked compose target.")
    if target.policies.staff_testing_hold is not None:
        raise DisposableLaneSafetyUnknown("The lane is held for staff testing.")
    if any(
        (other.context, other.instance) != (context, instance)
        and other.target_id == target_id.target_id
        for other in store.list_dokploy_target_id_records()
    ):
        raise DisposableLaneSafetyUnknown("The target aliases another lane.")
    protection = store.read_disposable_lane_safety(product, context, instance)
    # This program preserves application records and never copies/rebuilds data.
    # It does not turn absent or stale observations into unused/reproducible facts.
    # Known exceptions remain protective even after their observation expires.
    if protection is not None and (
        protection.usage == "in_use"
        or protection.data == "only_copy"
        or protection.production_resources in {"shared", "separation_ready"}
    ):
        raise DisposableLaneSafetyUnknown(
            "A recorded use, needed-copy or sharing protection applies."
        )
    return disposable_lane_binding_sha256(
        profile,
        target,
        target_id,
        configuration=disposable_lane_configuration(store, context, instance),
    )


# Executed inside the lane runner. Inputs are Docker metadata, never SQL or
# caller-provided commands. It emits only fixed refusal names and a digest.
RESOURCE_QUALIFICATION_LIBRARY = (
    f"DATABASE_DATA_DIRECTORY = {ODOO_DATABASE_DATA_DIRECTORY!r}\n"
    + r"""
import hashlib
import ipaddress
import json
from pathlib import PurePosixPath


def qualify_resources(snapshot, project, database, context, runtime_identity):
    containers = snapshot["containers"]
    owned = {}
    for item in containers:
        if item["project"] != project:
            continue
        service = item["service"]
        if service in {"web", "database", "script-runner"}:
            if service in owned:
                raise ValueError("testing_resources/ambiguous_service")
            owned[service] = item
        elif item["running"]:
            raise ValueError("testing_resources/unknown_writer")
    if set(owned) != {"web", "database", "script-runner"}:
        raise ValueError("testing_resources/missing_service")
    for service in ("web", "script-runner"):
        environment = owned[service]["environment"]
        if (environment.get("PLATFORM_INSTANCE") != "testing"
            or environment.get("PLATFORM_CONTEXT") != context
            or environment.get("ODOO_DB_NAME") != database
            or environment.get("ODOO_DB_HOST") != "database"
            or environment.get("ODOO_DB_PORT", "5432") != "5432"):
            raise ValueError("testing_resources/database_identity")
        if (not runtime_identity or json.loads(environment.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", "null"))
            != json.loads(runtime_identity)):
            raise ValueError("testing_resources/runtime_identity")
    if owned["web"]["image"] != owned["script-runner"]["image"]:
        raise ValueError("testing_resources/image_identity")
    db = owned["database"]
    runner = owned["script-runner"]
    if not db["running"] or not runner["running"] or db["ports"]:
        raise ValueError("testing_resources/database_endpoint")
    common = set(db["networks"]) & set(runner["networks"])
    networks = {item["id"]: item for item in snapshot["networks"]}
    if len(common) != 1 or set(db["networks"]) != common:
        raise ValueError("testing_resources/database_network")
    network_id = next(iter(common))
    network = networks[network_id]
    owned_ids = {item["id"] for item in owned.values()}
    if (network["project"] != project or not set(network["members"]) <= owned_ids
        or set(network["members"]) - {owned["web"]["id"]} != {db["id"], runner["id"]}):
        raise ValueError("testing_resources/shared_network")
    address = str(ipaddress.IPv4Address(db["networks"][network_id]))
    if not address or address == "0.0.0.0":
        raise ValueError("testing_resources/database_endpoint")
    names = {mount["Name"] for item in owned.values() for mount in item["mounts"]
             if mount["RW"] and mount["Type"] == "volume"}
    volumes = {item["name"]: item for item in snapshot["volumes"] if item["name"] in names}
    writable = []
    for item in owned.values():
        for mount in item["mounts"]:
            if not mount["RW"]:
                continue
            if mount["Type"] != "volume":
                raise ValueError("testing_resources/unsupported_mount")
            volume = volumes[mount["Name"]]
            if (volume["driver"] != "local" or volume["has_options"] is not False
                or volume["mountpoint"] != mount["Source"]
                or not PurePosixPath(mount["Source"]).is_absolute()):
                raise ValueError("testing_resources/unsupported_volume")
            writable.append(mount)
    if not any(
        mount["RW"] and mount["Destination"] == DATABASE_DATA_DIRECTORY
        for mount in db["mounts"]
    ):
        raise ValueError("testing_resources/database_storage")
    for other in containers:
        if other["id"] in owned_ids:
            continue
        for mount in other["mounts"]:
            if not mount["RW"] or not mount.get("Source"):
                continue
            path = PurePosixPath(mount["Source"])
            for own in writable:
                own_path = PurePosixPath(own["Source"])
                if path == own_path or path in own_path.parents or own_path in path.parents:
                    raise ValueError("testing_resources/shared_storage")
    # State/health and unrelated containers do not change the reviewed resources.
    binding = {
        "containers": {
            service: {
                **{key: item[key] for key in ("id", "image", "ports", "environment")},
                "mounts": sorted(item["mounts"], key=lambda mount: (mount["Destination"], mount.get("Name", ""))),
            }
            for service, item in owned.items()
        },
        "network": {**network, "members": sorted(network["members"])},
        "database_address": address,
        "volumes": sorted(volumes.values(), key=lambda item: item["name"]),
    }
    digest = hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return digest, address
"""
)

RESOURCE_QUALIFICATION_PROGRAM = (
    RESOURCE_QUALIFICATION_LIBRARY
    + r"""
import os
import sys

project, database, context, runtime_identity, reviewed = sys.argv[1:]
try:
    raw = sys.stdin.read(4 * 1024 * 1024 + 1)
    if len(raw) > 4 * 1024 * 1024:
        raise ValueError("testing_resources/inventory_limit")
    rows = [json.loads(line) for line in raw.splitlines() if line.strip()]
    snapshot = {"containers": [], "volumes": [], "networks": []}
    environments = {}
    keys = {"PLATFORM_CONTEXT", "PLATFORM_INSTANCE", "ODOO_DB_NAME", "ODOO_DB_HOST", "ODOO_DB_PORT", "LAUNCHPLANE_RUNTIME_IDENTITY_JSON"}
    for row in rows:
        kind = row.pop("kind")
        if kind == "environment":
            environments[row["id"]] = {
                key: value for raw_value in row["environment"]
                for key, _, value in [raw_value.partition("=")] if key in keys
            }
        elif kind == "container":
            row["networks"] = {item["NetworkID"]: item["IPAddress"] for item in row["networks"].values()}
            snapshot["containers"].append(row)
        elif kind == "volume":
            snapshot["volumes"].append(row)
        elif kind == "network":
            row["members"] = sorted((row["members"] or {}).keys())
            snapshot["networks"].append(row)
        else:
            raise ValueError("testing_resources/inventory_shape")
    for item in snapshot["containers"]:
        item["environment"] = environments.get(item["id"], {})
    digest, address = qualify_resources(snapshot, project, database, context, runtime_identity)
    if reviewed and digest != reviewed:
        raise ValueError("testing_resources/review_binding")
    print(digest + " " + address)
except (KeyError, TypeError, ValueError) as error:
    name = str(error)
    if not name.startswith("testing_resources/") or any(character not in "abcdefghijklmnopqrstuvwxyz_/" for character in name):
        name = "testing_resources/inventory_unverified"
    print("connection_isolation_resources_ok=false", file=sys.stderr)
    print("integration_readback_refused=" + name, file=sys.stderr)
    raise SystemExit(1)
"""
)


def resource_inventory_script(*, program: str) -> str:
    """Only the two selected testing containers supply environment metadata."""
    import shlex

    return r"""
inventory_refused() {
    printf 'connection_isolation_resources_ok=false\nintegration_readback_refused=testing_resources/%s\n' "$1" >&2
    exit 1
}
mapfile -t inventory_ids < <(docker ps -aq | sort)
[ "${#inventory_ids[@]}" -gt 0 ] || inventory_refused inventory_unverified
mapfile -t volume_ids < <(docker volume ls -q | sort)
mapfile -t network_ids < <(docker network ls -q | sort)
snapshot=$(set -e
    docker inspect --format '{"kind":"container","id":{{json .Id}},"project":{{json (index .Config.Labels "com.docker.compose.project")}},"service":{{json (index .Config.Labels "com.docker.compose.service")}},"image":{{json .Image}},"running":{{json .State.Running}},"mounts":{{json .Mounts}},"ports":{{json .HostConfig.PortBindings}},"networks":{{json .NetworkSettings.Networks}}}' "${inventory_ids[@]}" || exit 1
    docker inspect --format '{"kind":"environment","id":{{json .Id}},"environment":{{json .Config.Env}}}' "$web" "$runner" || exit 1
    if [ "${#volume_ids[@]}" -gt 0 ]; then
        docker volume inspect --format '{"kind":"volume","name":{{json .Name}},"driver":{{json .Driver}},"has_options":{{if .Options}}true{{else}}false{{end}},"mountpoint":{{json .Mountpoint}}}' "${volume_ids[@]}" || exit 1
    fi
    if [ "${#network_ids[@]}" -gt 0 ]; then
        docker network inspect --format '{"kind":"network","id":{{json .Id}},"project":{{json (index .Labels "com.docker.compose.project")}},"members":{{json .Containers}}}' "${network_ids[@]}" || exit 1
    fi
) || inventory_refused inventory_unverified
[ "$(docker ps -aq | sort)" = "$(printf '%s\n' "${inventory_ids[@]}")" ] || inventory_refused inventory_changed
""" + (
        'qualification=$(printf \'%s\n\' "$snapshot" | docker exec -i "$runner" python3 -I -c '
        + shlex.quote(program)
        + ' "$project" "$database" "$context" "$runtime_identity" "$reviewed_resources")\n'
        + 'read -r resource_digest database_address <<< "$qualification"\n'
        + '[[ "$resource_digest" =~ ^[a-f0-9]{64}$ ]] || exit 1\n'
        + 'printf "connection_isolation_resources_ok=true\\nconnection_isolation_resource_sha256=%s\\n" "$resource_digest"\n'
    )
