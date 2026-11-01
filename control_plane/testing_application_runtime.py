import hashlib
import inspect
import json
import time
from collections.abc import Callable
from control_plane.contracts.promotion_record import HealthcheckEvidence
from typing import Any, Literal
from control_plane.copy_readback_diagnostics import ReadbackPredicate
from control_plane.retained_volume_capture import _batched_docker_inspect

from control_plane.contracts.runtime_identity import (
    RuntimeIdentity,
    compare_runtime_identity,
    health_payload_runtime_identity_status,
)
from control_plane.dokploy.native_copy_stream import _read_remote, NATIVE_PROGRAM_SOURCE_SITE_LIMIT
from control_plane.testing_application_capabilities import runtime_capability_program_header
from control_plane.testing_application_capabilities import single_local_swarm_node


class VerifiedApplicationRuntimeFailure(ValueError):
    """A stable, attributed original runtime answered with a definite failure."""

    def __init__(self, evidence: dict[str, Any]) -> None:
        super().__init__("The attributed original application failed private qualification.")
        self.evidence = evidence


def application_service_inventory(
    docker: Callable[..., str],
) -> tuple[list[str], list[dict[str, Any]]]:
    services = docker("service", "ls", "-q").splitlines()

    def load(*arguments: str) -> object:
        return json.loads(docker(*arguments))

    return services, list(_batched_docker_inspect("service", services, load).values())


def require_application_runtime_absent(
    docker: Callable[..., str], application: str, service_id: str
) -> None:
    nodes = single_local_swarm_node(docker)
    services, rows = application_service_inventory(docker)
    if (
        not application
        or not service_id
        or any(
            row.get("ID") == service_id or row.get("Spec", {}).get("Name") == application
            for row in rows
        )
    ):
        raise ValueError("The original application service is present or unobserved.")
    containers = docker("ps", "-aq", "--no-trunc").splitlines()
    rows = json.loads(docker("inspect", *containers)) if containers else []
    if (
        not isinstance(rows, list)
        or any(not isinstance(row, dict) for row in rows)
        or len(set(containers)) != len(containers)
        or len(rows) != len(containers)
        or {row.get("Id") for row in rows} != set(containers)
    ):
        raise ValueError("The original application container inventory is incomplete.")
    for row in rows:
        labels = row.get("Config", {}).get("Labels")
        if labels is not None and not isinstance(labels, dict):
            raise ValueError("Application container ownership is unobserved.")
        labels = labels or {}
        if (
            labels.get("com.docker.swarm.service.id") == service_id
            or labels.get("com.docker.swarm.service.name") == application
        ):
            raise ValueError("An original application container remains present.")
    if (
        docker("service", "ls", "-q").splitlines() != services
        or docker("ps", "-aq", "--no-trunc").splitlines() != containers
        or single_local_swarm_node(docker) != nodes
    ):
        raise ValueError("The original application absence inventory changed.")


def require_stateless_application_runtime(
    docker: Callable[..., str], application: str, service_id: str
) -> None:
    if not application:
        raise ValueError("The original application identity is required.")
    nodes = single_local_swarm_node(docker)
    _, services = application_service_inventory(docker)
    selected = [
        row
        for row in services
        if row.get("ID") == service_id or row.get("Spec", {}).get("Name") == application
    ]
    if not selected and service_id:
        require_application_runtime_absent(docker, application, service_id)
        return
    if len(selected) != 1:
        raise ValueError("The original stateless service is unavailable.")
    service = selected[0]
    if (
        service.get("Spec", {}).get("Name") != application
        or service_id
        and service.get("ID") != service_id
    ):
        raise ValueError("The original stateless service identity changed.")
    if service.get("Spec", {}).get("TaskTemplate", {}).get("ContainerSpec", {}).get(
        "Mounts"
    ) not in (None, []):
        raise ValueError("A stateless service has persistent mounts.")
    tasks = docker("service", "ps", "--no-trunc", "-q", application).splitlines()
    if len(set(tasks)) != len(tasks):
        raise ValueError("The original task inventory is ambiguous.")
    for identifier in tasks:
        rows = json.loads(docker("inspect", "--type", "task", identifier))
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or rows[0].get("ID") != identifier
            or rows[0].get("ServiceID") != service["ID"]
            or rows[0].get("Spec", {}).get("ContainerSpec", {}).get("Mounts") not in (None, [])
        ):
            raise ValueError("The original task has mounts or unobserved custody.")
    containers = docker("ps", "-aq", "--no-trunc").splitlines()
    rows = json.loads(docker("inspect", *containers)) if containers else []
    if (
        not isinstance(rows, list)
        or len(rows) != len(containers)
        or any(not isinstance(row, dict) for row in rows)
        or len(set(containers)) != len(containers)
        or {row.get("Id") for row in rows} != set(containers)
    ):
        raise ValueError("The stateless application container inventory is incomplete.")
    for row in rows:
        labels = row.get("Config", {}).get("Labels")
        if labels is not None and not isinstance(labels, dict):
            raise ValueError("Container ownership is incomplete.")
        labels = labels or {}
        if (
            labels.get("com.docker.swarm.service.id") == service["ID"]
            or labels.get("com.docker.swarm.service.name") == application
        ):
            if row.get("Mounts") != []:
                raise ValueError("An actual stateless container has persistent mounts.")
    if (
        json.loads(docker("service", "inspect", service["ID"])) != selected
        or docker("service", "ps", "--no-trunc", "-q", application).splitlines() != tasks
        or docker("ps", "-aq", "--no-trunc").splitlines() != containers
        or single_local_swarm_node(docker) != nodes
    ):
        raise ValueError("The original stateless runtime changed during observation.")


