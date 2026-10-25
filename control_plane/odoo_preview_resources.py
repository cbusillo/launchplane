"""Host-owned, value-free resource inspection for an existing Odoo preview."""

import base64
import hashlib
import json
import re
from collections.abc import Callable, Mapping

import click
import yaml

from control_plane.dokploy import api, post_deploy, source
from control_plane.preview_filestore import PREVIEW_FILESTORE_LIBRARY
from control_plane.contracts.odoo_preview_runtime_plan import OdooPreviewRuntimeOperation
from control_plane.contracts.disposable_lane import (
    ODOO_PREVIEW_RESOURCES_OK_MARKER,
    ODOO_PREVIEW_RESOURCES_BINDING_MARKER,
)


PREVIEW_RESOURCE_SUFFIXES = (
    ("ODOO_DB_NAME", "db"),
    ("ODOO_DATA_VOLUME", "data"),
    ("ODOO_DB_VOLUME", "db_volume"),
    ("ODOO_LOG_VOLUME", "logs"),
)
RESOURCE_KEYS = tuple(key for key, _ in PREVIEW_RESOURCE_SUFFIXES if key.endswith("_VOLUME"))


def preview_resource_environment(compose_name: str) -> dict[str, str]:
    identifier = re.sub(r"[^a-zA-Z0-9]+", "_", compose_name.strip()).strip("_").lower()
    identifier = identifier or "odoo_preview"
    return {key: f"{identifier}_{suffix}" for key, suffix in PREVIEW_RESOURCE_SUFFIXES}


def require_existing_preview_resources(
    *,
    host: str,
    token: str,
    compose_id: str,
    compose_name: str,
    context: str,
    instance: str,
    environment: dict[str, str],
    checkpoint: Callable[[], None],
    operation: OdooPreviewRuntimeOperation = "refresh",
) -> str:
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=compose_id
    )
    binding = _preview_resource_binding(payload, compose_id=compose_id, environment=environment)
    if operation == "destroy":
        _require_preview_destroy_source(payload)
    volumes = [environment[key] for key in RESOURCE_KEYS]
    declaration = yaml.safe_load(str(payload.get("composeFile") or ""))
    if isinstance(declaration, dict) and "odoo_filestore" in declaration.get("volumes", {}):
        volumes.append(environment["ODOO_DATA_VOLUME"] + "-filestore")
    schedule_type, lookup_id, project, server_id = post_deploy._resolve_dokploy_schedule_runtime(
        host=host,
        token=token,
        compose_id=compose_id,
        compose_name=compose_name,
        target_payload=payload,
    )
    if operation == "destroy":
        volumes = _preview_destroy_volumes(payload, project=project, environment=environment)
    spec = {
        "compose_id": compose_id,
        "project": project,
        "volumes": volumes,
        "operation": operation,
    }
    encoded = base64.b64encode(json.dumps(spec, sort_keys=True).encode()).decode()
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    name = "Launchplane existing preview resource inspection"
    app_name = "lp-preview-inspect-" + hashlib.sha256(compose_id.encode()).hexdigest()[:24]
    existing = api.find_matching_dokploy_schedule(
        host=host,
        token=token,
        target_id=lookup_id,
        schedule_type=schedule_type,
        schedule_name=name,
        app_name=app_name,
    )
    if post_deploy._has_running_schedule_deployment(existing):
        # Inspection has no application effects. A stale provider job cannot
        # forbid a fresh read; preserve that job and use a separate disabled
        # schedule rather than cancelling it or clearing an effect fence.
        # Reuse one alternate for this exact stale job rather than accumulating
        # a new provider record on every retry. Concurrent read-only inspection
        # of the same immutable spec has no application effect.
        stale_jobs = sorted(
            api.deployment_key(job)
            for job in post_deploy._schedule_deployments(existing)
            if api._deployment_status(job) in post_deploy.DOKPLOY_RUNNING_DEPLOYMENT_STATUSES
        )
        stale_binding = json.dumps(stale_jobs, sort_keys=True, separators=(",", ":"))
        app_name += "-" + hashlib.sha256(stale_binding.encode()).hexdigest()[:12]
    checkpoint()
    evidence = post_deploy.run_manual_data_schedule(
        host=host,
        token=token,
        target_definition=source.DokployTargetDefinition(
            context=context,
            instance=instance,
            target_id=compose_id,
            target_name=compose_name,
        ),
        schedule_lookup_id=lookup_id,
        schedule_type=schedule_type,
        schedule_name=name,
        schedule_app_name=app_name,
        schedule_payload={
            "name": name,
            "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
            "appName": app_name,
            "shellType": "bash",
            "scheduleType": schedule_type,
            "command": "control-plane preview resource inspection",
            "script": "#!/usr/bin/env bash\nset -euo pipefail\npython3 - "
            + encoded
            + " <<'LP_PREVIEW_RESOURCES'\n"
            + PREVIEW_RESOURCE_PROGRAM
            + "\nLP_PREVIEW_RESOURCES\n",
            "serverId": server_id,
            "userId": lookup_id if schedule_type == "dokploy-server" else None,
            "enabled": False,
            "timezone": "UTC",
        },
        resolved_schedule_execution_timeout_seconds=120,
        before_provider_mutation=lambda _phase: checkpoint(),
    )
    checkpoint()
    if (
        evidence.get(ODOO_PREVIEW_RESOURCES_OK_MARKER) != "true"
        or evidence.get(ODOO_PREVIEW_RESOURCES_BINDING_MARKER) != digest
    ):
        raise click.ClickException("Existing preview resource inspection is unavailable or unsafe.")
    latest = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=compose_id
    )

    if _preview_resource_binding(latest, compose_id=compose_id, environment=environment) != binding:
        raise click.ClickException("Preview target changed during resource inspection.")
    return binding


