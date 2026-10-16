"""Fixed testing-account program over the existing private, non-echo transport."""

from control_plane.dokploy.native_copy_stream import _read_remote
from control_plane.odoo_testing_restore import TESTING_COPY_COMPLETION_PARAMETER


def testing_account_command(
    *, host: str, token: str, server_id: str, payload: dict[str, object]
) -> dict[str, object]:
    frames: list[dict[str, object]] = []
    _read_remote(
        host=host,
        token=token,
        server_id=server_id,
        program=ACCOUNT_HOST_PROGRAM,
        payload={**payload, "account_program": ACCOUNT_ODOO_PROGRAM},
        receive=frames.append,
        timeout_seconds=120,
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise ValueError("Account operation has no completion witness.")
    if frames[0].get("status") != "pass" or type(frames[0].get("account_id")) is not int:
        raise ValueError("Testing account operation did not pass.")
    return {"account_id": frames[0]["account_id"], "status": "pass"}


# There are no user-selected executable names, paths, SQL, shell or Python.
# The caller's password travels only in the echo-disabled input and docker
# exec stdin. Neither Docker argv, a schedule, nor a provider log carries it.
ACCOUNT_HOST_PROGRAM = r'''
import base64, json, subprocess, sys
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
def docker(*args, data=None):
    result=subprocess.run(['docker',*args],input=data,capture_output=True,timeout=100)
    if result.returncode: raise RuntimeError('Testing account provider command failed')
    return result.stdout
try:
    if spec['instance']!='testing': raise RuntimeError('Not testing')
    project=spec['project']
    ids=docker('ps','--filter','label=com.docker.compose.project='+project,
        '--filter','label=com.docker.compose.service=script-runner','--format','{{.ID}}').decode().split()
    if len(ids)!=1: raise RuntimeError('Ambiguous testing runner')
    runner=json.loads(docker('inspect',ids[0]))[0]
    env=dict(value.split('=',1) for value in runner['Config']['Env'] if '=' in value)
    observed=json.loads(env.get(spec['identity_key'],'{}'))
    expected=spec['identity']
    observed.pop('deployed_at',None); expected.pop('deployed_at',None)
    if observed!=expected or runner['Config']['Image']!=spec['image']:
        raise RuntimeError('Testing runner identity changed')
    if expected.get('instance')!='testing' or env.get('PLATFORM_INSTANCE')!='testing' or env.get('ODOO_DB_HOST')!='database' or env.get('ODOO_DB_NAME')!=spec['database']:
        raise RuntimeError('Testing runner database changed')
    source=spec.pop('account_program')
    wrapper="""
import argparse, contextlib, io, json, logging, os, subprocess, sys, tempfile
logging.disable(logging.CRITICAL)
request=json.loads(sys.stdin.read())
program=request.pop('program')
sys.path.insert(0,'/volumes/scripts')
import run_odoo_startup as startup
os.environ['ODOO_DB_NAME']=request['database']
os.environ['ODOO_LOG_LEVEL']='critical'
with tempfile.TemporaryDirectory(prefix='launchplane-walkthrough-') as directory:
    output=os.path.join(directory,'result.json')
    request['result_file']=output
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        settings=startup._load_settings(argparse.Namespace(config_path=os.path.join(directory,'odoo.conf')))
        startup._write_runtime_config(settings)
    child=subprocess.run(startup._build_odoo_shell_command(settings),
        input=('request='+repr(request)+'\\n'+program).encode(),
        env=startup._odoo_environment(settings),capture_output=True,timeout=80)
    if child.returncode: raise RuntimeError('Testing account consumer failed')
    with open(output) as stream: result=json.load(stream)
    print(json.dumps(result))
"""
    result=json.loads(docker('exec','-i',ids[0],'python3','-c',wrapper,
        data=json.dumps({**spec,'program':source}).encode()))
    if set(result)!={'account_id','status'}: raise RuntimeError('Invalid account receipt')
    print(sys.argv[2]+json.dumps({'type':'complete',**result}),flush=True)
except Exception:
    print(sys.argv[2]+json.dumps({'type':'complete','status':'unknown'}),flush=True)
'''


ACCOUNT_ODOO_PROGRAM = r"""
import json, logging, os, secrets
logging.disable(logging.CRITICAL)
if request['instance']!='testing': raise RuntimeError('Not testing')
parameters=env['ir.config_parameter'].sudo()
if parameters.get_param('__LP_COPY_WITNESS__')!=request['copy_binding']:
    raise RuntimeError('Testing copy witness changed')
users=env['res.users'].sudo().with_context(active_test=False,tracking_disable=True,
    mail_create_nosubscribe=True,no_reset_password=True)
key='launchplane.walkthrough.'+request['task_id']
raw=parameters.get_param(key)
marker=json.loads(raw) if raw else None
login='lp_walk_'+request['task_id']
user=users.browse(marker['account_id']).exists() if marker else users.browse([])
if marker and (marker['binding']!=request['binding'] or len(user)!=1
               or user.id<=2 or user.login!=login):
    raise RuntimeError('Testing task account ownership changed')
if marker and marker['groups']!=sorted(request['groups']):
    raise RuntimeError('Testing task group request changed')
if not marker and users.search_count([('login','=',login)]):
    raise RuntimeError('Existing login cannot be adopted')
env.cr.execute("SELECT EXISTS (SELECT 1 FROM res_users_log WHERE create_date >= %s::timestamptz AT TIME ZONE 'UTC' AND create_uid != %s)",(request['usage_since'],user.id if user else -1,))
if request['operation']!='cleanup' and env.cr.fetchone()[0]:
    raise RuntimeError('Testing lane has other recent successful sign-ins')
if request['operation']=='provision':
    if marker:
        if marker['state']!='active' or not user.active: raise RuntimeError('Task already closed')
    else:
        groups=[env.ref(name) for name in request['groups']]
        if any(group._name!='res.groups' for group in groups): raise RuntimeError('Invalid business groups')
        field='group_ids' if 'group_ids' in users._fields else 'groups_id'
        user=users.create({'name':'TEST Launchplane walkthrough','login':login,
            'password':request['password'],'active':True,field:[(6,0,[group.id for group in groups])]})
        if user.has_group('base.group_system') or user.has_group('base.group_erp_manager'):
            raise RuntimeError('Testing task may not administer Odoo')
        marker={'account_id':user.id,'binding':request['binding'],'state':'active','groups':sorted(request['groups'])}
        parameters.set_param(key,json.dumps(marker,sort_keys=True))
elif request['operation']=='cleanup':
    if marker and marker['state']=='active':
        user.write({'active':False,'password':secrets.token_urlsafe(48)})
        if 'res.users.apikeys' in env.registry:
            env['res.users.apikeys'].sudo().search([('user_id','=',user.id)]).unlink()
        marker['state']='closed'
        parameters.set_param(key,json.dumps(marker,sort_keys=True))
    if marker and user.active: raise RuntimeError('Account cleanup readback failed')
else:
    if not marker or marker['state']!='active' or not user.active:
        raise RuntimeError('Task is not active')
    if user.has_group('base.group_system') or user.has_group('base.group_erp_manager'):
        raise RuntimeError('Task privileges changed')
env.cr.commit()
with os.fdopen(os.open(request['result_file'],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
    json.dump({'status':'pass','account_id':user.id if marker else 0},stream)
""".replace("__LP_COPY_WITNESS__", TESTING_COPY_COMPLETION_PARAMETER)
