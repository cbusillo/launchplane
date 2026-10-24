from control_plane._odoo_snapshot_files import SNAPSHOT_FILES_LIBRARY


CLEAN_TESTING_SNAPSHOT_LIBRARY = (
    SNAPSHOT_FILES_LIBRARY
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


def retire_clean_snapshots(spec):
    root = clean_snapshot_root(spec['copy_binding_sha256']).parent
    with (root / 'snapshot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        pinned = set()
        attempts = root / 'preview-attempts'
        if attempts.exists():
            if attempts.is_symlink():
                raise RuntimeError('Snapshot reader custody is aliased')
            for attempt in attempts.iterdir():
                if attempt.is_symlink() or not attempt.is_file() or attempt.stat().st_size > 8192:
                    raise RuntimeError('Snapshot reader custody is unavailable')
                payload = json.loads(attempt.read_text())
                if not payload.get('completed'):
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
            shutil.rmtree(path)
        descriptor = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)


def capture_clean_snapshot(spec, containers):
    snapshot_id = spec['copy_binding_sha256']
    destination = clean_snapshot_root(snapshot_id)
    if destination.exists():
        return verified_clean_snapshot(snapshot_id)[1]
    budget = spec['generation'].get('drill_storage_budget')
    if budget is None:
        raise RuntimeError('Clean snapshot capture requires reviewed storage headroom')
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
        command(['cp', '-a', '--reflink=auto', str(filestore), str(path / 'filestore')])
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
