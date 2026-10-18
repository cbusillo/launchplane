"""Fixed managed-server transport for a testing lane's owned dynamic route.

The application containers never receive provider credentials or a host mount.
The provider's existing Traefik container performs its own atomic file rename;
no global configuration, shared route file or Traefik process is changed.
"""

from collections.abc import Callable
import hashlib
import inspect
import json
from typing import Any, cast

import yaml

from control_plane.contracts.runtime_identity import RuntimeIdentity, parse_runtime_identity_payload
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.dokploy.compose import TESTING_CONNECTION_CSP
from control_plane.dokploy.native_copy_stream import _read_remote
from control_plane.dokploy.route_rules import host_rule_may_match
from control_plane.dokploy.testing_route import (
    TestingLaneRoute,
    observe_testing_route,
    route_document,
    redirects_to_https,
)

TRAEFIK_DYNAMIC_DIRECTORY = "/etc/dokploy/traefik/dynamic"
TRAEFIK_STATIC_CONFIGURATION = "/etc/traefik/traefik.yml"

# Run by the already managed proxy container, with the same provider identity.
# The file name is service-derived; callers cannot provide a path or command.
ATOMIC_TESTING_ROUTE_WRITER = r"""
set -eu
root=$1
name=$2
expected=$3
desired=$4
case "$name" in lp-testing-route-*.yml) ;; *) exit 21 ;; esac
test -d "$root"
test ! -L "$root"
path="$root/$name"
if test -L "$path"; then exit 22; fi
if test -e "$path"; then
    test -f "$path"
    current=$(sha256sum "$path" | cut -d ' ' -f 1)
else
    current=absent
fi
if test "$current" = "$desired"; then exit 0; fi
test "$current" = "$expected"
temporary=
trap 'test -z "$temporary" || rm -f "$temporary"' EXIT HUP INT TERM
temporary=$(mktemp "$root/.launchplane-testing.pending.XXXXXX")
umask 077
cat > "$temporary"
test "$(sha256sum "$temporary" | cut -d ' ' -f 1)" = "$desired"
sync -f "$temporary"
# The lane lease and original-effect fence exclude concurrent managed writers.
# Recheck the preimage immediately before rename, including an absent new file.
if test -L "$path"; then exit 22; fi
if test -e "$path"; then
    test -f "$path"
    current=$(sha256sum "$path" | cut -d ' ' -f 1)
else
    current=absent
fi
test "$current" = "$expected"
mv -f "$temporary" "$path"
temporary=
sync -f "$root"
"""


