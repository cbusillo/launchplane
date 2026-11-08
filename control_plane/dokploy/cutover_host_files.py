import hashlib
import json
from collections.abc import Callable
from typing import Any

import yaml

from control_plane.contracts.prepared_public_site import PreparedPublicSite
from control_plane.dokploy.prepared_host import (
    ACTIVE_PAUSE_FILE,
    bundle_name,
    parse_active_pause,
    parse_prepared_bundle,
    prepared_bundle_text,
)
from control_plane.dokploy.slot_maintenance import ContainerCommand
from control_plane.dokploy.testing_route_transport import (
    MAX_TRAEFIK_API_BYTES,
    TRAEFIK_STATIC_CONFIGURATION,
)
from control_plane.dokploy.traefik_cutover import TraefikFileRoute, TraefikRouteTarget
from control_plane.prepared_public_site import verify_snapshot

MANAGED_PROXY_CONTAINER = "dokploy-traefik"

HOST_FILE_HELPERS = r"""
import fcntl,hashlib,json,os,subprocess,sys,tempfile,time
from pathlib import Path
spec=json.load(sys.stdin)
def docker(*arguments):
    return subprocess.run(['docker',*arguments],capture_output=True,text=True,timeout=60,check=True).stdout
def hold_lock(path):
    path=Path(path)
    if not path.parent.is_dir() or path.parent.is_symlink(): raise ValueError('host lock directory is unavailable')
    descriptor=os.open(path,os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    deadline=time.monotonic()+30
    while True:
        try:
            fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
            return descriptor
        except BlockingIOError:
            if time.monotonic()>=deadline: raise
            time.sleep(0.1)
def sync_directory(directory):
    descriptor=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)
def write_temporary(directory,content,prefix,suffix):
    descriptor,temporary=tempfile.mkstemp(dir=directory,prefix=prefix,suffix=suffix)
    with os.fdopen(descriptor,'w') as stream:
        os.fchmod(stream.fileno(),0o644);stream.write(content);stream.flush();os.fsync(stream.fileno())
    return temporary
def read_owned(path):
    if path.is_symlink(): raise ValueError('host file cannot be a symlink')
    try: return path.read_text()
    except FileNotFoundError: return None
def sha256(content):
    return 'absent' if content is None else hashlib.sha256(content.encode()).hexdigest()
def read_generation(path):
    path=Path(path)
    if path.is_symlink(): raise ValueError('host generation cannot be a symlink')
    try: value=json.loads(path.read_text())
    except FileNotFoundError: return 0
    if type(value) is not int or value<0: raise ValueError('host generation is malformed')
    return value
def replace_owned(path,content,prefix):
    temporary=write_temporary(path.parent,content,prefix,'.pending')
    try: os.replace(temporary,path)
    except BaseException:
        os.unlink(temporary); raise
    sync_directory(path.parent)
def advance_generation(expected):
    replace_owned(Path(spec['generation']),json.dumps(expected+1),'.generation-')
def admitted(expected):
    return read_generation(spec['generation'])==expected
"""

