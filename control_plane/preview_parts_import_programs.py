from control_plane._preview_fixture_program import (
    PREVIEW_CONTAINER_LIBRARY,
    PREVIEW_PROCESS_LIBRARY,
)
from control_plane.odoo_source_probe import grant_classifier_program

ODOO_IMPORT_PROGRAM = r"""
import hashlib, inspect, json, time

def new_receipt(request):
    return dict(identity=request['identity'], source_authority_sha256=request['source_authority_sha256'],
        operation=request['operation'], mode=request['mode'], parts_only=True, grants='unknown',
        stage='configuration', outcome='unavailable', committed=False, binding_current=True,
        timeout_seconds=request['timeout_seconds'], elapsed_seconds=0.0,
        counts=dict(parts=None,supplier_cost_rows=None,copied_fields=None,conflicts=None,unmapped_rows=None))

def measured_receipt(receipt, started):
    receipt['elapsed_seconds'] = time.monotonic()-started
    return receipt

def qualify_import(env, request, receipt):
    parameters = env['ir.config_parameter'].sudo()
    importer = env['fishbowl.importer']
    from odoo.addons.fishbowl_import.services.connection_settings import PARAMETER_KEYS, resolve_connection_settings
    raw = {key:parameters.get_str(key) or '' for key in PARAMETER_KEYS}
    observed = hashlib.sha256(json.dumps(raw, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    if observed != request['settings_sha256']:
        return None
    settings = resolve_connection_settings(raw)
    receipt['stage'] = 'entrypoint'
    inspect.signature(importer.run_full_import).bind()
    receipt['stage'] = 'grants'
    from odoo.addons.fishbowl_import.services.fishbowl_client import FishbowlClient
    with FishbowlClient(settings) as client:
        rows = client.fetch_all('SHOW GRANTS')
    classifier = {'__name__':'launchplane_grants'}
    exec(request['probe_program'], classifier)
    receipt['grants'] = classifier['classify_grants']([tuple(row.values()) for row in rows])
    if receipt['grants'] != 'read_only':
        return None
    receipt.update(stage='qualified', outcome='qualified')
    return importer, raw

def execute_import(env, request):
    started = time.monotonic()
    receipt = new_receipt(request)
    witness_key = 'launchplane.preview_parts_import.' + request['operation']
    try:
        parameters = env['ir.config_parameter'].sudo()
        env.cr.execute('SET TRANSACTION READ ONLY')
        witness = parameters.get_str(witness_key)
        if request['mode'] == 'readback':
            if not witness:
                receipt['outcome'] = 'unknown'
                return measured_receipt(receipt, started)
            saved = json.loads(witness)
            if (saved['identity'] != request['identity'] or saved['operation'] != request['operation']
                    or saved['source_authority_sha256'] != request['source_authority_sha256']
                    or saved['outcome'] != 'committed'):
                raise ValueError('Original witness differs')
            saved['mode'] = 'readback'
            return saved
        if not callable(getattr(parameters, 'set_str', None)):
            return measured_receipt(receipt, started)
        qualified = qualify_import(env, request, receipt)
    except Exception:
        if request['mode'] == 'readback':
            raise
        return measured_receipt(receipt, started)
    if qualified is None:
        return measured_receipt(receipt, started)
    receipt['elapsed_seconds'] = time.monotonic()-started
    if request['mode'] == 'dry-run':
        return measured_receipt(receipt, started)
    importer, raw = qualified
    try:
        env.cr.rollback()
        env.invalidate_all()
        current = {key:parameters.get_str(key) or '' for key in raw}
        if current != raw:
            raise ValueError('Import settings changed')
    except Exception:
        receipt.update(stage='configuration', outcome='unavailable', grants='unknown')
        return measured_receipt(receipt, started)
    receipt.update(stage='import', outcome='unknown')
    importer.run_full_import()
    receipt.update(stage='complete', outcome='committed', committed=True, elapsed_seconds=time.monotonic()-started)
    if receipt['elapsed_seconds'] >= request['timeout_seconds']:
        raise ValueError('Import deadline exceeded')
    parameters.set_str(witness_key, json.dumps(receipt, sort_keys=True, separators=(',', ':')))
    env.cr.commit()
    return measured_receipt(receipt, started)
"""

