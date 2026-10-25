"""Runtime inspection shared by lane copies and standalone upgrade rehearsals."""

import inspect

from control_plane.copy_readback_diagnostics import CopyReadbackRefused, readback_predicate_library
from control_plane._odoo_snapshot_files import CLEAN_SNAPSHOT_STATE_DIRECTORY
from control_plane.preview_filestore import PREVIEW_FILESTORE_LIBRARY

COPY_WEB_GATE_READ_PROGRAM = r"""
import hashlib,os,sys
from pathlib import Path
payload=os.environ.get('ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64','')
expected=hashlib.sha256((sys.argv[1]+'\n'+payload).encode()).hexdigest()
try:
    actual=Path(sys.argv[2]).read_text()
except FileNotFoundError:
    print(False)
else:
    print(actual==expected)
"""


VOLUME_BACKING_INSPECTION_LIBRARY = (
    PREVIEW_FILESTORE_LIBRARY
    + readback_predicate_library()
    + inspect.getsource(CopyReadbackRefused)
    + r"""
import subprocess


def copy_readback_failure_frame(error: Exception, predicate: str = ReadbackPredicate.NATIVE_READ) -> dict[str, str]:
    return {'type': 'failed', 'predicate': error.predicate if isinstance(error, CopyReadbackRefused) else predicate,
        'kind': 'refused' if isinstance(error, CopyReadbackRefused) else 'unavailable'}


def foreign_volume_inventory(containers, own):
    names = {mount['Name'] for container in containers if container.get('Id') not in own
        for mount in container.get('Mounts', []) if mount.get('Type') == 'volume' and mount.get('Name')}
    if not names:
        return {}
    result = subprocess.run(['docker', 'volume', 'inspect', *sorted(names)], capture_output=True,
        timeout=30, pass_fds=globals().get('inherited_lock_descriptors', ()))
    if result.returncode and not (result.stderr.strip() and all(
        re.fullmatch(r'Error response from daemon: (?:volume .+ not found|no such volume: .+)', line, re.IGNORECASE)
        for line in result.stderr.decode().splitlines() if line.strip())):
        raise CopyReadbackRefused(ReadbackPredicate.BACKING_STORAGE)
    rows = json.loads(result.stdout)
    found = {row.get('Name'): row for row in rows}
    if len(found) != len(rows) or not set(found) <= names or not result.returncode and set(found) != names:
        raise CopyReadbackRefused(ReadbackPredicate.BACKING_STORAGE)
    return found


def inspect_volume_backings(volumes, all_containers, own, *, expected_binding=None, require_distinct=False):
    paths, identities, backings = [], set(), {}
    for name in sorted(volumes):
        volume = json.loads(docker("volume", "inspect", name))[0]
        overlay = volume.get('Options', {}).get('type') == 'overlay' if isinstance(volume.get('Options'), dict) else False
        if overlay:
            if expected_binding is not None or not name.endswith('-filestore'):
                raise CopyReadbackRefused(ReadbackPredicate.BACKING_STORAGE)
            _, _, _, private = inspect_preview_filestore(name.removesuffix('-filestore'),
                (volume.get('Labels') or {}).get('com.docker.compose.project'))
        elif volume.get("Driver") != "local" or volume.get("Options") not in (None, {}):
            raise CopyReadbackRefused(ReadbackPredicate.BACKING_STORAGE)
        if expected_binding is not None and (volume.get("Labels") or {}).get("io.launchplane.testing-resources") != expected_binding:
            raise CopyReadbackRefused(ReadbackPredicate.BACKING_STORAGE)
        backing = (private / 'upper' if overlay else Path(volume["Mountpoint"])).resolve(strict=True)
        identity = (backing.stat().st_dev, backing.stat().st_ino)
        if (expected_binding is not None or require_distinct) and identity in identities:
            raise CopyReadbackRefused(ReadbackPredicate.BACKING_DISTINCT)
        identities.add(identity)
        paths.append(backing)
        backings[name] = {"path": str(backing), "device": identity[0], "inode": identity[1]}
    foreign_volumes = foreign_volume_inventory(all_containers, own)
    for container in all_containers:
        if container.get("Id") in own:
            continue
        for mount in container.get("Mounts", []):
            if not mount.get("Source"):
                continue
            source_paths = [Path(mount['Source'])]
            if mount.get('Type') == 'volume' and mount.get('Name'):
                foreign = foreign_volumes.get(mount['Name'])
                if foreign is None:
                    continue
                try:
                    source_paths = volume_storage_paths(foreign)
                except (RuntimeError, KeyError, TypeError):
                    raise CopyReadbackRefused(ReadbackPredicate.BACKING_STORAGE)
            source_paths = [path.resolve(strict=False) for path in source_paths]
            if any(source_path == path or source_path.is_relative_to(path)
                   or (mount.get("RW", True) and path.is_relative_to(source_path))
                   for path in paths for source_path in source_paths):
                raise CopyReadbackRefused(ReadbackPredicate.STORAGE_CONSUMERS)
    return backings
"""
)

