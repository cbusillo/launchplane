import inspect
from control_plane._odoo_copy_runtime_guard import VOLUME_BACKING_INSPECTION_LIBRARY
from typing import Any

from control_plane.dokploy.native_copy_stream import _read_remote


def require_complete_container_inventory(containers: list[dict[str, Any]]) -> None:
    for item in containers:
        config, state, mounts = item.get("Config"), item.get("State"), item.get("Mounts")
        if (
            not isinstance(config, dict)
            or "Labels" not in config
            or (config["Labels"] is not None and not isinstance(config["Labels"], dict))
            or not isinstance(state, dict)
            or not isinstance(state.get("Status"), str)
            or not isinstance(mounts, list)
        ):
            raise ValueError("Managed container ownership and storage inventory is incomplete")
        for mount in mounts:
            if (
                not isinstance(mount, dict)
                or not isinstance(mount.get("Type"), str)
                or not isinstance(mount.get("RW"), bool)
                or not isinstance(mount.get("Source"), str)
                or (mount["Type"] == "volume" and not mount.get("Name"))
            ):
                raise ValueError("Managed writable storage inventory is incomplete")


def retained_inventory_sha256(containers: list[dict[str, Any]], spec: dict[str, Any]) -> str:
    import hashlib
    import json

    members = [
        {
            "id": item["Id"],
            "labels": item.get("Config", {}).get("Labels"),
            "mounts": item.get("Mounts"),
        }
        for item in containers
        if (item.get("Config", {}).get("Labels") or {}).get("com.docker.compose.project")
        == spec["project"]
    ]
    return hashlib.sha256(
        json.dumps(
            {"project": spec["project"], "members": sorted(members, key=lambda item: item["id"])},
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


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
    if not members and spec.get("allow_unavailable") is True:
        return {
            "type": "complete",
            "availability": "absent",
            "stopped": True,
            "web_stopped": True,
            "volumes": [],
        }
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
    states = [item.get("State", {}).get("Status") for item in members]
    stopped = all(value in {"exited", "created"} for value in states)
    environment = dict(item.split("=", 1) for item in web["Config"].get("Env", []) if "=" in item)
    identity = json.loads(environment.get("LAUNCHPLANE_RUNTIME_IDENTITY_JSON", "null"))
    if (
        (
            not isinstance(identity, dict)
            and not (identity is None and stopped and spec.get("allow_unavailable") is True)
        )
        or isinstance(identity, dict)
        and (identity.get("context"), identity.get("instance")) != (spec["context"], "testing")
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
        "stopped": stopped,
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


def retained_copy_host_program() -> str:
    return (
        "import base64,hashlib,json,subprocess,sys\nfrom typing import Any\nfrom pathlib import Path\n"
        + VOLUME_BACKING_INSPECTION_LIBRARY
        + inspect.getsource(qualify_retained_copy)
        + inspect.getsource(retained_inventory_sha256)
        + inspect.getsource(require_complete_container_inventory)
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
    identifiers=docker('ps','-aq','--no-trunc').decode().splitlines()
    items=json.loads(docker('inspect',*identifiers)) if identifiers else []
    if not isinstance(items,list) or any(not isinstance(item,dict) for item in items) or len(set(identifiers))!=len(identifiers) or len(items)!=len(identifiers) or {item.get('Id') for item in items}!=set(identifiers):
        raise ValueError('Managed testing inventory is incomplete')
    if spec.get('allow_unavailable') is True:
        require_complete_container_inventory(items)
    return items

try:
    spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
    containers=inventory()
    own={item['Id'] for item in containers if (item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')==spec['project']}
    if spec['mode'] in {'remove','removal-readback'}:
        expected=spec.get('container_ids')
        if spec.get('allow_unavailable') is not True or not isinstance(expected,list) or not expected or not all(isinstance(value,str) and value for value in expected):
            raise ValueError('Container removal requires admitted unavailable testing custody')
        if not own.issubset(set(expected)) or any(item['State']['Status'] not in {'exited','created'} for item in containers if item['Id'] in own):
            raise ValueError('The admitted stopped testing containers changed')
        if spec['mode']=='remove':
            for identifier in sorted(own):
                docker('rm',identifier)
            containers=inventory()
            if any((item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')==spec['project'] for item in containers):
                raise ValueError('Old testing containers remain')
            own=set()
        result={'type':'complete','availability':'stopped' if own else 'absent'}
    else:
        result=qualify_retained_copy(containers,spec)
        if spec.get('allow_unavailable') is True:
            result['container_ids']=sorted(own)
        if result.get('availability')!='absent':
            backings=inspect_volume_backings(result['volumes'],containers,own)
            result['physical_binding']=hashlib.sha256(json.dumps({'logical':result['resource_binding'],'backings':backings},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if spec.get('physical_binding') and result['physical_binding']!=spec['physical_binding']:
            raise ValueError('Retained testing backing storage changed')
        if spec.get('allow_unavailable') is True:
            result['availability']=result.get('availability','stopped' if result['stopped'] else 'available')
            if result['availability']!='available':
                result['inventory_sha256']=retained_inventory_sha256(containers,spec)
                result['observation_sha256']=hashlib.sha256(json.dumps({'project':spec['project'],'context':spec['context'],'custody':result},sort_keys=True,separators=(',',':')).encode()).hexdigest()
            if spec.get('unavailable_sha256') and result.get('observation_sha256')!=spec['unavailable_sha256']:
                raise ValueError('The unavailable predecessor changed')
        if spec['mode']=='readiness':
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
        elif spec['mode'] not in {'read'}:
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