def inspect_application_state(
    *,
    host: str,
    token: str,
    server_id: str,
    application: str,
    mode: Literal["absent", "stateless"],
    service_id: str = "",
) -> None:
    frames: list[dict[str, object]] = []
    _read_remote(
        host=host,
        token=token,
        server_id=server_id,
        program=APPLICATION_STATE_PROGRAM,
        payload={"application": application, "service_id": service_id, "mode": mode},
        receive=frames.append,
        timeout_seconds=120,
    )
    if frames != [
        {"type": "complete", "application": application, "service_id": service_id, "mode": mode}
    ]:
        raise ValueError("Physical predecessor state is unobserved.")


def wait_for_application_runtime_absence(
    docker: Callable[..., str], application: str, service_id: str
) -> None:
    deadline = time.monotonic() + 30
    while True:
        try:
            require_application_runtime_absent(docker, application, service_id)
            return
        except ValueError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(1)


APPLICATION_STATE_PROGRAM_HEADER = (
    "import base64,json,subprocess,sys,time\n"
    "from collections.abc import Callable,Sequence\nfrom typing import Any\n"
    + inspect.getsource(_batched_docker_inspect)
    + inspect.getsource(single_local_swarm_node)
    + inspect.getsource(application_service_inventory)
    + inspect.getsource(require_application_runtime_absent)
    + inspect.getsource(require_stateless_application_runtime)
    + inspect.getsource(wait_for_application_runtime_absence)
    + r"""
def docker(*arguments):
    result=subprocess.run(['docker',*arguments],capture_output=True,text=True,timeout=45,check=True)
    if len(result.stdout)>4*1024*1024: raise ValueError('bounded inventory unavailable')
    return result.stdout
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
"""
)

APPLICATION_STATE_PROGRAM = (
    APPLICATION_STATE_PROGRAM_HEADER
    + r"""
if spec['mode']=='absent': require_application_runtime_absent(docker,spec['application'],spec['service_id'])
elif spec['mode']=='stateless': require_stateless_application_runtime(docker,spec['application'],spec['service_id'])
else: raise ValueError('unsupported application state observation')
print(sys.argv[2]+json.dumps({'type':'complete',**spec}))
"""
)

APPLICATION_RETIREMENT_PROGRAM = (
    APPLICATION_STATE_PROGRAM_HEADER
    + r"""
if spec['mode']!='retire' or not spec['service_id']: raise ValueError('original predecessor identity required')
_,services=application_service_inventory(docker)
if any(row.get('ID')==spec['service_id'] or row.get('Spec',{}).get('Name')==spec['application'] for row in services):
    require_stateless_application_runtime(docker,spec['application'],spec['service_id'])
    docker('service','rm',spec['service_id'])
wait_for_application_runtime_absence(docker,spec['application'],spec['service_id'])
print(sys.argv[2]+json.dumps({'type':'complete',**spec}))
"""
)


