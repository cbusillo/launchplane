from collections.abc import Callable

import click

from control_plane._odoo_snapshot_files import (
    CLEAN_SNAPSHOT_STATE_DIRECTORY,
    SNAPSHOT_FILES_LIBRARY,
)
from control_plane.clean_testing_snapshot import CleanTestingSnapshot

PREVIEW_RESTORE_DISPOSAL_REQUIRED = "preview_restore_disposal_required"


class PreviewRestoreDisposalRequired(click.ClickException):
    def __init__(self) -> None:
        super().__init__(
            f"{PREVIEW_RESTORE_DISPOSAL_REQUIRED}: Interrupted preview restore requires qualified disposal and rebuild."
        )


PREVIEW_FILESTORE_LIBRARY = (
    f"CLEAN_SNAPSHOT_STATE_DIRECTORY = {CLEAN_SNAPSHOT_STATE_DIRECTORY!r}\n"
    f"PREVIEW_RESTORE_DISPOSAL_REQUIRED = {PREVIEW_RESTORE_DISPOSAL_REQUIRED!r}\n"
    + r"""
import json
import os
from pathlib import Path
import re


class InterruptedPreviewRestore(RuntimeError):
    pass


def preview_docker_text(*arguments):
    output = docker(*arguments)
    return output.decode() if isinstance(output, bytes) else output


def missing_container_error(stderr):
    message = stderr.decode() if isinstance(stderr, bytes) else stderr
    return bool(message.strip()) and all(
        re.search(r'No such (?:container|object):', line, re.IGNORECASE)
        for line in message.splitlines() if line.strip())


def preview_filestore_layout(data_name, project, snapshot_id, *, allow_missing=False):
    if not re.fullmatch(r'[a-f0-9]{64}', snapshot_id):
        raise RuntimeError('Preview filestore snapshot identity is invalid')
    names = preview_docker_text('volume', 'ls', '--format', '{{.Name}}').splitlines()
    if allow_missing and data_name not in names:
        docker_root = Path(preview_docker_text('info', '--format', '{{.DockerRootDir}}').strip())
        if docker_root.resolve(strict=True) != docker_root.absolute():
            raise RuntimeError('Docker storage root is aliased')
        data = {'Name': data_name, 'Driver': 'local', 'Options': {},
            'Mountpoint': str(docker_root / 'volumes' / data_name / '_data'),
            'Labels': {'com.docker.compose.project': project}}
    else:
        data = json.loads(docker('volume', 'inspect', data_name))[0]
    if (data.get('Name') != data_name or data.get('Driver') != 'local' or data.get('Options')
        or (data.get('Labels') or {}).get('com.docker.compose.project') != project):
        raise RuntimeError('Preview filestore backing is not exclusively owned')
    backing = Path(data['Mountpoint'])
    if backing.name != '_data' or backing.resolve(strict=not allow_missing) != backing.absolute():
        raise RuntimeError('Preview filestore backing is aliased')
    private = backing.parent / 'launchplane-preview-filestore'
    lower = Path.home() / CLEAN_SNAPSHOT_STATE_DIRECTORY / snapshot_id / 'filestore'
    if lower.resolve(strict=not allow_missing) != lower.absolute() or not allow_missing and not lower.is_dir():
        raise RuntimeError('Preview filestore source is unavailable or aliased')
    options = {'type': 'overlay', 'device': 'overlay',
        'o': f'lowerdir={lower},upperdir={private}/upper,workdir={private}/work,userxattr'}
    return data, lower, private, options


def inspect_preview_filestore(data_name, project, *, allow_missing_backing=False):
    name = data_name + '-filestore'
    volume = json.loads(docker('volume', 'inspect', name))[0]
    labels = volume.get('Labels') or {}
    snapshot_id = labels.get('io.launchplane.preview-snapshot', '')
    data, lower, private, options = preview_filestore_layout(data_name, project, snapshot_id, allow_missing=allow_missing_backing)
    if (volume.get('Name') != name or volume.get('Driver') != 'local'
        or volume.get('Options') != options or labels.get('com.docker.compose.project') != project):
        raise RuntimeError('Preview filestore overlay identity differs')
    for directory in (private, private / 'upper', private / 'work'):
        if allow_missing_backing and not directory.exists() and not directory.is_symlink():
            continue
        if directory.resolve(strict=True) != directory.absolute() or not directory.is_dir():
            raise RuntimeError('Preview filestore writable custody is aliased')
    return volume, data, lower, private


def volume_storage_paths(volume):
    paths = [volume['Mountpoint']]
    options = volume.get('Options') or {}
    if isinstance(options.get('device'), str) and options['device'].startswith('/'):
        paths.append(options['device'])
    if options.get('type') == 'overlay':
        fields = dict(item.split('=', 1) for item in options.get('o', '').split(',') if '=' in item)
        for key in ('lowerdir', 'upperdir', 'workdir'):
            paths.extend(fields.get(key, '').split(':'))
    if any(not isinstance(path, str) or not path.startswith('/') or '..' in Path(path).parts for path in paths):
        raise RuntimeError('Volume backing paths are unavailable')
    return [Path(path) for path in paths]


def require_unused_preview_upper(private):
    for directory in (private, private / 'upper', private / 'work'):
        if directory.is_symlink() or directory.exists() and (
            directory.resolve(strict=True) != directory.absolute() or not directory.is_dir()):
            raise RuntimeError('Preview filestore writable custody is aliased')
    upper = private / 'upper'
    if upper.exists() and any(upper.iterdir()):
        raise RuntimeError('Preserve preview filestore without an owned unreleased attempt')


def require_unused_preview_backing(data):
    filestore = Path(data['Mountpoint']) / 'filestore'
    if filestore.is_symlink() or filestore.exists() and (
        filestore.resolve(strict=True) != filestore.absolute() or not filestore.is_dir()):
        raise RuntimeError('Preview filestore backing custody is aliased')
    if filestore.exists() and any(
        child.is_symlink() or not child.is_dir() or any(child.iterdir())
        for child in filestore.iterdir()):
        raise RuntimeError('Preserve an existing preview filestore beneath the overlay')


def preview_attempt_binding(project, database, volumes, snapshot):
    binding = {'compose_project': project, 'database_name': database, 'preview_volumes': volumes,
        **{key: snapshot[key] for key in ('server_id', 'product', 'context')}, 'volume_backings': {}}
    for role, name in volumes.items():
        volume = json.loads(docker('volume', 'inspect', name))[0]
        backing = Path(volume['Mountpoint'])
        created = volume.get('CreatedAt')
        if (volume.get('Name') != name or volume.get('Driver') != 'local' or volume.get('Options')
            or (volume.get('Labels') or {}).get('com.docker.compose.project') != project
            or not isinstance(created, str) or not created
            or backing.resolve(strict=True) != backing.absolute()):
            raise RuntimeError('Preview storage creation identity is unavailable')
        metadata = backing.stat()
        binding['volume_backings'][role] = {'created_at': created, 'path': str(backing),
            'device': metadata.st_dev, 'inode': metadata.st_ino}
    return binding


def preview_restore_attempt(project, database, volumes, snapshot, root):
    requested_snapshot = snapshot
    binding = preview_attempt_binding(project, database, volumes, snapshot)
    attempt_id = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    path = root / 'preview-attempts' / (attempt_id + '.json')
    previous = None
    if path.exists() or path.is_symlink():
        if (path.is_symlink() or not path.is_file() or path.stat().st_size > 8192
            or path.parent.resolve(strict=True) != path.parent.absolute()):
            raise RuntimeError('Preview restore attempt custody is invalid')
        previous = json.loads(path.read_text())
        if previous.get('binding') != binding or type(previous.get('completed')) is not bool:
            raise RuntimeError('Preview restore attempt belongs to another target')
        snapshot = previous['snapshot']
    witness = hashlib.sha256(json.dumps(['preview-restore-v1', attempt_id, snapshot['snapshot_id']]).encode()).hexdigest()
    if previous is not None and previous.get('restore_witness') != witness:
        raise RuntimeError('Preview restore attempt witness differs')
    if previous is not None and not previous['completed'] and previous.get('started') is not False:
        if previous.get('started') is True and snapshot == requested_snapshot:
            raise InterruptedPreviewRestore('Interrupted preview restore requires disposal')
        raise RuntimeError('Interrupted preview restore requires qualified disposal and rebuild')
    return {'binding': binding, 'attempt_id': attempt_id, 'path': path, 'previous': previous,
        'snapshot': snapshot, 'witness': witness}


def write_preview_restore_attempt(attempt, *, completed, started=True, overlay_filestore=True):
    path = attempt['path']
    path.parent.mkdir(mode=0o700, exist_ok=True)
    if path.parent.resolve(strict=True) != path.parent.absolute():
        raise RuntimeError('Preview restore attempt custody is aliased')
    write_snapshot_receipt(path, {'binding': attempt['binding'], 'snapshot': attempt['snapshot'],
        'restore_witness': attempt['witness'], 'completed': completed,
        'started': started, 'overlay_filestore': overlay_filestore})


def preview_restore_database_custody(container, database, parameter, attempt):
    if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,62}', database):
        raise RuntimeError('Invalid preview database identity')
    present = docker('exec', '-i', container, 'sh', '-c',
        'psql -X -qAt -v ON_ERROR_STOP=1 -v database_name="$1" -U "$POSTGRES_USER" -d postgres',
        '_', database, stdin=b"SELECT count(*) FROM pg_database WHERE datname = :'database_name';\n").decode().strip()
    previous = attempt['previous'] if attempt is not None else None
    if present not in {'0', '1'} or present == '1' and (
        previous is None or previous.get('completed') is not True):
        raise RuntimeError('An existing preview database cannot be overwritten by a snapshot')
    if present == '1':
        probe = ['exec', '-i', container, 'sh', '-c',
            'psql -X -qAt -v ON_ERROR_STOP=1 -v marker="$2" -U "$POSTGRES_USER" -d "$1"',
            '_', database, parameter]
        table = docker(*probe, stdin=b"SELECT to_regclass('public.ir_config_parameter') IS NOT NULL;\n").decode().strip()
        if table not in {'t', 'f'}:
            raise RuntimeError('Preview restore witness inspection is ambiguous')
        stored = docker(*probe, stdin=b"SELECT value FROM ir_config_parameter WHERE key=:'marker';\n").decode().strip() if table == 't' else ''
        if stored != attempt['witness']:
            raise RuntimeError('Preserve preview data without this attempt restore witness')
    if present == '0' and previous is not None and previous.get('completed'):
        raise RuntimeError('A completed preview lost its database; preserve its retained data')
    return present
"""
)


