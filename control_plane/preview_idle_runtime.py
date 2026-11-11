import inspect

from control_plane._odoo_copy_runtime_guard import COPY_RUNTIME_INSPECTION_LIBRARY
from control_plane.dokploy.memory_capacity import observe_memory_headroom
from control_plane.preview_gateway import PREVIEW_GATEWAY_SOCKET


PREVIEW_IDLE_WAKE_TIMEOUT_SECONDS = 300
PREVIEW_IDLE_START_LOCK_NAME = "idle-start.lock"


PREVIEW_IDLE_RUNTIME_PROGRAM = (
    COPY_RUNTIME_INSPECTION_LIBRARY
    + inspect.getsource(observe_memory_headroom)
    + f"\nPREVIEW_GATEWAY_SOCKET = {PREVIEW_GATEWAY_SOCKET!r}\n"
    + f"WAKE_HEALTH_SECONDS = {PREVIEW_IDLE_WAKE_TIMEOUT_SECONDS!r}\n"
    + f"PREVIEW_IDLE_START_LOCK_NAME = {PREVIEW_IDLE_START_LOCK_NAME!r}\n"
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


def poll_receipts(router, status):
    program = r'''
import json, os, stat, sys
current = json.loads(sys.argv[1])
result = {'persisted_state': 'unavailable'}
for name in ('sleep', 'wake'):
    value = current.get(name + '_record_id')
    result[name + '_present'] = bool(value) if isinstance(value, str) else None
try:
    descriptor = os.open('/tmp/launchplane-preview-state.json', os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(descriptor, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > 4096:
            raise ValueError('Invalid receipt file')
        saved = json.loads(stream.read(4097))
    if not isinstance(saved, dict) or saved.get('mode') not in ('awake', 'asleep'):
        raise ValueError('Invalid receipt state')
    if not isinstance(saved.get('identity'), str):
        raise ValueError('Invalid receipt identity')
    for source in (saved, current):
        for name in ('sleep', 'wake'):
            value = source.get(name + '_record_id')
            if not isinstance(value, str) or (value and (not value.startswith('mutation-reservation-') or len(value) > 128)):
                raise ValueError('Invalid receipt')
    result.update(persisted_state='observed', identity_agrees=saved['identity'] == current.get('identity'),
        mode_agrees=saved['mode'] == current.get('mode'))
    for name in ('sleep', 'wake'):
        key = name + '_record_id'
        result[name + '_agrees'] = saved[key] == current[key]
except FileNotFoundError:
    result['persisted_state'] = 'missing'
except (ValueError, TypeError, KeyError):
    result['persisted_state'] = 'invalid'
except OSError:
    result['persisted_state'] = 'unavailable'
print(json.dumps(result))
'''
    try:
        return json.loads(docker('exec', router, '/venv/bin/python3', '-I', '-S', '-c', program, json.dumps(status)))
    except (OSError, ValueError, subprocess.SubprocessError):
        return {'persisted_state': 'unavailable'}


def poll_evidence(containers, router, status):
    environment = dict(item.split('=', 1) for item in containers['web'][1]['Config'].get('Env', []) if '=' in item)
    identity = json.loads(environment['LAUNCHPLANE_RUNTIME_IDENTITY_JSON'])
    binding = {key: identity.get(key) for key in ('product', 'context', 'instance', 'preview_id',
        'artifact_id', 'source_git_ref', 'image_reference')}
    binding['generation_id'] = identity.get('preview_generation_id')
    services = {}
    for name, (_, item) in containers.items():
        state = item.get('State', {})
        running = state.get('Running')
        health = state.get('Health', {}).get('Status')
        services[name.replace('-', '_')] = {
            'running': running if isinstance(running, bool) else None,
            'healthy': health == 'healthy' if health in ('starting', 'healthy', 'unhealthy') else None}
    return {'binding': binding, 'services': services, 'receipts': poll_receipts(router, status)}


def wait_healthy(identifier, deadline):
    while time.monotonic() < deadline:
        state = one_inspection(identifier)['State']
        health = state.get('Health', {}).get('Status')
        if not state['Running'] or state.get('Restarting') or health not in {'starting', 'healthy'}:
            raise RuntimeError('Preview wake service is stopped or unhealthy')
        if health == 'healthy':
            return
        time.sleep(2)
    raise RuntimeError('Preview wake health is unavailable')


def refuse_capacity(containers, status):
    for service in ('web', 'script-runner', 'database'):
        docker('stop', '-t', '30', containers[service][0])
    return {'gateway': status, 'services': application_state(runtime()[0]),
        'capacity': False, 'accepted': False}


def execute():
    containers, router = runtime()
    status = control(router, {'action': 'status'})
    states = application_state(containers)
    action = spec['action']
    if action in {'observe', 'poll'}:
        result = {'gateway': status, 'services': states}
        if action == 'poll' and spec.get('retain_evidence'):
            result['evidence'] = poll_evidence(containers, router, status)
        return result
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
        if status['wake_record_id'] not in {'', request['record_id']}:
            raise RuntimeError('Preview wake belongs to another original operation')
        if any(not item['running'] for item in states.values()):
            floor = spec['memory_floor']
            if floor is not None and observe_memory_headroom(Path('/proc'), Path('/sys/fs/cgroup')) < floor:
                return refuse_capacity(containers, status)
            volume = json.loads(docker('volume', 'inspect', spec['preview_volumes']['data']))[0]
            space = os.statvfs(volume['Mountpoint'])
            if space.f_bavail * space.f_frsize < spec['disk_reserve']:
                return refuse_capacity(containers, status)
        if not status['wake_record_id']:
            status = control(router, {**request, 'action': 'waking'})
            if not status.get('accepted'):
                raise RuntimeError('Preview wake admission receipt was refused')
        deadline = time.monotonic() + WAKE_HEALTH_SECONDS
        docker('start', containers['database'][0])
        wait_healthy(containers['database'][0], deadline)
        docker('start', containers['script-runner'][0], containers['web'][0])
        for service in ('script-runner', 'web'):
            wait_healthy(containers[service][0], deadline)
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
        names = [target + '.lock'] + ([PREVIEW_IDLE_START_LOCK_NAME] if spec['action'] == 'awake' else [])
        for name in names:
            descriptor = os.open(root / name, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            descriptors.append(descriptor)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                frame = {'type': 'complete', 'result': {'busy': True}}
                break
        else:
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
