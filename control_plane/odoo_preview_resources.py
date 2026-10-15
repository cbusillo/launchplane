"""Host-owned, value-free resource inspection for an existing Odoo preview."""

import base64
import hashlib
import json
import re
from collections.abc import Callable, Mapping

import click

from control_plane.dokploy import api, post_deploy, source


RESOURCE_KEYS = ("ODOO_DATA_VOLUME", "ODOO_DB_VOLUME", "ODOO_LOG_VOLUME")


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
) -> None:
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=compose_id
    )
    current = api.parse_dokploy_env_text(str(payload.get("env") or ""))
    volumes = [environment.get(key, "") for key in RESOURCE_KEYS]
    if (
        payload.get("composeId") != compose_id
        or len(set(volumes)) != len(RESOURCE_KEYS)
        or any(not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,200}", name) for name in volumes)
        or any(current.get(key) != environment.get(key) for key in RESOURCE_KEYS)
    ):
        raise click.ClickException(
            "Existing preview storage changed or is unavailable; refresh refused."
        )
    schedule_type, lookup_id, project, server_id = post_deploy._resolve_dokploy_schedule_runtime(
        host=host,
        token=token,
        compose_id=compose_id,
        compose_name=compose_name,
        target_payload=payload,
    )
    spec = {"compose_id": compose_id, "project": project, "volumes": volumes}
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
        raise click.ClickException("Preview resource inspection is already running.")
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
        evidence.get("preview_resources_ok") != "true"
        or evidence.get("preview_resources_binding_sha256") != digest
    ):
        raise click.ClickException("Existing preview resource inspection is unavailable or unsafe.")
    latest = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=compose_id
    )

    # No provider-side write is accepted as inspection. A changed placement or
    # environment needs a new read, not reuse of a successful old schedule log.
    def binding(value: Mapping[str, object]) -> tuple[object, ...]:
        env = api.parse_dokploy_env_text(str(value.get("env") or ""))
        return (
            value.get("composeId"),
            value.get("serverId"),
            value.get("appName"),
            *(env.get(key) for key in RESOURCE_KEYS),
        )

    if binding(latest) != binding(payload):
        raise click.ClickException("Preview target changed during resource inspection.")


# This program only inspects. It cannot create, start, stop, reset or delete a
# resource. It runs on the selected host, outside application containers; the
# schedule carries identifiers only, never copied environment/secret values.
PREVIEW_RESOURCE_PROGRAM = r"""
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


def one(*arguments):
    rows = json.loads(docker(*arguments))
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError("Incomplete resource inspection")
    return rows[0]


def container(identifier):
    result = subprocess.run(["docker", "inspect", identifier], check=False, capture_output=True, text=True, timeout=15)
    if result.returncode:
        if re.search(r"No such (?:container|object):", result.stderr, re.IGNORECASE):
            return None
        raise RuntimeError("Container inspection unavailable")
    rows = json.loads(result.stdout)
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError("Incomplete container inspection")
    return rows[0]


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
    if len(spec["volumes"]) != 3 or len(names) != 3 or not spec["project"]:
        raise RuntimeError("Invalid inspection scope")
    paths = {}

    def add_volume(name):
        if name in paths:
            return
        volume = one("volume", "inspect", name)
        # Docker's default local allocation has no remote/bind backing options.
        if volume.get("Name") != name or volume.get("Driver") != "local" or volume.get("Options") not in (None, {}):
            raise RuntimeError("Unsupported storage backing")
        path = backing_path(volume.get("Mountpoint"))
        if path.name != "_data" or path.parent.name != name or any(path == old or path in old.parents or old in path.parents for old in paths.values()):
            raise RuntimeError("Aliased storage backing")
        paths[name] = path

    for name in names:
        add_volume(name)
    # Re-read complete inventory to include new consumers. An unrelated helper
    # disappearing is gone, not evidence of a hidden writable consumer.
    for _ in range(2):
        containers = []
        own = 0
        for identifier in docker("ps", "-aq").splitlines():
            item = container(identifier)
            if item is None:
                continue
            config = item.get("Config")
            labels = config.get("Labels") if isinstance(config, dict) else None
            if not isinstance(config, dict) or (labels is not None and not isinstance(labels, dict)):
                raise RuntimeError("Container ownership unavailable")
            ours = (labels or {}).get("com.docker.compose.project") == spec["project"]
            mounts = item.get("Mounts")
            if not isinstance(mounts, list):
                raise RuntimeError("Mount inventory unavailable")
            if ours:
                own += 1
                host = item.get("HostConfig")
                if not isinstance(host, dict) or host.get("PortBindings") not in (None, {}) or host.get("NetworkMode") == "host":
                    raise RuntimeError("Preview has shared host networking")
                declared = config.get("Volumes") or {}
                if not isinstance(declared, dict):
                    raise RuntimeError("Image volume declarations unavailable")
                for mount in mounts:
                    name = mount.get("Name", "")
                    if mount.get("Type") == "volume" and name not in names:
                        if not re.fullmatch(r"[a-f0-9]{64}", name) or mount.get("Destination") not in declared:
                            raise RuntimeError("Unexpected preview volume")
                        add_volume(name)
            containers.append((ours, mounts))
        if not own:
            raise RuntimeError("Existing preview containers unavailable")
        for ours, mounts in containers:
            for mount in mounts:
                if mount.get("Type") == "tmpfs":
                    continue
                path = backing_path(mount.get("Source"))
                overlap = any(path == candidate or path in candidate.parents or candidate in path.parents for candidate in paths.values())
                if ours:
                    if mount.get("RW") is not False and (mount.get("Type") != "volume" or paths.get(mount.get("Name")) != path):
                        raise RuntimeError("Unexpected writable preview mount")
                elif overlap or mount.get("Name") in paths:
                    if mount.get("Type") == "bind" and mount.get("RW") is False and all(path != candidate and candidate not in path.parents for candidate in paths.values()):
                        continue
                    raise RuntimeError("Storage is used outside this preview")
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    print("preview_resources_binding_sha256=" + digest)
    print("preview_resources_ok=true")


def expired(*_args):
    raise TimeoutError("Inspection deadline")


old_handler = signal.signal(signal.SIGALRM, expired)
signal.alarm(100)
try:
    main()
except Exception:
    print("preview_resources_ok=false")
    sys.exit(1)
finally:
    signal.alarm(0)
    signal.signal(signal.SIGALRM, old_handler)
"""
