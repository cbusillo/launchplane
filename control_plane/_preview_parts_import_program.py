from pathlib import Path

from control_plane._preview_fixture_program import PREVIEW_CONTAINER_LIBRARY

ODOO_IMPORT_PROGRAM = r"""
import contextlib, hashlib, inspect, json, os, time

def execute_import(env, request):
    started = time.monotonic()
    receipt = dict(identity=request['identity'], source_authority_sha256=request['source_authority_sha256'],
        operation=request['operation'], mode=request['mode'], parts_only=True, grants='unknown',
        stage='configuration', outcome='unavailable', committed=False, binding_current=True,
        timeout_seconds=request['timeout_seconds'], elapsed_seconds=0.0,
        counts=dict(parts=None,supplier_cost_rows=None,copied_fields=None,conflicts=None,unmapped_rows=None))
    witness_key = 'launchplane.preview_parts_import.' + request['operation']
    parameters = env['ir.config_parameter'].sudo()
    env.cr.execute('SET TRANSACTION READ ONLY')
    if request['mode'] == 'readback':
        raw = parameters.get_param(witness_key) if request['operation'] else None
        if raw:
            saved = json.loads(raw)
            if (saved['identity'] != request['identity'] or saved['operation'] != request['operation']
                    or saved['source_authority_sha256'] != request['source_authority_sha256']
                    or saved['outcome'] != 'committed'):
                raise ValueError('Original witness differs')
            saved['mode'] = 'readback'
            return saved
        receipt['outcome'] = 'unknown'
        return receipt
    importer = env['fishbowl.importer']
    from odoo.addons.fishbowl_import.services.connection_settings import PARAMETER_KEYS
    raw = {key:parameters.get_str(key) or '' for key in PARAMETER_KEYS}
    observed = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if observed != request['settings_sha256']:
        return receipt
    settings = importer._get_fishbowl_settings()
    if not settings.use_ssl:
        return receipt
    receipt['stage'] = 'entrypoint'
    if 'parts_only' not in inspect.signature(importer.run_full_import).parameters:
        return receipt
    inspect.signature(importer.run_full_import).bind(parts_only=True)
    receipt['stage'] = 'grants'
    from odoo.addons.fishbowl_import.services.fishbowl_client import FishbowlClient
    with FishbowlClient(settings) as client:
        rows = client.fetch_all('SHOW GRANTS')
    classifier = {'__name__':'launchplane_grants'}
    exec(request['probe_program'], classifier)
    receipt['grants'] = classifier['classify_grants']([tuple(row.values()) for row in rows])
    if receipt['grants'] != 'read_only':
        return receipt
    receipt.update(stage='qualified', outcome='qualified', elapsed_seconds=time.monotonic()-started)
    if request['mode'] == 'dry-run':
        return receipt
    env.cr.rollback()
    env.invalidate_all()
    current = {key:parameters.get_str(key) or '' for key in PARAMETER_KEYS}
    if current != raw:
        raise ValueError('Import settings changed')
    importer.run_full_import(parts_only=True)
    receipt.update(stage='complete', outcome='committed', committed=True, elapsed_seconds=time.monotonic()-started)
    if receipt['elapsed_seconds'] >= request['timeout_seconds']:
        raise ValueError('Import deadline exceeded')
    parameters.set_param(witness_key, json.dumps(receipt, sort_keys=True, separators=(',', ':')))
    env.cr.commit()
    return receipt
"""

CONTAINER_IMPORT_PROGRAM = r"""
import json, os, selectors, signal, subprocess, sys, tempfile, time
request=json.loads(sys.stdin.read(262145))
observed=json.loads(os.environ['LAUNCHPLANE_RUNTIME_IDENTITY_JSON'])
if ({k:v for k,v in observed.items() if k!='deployed_at'} !=
        {k:v for k,v in request['identity'].items() if k!='deployed_at'}):
    raise ValueError('Runtime changed')
sys.path[:0]=['/odoo','/volumes/scripts']
from run_odoo_data_workflows import LocalServerSettings, OdooDataWorkflowRunner
local=LocalServerSettings()
if (local.host,local.platform_instance,local.db_name)!=('database','preview',request['database']):
    raise ValueError('Native preview differs')
runner=OdooDataWorkflowRunner(local,upstream=None,env_file=None)
runner.local.addons_path=','.join(str(path) for path in runner._resolve_addons_paths())
applying=request['mode']=='apply'
if applying:
    runner.acquire_data_workflow_lock()
child=None
try:
    script = ('import contextlib,json,os\n'
        'with open(os.devnull,"w") as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):\n'
        '    scope={}\n'
        '    exec('+repr(request.pop('odoo_program'))+',scope)\n'
        '    result=scope["execute_import"](env,json.loads('+repr(json.dumps(request))+'))\n'
        'print("preview_parts_import="+json.dumps(result,separators=(",",":")).encode().hex())\n')
    with tempfile.TemporaryFile() as input_file:
        input_file.write(script.encode())
        input_file.seek(0)
        child=subprocess.Popen(runner._odoo_shell_command(),stdin=input_file,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,env=runner.os_env,start_new_session=True)
    output=bytearray()
    deadline=time.monotonic()+request['timeout_seconds']
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout,selectors.EVENT_READ)
        while selector.get_map():
            if time.monotonic()>=deadline:
                raise TimeoutError('Native import writer exceeded deadline')
            for key,_ in selector.select(min(1,max(0,deadline-time.monotonic()))):
                chunk=os.read(key.fd,8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output)>65536:
                    raise ValueError('Native output exceeds bound')
    if child.wait(timeout=max(0.01,deadline-time.monotonic())):
        raise ValueError('Native import unavailable')
    receipts=[line.partition(b'=')[2] for line in output.splitlines() if line.startswith(b'preview_parts_import=')]
    if len(receipts)!=1 or len(receipts[0])>32768:
        raise ValueError('Native receipt unavailable')
    print(bytes.fromhex(receipts[0].decode()).decode(),flush=True)
finally:
    if child is not None:
        try:
            os.killpg(child.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        child.stdout.close()
    if applying:
        runner.release_data_workflow_lock()
"""


