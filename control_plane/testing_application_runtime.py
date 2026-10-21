"""Fixed read-only qualification of a fresh single-replica application runtime."""

import hashlib
import time
from collections.abc import Callable
from control_plane.contracts.promotion_record import HealthcheckEvidence
from typing import Any

from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    compare_runtime_identity,
    health_payload_runtime_identity_status,
)
from control_plane.dokploy.native_copy_stream import _read_remote
from control_plane.testing_application_capabilities import runtime_capability_program_header


class VerifiedApplicationRuntimeFailure(ValueError):
    """A stable, attributed original runtime answered with a definite failure."""

    def __init__(self, evidence: dict[str, Any]) -> None:
        super().__init__("The attributed original application failed private qualification.")
        self.evidence = evidence


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
    observed_at = observation_clock()
    deadline = observed_at + observation_window_seconds()
    while True:
        result = None
        try:
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
                        key: hashlib.sha256(value.encode()).hexdigest()
                        for key, value in values.items()
                    },
                    "volume": volume,
                    "volume_binding": volume_binding,
                    "mount_path": mount_path,
                    "health_path": health_path,
                },
                receive=frames.append,
                timeout_seconds=max(1, min(120, int(deadline - observed_at))),
            )
            if len(frames) != 1 or frames[0].get("type") != "complete":
                raise ValueError("Application runtime proof is unavailable.")
            result = dict(frames[0])
            status, detail, observed = health_payload_runtime_identity_status(
                expected=identity, payload=result.get("health_payload")
            )
            result.update(
                health_identity=observed.model_dump(mode="json") if observed else None,
                identity_status=status,
                identity_detail=detail,
            )
            http_status = result.get("http_status")
            if not isinstance(http_status, int):
                raise ValueError("The HTTP observation is unavailable.")
            failed = result.get("task_failed") is True or http_status >= 400
            if not failed and status == "match":
                result["verified_failure"] = False
                return result
            result["verified_failure"] = True
        except (ValueError, OSError, RuntimeError):
            # A later unavailable sample clears an earlier provisional failure.
            result = None
        observed_at = observation_clock()
        if observed_at >= deadline:
            if result is not None:
                raise VerifiedApplicationRuntimeFailure(result)
            raise ValueError("Application runtime proof remains unknown.")
        pause_observation()


def observation_clock() -> float:
    return time.monotonic()


def observation_window_seconds() -> float:
    return 90.0


def pause_observation() -> None:
    time.sleep(1)


def qualify_edge_health(
    read: Callable[[], HealthcheckEvidence], expected: RuntimeIdentity
) -> HealthcheckEvidence:
    """Observe a previously qualified edge without making a transient sample final."""
    deadline = observation_clock() + observation_window_seconds()
    while True:
        health = read()
        if health.verified and health.status != "pending":
            status, detail = (health.runtime_identity_status, health.runtime_identity_detail)
            if status == "unchecked":
                status, detail = compare_runtime_identity(
                    expected=expected, observed=health.observed_runtime_identity
                )
            health = health.model_copy(
                update={"runtime_identity_status": status, "runtime_identity_detail": detail}
            )
            if health.status == "pass" and status == "match":
                return health
            health = health.model_copy(update={"status": "fail"})
        if observation_clock() >= deadline:
            return health
        pause_observation()