def prepare_preview_filestore(
    *,
    host: str,
    token: str,
    server_id: str,
    project: str,
    data_volume: str,
    database_volume: str,
    logs_volume: str,
    database_name: str,
    snapshot: CleanTestingSnapshot | None,
    apply: bool = True,
    require_existing: bool = False,
    dispatch_guard: Callable[[Callable[[], None]], None],
) -> dict[str, object] | None:
    from control_plane.dokploy import native_copy_stream
    from control_plane.dokploy.compose import ODOO_DATABASE_DATA_DIRECTORY
    from control_plane.odoo_testing_restore import TESTING_COPY_COMPLETION_PARAMETER

    if snapshot is not None and snapshot.server_id != server_id:
        raise click.ClickException(
            "The preview and clean snapshot require the same execution server."
        )
    frames: list[dict[str, object]] = []
    native_copy_stream._read_remote(
        host=host,
        token=token,
        server_id=server_id,
        program=PREPARE_PREVIEW_FILESTORE_PROGRAM,
        payload={
            "project": project,
            "data_volume": data_volume,
            "volumes": {"data": data_volume, "database": database_volume, "logs": logs_volume},
            "database_name": database_name,
            "database_data_directory": ODOO_DATABASE_DATA_DIRECTORY,
            "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
            "apply": apply,
            "require_existing": require_existing,
            "copy_witness_parameter": TESTING_COPY_COMPLETION_PARAMETER,
        },
        receive=frames.append,
        timeout_seconds=120,
        dispatch_guard=dispatch_guard,
    )
    if len(frames) == 1 and frames[0] == {
        "type": "failed",
        "reason": PREVIEW_RESTORE_DISPOSAL_REQUIRED,
    }:
        raise PreviewRestoreDisposalRequired()
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise click.ClickException("Preview filestore preparation is unavailable.")
    declaration = frames[0].get("declaration")
    if declaration is None:
        return None
    if (
        not isinstance(declaration, dict)
        or set(declaration) != {"name", "driver", "driver_opts", "labels"}
        or declaration.get("name") != data_volume + "-filestore"
        or declaration.get("driver") != "local"
        or not isinstance(declaration.get("driver_opts"), dict)
    ):
        raise click.ClickException("Preview filestore preparation returned invalid evidence.")
    return declaration


