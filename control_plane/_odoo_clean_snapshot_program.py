from control_plane._odoo_snapshot_files import SNAPSHOT_FILES_LIBRARY
from control_plane.preview_filestore import PREVIEW_FILESTORE_LIBRARY


CLEAN_TESTING_SNAPSHOT_LIBRARY = (
    SNAPSHOT_FILES_LIBRARY
    + PREVIEW_FILESTORE_LIBRARY
    + r"""
import json
from pathlib import Path
import re
import stat
import fcntl
import shutil


def clean_snapshot_root(snapshot_id, *, create=True):
    if not re.fullmatch(r'[a-f0-9]{64}', snapshot_id):
        raise RuntimeError('Invalid clean snapshot identity')
    root = Path.home() / CLEAN_SNAPSHOT_STATE_DIRECTORY
    if root.resolve() != root.absolute():
        raise RuntimeError('Clean snapshot custody is aliased')
    if create:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        os.chmod(root, 0o700)
    return root / snapshot_id


def snapshot_tree_digest(path, bound=None):
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError('Clean snapshot filestore is unavailable')
    digest = hashlib.sha256()
    size = 0
    for entry in sorted(path.rglob('*')):
        metadata = entry.lstat()
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise RuntimeError('Clean snapshot filestore contains an unsupported entry')
        relative = entry.relative_to(path).as_posix()
        if entry.is_file():
            size += metadata.st_size
            if bound is not None and size > bound:
                raise RuntimeError('Clean snapshot filestore exceeds its reviewed storage bound')
            digest.update(json.dumps([relative, snapshot_file_digest(entry)]).encode())
        else:
            digest.update(json.dumps([relative, None]).encode())
    return digest.hexdigest()


def verified_clean_snapshot(snapshot_id):
    path = clean_snapshot_root(snapshot_id)
    if path.is_symlink() or not path.is_dir():
        raise RuntimeError('The completed clean snapshot is unavailable')
    receipt_path = path / 'receipt.json'
    if receipt_path.is_symlink() or receipt_path.stat().st_size > 4096:
        raise RuntimeError('The clean snapshot receipt is invalid')
    receipt = json.loads(receipt_path.read_text())
    if (receipt.get('snapshot_id') != snapshot_id
        or (path / 'database.dump').is_symlink()
        or snapshot_file_digest(path / 'database.dump') != receipt.get('database_sha256')
        or snapshot_tree_digest(path / 'filestore') != receipt.get('filestore_sha256')):
        raise RuntimeError('Clean snapshot integrity readback failed')
    return path, receipt


def snapshot_attempt_backings_retired(binding, inventory):
    names = binding.get('preview_volumes')
    backings = binding.get('volume_backings')
    if not isinstance(names, dict) or set(names) != {'data', 'database', 'logs'} or not isinstance(backings, dict):
        return False
    for role, name in names.items():
        previous = backings[role]
        old_path = Path(previous['path'])
        if old_path.resolve(strict=False) != old_path.absolute():
            return False
        try:
            old_metadata = old_path.stat()
        except FileNotFoundError:
            old_metadata = None
        if old_metadata is not None and (old_metadata.st_dev, old_metadata.st_ino) == (previous['device'], previous['inode']):
            return False
        if name not in inventory:
            continue
        rows = json.loads(docker('volume', 'inspect', name))
        if not isinstance(rows, list) or len(rows) != 1:
            return False
        volume = rows[0]
        if volume.get('Name') != name or volume.get('Driver') != 'local' or volume.get('Options'):
            return False
        path = Path(volume['Mountpoint'])
        if path.resolve(strict=True) != path.absolute():
            return False
        metadata = path.stat()
        if (not volume.get('CreatedAt') or volume['CreatedAt'] == previous['created_at']
            or (str(path), metadata.st_dev, metadata.st_ino) == (previous['path'], previous['device'], previous['inode'])):
            return False
    return True


def retire_clean_snapshots(spec):
    root = clean_snapshot_root(spec['copy_binding_sha256']).parent
    try:
        retire_clean_snapshot_bytes(spec, root)
    except (RuntimeError, OSError, ValueError, KeyError, TypeError, IndexError):
        print('clean_snapshot_retirement_deferred=true')


def retire_clean_snapshot_bytes(spec, root):
    with (root / 'snapshot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        for tombstone in root.iterdir():
            if not re.fullmatch(r'\.retiring-[a-f0-9]{64}', tombstone.name):
                continue
            if tombstone.is_symlink() or not tombstone.is_dir():
                raise RuntimeError('Snapshot retirement custody is unavailable')
            shutil.rmtree(tombstone)
        pinned = set()
        overlay_names = docker('volume', 'ls', '--filter', 'label=io.launchplane.preview-snapshot', '--format', '{{.Name}}').decode().splitlines()
        for name in overlay_names:
            volume = json.loads(docker('volume', 'inspect', name))[0]
            snapshot_id = (volume.get('Labels') or {}).get('io.launchplane.preview-snapshot', '')
            if not isinstance(snapshot_id, str) or not re.fullmatch(r'[a-f0-9]{64}', snapshot_id):
                raise RuntimeError('Snapshot overlay reader identity is unavailable')
            pinned.add(snapshot_id)
        attempts = root / 'preview-attempts'
        inventory = None
        inventory_read = False
        if attempts.exists():
            if attempts.is_symlink():
                raise RuntimeError('Snapshot reader custody is aliased')
            for attempt in attempts.iterdir():
                if attempt.name.startswith('.pending-'):
                    continue
                if not re.fullmatch(r'[a-f0-9]{64}\.json', attempt.name):
                    raise RuntimeError('Snapshot reader custody is unavailable')
                if attempt.is_symlink() or not attempt.is_file() or attempt.stat().st_size > 8192:
                    raise RuntimeError('Snapshot reader custody is unavailable')
                payload = json.loads(attempt.read_text())
                if not isinstance(payload, dict) or not isinstance(payload.get('snapshot'), dict) or not isinstance(payload.get('binding'), dict) or not isinstance(payload.get('completed', False), bool) or not isinstance(payload.get('overlay_filestore', False), bool):
                    raise RuntimeError('Snapshot reader custody is unavailable')
                binding = payload['binding']
                expected_name = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest() + '.json'
                snapshot_id = payload['snapshot'].get('snapshot_id', '')
                if attempt.name != expected_name or not isinstance(snapshot_id, str) or not re.fullmatch(r'[a-f0-9]{64}', snapshot_id):
                    raise RuntimeError('Snapshot reader binding is unavailable')
                if any(payload['snapshot'].get(key) != binding.get(key) for key in ('product', 'context', 'server_id')):
                    raise RuntimeError('Snapshot reader source is unavailable')
                if binding.get('volume_backings'):
                    if not inventory_read:
                        inventory_read = True
                        try:
                            names = docker('volume', 'ls', '--format', '{{.Name}}').decode().splitlines()
                            inventory = set(names) if len(names) == len(set(names)) and all(re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]*', name) for name in names) else None
                        except (RuntimeError, OSError, ValueError):
                            inventory = None
                    try:
                        retired = inventory is not None and snapshot_attempt_backings_retired(binding, inventory)
                    except (RuntimeError, OSError, ValueError, KeyError, TypeError, IndexError):
                        retired = False
                    if retired:
                        attempt.unlink()
                        continue
                if not payload.get('completed') or payload.get('overlay_filestore'):
                    pinned.add(payload['snapshot']['snapshot_id'])
        for snapshot in spec.get('clean_snapshot_retirements', []):
            snapshot_id = snapshot['snapshot_id']
            if snapshot_id == spec['copy_binding_sha256'] or snapshot_id in pinned:
                continue
            path = clean_snapshot_root(snapshot_id)
            if not path.exists():
                continue
            path, receipt = verified_clean_snapshot(snapshot_id)
            if any(receipt[key] != snapshot[key] for key in ('database_sha256', 'filestore_sha256')):
                raise RuntimeError('Obsolete snapshot differs from its immutable service receipt')
            tombstone = root / ('.retiring-' + snapshot_id)
            os.rename(path, tombstone)
            descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
            try:
                os.fsync(descriptor)
            finally:
                os.close(descriptor)
            shutil.rmtree(tombstone)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def capture_clean_snapshot(spec, containers, clone):
    snapshot_id = spec['copy_binding_sha256']
    destination = clean_snapshot_root(snapshot_id)
    if destination.exists():
        return verified_clean_snapshot(snapshot_id)[1]
    budget = spec['generation'].get('drill_storage_budget')
    if budget is None:
        raise RuntimeError('Clean snapshot capture requires reviewed storage headroom')
    space = os.statvfs(destination.parent)
    required = budget['database_bytes'] + budget['filestore_bytes'] + budget['reserve_bytes']
    if space.f_bavail * space.f_frsize < required:
        raise RuntimeError('Clean snapshot capture lacks reviewed storage headroom')
    web = json.loads(docker('inspect', containers['web'][0]))[0]
    if (web.get('State') or {}).get('Running') is not False:
        raise RuntimeError('Clean snapshot capture requires a stopped testing writer')
    volume = json.loads(docker('volume', 'inspect', spec['generation']['namespace'] + '-data'))[0]
    mountpoint = Path(volume['Mountpoint'])
    filestore = mountpoint / 'filestore' / spec['database_name']
    if filestore.resolve() != filestore.absolute():
        raise RuntimeError('Clean snapshot source filestore is aliased')
    expected_tree = snapshot_tree_digest(filestore, budget['filestore_bytes'])
    with tempfile.TemporaryDirectory(prefix=snapshot_id + '-', dir=destination.parent) as temporary:
        path = Path(temporary)
        status, _, _ = capture_snapshot_file(
            ['docker', 'exec', containers['database'][0], 'sh', '-c',
             'pg_dump -Fc --no-owner --no-acl -U "$POSTGRES_USER" -- "$1"', '_', spec['database_name']],
            path / 'database.dump', budget['database_bytes'])
        if status:
            raise RuntimeError('Clean snapshot database capture failed')
        if clone is None:
            clone = filestore_clone_proved(mountpoint, destination.parent)
        copy_filestore(filestore, path / 'filestore', clone)
        actual_tree = snapshot_tree_digest(path / 'filestore', budget['filestore_bytes'])
        if actual_tree != expected_tree:
            raise RuntimeError('Clean snapshot filestore changed during capture')
        receipt = {'snapshot_id': snapshot_id,
            'database_sha256': snapshot_file_digest(path / 'database.dump'),
            'filestore_sha256': actual_tree}
        with (path / 'receipt.json').open('x') as output:
            json.dump(receipt, output, sort_keys=True)
            output.flush()
            os.fsync(output.fileno())
        sync_snapshot_directory(path)
        os.rename(path, destination)
        descriptor = os.open(destination.parent, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return receipt
"""
)
