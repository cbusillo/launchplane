"""Fixed host maintenance library embedded behind the existing restore guard."""

MAX_DISPOSALS_PER_PASS = 4
RESTORE_STAGING_DIRECTORY = ".local/state/launchplane-testing-restore-staging"
CLONE_PROBE_BYTES = 65536
TXG_SYNC_WAIT_SECONDS = 6

STORAGE_MAINTENANCE_LIBRARY = r"""
import os
from pathlib import Path
import subprocess
import time
import uuid


def restore_staging_root():
    root = Path.home() / __RESTORE_STAGING_DIRECTORY__
    if root.resolve() != root.absolute():
        raise RuntimeError('Restore staging is aliased')
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    return root


def filestore_clone_proved(source_directory, target_directory):
    probe = Path(source_directory) / ('.lp-clone-probe-' + uuid.uuid4().hex)
    clone = Path(target_directory) / probe.name
    try:
        probe.write_bytes(os.urandom(__CLONE_PROBE_BYTES__))
        # https://openzfs.github.io/openzfs-docs/Performance%20and%20Tuning/Module%20Parameters.html#zfs-bclone-wait-dirty
        for wait in (0, __TXG_SYNC_WAIT_SECONDS__):
            time.sleep(wait)
            if subprocess.run(['cp', '--reflink=always', str(probe), str(clone)],
                    stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False).returncode == 0:
                return True
        return False
    finally:
        probe.unlink(missing_ok=True)
        clone.unlink(missing_ok=True)


def copy_filestore(source, destination, clone):
    Path(destination).mkdir(parents=True, exist_ok=True)
    arguments = ['cp', '-a', '--reflink=always' if clone else '--reflink=auto', str(source) + '/.', str(destination)]
    try:
        command(arguments)
    except RuntimeError:
        if not clone:
            raise
        time.sleep(__TXG_SYNC_WAIT_SECONDS__)
        command(arguments)


def storage_maintenance(spec, containers):
    from datetime import datetime, timezone
    gen = spec['generation']
    maintenance = spec['storage_maintenance']
    if len(maintenance['copies']) > __MAX_DISPOSALS__:
        raise RuntimeError('Unbounded storage maintenance')
    observed = docker('exec','-i',containers['database'][0],'sh','-c',
        'psql -X -q -t -A -v ON_ERROR_STOP=1 -v marker="$2" -U "$POSTGRES_USER" -d "$1"',
        '_', spec['database_name'], spec['copy_witness_parameter'],
        stdin=b"SELECT value FROM ir_config_parameter WHERE key=:'marker';\n").decode().strip()
    if observed != spec['copy_binding_sha256']:
        raise RuntimeError('Current testing copy lacks its committed witness')
    results = []
    for copy in maintenance['copies']:
        if datetime.now(timezone.utc).timestamp() >= maintenance['expires_at']:
            raise RuntimeError('Maintenance qualification expired')
        candidate = {**gen, **copy, 'disposal_expires_at':maintenance['expires_at']}
        try:
            status = discard_previous_drill(candidate, containers['database'][0], containers['script-runner'][0], spec['database_name'])
            results.append(status or 'disposed')
        except Exception:
            results.append('deferred')
    capacity = storage_capacity(spec, containers, maintenance['budget'])
    print(sys.argv[2] + json.dumps({'type':'complete', 'disposals':results, 'capacity':capacity}), flush=True)


def storage_capacity(spec, containers, budget, staged_bytes=0, assembly_bytes=0, *,
        runner_staging_bytes=None, host_staging_bytes=None, database_staging_bytes=0,
        filestore_clone_source=None, clone_decisions=None):
    gen = spec['generation']
    decisions = {} if clone_decisions is None else clone_decisions
    ssh_restore = host_staging_bytes is None and gen.get('source_mode', 'ssh') != 'container-export'
    if ssh_restore:
        filestore_clone_source = restore_staging_root()
    # Group demands on the same actual backing filesystem. Comparing each
    # volume independently would admit two allocations against the same free bytes.
    required = {}
    available = {}
    def demand(path, size):
        path = Path(path).resolve(strict=True)
        device = path.stat().st_dev
        stat = os.statvfs(path)
        free = stat.f_bavail * stat.f_frsize
        if size:
            required[device] = required.get(device, 0) + size
        available[device] = min(available.get(device, free), free)
    backings = {}
    for role, suffix, size in (('database', '-db', budget['database_bytes']), ('data', '-data', budget['filestore_bytes'])):
        name = spec.get('preview_volumes', {}).get(role, gen['namespace'] + suffix)
        volume = json.loads(docker('volume', 'inspect', name))[0]
        backings[role] = volume['Mountpoint']
        if role == 'data' and filestore_clone_source is not None:
            decisions['data'] = filestore_clone_proved(filestore_clone_source, backings[role])
            if decisions['data']:
                size = 0
        demand(backings[role], size)
    # SSH capture stages on the host; native capture/assembly stages in the
    # runner's writable layer. Require known backing rather than guessing overlay space.
    driver = containers['script-runner'][1].get('GraphDriver', {})
    upper = driver.get('Data', {}).get('UpperDir')
    if not upper:
        raise RuntimeError('Runner staging backing is unavailable')
    if runner_staging_bytes is None:
        runner_staging_bytes = (budget['database_bytes'] if ssh_restore
            else max(assembly_bytes, budget['staging_bytes'] - staged_bytes))
    demand(upper, runner_staging_bytes)
    if ssh_restore:
        demand(filestore_clone_source, budget['staging_bytes'])
    elif host_staging_bytes is not None:
        demand(spec['staging_directory'], host_staging_bytes)
    if database_staging_bytes:
        upper = containers['database'][1].get('GraphDriver', {}).get('Data', {}).get('UpperDir')
        if not upper:
            raise RuntimeError('Database staging backing is unavailable')
        demand(upper, database_staging_bytes)
    if gen.get('clean_snapshot_enabled'):
        snapshot_path = clean_snapshot_root(spec['copy_binding_sha256'], create=False).parent
        while not snapshot_path.exists():
            snapshot_path = snapshot_path.parent
        decisions['snapshot'] = filestore_clone_proved(backings['data'], snapshot_path)
        snapshot_filestore = 0 if decisions['snapshot'] else budget['filestore_bytes']
        demand(snapshot_path, budget['database_bytes'] + snapshot_filestore)
    capacity = all(available[device] >= size + budget['reserve_bytes'] for device, size in required.items())
    return capacity
"""
STORAGE_MAINTENANCE_LIBRARY = (
    STORAGE_MAINTENANCE_LIBRARY.replace("__MAX_DISPOSALS__", str(MAX_DISPOSALS_PER_PASS))
    .replace("__RESTORE_STAGING_DIRECTORY__", repr(RESTORE_STAGING_DIRECTORY))
    .replace("__CLONE_PROBE_BYTES__", str(CLONE_PROBE_BYTES))
    .replace("__TXG_SYNC_WAIT_SECONDS__", str(TXG_SYNC_WAIT_SECONDS))
)
