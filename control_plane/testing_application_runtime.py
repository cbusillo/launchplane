"""Fixed read-only qualification of a fresh single-replica application runtime."""

import hashlib
from typing import Any

from control_plane.contracts.runtime_identity import RuntimeIdentity, compare_runtime_identity
from control_plane.dokploy.native_copy_stream import _read_remote
from control_plane.testing_application_capabilities import runtime_capability_program_header


def inspect_application_runtime(
    *,
    host: str,
    token: str,
    server_id: str,
    application: str,
    runtime_port: int,
    network: str,
    image: str,
    identity: RuntimeIdentity,
    values: dict[str, str],
    volume: str = "",
    volume_binding: str = "",
    mount_path: str = "",
    health_path: str,
) -> dict[str, Any]:
    """Values stay internal; the provider request carries only comparison digests."""
    frames: list[dict[str, object]] = []
    _read_remote(
        host=host,
        token=token,
        server_id=server_id,
        program=APPLICATION_RUNTIME_PROGRAM,
        payload={
            "application": application,
            "port": runtime_port,
            "network": network,
            "image": image,
            "identity": identity.model_dump(mode="json"),
            "values": {
                key: hashlib.sha256(value.encode()).hexdigest() for key, value in values.items()
            },
            "volume": volume,
            "volume_binding": volume_binding,
            "mount_path": mount_path,
            "health_path": health_path,
        },
        receive=frames.append,
        timeout_seconds=120,
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise ValueError("Application runtime proof is unavailable.")
    result: dict[str, Any] = dict(frames[0])
    observed = RuntimeIdentity.model_validate(result["identity"])
    if compare_runtime_identity(expected=identity, observed=observed)[0] != "match":
        raise ValueError("The private health identity changed.")
    return result


APPLICATION_RUNTIME_PROGRAM = (
    runtime_capability_program_header()
    + r"""
import base64,hashlib,json,subprocess,sys

def docker(*arguments):
    result=subprocess.run(['docker',*arguments],capture_output=True,text=True,timeout=45,check=True)
    if len(result.stdout)>4*1024*1024: raise ValueError('bounded inventory unavailable')
    return result.stdout

def one(*arguments):
    result=json.loads(docker(*arguments))
    if not isinstance(result,list) or len(result)!=1: raise ValueError('physical identity unavailable')
    return result[0]

def environment(config):
    rows=config.get('Env')
    if not isinstance(rows,list): raise ValueError('runtime environment unavailable')
    result={}
    for row in rows:
        key,separator,value=row.partition('=')
        if not separator or key in result: raise ValueError('ambiguous runtime environment')
        result[key]=value
    return result

try:
    spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
    service=one('service','inspect',spec['application'])
    if service.get('Spec',{}).get('Name')!=spec['application']: raise ValueError('service changed')
    mode=service['Spec'].get('Mode',{}).get('Replicated',{})
    if mode.get('Replicas')!=1 or service.get('Endpoint',{}).get('Ports'): raise ValueError('runtime topology changed')
    task_ids=docker('service','ps','--no-trunc','-q',spec['application']).splitlines()
    tasks=[one('inspect','--type','task',identifier) for identifier in task_ids]
    active=[item for item in tasks if item.get('DesiredState')=='running']
    if len(active)!=1: raise ValueError('single running task unavailable')
    task=active[0]
    if task.get('ServiceID')!=service['ID'] or task.get('Status',{}).get('State')!='running': raise ValueError('task not ready')
    identifier=task['Status']['ContainerStatus']['ContainerID']
    container=one('inspect',identifier)
    labels=container.get('Config',{}).get('Labels') or {}
    if (container.get('Id')!=identifier or labels.get('com.docker.swarm.service.id')!=service['ID']
        or labels.get('com.docker.swarm.task.id')!=task['ID'] or labels.get('com.docker.swarm.service.name')!=spec['application']
        or container.get('State',{}).get('Status')!='running' or container['State'].get('Paused') or container['State'].get('Restarting')):
        raise ValueError('container ownership changed')
    for config in (task['Spec']['ContainerSpec'],container['Config']):
        values=environment(config)
        if find_platform_credentials(values) or values.get('DOCKER_HOST'):
            raise ValueError('application inherited provider authority')
        if any(value and looks_like_credential(key,value) and key not in spec['values'] for key,value in values.items()):
            raise ValueError('application credential has no reviewed independent supplier')
        if any(hashlib.sha256(values.get(key,'').encode()).hexdigest()!=value for key,value in spec['values'].items()):
            raise ValueError('runtime delivery changed')
        if json.loads(values.get('LAUNCHPLANE_RUNTIME_IDENTITY_JSON','null'))!=spec['identity']:
            raise ValueError('runtime identity changed')
    image=one('image','inspect',container['Image'])
    if spec['image'] not in image.get('RepoDigests',[]): raise ValueError('immutable image unavailable')
    mounts=container.get('Mounts')
    expected=[] if not spec['volume'] else [(spec['volume'],spec['mount_path'])]
    if not isinstance(mounts,list) or [(item.get('Name'),item.get('Destination')) for item in mounts]!=expected or any(item.get('Type')!='volume' or item.get('RW') is not True for item in mounts):
        raise ValueError('runtime mounts changed')
    if spec['volume']:
        volume=one('volume','inspect',spec['volume'])
        if (volume.get('Name')!=spec['volume'] or volume.get('Driver')!='local' or volume.get('Options') not in (None,{})
            or (volume.get('Labels') or {}).get('io.launchplane.testing-resources')!=spec['volume_binding']
            or mounts[0].get('Source')!=volume.get('Mountpoint')):
            raise ValueError('runtime state backing provenance changed')
    networks=container.get('NetworkSettings',{}).get('Networks',{})
    endpoint=networks.get(spec['network'])
    if not isinstance(endpoint,dict) or not endpoint.get('IPAddress'): raise ValueError('runtime network unavailable')
    if container.get('HostConfig',{}).get('PortBindings'): raise ValueError('runtime publishes a host port')
    proxies=docker('ps','-q','--filter','name=^/dokploy-traefik$').splitlines()
    if len(proxies)!=1: raise ValueError('private health transport unavailable')
    proxy=one('inspect',proxies[0])
    if proxy.get('State',{}).get('Status')!='running' or spec['network'] not in proxy.get('NetworkSettings',{}).get('Networks',{}):
        raise ValueError('private health network unavailable')
    address='http://'+endpoint['IPAddress']+':'+str(spec['port'])+spec['health_path']
    health=json.loads(docker('exec',proxies[0],'wget','-q','-T','10','-Y','off','--max-redirect','0','-O','-',address))
    identity=next((health[key] for key in ('runtime_identity','launchplane_runtime_identity','launchplaneRuntimeIdentity','LAUNCHPLANE_RUNTIME_IDENTITY_JSON') if key in health),None)
    if isinstance(identity,str): identity=json.loads(identity)
    if identity!=spec['identity']: raise ValueError('private health identity changed')
    if (one('service','inspect',spec['application'])!=service or one('inspect',identifier)!=container
        or docker('service','ps','--no-trunc','-q',spec['application']).splitlines()!=task_ids): raise ValueError('runtime changed during proof')
    print(sys.argv[2]+json.dumps({'type':'complete','service_id':service['ID'],'task_id':task['ID'],'container_id':identifier,
        'backend':'http://'+spec['application']+':'+str(spec['port']),'identity':identity,'health_url':address}))
except Exception:
    print(sys.argv[2]+json.dumps({'type':'refused'}))
    sys.exit(1)
"""
)
