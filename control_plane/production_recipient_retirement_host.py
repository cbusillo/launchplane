import inspect
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from control_plane.dokploy import native_copy_stream, source
from control_plane.retained_volume_capture import (
    _batched_docker_inspect,
    retained_volume_docker_command,
    retained_volume_engine_identity,
    retained_volume_identity,
)
from control_plane.testing_application_capabilities import single_local_swarm_node


RETIREMENT_RESOURCE_PROGRAM = (
    "from collections.abc import Callable,Mapping,Sequence\nfrom typing import Any\n"
    "import base64,hashlib,json,os,re,subprocess,sys,time\nfrom pathlib import Path\n"
    + inspect.getsource(_batched_docker_inspect)
    + inspect.getsource(retained_volume_docker_command)
    + inspect.getsource(retained_volume_identity)
    + inspect.getsource(retained_volume_engine_identity)
    + inspect.getsource(single_local_swarm_node)
    + r"""
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
deadline=time.monotonic()+70

def docker(*arguments):
    remaining=deadline-time.monotonic()
    if remaining<=0:
        raise ValueError('inventory deadline')
    result=subprocess.run(retained_volume_docker_command(*arguments),capture_output=True,timeout=min(10,remaining),check=False)
    if result.returncode or len(result.stdout)>8*1024*1024:
        raise ValueError('inventory unavailable')
    return result.stdout.decode('utf-8',errors='strict')

def load(*arguments):
    return json.loads(docker(*arguments))

def identifiers(*arguments):
    rows=docker(*arguments).splitlines()
    if len(rows)>10000 or len(rows)!=len(set(rows)) or any(not row or len(row)>256 for row in rows):
        raise ValueError('incomplete inventory')
    return rows

def overlap(first,second):
    return first==second or first in second.parents or second in first.parents

def capture():
    project=spec['project']
    if not isinstance(project,str) or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',project) is None:
        raise ValueError('project identity')
    names=spec['volumes']
    if not isinstance(names,list) or len(names)>16 or len(names)!=len(set(names)) or any(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',name) is None for name in names):
        raise ValueError('volume inventory')
    nodes=single_local_swarm_node(docker)
    engine=load('info','--format','{{json .}}')
    engine_id=engine['ID']
    root=Path(engine['DockerRootDir'])
    if not isinstance(engine_id,str) or not engine_id or not root.is_absolute() or root.resolve(strict=True)!=root:
        raise ValueError('engine identity')
    available=identifiers('volume','ls','-q')
    volumes=_batched_docker_inspect('volume',available,load)
    paths={}
    result=[]
    for name in names:
        row=volumes.get(name)
        if row is None:
            continue
        point=Path(row['Mountpoint'])
        if row.get('Driver')!='local' or row.get('Options') not in (None,{}) or not row.get('CreatedAt') or point!=root/'volumes'/name/'_data' or point.resolve(strict=True)!=point or not point.is_dir():
            raise ValueError('unsupported backing')
        metadata=point.stat()
        labels=row.get('Labels') or {}
        if not isinstance(labels,dict):
            raise ValueError('owner unavailable')
        owned=(labels.get('com.docker.compose.project')==project or labels.get('dokploy.application.id')==spec['target'])
        foreign=(labels.get('com.docker.compose.project') not in (None,project) or labels.get('dokploy.application.id') not in (None,spec['target']) or 'io.launchplane.production-recipient' in labels)
        paths[name]=point
        result.append({'name':name,'identity':retained_volume_identity(row,engine_id,metadata.st_dev,metadata.st_ino),'engine_identity':retained_volume_engine_identity(engine_id),'held':not owned or foreign})
    containers=_batched_docker_inspect('container',identifiers('ps','-aq','--no-trunc'),load)
    project_path=Path('/etc/dokploy/compose')/project
    found=[]
    def protect(mounts,own=False):
        if not isinstance(mounts,list):
            raise ValueError('mount inventory')
        for mount in mounts:
            if not isinstance(mount,dict) or mount.get('Type') not in ('volume','bind','tmpfs'):
                raise ValueError('mount kind')
            if mount['Type']=='tmpfs':
                continue
            raw=mount.get('Source')
            if not isinstance(raw,str) or not raw:
                raise ValueError('mount source')
            point=Path(raw).resolve(strict=False) if raw.startswith('/') else None
            if not own and mount['Type']=='bind' and point is not None and overlap(point,project_path):
                raise ValueError('shared project path')
            for row in result:
                if not own and (mount.get('Name')==row['name'] or raw==row['name'] or point is not None and overlap(point,paths[row['name']])):
                    row['held']=True
    for identifier,row in containers.items():
        labels=row.get('Config',{}).get('Labels')
        if not isinstance(labels,dict):
            raise ValueError('container owner')
        own=labels.get('com.docker.compose.project')==project
        if own:
            if labels.get('dokploy.application.id') not in (None,spec['target']):
                raise ValueError('foreign owner')
            if labels.get('com.docker.compose.service')==spec['application_service'] and row.get('State',{}).get('Running') is not False:
                raise ValueError('old writer is not parked')
            if any(mount.get('Type')!='volume' or mount.get('Name') not in names for mount in row.get('Mounts',[]) if mount.get('Type')!='tmpfs'):
                raise ValueError('unreviewed old persistent storage')
            found.append({'id':identifier,'image':row.get('Image'),'mounts':row.get('Mounts'),'labels':labels,'running':row.get('State',{}).get('Running')})
        networks=row.get('NetworkSettings',{}).get('Networks')
        if not isinstance(networks,dict):
            raise ValueError('network inventory')
        if not own and set(networks)&{project,project+'_default'}:
            raise ValueError('shared project network')
        protect(row.get('Mounts'),own)
    networks=_batched_docker_inspect('network',identifiers('network','ls','--format','{{.Name}}'),load)
    protected_networks={row['Id'] for name,row in networks.items() if name in {project,project+'_default'}}|{project,project+'_default'}
    services=_batched_docker_inspect('service',identifiers('service','ls','-q'),load)
    for row in services.values():
        definition=row['Spec']
        if (definition.get('Labels') or {}).get('com.docker.stack.namespace')==project:
            raise ValueError('stack is unsupported')
        protect(definition['TaskTemplate']['ContainerSpec'].get('Mounts',[]))
        if any(network.get('Target') in protected_networks for network in definition['TaskTemplate'].get('Networks',[])):
            raise ValueError('shared project network')
    task_ids=[]
    for identifier in services:
        task_ids.extend(identifiers('service','ps','-q','--no-trunc',identifier))
        if len(task_ids)>10000:
            raise ValueError('task inventory bound')
    for task in _batched_docker_inspect('task',task_ids,load).values():
        if task.get('ServiceID') not in services or task.get('NodeID','') not in ('',nodes[0]['ID']):
            raise ValueError('task attribution')
        protect(task['Spec']['ContainerSpec'].get('Mounts',[]))
        if any(attachment['Network']['ID'] in protected_networks for attachment in task.get('NetworksAttachments',[])):
            raise ValueError('shared project network')
    for name,path in paths.items():
        for other,volume in volumes.items():
            if other==name:
                continue
            raw=volume.get('Mountpoint')
            if not isinstance(raw,str) or not raw.startswith('/'):
                raise ValueError('volume alias inventory')
            point=Path(raw).resolve(strict=True)
            if overlap(point,path) or (point.stat().st_dev,point.stat().st_ino)==(path.stat().st_dev,path.stat().st_ino):
                raise ValueError('aliased backing')
    if single_local_swarm_node(docker)!=nodes or load('info','--format','{{json .}}')['ID']!=engine_id:
        raise ValueError('host changed')
    return {'project_absent':not found,'containers':found,'volumes':result,'engine_identity':retained_volume_engine_identity(engine_id),'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),'monotonic':time.monotonic()}
try:
    result=capture()
    print(sys.argv[2]+json.dumps({'type':'complete','result':result},sort_keys=True),flush=True)
except Exception:
    print(sys.argv[2]+json.dumps({'type':'complete','result':{'unknown':True}}),flush=True)
"""
)


def inspect_retirement_resources(root: Path, spec: dict[str, Any]) -> dict[str, Any]:
    host, token = source.read_dokploy_config(control_plane_root=root)
    frames: list[dict[str, object]] = []
    native_copy_stream.run_managed_server_program(
        host=host,
        token=token,
        server_id=spec["server"],
        program=RETIREMENT_RESOURCE_PROGRAM,
        payload=spec,
        receive=frames.append,
        timeout_seconds=90,
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise ValueError("Old-resource inventory is incomplete.")
    result = frames[0].get("result")
    if (
        not isinstance(result, dict)
        or result.get("unknown")
        or not isinstance(result.get("project_absent"), bool)
        or not isinstance(result.get("volumes"), list)
        or not isinstance(result.get("boot_id"), str)
        or not result["boot_id"]
        or type(result.get("monotonic")) not in (float, int)
    ):
        raise ValueError("Old-resource custody is unknown.")
    result["received_at"] = datetime.now(timezone.utc).timestamp()
    return result
