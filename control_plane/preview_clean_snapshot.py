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
    spec: dict[str, object] = {
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
    from control_plane.host_package import host_schedule

    return host_schedule("preview_snapshot", spec)


PREVIEW_CLEAN_SNAPSHOT_PROGRAM = (
    CLEAN_TESTING_SNAPSHOT_LIBRARY
    + COPY_RUNTIME_INSPECTION_LIBRARY
    + STORAGE_MAINTENANCE_LIBRARY
    + r"""
import base64
import tempfile
import time
import uuid


def command(arguments, *, stdin=None, diagnose_failure=False):
    result = subprocess.run(arguments, input=stdin, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False, pass_fds=inherited_lock_descriptors)
    if result.returncode:
        if diagnose_failure:
            emit_copy_command_failure(result, spec)
        raise RuntimeError('Clean snapshot preview operation failed')
    return result.stdout


def docker(*arguments, stdin=None, diagnose_failure=False):
    return command(['docker', *arguments], stdin=stdin, diagnose_failure=diagnose_failure)


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
    attempt = preview_restore_attempt(spec['compose_project'], spec['database_name'],
        spec['preview_volumes'], snapshot, clean_snapshot_root(snapshot['snapshot_id']).parent)
    snapshot, witness = (attempt[key] for key in ('snapshot', 'witness'))
    database = spec['database_name']
    present = preview_restore_database_custody(containers['database'][0], database,
        spec['copy_witness_parameter'], attempt)
    path = clean_snapshot_root(snapshot['snapshot_id'])
    if present == '0':
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
    overlay_mounts = [mount for mount in containers['script-runner'][1]['Mounts']
        if mount.get('Name') == mounts[0]['Name'] + '-filestore']
    overlay = inspect_preview_filestore(mounts[0]['Name'], spec['compose_project']) if overlay_mounts else None
    if overlay and overlay[2] != path / 'filestore':
        raise RuntimeError('Preview overlay differs from its retained snapshot')
    if present == '0' and overlay is None:
        raise RuntimeError('A new snapshot preview requires its overlay filestore')
    if present == '0':
        require_unused_preview_backing(volume)
        require_unused_preview_upper(overlay[3])
    if present == '0' and not storage_capacity({**spec, 'staging_directory': str(path.parent)},
        containers, {**snapshot['storage_budget'], 'filestore_bytes': 0},
        runner_staging_bytes=(path / 'database.dump').stat().st_size, host_staging_bytes=0):
        raise RuntimeError('Preview overlay lacks reviewed storage headroom')
    mounted = docker('ps', '--all', '--filter', 'volume=' + mounts[0]['Name'],
        '--format', '{{.ID}}').decode().splitlines()
    if set(mounted) != {runner, containers['web'][0]}:
        raise RuntimeError('Preview data volume has another or unknown consumer')
    docker('stop', containers['web'][0])
    docker('exec', '-u', 'root', runner, 'rm', '-f', spec['web_gate_path'])
    if present == '0':
        write_preview_restore_attempt(attempt, completed=False)
    dump = '/tmp/preview-snapshot-' + uuid.uuid4().hex + '.dump'
    try:
        if present == '0':
            docker('cp', str(path / 'database.dump'), runner + ':' + dump)
        uid = docker('exec', runner, 'id', '-u').decode().strip()
        gid = docker('exec', runner, 'id', '-g').decode().strip()
        if not uid.isdigit() or not gid.isdigit():
            raise RuntimeError('Preview workflow identity is unavailable')
        if overlay and (overlay[2].stat().st_uid, overlay[2].stat().st_gid) != (int(uid), int(gid)):
            raise RuntimeError('Preview overlay ownership differs from its workflow identity')
        docker('exec', '-u', 'root', runner, 'chown', uid + ':' + gid, '/volumes/data', *([dump] if present == '0' else []))
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
            'clean-snapshot', stdin=spec['runner'].encode(), diagnose_failure=True).decode()
        markers = dict(line.split('=', 1) for line in output.splitlines() if '=' in line)
        if any(markers.get(key) != 'true' for key in ('testing_copy_sanitized',
            'testing_copy_pre_registry_sanitized', 'odoo_module_update_completed',
            'odoo_module_update_modules_configured')):
            raise RuntimeError('Snapshot preview lacks its sanitization and module update proof')
        readback = docker(*arguments, runner, 'python3', '-I', '-', database, spec['readback_spec'],
            stdin=spec['readback_program'].encode()).decode()
        if 'integration_readback_ok=true' not in readback.splitlines():
            raise RuntimeError('Snapshot preview connection readback failed')
        write_preview_restore_attempt(attempt, completed=True, overlay_filestore=overlay is not None)
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
