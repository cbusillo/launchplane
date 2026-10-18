"""Read and stop one recorded testing web process, retaining its database and data."""

from collections.abc import Callable
import inspect
from typing import Any

from control_plane.dokploy.native_copy_stream import _read_remote


def qualify_retained_copy(containers: list[dict[str, Any]], spec: dict[str, Any]) -> dict[str, Any]:
    """Runs on the managed host; no environment value leaves the host."""
    import hashlib
    import json

    project = spec["project"]
    members = [
        item
        for item in containers
        if (item.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project") == project
    ]
    web_members = [
        item
        for item in members
        if item["Config"]["Labels"].get("com.docker.compose.service") == "web"
    ]
    database_members = [
        item
        for item in members
        if item["Config"]["Labels"].get("com.docker.compose.service") == "database"
    ]
    if len(web_members) != 1 or len(database_members) != 1:
        raise ValueError("Retained testing container custody is unavailable")
    web = web_members[0]
    database = database_members[0]
    environment = dict(item.split("=", 1) for item in web["Config"].get("Env", []) if "=" in item)
    identity = json.loads(environment.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", "null"))
    if (
        not isinstance(identity, dict)
        or identity.get("context") != spec["context"]
        or identity.get("instance") != "testing"
        or environment.get("PLATFORM_INSTANCE") != "testing"
        or environment.get("ODOO_DB_HOST") != "database"
    ):
        raise ValueError("Retained testing runtime is not an isolated local database consumer")
    if spec.get("identity") is not None and identity != spec["identity"]:
        raise ValueError("Retained testing runtime changed")
    if spec.get("web_id") and web["Id"] != spec["web_id"]:
        raise ValueError("Retained testing web process changed")
    resources = []
    for member, destination in ((web, "/volumes/data"), (database, "/var/lib/postgresql/data")):
        mounts = [
            item for item in member.get("Mounts", []) if item.get("Destination") == destination
        ]
        if (
            len(mounts) != 1
            or mounts[0].get("Type") != "volume"
            or mounts[0].get("RW") is not True
            or not mounts[0].get("Name")
        ):
            raise ValueError("Retained testing data custody is unavailable")
        resources.append(
            {"container": member["Id"], "destination": destination, "volume": mounts[0]["Name"]}
        )
    volumes = {item["volume"] for item in resources}
    if len(volumes) != len(resources):
        raise ValueError("Retained testing resources are shared")
    local_keys = ("ODOO_DB_PASSWORD", "ODOO_ADMIN_PASSWORD", "ODOO_MASTER_PASSWORD")
    passwords = {environment[key] for key in local_keys if environment.get(key)}
    if len(passwords) != len(local_keys):
        raise ValueError("Retained testing local credentials are unavailable or reused")
    for other in containers:
        other_project = (other.get("Config", {}).get("Labels") or {}).get(
            "com.docker.compose.project"
        )
        if other_project == project:
            continue
        if any(item.get("Name") in volumes for item in other.get("Mounts", [])):
            raise ValueError("Retained testing storage is shared outside its project")
        if other_project in spec.get("independent_candidate_projects", []):
            continue
        other_environment = dict(
            item.split("=", 1) for item in other.get("Config", {}).get("Env", []) if "=" in item
        )
        if any(password in value for password in passwords for value in other_environment.values()):
            raise ValueError("Retained testing credentials are shared outside the admitted copies")
    binding = hashlib.sha256(
        json.dumps(resources, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if spec.get("resource_binding") and spec["resource_binding"] != binding:
        raise ValueError("Retained testing data custody changed")
    state = web.get("State", {}).get("Status")
    if state not in {"running", "exited"}:
        raise ValueError("Retained testing web state is unknown")
    return {
        "type": "complete",
        "web_id": web["Id"],
        "identity": identity,
        "resource_binding": binding,
        "stopped": state == "exited",
    }


def retained_copy_host_program() -> str:
    return (
        "import base64,json,subprocess,sys\nfrom typing import Any\n"
        + inspect.getsource(qualify_retained_copy)
        + r"""

def docker(*arguments):
    result=subprocess.run(['docker',*arguments],stdout=subprocess.PIPE,stderr=subprocess.PIPE,timeout=90,check=False)
    if result.returncode:
        if any(token in result.stderr.lower() for token in (b'permission denied',b'unauthorized',b'access denied')):
            raise PermissionError('Managed testing retirement was refused')
        raise RuntimeError('Managed testing retirement did not complete')
    return result.stdout

def inventory():
    identifiers=docker('ps','-aq').decode().splitlines()
    return json.loads(docker('inspect',*identifiers)) if identifiers else []

try:
    spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
    result=qualify_retained_copy(inventory(),spec)
    if spec['mode']=='stop':
        if not spec.get('web_id') or not spec.get('resource_binding') or not spec.get('identity'):
            raise ValueError('Retirement requires original physical custody')
        if not result['stopped']:
            docker('stop','--time','30',result['web_id'])
        result=qualify_retained_copy(inventory(),spec)
        if not result['stopped']:
            raise RuntimeError('Retained testing process is not stopped')
    elif spec['mode']!='read':
        raise ValueError('Unsupported retained testing operation')
    print(sys.argv[2]+json.dumps(result),flush=True)
except PermissionError:
    print(sys.argv[2]+json.dumps({'type':'denied'}),flush=True)
    sys.exit(1)
except Exception:
    print(sys.argv[2]+json.dumps({'type':'failed'}),flush=True)
"""
    )


def retained_copy_operation(
    *,
    host: str,
    token: str,
    server_id: str,
    payload: dict[str, Any],
    dispatch_guard: Callable[[Callable[[], None]], None] | None = None,
) -> dict[str, Any]:
    frames: list[dict[str, Any]] = []
    _read_remote(
        host=host,
        token=token,
        server_id=server_id,
        program=retained_copy_host_program(),
        payload=payload,
        receive=frames.append,
        timeout_seconds=180,
        dispatch_guard=dispatch_guard,
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise ValueError("Retained testing custody is unverified.")
    return frames[0]