APPLICATION_RUNTIME_PROGRAM = (
    runtime_capability_program_header()
    + r"""
import base64,hashlib,http.client,io,ipaddress,json,subprocess,sys

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
    if any(item.get('ServiceID')!=service['ID'] for item in tasks): raise ValueError('foreign task inventory')
    active=[item for item in tasks if item.get('DesiredState')=='running']
    failed=[item for item in tasks if item.get('Status',{}).get('State') in {'failed','rejected'}]
    running=[item for item in active if item.get('Status',{}).get('State')=='running']
    terminal=bool(tasks) and all(item.get('ServiceID')==service['ID'] and item.get('Status',{}).get('State') in {'failed','rejected'} for item in tasks)
    task=running[0] if len(running)==1 and len(active)==1 else failed[-1] if terminal else None
    if task is None or task.get('ServiceID')!=service['ID']: raise ValueError('single original task unavailable')
    task_failed=task.get('Status',{}).get('State') in {'failed','rejected'}
    if not task_failed and task.get('Status',{}).get('State')!='running': raise ValueError('task not ready')
    def inspect_attributed_task(task, task_failed):
        identifier=task['Status']['ContainerStatus']['ContainerID']
        container=one('inspect',identifier)
        labels=container.get('Config',{}).get('Labels') or {}
        if (container.get('Id')!=identifier or labels.get('com.docker.swarm.service.id')!=service['ID']
            or labels.get('com.docker.swarm.task.id')!=task['ID'] or labels.get('com.docker.swarm.service.name')!=spec['application']
            or container.get('State',{}).get('Status')!=('exited' if task_failed else 'running') or container['State'].get('Paused') or container['State'].get('Restarting')):
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
        if container.get('HostConfig',{}).get('PortBindings'): raise ValueError('runtime publishes a host port')
        return container,identifier
    if terminal:
        for item in tasks:
            inspect_attributed_task(item, True)
    container,identifier=inspect_attributed_task(task, task_failed)
    address='http://'+spec['application']+':'+str(spec['port'])+spec['health_path']
    failed=task_failed
    health=None
    http_status=0
    if not failed:
        networks=container.get('NetworkSettings',{}).get('Networks',{})
        endpoint=networks.get(spec['network'])
        if not isinstance(endpoint,dict) or not endpoint.get('IPAddress'): raise ValueError('runtime network unavailable')
        proxies=docker('ps','-q','--filter','name=^/dokploy-traefik$').splitlines()
        if len(proxies)!=1: raise ValueError('private health transport unavailable')
        proxy=one('inspect',proxies[0])
        if proxy.get('State',{}).get('Status')!='running' or spec['network'] not in proxy.get('NetworkSettings',{}).get('Networks',{}):
            raise ValueError('private health network unavailable')
        address='http://'+endpoint['IPAddress']+':'+str(spec['port'])+spec['health_path']
        endpoint_address=str(ipaddress.ip_address(endpoint['IPAddress']))
        if ':' in endpoint_address or not isinstance(spec['port'],int) or not 1<=spec['port']<=65535:
            raise ValueError('private HTTP endpoint unavailable')
        path=spec['health_path']
        if not isinstance(path,str) or not path.startswith('/') or path.startswith('//') or any(character.isspace() for character in path) or '?' in path or '#' in path:
            raise ValueError('private HTTP path unavailable')
        request=('GET '+path+' HTTP/1.1\r\nHost: '+endpoint_address+':'+str(spec['port'])+'\r\nConnection: close\r\nAccept: application/json\r\nAccept-Encoding: identity\r\n\r\n').encode('ascii')
        reply=subprocess.run(['docker','exec','-i',proxies[0],'nc','-n','-w','10',endpoint_address,str(spec['port'])],input=request,capture_output=True,timeout=15,check=True)
        if len(reply.stdout)>327680: raise ValueError('private HTTP response exceeds bound')
        class Socket:
            def makefile(self,*arguments): return io.BytesIO(reply.stdout)
        response=http.client.HTTPResponse(Socket())
        response.begin()
        body=response.read()
        if len(body)>262144: raise ValueError('private HTTP body exceeds bound')
        http_status=response.status
        if not 200<=response.status<300 and not 400<=response.status<600:
            raise ValueError('private HTTP response is not qualifying')
        if 200<=response.status<300:
            health=json.loads(body)
            if not isinstance(health,dict): raise ValueError('private health document unavailable')
    if (one('service','inspect',spec['application'])!=service or one('inspect',identifier)!=container
        or one('inspect','--type','task',task['ID'])!=task): raise ValueError('runtime changed during proof')
    print(sys.argv[2]+json.dumps({'type':'complete','service_id':service['ID'],'task_id':task['ID'],'container_id':identifier,
        'backend':'http://'+spec['application']+':'+str(spec['port']),'identity':spec['identity'],'health_url':address,
        'task_failed':task_failed,'http_status':http_status,'health_payload':health}))
except Exception:
    print(sys.argv[2]+json.dumps({'type':'refused'}))
    sys.exit(1)
"""
)
