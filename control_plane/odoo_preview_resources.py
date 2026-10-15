"""Host-owned, value-free resource inspection for an existing Odoo preview."""

import base64
import hashlib
import json
import re
from collections.abc import Callable

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
    if latest != payload:
        raise click.ClickException("Preview target changed during resource inspection.")


# This program only inspects. It cannot create, start, stop, reset or delete a
# resource. It runs on the selected host, outside application containers; the
# schedule carries identifiers only, never copied environment/secret values.
PREVIEW_RESOURCE_PROGRAM = r"""
import base64
import hashlib
import json
from pathlib import Path
import subprocess
import sys


def docker(*arguments):
    result = subprocess.run(["docker", *arguments], check=False, capture_output=True, text=True)
    if result.returncode:
        raise RuntimeError("Resource inventory unavailable")
    return result.stdout


def one(*arguments):
    rows = json.loads(docker(*arguments))
    if not isinstance(rows, list) or len(rows) != 1 or not isinstance(rows[0], dict):
        raise RuntimeError("Incomplete resource inspection")
    return rows[0]


def main():
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    names = spec["volumes"]
    if len(names) != 3 or len(set(names)) != 3 or not spec["project"]:
        raise RuntimeError("Invalid inspection scope")
    paths = []
    identities = set()
    for name in names:
        volume = one("volume", "inspect", name)
        if volume.get("Name") != name or volume.get("Driver") != "local" or volume.get("Options") not in (None, {}):
            raise RuntimeError("Unsupported storage backing")
        raw = Path(volume["Mountpoint"])
        if not raw.is_absolute():
            raise RuntimeError("Unavailable backing path")
        path = raw.resolve(strict=True)
        if not path.is_dir() or path.name != "_data" or path.parent.name != name:
            raise RuntimeError("Unexpected backing path")
        identity = (path.stat().st_dev, path.stat().st_ino)
        if identity in identities:
            raise RuntimeError("Aliased storage")
        paths.append(path)
        identities.add(identity)
    own = 0
    ids = docker("ps", "-aq").splitlines()
    for identifier in ids:
        container = one("inspect", identifier)
        config = container.get("Config")
        labels = config.get("Labels") if isinstance(config, dict) else None
        if not isinstance(config, dict) or (labels is not None and not isinstance(labels, dict)):
            raise RuntimeError("Container ownership unavailable")
        ours = (labels or {}).get("com.docker.compose.project") == spec["project"]
        mounts = container.get("Mounts")
        if not isinstance(mounts, list):
            raise RuntimeError("Mount inventory unavailable")
        if ours:
            own += 1
            host = container.get("HostConfig")
            if not isinstance(host, dict) or host.get("PortBindings") not in (None, {}) or host.get("NetworkMode") == "host":
                raise RuntimeError("Preview has shared host networking")
        for mount in mounts:
            if mount.get("Type") == "tmpfs":
                continue
            raw = mount.get("Source")
            if not isinstance(raw, str) or not raw.startswith("/"):
                raise RuntimeError("Backing mount unavailable")
            path = Path(raw).resolve(strict=True)
            overlap = any(path == candidate or path in candidate.parents or candidate in path.parents for candidate in paths)
            if ours:
                if mount.get("RW") is not False and (mount.get("Type") != "volume" or mount.get("Name") not in names or path not in paths):
                    raise RuntimeError("Preview has an unexpected writable mount")
            elif overlap or mount.get("Name") in names:
                # A read-only administrative parent mount is not a consumer of
                # this volume; exact/child mounts and writable parents are.
                if mount.get("Type") == "bind" and mount.get("RW") is False and all(path != candidate and candidate not in path.parents for candidate in paths):
                    continue
                raise RuntimeError("Storage is used outside this preview")
    if not own or docker("ps", "-aq").splitlines() != ids:
        raise RuntimeError("Preview is absent or container inventory changed")
    digest = hashlib.sha256(json.dumps(spec, sort_keys=True).encode()).hexdigest()
    print("preview_resources_binding_sha256=" + digest)
    print("preview_resources_ok=true")


try:
    main()
except Exception:
    print("preview_resources_ok=false")
    sys.exit(1)
"""