def testing_route_host_program() -> str:
    """The emitted host program uses the same conservative rule evaluator."""
    return (
        "import base64,hashlib,ipaddress,json,re,subprocess,sys\nfrom typing import Any\n"
        "from urllib.request import ProxyHandler,build_opener\n"
        + inspect.getsource(host_rule_may_match)
        + "\n"
        + inspect.getsource(redirects_to_https)
        + "\nDIRECTORY="
        + repr(TRAEFIK_DYNAMIC_DIRECTORY)
        + "\nSTATIC="
        + repr(TRAEFIK_STATIC_CONFIGURATION)
        + "\nWRITER="
        + repr(ATOMIC_TESTING_ROUTE_WRITER)
        + "\nCSP="
        + repr(TESTING_CONNECTION_CSP)
        + r"""

def docker(*arguments, stdin=None):
    result = subprocess.run(['docker', *arguments], input=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60, check=False)
    if result.returncode:
        if any(token in result.stderr.lower() for token in (b'permission denied while trying to connect to the docker daemon socket',b'authorization denied by plugin',b'error response from daemon: unauthorized',b'error response from daemon: access denied')):
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
        if not set(item['entryPoints']) & set(points) or not host_rule_may_match(str(item.get('rule') or ''),host):
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

def main(spec):
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
    mounts=[item for item in proxy.get('Mounts',[]) if item.get('Destination')==DIRECTORY]
    if len(mounts)!=1 or mounts[0].get('Type')!='bind' or mounts[0].get('RW') is not True:
        raise RuntimeError('Managed proxy must watch a writable directory mount')
    address=proxy.get('NetworkSettings',{}).get('Networks',{}).get('dokploy-network',{}).get('IPAddress')
    if not isinstance(address,str) or not ipaddress.ip_address(address).is_private:
        raise RuntimeError('Managed proxy API endpoint is unavailable')
    static=docker('exec',proxy['Id'],'cat',STATIC).decode()
    with build_opener(ProxyHandler({})).open('http://'+address+':8080/api/rawdata',timeout=10) as response:
        raw=json.loads(response.read(2000000))
    raw=selected_raw(raw,spec['host'],spec['entrypoints'])
    name=spec['route_id']+'.yml'
    file_read=docker('exec',proxy['Id'],'sh','-c','set -eu; p="$1/$2"; test ! -L "$1"; test ! -L "$p"; if test -e "$p"; then test -f "$p"; printf "present\\n"; cat "$p"; else printf "absent\\n"; fi','_',DIRECTORY,name).decode()
    presence, separator, content=file_read.partition('\n')
    if not separator or presence not in {'present','absent'} or (presence=='absent' and content):
        raise RuntimeError('Testing route file presence is unverified')
    observed=content if presence=='present' else None
    digest=hashlib.sha256(observed.encode()).hexdigest() if observed is not None else 'absent'
    old_project=spec['old_project']
    old=[item for item in containers if (item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')==old_project and (item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.service')=='web']
    endpoints=[]
    old_identity=None
    if len(old)==1 and old[0].get('State',{}).get('Status')=='running':
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
            if alias.startswith('lp-testing-') or alias.startswith(old_project+'-'):
                if any(item['Id']!=old[0]['Id'] and alias in (item.get('NetworkSettings',{}).get('Networks',{}).get('dokploy-network',{}).get('Aliases') or []) for item in containers):
                    raise RuntimeError('Retained testing alias is shared')
                endpoints.append('http://'+alias+':'+port)
    if spec['mode']=='publish':
        if spec['retained_backend'] not in endpoints or not isinstance(old_identity,dict) or spec['expected_old_identity_sha256']!=hashlib.sha256(json.dumps(old_identity,sort_keys=True,separators=(',',':')).encode()).hexdigest():
            raise RuntimeError('The retained physical testing copy changed')
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
        docker('exec','-i',proxy['Id'],'sh','-c',WRITER,'_',DIRECTORY,name,digest,hashlib.sha256(document).hexdigest(),stdin=document)
    elif spec['mode']!='read':
        raise RuntimeError('Unsupported testing route operation')
    return {'type':'complete','static_configuration':static,'raw':raw,'owned_file':observed,'file_sha256':digest,'old_endpoints':endpoints,'proxy_id':proxy['Id'],'old_identity':old_identity}

if __name__=='__main__':
    try:
        payload=json.loads(base64.b64decode(sys.argv[1],validate=True))
        print(sys.argv[2]+json.dumps(main(payload)),flush=True)
    except PermissionError:
        print(sys.argv[2]+json.dumps({'type':'denied'}),flush=True)
        sys.exit(1)
    except Exception:
        print(sys.argv[2]+json.dumps({'type':'failed'}),flush=True)
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
        old_runtime_identity: RuntimeIdentity,
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

    def _run(
        self,
        payload: dict[str, object],
        dispatch_guard: Callable[[Callable[[], None]], None] | None = None,
    ) -> dict[str, Any]:
        frames: list[dict[str, object]] = []
        _read_remote(
            host=self.host,
            token=self.token,
            server_id=self.server_id,
            program=testing_route_host_program(),
            payload={**self.payload, **payload},
            receive=frames.append,
            timeout_seconds=120,
            dispatch_guard=dispatch_guard,
        )
        if len(frames) != 1 or frames[0].get("type") != "complete":
            raise ValueError("Testing route observation is incomplete.")
        return cast(dict[str, Any], frames[0])

    def read(self) -> dict[str, Any]:
        frame = self._run({"mode": "read"})
        static = yaml.safe_load(frame["static_configuration"])
        provider = static.get("providers", {}).get("file", {}) if isinstance(static, dict) else {}
        if (
            provider.get("directory") != TRAEFIK_DYNAMIC_DIRECTORY
            or provider.get("watch") is not True
        ):
            raise ValueError("The managed proxy does not watch its directory mount.")
        observed_identity = parse_runtime_identity_payload(frame.get("old_identity"))
        if observed_identity is not None and observed_identity.model_dump(
            exclude={"deployed_at"}
        ) != self.old_runtime_identity.model_dump(exclude={"deployed_at"}):
            raise ValueError("Retained testing runtime identity changed.")
        return frame

    def publish(
        self,
        plan: TestingLaneRoute,
        backend: str,
        *,
        expected_backend: str,
        dispatch_guard: Callable[[Callable[[], None]], None],
    ) -> None:
        if (plan.route_id, plan.host) != (
            self.payload["route_id"],
            self.payload["host"],
        ) or expected_backend not in {plan.old_backend, plan.new_backend}:
            raise ValueError("Testing route publication crossed its admitted lane or copy.")
        frame = self.read()
        owned = None if frame["owned_file"] is None else json.loads(frame["owned_file"])
        if frame["owned_file"] is not None and not isinstance(owned, dict):
            raise ValueError("Testing route file is not an owned document.")
        observed = observe_testing_route(plan, frame["raw"], owned)
        if observed not in {expected_backend, backend}:
            raise ValueError("Testing route no longer identifies its admitted previous copy.")
        intended = route_document(plan, backend)
        if owned == intended:
            return
        if (
            plan.old_backend not in frame["old_endpoints"]
            or parse_runtime_identity_payload(frame.get("old_identity")) is None
        ):
            raise ValueError("Testing route no longer identifies its retained physical copy.")
        self._run(
            {
                "mode": "publish",
                "document": intended,
                "retained_backend": plan.old_backend,
                "desired_backend": backend,
                "allowed_backends": [plan.old_backend, plan.new_backend],
                "expected_old_identity_sha256": canonical_json_sha256(frame["old_identity"]),
                "expected_file_sha256": frame["file_sha256"],
                "expected_static_sha256": hashlib.sha256(
                    frame["static_configuration"].encode()
                ).hexdigest(),
                "expected_state_sha256": canonical_json_sha256(frame["raw"]),
            },
            dispatch_guard=dispatch_guard,
        )
        # The controller keeps switching until this later applied read succeeds.
        latest = self.read()
        actual = None if latest["owned_file"] is None else json.loads(latest["owned_file"])
        if observe_testing_route(plan, latest["raw"], actual) != backend:
            raise ValueError("Testing route publication is not yet applied.")
