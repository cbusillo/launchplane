from collections.abc import Callable

import click

from control_plane._odoo_snapshot_files import (
    CLEAN_SNAPSHOT_STATE_DIRECTORY,
    SNAPSHOT_FILES_LIBRARY,
)
from control_plane.clean_testing_snapshot import CleanTestingSnapshot


PREVIEW_FILESTORE_LIBRARY = (
    f"CLEAN_SNAPSHOT_STATE_DIRECTORY = {CLEAN_SNAPSHOT_STATE_DIRECTORY!r}\n"
    + r"""
import json
import os
from pathlib import Path
import re


def preview_filestore_layout(data_name, project, snapshot_id):
    if not re.fullmatch(r'[a-f0-9]{64}', snapshot_id):
        raise RuntimeError('Preview filestore snapshot identity is invalid')
    data = json.loads(docker('volume', 'inspect', data_name))[0]
    if (data.get('Name') != data_name or data.get('Driver') != 'local' or data.get('Options')
        or (data.get('Labels') or {}).get('com.docker.compose.project') != project):
        raise RuntimeError('Preview filestore backing is not exclusively owned')
    backing = Path(data['Mountpoint'])
    if backing.name != '_data' or backing.resolve(strict=True) != backing.absolute():
        raise RuntimeError('Preview filestore backing is aliased')
    private = backing.parent / 'launchplane-preview-filestore'
    lower = Path.home() / CLEAN_SNAPSHOT_STATE_DIRECTORY / snapshot_id / 'filestore'
    if lower.resolve(strict=True) != lower.absolute() or not lower.is_dir():
        raise RuntimeError('Preview filestore source is unavailable or aliased')
    options = {'type': 'overlay', 'device': 'overlay',
        'o': f'lowerdir={lower},upperdir={private}/upper,workdir={private}/work,userxattr'}
    return data, lower, private, options


def inspect_preview_filestore(data_name, project):
    name = data_name + '-filestore'
    volume = json.loads(docker('volume', 'inspect', name))[0]
    labels = volume.get('Labels') or {}
    snapshot_id = labels.get('io.launchplane.preview-snapshot', '')
    data, lower, private, options = preview_filestore_layout(data_name, project, snapshot_id)
    if (volume.get('Name') != name or volume.get('Driver') != 'local'
        or volume.get('Options') != options or labels.get('com.docker.compose.project') != project):
        raise RuntimeError('Preview filestore overlay identity differs')
    for directory in (private, private / 'upper', private / 'work'):
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
    dispatch_guard: Callable[[Callable[[], None]], None],
) -> dict[str, object] | None:
    from control_plane.dokploy import native_copy_stream

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
            "snapshot": snapshot.model_dump(mode="json") if snapshot else None,
        },
        receive=frames.append,
        timeout_seconds=120,
        dispatch_guard=dispatch_guard,
    )
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


def docker(*arguments):
    result = subprocess.run(['docker', *arguments], capture_output=True, check=True, timeout=30,
        pass_fds=inherited_lock_descriptors)
    return result.stdout


def prepare(spec):
    project, data_name, database = (spec[key] for key in ('project', 'data_volume', 'database_name'))
    if any(not isinstance(value, str) or not re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,200}', value)
           for value in (project, data_name, database)):
        raise RuntimeError('Invalid preview filestore scope')
    names = set(docker('volume', 'ls', '--format', '{{.Name}}').decode().splitlines())
    name = data_name + '-filestore'
    snapshot = spec.get('snapshot')
    if name not in names and snapshot is None:
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
    snapshot = spec.get('snapshot')
    names = set(docker('volume', 'ls', '--format', '{{.Name}}').decode().splitlines())
    name = data_name + '-filestore'
    with (root / 'snapshot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        if name not in names:
            identifiers = docker('ps', '-aq').decode().splitlines()
            containers = json.loads(docker('inspect', *identifiers)) if identifiers else []
            for container in containers:
                labels = container.get('Config', {}).get('Labels') or {}
                if any(mount.get('Name') in spec['volumes'].values() for mount in container.get('Mounts', [])) and labels.get('com.docker.compose.project') != project:
                    raise RuntimeError('Preview backing has another consumer')
                if labels.get('com.docker.compose.project') == project and labels.get('com.docker.compose.service') == 'database':
                    result = subprocess.run(['docker', 'exec', '-i', container['Id'], 'sh', '-c',
                        'psql -X -qAt -v ON_ERROR_STOP=1 -v database_name="$1" -U "$POSTGRES_USER" -d postgres', '_', database],
                        input=b"SELECT count(*) FROM pg_database WHERE datname = :'database_name';\n", capture_output=True, check=True, timeout=30,
                        pass_fds=inherited_lock_descriptors)
                    present = result.stdout.decode().strip()
                    if present == '1':
                        return None
                    if present != '0':
                        raise RuntimeError('Preview database custody is unavailable')
            for role, volume_name in spec['volumes'].items():
                if volume_name not in names:
                    compose_role = {'data': 'odoo_data', 'database': 'odoo_db', 'logs': 'odoo_logs'}[role]
                    docker('volume', 'create', '--label', 'com.docker.compose.project=' + project,
                        '--label', 'com.docker.compose.volume=' + compose_role, volume_name)
            binding = preview_attempt_binding(project, database, spec['volumes'], snapshot)
            attempt_id = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
            attempts = root / 'preview-attempts'
            attempts.mkdir(mode=0o700, exist_ok=True)
            if attempts.resolve(strict=True) != attempts.absolute():
                raise RuntimeError('Preview restore attempt custody is aliased')
            attempt = attempts / (attempt_id + '.json')
            previous = None
            if attempt.exists():
                if attempt.is_symlink() or attempt.stat().st_size > 8192:
                    raise RuntimeError('Preview restore attempt custody is invalid')
                previous = json.loads(attempt.read_text())
                if previous.get('binding') != binding or previous.get('completed') is not False:
                    raise RuntimeError('Preserve a completed or foreign preview restore attempt')
                snapshot = previous['snapshot']
            witness = hashlib.sha256(json.dumps(['preview-restore-v1', attempt_id, snapshot['snapshot_id']]).encode()).hexdigest()
            if previous is not None and previous.get('restore_witness') != witness:
                raise RuntimeError('Preview restore attempt witness differs')
            write_snapshot_receipt(attempt, {'binding': binding, 'snapshot': snapshot, 'restore_witness': witness, 'completed': False})
            data, lower, private, options = preview_filestore_layout(data_name, project, snapshot['snapshot_id'])
            filestore = Path(data['Mountpoint']) / 'filestore'
            if filestore.is_symlink() or filestore.exists() and any(filestore.iterdir()):
                raise RuntimeError('Preserve an existing preview filestore')
            receipt_path = lower.parent / 'receipt.json'
            if receipt_path.is_symlink() or receipt_path.stat().st_size > 4096:
                raise RuntimeError('Preview snapshot receipt is invalid')
            receipt = json.loads(receipt_path.read_text())
            if any(receipt.get(key) != snapshot[key] for key in ('snapshot_id', 'database_sha256', 'filestore_sha256')):
                raise RuntimeError('Preview snapshot differs from its service receipt')
            space = os.statvfs(Path(data['Mountpoint']))
            if space.f_bavail * space.f_frsize < snapshot['storage_budget']['reserve_bytes'] + snapshot['storage_budget']['database_bytes'] + snapshot['storage_budget']['staging_bytes']:
                raise RuntimeError('Preview overlay lacks reviewed disk headroom')
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
        volume, _, _, _ = inspect_preview_filestore(data_name, project)
        return {'name': name, 'driver': 'local', 'driver_opts': volume['Options'],
            'labels': {'io.launchplane.preview-snapshot': volume['Labels']['io.launchplane.preview-snapshot']}}


try:
    declaration = prepare(json.loads(base64.b64decode(sys.argv[1], validate=True)))
    print(sys.argv[2] + json.dumps({'type': 'complete', 'declaration': declaration}), flush=True)
except (OSError, ValueError, KeyError, TypeError, RuntimeError, subprocess.SubprocessError):
    print(sys.argv[2] + json.dumps({'type': 'failed'}), flush=True)
"""
)
