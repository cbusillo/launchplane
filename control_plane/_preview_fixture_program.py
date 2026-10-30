PREVIEW_FIXTURE_RESULT_MARKER = "preview_fixture_result"

PREVIEW_FIXTURE_PROGRAM = (
    "RESULT_MARKER = "
    + repr(PREVIEW_FIXTURE_RESULT_MARKER)
    + "\n"
    + r"""
import json, os, re, selectors, subprocess, time

def inspect_container(container):
    result = subprocess.run(['docker', 'inspect', container], capture_output=True, timeout=30, check=True)
    rows = json.loads(result.stdout)
    if len(rows) != 1:
        raise ValueError('Container identity unavailable')
    return rows[0]

def checked_container(spec, *, require_running):
    expected = spec['identity']
    if expected['environment_kind'] != 'preview' or expected['instance'] in {'testing', 'prod', 'production'}:
        raise ValueError('Preview required')
    web = inspect_container(spec['container_id'])
    config = web['Config']
    labels = config['Labels']
    environment = dict(item.split('=', 1) for item in config['Env'] if '=' in item)
    observed = json.loads(environment['LAUNCHPLANE_RUNTIME_IDENTITY_JSON'])
    observed.pop('deployed_at', None)
    identity = dict(expected)
    identity.pop('deployed_at', None)
    if (observed != identity or require_running and not web['State']['Running'] or config['Image'] != expected['image_reference']
            or labels['com.docker.compose.project'] != spec['app_name']
            or labels['com.docker.compose.service'] != 'web'):
        raise ValueError('Preview container changed')
    if not spec['database'] or environment.get('ODOO_DB_NAME') != spec['database']:
        raise ValueError('Preview database changed')
    return web

def run_fixture(spec):
    checked_container(spec, require_running=True)
    expected = spec['identity']
    request = {key: spec[key] for key in ('identity', 'database', 'fixture', 'mode')}
    child = subprocess.Popen(
        ['docker', 'exec', '-i', spec['container_id'], 'python3', '/opt/project/addons/launchplane_preview_fixture.py'],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        child.stdin.write(json.dumps(request).encode())
        child.stdin.close()
        selector = selectors.DefaultSelector()
        selector.register(child.stdout, selectors.EVENT_READ)
        output = bytearray()
        deadline = time.monotonic() + 180
        while selector.get_map():
            if time.monotonic() >= deadline:
                raise ValueError('Fixture observation timed out')
            for key, _ in selector.select(min(1, max(0, deadline - time.monotonic()))):
                chunk = os.read(key.fd, 8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output) > 32768:
                    raise ValueError('Fixture output exceeds bound')
        selector.close()
        if child.wait(timeout=max(1, deadline - time.monotonic())) != 0:
            raise ValueError('Fixture command failed')
        receipt = json.loads(output)
        if (not isinstance(receipt, dict) or set(receipt) != {'identity','fixture','mode','fixture_sha256','committed','verified','records'}
                or receipt.get('identity') != expected or receipt.get('fixture') != spec['fixture'] or receipt.get('mode') != spec['mode']
                or not re.fullmatch('[a-f0-9]{64}', str(receipt.get('fixture_sha256', '')))
                or receipt['verified'] is not True or type(receipt['committed']) is not bool
                or receipt['committed'] and spec['mode'] != 'apply'
                or not isinstance(receipt['records'], list) or len(receipt['records']) > 128):
            raise ValueError('Fixture receipt identity changed')
        if spec['mode'] == 'apply' and receipt['committed'] != bool(receipt['records']):
            raise ValueError('Fixture commit evidence invalid')
        identities = set()
        for record in receipt['records']:
            if (not isinstance(record, dict) or not {'model','id','disposition'} <= set(record) <= {'model','id','disposition','reference'}
                    or not isinstance(record['model'], str) or len(record['model']) > 128
                    or not re.fullmatch(r'[a-z][a-z0-9_]*(\.[a-z][a-z0-9_]*)+', record['model'])
                    or type(record['id']) is not int or record['id'] < 1
                    or record['disposition'] not in {'created','reused'}
                    or not isinstance(record.get('reference',''), str)
                    or not re.fullmatch(r'(|[A-Za-z0-9][A-Za-z0-9 /._-]{0,79})', record.get('reference',''))):
                raise ValueError('Fixture record identity invalid')
            key = (record['model'], record['id'])
            if key in identities:
                raise ValueError('Fixture record identity repeated')
            identities.add(key)
        if inspect_container(spec['container_id'])['State']['Running'] is not True:
            raise ValueError('Preview stopped during fixture')
        print(RESULT_MARKER + '=' + json.dumps(receipt, separators=(',', ':')).encode().hex(), flush=True)
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()
        child.stdout.close()

def original_container_stopped(spec):
    expected = spec['identity']
    if expected['environment_kind'] != 'preview' or expected['instance'] in {'testing', 'prod', 'production'}:
        raise ValueError('Preview required')
    container = spec['container_id']
    if not re.fullmatch('[a-f0-9]{64}', container):
        raise ValueError('Exact container identity required')
    result = subprocess.run(['docker', 'container', 'ls', '-a', '--no-trunc', '--format', '{{.ID}}'],
                            capture_output=True, timeout=30, check=True)
    identifiers = result.stdout.decode().splitlines()
    if any(not re.fullmatch('[a-f0-9]{64}', item) for item in identifiers):
        raise ValueError('Container inventory incomplete')
    if container not in identifiers:
        return True
    state = checked_container(spec, require_running=False)['State']
    if type(state.get('Running')) is not bool or type(state.get('Restarting')) is not bool:
        raise ValueError('Original fixture state unavailable')
    return not state['Running'] and not state['Restarting']

def cancel_fixture(spec):
    stopped = original_container_stopped(spec)
    if not stopped and spec['mode'] == 'cancel':
        subprocess.run(['docker', 'stop', '--time', '30', spec['container_id']], capture_output=True, timeout=45, check=True)
        stopped = original_container_stopped(spec)
    print(RESULT_MARKER + '=' + json.dumps({'container_id': spec['container_id'], 'stopped': stopped}, separators=(',', ':')).encode().hex(), flush=True)

if spec['mode'] in {'cancel', 'cancel-readback'}:
    cancel_fixture(spec)
else:
    run_fixture(spec)
"""
)
