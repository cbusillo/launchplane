"""Fixed managed-server transport for a testing lane's owned dynamic route.

The application containers never receive provider credentials or a host mount.
The provider's existing Traefik container performs its own atomic file rename;
no global configuration, shared route file or Traefik process is changed.
"""

from collections.abc import Callable
import hashlib
import inspect
import json
import re
from typing import Any, Literal, cast

import yaml

from control_plane.contracts.runtime_identity import RuntimeIdentity, parse_runtime_identity_payload
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.dokploy.compose import TESTING_CONNECTION_CSP
from control_plane.dokploy.testing_route_execution import (
    ALLOCATE_TESTING_ROUTE_LOCK,
    FENCED_TESTING_ROUTE_WRITER,
    READ_TESTING_ROUTE_FENCE,
    TestingRouteExecutionCustody,
    TestingRouteExecutionIntent,
    TestingRouteFence,
    TestingRouteInitializer,
    TestingRouteWriterAttempt,
    parse_testing_route_fence,
)
from control_plane.docker_errors import docker_daemon_authorization_denied
from control_plane.dokploy.native_copy_stream import _read_remote, NativeSourceUnavailable
from control_plane.dokploy.route_rules import host_rule_may_match
from control_plane.dokploy.testing_route import (
    TestingLaneRoute,
    observe_testing_route,
    route_document,
    redirects_to_https,
)

TRAEFIK_DYNAMIC_DIRECTORY = "/etc/dokploy/traefik/dynamic"
TRAEFIK_STATIC_CONFIGURATION = "/etc/traefik/traefik.yml"
MAX_TRAEFIK_API_BYTES = 2_000_000
READ_TRAEFIK_API = r"""set -eu
set -o pipefail
wget -q -T 10 -Y off -O - http://127.0.0.1:8080/api/rawdata | head -c "$1"
"""


class TestingRouteWriteNotStarted(NativeSourceUnavailable):
    """The fixed program returned before invoking its only route writer."""


class TestingRouteObservationRefused(ValueError):
    """A fixed observation fence failed, independently of read availability."""

    def __init__(
        self,
        code: Literal["testing_route_provider_unverified", "testing_route_identity_changed"],
        message: str,
    ) -> None:
        self.code = code
        super().__init__(message)


