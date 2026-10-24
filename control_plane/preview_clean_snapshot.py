import base64
import gzip
import json

from control_plane.clean_testing_snapshot import CleanTestingSnapshot
from control_plane._odoo_clean_snapshot_program import CLEAN_TESTING_SNAPSHOT_LIBRARY
from control_plane.odoo_testing_restore import TESTING_COPY_RUNNER
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
    + r"""
import base64
import shutil
import tempfile
import uuid


def command(arguments, *, stdin=None):
    result = subprocess.run(arguments, input=stdin, stdout=subprocess.PIPE,
        stderr=subprocess.PIPE, check=False)
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


def restore_preview_snapshot():
    snapshot = spec['snapshot']
    path, receipt = verified_clean_snapshot(snapshot['snapshot_id'])
    if any(receipt[key] != snapshot[key] for key in ('database_sha256', 'filestore_sha256')):
        raise RuntimeError('The clean snapshot differs from its service receipt')
    containers = inspect_copy_runtime(spec, expected_instance='preview')
    database = spec['database_name']
    if not re.fullmatch(r'[a-zA-Z0-9_][a-zA-Z0-9_.-]{0,62}', database):
        raise RuntimeError('Invalid preview database identity')
    present = docker('exec', '-i', containers['database'][0], 'sh', '-c',
        'psql -X -qAt -v ON_ERROR_STOP=1 -v database_name="$1" -U "$POSTGRES_USER" -d postgres',
        '_', database, stdin=b"SELECT count(*) FROM pg_database WHERE datname = :'database_name';\n").decode().strip()
    if present != '0':
        raise RuntimeError('An existing preview database cannot be overwritten by a snapshot')
    runner = containers['script-runner'][0]
    docker('stop', containers['web'][0])
    docker('exec', '-u', 'root', runner, 'rm', '-f', spec['web_gate_path'])
    mounts = [mount for mount in containers['script-runner'][1]['Mounts']
        if mount.get('Destination') == '/volumes/data']
    if len(mounts) != 1:
        raise RuntimeError('Preview data volume is ambiguous')
    volume = json.loads(docker('volume', 'inspect', mounts[0]['Name']))[0]
    if (volume.get('Driver') != 'local' or volume.get('Options')
        or (volume.get('Labels') or {}).get('com.docker.compose.project') != spec['compose_project']):
        raise RuntimeError('Preview data volume is not exclusively owned')
    mounted = docker('ps', '--all', '--filter', 'volume=' + mounts[0]['Name'],
        '--format', '{{.ID}}').decode().splitlines()
    if set(mounted) != {runner, containers['web'][0]}:
        raise RuntimeError('Preview data volume has another or unknown consumer')
    docker('pause', runner)
    try:
        destination = Path(volume['Mountpoint']) / 'filestore' / database
        if destination.resolve() != destination.absolute() or destination.exists():
            raise RuntimeError('Preview filestore already exists or is aliased')
        destination.parent.mkdir(parents=True, exist_ok=True)
        command(['cp', '-a', '--reflink=auto', str(path / 'filestore'), str(destination)])
    finally:
        docker('unpause', runner)
    dump = '/tmp/preview-snapshot-' + uuid.uuid4().hex + '.dump'
    try:
        docker('cp', str(path / 'database.dump'), runner + ':' + dump)
        uid = docker('exec', runner, 'id', '-u').decode().strip()
        gid = docker('exec', runner, 'id', '-g').decode().strip()
        if not uid.isdigit() or not gid.isdigit():
            raise RuntimeError('Preview workflow identity is unavailable')
        docker('exec', '-u', 'root', runner, 'chown', '-R', uid + ':' + gid, '/volumes/data', dump)
        arguments = ['exec', '-i']
        environment = {**spec['workflow_environment'], 'PLATFORM_INSTANCE': 'preview',
            'ODOO_FILESTORE_PATH': '/volumes/data/filestore', 'ODOO_FILESTORE_OWNER': uid + ':' + gid,
            'ENV_OVERRIDE_DISABLE_CRON': 'true', 'ODOO_RESTORE_KEPT_INTEGRATIONS': '', 'NO_SANITIZE': 'false'}
        for key, value in environment.items():
            if not re.fullmatch(r'[A-Z][A-Z0-9_]*', key):
                raise RuntimeError('Invalid preview workflow environment key')
            arguments.extend(['-e', key + '=' + value])
        output = docker(*arguments, runner, 'python3', '-I', '-', dump, database, spec['readback_spec'],
            base64.b64encode(spec['readback_program'].encode()).decode(), 'fresh', snapshot['snapshot_id'],
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
        docker(*arguments, runner, 'python3', '-I', '-c',
            "import hashlib,os;from pathlib import Path;Path(" + repr(spec['web_gate_path']) + ").write_text(hashlib.sha256((os.environ['ODOO_DB_NAME']+chr(10)+os.environ.get('ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64','')).encode()).hexdigest())")
        docker('start', containers['web'][0])
        for key in ('testing_copy_sanitized', 'testing_copy_pre_registry_sanitized',
            'odoo_module_update_completed', 'odoo_module_update_modules_configured',
            'integration_readback_ok', 'odoo_restore_completed'):
            print(key + '=true')
        print('clean_preview_snapshot_restored=true')
    finally:
        docker('exec', '-u', 'root', runner, 'rm', '-f', dump)


restore_preview_snapshot()
"""
)
