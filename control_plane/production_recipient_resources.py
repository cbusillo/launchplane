"""Fixed host program for new empty production-recipient resources only."""

import base64
import json
from collections.abc import Callable
from pathlib import Path

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.production_recipient import ProductionRecipientRecord
from control_plane.dokploy import api, post_deploy, source
from control_plane.production_recipient_fence import fenced_program


def resource_spec(record: ProductionRecipientRecord) -> dict[str, object]:
    return {
        "namespace": record.namespace,
        "volumes": record.volumes,
        "ports": sorted(record.request.ports.values()),
        "binding": record.plan_sha256,
        "project": record.app_name,
    }


def inspect_resources(
    root: Path,
    record: ProductionRecipientRecord,
    *,
    prepare: bool,
    checkpoint: Callable[[str], None],
) -> None:
    host, token = source.read_dokploy_config(control_plane_root=root)
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=record.target_id
    )
    if str(payload.get("serverId") or "") != record.request.destination_server_id:
        raise ValueError("Recipient placement changed.")
    kind, lookup, _, server = post_deploy._resolve_dokploy_schedule_runtime(
        host=host,
        token=token,
        compose_id=record.target_id,
        compose_name=record.namespace,
        target_payload=payload,
    )
    spec = resource_spec(record)
    encoded = base64.b64encode(json.dumps({**spec, "prepare": prepare}).encode()).decode()
    evidence = post_deploy.run_manual_data_schedule(
        host=host,
        token=token,
        target_definition=source.DokployTargetDefinition(
            context=record.context, instance="prod", target_id=record.target_id
        ),
        schedule_lookup_id=lookup,
        schedule_type=kind,
        schedule_name="Launchplane production recipient resources",
        schedule_app_name=record.namespace + "-resources",
        schedule_payload={
            "name": "Launchplane production recipient resources",
            "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
            "appName": record.namespace + "-resources",
            "shellType": "bash",
            "scheduleType": kind,
            "command": "control-plane production recipient resources",
            "script": "#!/usr/bin/env bash\nset -euo pipefail\npython3 - "
            + encoded
            + " <<'LP_RESOURCES'\n"
            + fenced_program(RESOURCE_PROGRAM, record.namespace, record.plan_sha256)
            + "\nLP_RESOURCES\n",
            "serverId": server,
            "userId": lookup if kind == "dokploy-server" else None,
            "enabled": False,
            "timezone": "UTC",
        },
        resolved_schedule_execution_timeout_seconds=300,
        before_provider_mutation=checkpoint,
    )
    if evidence.get("production_recipient_resources_ok") != "true" or evidence.get(
        "production_recipient_resources_sha256"
    ) != canonical_json_sha256(spec):
        raise ValueError("Recipient storage and ports were not qualified on their actual host.")


RESOURCE_LIBRARY = r"""
import base64, hashlib, json, re, socket, subprocess, sys
from pathlib import Path

def docker(*args):
    result = subprocess.run(["docker", *args], capture_output=True, text=True, timeout=60)
    if result.returncode or len(result.stdout)>1024*1024:
        raise RuntimeError("resource operation failed")
    return result.stdout

def port_conflicts(binding, ports):
    value = str(binding.get("HostPort") or "")
    if not value:
        return False
    bounds = value.split("-")
    if len(bounds) not in (1, 2) or any(not bound.isdigit() for bound in bounds):
        raise ValueError("unqualified port inventory")
    low, high = int(bounds[0]), int(bounds[-1])
    if not 0 <= low <= high <= 65535:
        raise ValueError("unqualified port inventory")
    return any(low <= port <= high for port in ports)

def main():
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    namespace = spec["namespace"]
    if not re.fullmatch(r"lp-prod-recipient-[a-f0-9]{32}", namespace):
        raise ValueError("invalid namespace")
    if not re.fullmatch(re.escape(namespace) + r"(?:-[a-z0-9]{6})?", spec["project"]):
        raise ValueError("invalid project")
    if spec["volumes"] != {"database": namespace + "-db", "config": namespace + "-config"}:
        raise ValueError("invalid allocation")
    if not re.fullmatch(r"[a-f0-9]{64}", spec["binding"]) or any(type(port) is not int or not 1024 <= port <= 65535 for port in spec["ports"]):
        raise ValueError("invalid binding")
    names = set(docker("volume", "ls", "--format", "{{.Name}}").splitlines())
    paths = []
    identities = set()
    for role, name in spec["volumes"].items():
        labels = {"io.launchplane.production-recipient": spec["binding"], "io.launchplane.resource-role": role}
        if name not in names:
            if not spec["prepare"]:
                raise ValueError("missing allocation")
            args = ["volume", "create", "--driver", "local"]
            for key, value in labels.items():
                args.extend(["--label", key + "=" + value])
            docker(*args, name)
        rows = json.loads(docker("volume", "inspect", name))
        if len(rows) != 1:
            raise ValueError("ambiguous volume")
        row = rows[0]
        if row.get("Name") != name or row.get("Driver") != "local" or row.get("Options") not in (None, {}) or any((row.get("Labels") or {}).get(k) != v for k,v in labels.items()):
            raise ValueError("foreign allocation")
        path = Path(row["Mountpoint"])
        if not path.is_absolute() or path.is_symlink():
            raise ValueError("invalid backing")
        path = path.resolve(strict=True)
        if path.name != "_data" or path.parent.name != name or not path.is_dir() or (not spec.get("recover", False) and any(path.iterdir())):
            raise ValueError("nonempty allocation")
        identity = (path.stat().st_dev, path.stat().st_ino)
        if identity in identities:
            raise ValueError("aliased backing")
        identities.add(identity)
        paths.append(path)
    for identifier in docker("ps", "-aq").splitlines():
        rows = json.loads(docker("inspect", identifier))
        if len(rows) != 1:
            raise ValueError("incomplete mount inventory")
        row = rows[0]
        if (row.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project") == spec["project"]:
            raise ValueError("recipient already has containers")
        for inventory in ((row.get("HostConfig") or {}).get("PortBindings") or {}, (row.get("NetworkSettings") or {}).get("Ports") or {}):
            for bindings in inventory.values():
                if any(port_conflicts(binding, spec["ports"]) for binding in (bindings or [])):
                    raise ValueError("port already referenced")
        for mount in row["Mounts"]:
            if mount.get("Type") == "tmpfs":
                continue
            if mount.get("Name") in spec["volumes"].values():
                raise ValueError("storage already referenced")
            if not mount.get("Source"):
                continue
            source = Path(mount["Source"]).resolve()
            if mount.get("Type") == "bind" and mount.get("RW") is False and not any(source == path or path in source.parents for path in paths):
                # A host monitor's read-only parent is not a writable alias.
                # Exact resource paths and their children remain consumers.
                continue
            if any(source == path or path in source.parents or source in path.parents for path in paths):
                raise ValueError("storage already referenced")
    sockets = []
    try:
        for port in spec["ports"]:
            listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sockets.append(listener)
            listener.bind(("0.0.0.0", port))
    finally:
        for listener in sockets:
            listener.close()
    binding = {key:value for key,value in spec.items() if key != "prepare"}
    print("production_recipient_resources_sha256=" + hashlib.sha256(json.dumps(binding, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    print("production_recipient_resources_ok=true")
    return paths
"""

RESOURCE_PROGRAM = (
    RESOURCE_LIBRARY
    + r"""
try:
    main()
except Exception:
    print("production_recipient_resources_ok=false")
    sys.exit(1)
"""
)