ROUTE_HOST_PROGRAM = (
    HOST_FILE_HELPERS
    + r"""
route=Path(spec['route_file']); directory=route.parent
def watched_destination():
    if directory.is_symlink() or not directory.is_dir(): raise ValueError('route directory is unavailable')
    proxies=json.loads(docker('inspect',spec['proxy']))
    if len(proxies)!=1 or proxies[0].get('Name')!='/'+spec['proxy'] or proxies[0].get('State',{}).get('Running') is not True:
        raise ValueError('managed proxy is unavailable')
    mounts=[mount for mount in proxies[0].get('Mounts',[]) if mount.get('Source')==str(directory)]
    if len(mounts)!=1 or mounts[0].get('Type')!='bind' or mounts[0].get('RW') is not True or not mounts[0].get('Destination'):
        raise ValueError('managed proxy does not mount the route directory')
    metadata=directory.stat()
    if docker('exec',spec['proxy'],'stat','-c','%d:%i',mounts[0]['Destination']).strip()!=f'{metadata.st_dev}:{metadata.st_ino}':
        raise ValueError('managed proxy mount differs from the host route directory')
    return mounts[0]['Destination']
if spec['mode']=='watch':
    destination=watched_destination()
    print(json.dumps({'destination':destination,'static':docker('exec',spec['proxy'],'cat',spec['static'])}))
elif spec['mode']=='read':
    hold_lock(spec['lock'])
    generation=read_generation(spec['generation'])
    content=read_owned(route)
    print(json.dumps({'present':content is not None,'content':content or '','generation':generation}))
elif spec['mode']=='enter':
    hold_lock(spec['lock'])
    generation=read_generation(spec['generation'])
    advance_generation(generation)
    print(json.dumps({'generation':generation+1}))
elif spec['mode']=='publish':
    hold_lock(spec['lock'])
    watched_destination()
    if sha256(read_owned(route))!=spec['expected_sha256'] or not admitted(spec['expected_generation']):
        print(json.dumps({'outcome':'predecessor_changed'}))
    else:
        replace_owned(route,spec['content'],'.cutover-')
        advance_generation(spec['expected_generation'])
        print(json.dumps({'outcome':'published','sha256':sha256(read_owned(route))}))
else:
    raise ValueError('unknown route host mode')
"""
)

PREPARED_HOST_PROGRAM = (
    HOST_FILE_HELPERS
    + r"""
VOLUME_FORMAT='{"Name":{{json .Name}},"Driver":{{json .Driver}},"Options":{{json .Options}},"Mountpoint":{{json .Mountpoint}}}'
def site_directory():
    root=Path(json.loads(docker('info','--format','{{json .DockerRootDir}}')))
    if not root.is_absolute() or root.is_symlink(): raise ValueError('docker root is unavailable')
    volume=json.loads(docker('volume','inspect','--format',VOLUME_FORMAT,spec['volume']))
    expected=root.resolve(strict=True)/'volumes'/spec['volume']/'_data'
    if (volume.get('Name')!=spec['volume'] or volume.get('Driver')!='local' or volume.get('Options') not in (None,{})
        or volume.get('Mountpoint')!=str(expected) or expected.is_symlink() or not expected.is_dir()):
        raise ValueError('prepared volume is unavailable on its product host')
    directory=expected/spec['site']
    if directory.is_symlink(): raise ValueError('prepared site directory cannot be a symlink')
    return directory
def owned_name(name):
    if '/' in name or name.startswith('.') or not name.endswith('.json'): raise ValueError('prepared file name is not owned')
    return name
directory=site_directory()
if spec['mode']=='read':
    hold_lock(spec['lock'])
    generation=read_generation(spec['generation'])
    content=read_owned(directory/owned_name(spec['name'])) if directory.is_dir() else None
    print(json.dumps({'present':content is not None,'content':content or '','generation':generation}))
elif spec['mode']=='stage':
    hold_lock(spec['lock'])
    directory.mkdir(mode=0o755,exist_ok=True); os.chmod(directory,0o755)
    path=directory/owned_name(spec['name'])
    if read_owned(path) is None:
        temporary=write_temporary(directory,spec['content'],'.stage-','.pending')
        try: os.link(temporary,path)
        except FileExistsError: pass
        finally: os.unlink(temporary)
        sync_directory(directory)
    print(json.dumps({'content':read_owned(path)}))
elif spec['mode']=='activate':
    hold_lock(spec['lock'])
    path=directory/'active.json'
    if not directory.is_dir() or path.is_symlink(): raise ValueError('prepared publication is unavailable')
    if read_owned(directory/owned_name(spec['name'])) is None: raise ValueError('prepared publication has no staged bundle')
    if not admitted(spec['expected_generation']): raise ValueError('prepared publication changed after admission')
    replace_owned(path,spec['content'],'.active-')
    advance_generation(spec['expected_generation'])
    print(json.dumps({'content':read_owned(path)}))
elif spec['mode']=='prune':
    hold_lock(spec['lock'])
    if not admitted(spec['expected_generation']): raise ValueError('prepared publication changed during retention')
    if spec['deactivate'] is not None and read_owned(directory/'active.json')!=spec['deactivate']:
        raise ValueError('prepared publication changed during retention')
    paths=[directory/owned_name(item['name']) for item in spec['remove']]
    if any(sha256(read_owned(path))!=item['sha256'] for path,item in zip(paths,spec['remove'])):
        raise ValueError('prepared retention differs from its durable reconstruction')
    if spec['deactivate'] is not None: (directory/'active.json').unlink()
    for path in paths: path.unlink()
    sync_directory(directory)
    advance_generation(spec['expected_generation'])
    print(json.dumps({'pruned':len(paths)}))
else:
    raise ValueError('unknown prepared host mode')
"""
)