def testing_route_host_program() -> str:
    """The emitted host program uses the same conservative rule evaluator."""
    return (
        "import base64,hashlib,json,os,re,subprocess,sys\nfrom typing import Any\n"
        "from urllib.parse import urlsplit\n"
        + inspect.getsource(host_rule_may_match)
        + "\n"
        + inspect.getsource(redirects_to_https)
        + "\n"
        + inspect.getsource(docker_daemon_authorization_denied)
        + "\n"
        + inspect.getsource(parse_testing_route_fence)
        + "\nDIRECTORY="
        + repr(TRAEFIK_DYNAMIC_DIRECTORY)
        + "\nSTATIC="
        + repr(TRAEFIK_STATIC_CONFIGURATION)
        + "\nFENCED_WRITER="
        + repr(FENCED_TESTING_ROUTE_WRITER)
        + "\nFENCE_READER="
        + repr(READ_TESTING_ROUTE_FENCE)
        + "\nLOCK_ALLOCATOR="
        + repr(ALLOCATE_TESTING_ROUTE_LOCK)
        + "\nCSP="
        + repr(TESTING_CONNECTION_CSP)
        + "\nMAX_API_BYTES="
        + repr(MAX_TRAEFIK_API_BYTES)
        + "\nAPI_READER="
        + repr(READ_TRAEFIK_API)
        + r"""

def docker(*arguments, stdin=None):
    result = subprocess.run(['docker', *arguments], input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
    if result.returncode:
        if docker_daemon_authorization_denied(result.stderr):
            raise PermissionError('The managed testing route operation was refused')
        raise RuntimeError('The managed testing route operation did not complete')
    return result.stdout

def selected_raw(raw, host, points):
    routers=raw.get('routers')
    if not isinstance(routers,dict):
        raise RuntimeError('Applied router inventory is unavailable')
    selected={}
    services={}
    middlewares={}
    for name,item in routers.items():
        if not isinstance(item,dict) or not isinstance(item.get('entryPoints'),list):
            raise RuntimeError('Applied router inventory is incomplete')
        if (points and not set(item['entryPoints']) & set(points)) or not host_rule_may_match(str(item.get('rule') or ''),host):
            continue
        selected[name]={key:item[key] for key in ('rule','ruleSyntax','service','entryPoints','priority','middlewares','tls','status') if key in item}
        provider=name.rsplit('@',1)[-1]
        service=str(item.get('service') or '')
        service=service if '@' in service else service+'@'+provider
        value=raw.get('services',{}).get(service)
        if not isinstance(value,dict):
            raise RuntimeError('Applied backend inventory is incomplete')
        services[service]={'status':value.get('status'),'loadBalancer':value.get('loadBalancer')}
        for reference in item.get('middlewares',[]):
            qualified=reference if '@' in reference else reference+'@'+provider
            middleware=raw.get('middlewares',{}).get(qualified)
            if not isinstance(middleware,dict):
                raise RuntimeError('Applied middleware inventory is incomplete')
            middlewares[qualified]={key:middleware[key] for key in ('headers','redirectScheme','status') if key in middleware}
    return {'routers':selected,'services':services,'middlewares':middlewares}

WRITE_STARTED=False

def proxy_dns_names(container, proxy_networks):
    names=set()
    networks=container.get('NetworkSettings',{}).get('Networks',{})
    for name,endpoint in networks.items():
        if name not in proxy_networks or not (endpoint.get('IPAddress') or endpoint.get('GlobalIPv6Address')):
            continue
        names.update(value.casefold() for value in (endpoint.get('DNSNames') or [])+(endpoint.get('Aliases') or []) if isinstance(value,str))
        container_name=str(container.get('Name') or '').lstrip('/')
        if container_name:
            names.add(container_name.casefold())
    return names

def application_container(containers, binding, proxy_networks):
    service=json.loads(docker('service','inspect',binding['name']))
    if not isinstance(service,list) or len(service)!=1 or service[0].get('Spec',{}).get('Name')!=binding['name']:
        raise RuntimeError('Application service ownership is unavailable')
    service=service[0]
    if binding.get('service_id') and service['ID']!=binding['service_id']:
        raise RuntimeError('Application service changed')
    if service['Spec'].get('Mode',{}).get('Replicated',{}).get('Replicas')!=1 or service.get('Endpoint',{}).get('Ports'):
        raise RuntimeError('Application endpoint topology changed')
    identifiers=docker('service','ps','--no-trunc','-q',binding['name']).decode().splitlines()
    tasks=[json.loads(docker('inspect','--type','task',identifier))[0] for identifier in identifiers]
    active=[task for task in tasks if task.get('DesiredState')=='running']
    if len(active)!=1 or active[0].get('ServiceID')!=service['ID'] or active[0].get('Status',{}).get('State')!='running':
        raise RuntimeError('Application task ownership is unavailable')
    task=active[0]
    if binding.get('task_id') and task['ID']!=binding['task_id']:
        raise RuntimeError('Application task changed')
    matching=[item for item in containers if item['Id']==task.get('Status',{}).get('ContainerStatus',{}).get('ContainerID')]
    if len(matching)!=1:
        raise RuntimeError('Application container is unavailable')
    container=matching[0]
    labels=container.get('Config',{}).get('Labels') or {}
    if (container.get('State',{}).get('Status')!='running' or labels.get('com.docker.swarm.service.id')!=service['ID']
        or labels.get('com.docker.swarm.task.id')!=task['ID'] or labels.get('com.docker.swarm.service.name')!=binding['name']):
        raise RuntimeError('Application container ownership changed')
    values=dict(item.split('=',1) for item in container['Config'].get('Env',[]) if '=' in item)
    identity=json.loads(values.get('LAUNCHPLANE_RUNTIME_IDENTITY_JSON','null'))
    if not isinstance(identity,dict): raise RuntimeError('Application runtime identity is unavailable')
    expected=binding.get('deployments')
    if expected is not None:
        original=expected.get(identity.get('deployment_record_id'))
        if (not isinstance(original,dict) or (identity.get('product'),identity.get('context'),identity.get('instance'))
            !=(binding['product'],binding['context'],'testing') or identity.get('artifact_id')!=original['artifact']
            or identity.get('source_git_ref')!=original['source']):
            raise RuntimeError('Application does not identify the retained original')
    attached=container.get('NetworkSettings',{}).get('Networks',{})
    shared={key:value for key,value in attached.items() if key in proxy_networks and (value.get('IPAddress') or value.get('GlobalIPv6Address'))}
    if not shared or not isinstance(binding.get('port'),int) or not 0<binding['port']<65536:
        raise RuntimeError('Application ingress network or port is unavailable')
    network_ids={value.get('NetworkID') for value in shared.values() if value.get('NetworkID')}
    attachments=service.get('Spec',{}).get('TaskTemplate',{}).get('Networks')
    if not network_ids or not isinstance(attachments,list) or not any(item.get('Target') in network_ids for item in attachments):
        raise RuntimeError('Application service network ownership is unavailable')
    service_ids=docker('service','ls','-q','--no-trunc').decode().splitlines()
    if len(service_ids)>10000 or len(service_ids)!=len(set(service_ids)) or service['ID'] not in service_ids:
        raise RuntimeError('Application service inventory is incomplete')
    for identifier in service_ids:
        if identifier==service['ID']: continue
        metadata=json.loads(docker('service','inspect','--format','{"name":{{json .Spec.Name}},"networks":{{json .Spec.TaskTemplate.Networks}}}',identifier))
        networks=metadata.get('networks')
        if networks is None and 'networks' in metadata: networks=[]
        if not isinstance(networks,list): raise RuntimeError('Application network aliases are unavailable')
        for attachment in networks:
            if attachment.get('Target') in network_ids and binding['name'].casefold() in {value.casefold() for value in attachment.get('Aliases') or []}:
                raise RuntimeError('Application private name aliases another service')
    for item in containers:
        if item['Id']!=container['Id'] and binding['name'].casefold() in proxy_dns_names(item,proxy_networks):
            raise RuntimeError('Application private name is shared')
    if (json.loads(docker('service','inspect',binding['name']))[0]!=service or docker('service','ps','--no-trunc','-q',binding['name']).decode().splitlines()!=identifiers
        or docker('service','ls','-q','--no-trunc').decode().splitlines()!=service_ids):
        raise RuntimeError('Application service changed during route observation')
    return container,identity,shared

def read_fence(proxy_id, name, filesystem):
    return parse_testing_route_fence(docker('exec',proxy_id,'sh','-c',FENCE_READER,'_',DIRECTORY,name,filesystem).decode())

def same_physical_root(left, right):
    return all(left.get(key)==right[key] for key in ('proxy_id','filesystem','proxy_started_at','process_epoch'))

def fenced_call(proxy_id, spec, custody, mode, document=b''):
    global WRITE_STARTED
    previous=spec.get('fence')
    generation=spec['generation']
    revision=str(previous['revision']) if previous is not None else '0'
    prior_generation=previous['generation'] if previous is not None else 'absent'
    lock_identity=spec.get('lock_identity') or (previous['lock_identity'] if previous is not None else 'absent')
    prior_lock_identity=previous['lock_identity'] if previous is not None else lock_identity
    key=spec.get('execution_key') or generation
    expected=spec.get('expected_file_sha256','absent')
    desired=spec.get('document_sha256') or hashlib.sha256(document).hexdigest()
    WRITE_STARTED=True
    return docker('exec','-i',proxy_id,'sh','-c',FENCED_WRITER,'_',DIRECTORY,spec['route_id']+'.yml',custody['filesystem'],mode,generation,revision,key,expected,desired,prior_generation,lock_identity,custody['process_epoch'],prior_lock_identity,spec.get('retired_boot_id',''),stdin=document).decode()

def main(spec):
    global WRITE_STARTED
    if not re.fullmatch(r'lp-testing-route-[a-f0-9]{32}',spec['route_id']) or not re.fullmatch(r'[a-zA-Z0-9.-]+',spec['host']):
        raise RuntimeError('Testing route identity is invalid')
    identifiers=docker('ps','-aq').decode().splitlines()
    containers=json.loads(docker('inspect',*identifiers)) if identifiers else []
    if not isinstance(containers,list) or any(not isinstance(item,dict) for item in containers):
        raise RuntimeError('Managed container inventory is incomplete')
    proxies=[item for item in containers if item.get('State',{}).get('Status')=='running' and (item.get('Name')=='/dokploy-traefik' or (item.get('Config',{}).get('Labels') or {}).get('com.docker.swarm.service.name')=='dokploy-traefik')]
    if len(proxies)!=1:
        raise RuntimeError('Exactly one running managed proxy is required')
    proxy=proxies[0]
    if proxy.get('HostConfig',{}).get('PidMode','') not in {'','private'}:
        raise RuntimeError('Managed proxy requires its own process namespace')
    proxy_networks=proxy.get('NetworkSettings',{}).get('Networks',{})
    mounts=[item for item in proxy.get('Mounts',[]) if item.get('Destination')==DIRECTORY]
    if len(mounts)!=1 or mounts[0].get('Type')!='bind' or mounts[0].get('RW') is not True or not mounts[0].get('Source'):
        raise RuntimeError('Managed proxy must watch a writable directory mount')
    filesystem=docker('exec',proxy['Id'],'sh','-c','set -eu; command -v flock >/dev/null; test -d "$1"; test ! -L "$1"; stat -c "%d:%i" "$1"','_',DIRECTORY).decode().strip()
    if not re.fullmatch(r'[0-9]+:[0-9]+',filesystem):
        raise RuntimeError('Managed proxy route filesystem is unavailable')
    epoch=docker('exec',proxy['Id'],'sh','-c',"cat /proc/sys/kernel/random/boot_id; cat /proc/1/stat; cat /proc/uptime; if test -e /proc/self/timens_offsets; then sed -n '/^boottime/p' /proc/self/timens_offsets; else printf 'boottime 0 0\\n'; fi").decode().splitlines()
    if len(epoch)!=4 or not re.fullmatch(r'[a-f0-9-]{36}',epoch[0]):
        raise RuntimeError('Managed proxy process identity is unavailable')
    start=epoch[1].rsplit(') ',1)[-1].split()[19]
    if not start.isdigit() or int(float(epoch[2].split()[0])) <= int(start)//os.sysconf('SC_CLK_TCK')+1:
        raise RuntimeError('Managed proxy process identity has not stabilized')
    current_proxy=json.loads(docker('inspect',proxy['Id']))[0]
    started_at=proxy.get('State',{}).get('StartedAt')
    if not started_at or current_proxy.get('State',{}).get('StartedAt')!=started_at:
        raise RuntimeError('Managed proxy restarted during route observation')
    offset=epoch[3].split()
    if len(offset)!=3 or offset[0]!='boottime' or any(not re.fullmatch(r'-?[0-9]+',value) for value in offset[1:]):
        raise RuntimeError('Managed proxy clock identity is unavailable')
    custody={'mount_source':mounts[0]['Source'],'filesystem':filesystem,'proxy_id':proxy['Id'],'proxy_started_at':started_at,'process_epoch':':'.join([epoch[0],start,*offset[1:]])}
    if spec['mode'] in {'allocate_lock_v2','read_lock_v2','initialize_v2','inspect_initializer_v2','settle_v2'}:
        original=spec['execution_custody']
        binding={key:spec[key] for key in ('execution_key','document_sha256','generation') if key in spec}
        readonly=spec['mode'] in {'read_lock_v2','inspect_initializer_v2'}
        matches=original.get('filesystem')==filesystem if readonly else same_physical_root(original,custody)
        if not matches:
            return {'type':'complete','writer_outcome':'retired','execution_custody':custody,**binding}
        if spec['mode']=='allocate_lock_v2':
            WRITE_STARTED=True
            value=docker('exec','-i',proxy['Id'],'sh','-c',LOCK_ALLOCATOR,'_',DIRECTORY,spec['route_id']+'.yml',filesystem,custody['process_epoch'],stdin=b'').decode().strip()
            return {'type':'complete','execution_custody':custody,'lock_identity':value}
        if spec['mode']=='read_lock_v2':
            return {'type':'complete','execution_custody':custody,'execution_fence':read_fence(proxy['Id'],spec['route_id']+'.yml',filesystem)}
        if spec['mode']=='initialize_v2':
            content=fenced_call(proxy['Id'],spec,custody,'initialize')
            if content.strip() in {'busy','not-started'}:
                return {'type':'route_initializer_not_started'}
            state=parse_testing_route_fence('ready\n'+content)
            return {'type':'complete','execution_custody':custody,'execution_fence':state}
        state=read_fence(proxy['Id'],spec['route_id']+'.yml',filesystem)
        if spec['mode']=='inspect_initializer_v2':
            return {'type':'complete','execution_custody':custody,'execution_fence':state}
        fence=state.get('fence')
        expected_fence=spec['fence']
        if state['status']!='ready' or not isinstance(fence,dict):
            outcome='unknown' if state['status']!='busy' else 'busy'
        elif fence['lock_identity']!=expected_fence['lock_identity']:
            outcome='unknown'
        elif fence['generation']!=spec['generation']:
            outcome='retired'
        else:
            outcome=fenced_call(proxy['Id'],spec,custody,'settle').strip()
            if outcome=='superseded': outcome='retired'
        if outcome not in {'applied','excluded','busy','unknown','retired'}:
            raise RuntimeError('The original route execution outcome is unavailable')
        return {'type':'complete','writer_outcome':outcome,'execution_custody':custody,**binding}
    network_mode=proxy.get('HostConfig',{}).get('NetworkMode','')
    if not isinstance(proxy_networks,dict) or not isinstance(proxy_networks.get('dokploy-network'),dict) or not isinstance(network_mode,str) or network_mode=='host' or network_mode.startswith('container:'):
        raise RuntimeError('Managed proxy requires managed ingress in its own network namespace')
    static=docker('exec',proxy['Id'],'cat',STATIC).decode()
    response=docker('exec',proxy['Id'],'sh','-c',API_READER,'_',str(MAX_API_BYTES+1))
    if len(response)>MAX_API_BYTES:
        raise RuntimeError('Managed proxy API response exceeds its bound')
    raw=json.loads(response)
    raw=selected_raw(raw,spec['host'],spec['entrypoints'])
    name=spec['route_id']+'.yml'
    file_read=docker('exec',proxy['Id'],'sh','-c','set -eu; p="$1/$2"; test ! -L "$1"; test ! -L "$p"; if test -e "$p"; then test -f "$p"; printf "present\\n"; cat "$p"; else printf "absent\\n"; fi','_',DIRECTORY,name).decode()
    presence, separator, content=file_read.partition('\n')
    if not separator or presence not in {'present','absent'} or (presence=='absent' and content):
        raise RuntimeError('Testing route file presence is unverified')
    observed=content if presence=='present' else None
    digest=hashlib.sha256(observed.encode()).hexdigest() if observed is not None else 'absent'
    application_file=None
    application_file_name=''
    application_file_sha256=''
    if spec.get('old_application') is not None:
        application_file_name=spec['old_application'].get('route_file_name') or spec['old_application']['name']+'.yml'
        if not re.fullmatch(r'[A-Za-z0-9_-]+\.yml',application_file_name):
            raise RuntimeError('Retained application route filename is invalid')
        value=docker('exec',proxy['Id'],'sh','-c','set -eu; p="$1/$2"; test ! -L "$1"; test ! -L "$p"; if test -e "$p"; then test -f "$p"; printf "present\\n"; head -c 262145 "$p"; else printf "absent\\n"; fi','_',DIRECTORY,application_file_name).decode()
        present,separator,body=value.partition('\n')
        if not separator or present not in {'present','absent'} or len(body.encode())>262144 or (present=='absent' and body):
            raise RuntimeError('Retained application route file is unavailable')
        if present=='present':
            application_file=body
            application_file_sha256=hashlib.sha256(body.encode()).hexdigest()
        expected=spec.get('expected_application_file_sha256')
        if expected and expected!=application_file_sha256:
            raise RuntimeError('The original application route file changed')
    old_project=spec['old_project']
    old=[item for item in containers if (item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')==old_project and (item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.service')=='web']
    endpoints=[]
    old_identity=None
    application=spec.get('old_application')
    if application is not None:
        old_container,old_identity,shared=application_container(containers,application,proxy_networks)
        port=str(application['port'])
        endpoints=['http://'+endpoint['IPAddress']+':'+port for endpoint in shared.values() if endpoint.get('IPAddress')]
        endpoints.append('http://'+application['name']+':'+port)
        endpoints.extend('http://'+alias+':'+port for alias in proxy_dns_names(old_container,proxy_networks))
    elif len(old)==1 and old[0].get('State',{}).get('Status')=='running':
        environment=dict(item.split('=',1) for item in old[0]['Config'].get('Env',[]) if '=' in item)
        if environment.get('PLATFORM_INSTANCE')!='testing':
            raise RuntimeError('The retained web process is not a testing copy')
        old_identity=json.loads(environment.get('LAUNCHPLANE_RUNTIME_IDENTITY_JSON','null'))
        port=environment.get('ODOO_HTTP_PORT') or '8069'
        if not port.isdigit() or not 0<int(port)<65536:
            raise RuntimeError('The retained testing port is invalid')
        network=old[0].get('NetworkSettings',{}).get('Networks',{}).get('dokploy-network')
        if not isinstance(network,dict) or not network.get('IPAddress'):
            raise RuntimeError('Retained testing backend is not on managed ingress')
        endpoints=['http://'+network['IPAddress']+':'+port]
        # User-defined bridge DNS resolves the container name even when Docker
        # reports no additional network aliases. It remains bound to this exact
        # testing process, with a complete check for a foreign alias collision.
        aliases=list(network.get('Aliases') or [])
        container_name=str(old[0].get('Name') or '').lstrip('/')
        if container_name and container_name not in aliases:
            aliases.append(container_name)
        for alias in aliases:
            if alias==container_name or alias.startswith('lp-testing-') or alias.startswith(old_project+'-'):
                if any(item['Id']!=old[0]['Id'] and alias.casefold() in proxy_dns_names(item,proxy_networks) for item in containers):
                    raise RuntimeError('Retained testing alias is shared')
                endpoints.append('http://'+alias+':'+port)
    if spec['mode']=='publish_v2':
        custody_matches=same_physical_root(spec['execution_custody'],custody)
        if not custody_matches or not re.fullmatch(r'[a-f0-9]{64}',spec['execution_key']):
            raise RuntimeError('The original route execution custody changed')
        if spec['retained_backend'] not in endpoints or not isinstance(old_identity,dict) or spec['expected_old_identity_sha256']!=hashlib.sha256(json.dumps(old_identity,sort_keys=True,separators=(',',':')).encode()).hexdigest():
            raise RuntimeError('The retained physical testing copy changed')
        desired=urlsplit(spec['desired_backend'])
        if spec['desired_backend']!=spec['retained_backend']:
            desired_application=spec.get('new_application')
            if desired_application is not None:
                candidate,identity,_shared=application_container(containers,desired_application,proxy_networks)
                if desired.hostname!=desired_application['name'] or desired.port!=desired_application['port']:
                    raise RuntimeError('The candidate application origin changed')
                identity.pop('deployed_at',None)
                if hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=spec['expected_desired_identity_sha256']:
                    raise RuntimeError('The candidate application runtime changed')
            else:
                matching=[item for item in containers if desired.hostname.casefold() in proxy_dns_names(item,proxy_networks)]
                if len(matching)!=1:
                    raise RuntimeError('The candidate testing alias is absent or shared')
                candidate=matching[0]
                labels=candidate.get('Config',{}).get('Labels') or {}
                if candidate.get('State',{}).get('Status')!='running' or labels.get('com.docker.compose.project')!=desired.hostname or labels.get('com.docker.compose.service')!='web':
                    raise RuntimeError('The candidate testing alias changed ownership')
                environment=dict(item.split('=',1) for item in candidate.get('Config',{}).get('Env',[]) if '=' in item)
                port=environment.get('ODOO_HTTP_PORT') or '8069'
                if not port.isdigit() or not 0<int(port)<65536 or desired.port!=int(port):
                    raise RuntimeError('The candidate testing port changed')
                identity=json.loads(environment.get('LAUNCHPLANE_RUNTIME_IDENTITY_JSON','null'))
                if not isinstance(identity,dict) or environment.get('PLATFORM_INSTANCE')!='testing':
                    raise RuntimeError('The candidate testing runtime is unavailable')
                identity.pop('deployed_at',None)
                if hashlib.sha256(json.dumps(identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()!=spec['expected_desired_identity_sha256']:
                    raise RuntimeError('The candidate testing runtime changed')
        elif spec['expected_desired_identity_sha256']!=hashlib.sha256(json.dumps({key:value for key,value in old_identity.items() if key!='deployed_at'},sort_keys=True,separators=(',',':')).encode()).hexdigest():
            raise RuntimeError('The inverse does not identify its original retained runtime')
        document=spec['document']
        http=document.get('http',{})
        if set(document)!={'http'} or set(http)-{'routers','services','middlewares'} or set(http.get('services',{}))!={spec['route_id']}:
            raise RuntimeError('Testing publication contains another route or service')
        for router_name,item in http.get('routers',{}).items():
            if not re.fullmatch(re.escape(spec['route_id'])+r'-[0-9]+',router_name) or item.get('rule')!='Host(`'+spec['host']+'`)' or item.get('service')!=spec['route_id'] or not set(item.get('entryPoints',[]))<=set(spec['entrypoints']):
                raise RuntimeError('Testing publication crosses its lane host')
        if not http.get('routers') or spec['desired_backend'] not in spec['allowed_backends'] or http['services'][spec['route_id']].get('loadBalancer',{}).get('servers')!=[{'url':spec['desired_backend']}]:
            raise RuntimeError('Testing publication selected an unbound backend')
        if http.get('middlewares',{}) not in ({},{spec['route_id']+'-connections':{'headers':{'contentSecurityPolicy':CSP}}}) or any(not redirects_to_https(raw,item) and spec['route_id']+'-connections@file' not in item.get('middlewares',[]) for item in http['routers'].values()):
            raise RuntimeError('Testing publication contains unbound middleware')
        if spec['expected_static_sha256']!=hashlib.sha256(static.encode()).hexdigest() or spec['expected_state_sha256']!=hashlib.sha256(json.dumps(raw,sort_keys=True,separators=(',',':')).encode()).hexdigest() or spec['expected_file_sha256']!=digest:
            raise RuntimeError('Testing route authority changed before publication')
        document=json.dumps(spec['document'],sort_keys=True,separators=(',',':')).encode()
        # One provider path, with no alternate identity or host-file fallback.
        WRITE_STARTED=True
        outcome=fenced_call(proxy['Id'],spec,custody,'publish',document).strip()
        if outcome not in {'applied','excluded','busy','unknown','superseded'}:
            raise RuntimeError('The original route execution outcome is unavailable')
        return {'type':'complete','execution_key':spec['execution_key'],'document_sha256':hashlib.sha256(document).hexdigest(),'execution_custody':custody,'writer_outcome':outcome}
    elif spec['mode']!='read':
        raise RuntimeError('Unsupported testing route operation')
    return {'type':'complete','static_configuration':static,'raw':raw,'owned_file':observed,'file_sha256':digest,'old_endpoints':endpoints,'proxy_id':proxy['Id'],'old_identity':old_identity,'execution_custody':custody,'execution_fence':read_fence(proxy['Id'],name,filesystem), 'application_file':application_file,'application_file_name':application_file_name if application_file is not None else '', 'application_file_sha256':application_file_sha256}

if __name__=='__main__':
    payload={}
    try:
        payload=json.loads(base64.b64decode(sys.argv[1],validate=True))
        print(sys.argv[2]+json.dumps(main(payload)),flush=True)
    except PermissionError:
        print(sys.argv[2]+json.dumps({'type':'denied'}),flush=True)
        sys.exit(1)
    except Exception:
        kind='route_write_not_started' if payload.get('mode') in {'publish_v2','initialize_v2','allocate_lock_v2'} and not WRITE_STARTED else 'failed'
        print(sys.argv[2]+json.dumps({'type':kind}),flush=True)
        sys.exit(1)
"""
    )


