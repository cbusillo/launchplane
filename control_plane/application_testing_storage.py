"""Fixed read-only host proof for one never-deployed application allocation."""

import base64
import json
from collections.abc import Callable

from control_plane.application_state_volume import ApplicationStateVolumeAdapter
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.dokploy import post_deploy, source


def inspect_application_testing_storage(
    adapter: ApplicationStateVolumeAdapter, *, checkpoint: Callable[[str], None]
) -> None:
    """Dispatch only the fixed inspection, using original service-owned placement."""
    phase = json.loads(adapter._progress_phase())
    spec = {
        "plan_id": adapter.intent["plan_id"],
        "binding": adapter.intent["plan_sha256"],
        "volume": adapter.intent["volume_name"],
        "application": phase["app_name"],
        "application_id": phase["application"],
        "server": adapter.request.server_id,
    }
    live = adapter._live(phase["application"], phase["environment"], phase["app_name"])
    host, token = source.read_dokploy_config(control_plane_root=adapter.root)
    kind, lookup, _, server = post_deploy._resolve_dokploy_schedule_runtime(
        host=host,
        token=token,
        compose_id=phase["application"],
        compose_name=phase["app_name"],
        target_payload=live,
    )
    if kind != "server" or server != adapter.request.server_id:
        raise ValueError("Testing storage inspection requires its original recorded server.")
    encoded = base64.b64encode(json.dumps(spec).encode()).decode()
    schedule_name = "Launchplane fresh testing application storage inspection"
    # A server has one inspection slot per owned lane, reused after a replacement.
    # The caller holds the lane and current target's event-execution locks.
    schedule_app = (
        "lp-testing-storage-check-"
        + canonical_json_sha256([adapter.product, adapter.request.context, "testing"])[:32]
    )
    evidence = post_deploy.run_manual_data_schedule(
        host=host,
        token=token,
        target_definition=source.DokployTargetDefinition(
            context=adapter.request.context,
            instance="testing",
            target_type="application",
            target_id=phase["application"],
        ),
        schedule_lookup_id=lookup,
        schedule_type=kind,
        schedule_name=schedule_name,
        schedule_app_name=schedule_app,
        schedule_payload={
            "name": schedule_name,
            "appName": schedule_app,
            "scheduleType": kind,
            "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
            "shellType": "bash",
            "serverId": server,
            "enabled": False,
            "timezone": "UTC",
            "command": "control-plane fresh testing application storage inspection",
            "script": "#!/usr/bin/env bash\nset -euo pipefail\npython3 - "
            + encoded
            + " <<'LP_APPLICATION_STORAGE'\n"
            + APPLICATION_STORAGE_INSPECTION_PROGRAM
            + "\nLP_APPLICATION_STORAGE\n",
        },
        resolved_schedule_execution_timeout_seconds=300,
        before_provider_mutation=checkpoint,
    )
    if evidence.get("testing_resources_ok") != "true" or evidence.get(
        "testing_resources_binding_sha256"
    ) != canonical_json_sha256(spec):
        raise ValueError("Actual testing storage absence and isolation are unproved.")


APPLICATION_STORAGE_INSPECTION_PROGRAM = r"""
import base64, hashlib, json, re, subprocess, sys
from pathlib import Path

def docker(*arguments):
    result = subprocess.run(["docker", *arguments], capture_output=True, text=True, timeout=60)
    if result.returncode or len(result.stdout) > 1024 * 1024:
        raise ValueError("inventory unavailable")
    return result.stdout

def identifiers(*arguments):
    rows = docker(*arguments).splitlines()
    if len(rows) > 10000 or len(set(rows)) != len(rows) or any(not row.strip() for row in rows):
        raise ValueError("inventory incomplete")
    return sorted(rows)

try:
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    plan = spec["plan_id"]
    if (not re.fullmatch(r"[a-f0-9]{32}", plan)
        or spec["volume"] != "lp-testing-state-" + plan
        or not re.fullmatch(r"lp-testing-app-" + plan + r"-state(?:-[a-z0-9]{6})?", spec["application"])
        or not re.fullmatch(r"[a-f0-9]{64}", spec["binding"])
        or not spec["server"] or not spec["application_id"]):
        raise ValueError("invalid allocation")
    volumes = identifiers("volume", "ls", "-q")
    if spec["volume"] in volumes:
        raise ValueError("allocation volume already exists")
    root = Path(json.loads(docker("info", "--format", "{{json .DockerRootDir}}")))
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("unsupported docker root")
    root = root.resolve(strict=True)
    parent = root / "volumes"
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("unsupported volume root")
    allocation = parent / spec["volume"]
    if allocation.exists() or allocation.is_symlink():
        raise ValueError("allocation backing already exists")
    containers = identifiers("ps", "-aq", "--no-trunc")
    template = '{"id":{{json .Id}},"name":{{json .Name}},"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}}}'
    for identifier in containers:
        row = json.loads(docker("inspect", "--format", template, identifier))
        labels = row.get("labels")
        mounts = row.get("mounts")
        if (row.get("id") != identifier or not isinstance(row.get("name"), str)
            or not isinstance(labels, (dict, type(None))) or not isinstance(mounts, list)):
            raise ValueError("container inventory incomplete")
        if (row["name"].lstrip("/").startswith(spec["application"])
            or any(str(value).startswith(spec["application"]) for value in (labels or {}).values())):
            raise ValueError("allocation application already has containers")
        for mount in mounts:
            if not isinstance(mount, dict):
                raise ValueError("mount inventory incomplete")
            if mount.get("Type") == "tmpfs":
                continue
            if mount.get("Name") == spec["volume"]:
                raise ValueError("allocation already referenced")
            if not isinstance(mount.get("Source"), str) or not Path(mount["Source"]).is_absolute():
                raise ValueError("mount backing unavailable")
            source = Path(mount["Source"]).resolve()
            if mount.get("Type") == "bind" and mount.get("RW") is False and source in allocation.parents:
                # An administrative read-only parent cannot modify the allocation.
                # Exact resource paths and children remain consumers.
                continue
            if source == allocation or source in allocation.parents or allocation in source.parents:
                raise ValueError("allocation backing overlaps a container")
    if (identifiers("ps", "-aq", "--no-trunc") != containers or identifiers("volume", "ls", "-q") != volumes
        or allocation.exists() or allocation.is_symlink()):
        raise ValueError("inventory changed")
    print("testing_resources_binding_sha256=" + hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    print("testing_resources_ok=true")
except Exception:
    print("testing_resources_ok=false")
    sys.exit(1)
"""
