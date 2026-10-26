import base64
import json
import inspect
from collections.abc import Callable

from control_plane.application_state_volume import ApplicationStateVolumeAdapter
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.dokploy import post_deploy, source
from control_plane.testing_application_capabilities import single_local_swarm_node


def inspect_application_testing_storage(
    adapter: ApplicationStateVolumeAdapter,
    *,
    checkpoint: Callable[[str], None],
    serving_deployment: DeploymentRecord | None = None,
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
    if serving_deployment is not None:
        assert serving_deployment.artifact_identity is not None
        spec.update(
            image=serving_deployment.artifact_identity.artifact_id,
            deployment=serving_deployment.record_id,
            mount_path=adapter.request.state_volume_path,
        )
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
    schedule_name = "Launchplane testing application storage inspection"
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


APPLICATION_STORAGE_INSPECTION_PROGRAM = (
    "from collections.abc import Callable\nfrom typing import Any\nimport json\n"
    + inspect.getsource(single_local_swarm_node)
    + r"""
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
    serving = bool(spec.get("deployment"))
    if not serving and spec["volume"] in volumes:
        raise ValueError("allocation volume already exists")
    root = Path(json.loads(docker("info", "--format", "{{json .DockerRootDir}}")))
    if not root.is_absolute() or root.is_symlink():
        raise ValueError("unsupported docker root")
    root = root.resolve(strict=True)
    parent = root / "volumes"
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError("unsupported volume root")
    allocation = parent / spec["volume"]
    if not serving and (allocation.exists() or allocation.is_symlink()):
        raise ValueError("allocation backing already exists")
    service = volume = None
    if serving:
        nodes = single_local_swarm_node(docker)
        if spec["volume"] not in volumes or not spec.get("mount_path"):
            raise ValueError("serving state unavailable")
        volume = json.loads(docker("volume", "inspect", spec["volume"]))
        if not isinstance(volume, list) or len(volume) != 1:
            raise ValueError("volume identity unavailable")
        volume = volume[0]
        backing = allocation / "_data"
        if (volume.get("Name") != spec["volume"] or volume.get("Driver") != "local"
            or volume.get("Options") not in (None, {}) or volume.get("Mountpoint") != str(backing)
            or allocation.is_symlink() or backing.is_symlink() or not backing.is_dir()):
            raise ValueError("state is not isolated local storage")
        service = json.loads(docker("service", "inspect", spec["application"]))[0]
        current = service["Spec"]
        runtime = current["TaskTemplate"]["ContainerSpec"]
        image = runtime["Image"]
        def repository(reference):
            value = reference.split("@", 1)[0]
            return value.rsplit(":", 1)[0] if ":" in value.rsplit("/", 1)[-1] else value
        if (current.get("Name") != spec["application"]
            or current.get("Mode") != {"Replicated": {"Replicas": 1}}
            or service.get("Endpoint", {}).get("Ports") or current.get("EndpointSpec", {}).get("Ports")
            or (service.get("UpdateStatus") or {}).get("State", "") not in {"", "completed", "rollback_completed"}
            or image.rpartition("@")[2] != spec["image"].rpartition("@")[2]
            or repository(image) != repository(spec["image"])
            or [value for value in runtime.get("Env", []) if value.startswith("LAUNCHPLANE_DEPLOYMENT_RECORD_ID=")] != ["LAUNCHPLANE_DEPLOYMENT_RECORD_ID=" + spec["deployment"]]
            or len(runtime.get("Mounts") or []) != 1):
            raise ValueError("serving runtime changed")
        mount = runtime["Mounts"][0]
        if (mount.get("Type"), mount.get("Source"), mount.get("Target")) != ("volume", spec["volume"], spec["mount_path"]) or mount.get("ReadOnly") not in (None, False):
            raise ValueError("serving state mount changed")
        services = identifiers("service", "ls", "-q")
        service_rows = {}
        for identifier in services:
            other = json.loads(docker("service", "inspect", identifier))[0]
            service_rows[identifier] = other
            if other.get("ID") != service.get("ID"):
                for service_spec in (other["Spec"], other.get("PreviousSpec")):
                    if service_spec is None:
                        continue
                    for mount in service_spec["TaskTemplate"]["ContainerSpec"].get("Mounts") or []:
                        if mount.get("Source") == spec["volume"]:
                            raise ValueError("foreign service references state")
                        if mount.get("Type") == "bind":
                            source = Path(mount["Source"]).resolve()
                            if source == allocation or source in allocation.parents or allocation in source.parents:
                                raise ValueError("foreign service overlaps state")
    containers = identifiers("ps", "-aq", "--no-trunc")
    template = '{"id":{{json .Id}},"name":{{json .Name}},"labels":{{json .Config.Labels}},"mounts":{{json .Mounts}},"state":{{json .State.Status}},"env":{{json .Config.Env}},"image":{{json .Config.Image}},"ports":{{json .HostConfig.PortBindings}},"privileged":{{json .HostConfig.Privileged}}}'
    owned = running = 0
    container_rows = {}
    for identifier in containers:
        row = json.loads(docker("inspect", "--format", template, identifier))
        container_rows[identifier] = row
        labels = row.get("labels")
        mounts = row.get("mounts")
        if (row.get("id") != identifier or not isinstance(row.get("name"), str)
            or not isinstance(labels, (dict, type(None))) or not isinstance(mounts, list)):
            raise ValueError("container inventory incomplete")
        attributed = serving and (labels or {}).get("com.docker.swarm.service.id") == service["ID"]
        if attributed:
            if (labels or {}).get("com.docker.swarm.service.name") != spec["application"]:
                raise ValueError("container attribution changed")
            if len(mounts) != 1 or mounts[0].get("RW") is not True or (mounts[0].get("Type"), mounts[0].get("Name"), mounts[0].get("Destination"), mounts[0].get("Source")) != ("volume", spec["volume"], spec["mount_path"], str(allocation / "_data")):
                raise ValueError("serving container mounts changed")
            owned += 1
            if row.get("state") == "running":
                running += 1
                if ([value for value in row.get("env", []) if value.startswith("LAUNCHPLANE_DEPLOYMENT_RECORD_ID=")] != ["LAUNCHPLANE_DEPLOYMENT_RECORD_ID=" + spec["deployment"]]
                    or row.get("ports") or row.get("privileged") is not False):
                    raise ValueError("serving container authority changed")
                image_row = json.loads(docker("image", "inspect", row["image"]))[0]
                if spec["image"] not in image_row.get("RepoDigests", []):
                    raise ValueError("serving container image changed")
            continue
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
        or not serving and (allocation.exists() or allocation.is_symlink())):
        raise ValueError("inventory changed")
    if serving and (not owned or running != 1 or single_local_swarm_node(docker) != nodes
        or json.loads(docker("service", "inspect", spec["application"]))[0] != service
        or identifiers("service", "ls", "-q") != services
        or json.loads(docker("volume", "inspect", spec["volume"])) != [volume]):
        raise ValueError("serving resource inventory changed")
    if serving:
        for identifier, previous in service_rows.items():
            if json.loads(docker("service", "inspect", identifier))[0] != previous:
                raise ValueError("service inventory changed")
        for identifier, previous in container_rows.items():
            if json.loads(docker("inspect", "--format", template, identifier)) != previous:
                raise ValueError("container inventory changed")
    print("testing_resources_binding_sha256=" + hashlib.sha256(json.dumps(spec, sort_keys=True, separators=(",", ":")).encode()).hexdigest())
    print("testing_resources_ok=true")
except Exception:
    print("testing_resources_ok=false")
    sys.exit(1)
"""
)
