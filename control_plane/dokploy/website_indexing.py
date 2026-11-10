from collections.abc import Callable

from control_plane._odoo_website_settings_program import WEBSITE_INDEXING_PROGRAM
from control_plane.dokploy.native_copy_stream import run_managed_server_program
from control_plane.dokploy.post_deploy import ODOO_TESTING_SCHEDULE_LOCK_DIRECTORY


WEBSITE_INDEXING_HOST_PROGRAM = r"""
import base64, fcntl, hashlib, json, subprocess, sys
spec = json.loads(base64.b64decode(sys.argv[1]))
prefix = sys.argv[2]
def emit(result):
    print(prefix + json.dumps({'type': 'complete', 'result': result}), flush=True)
def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, timeout=30)
try:
    project = spec['project']
    lock = open('__LOCK_DIRECTORY__/launchplane-odoo-' + hashlib.sha256(project.encode()).hexdigest() + '.lock', 'a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    containers = {}
    for service in ('web', 'script-runner'):
        ids = docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project,
                     '--filter', 'label=com.docker.compose.service=' + service,
                     '--filter', 'label=com.docker.compose.oneoff=False').split()
        if len(ids) != 1:
            raise ValueError('container binding')
        container = json.loads(docker('inspect', ids[0]))[0]
        if not container['State']['Running'] or container['State'].get('Restarting'):
            raise ValueError('container state')
        environment = dict(value.split('=', 1) for value in container['Config']['Env'])
        if json.loads(environment.get('LAUNCHPLANE_RUNTIME_IDENTITY_JSON', '{}')) != spec['runtime']:
            raise ValueError('runtime binding')
        if environment.get('ODOO_DB_NAME') != spec['database']:
            raise ValueError('database binding')
        if container['Config']['Image'] != spec['image']:
            raise ValueError('image binding')
        containers[service] = container
    runner = containers['script-runner']
    if runner.get('ExecIDs'):
        raise ValueError('runner active')
    request = json.dumps(spec['request'], sort_keys=True)
except Exception:
    emit({'status': 'refused'})
    raise SystemExit(0)
try:
    completed = subprocess.run(['docker', 'exec', '-i', runner['Id'], '/venv/bin/python', '-c',
                                spec['program'], spec['database'], request],
                               capture_output=True, timeout=180)
    if completed.returncode:
        emit({'status': 'failed'})
    else:
        lines = [line.split('=', 1)[1] for line in completed.stdout.decode().splitlines()
                 if line.startswith('website_indexing_result=')]
        if len(lines) != 1:
            emit({'status': 'unknown'})
        else:
            emit({'status': 'complete', 'native': json.loads(lines[0])})
except Exception:
    emit({'status': 'unknown'})
""".replace("__LOCK_DIRECTORY__", str(ODOO_TESTING_SCHEDULE_LOCK_DIRECTORY))


def run_native_indexing(
    *,
    host: str,
    token: str,
    server_id: str,
    binding: dict[str, object],
    request: dict[str, object],
    before_mutation: Callable[[], None] | None = None,
) -> dict[str, object]:
    frames: list[dict[str, object]] = []

    def dispatch(send: Callable[[], None]) -> None:
        if before_mutation is not None:
            before_mutation()
        send()

    run_managed_server_program(
        host=host,
        token=token,
        server_id=server_id,
        program=WEBSITE_INDEXING_HOST_PROGRAM,
        payload={**binding, "request": request, "program": WEBSITE_INDEXING_PROGRAM},
        receive=frames.append,
        timeout_seconds=240,
        dispatch_guard=dispatch if before_mutation is not None else None,
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise ValueError("Native indexing outcome is unknown.")
    result = frames[0].get("result")
    if not isinstance(result, dict):
        raise ValueError("Native indexing outcome is unknown.")
    return result
