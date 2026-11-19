from collections.abc import Callable
import inspect

from control_plane._odoo_website_settings_program import WEBSITE_INDEXING_PROGRAM
from control_plane.dokploy.native_copy_stream import run_managed_server_program
from control_plane.dokploy.post_deploy import ODOO_TESTING_SCHEDULE_LOCK_DIRECTORY


INDEXING_FAILURE_PATTERNS = (
    ("registry_busy", ("LockNotAvailable", "registry_loading")),
    ("origin_unbound", ("native origin binding", "native website/origin binding")),
)
INDEXING_REFUSAL_REASONS = frozenset(
    {
        "lock_busy",
        "container_binding",
        "container_state",
        "runtime_binding",
        "database_binding",
        "image_binding",
        "runner_active",
        "binding_unverified",
        "web_setup_incomplete",
    }
)
INDEXING_FAILURE_REASONS = frozenset(
    {reason for reason, _patterns in INDEXING_FAILURE_PATTERNS} | INDEXING_REFUSAL_REASONS
)


def indexing_failure_reason(output: str) -> str:
    for reason, patterns in INDEXING_FAILURE_PATTERNS:
        if any(pattern in output for pattern in patterns):
            return reason
    return ""


WEBSITE_INDEXING_HOST_PROGRAM = (
    r"""
import base64, fcntl, hashlib, json, subprocess, sys
spec = json.loads(base64.b64decode(sys.argv[1]))
prefix = sys.argv[2]
INDEXING_FAILURE_PATTERNS = __PATTERNS__
__CLASSIFIER__
def emit(result):
    print(prefix + json.dumps({'type': 'complete', 'result': result}), flush=True)
def docker(*args):
    return subprocess.check_output(['docker', *args], text=True, timeout=30)
reason = 'binding_unverified'
try:
    project = spec['project']
    lock = open('__LOCK_DIRECTORY__/launchplane-odoo-' + hashlib.sha256(project.encode()).hexdigest() + '.lock', 'a')
    reason = 'lock_busy'
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    containers = {}
    for service in ('web', 'script-runner'):
        reason = 'container_binding'
        ids = docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + project,
                     '--filter', 'label=com.docker.compose.service=' + service,
                     '--filter', 'label=com.docker.compose.oneoff=False').split()
        if len(ids) != 1:
            raise ValueError(reason)
        container = json.loads(docker('inspect', ids[0]))[0]
        reason = 'container_state'
        if not container['State']['Running'] or container['State'].get('Restarting'):
            raise ValueError(reason)
        environment = dict(value.split('=', 1) for value in container['Config']['Env'])
        reason = 'runtime_binding'
        if json.loads(environment.get('LAUNCHPLANE_RUNTIME_IDENTITY_JSON', '{}')) != spec['runtime']:
            raise ValueError(reason)
        reason = 'database_binding'
        if environment.get('ODOO_DB_NAME') != spec['database']:
            raise ValueError(reason)
        reason = 'image_binding'
        if container['Config']['Image'] != spec['image']:
            raise ValueError(reason)
        containers[service] = container
    runner = containers['script-runner']
    reason = 'runner_active'
    if runner.get('ExecIDs'):
        raise ValueError(reason)
    reason = 'binding_unverified'
    request = json.dumps(spec['request'], sort_keys=True)
except Exception:
    emit({'status': 'refused', 'reason': reason})
    raise SystemExit(0)
if spec.get('check_only') is True:
    emit({'status': 'bound'})
    raise SystemExit(0)
try:
    completed = subprocess.run(['docker', 'exec', '-i', runner['Id'], '/venv/bin/python', '-c',
                                spec['program'], spec['database'], request],
                               capture_output=True, timeout=180)
    if completed.returncode:
        emit({'status': 'failed', 'exit_code': completed.returncode,
              'reason': indexing_failure_reason(completed.stderr.decode(errors='replace'))})
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
    .replace("__PATTERNS__", repr(INDEXING_FAILURE_PATTERNS))
    .replace("__CLASSIFIER__", inspect.getsource(indexing_failure_reason))
)


def indexing_failure_evidence(result: dict[str, object]) -> dict[str, str]:
    status = result.get("status")
    if status not in {"refused", "failed", "unknown"}:
        status = "unknown"
    evidence = {"website_indexing_status": str(status)}
    exit_code = result.get("exit_code")
    if status == "failed" and type(exit_code) is int and -255 <= exit_code <= 255:
        evidence["website_indexing_exit_code"] = str(exit_code)
    reason = result.get("reason")
    if isinstance(reason, str) and reason in INDEXING_FAILURE_REASONS:
        evidence["website_indexing_reason"] = reason
    return evidence


def run_native_indexing(
    *,
    host: str,
    token: str,
    server_id: str,
    binding: dict[str, object],
    request: dict[str, object],
    before_mutation: Callable[[], None] | None = None,
    check_only: bool = False,
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
        payload={
            **binding,
            "request": request,
            "program": WEBSITE_INDEXING_PROGRAM,
            "check_only": check_only,
        },
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