def lane_fence(lock_directory: str, route_id: str) -> tuple[str, str]:
    authority = f"{lock_directory.rstrip('/')}/{route_id}.cutover"
    return authority + ".lock", authority + ".generation"


def content_sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def run_host_program(
    command: ContainerCommand, program: str, request: dict[str, Any]
) -> dict[str, Any]:
    frame = json.loads(command(("python3", "-c", program), json.dumps(request)))
    if not isinstance(frame, dict):
        raise ValueError("product-host program returned malformed evidence")
    return frame


class ProductHostFileRoute(TraefikFileRoute):
    def __init__(
        self,
        target: TraefikRouteTarget,
        *,
        reader: ContainerCommand,
        execute: ContainerCommand,
        lock_directory: str,
        guard: Callable[[], None],
    ) -> None:
        super().__init__(target)
        self.reader, self.execute, self.guard = reader, execute, guard
        self.lock, self.generation = lane_fence(lock_directory, target.route_id)
        self.admitted: tuple[str, int] | None = None

    def _request(self, mode: str, **values: object) -> dict[str, Any]:
        return {
            "mode": mode,
            "route_file": str(self.target.route_file),
            "proxy": MANAGED_PROXY_CONTAINER,
            "lock": self.lock,
            "generation": self.generation,
            **values,
        }

    def publish(self, backend: str, *, expected: tuple[str, ...]) -> str:
        self.guard()
        return super().publish(backend, expected=expected)

    def fence_earlier_commands(self) -> None:
        frame = run_host_program(self.execute, ROUTE_HOST_PROGRAM, self._request("enter"))
        if type(frame.get("generation")) is not int:
            raise ValueError("lane fence has no host generation readback")

    def verify_watch(self) -> None:
        frame = run_host_program(
            self.reader,
            ROUTE_HOST_PROGRAM,
            self._request("watch", static=TRAEFIK_STATIC_CONFIGURATION),
        )
        try:
            static = yaml.safe_load(frame["static"])
        except yaml.YAMLError:
            static = None
        providers = static.get("providers") if isinstance(static, dict) else None
        provider = providers.get("file") if isinstance(providers, dict) else None
        if (
            not isinstance(provider, dict)
            or provider.get("directory") != frame["destination"]
            or provider.get("watch") is not True
        ):
            raise ValueError("managed proxy does not watch the route directory")

    def _read_owned(self) -> str:
        frame = run_host_program(self.reader, ROUTE_HOST_PROGRAM, self._request("read"))
        if frame["present"] is not True:
            raise FileNotFoundError("product-host route file is absent")
        self.admitted = (str(frame["content"]), int(frame["generation"]))
        return self.admitted[0]

    def _replace_owned(self, payload: str, predecessor: str) -> None:
        if self.admitted is None or self.admitted[0] != predecessor:
            raise ValueError("route authority changed; refusing a stale traffic switch")
        frame = run_host_program(
            self.execute,
            ROUTE_HOST_PROGRAM,
            self._request(
                "publish",
                expected_sha256=content_sha256(predecessor),
                expected_generation=self.admitted[1],
                content=payload,
            ),
        )
        if frame != {"outcome": "published", "sha256": content_sha256(payload)}:
            raise ValueError("route authority changed; refusing a stale traffic switch")

    def _api(self, suffix: str) -> Any:
        url = self.target.api_url.rstrip("/") + suffix
        response = self.reader(
            (
                "docker",
                "exec",
                MANAGED_PROXY_CONTAINER,
                "wget",
                "-q",
                "-T",
                "10",
                "-Y",
                "off",
                "-O",
                "-",
                url,
            ),
            None,
        )
        if len(response.encode()) > MAX_TRAEFIK_API_BYTES:
            raise ValueError("managed proxy API response exceeds its bound")
        return json.loads(response)


