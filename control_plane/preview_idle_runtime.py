import inspect

from control_plane._odoo_copy_runtime_guard import COPY_RUNTIME_INSPECTION_LIBRARY
from control_plane.dokploy.memory_capacity import observe_memory_headroom
from control_plane.preview_gateway import PREVIEW_GATEWAY_SOCKET


PREVIEW_IDLE_RUNTIME_PROGRAM = (
    COPY_RUNTIME_INSPECTION_LIBRARY
    + inspect.getsource(observe_memory_headroom)
    + f"\nPREVIEW_GATEWAY_SOCKET = {PREVIEW_GATEWAY_SOCKET!r}\n"
    + r"""
import base64
import fcntl
import hashlib
import sys
import time

inherited_lock_descriptors = ()


def docker(*args):
    return subprocess.run(['docker', *args], check=True, capture_output=True,
        timeout=60, pass_fds=inherited_lock_descriptors).stdout


def one_inspection(identifier):
    rows = json.loads(docker('inspect', identifier))
    if len(rows) != 1:
        raise RuntimeError('Preview container is ambiguous')
    return rows[0]


def control(router, request):
    program = 'import json,socket,sys;s=socket.socket(socket.AF_UNIX);s.settimeout(5);s.connect(sys.argv[1]);s.sendall(sys.argv[2].encode()+b"\\n");print(s.makefile().readline())'
    response = json.loads(docker('exec', router, '/venv/bin/python3', '-I', '-S', '-c',
        program, PREVIEW_GATEWAY_SOCKET, json.dumps(request)))
    if response.get('identity') != hashlib.sha256(spec['identity'].encode()).hexdigest():
        raise RuntimeError('Preview gateway generation differs')
    return response


def runtime():
    if spec['action'] == 'poll':
        selected = {}
        for identifier in docker('ps', '-aq', '--filter', 'label=com.docker.compose.project=' + spec['compose_project']).decode().splitlines():
            item = one_inspection(identifier)
            service = item['Config'].get('Labels', {}).get('com.docker.compose.service')
            if service in selected:
                raise RuntimeError('Preview idle services are ambiguous')
            selected[service] = (identifier, item)
        if set(selected) != {'database', 'script-runner', 'web', 'preview-router'}:
            raise RuntimeError('Preview idle services differ')
        routers = [selected.pop('preview-router')[1]]
        containers = selected
    else:
        containers = inspect_copy_runtime(spec, expected_instance='preview')
        routers = [one_inspection(item) for item in docker('ps', '-aq', '--filter',
            'label=com.docker.compose.project=' + spec['compose_project'], '--filter',
            'label=com.docker.compose.service=preview-router').decode().splitlines()]
    if len(routers) != 1 or not routers[0]['State']['Running']:
        raise RuntimeError('Preview gateway is unavailable')
    inspect_preview_gateway(routers[0], containers['web'][1])
    for service in ('web', 'script-runner'):
        env = dict(item.split('=', 1) for item in containers[service][1]['Config'].get('Env', []) if '=' in item)
        if env.get('LAUNCHPLANE_RUNTIME_IDENTITY_JSON') != spec['identity']:
            raise RuntimeError('Preview application generation differs')
    return containers, routers[0]['Id']


def application_state(containers):
    return {name: {'running': item['State']['Running'],
        'healthy': item['State'].get('Health', {}).get('Status') == 'healthy'}
        for name, (_, item) in containers.items()}


def wait_healthy(identifier):
    deadline = time.monotonic() + 180
    while time.monotonic() < deadline:
        state = one_inspection(identifier)['State']
        if not state['Running']:
            raise RuntimeError('Preview stopped while waking')
        if state.get('Health', {}).get('Status') == 'healthy':
            return
        time.sleep(2)
    raise RuntimeError('Preview wake health is unavailable')


def execute():
    containers, router = runtime()
    status = control(router, {'action': 'status'})
    states = application_state(containers)
    action = spec['action']
    if action in {'observe', 'poll'}:
        return {'gateway': status, 'services': states}
    request = spec['control']
    if action == 'sleep':
        if status['mode'] == 'awake':
            if any(not item['running'] or not item['healthy'] for item in states.values()):
                return {'gateway': status, 'services': states, 'accepted': False}
            if containers['script-runner'][1].get('ExecIDs', 'unknown') not in (None, []):
                return {'gateway': status, 'services': states, 'accepted': False}
        status = control(router, request)
        if not status.get('accepted'):
            return {'gateway': status, 'services': states, 'accepted': False}
        for service in ('web', 'script-runner', 'database'):
            docker('stop', '-t', '30', containers[service][0])
    elif action == 'awake':
        if status['mode'] == 'awake' and status['wake_record_id'] == request['record_id']:
            return {'gateway': status, 'services': states, 'accepted': True}
        if status['mode'] != 'asleep' or status['sleep_record_id'] != request['sleep_record_id']:
            raise RuntimeError('Preview wake lacks its original sleep receipt')
        if status['wake_record_id'] != request['record_id']:
            if status['wake_record_id']:
                raise RuntimeError('Preview wake belongs to another original operation')
            floor = spec['memory_floor']
            if floor is not None and observe_memory_headroom(Path('/proc'), Path('/sys/fs/cgroup')) < floor:
                return {'gateway': status, 'services': states, 'capacity': False, 'accepted': False}
            volume = json.loads(docker('volume', 'inspect', spec['preview_volumes']['data']))[0]
            space = os.statvfs(volume['Mountpoint'])
            if space.f_bavail * space.f_frsize < spec['disk_reserve']:
                return {'gateway': status, 'services': states, 'capacity': False, 'accepted': False}
            status = control(router, {**request, 'action': 'waking'})
            if not status.get('accepted'):
                raise RuntimeError('Preview wake admission receipt was refused')
        docker('start', containers['database'][0])
        wait_healthy(containers['database'][0])
        docker('start', containers['script-runner'][0], containers['web'][0])
        for service in ('script-runner', 'web'):
            wait_healthy(containers[service][0])
        program = 'import json,sys,urllib.request;response=json.load(urllib.request.urlopen("http://127.0.0.1:"+sys.argv[1]+sys.argv[2],timeout=10));print(json.dumps(response))'
        port = one_inspection(router)['Config']['Cmd'][1]
        health = json.loads(docker('exec', containers['web'][0], '/venv/bin/python3', '-I', '-S', '-c', program, port, spec['health_path']))
        identity = next((health[key] for key in ('runtime_identity', 'launchplane_runtime_identity', 'launchplaneRuntimeIdentity', 'LAUNCHPLANE_RUNTIME_IDENTITY_JSON') if isinstance(health.get(key), dict)), None)
        if not isinstance(identity, dict):
            raise RuntimeError('Preview health lacks its runtime identity')
        if 'source_revision' in identity:
            identity['source_git_ref'] = identity.pop('source_revision')
        expected = json.loads(spec['identity'])
        if any(expected.get(key) and identity.get(key) != expected[key] for key in (
            'product', 'context', 'instance', 'environment_kind', 'deployment_record_id',
            'artifact_id', 'source_git_ref', 'preview_id', 'preview_generation_id')):
            raise RuntimeError('Preview wake health identity differs')
        status = control(router, request)
        if not status.get('accepted'):
            raise RuntimeError('Preview gateway refused completed wake')
    else:
        raise RuntimeError('Unsupported preview idle action')
    containers, _ = runtime()
    return {'gateway': status, 'services': application_state(containers), 'accepted': True}


try:
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    if spec['action'] == 'poll':
        print(sys.argv[2] + json.dumps({'type': 'complete', 'result': execute()}), flush=True)
        sys.exit(0)
    root = Path.home() / CLEAN_SNAPSHOT_STATE_DIRECTORY / 'preview-locks'
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    if root.resolve(strict=True) != root.absolute():
        raise RuntimeError('Preview idle lock custody differs')
    target = hashlib.sha256(spec['compose_project'].encode()).hexdigest()
    descriptors = []
    try:
        names = [target + '.lock'] + (['idle-start.lock'] if spec['action'] == 'awake' else [])
        for name in names:
            descriptor = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            descriptors.append(descriptor)
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        inherited_lock_descriptors = tuple(descriptors)
        result = execute()
        frame = {'type': 'complete', 'result': result}
    finally:
        for descriptor in reversed(descriptors):
            os.close(descriptor)
except (OSError, ValueError, KeyError, IndexError, TypeError, RuntimeError, subprocess.SubprocessError):
    frame = {'type': 'failed'}
print(sys.argv[2] + json.dumps(frame), flush=True)
"""
)