PREPARE_PREVIEW_FILESTORE_PROGRAM = (
    SNAPSHOT_FILES_LIBRARY
    + PREVIEW_FILESTORE_LIBRARY
    + r"""
import fcntl
import base64
import hashlib
import subprocess
import sys

inherited_lock_descriptors = ()


def docker(*arguments, stdin=None):
    result = subprocess.run(['docker', *arguments], input=stdin, capture_output=True, check=True, timeout=30,
        pass_fds=inherited_lock_descriptors)
    return result.stdout


def prepare(spec):
    project, data_name, database = (spec[key] for key in ('project', 'data_volume', 'database_name'))
    volumes = spec['volumes']
    if (set(volumes) != {'data', 'database', 'logs'} or len(set(volumes.values())) != 3
        or volumes.get('data') != data_name
        or any(not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,200}', value)
           for value in (project, data_name, database, *volumes.values()))):
        raise RuntimeError('Invalid preview filestore scope')
    names = set(docker('volume', 'ls', '--format', '{{.Name}}').decode().splitlines())
    name = data_name + '-filestore'
    snapshot = spec.get('snapshot')
    if name not in names and snapshot is None:
        if spec.get('require_existing'):
            raise RuntimeError('Retained preview overlay is missing')
        return None
    root = Path.home() / CLEAN_SNAPSHOT_STATE_DIRECTORY
    if root.resolve(strict=True) != root.absolute():
        raise RuntimeError('Preview snapshot custody is aliased')
    locks = root / 'preview-locks'
    locks.mkdir(mode=0o700, exist_ok=True)
    if locks.resolve(strict=True) != locks.absolute():
        raise RuntimeError('Preview target lock custody is aliased')
    target = hashlib.sha256(project.encode()).hexdigest()
    with os.fdopen(os.open(locks / (target + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600), 'a') as target_lock:
        fcntl.flock(target_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        global inherited_lock_descriptors
        inherited_lock_descriptors = (target_lock.fileno(),)
        try:
            return prepare_locked(spec, root)
        finally:
            inherited_lock_descriptors = ()



def prepare_locked(spec, root):
    project, data_name, database = (spec[key] for key in ('project', 'data_volume', 'database_name'))
    names = set(preview_docker_text('volume', 'ls', '--format', '{{.Name}}').splitlines())
    name = data_name + '-filestore'
    with (root / 'snapshot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        declaration = None
        if name in names:
            volume, _, _, _ = inspect_preview_filestore(data_name, project)
            declaration = {'name': name, 'driver': 'local', 'driver_opts': volume['Options'],
                'labels': {'io.launchplane.preview-snapshot': volume['Labels']['io.launchplane.preview-snapshot']}}
            if spec['snapshot'] is None:
                return declaration
        attempt = preview_restore_attempt(project, database, spec['volumes'], spec['snapshot'], root) if set(spec['volumes'].values()) <= names else None
        previous = attempt['previous'] if attempt is not None else None
        if declaration is None and previous is not None and previous.get('completed'):
            raise RuntimeError('Retained preview overlay is missing')
        snapshot = attempt['snapshot'] if attempt is not None else spec['snapshot']
        identifiers = preview_docker_text('ps', '-aq', '--no-trunc').splitlines()
        containers = []
        if identifiers:
            result = subprocess.run(['docker', 'inspect', *identifiers], capture_output=True,
                check=False, timeout=30, pass_fds=inherited_lock_descriptors)
            if result.returncode and not missing_container_error(result.stderr):
                raise RuntimeError('Preview container inspection is unavailable')
            containers = json.loads(result.stdout)
            found = [item.get('Id') for item in containers if isinstance(item, dict)]
            if (len(found) != len(containers) or len(set(found)) != len(found)
                or not set(found).issubset(identifiers)
                or not result.returncode and set(found) != set(identifiers)):
                raise RuntimeError('Preview container inventory is incomplete')
        databases = []
        for container in containers:
            labels = container.get('Config', {}).get('Labels') or {}
            if any(mount.get('Name') in spec['volumes'].values() for mount in container.get('Mounts', [])) and labels.get('com.docker.compose.project') != project:
                raise RuntimeError('Preview backing has another consumer')
            if labels.get('com.docker.compose.project') == project and labels.get('com.docker.compose.service') == 'database':
                databases.append(container)
        if len(databases) > 1:
            raise RuntimeError('Preview database custody is ambiguous')
        if databases:
            mounts = databases[0].get('Mounts', [])
            if (len(mounts) != 1 or mounts[0].get('Type') != 'volume'
                or mounts[0].get('Name') != spec['volumes']['database']
                or mounts[0].get('Destination') != spec['database_data_directory']
                or spec['volumes']['database'] not in names
                or attempt is not None and mounts[0].get('Source') != attempt['binding']['volume_backings']['database']['path']):
                raise RuntimeError('Preview database physical binding differs')
            present = preview_restore_database_custody(databases[0]['Id'], database,
                spec['copy_witness_parameter'], attempt)
            if declaration is None and present == '1':
                if spec.get('require_existing'):
                    raise RuntimeError('Retained preview overlay is missing')
                return None
        elif previous is not None and previous.get('completed'):
            raise RuntimeError('A completed preview lost its database; preserve its retained data')
        elif spec['volumes']['database'] in names:
            backing = json.loads(docker('volume', 'inspect', spec['volumes']['database']))[0]
            if any(Path(backing['Mountpoint']).iterdir()):
                raise RuntimeError('Preview database custody is unavailable')
        if declaration is not None:
            if previous is None:
                _, data, _, private = inspect_preview_filestore(data_name, project)
                require_unused_preview_backing(data)
                require_unused_preview_upper(private)
            return declaration
        data, lower, private, options = preview_filestore_layout(data_name, project, snapshot['snapshot_id'], allow_missing=True)
        require_unused_preview_backing(data)
        require_unused_preview_upper(private)
        if not lower.is_dir():
            raise RuntimeError('Preview snapshot source is unavailable')
        receipt_path = lower.parent / 'receipt.json'
        if receipt_path.is_symlink() or receipt_path.stat().st_size > 4096:
            raise RuntimeError('Preview snapshot receipt is invalid')
        receipt = json.loads(receipt_path.read_text())
        if any(receipt.get(key) != snapshot[key] for key in ('snapshot_id', 'database_sha256', 'filestore_sha256')):
            raise RuntimeError('Preview snapshot differs from its service receipt')
        dump_path = lower.parent / 'database.dump'
        if dump_path.is_symlink() or not dump_path.is_file():
            raise RuntimeError('Preview snapshot database dump custody is invalid')
        dump_bytes = dump_path.stat().st_size
        if not 0 < dump_bytes <= min(snapshot['storage_budget']['database_bytes'], snapshot['storage_budget']['staging_bytes']):
            raise RuntimeError('Preview snapshot database dump exceeds reviewed bounds')
        available_path = Path(data['Mountpoint'])
        while not available_path.exists():
            available_path = available_path.parent
        space = os.statvfs(available_path)
        if space.f_bavail * space.f_frsize < snapshot['storage_budget']['reserve_bytes'] + snapshot['storage_budget']['database_bytes'] + dump_bytes:
            raise RuntimeError('Preview overlay lacks reviewed disk headroom')
        declaration = {'name': name, 'driver': 'local', 'driver_opts': options,
            'labels': {'io.launchplane.preview-snapshot': snapshot['snapshot_id']}}
        if not spec.get('apply', True):
            return declaration
        for role, volume_name in spec['volumes'].items():
            if volume_name not in names:
                compose_role = {'data': 'odoo_data', 'database': 'odoo_db', 'logs': 'odoo_logs'}[role]
                docker('volume', 'create', '--label', 'com.docker.compose.project=' + project,
                    '--label', 'com.docker.compose.volume=' + compose_role, volume_name)
        attempt = preview_restore_attempt(project, database, spec['volumes'], snapshot, root)
        write_preview_restore_attempt(attempt, completed=False, started=False)
        for directory in (private, private / 'upper', private / 'work'):
            directory.mkdir(mode=0o700, exist_ok=True)
            if directory.resolve(strict=True) != directory.absolute():
                raise RuntimeError('Preview filestore writable custody is aliased')
            os.chown(directory, lower.stat().st_uid, lower.stat().st_gid)
        arguments = ['volume', 'create', '--driver', 'local', '--label', 'com.docker.compose.project=' + project,
            '--label', 'com.docker.compose.volume=odoo_filestore', '--label', 'io.launchplane.preview-snapshot=' + snapshot['snapshot_id']]
        for key, value in options.items():
            arguments.extend(['--opt', key + '=' + value])
        docker(*arguments, name)
        inspect_preview_filestore(data_name, project)
        return declaration

try:
    declaration = prepare(json.loads(base64.b64decode(sys.argv[1], validate=True)))
    print(sys.argv[2] + json.dumps({'type': 'complete', 'declaration': declaration}), flush=True)
except InterruptedPreviewRestore:
    print(sys.argv[2] + json.dumps({'type': 'failed', 'reason': PREVIEW_RESTORE_DISPOSAL_REQUIRED}), flush=True)
except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
    print(sys.argv[2] + json.dumps({'type': 'failed'}), flush=True)
"""
)