class ProductHostPreparedSites:
    def __init__(
        self,
        *,
        reader: ContainerCommand,
        execute: ContainerCommand,
        volume: str,
        site: str,
        lock_directory: str,
    ) -> None:
        self.reader, self.execute = reader, execute
        self.volume, self.site = volume, site
        self.lock, self.generation = lane_fence(lock_directory, site)

    def _run(self, command: ContainerCommand, mode: str, **values: object) -> dict[str, Any]:
        request = {
            "mode": mode,
            "volume": self.volume,
            "site": self.site,
            "lock": self.lock,
            "generation": self.generation,
            **values,
        }
        return run_host_program(command, PREPARED_HOST_PROGRAM, request)

    def _read_frame(self, name: str) -> dict[str, Any]:
        return self._run(self.reader, "read", name=name)

    def _read(self, name: str) -> str:
        frame = self._read_frame(name)
        if frame["present"] is not True:
            raise FileNotFoundError("prepared file is absent on its product host")
        return str(frame["content"])

    def load(self, pause_id: str) -> PreparedPublicSite:
        return parse_prepared_bundle(self._read(bundle_name(pause_id)), pause_id)

    def active_pause(self) -> str:
        return parse_active_pause(self._read(ACTIVE_PAUSE_FILE))

    def publish(self, site: PreparedPublicSite, pause_id: str) -> None:
        verify_snapshot(site)
        generation = int(self._read_frame(ACTIVE_PAUSE_FILE)["generation"])
        staged = self._run(
            self.execute,
            "stage",
            name=bundle_name(pause_id),
            content=prepared_bundle_text(site, pause_id),
        )
        if parse_prepared_bundle(str(staged["content"]), pause_id) != site:
            raise ValueError("prepared bundle differs from its immutable pause")
        activated = self._run(
            self.execute,
            "activate",
            name=bundle_name(pause_id),
            content=json.dumps(pause_id),
            expected_generation=generation,
        )
        if parse_active_pause(str(activated["content"])) != pause_id:
            raise ValueError("prepared publication readback differs")

    def prune(
        self,
        reconstructable: dict[str, PreparedPublicSite],
        *,
        protected: set[str],
        serving_prepared: bool,
    ) -> None:
        publication = self._read_frame(ACTIVE_PAUSE_FILE)
        generation = int(publication["generation"])
        active = parse_active_pause(publication["content"]) if publication["present"] else ""
        if serving_prepared:
            protected = protected | {active}
        removals: list[dict[str, str]] = []
        deactivate: str | None = None
        for pause_id, site in reconstructable.items():
            if pause_id in protected:
                continue
            try:
                stored = self._read(bundle_name(pause_id))
            except FileNotFoundError:
                continue
            if parse_prepared_bundle(stored, pause_id) != site:
                raise ValueError("prepared retention lacks its exact durable reconstruction")
            if pause_id == active:
                deactivate = json.dumps(pause_id)
            removals.append({"name": bundle_name(pause_id), "sha256": content_sha256(stored)})
        if removals:
            self._run(
                self.execute,
                "prune",
                remove=removals,
                deactivate=deactivate,
                expected_generation=generation,
            )