def require_preview_resource_binding(
    *, host: str, token: str, compose_id: str, environment: dict[str, str], binding: str
) -> None:
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=compose_id
    )
    if (
        _preview_resource_binding(payload, compose_id=compose_id, environment=environment)
        != binding
    ):
        raise click.ClickException("Preview target changed after resource inspection.")


def _preview_resource_binding(
    payload: Mapping[str, object], *, compose_id: str, environment: dict[str, str]
) -> str:
    current = api.parse_dokploy_env_text(str(payload.get("env") or ""))
    volumes = [environment.get(key, "") for key in RESOURCE_KEYS]
    if (
        payload.get("composeId") != compose_id
        or len(set(volumes)) != len(RESOURCE_KEYS)
        or any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,200}", name) for name in volumes)
        or any(current.get(key) != environment.get(key) for key in RESOURCE_KEYS)
    ):
        raise click.ClickException("Existing preview storage changed or is unavailable.")
    fields = (
        "composeId",
        "serverId",
        "appName",
        "sourceType",
        "composePath",
        "composeFile",
        "composeType",
        "randomize",
        "isolatedDeployment",
    )
    value = {key: payload.get(key) for key in fields}
    value["volumes"] = volumes
    value["compose_environment"] = {
        key: value for key, value in current.items() if key.startswith("COMPOSE_")
    }
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def _require_preview_destroy_source(payload: Mapping[str, object]) -> None:
    if (
        payload.get("sourceType") != "raw"
        or payload.get("composePath") != "docker-compose.yml"
        or payload.get("randomize")
    ):
        raise click.ClickException("Preview destruction source is unavailable or unsupported.")
    try:
        actual = yaml.safe_load(str(payload.get("composeFile") or ""))
    except yaml.YAMLError:
        raise click.ClickException("Preview destruction source is invalid.") from None
    declarations = actual.get("volumes") if isinstance(actual, dict) else None
    allowed_names = {"${" + key + ":?missing}" for key in RESOURCE_KEYS}
    explicit_names: set[str] = set()
    if not isinstance(declarations, dict) or not declarations:
        raise click.ClickException("Preview destruction storage declarations are unavailable.")
    for key, declaration in declarations.items():
        if declaration in (None, {}):
            continue
        if key == "odoo_filestore":
            environment = api.parse_dokploy_env_text(str(payload.get("env") or ""))
            if (
                not isinstance(declaration, dict)
                or set(declaration) != {"name", "driver", "driver_opts", "labels"}
                or declaration.get("name") != environment.get("ODOO_DATA_VOLUME", "") + "-filestore"
                or declaration.get("driver") != "local"
                or not isinstance(declaration.get("driver_opts"), dict)
                or declaration["driver_opts"].get("type") != "overlay"
            ):
                raise click.ClickException(
                    "Preview filestore declaration is unsafe or unsupported."
                )
            continue
        if (
            not isinstance(declaration, dict)
            or set(declaration) != {"name"}
            or not isinstance(declaration["name"], str)
            or declaration["name"] not in allowed_names
            or declaration["name"] in explicit_names
        ):
            raise click.ClickException(
                "Preview destruction storage declarations are unsafe or unsupported."
            )
        explicit_names.add(declaration["name"])
    if (
        not isinstance(actual, dict)
        or "include" in actual
        or any(
            key.startswith("COMPOSE_")
            for key in api.parse_dokploy_env_text(str(payload.get("env") or ""))
        )
        or not isinstance(actual.get("services"), dict)
        or any(
            not isinstance(service, dict) or "extends" in service
            for service in actual["services"].values()
        )
    ):
        raise click.ClickException(
            "Preview destruction storage declarations are unsafe or unsupported."
        )


