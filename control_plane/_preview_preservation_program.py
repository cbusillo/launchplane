from control_plane._odoo_snapshot_files import SNAPSHOT_FILES_LIBRARY
from control_plane.preview_filestore import PREVIEW_FILESTORE_LIBRARY
from control_plane.dokploy.memory_capacity import observe_memory_headroom
import inspect

PREVIEW_PRESERVATION_CLEANUP_SECONDS = 60

PREVIEW_PRESERVATION_PROGRAM = (
    SNAPSHOT_FILES_LIBRARY
    + PREVIEW_FILESTORE_LIBRARY
    + inspect.getsource(observe_memory_headroom)
    + f"\nPRESERVATION_CLEANUP_SECONDS = {PREVIEW_PRESERVATION_CLEANUP_SECONDS!r}\n"
    + r"""
import base64
from contextlib import contextmanager
import fcntl
from pathlib import Path
import stat
import sys
import tarfile
import time

cleanup_deadline = None


@contextmanager
def preservation_cleanup():
    global deadline, cleanup_deadline
    work_deadline = deadline
    if cleanup_deadline is None:
        cleanup_deadline = time.monotonic() + PRESERVATION_CLEANUP_SECONDS
    deadline = cleanup_deadline
    try:
        yield
    finally:
        deadline = work_deadline


def docker(*arguments: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(['docker', *arguments], input=stdin,
        capture_output=True, timeout=max(1, deadline - time.monotonic()),
        pass_fds=inherited_lock_descriptors)
    if result.returncode:
        raise RuntimeError('Preview preservation Docker operation failed')
    return result.stdout


def one_container(identifier: str) -> dict:
    rows = json.loads(docker('inspect', identifier))
    if len(rows) != 1:
        raise RuntimeError('Preview preservation container is ambiguous')
    return rows[0]


def require_idle_container(container: dict) -> dict:
    for attempt in range(3):
        if container.get('ExecIDs', 'unknown') in (None, []):
            return container
        if attempt < 2:
            time.sleep(0.1)
            container = one_container(container['Id'])
    raise RuntimeError('Preview preservation execution custody is active')


def tree_manifest(root: Path) -> dict:
    if root.is_symlink() or not root.is_dir() or root.resolve() != root.absolute():
        raise RuntimeError('Preview preservation filestore is aliased')
    entries = {}
    manifest_bytes = 0
    for path in root.rglob('*'):
        if len(entries) >= 8192:
            raise RuntimeError('Preview preservation inventory exceeds its bound')
        metadata = path.lstat()
        if path.is_symlink() or path.resolve() != path.absolute():
            raise RuntimeError('Preview preservation contains an aliased entry')
        name = str(path.relative_to(root))
        if stat.S_ISDIR(metadata.st_mode):
            entries[name] = {'directory': True}
        elif stat.S_ISREG(metadata.st_mode):
            entries[name] = {'bytes': metadata.st_size, 'sha256': snapshot_file_digest(path)}
        else:
            raise RuntimeError('Preview preservation contains an unsupported entry')
        manifest_bytes += len(json.dumps({name: entries[name]}).encode())
        if manifest_bytes > 512 * 1024:
            raise RuntimeError('Preview preservation manifest exceeds its bound')
    return entries


def verify_filestore(path: Path, expected: dict) -> None:
    observed = {}
    with tarfile.open(path, 'r:gz') as archive:
        for member in archive:
            name = Path(member.name)
            if name.is_absolute() or '..' in name.parts or member.name in observed:
                raise RuntimeError('Preview preservation archive custody differs')
            if member.isdir():
                observed[member.name] = {'directory': True}
            elif member.isfile():
                digest = hashlib.sha256()
                stream = archive.extractfile(member)
                if stream is None:
                    raise RuntimeError('Preview preservation archive is incomplete')
                with stream:
                    for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                        digest.update(chunk)
                observed[member.name] = {'bytes': member.size, 'sha256': digest.hexdigest()}
            else:
                raise RuntimeError('Preview preservation archive has an unsupported entry')
    if observed != expected:
        raise RuntimeError('Preview preservation archive differs from its source')


def verify_preservation(path: Path) -> dict:
    if path.is_symlink() or not path.is_dir() or path.resolve() != path.absolute():
        raise RuntimeError('Preview preservation receipt is aliased')
    receipt_path = path / 'receipt.json'
    if receipt_path.is_symlink() or receipt_path.stat().st_size > 1024 * 1024:
        raise RuntimeError('Preview preservation receipt is unavailable')
    receipt = json.loads(receipt_path.read_text())
    if receipt['authority'] != spec['authority'] or receipt.get('database_restore_verified') is not True:
        raise RuntimeError('Preview preservation authority differs')
    for name, digest in receipt['artifacts'].items():
        if name not in {'database.dump', 'filestore.tar.gz'}:
            raise RuntimeError('Preview preservation artifact is unsupported')
        artifact = path / name
        if artifact.is_symlink() or not artifact.is_file() or snapshot_file_digest(artifact) != digest:
            raise RuntimeError('Preview preservation artifact changed')
    if set(receipt['artifacts']) != {'database.dump', 'filestore.tar.gz'}:
        raise RuntimeError('Preview preservation artifacts are incomplete')
    verify_filestore(path / 'filestore.tar.gz', receipt['filestore_manifest'])
    return receipt


def preview_containers(source: dict) -> dict:
    selected = {}
    identifiers = docker('ps', '-aq', '--filter',
        'label=com.docker.compose.project=' + source['compose_project']).decode().split()
    for identifier in identifiers:
        container = require_idle_container(one_container(identifier))
        service = container['Config'].get('Labels', {}).get('com.docker.compose.service')
        if service in selected:
            raise RuntimeError('Preview preservation service is ambiguous')
        selected[service] = container
    if set(selected) != {'database', 'web', 'script-runner'}:
        raise RuntimeError('Preview preservation requires its original plain preview')
    for service in ('web', 'script-runner'):
        container = selected[service]
        environment = dict(value.split('=', 1) for value in container['Config'].get('Env', []))
        if environment.get('PLATFORM_INSTANCE') != 'preview' or environment.get('ODOO_DB_NAME') != source['database_name']:
            raise RuntimeError('Preview preservation application identity differs')
        if container.get('ExecIDs', 'unknown') not in (None, []):
            raise RuntimeError('Preview preservation execution custody is active')
    destinations = {
        'database': {'/var/lib/postgresql/data': 'database'},
        'web': {'/volumes/data': 'data', '/volumes/logs': 'logs'},
        'script-runner': {'/volumes/data': 'data', '/volumes/logs': 'logs'},
    }
    for service, expected in destinations.items():
        host = selected[service]['HostConfig']
        mounts = selected[service]['Mounts']
        extra = [mount for mount in mounts if mount.get('Destination') not in expected]
        if any(service != 'script-runner' or mount.get('Type') != 'bind'
            or mount.get('RW') is not False or not isinstance(mount.get('Destination'), str)
            or mount['Destination'] == '/' or mount['Destination'].startswith('/volumes')
            for mount in extra):
            raise RuntimeError('Preview preservation has an unowned mount')
        if (host.get('NetworkMode') == 'host' or host.get('PidMode') == 'host'
            or host.get('Privileged') or host.get('Devices')
            or any(not isinstance(value, str) or not value.endswith(':host-gateway')
                for value in host.get('ExtraHosts') or [])
            or set(mount.get('Destination') for mount in mounts if mount not in extra) != set(expected)):
            raise RuntimeError('Preview preservation runtime isolation is unavailable')
        for destination, role in expected.items():
            mounts = [mount for mount in selected[service]['Mounts'] if mount.get('Destination') == destination]
            if len(mounts) != 1 or mounts[0].get('Type') != 'volume' or mounts[0].get('Name') != source['preview_volumes'][role]:
                raise RuntimeError('Preview preservation mount ownership differs')
    for role, name in source['preview_volumes'].items():
        expected = {selected['database']['Id']} if role == 'database' else {selected[service]['Id'] for service in ('web', 'script-runner')}
        actual = set(docker('ps', '-aq', '--no-trunc', '--filter', 'volume=' + name).decode().split())
        if actual != expected:
            raise RuntimeError('Preview preservation storage has another consumer')
    return selected


def verify_preservation_source(receipt: dict, source: dict) -> None:
    if receipt['source'] != source:
        raise RuntimeError('Preview preservation provider source changed')
    containers = preview_containers(source)
    if any(containers[service]['State']['Running'] for service in ('web', 'script-runner')):
        raise RuntimeError('Preview preservation source was reopened')
    binding = preview_attempt_binding(source['compose_project'], source['database_name'],
        source['preview_volumes'], spec['authority'])
    if binding != receipt['volume_binding']:
        raise RuntimeError('Preview preservation physical storage changed')
    database = containers['database']
    if database.get('ExecIDs', 'unknown') not in (None, []):
        raise RuntimeError('Preview preservation database execution is active')
    filestore = Path(binding['volume_backings']['data']['path']) / 'filestore'
    if tree_manifest(filestore) != receipt['filestore_manifest']:
        raise RuntimeError('Preview preservation original filestore changed')


def database_query(container: str, database: str, query: bytes) -> str:
    return docker('exec', '-i', container, 'sh', '-c',
        'psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"',
        '_', database, stdin=query).decode().strip()


def require_unserved_web(container: dict) -> None:
    probe = 'from pathlib import Path;rows=[line.split() for name in ("tcp","tcp6") for line in (Path("/proc/net")/name).read_text().splitlines()[1:]];raise SystemExit(any(row[3] in {"01","0A"} and row[1].split(":")[0]!="0B00007F" for row in rows))'
    if container['State']['Running']:
        docker('exec', container['Id'], '/venv/bin/python3', '-I', '-S', '-c', probe)


def wait_database(identifier: str) -> None:
    while time.monotonic() < deadline:
        ready = subprocess.run(['docker', 'exec', identifier, 'pg_isready', '-h', '127.0.0.1', '-U', 'postgres'],
            capture_output=True, timeout=10, pass_fds=inherited_lock_descriptors)
        if ready.returncode == 0:
            return
        time.sleep(1)
    raise RuntimeError('Preview preservation verification database did not start')


def verification_resources(name: str, image: str) -> None:
    identifiers = docker('ps', '-aq', '--filter', 'name=^/' + name + '$').decode().split()
    if identifiers:
        if len(identifiers) != 1:
            raise RuntimeError('Preview preservation verifier is ambiguous')
        container = one_container(identifiers[0])
        mounts = container.get('Mounts', [])
        if (container['Config'].get('Labels', {}).get('io.launchplane.preview-preservation') != spec['preservation_id']
            or container['Image'] != image or container['HostConfig']['NetworkMode'] != 'none'
            or container['HostConfig'].get('Binds') or container['HostConfig'].get('Privileged')
            or len(mounts) != 1 or mounts[0].get('Type') != 'volume' or mounts[0].get('Name') != name
            or mounts[0].get('Destination') != '/var/lib/postgresql/data'):
            raise RuntimeError('Preview preservation verifier has foreign custody')
    names = docker('volume', 'ls', '--format', '{{.Name}}').decode().splitlines()
    if name in names:
        volume = json.loads(docker('volume', 'inspect', name))[0]
        consumers = docker('ps', '-aq', '--no-trunc', '--filter', 'volume=' + name).decode().split()
        expected = {one_container(identifier)['Id'] for identifier in identifiers}
        if ((volume.get('Labels') or {}).get('io.launchplane.preview-preservation') != spec['preservation_id']
            or volume.get('Options') or volume.get('Driver') != 'local' or set(consumers) != expected):
            raise RuntimeError('Preview preservation verification storage has foreign custody')
    elif identifiers:
        raise RuntimeError('Preview preservation verification storage is missing')
    if identifiers:
        docker('rm', '-f', identifiers[0])
    if name in names:
        docker('volume', 'rm', name)


def pending_preservation(temporary: Path, source: dict, binding: dict, image: str) -> bool:
    if not temporary.exists() and not temporary.is_symlink():
        return False
    if temporary.is_symlink() or not temporary.is_dir() or temporary.resolve() != temporary.absolute():
        raise RuntimeError('Preview preservation has foreign pending custody')
    if not any(temporary.iterdir()):
        return False
    pending = temporary / 'pending.json'
    if (pending.is_symlink() or not pending.is_file() or pending.stat().st_size > 16384
        or json.loads(pending.read_text()) != {'authority': spec['authority'], 'source': source,
            'volume_binding': binding, 'verification_image': image}):
        raise RuntimeError('Preview preservation pending source changed')
    if any((entry.name not in {'pending.json', 'database.dump', 'filestore.tar.gz', 'receipt.json'}
        and not entry.name.startswith('.pending-'))
        or entry.is_symlink() or not entry.is_file() for entry in temporary.iterdir()):
        raise RuntimeError('Preview preservation pending directory has foreign data')
    return True


def restore_verification(path: Path, image: str) -> int:
    name = 'lp-preview-preserve-' + spec['preservation_id'][:32]
    verification_resources(name, image)
    docker('volume', 'create', '--label', 'io.launchplane.preview-preservation=' + spec['preservation_id'], name)
    try:
        identifier = docker('run', '-d', '--pull', 'never', '--name', name,
            '--label', 'io.launchplane.preview-preservation=' + spec['preservation_id'],
            '--network', 'none', '--memory', '512m', '--memory-swap', '512m', '--cpus', '1',
            '--security-opt', 'no-new-privileges:true',
            '--mount', 'type=volume,src=' + name + ',dst=/var/lib/postgresql/data',
            '-e', 'POSTGRES_HOST_AUTH_METHOD=trust', '-e', 'POSTGRES_USER=postgres', image).decode().strip()
        wait_database(identifier)
        docker('exec', identifier, 'createdb', '-U', 'postgres', 'preservation_check')
        with path.open('rb') as stream:
            completed = subprocess.run(['docker', 'exec', '-i', identifier, 'pg_restore',
                '--exit-on-error', '--no-owner', '--no-acl', '-U', 'postgres',
                '--dbname=preservation_check'], stdin=stream, capture_output=True,
                timeout=max(1, deadline - time.monotonic()), pass_fds=inherited_lock_descriptors)
        if completed.returncode:
            raise RuntimeError('Preview preservation database restore verification failed')
        return int(database_query(identifier, 'preservation_check',
            b"SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p');\n"))
    finally:
        with preservation_cleanup():
            verification_resources(name, image)


def sync_preservation_parent(path: Path) -> None:
    descriptor = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def clear_owned_pending_preservation(temporary: Path) -> None:
    for entry in temporary.iterdir():
        if entry.name != 'pending.json':
            entry.unlink()
    (temporary / 'pending.json').unlink()
    temporary.rmdir()


def initialize_pending_preservation(temporary: Path, owner: dict) -> None:
    initial = Path(tempfile.mkdtemp(prefix='.initial-' + temporary.name + '-', dir=temporary.parent))
    write_snapshot_receipt(initial / 'pending.json', owner)
    sync_snapshot_directory(initial)
    os.rename(initial, temporary)
    sync_preservation_parent(temporary)


def require_preservation_databases(database: dict, source: dict) -> None:
    expected = sorted([source['database_name'], 'postgres'])
    actual = json.loads(database_query(database['Id'], 'postgres',
        b'SELECT json_agg(datname ORDER BY datname) FROM pg_database WHERE NOT datistemplate;\n'))
    if actual != expected:
        raise RuntimeError('Preview preservation has additional database custody')


@contextmanager
def preservation_database(database: dict):
    started = not database['State']['Running']
    try:
        if started:
            docker('start', database['Id'])
            wait_database(database['Id'])
            floor = spec.get('memory_floor')
            if floor is not None and observe_memory_headroom(Path('/proc'), Path('/sys/fs/cgroup')) < floor:
                raise RuntimeError('Preview preservation lacks reviewed memory headroom')
        yield
    finally:
        if started:
            with preservation_cleanup():
                docker('stop', '-t', '30', database['Id'])


def preserve(path: Path, source: dict) -> dict:
    containers = preview_containers(source)
    database = containers['database']
    if database.get('ExecIDs', 'unknown') not in (None, []):
        raise RuntimeError('Preview preservation database custody is unavailable')
    binding = preview_attempt_binding(source['compose_project'], source['database_name'],
        source['preview_volumes'], spec['authority'])
    paths = {role: Path(value['path']) for role, value in binding['volume_backings'].items()}
    if any(entry.name not in {'filestore', 'addons'} for entry in paths['data'].iterdir()):
        raise RuntimeError('Preview preservation has additional data custody')
    addons = paths['data'] / 'addons'
    if addons.is_symlink() or addons.exists() and any(
        entry.is_symlink() or not entry.is_dir() for entry in addons.rglob('*')):
        raise RuntimeError('Preview preservation has additional addon custody')
    if any(paths['logs'].iterdir()):
        raise RuntimeError('Preview preservation has additional log custody')
    filestore = paths['data'] / 'filestore'
    manifest = tree_manifest(filestore)
    total = sum(entry.get('bytes', 0) for entry in manifest.values())
    space = os.statvfs(path.parent)
    budget = spec['storage_budget']
    floor = spec.get('memory_floor')
    if floor is not None and observe_memory_headroom(Path('/proc'), Path('/sys/fs/cgroup')) < floor:
        raise RuntimeError('Preview preservation lacks reviewed memory headroom')
    if total > budget['filestore_bytes'] or space.f_bavail * space.f_frsize < budget['reserve_bytes'] + 2 * budget['database_bytes'] + total:
        raise RuntimeError('Preview preservation lacks reviewed storage headroom')
    if database['State']['Running']:
        require_preservation_databases(database, source)
    temporary = path.with_name('.pending-' + path.name)
    if pending_preservation(temporary, source, binding, database['Image']):
        verification_resources('lp-preview-preserve-' + spec['preservation_id'][:32], database['Image'])
        clear_owned_pending_preservation(temporary)
    elif temporary.exists():
        temporary.rmdir()
    verifier = 'lp-preview-preserve-' + spec['preservation_id'][:32]
    if (docker('ps', '-aq', '--filter', 'name=^/' + verifier + '$').decode().split()
        or verifier in docker('volume', 'ls', '--format', '{{.Name}}').decode().splitlines()):
        raise RuntimeError('Preview preservation verifier lacks its pending custody')
    require_unserved_web(containers['web'])
    for service in ('web', 'script-runner'):
        docker('stop', '-t', '30', containers[service]['Id'])
    with preservation_database(database):
        require_preservation_databases(database, source)
        other_connections = database_query(database['Id'], source['database_name'],
            b"SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend';\n")
        if other_connections != '0':
            raise RuntimeError('Preview preservation database is in use')
        source_tables = int(database_query(database['Id'], source['database_name'],
            b"SELECT count(*) FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname='public' AND c.relkind IN ('r','p');\n"))
        initialize_pending_preservation(temporary, {'authority': spec['authority'], 'source': source,
            'volume_binding': binding, 'verification_image': database['Image']})
        status, _, _ = capture_snapshot_file(['docker', 'exec', database['Id'], 'sh', '-c',
            'pg_dump -Fc --lock-wait-timeout=10s -U "$POSTGRES_USER" --dbname "$1"', '_', source['database_name']],
            temporary / 'database.dump', budget['database_bytes'],
            timeout_seconds=max(1, deadline - time.monotonic()))
        if status:
            raise RuntimeError('Preview preservation database capture failed')
        with tarfile.open(temporary / 'filestore.tar.gz', 'w:gz') as archive:
            for name in sorted(manifest):
                archive.add(filestore / name, arcname=name, recursive=False)
        os.chmod(temporary / 'filestore.tar.gz', 0o600)
        if tree_manifest(filestore) != manifest:
            raise RuntimeError('Preview preservation filestore changed during capture')
        verify_filestore(temporary / 'filestore.tar.gz', manifest)
        if restore_verification(temporary / 'database.dump', database['Image']) != source_tables:
            raise RuntimeError('Preview preservation restored database differs')
        if preview_attempt_binding(source['compose_project'], source['database_name'], source['preview_volumes'], spec['authority']) != binding:
            raise RuntimeError('Preview preservation source storage changed')
        receipt = {'authority': spec['authority'], 'source': source, 'volume_binding': binding,
            'filestore_manifest': manifest, 'database_restore_verified': True,
            'empty_addon_directories': sorted(str(entry.relative_to(addons)) for entry in addons.rglob('*')) if addons.exists() else [],
            'artifacts': {name: snapshot_file_digest(temporary / name) for name in ('database.dump', 'filestore.tar.gz')}}
        write_snapshot_receipt(temporary / 'receipt.json', receipt)
        sync_snapshot_directory(temporary)
        os.rename(temporary, path)
        sync_preservation_parent(path)
        return verify_preservation(path)


def inspect_pending_preservation(path: Path, source: dict) -> dict:
    containers = preview_containers(source)
    database = containers['database']
    if database.get('ExecIDs', 'unknown') not in (None, []):
        raise RuntimeError('Preview preservation database execution is unavailable')
    require_unserved_web(containers['web'])
    binding = preview_attempt_binding(source['compose_project'], source['database_name'],
        source['preview_volumes'], spec['authority'])
    temporary = path.with_name('.pending-' + path.name)
    if not pending_preservation(temporary, source, binding, database['Image']):
        verifier = 'lp-preview-preserve-' + spec['preservation_id'][:32]
        if (docker('ps', '-aq', '--filter', 'name=^/' + verifier + '$').decode().split()
            or verifier in docker('volume', 'ls', '--format', '{{.Name}}').decode().splitlines()):
            raise RuntimeError('Preview preservation verifier lacks its pending custody')
    return {'source': source, 'volume_binding': binding}


def execute() -> dict:
    root = Path.home() / CLEAN_SNAPSHOT_STATE_DIRECTORY
    preserved = root / 'retained-previews'
    preserved.mkdir(mode=0o700, exist_ok=True)
    if preserved.is_symlink() or preserved.resolve() != preserved.absolute():
        raise RuntimeError('Preview preservation root is aliased')
    identifier = spec['preservation_id']
    if not re.fullmatch(r'[a-f0-9]{64}', identifier):
        raise RuntimeError('Preview preservation identity is invalid')
    path = preserved / identifier
    locks = root / 'preview-locks'
    locks.mkdir(mode=0o700, exist_ok=True)
    if locks.is_symlink() or locks.resolve() != locks.absolute():
        raise RuntimeError('Preview preservation lock custody is aliased')
    if spec.get('source'):
        project = spec['source']['compose_project']
    else:
        previous = verify_preservation(path)
        project = previous['source']['compose_project']
    descriptor = os.open(locks / (hashlib.sha256(project.encode()).hexdigest() + '.lock'),
        os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        global inherited_lock_descriptors
        inherited_lock_descriptors = (descriptor,)
        if path.exists() or path.is_symlink():
            receipt = verify_preservation(path)
            if spec.get('source'):
                verify_preservation_source(receipt, spec['source'])
            return receipt
        if spec['mode'] == 'inspect' and spec.get('source'):
            return inspect_pending_preservation(path, spec['source'])
        if spec['mode'] != 'preserve' or not spec.get('source'):
            raise RuntimeError('Preview preservation is not complete')
        return preserve(path, spec['source'])
    finally:
        os.close(descriptor)


try:
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    deadline = time.monotonic() + max(1, spec['timeout_seconds'] - 2 * PRESERVATION_CLEANUP_SECONDS)
    receipt = execute()
    print(sys.argv[2] + json.dumps({'type': 'complete', 'preservation_id': spec['preservation_id'],
        'source': receipt['source'], 'volume_binding': receipt['volume_binding'],
        'retry_safe': spec['mode'] == 'inspect'}), flush=True)
except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError, tarfile.TarError):
    print(sys.argv[2] + json.dumps({'type': 'failed'}), flush=True)
"""
)