CONTAINER_IMPORT_PROGRAM = (
    PREVIEW_PROCESS_LIBRARY
    + r"""
import json, os, selectors, signal, subprocess, sys, tempfile, time
request=json.loads(sys.stdin.read(262145))
observed=json.loads(os.environ['LAUNCHPLANE_RUNTIME_IDENTITY_JSON'])
if ({k:v for k,v in observed.items() if k!='deployed_at'} !=
        {k:v for k,v in request['identity'].items() if k!='deployed_at'}):
    raise ValueError('Runtime changed')
scope={}
exec(request['odoo_program'],scope)
sys.path[:0]=['/odoo','/volumes/scripts']
child=None
runner=None
locked=False
started=time.monotonic()
try:
    from run_odoo_data_workflows import LocalServerSettings, OdooDataWorkflowRunner
    local=LocalServerSettings()
    if (local.host,local.platform_instance,local.db_name)!=('database','preview',request['database']):
        raise ValueError('Native preview differs')
    runner=OdooDataWorkflowRunner(local,upstream=None,env_file=None)
    runner.local.addons_path=','.join(str(path) for path in runner._resolve_addons_paths())
    if request['mode']=='apply':
        runner.acquire_data_workflow_lock()
        locked=True
    script = ('import contextlib,json,os\n'
        'with open(os.devnull,"w") as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):\n'
        '    scope={}\n'
        '    exec('+repr(request.pop('odoo_program'))+',scope)\n'
        '    result=scope["execute_import"](env,json.loads('+repr(json.dumps(request))+'))\n'
        'print("preview_parts_import="+json.dumps(result,separators=(",",":")).encode().hex())\n')
    with tempfile.TemporaryFile() as input_file:
        input_file.write(script.encode())
        input_file.seek(0)
        deadline=time.monotonic()+request['timeout_seconds']
        child=subprocess.Popen(runner._odoo_shell_command(),stdin=input_file,stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,env=runner.os_env,start_new_session=True)
    output=bounded_child_output(child, timeout_seconds=request['timeout_seconds'],
        deadline=deadline, maximum_bytes=4*request['maximum_result_bytes'])
    receipts=[line.partition(b'=')[2] for line in output.splitlines() if line.startswith(b'preview_parts_import=')]
    if len(receipts)!=1 or len(receipts[0])>2*request['maximum_result_bytes']:
        raise ValueError('Native receipt unavailable')
    print(bytes.fromhex(receipts[0].decode()).decode(),flush=True)
except Exception:
    if child is not None:
        raise
    print(json.dumps(scope['measured_receipt'](scope['new_receipt'](request),started)),flush=True)
finally:
    if child is not None:
        try:
            os.killpg(child.pid,signal.SIGKILL)
        except ProcessLookupError:
            pass
        child.wait()
        child.stdout.close()
    if locked:
        runner.release_data_workflow_lock()
"""
)

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
        or receipt['outcome']=='qualified' and (spec['mode']!='dry-run' or receipt['stage']!='qualified' or receipt['grants']!='read_only')
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
    output=bounded_child_output(child, timeout_seconds=spec['timeout_seconds']+30, maximum_bytes=spec["maximum_result_bytes"])
    receipt=json.loads(output)
    validate_receipt(receipt,spec)
    checked_container(spec,require_running=True)
    print(RESULT_MARKER+'='+json.dumps(receipt,separators=(',',':')).encode().hex(),flush=True)
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
        "import_grants_probe": grant_classifier_program(),
    }