def _preview_destroy_volumes(
    payload: Mapping[str, object], *, project: str, environment: dict[str, str]
) -> list[str]:
    declarations = yaml.safe_load(str(payload["composeFile"]))["volumes"]
    explicit = {"${" + key + ":?missing}": environment[key] for key in RESOURCE_KEYS}
    return [
        (
            environment["ODOO_DATA_VOLUME"] + "-filestore"
            if key == "odoo_filestore"
            else explicit[declaration["name"]]
        )
        if declaration
        else f"{project}_{key}"
        for key, declaration in declarations.items()
    ]


# This program only inspects. It cannot create, start, stop, reset or delete a
# resource. It runs on the selected host, outside application containers; the
# schedule carries identifiers only, never copied environment/secret values.
PREVIEW_RESOURCE_PROGRAM = (
    PREVIEW_FILESTORE_LIBRARY
    + f"OK_MARKER = {ODOO_PREVIEW_RESOURCES_OK_MARKER!r}\nBINDING_MARKER = {ODOO_PREVIEW_RESOURCES_BINDING_MARKER!r}\n"
    + r"""
import base64
import hashlib
import json
from pathlib import PurePosixPath
import re
import signal
import subprocess
import sys


def docker(*arguments):
    result = subprocess.run(["docker", *arguments], check=False, capture_output=True, text=True, timeout=15)
    if result.returncode:
        raise RuntimeError("Resource inventory unavailable")
    return result.stdout


# Query only needed metadata. Do not collect container environment values,
# arbitrary labels, or driver options that may hold credentials.
VOLUME_FORMAT = '{"Name":{{json .Name}},"Driver":{{json .Driver}},"HasOptions":{{if .Options}}true{{else}}false{{end}},"Device":{{if .Options}}{{json (index .Options "device")}}{{else}}null{{end}},"Overlay":{{if .Options}}{{if eq (index .Options "type") "overlay"}}{{json .Options}}{{else}}null{{end}}{{else}}null{{end}},"Anonymous":{{$anon := false}}{{range $key,$value := .Labels}}{{if eq $key "com.docker.volume.anonymous"}}{{$anon = true}}{{end}}{{end}}{{$anon}},"Mountpoint":{{json .Mountpoint}}}'
CONTAINER_FORMAT = '{"Id":{{json .Id}},"Config":{"Labels":{"com.docker.compose.project":{{if (index .Config "Labels")}}{{json (index (index .Config "Labels") "com.docker.compose.project")}}{{else}}null{{end}}},"Volumes":{{json (index .Config "Volumes")}}},"HostConfig":{"PortBindings":{{json .HostConfig.PortBindings}},"NetworkMode":{{json .HostConfig.NetworkMode}}},"Mounts":{{json .Mounts}}}'


volumes = {}

def volume(name):
    if not isinstance(name, str) or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*", name):
        raise RuntimeError("Volume identity unavailable")
    if name not in volumes:
        item = json.loads(docker("volume", "inspect", "--format", VOLUME_FORMAT, name))
        if not isinstance(item, dict) or item.get("Name") != name:
            raise RuntimeError("Incomplete volume inspection")
        volumes[name] = item
    return volumes[name]


def container_inventory():
    identifiers = docker("ps", "-aq", "--no-trunc").splitlines()
    if not identifiers:
        return []
    result = subprocess.run(["docker", "inspect", "--format", CONTAINER_FORMAT, *identifiers], check=False, capture_output=True, text=True, timeout=15)
    if result.returncode and (not result.stderr.strip() or any(not re.search(r"No such (?:container|object):", line, re.IGNORECASE) for line in result.stderr.splitlines() if line.strip())):
        raise RuntimeError("Container inspection unavailable")
    rows = [json.loads(line) for line in result.stdout.splitlines() if line.strip()]
    found = [item.get("Id") for item in rows if isinstance(item, dict)]
    if len(found) != len(rows) or len(set(found)) != len(found) or not set(found).issubset(identifiers) or (not result.returncode and set(found) != set(identifiers)):
        raise RuntimeError("Incomplete container inventory")
    return rows


def backing_path(value):
    # These are the Docker daemon's Linux paths, not the schedule's local paths.
    if not isinstance(value, str) or not value.startswith("/"):
        raise RuntimeError("Backing path unavailable")
    path = PurePosixPath(value)
    if ".." in path.parts:
        raise RuntimeError("Backing path is not canonical")
    return path


def main():
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    names = set(spec["volumes"])
    if not names or len(names) != len(spec["volumes"]) or not spec["project"]:
        raise RuntimeError("Invalid inspection scope")
    paths = {}

    def add_volume(name):
        if name in paths:
            return
        item = volume(name)
        overlay = name.endswith('-filestore') and item.get('Driver') == 'local' and item.get('HasOptions')
        if overlay:
            _, _, _, private = inspect_preview_filestore(name.removesuffix('-filestore'), spec['project'])
            paths[name + '-writable'] = backing_path(str(private))
        # Docker's default local allocation has no remote/bind backing options.
        if not overlay and (item.get("Driver") != "local" or item.get("HasOptions") is not False):
            raise RuntimeError("Unsupported storage backing")
        path = backing_path(item.get("Mountpoint"))
        if path.name != "_data" or path.parent.name != name or any(path == old or path in old.parents or old in path.parents for old in paths.values()):
            raise RuntimeError("Aliased storage backing")
        paths[name] = path

    destroying = spec.get("operation") == "destroy"
    existing_names = set(docker("volume", "ls", "--format", "{{.Name}}").splitlines()) if destroying else names
    for name in names:
        if name in existing_names:
            add_volume(name)

    def resolved_backing(path, seen=frozenset()):
        # A bind-backed volume can outlive an intermediate container/unmount.
        # Follow daemon-owned device records, including unmounted volumes.
        for root in {item.parent.parent for item in paths.values()}:
            if root not in path.parents:
                continue
            relative = path.relative_to(root)
            name = relative.parts[0]
            item = volume(name)
            mountpoint = backing_path(item.get("Mountpoint"))
            if item.get("Driver") != "local" or mountpoint.parent.parent != root:
                raise RuntimeError("Indirect backing identity unavailable")
            if item.get("HasOptions"):
                if name in seen:
                    raise RuntimeError("Cyclic backing identity")
                if item.get('Overlay'):
                    return path
                device = backing_path(item.get("Device"))
                tail = path.relative_to(mountpoint) if path == mountpoint or mountpoint in path.parents else PurePosixPath()
                return resolved_backing(device / tail, seen | {name})
        return path

    # Re-read complete inventory to include new consumers. An unrelated helper
    # disappearing is gone, not evidence of a hidden writable consumer.
    for _ in range(2):
        containers = []
        for item in container_inventory():
            config = item.get("Config")
            labels = config.get("Labels") if isinstance(config, dict) else None
            if not isinstance(config, dict) or (labels is not None and not isinstance(labels, dict)):
                raise RuntimeError("Container ownership unavailable")
            ours = (labels or {}).get("com.docker.compose.project") == spec["project"]
            mounts = item.get("Mounts")
            if not isinstance(mounts, list):
                raise RuntimeError("Mount inventory unavailable")
            if ours:
                host = item.get("HostConfig")
                if not destroying and (not isinstance(host, dict) or host.get("PortBindings") not in (None, {}) or host.get("NetworkMode") == "host"):
                    raise RuntimeError("Preview has shared host networking")
                declared = config.get("Volumes") or {}
                if not isinstance(declared, dict):
                    raise RuntimeError("Image volume declarations unavailable")
                for mount in mounts:
                    name = mount.get("Name", "")
                    if mount.get("Type") == "volume" and name not in names:
                        destination = mount.get("Destination")
                        # Compose carries image-created anonymous volumes into
                        # HostConfig.Mounts during recreation. Docker's own
                        # anonymous-volume marker survives that reuse; a hash
                        # name or mount syntax alone is not provenance.
                        if not re.fullmatch(r"[a-f0-9]{64}", name) or destination not in declared or volume(name).get("Anonymous") is not True:
                            raise RuntimeError("Unexpected preview volume")
                        add_volume(name)
            containers.append((ours, mounts))
        for ours, mounts in containers:
            for mount in mounts:
                if mount.get("Type") == "tmpfs":
                    continue
                path = backing_path(mount.get("Source"))
                if not ours and mount.get("Type") == "bind":
                    path = resolved_backing(path)
                overlap = any(path == candidate or path in candidate.parents or candidate in path.parents for candidate in paths.values())
                if ours:
                    if not destroying and mount.get("RW") is not False and (mount.get("Type") != "volume" or paths.get(mount.get("Name")) != path):
                        raise RuntimeError("Unexpected writable preview mount")
                else:
                    device_overlap = False
                    if mount.get("Type") == "volume":
                        foreign = volume(mount.get("Name"))
                        device = foreign.get("Device")
                        if isinstance(device, str) and device.startswith("/"):
                            device = resolved_backing(backing_path(device))
                            device_overlap = any(device == candidate or device in candidate.parents or candidate in device.parents for candidate in paths.values())
                    if mount.get('Type') == 'volume' and foreign.get('Overlay'):
                        referenced = volume_storage_paths({'Mountpoint': foreign['Mountpoint'], 'Options': foreign['Overlay']})
                        device_overlap = device_overlap or any(
                            reference == candidate or reference in candidate.parents or candidate in reference.parents
                            for reference in map(lambda item: backing_path(str(item)), referenced)
                            for candidate in paths.values())
                    if device_overlap or overlap or mount.get("Name") in names or mount.get("Name") in paths:
                        if not device_overlap and mount.get("Type") == "bind" and mount.get("RW") is False and all(path != candidate and candidate not in path.parents for candidate in paths.values()):
                            continue
                        raise RuntimeError("Storage is used outside this preview")
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    print(BINDING_MARKER + "=" + digest)
    print(OK_MARKER + "=true")


def expired(*_args):
    raise TimeoutError("Inspection deadline")


old_handler = signal.signal(signal.SIGALRM, expired)
signal.alarm(100)
try:
    main()
except Exception:
    print(OK_MARKER + "=false")
    sys.exit(1)
finally:
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old_handler)
"""
)
