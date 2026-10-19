"""Read testing copy custody and private health without a provider mutation."""

import inspect
from collections.abc import Callable
from pathlib import Path
from control_plane._odoo_copy_runtime_guard import VOLUME_BACKING_INSPECTION_LIBRARY
from control_plane._odoo_testing_storage_program import FILESYSTEM_CAPACITY_LIBRARY
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
    services = [item["Config"]["Labels"].get("com.docker.compose.service") for item in members]
    if len(set(services)) != len(services) or any(
        service not in {"web", "database", "script-runner"} for service in services
    ):
        raise ValueError("Retained testing project contains an unowned service")
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
        resources.append({"destination": destination, "volume": mounts[0]["Name"]})
    volumes = {item["volume"] for item in resources}
    if len(volumes) != len(resources):
        raise ValueError("Retained testing resources are shared")
    for member in members:
        configured = dict(
            item.split("=", 1) for item in member.get("Config", {}).get("Env", []) if "=" in item
        )
        if configured.get("PLATFORM_INSTANCE") not in (None, "", "testing"):
            raise ValueError("The testing project contains another lane's process")
        for mount in member.get("Mounts", []):
            if mount.get("RW", True):
                if mount.get("Type") != "volume" or not mount.get("Name"):
                    raise ValueError("Testing retirement requires qualified named writable storage")
                volumes.add(mount["Name"])
    for other in containers:
        other_project = (other.get("Config", {}).get("Labels") or {}).get(
            "com.docker.compose.project"
        )
        if other_project != project and any(
            item.get("Name") in volumes for item in other.get("Mounts", [])
        ):
            raise ValueError("Retained testing storage is shared outside its project")
    binding = hashlib.sha256(
        json.dumps(
            {"data": resources, "writable_volumes": sorted(volumes)},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    if spec.get("resource_binding") and spec["resource_binding"] != binding:
        raise ValueError("Retained testing data custody changed")
    state = web.get("State", {}).get("Status")
    states = [item.get("State", {}).get("Status") for item in members]
    if any(
        value not in {"running", "exited", "created", "paused", "restarting", "dead"}
        for value in states
    ):
        raise ValueError("Retained testing process state is unknown")
    return {
        "type": "complete",
        "web_id": web["Id"],
        "identity": identity,
        "resource_binding": binding,
        "stopped": all(value in {"exited", "created"} for value in states),
        "web_stopped": state in {"exited", "created", "paused"},
        "volumes": sorted(volumes),
    }


RETAINED_PRIVATE_HEALTH_PROGRAM = r"""
import base64,json,sys
from urllib.request import HTTPRedirectHandler,ProxyHandler,Request,build_opener
class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self,req,fp,code,msg,headers,newurl):
        return None
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
url='http://127.0.0.1:'+str(spec['port'])+spec['path']
with build_opener(ProxyHandler({}),NoRedirect()).open(Request(url,method='GET'),timeout=5) as response:
    assert response.geturl()==url and 200<=response.status<300
    payload=json.loads(response.read(262144).decode())
assert isinstance(payload,dict) and payload.get('runtime_identity')==spec['identity']
"""


def native_storage_capacity(
    spec: dict[str, Any],
    docker: Callable[..., bytes],
    capacity: Callable[[list[tuple[Path, int]], int], bool],
) -> bool:
    """Prove fresh default-local allocation and overlay2 staging backing."""
    import json
    import re

    budget = spec["budget"]
    if set(budget) != {
        "database_bytes",
        "filestore_bytes",
        "staging_bytes",
        "reserve_bytes",
    } or any(type(value) is not int or value <= 0 for value in budget.values()):
        raise ValueError("Testing storage requirements are unavailable")
    namespace = spec["candidate_namespace"]
    if not re.fullmatch(r"lp-testing-[a-f0-9]{32}", namespace):
        raise ValueError("Testing allocation namespace is invalid")
    info = json.loads(docker("info", "--format", "{{json .}}"))
    if info.get("Driver") != "overlay2":
        raise ValueError("Testing staging allocation driver is unverified")
    root = Path(info["DockerRootDir"]).resolve(strict=True)
    volume_root, overlay_root = root / "volumes", root / "overlay2"
    for path in (volume_root, overlay_root):
        if path.resolve(strict=True) != path:
            raise ValueError("Testing allocation backing is redirected")
    # New overlay2 upper directories are made under this backing. A nested
    # mount on a diff/work directory would invalidate that topology; mounted
    # container merged views do not back writable-layer allocation.
    for line in Path("/proc/self/mountinfo").read_text().splitlines():
        fields = line.split()
        mount = Path(re.sub(r"\\([0-7]{3})", lambda item: chr(int(item[1], 8)), fields[4]))
        if mount != overlay_root and mount.is_relative_to(overlay_root):
            if mount.name != "merged" or fields[fields.index("-") + 1] != "overlay":
                raise ValueError("Testing staging allocation has an unsupported nested mount")
    demands = []
    for suffix, size in (("-db", budget["database_bytes"]), ("-data", budget["filestore_bytes"])):
        name = namespace + suffix
        if spec["candidate_allocated"]:
            volume = json.loads(docker("volume", "inspect", name))[0]
            if (
                volume.get("Name") != name
                or volume.get("Driver") != "local"
                or volume.get("Options") not in (None, {})
                or (volume.get("Labels") or {}).get("io.launchplane.testing-resources")
                != spec["candidate_resources_binding"]
            ):
                raise ValueError("Original testing allocation backing is unverified")
            backing = Path(volume["Mountpoint"]).resolve(strict=True)
        else:
            # Default local volumes allocate at these new names. A preexisting
            # path belongs to custody reconciliation, never a capacity guess.
            if (volume_root / name).exists():
                raise ValueError("Testing allocation path already exists")
            backing = volume_root
        demands.append((backing, size))
    demands.append((overlay_root, budget["staging_bytes"]))
    return capacity(demands, budget["reserve_bytes"])


def retained_copy_host_program() -> str:
    return (
        "import base64,hashlib,json,os,subprocess,sys\nfrom typing import Any\nfrom collections.abc import Callable\nfrom pathlib import Path\n"
        + VOLUME_BACKING_INSPECTION_LIBRARY
        + FILESYSTEM_CAPACITY_LIBRARY
        + inspect.getsource(native_storage_capacity)
        + inspect.getsource(qualify_retained_copy)
        + "\nHEALTH_PROGRAM="
        + repr(RETAINED_PRIVATE_HEALTH_PROGRAM)
        + "\n"
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
    containers=inventory()
    result=qualify_retained_copy(containers,spec)
    own={item['Id'] for item in containers if (item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')==spec['project']}
    backings=inspect_volume_backings(result['volumes'],containers,own)
    result['physical_binding']=hashlib.sha256(json.dumps({'logical':result['resource_binding'],'backings':backings},sort_keys=True,separators=(',',':')).encode()).hexdigest()
    if spec.get('physical_binding') and result['physical_binding']!=spec['physical_binding']:
        raise ValueError('Retained testing backing storage changed')
    if spec['mode']=='capacity':
        result['capacity']=native_storage_capacity(spec,docker,filesystem_capacity)
    elif spec['mode']=='readiness':
        if result['web_stopped'] or not spec.get('identity') or not spec.get('health_path','').startswith('/'):
            raise ValueError('Retained private health requires the original running process')
        web=next(item for item in inventory() if item['Id']==result['web_id'])
        environment=dict(item.split('=',1) for item in web['Config'].get('Env',[]) if '=' in item)
        port=environment.get('ODOO_HTTP_PORT') or '8069'
        if not port.isdigit() or not 0<int(port)<65536:
            raise ValueError('Retained private health port is invalid')
        health={'port':int(port),'path':spec['health_path'],'identity':spec['identity']}
        docker('exec',result['web_id'],'python3','-c',HEALTH_PROGRAM,base64.b64encode(json.dumps(health).encode()).decode())
        result['healthy']=True
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
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise ValueError("Retained testing custody is unverified.")
    return frames[0]