COPY_RUNTIME_INSPECTION_LIBRARY = (
    VOLUME_BACKING_INSPECTION_LIBRARY
    + "\nCOPY_WEB_GATE_READ_PROGRAM="
    + repr(COPY_WEB_GATE_READ_PROGRAM)
    + "\n"
    + f"CLEAN_SNAPSHOT_STATE_DIRECTORY = {CLEAN_SNAPSHOT_STATE_DIRECTORY!r}\n"
    + r"""
def inspect_copy_runtime(spec, *, expected_instance):
    gen = spec["generation"]
    namespace, project, database = gen["namespace"], spec["compose_project"], spec["database_name"]
    if expected_instance not in {"testing", "rehearsal", "preview"} or gen["instance"] != expected_instance:
        raise CopyReadbackRefused(ReadbackPredicate.INSTANCE)
    prefix = "lp-testing-" if expected_instance == "testing" else "lp-rehearsal-"
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]*" if expected_instance == 'preview' else re.escape(prefix) + r"[a-f0-9]{32}", namespace):
        raise CopyReadbackRefused(ReadbackPredicate.NAMESPACE)
    volumes = {namespace + "-data", namespace + "-db", namespace + "-logs"}
    names = {'data':namespace+'-data', 'database':namespace+'-db', 'logs':namespace+'-logs'}
    if expected_instance == 'preview':
        names = spec['preview_volumes']
        if (set(names) != {'data','database','logs'} or len(set(names.values())) != 3
            or any(not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,200}', name) for name in names.values())):
            raise CopyReadbackRefused(ReadbackPredicate.MOUNTS)
        volumes = set(names.values())
    containers = {}
    all_containers = []
    for identifier in docker("ps", "-aq").decode().splitlines():
        container = one_inspection(identifier)
        all_containers.append(container)
        labels = container.get("Config", {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") == project:
            service = labels.get("com.docker.compose.service")
            if service not in {"web", "database", "script-runner"} or service in containers:
                raise CopyReadbackRefused(ReadbackPredicate.SERVICES)
            containers[service] = (identifier, container)
    if set(containers) != {"web", "database", "script-runner"}:
        raise CopyReadbackRefused(ReadbackPredicate.SERVICES)
    filestore_name = names['data'] + '-filestore'
    overlay_filestore = expected_instance == 'preview' and any(
        mount.get('Name') == filestore_name for _, item in containers.values() for mount in item.get('Mounts', []))
    if overlay_filestore:
        inspect_preview_filestore(names['data'], project)
        volumes.add(filestore_name)
    for service, (identifier, container) in containers.items():
        configuration = container["Config"]
        if container.get("HostConfig", {}).get("Privileged") or container.get("HostConfig", {}).get("NetworkMode") == "host" or container.get("HostConfig", {}).get("PidMode") not in (None, "", "private"):
            raise CopyReadbackRefused(ReadbackPredicate.CAPABILITIES)
        if any(container.get("HostConfig", {}).get("PortBindings", {}).values()):
            raise CopyReadbackRefused(ReadbackPredicate.PORTS)
        expected = {names['database']} if service == "database" else {names['data'], names['logs']}
        if overlay_filestore and service != 'database':
            expected.add(filestore_name)
            overlays = [mount for mount in container.get('Mounts', []) if mount.get('Name') == filestore_name]
            if len(overlays) != 1 or overlays[0].get('Destination') != '/volumes/data/filestore/' + database:
                raise CopyReadbackRefused(ReadbackPredicate.MOUNTS)
        mounts = container.get("Mounts")
        if not isinstance(mounts, list) or {item.get("Name") for item in mounts} != expected or any(item.get("Type") != "volume" for item in mounts):
            raise CopyReadbackRefused(ReadbackPredicate.MOUNTS)
        if service == "database" and mounts[0].get("Destination") != spec["database_data_directory"]:
            raise CopyReadbackRefused(ReadbackPredicate.DATABASE_STORAGE)
        if service != "database":
            if configuration.get("Image") != spec["image_reference"]:
                raise CopyReadbackRefused(ReadbackPredicate.IMAGE)
            environment = dict(item.split("=", 1) for item in configuration.get("Env", []) if "=" in item)
            allowed_databases = spec.get('usage_database_names',[database]) if spec.get('usage_only') else [database]
            if environment.get("PLATFORM_INSTANCE") != expected_instance or environment.get("ODOO_DB_HOST") != "database" or environment.get("ODOO_DB_NAME") not in allowed_databases:
                raise CopyReadbackRefused(ReadbackPredicate.DATABASE_ENDPOINT)
            if not (spec.get('storage_maintenance') or spec.get('usage_only') or spec.get('verification_only')) and environment.get("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", "") != spec["expected_overrides_payload"]:
                raise CopyReadbackRefused(ReadbackPredicate.SETTINGS)
            if environment.get("ODOO_MAX_CRON_THREADS") != "0":
                raise CopyReadbackRefused(ReadbackPredicate.CRON)
            if container.get("HostConfig", {}).get("ExtraHosts"):
                raise CopyReadbackRefused(ReadbackPredicate.HOST_ALIASES)
            if any(environment.get(key) for key in ("GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK", "DATA_WORKFLOW_SSH_KEY", "ODOO_KEY", "ODOO_SMTP_PASSWORD")):
                raise CopyReadbackRefused(ReadbackPredicate.CREDENTIALS)
            if expected_instance == 'preview' and any(value and key.startswith(('ODOO_UPSTREAM_', 'DATA_WORKFLOW_SSH_')) for key, value in environment.items()):
                raise CopyReadbackRefused(ReadbackPredicate.CREDENTIALS)
        if service != "web":
            networks = container.get("NetworkSettings", {}).get("Networks")
            if not isinstance(networks, dict) or len(networks) != 1:
                raise CopyReadbackRefused(ReadbackPredicate.NETWORK)
            network = one_inspection(next(iter(networks)))
            if expected_instance != 'preview' and network.get("Internal") is not True:
                raise CopyReadbackRefused(ReadbackPredicate.NETWORK)
            if expected_instance == 'preview' and (network.get('Labels') or {}).get('com.docker.compose.project') != project:
                raise CopyReadbackRefused(ReadbackPredicate.NETWORK)
    data_networks = set(containers["database"][1]["NetworkSettings"]["Networks"])
    if set(containers["script-runner"][1]["NetworkSettings"]["Networks"]) != data_networks or set(containers["web"][1]["NetworkSettings"]["Networks"]) != data_networks | ({"dokploy-network"} if expected_instance in {"testing", "preview"} else set()):
        raise CopyReadbackRefused(ReadbackPredicate.NETWORK_REACH)
    if expected_instance in {'rehearsal', 'preview'}:
        own={item[1]['Id'] for item in containers.values()}
        network=one_inspection(next(iter(data_networks)))
        members=network.get('Containers')
        if not isinstance(members,dict) or not set(members)<=own:
            raise CopyReadbackRefused(ReadbackPredicate.NETWORK_MEMBERS)
    for container in all_containers:
        if container in [item[1] for item in containers.values()]:
            continue
        if any(mount.get("Name") in volumes for mount in container.get("Mounts", [])):
            raise CopyReadbackRefused(ReadbackPredicate.STORAGE)
    if expected_instance == 'preview':
        for name in volumes:
            volume = json.loads(docker('volume', 'inspect', name))[0]
            if (volume.get('Labels') or {}).get('com.docker.compose.project') != project:
                raise CopyReadbackRefused(ReadbackPredicate.STORAGE)
    inspect_volume_backings(volumes, all_containers, {item[1]["Id"] for item in containers.values()},
        expected_binding=gen.get('resources_binding_sha256'), require_distinct=True)
    private_state = Path.home().resolve(strict=True) / ('.local/state/launchplane-testing-disposal' if expected_instance == 'testing' else '.local/state/launchplane-rehearsals')
    for container in all_containers:
        for mount in container.get('Mounts', []):
            if mount.get('Source'):
                source_path = Path(mount['Source']).resolve(strict=False)
                snapshot_state = Path.home().resolve(strict=True) / CLEAN_SNAPSHOT_STATE_DIRECTORY
                if ((expected_instance == 'preview' or gen.get('clean_snapshot_enabled') or snapshot_state.exists())
                    and (snapshot_state.is_relative_to(source_path) or source_path.is_relative_to(snapshot_state))
                    or (expected_instance == 'rehearsal' or mount.get('RW', True))
                    and (private_state.is_relative_to(source_path) or source_path.is_relative_to(private_state))):
                    raise CopyReadbackRefused(ReadbackPredicate.PRIVATE_STATE)
    return containers
"""
)