class DokployTestingRouteTransport:
    """The service binds records and retains its lane/provider lease throughout."""

    def __init__(
        self,
        *,
        host: str,
        token: str,
        server_id: str,
        old_project: str,
        product: str,
        context: str,
        public_host: str,
        entrypoints: tuple[str, ...],
        old_runtime_identity: RuntimeIdentity | None,
        old_application: dict[str, object] | None = None,
        new_application: dict[str, object] | None = None,
    ) -> None:
        from control_plane.dokploy.testing_route import testing_route_id

        self.host, self.token, self.server_id = host, token, server_id
        self.old_runtime_identity = old_runtime_identity
        self.payload: dict[str, object] = {
            "route_id": testing_route_id(product, context),
            "host": public_host,
            "old_project": old_project,
            "entrypoints": entrypoints,
        }
        if old_application is not None:
            self.payload.update(old_application=old_application, new_application=new_application)
        elif old_runtime_identity is None:
            raise ValueError("Compose routes require their captured runtime identity.")

    def _run(
        self,
        payload: dict[str, object],
        dispatch_guard: Callable[[Callable[[], None]], None] | None = None,
    ) -> dict[str, Any]:
        frames: list[dict[str, object]] = []

        def receive(frame: dict[str, object]) -> None:
            if frame.get("type") == "route_initializer_not_started":
                if payload.get("mode") != "initialize_v2":
                    raise ValueError("An initializer receipt cannot qualify a route writer.")
                raise TestingRouteWriteNotStarted(
                    "The initializer returned before creating its nonce."
                )
            if frame.get("type") == "route_write_not_started":
                if payload.get("mode") not in {
                    "publish_v2",
                    "initialize_v2",
                    "allocate_lock_v2",
                }:
                    raise ValueError("A route writer receipt cannot qualify an observation.")
                raise TestingRouteWriteNotStarted("The original route writer was not started.")
            frames.append(frame)

        _read_remote(
            host=self.host,
            token=self.token,
            server_id=self.server_id,
            program=testing_route_host_program(),
            payload={**self.payload, **payload},
            receive=receive,
            timeout_seconds=120,
            dispatch_guard=dispatch_guard,
        )
        if len(frames) != 1 or frames[0].get("type") != "complete":
            raise ValueError("Testing route observation is incomplete.")
        return cast(dict[str, Any], frames[0])

    def read(self) -> dict[str, Any]:
        frame = self._run({"mode": "read"})
        try:
            static = yaml.safe_load(frame["static_configuration"])
        except yaml.YAMLError:
            static = None
        providers = static.get("providers") if isinstance(static, dict) else None
        provider = providers.get("file") if isinstance(providers, dict) else None
        if (
            not isinstance(provider, dict)
            or provider.get("directory") != TRAEFIK_DYNAMIC_DIRECTORY
            or provider.get("watch") is not True
        ):
            raise TestingRouteObservationRefused(
                "testing_route_provider_unverified",
                "The managed proxy does not watch its directory mount.",
            )
        observed_identity = parse_runtime_identity_payload(frame.get("old_identity"))
        if self.old_runtime_identity is None and self.payload.get("old_application") is not None:
            if observed_identity is None:
                raise TestingRouteObservationRefused(
                    "testing_route_identity_changed",
                    "The retained application runtime is unavailable.",
                )
            self.old_runtime_identity = observed_identity
        if (
            self.old_runtime_identity is not None
            and observed_identity is not None
            and observed_identity.model_dump(exclude={"deployed_at"})
            != self.old_runtime_identity.model_dump(exclude={"deployed_at"})
        ):
            raise TestingRouteObservationRefused(
                "testing_route_identity_changed", "Retained testing runtime identity changed."
            )
        return frame

    def execution_intent(
        self, plan: TestingLaneRoute, backend: str, *, operation_id: str
    ) -> TestingRouteExecutionIntent:
        if (plan.route_id, plan.host) != (
            self.payload["route_id"],
            self.payload["host"],
        ) or backend not in {plan.old_backend, plan.new_backend}:
            raise ValueError("The route execution crosses its original lane.")
        frame = self.read()
        return TestingRouteExecutionIntent(
            operation_id=operation_id,
            server_id=self.server_id,
            route_id=plan.route_id,
            route_digest=plan.digest,
            backend=backend,
            document_sha256=canonical_json_sha256(route_document(plan, backend)),
            custody=TestingRouteExecutionCustody.model_validate(frame["execution_custody"]),
        )

    def allocate_execution_lock(
        self,
        custody: TestingRouteExecutionCustody,
        *,
        dispatch_guard: Callable[[Callable[[], None]], None],
    ) -> str:
        frame = self._run(
            {
                "mode": "allocate_lock_v2",
                "execution_custody": custody.model_dump(mode="json"),
            },
            dispatch_guard=dispatch_guard,
        )
        value = frame.get("lock_identity")
        if (
            TestingRouteExecutionCustody.model_validate(frame["execution_custody"]).physical_sha256
            != custody.physical_sha256
            or not isinstance(value, str)
            or re.fullmatch(r"[0-9]+:[0-9]+", value) is None
        ):
            raise NativeSourceUnavailable("The permanent route lock remains unqualified.")
        return value

    def read_execution_fence(self, custody: TestingRouteExecutionCustody) -> dict[str, Any]:
        frame = self._run(
            {
                "mode": "read_lock_v2",
                "execution_custody": custody.model_dump(mode="json"),
            }
        )
        state = frame.get("execution_fence", {})
        if TestingRouteExecutionCustody.model_validate(
            frame["execution_custody"]
        ).directory_sha256 != custody.directory_sha256 or state.get("status") not in {
            "ready",
            "partial",
            "missing",
            "busy",
            "rebound",
        }:
            raise NativeSourceUnavailable("The original permanent route lock remains unknown.")
        return cast(dict[str, Any], state)

    def observe_execution_lock(self, custody: TestingRouteExecutionCustody) -> str:
        state = self.read_execution_fence(custody)
        if state["status"] not in {"ready", "partial", "rebound"}:
            raise NativeSourceUnavailable("The original permanent route lock remains unknown.")
        return str(state["lock_identity"])

    def initialize_execution(
        self,
        initializer: TestingRouteInitializer,
        *,
        dispatch_guard: Callable[[Callable[[], None]], None],
    ) -> TestingRouteFence:
        self._require_initializer_target(initializer)
        frame = self._run(
            {
                "mode": "initialize_v2",
                "execution_custody": initializer.custody.model_dump(mode="json"),
                "generation": initializer.generation,
                "fence": initializer.previous.model_dump(mode="json")
                if initializer.previous is not None
                else None,
                "lock_identity": initializer.lock_identity,
                "retired_boot_id": initializer.retired_boot_id,
            },
            dispatch_guard=dispatch_guard,
        )
        return self._initializer_witness(frame, initializer)

    def observe_initializer(self, initializer: TestingRouteInitializer) -> TestingRouteFence:
        self._require_initializer_target(initializer)
        frame = self._run(
            {
                "mode": "inspect_initializer_v2",
                "execution_custody": initializer.custody.model_dump(mode="json"),
            }
        )
        return self._initializer_witness(frame, initializer)

    def _require_initializer_target(self, initializer: TestingRouteInitializer) -> None:
        if (initializer.server_id, initializer.route_id) != (
            self.server_id,
            self.payload["route_id"],
        ):
            raise ValueError("The original route initializer belongs to another target.")
        if (
            initializer.previous is not None
            and initializer.previous.lock_identity != initializer.lock_identity
            and (
                not initializer.retired_boot_id
                or initializer.retired_boot_id == initializer.custody.boot_id
            )
        ):
            raise ValueError("The route initializer changed its original lock identity.")

    @staticmethod
    def _initializer_witness(
        frame: dict[str, Any], initializer: TestingRouteInitializer
    ) -> TestingRouteFence:
        if (
            TestingRouteExecutionCustody.model_validate(frame["execution_custody"]).directory_sha256
            != initializer.custody.directory_sha256
            or frame.get("execution_fence", {}).get("status") != "ready"
        ):
            raise NativeSourceUnavailable("The original route initializer remains unknown.")
        fence = TestingRouteFence.model_validate(frame["execution_fence"]["fence"])
        if (
            fence.generation != initializer.generation
            or fence.lock_identity != initializer.lock_identity
        ):
            raise NativeSourceUnavailable("The original route initializer witness changed.")
        return fence

    def settle_attempt(
        self,
        attempt: TestingRouteWriterAttempt,
        *,
        dispatch_guard: Callable[[Callable[[], None]], None],
    ) -> Literal["applied", "excluded", "busy", "unknown", "retired"]:
        if (attempt.execution.server_id, attempt.execution.route_id) != (
            self.server_id,
            self.payload["route_id"],
        ):
            raise ValueError("The original route attempt belongs to another target.")
        frame = self._run(
            {
                "mode": "settle_v2",
                "execution_custody": attempt.custody.model_dump(mode="json"),
                "execution_key": attempt.key,
                "document_sha256": attempt.execution.document_sha256,
                "generation": attempt.fence.generation,
                "fence": attempt.fence.model_dump(mode="json"),
                "expected_file_sha256": attempt.expected_file_sha256,
            },
            dispatch_guard=dispatch_guard,
        )
        outcome = frame.get("writer_outcome")
        if (
            frame.get("execution_key") != attempt.key
            or frame.get("document_sha256") != attempt.execution.document_sha256
            or frame.get("generation") != attempt.fence.generation
            or outcome not in {"applied", "excluded", "busy", "unknown", "retired"}
        ):
            raise NativeSourceUnavailable("The original route attempt outcome is unavailable.")
        return cast(Literal["applied", "excluded", "busy", "unknown", "retired"], outcome)

    def publish(
        self,
        plan: TestingLaneRoute,
        backend: str,
        *,
        expected_backend: str,
        dispatch_guard: Callable[[Callable[[], None]], None],
        desired_runtime_identity: RuntimeIdentity,
        execution: TestingRouteExecutionIntent,
        execution_key: str,
        writer_attempt: TestingRouteWriterAttempt,
    ) -> Literal["applied", "excluded", "busy", "unknown", "superseded"]:
        if self.old_runtime_identity is None:
            raise ValueError("Publication requires the originally observed retained runtime.")
        if not self.payload["entrypoints"]:
            raise ValueError("Testing publication requires its discovered entrypoints to be bound.")
        if (plan.route_id, plan.host) != (
            self.payload["route_id"],
            self.payload["host"],
        ) or expected_backend not in {plan.old_backend, plan.new_backend}:
            raise ValueError("Testing route publication crossed its admitted lane or copy.")
        if (
            desired_runtime_identity.product,
            desired_runtime_identity.context,
            desired_runtime_identity.instance,
        ) != (
            self.old_runtime_identity.product,
            self.old_runtime_identity.context,
            "testing",
        ):
            raise ValueError("The desired testing runtime belongs to another lane.")
        if (
            execution.server_id,
            execution.route_id,
            execution.route_digest,
            execution.backend,
            execution.document_sha256,
        ) != (
            self.server_id,
            plan.route_id,
            plan.digest,
            backend,
            canonical_json_sha256(route_document(plan, backend)),
        ):
            raise ValueError("The original route execution intent changed.")
        if writer_attempt.execution != execution or writer_attempt.key != execution_key:
            raise ValueError("The physical writer attempt changed its original intent.")
        frame = self.read()
        custody = TestingRouteExecutionCustody.model_validate(frame["execution_custody"])
        custody_matches = custody.physical_sha256 == writer_attempt.custody.physical_sha256
        if not custody_matches:
            raise ValueError("The original route execution filesystem changed.")
        owned = None if frame["owned_file"] is None else json.loads(frame["owned_file"])
        if frame["owned_file"] is not None and not isinstance(owned, dict):
            raise ValueError("Testing route file is not an owned document.")
        observed = observe_testing_route(plan, frame["raw"], owned)
        if observed not in {expected_backend, backend}:
            raise ValueError("Testing route no longer identifies its admitted previous copy.")
        intended = route_document(plan, backend)
        if owned == intended:
            return "applied"
        if (
            plan.old_backend not in frame["old_endpoints"]
            or parse_runtime_identity_payload(frame.get("old_identity")) is None
        ):
            raise ValueError("Testing route no longer identifies its retained physical copy.")
        state = frame.get("execution_fence", {})
        if (
            state.get("status") != "ready"
            or TestingRouteFence.model_validate(state.get("fence")) != writer_attempt.fence
            or frame["file_sha256"] != writer_attempt.expected_file_sha256
        ):
            raise TestingRouteWriteNotStarted("The original route attempt is no longer current.")
        receipt = self._run(
            {
                "mode": "publish_v2",
                "execution_key": execution_key,
                "execution_custody": writer_attempt.custody.model_dump(mode="json"),
                "document": intended,
                "retained_backend": plan.old_backend,
                "desired_backend": backend,
                "allowed_backends": [plan.old_backend, plan.new_backend],
                "expected_old_identity_sha256": canonical_json_sha256(frame["old_identity"]),
                "expected_desired_identity_sha256": canonical_json_sha256(
                    desired_runtime_identity.model_dump(mode="json", exclude={"deployed_at"})
                ),
                "expected_file_sha256": frame["file_sha256"],
                "expected_static_sha256": hashlib.sha256(
                    frame["static_configuration"].encode()
                ).hexdigest(),
                "expected_state_sha256": canonical_json_sha256(frame["raw"]),
                **(
                    {"expected_application_file_sha256": plan.application_file_sha256}
                    if plan.application_file_sha256
                    else {}
                ),
                "generation": writer_attempt.fence.generation,
                "fence": writer_attempt.fence.model_dump(mode="json"),
            },
            dispatch_guard=dispatch_guard,
        )
        outcome = receipt.get("writer_outcome")
        if (
            receipt.get("execution_key") != writer_attempt.key
            or receipt.get("document_sha256") != execution.document_sha256
            or TestingRouteExecutionCustody.model_validate(
                receipt.get("execution_custody")
            ).physical_sha256
            != writer_attempt.custody.physical_sha256
            or outcome not in {"applied", "excluded", "busy", "unknown", "superseded"}
        ):
            raise NativeSourceUnavailable("The original physical route receipt changed.")
        return cast(Literal["applied", "excluded", "busy", "unknown", "superseded"], outcome)
