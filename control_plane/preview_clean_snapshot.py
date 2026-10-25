import base64
import gzip
import json

from control_plane.clean_testing_snapshot import CleanTestingSnapshot
from control_plane._odoo_testing_storage_program import STORAGE_MAINTENANCE_LIBRARY
from control_plane._odoo_clean_snapshot_program import CLEAN_TESTING_SNAPSHOT_LIBRARY
from control_plane.odoo_testing_restore import (
    TESTING_COPY_RUNNER,
    TESTING_COPY_COMPLETION_PARAMETER,
)
from control_plane._odoo_copy_runtime_guard import COPY_RUNTIME_INSPECTION_LIBRARY
from control_plane.dokploy.compose import ODOO_DATABASE_DATA_DIRECTORY


def build_preview_clean_snapshot_script(
    *,
    snapshot: CleanTestingSnapshot,
    server_id: str,
    compose_project: str,
    database_name: str,
    image_reference: str,
    workflow_environment: dict[str, str],
    runtime_environment: dict[str, str],
    readback_spec: str,
    readback_program: str,
    web_gate_path: str,
) -> str:
    if snapshot.server_id != server_id:
        raise ValueError("The preview and clean snapshot require the same execution server.")
    if any(
        key.startswith(("ODOO_UPSTREAM_", "DATA_WORKFLOW_SSH_")) for key in workflow_environment
    ):
        raise ValueError("A snapshot preview cannot receive an upstream connection or identity.")
    spec = {
        "snapshot": snapshot.model_dump(mode="json"),
        "copy_witness_parameter": TESTING_COPY_COMPLETION_PARAMETER,
        "compose_project": compose_project,
        "database_name": database_name,
        "image_reference": image_reference,
        "workflow_environment": workflow_environment,
        "readback_spec": readback_spec,
        "readback_program": readback_program,
        "web_gate_path": web_gate_path,
        "runner": "expected_copy_instance = 'preview'\n" + TESTING_COPY_RUNNER,
        "generation": {"instance": "preview", "namespace": compose_project},
        "preview_volumes": {
            "data": runtime_environment.get("ODOO_DATA_VOLUME", ""),
            "database": runtime_environment.get("ODOO_DB_VOLUME", ""),
            "logs": runtime_environment.get("ODOO_LOG_VOLUME", ""),
        },
        "database_data_directory": ODOO_DATABASE_DATA_DIRECTORY,
        "expected_overrides_payload": runtime_environment.get(
            "ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", ""
        ),
    }
    encoded = base64.b64encode(gzip.compress(json.dumps(spec).encode(), mtime=0)).decode()
    program = base64.b64encode(
        gzip.compress(PREVIEW_CLEAN_SNAPSHOT_PROGRAM.encode(), mtime=0)
    ).decode()
    return (
        "#!/usr/bin/env bash\nset -euo pipefail\npython3 - <<'LP_PREVIEW_SNAPSHOT'\n"
        "import base64,gzip,json\n"
        f"spec=json.loads(gzip.decompress(base64.b64decode({encoded!r})))\n"
        f"exec(compile(gzip.decompress(base64.b64decode({program!r})), '<preview-clean-snapshot>', 'exec'))\n"
        "LP_PREVIEW_SNAPSHOT\n"
    )