def retire_application_runtime(
    *,
    host: str,
    token: str,
    server_id: str,
    application: str,
    service_id: str,
    dispatch_guard: Callable[[Callable[[], None]], None],
) -> None:
    frames: list[dict[str, object]] = []
    payload: dict[str, object] = {
        "application": application,
        "service_id": service_id,
        "mode": "retire",
    }
    _read_remote(
        host=host,
        token=token,
        server_id=server_id,
        program=APPLICATION_RETIREMENT_PROGRAM,
        payload=payload,
        receive=frames.append,
        timeout_seconds=120,
        dispatch_guard=dispatch_guard,
    )
    if frames != [{"type": "complete", **payload}]:
        raise ValueError("The original predecessor retirement is unobserved.")


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
    qualify_backend: bool = True,
) -> dict[str, Any]:
    """Values stay internal; the provider request carries only comparison digests."""
    observed_at = observation_clock()
    deadline = observed_at + observation_window_seconds()
    last_error: ValueError | OSError | RuntimeError | None = None
    while True:
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
                    "qualify_backend": qualify_backend,
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
        except (ValueError, OSError, RuntimeError) as error:
            # A later unavailable sample clears an earlier provisional failure.
            result = None
            last_error = error
        observed_at = observation_clock()
        if observed_at >= deadline:
            if result is not None:
                raise VerifiedApplicationRuntimeFailure(result)
            raise ValueError("Application runtime proof remains unknown.") from last_error
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
    + f"\nNATIVE_PROGRAM_SOURCE_SITE_LIMIT={NATIVE_PROGRAM_SOURCE_SITE_LIMIT}\n"
    + f"NATIVE_READ_PREDICATE={ReadbackPredicate.NATIVE_READ.value!r}\n"
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
    backend_host=spec['application']
    address='http://'+backend_host+':'+str(spec['port'])+spec['health_path']
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
        endpoint_address=str(ipaddress.ip_address(endpoint['IPAddress']))
        if ':' in endpoint_address or not isinstance(spec['port'],int) or not 1<=spec['port']<=65535:
            raise ValueError('private HTTP endpoint unavailable')
        path=spec['health_path']
        if not isinstance(path,str) or not path.startswith('/') or path.startswith('//') or any(character.isspace() for character in path) or '?' in path or '#' in path:
            raise ValueError('private HTTP path unavailable')
        def require_owned_backend_address():
            resolved=docker('exec',proxies[0],'nslookup','-type=A',spec['application']+'.')
            addresses=set()
            answered=False
            for line in resolved.splitlines():
                line=line.strip()
                if line.startswith('Name:'):
                    if line.partition(':')[2].strip().rstrip('.').casefold()!=spec['application'].casefold():
                        raise ValueError('application backend DNS name changed')
                    answered=True
                elif answered and line.startswith('Address'):
                    match=re.fullmatch(r'Address(?:\s+\d+)?:\s+(\S+)(?:\s+.*)?',line)
                    if match is None: raise ValueError('application backend DNS answer unavailable')
                    addresses.add(str(ipaddress.IPv4Address(match[1])))
            if addresses!={endpoint_address}: raise ValueError('application backend DNS ownership changed')
        if spec['qualify_backend']: require_owned_backend_address()
        backend_host=spec['application'] if spec['qualify_backend'] else endpoint_address
        address='http://'+backend_host+':'+str(spec['port'])+path
        request=('GET '+path+' HTTP/1.1\r\nHost: '+endpoint_address+':'+str(spec['port'])+'\r\nConnection: close\r\nAccept: application/json\r\nAccept-Encoding: identity\r\n\r\n').encode('ascii')
        reply=subprocess.run(['docker','exec','-i',proxies[0],'nc',*([] if spec['qualify_backend'] else ['-n']),'-w','10',backend_host,str(spec['port'])],input=request,capture_output=True,timeout=15,check=True)
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
    if not failed and spec['qualify_backend']: require_owned_backend_address()
    print(sys.argv[2]+json.dumps({'type':'complete','service_id':service['ID'],'task_id':task['ID'],'container_id':identifier,
        'backend':'http://'+backend_host+':'+str(spec['port']),'identity':spec['identity'],'health_url':address,
        'task_failed':task_failed,'http_status':http_status,'health_payload':health}))
except Exception as error:
    sites=[]
    failure_traceback=error.__traceback__
    while failure_traceback is not None:
        if failure_traceback.tb_frame.f_code.co_filename=='<string>': sites.append(failure_traceback.tb_lineno)
        failure_traceback=failure_traceback.tb_next
    print(sys.argv[2]+json.dumps({'type':'failed','kind':'unavailable','predicate':NATIVE_READ_PREDICATE,'sites':sites[-NATIVE_PROGRAM_SOURCE_SITE_LIMIT:]}))
    sys.exit(1)
"""
)