IMPORT_RECEIPT_VALIDATOR = r"""
def validate_receipt(receipt, spec):
    keys={'identity','source_authority_sha256','operation','mode','parts_only','grants','stage','outcome',
          'committed','binding_current','timeout_seconds','elapsed_seconds','counts'}
    if (not isinstance(receipt,dict) or set(receipt)!=keys or receipt['identity']!=spec['identity']
        or receipt['source_authority_sha256']!=spec['source_authority_sha256']
        or receipt['operation']!=spec['operation'] or receipt['mode']!=spec['mode']
        or receipt['parts_only'] is not True or receipt['binding_current'] is not True
        or receipt['grants'] not in {'read_only','unsafe','unknown'}
        or receipt['stage'] not in {'configuration','entrypoint','grants','qualified','import','complete'}
        or receipt['outcome'] not in {'qualified','unavailable','committed','unknown'}
        or type(receipt['committed']) is not bool
        or receipt['committed'] != (receipt['outcome']=='committed')
        or receipt['committed'] and (not spec['operation'] or receipt['stage']!='complete' or receipt['grants']!='read_only')
        or receipt['timeout_seconds']!=spec['timeout_seconds']
        or type(receipt['elapsed_seconds']) not in (int,float)
        or not 0<=receipt['elapsed_seconds']<=spec['timeout_seconds']):
        raise ValueError('Invalid import receipt')
    counts=receipt['counts']
    if not isinstance(counts,dict) or set(counts)!={'parts','supplier_cost_rows','copied_fields','conflicts','unmapped_rows'}:
        raise ValueError('Invalid import counts')
    for name,value in counts.items():
        if value is None:
            continue
        if name=='copied_fields':
            if (not isinstance(value,dict) or set(value)-{'text','number','boolean','date','relation'}
                or any(type(count) is not int or count<0 for count in value.values())):
                raise ValueError('Invalid copied field counts')
        elif type(value) is not int or value<0:
            raise ValueError('Invalid aggregate count')
"""


def parts_import_host_program() -> str:
    return (
        PREVIEW_CONTAINER_LIBRARY
        + IMPORT_RECEIPT_VALIDATOR
        + "\nCONTAINER_PROGRAM="
        + repr(CONTAINER_IMPORT_PROGRAM)
        + "\n"
        + r"""
checked_container(spec,require_running=True)
request={key:value for key,value in spec.items() if key not in {'container_id','app_name'}}
child=subprocess.Popen(['docker','exec','-i',spec['container_id'],'python3','-c',CONTAINER_PROGRAM],
    stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
try:
    child.stdin.write(json.dumps(request).encode())
    child.stdin.close()
    output=bytearray()
    deadline=time.monotonic()+spec['timeout_seconds']+30
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout,selectors.EVENT_READ)
        while selector.get_map():
            if time.monotonic()>=deadline:
                raise TimeoutError('Import observation timed out')
            for key,_ in selector.select(min(1,max(0,deadline-time.monotonic()))):
                chunk=os.read(key.fd,8192)
                if not chunk:
                    selector.unregister(key.fileobj)
                    continue
                output.extend(chunk)
                if len(output)>16384:
                    raise ValueError('Import output exceeds bound')
    if child.wait(timeout=max(0.01,deadline-time.monotonic())):
        raise ValueError('Import observation unavailable')
    receipt=json.loads(output)
    validate_receipt(receipt,spec)
    checked_container(spec,require_running=True)
    print('preview_parts_import_result='+json.dumps(receipt,separators=(',',':')).encode().hex(),flush=True)
finally:
    if child.poll() is None:
        child.kill()
        child.wait()
    child.stdout.close()
"""
    )


def parts_import_programs() -> dict[str, str]:
    return {
        "preview_parts_import": parts_import_host_program(),
        "odoo_parts_import": ODOO_IMPORT_PROGRAM,
        "import_grants_probe": Path(__file__).with_name("odoo_source_probe.py").read_text(),
    }