PREVIEW_CLEAN_SNAPSHOT_PROGRAM = (
    CLEAN_TESTING_SNAPSHOT_LIBRARY
    + COPY_RUNTIME_INSPECTION_LIBRARY
    + STORAGE_MAINTENANCE_LIBRARY
    + r"""
import base64
import shutil
import tempfile
import time
import uuid


def command(arguments, *, stdin=None):
    result = subprocess.run(arguments, input=stdin, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False, pass_fds=inherited_lock_descriptors)
    if result.returncode:
        raise RuntimeError('Clean snapshot preview operation failed')
    return result.stdout


def docker(*arguments, stdin=None):
    return command(['docker', *arguments], stdin=stdin)


def one_inspection(identifier):
    rows = json.loads(docker('inspect', identifier))
    if not isinstance(rows, list) or len(rows) != 1:
        raise RuntimeError('Preview inspection is ambiguous')
    return rows[0]


def wait_for_idle_preview_runner(containers):
    runner, inspection = containers['script-runner']
    if inspection.get('State', {}).get('Status') == 'paused':
        docker('unpause', runner)
    for attempt in range(3):
        inspection = one_inspection(runner)
        if 'ExecIDs' in inspection and inspection['ExecIDs'] in (None, []):
            return
        if attempt < 2:
            time.sleep(0.1)
    raise RuntimeError('Preview runner execution custody is active or unavailable')


def restore_preview_snapshot(containers, source_lock):
    wait_for_idle_preview_runner(containers)
    snapshot = spec['snapshot']
    binding = {key: spec[key] for key in ('compose_project', 'database_name', 'preview_volumes')}
    binding.update({key: snapshot[key] for key in ('server_id', 'product', 'context')})
    binding['volume_backings'] = {}
    for role, name in spec['preview_volumes'].items():
        volume = json.loads(docker('volume', 'inspect', name))[0]
        backing = Path(volume['Mountpoint'])
        created = volume.get('CreatedAt')
        if not isinstance(created, str) or not created:
            raise RuntimeError('Preview storage creation identity is unavailable')
        metadata = backing.stat()
        binding['volume_backings'][role] = {'created_at': created, 'path': str(backing),
            'device': metadata.st_dev, 'inode': metadata.st_ino}

    attempt_id = hashlib.sha256(json.dumps(binding, sort_keys=True).encode()).hexdigest()
    attempts = clean_snapshot_root(snapshot['snapshot_id']).parent / 'preview-attempts'
    attempts.mkdir(mode=0o700, exist_ok=True)
    attempt_path = attempts / (attempt_id + '.json')
    previous_attempt = None
    if attempt_path.exists():
        if attempt_path.is_symlink() or attempt_path.stat().st_size > 8192:
            raise RuntimeError('Preview snapshot attempt custody is invalid')
        previous_attempt = json.loads(attempt_path.read_text())
        if previous_attempt['binding'] != binding:
            raise RuntimeError('Preview snapshot attempt belongs to another target')
        snapshot = previous_attempt['snapshot']
    witness = hashlib.sha256(json.dumps(['preview-restore-v1', attempt_id, snapshot['snapshot_id']]).encode()).hexdigest()
    owned_unreleased = bool(previous_attempt and previous_attempt.get('restore_witness') == witness and previous_attempt.get('completed') is False)
    database = spec['database_name']
    if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,62}', database):
        raise RuntimeError('Invalid preview database identity')
    present = docker('exec', '-i', containers['database'][0], 'sh', '-c',
        'psql -X -qAt -v ON_ERROR_STOP=1 -v database_name="$1" -U "$POSTGRES_USER" -d postgres',
        '_', database, stdin=b"SELECT count(*) FROM pg_database WHERE datname = :'database_name';\n").decode().strip()
    if present not in {'0', '1'} or (present == '1' and previous_attempt is None):
        raise RuntimeError('An existing preview database cannot be overwritten by a snapshot')
    restart = False
    if present == '1':
        probe = ['exec', '-i', containers['database'][0], 'sh', '-c',
            'psql -X -qAt -v ON_ERROR_STOP=1 -v marker="$2" -U "$POSTGRES_USER" -d "$1"',
            '_', database, spec['copy_witness_parameter']]
        table = docker(*probe, stdin=b"SELECT to_regclass('public.ir_config_parameter') IS NOT NULL;\n").decode().strip()
        if table not in {'t', 'f'}:
            raise RuntimeError('Preview restore witness inspection is ambiguous')
        stored = docker(*probe, stdin=b"SELECT value FROM ir_config_parameter WHERE key=:'marker';\n").decode().strip() if table == 't' else ''
        if stored != witness:
            if not owned_unreleased or stored not in {'', snapshot['snapshot_id']}:
                raise RuntimeError('Preserve preview data without this attempt restore witness')
            restart = True
    path = clean_snapshot_root(snapshot['snapshot_id'])
    if present == '0' or restart:
        if previous_attempt is not None and previous_attempt.get('completed'):
            raise RuntimeError('A completed preview lost its database; preserve its retained data')
        path, receipt = verified_clean_snapshot(snapshot['snapshot_id'])
        if any(receipt[key] != snapshot[key] for key in ('database_sha256', 'filestore_sha256')):
            raise RuntimeError('The clean snapshot differs from its service receipt')
    runner = containers['script-runner'][0]
    mounts = [mount for mount in containers['script-runner'][1]['Mounts']
        if mount.get('Destination') == '/volumes/data']
    if len(mounts) != 1:
        raise RuntimeError('Preview data volume is ambiguous')
    volume = json.loads(docker('volume', 'inspect', mounts[0]['Name']))[0]
    if (volume.get('Driver') != 'local' or volume.get('Options')
        or (volume.get('Labels') or {}).get('com.docker.compose.project') != spec['compose_project']):
        raise RuntimeError('Preview data volume is not exclusively owned')
    clones = {}
    if (present == '0' or restart) and not storage_capacity({**spec, 'staging_directory': str(path.parent)},
        containers, snapshot['storage_budget'], runner_staging_bytes=(path / 'database.dump').stat().st_size,
        host_staging_bytes=0, filestore_clone_source=path.parent, clone_decisions=clones):
        raise RuntimeError('Preview snapshot clone lacks reviewed storage headroom')
    mounted = docker('ps', '--all', '--filter', 'volume=' + mounts[0]['Name'],
        '--format', '{{.ID}}').decode().splitlines()
    if set(mounted) != {runner, containers['web'][0]}:
        raise RuntimeError('Preview data volume has another or unknown consumer')
    docker('stop', containers['web'][0])
    docker('exec', '-u', 'root', runner, 'rm', '-f', spec['web_gate_path'])
    if restart:
        docker('exec', containers['database'][0], 'sh', '-c', 'dropdb --if-exists --force -U "$POSTGRES_USER" -- "$1"', '_', database)
        present = '0'
    docker('pause', runner)
    try:
        destination = Path(volume['Mountpoint']) / 'filestore' / database
        if destination.resolve() != destination.absolute():
            raise RuntimeError('Preview filestore is aliased')
        if present == '0':
            if destination.exists():
                if not owned_unreleased:
                    raise RuntimeError('Preserve preview filestore without an owned unreleased attempt')
                shutil.rmtree(destination)
            write_snapshot_receipt(attempt_path, {'binding': binding, 'snapshot': snapshot, 'restore_witness': witness, 'completed': False})
            copy_filestore(path / 'filestore', destination, clones.get('data', False))
    finally:
        docker('unpause', runner)
    dump = '/tmp/preview-snapshot-' + uuid.uuid4().hex + '.dump'
    try:
        if present == '0':
            docker('cp', str(path / 'database.dump'), runner + ':' + dump)
        uid = docker('exec', runner, 'id', '-u').decode().strip()
        gid = docker('exec', runner, 'id', '-g').decode().strip()
        if not uid.isdigit() or not gid.isdigit():
            raise RuntimeError('Preview workflow identity is unavailable')
        docker('exec', '-u', 'root', runner, 'chown', '-R', uid + ':' + gid, '/volumes/data', *([dump] if present == '0' else []))
        fcntl.flock(source_lock, fcntl.LOCK_UN)
        arguments = ['exec', '-i']
        environment = {**spec['workflow_environment'], 'PLATFORM_INSTANCE': 'preview',
            'ODOO_FILESTORE_PATH': '/volumes/data/filestore', 'ODOO_FILESTORE_OWNER': uid + ':' + gid,
            'ENV_OVERRIDE_DISABLE_CRON': 'true', 'ODOO_RESTORE_KEPT_INTEGRATIONS': '', 'NO_SANITIZE': 'false', 'OPENUPGRADE_ENABLED': 'false'}
        for key, value in environment.items():
            if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
                raise RuntimeError('Invalid preview workflow environment key')
            arguments.extend(['-e', key + '=' + value])
        output = docker(*arguments, runner, 'python3', '-I', '-', dump, database, spec['readback_spec'],
            base64.b64encode(spec['readback_program'].encode()).decode(), 'resume' if present == '1' else 'fresh', witness,
            'clean-snapshot', stdin=spec['runner'].encode()).decode()
        markers = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if any(markers.get(key) != 'true' for key in ('testing_copy_sanitized',
            'testing_copy_pre_registry_sanitized', 'odoo_module_update_completed',
            'odoo_module_update_modules_configured')):
            raise RuntimeError('Snapshot preview lacks its sanitization and module update proof')
        readback = docker(*arguments, runner, 'python3', '-I', '-', database, spec['readback_spec'],
            stdin=spec['readback_program'].encode()).decode()
        if 'integration_readback_ok=true' not in readback.splitlines():
            raise RuntimeError('Snapshot preview connection readback failed')
        write_snapshot_receipt(attempt_path, {'binding': binding, 'snapshot': snapshot, 'restore_witness': witness, 'completed': True})
        docker(*arguments, runner, 'python3', '-I', '-c',
            "import hashlib,os;from pathlib import Path;Path(" + repr(spec['web_gate_path']) + ").write_text(hashlib.sha256((os.environ['ODOO_DB_NAME']+chr(10)+os.environ.get('ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64','')).encode()).hexdigest())")
        docker('start', containers['web'][0])
        for line in output.splitlines():
            if line.startswith(('website_bootstrap_', 'website_settings_')):
                print(line)
        for key in ('testing_copy_sanitized', 'testing_copy_pre_registry_sanitized',
            'odoo_module_update_completed', 'odoo_module_update_modules_configured',
            'integration_readback_ok', 'odoo_restore_completed'):
            print(key + '=true')
        print('odoo_module_update_image_match=true')
        print('clean_preview_snapshot_id=' + snapshot['snapshot_id'])
        print('clean_preview_snapshot_restored=true')
    finally:
        docker('exec', '-u', 'root', runner, 'rm', '-f', dump)


import fcntl
root = clean_snapshot_root(spec['snapshot']['snapshot_id']).parent
locks = root / 'preview-locks'
locks.mkdir(mode=0o700, exist_ok=True)
if locks.resolve() != locks.absolute():
    raise RuntimeError('Preview target lock custody is aliased')
target = hashlib.sha256(spec['compose_project'].encode()).hexdigest()
descriptor = os.open(locks / (target + '.lock'), os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
try:
    fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
    inherited_lock_descriptors = (descriptor,)
    containers = inspect_copy_runtime(spec, expected_instance='preview')
    with (root / 'snapshot.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        restore_preview_snapshot(containers, lock)
finally:
    os.close(descriptor)
"""
)
