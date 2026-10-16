"""Fixed testing-account program over the existing private, non-echo transport."""

from control_plane.dokploy.native_copy_stream import _read_remote
from control_plane.odoo_testing_restore import TESTING_COPY_COMPLETION_PARAMETER
from control_plane.contracts.odoo_walkthrough import (
    WALKTHROUGH_LOGIN_PREFIX,
    WALKTHROUGH_MARKER_PREFIX,
    WALKTHROUGH_LOCK_PREFIX,
    WALKTHROUGH_ACTIVE_STATE,
)

WALKTHROUGH_JOURNAL_DIRECTORY = "/tmp/launchplane-walkthrough"


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
    if (
        frames[0].get("status") not in {"pass", "fail"}
        or type(frames[0].get("account_id")) is not int
    ):
        raise ValueError("Testing account operation did not pass.")
    return {"account_id": frames[0]["account_id"], "status": frames[0]["status"]}


def testing_account_dispatch_finished(
    *, host: str, token: str, server_id: str, dispatch_id: str, original_intent: dict[str, object]
) -> bool:
    frames: list[dict[str, object]] = []
    _read_remote(
        host=host,
        token=token,
        server_id=server_id,
        program=ACCOUNT_JOURNAL_READ_PROGRAM,
        payload={
            **original_intent,
            "dispatch_id": dispatch_id,
            "mode": "quiescence",
            "probe": ACCOUNT_DATABASE_PROBE,
        },
        receive=frames.append,
        timeout_seconds=30,
    )
    return len(frames) == 1 and frames[0].get("clear") is True


def testing_task_unused(
    *, host: str, token: str, server_id: str, intent: dict[str, object]
) -> bool:
    frames: list[dict[str, object]] = []
    _read_remote(
        host=host,
        token=token,
        server_id=server_id,
        program=ACCOUNT_JOURNAL_READ_PROGRAM,
        payload={**intent, "mode": "usage", "probe": ACCOUNT_DATABASE_PROBE},
        receive=frames.append,
        timeout_seconds=30,
    )
    return len(frames) == 1 and frames[0].get("clear") is True


ACCOUNT_DATABASE_PROBE = (
    r"""
import json,os,sys,psycopg2
spec=json.loads(sys.stdin.read())
connection=psycopg2.connect(host=os.environ['ODOO_DB_HOST'],port=os.environ.get('ODOO_DB_PORT','5432'),
    dbname=os.environ['ODOO_DB_NAME'],user=os.environ['ODOO_DB_USER'],password=os.environ['ODOO_DB_PASSWORD'],connect_timeout=5)
try:
    connection.set_session(readonly=True,autocommit=False)
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout='5s'")
        cursor.execute('SELECT value FROM ir_config_parameter WHERE key=%s',('__LP_COPY_WITNESS__',))
        row=cursor.fetchone()
        if not row or row[0]!=spec['copy_binding']: raise RuntimeError('Testing copy changed')
        if spec['mode']=='quiescence':
            cursor.execute('SELECT pg_try_advisory_xact_lock(hashtextextended(%s,0))',(__TASK_LOCK_PREFIX__+spec['binding'],))
            clear=cursor.fetchone()[0] is True
        elif spec['mode']=='usage':
            cursor.execute('SELECT value FROM ir_config_parameter WHERE key=%s',(__TASK_MARKER_PREFIX__+spec['task_id'],))
            row=cursor.fetchone()
            marker=json.loads(row[0]) if row else {}
            uid=marker.get('account_id')
            if marker.get('binding')!=spec['binding'] or marker.get('state')!=__TASK_ACTIVE_STATE__ or type(uid)is not int or uid<=2:
                raise RuntimeError('Task account changed')
            cursor.execute('SELECT login,active FROM res_users WHERE id=%s',(uid,))
            if cursor.fetchone()!=(__TASK_LOGIN_PREFIX__+spec['task_id'],True): raise RuntimeError('Task account changed')
            cursor.execute("SELECT EXISTS (SELECT 1 FROM res_users_log WHERE create_date >= %s::timestamptz AT TIME ZONE 'UTC' AND create_uid != %s)",(spec['usage_since'],uid))
            clear=cursor.fetchone()[0] is False
        else: raise RuntimeError('Unsupported read')
    connection.rollback()
    print(json.dumps({'clear':clear}))
finally:
    connection.close()
""".replace("__LP_COPY_WITNESS__", TESTING_COPY_COMPLETION_PARAMETER)
    .replace("__TASK_LOCK_PREFIX__", repr(WALKTHROUGH_LOCK_PREFIX))
    .replace("__TASK_MARKER_PREFIX__", repr(WALKTHROUGH_MARKER_PREFIX))
    .replace("__TASK_LOGIN_PREFIX__", repr(WALKTHROUGH_LOGIN_PREFIX))
    .replace("__TASK_ACTIVE_STATE__", repr(WALKTHROUGH_ACTIVE_STATE))
)


ACCOUNT_JOURNAL_READ_PROGRAM = r"""
import base64,json,re,sys,os,stat,time,subprocess
from pathlib import Path
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
clear=False
try:
    if spec.get('mode')=='quiescence':
        if not re.fullmatch('[a-f0-9]{64}',spec['dispatch_id']): raise RuntimeError('Invalid dispatch')
        path=Path(__JOURNAL_DIRECTORY__)/(spec['dispatch_id']+'.json')
        try:
            directory=path.parent
            mode=directory.lstat()
            if directory.is_symlink() or mode.st_uid!=os.getuid() or stat.S_IMODE(mode.st_mode)&0o077 or path.is_symlink():
                raise ValueError('Untrusted journal')
            value=json.loads(path.read_text())
            clear=value.get('finished') is True and value.get('needs_quiescence') is not True
        except (OSError,ValueError):
            pass
    if not clear and (spec.get('mode')=='usage' or ('dispatch_deadline' in spec and time.time()>spec['dispatch_deadline'])):
        def docker(*args,data=None):
            result=subprocess.run(['docker',*args],input=data,capture_output=True,timeout=15)
            if result.returncode: raise RuntimeError('Testing read unavailable')
            return result.stdout
        if spec['identity']['instance']!='testing': raise RuntimeError('Not testing')
        ids=docker('ps','--filter','label=com.docker.compose.project='+spec['project'],
            '--filter','label=com.docker.compose.service=script-runner','--format','{{.ID}}').decode().split()
        if len(ids)!=1: raise RuntimeError('Ambiguous testing runner')
        runner=json.loads(docker('inspect',ids[0]))[0]
        environment=dict(value.split('=',1) for value in runner['Config']['Env'] if '=' in value)
        observed=json.loads(environment.get(spec['identity_key'],'{}')); expected=spec['identity']
        observed.pop('deployed_at',None); expected.pop('deployed_at',None)
        if observed!=expected or runner['Config']['Image']!=spec['image'] or environment.get('PLATFORM_INSTANCE')!='testing' or environment.get('ODOO_DB_HOST')!='database' or environment.get('ODOO_DB_NAME')!=spec['database']:
            raise RuntimeError('Testing receiver changed')
        response=json.loads(docker('exec','-i',ids[0],'python3','-c',spec['probe'],data=json.dumps(spec).encode()))
        clear=response.get('clear') is True
except Exception:
    clear=False
print(sys.argv[2]+json.dumps({'type':'complete','clear':clear}),flush=True)
""".replace("__JOURNAL_DIRECTORY__", repr(WALKTHROUGH_JOURNAL_DIRECTORY))


# There are no user-selected executable names, paths, SQL, shell or Python.
# The caller's password travels only in the echo-disabled input and docker
# exec stdin. Neither Docker argv, a schedule, nor a provider log carries it.
ACCOUNT_HOST_PROGRAM = r'''
import base64, json, subprocess, sys, os, re, stat, time
from pathlib import Path
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
journal=None; journal_owned=False; dispatched=False
def finish(needs_quiescence):
    temporary=journal.with_suffix('.complete')
    with os.fdopen(os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
        json.dump({'finished':True,'needs_quiescence':needs_quiescence},stream);stream.flush();os.fsync(stream.fileno())
    os.replace(temporary,journal)
def docker(*args, data=None):
    result=subprocess.run(['docker',*args],input=data,capture_output=True,timeout=100)
    if result.returncode: raise RuntimeError('Testing account provider command failed')
    return result.stdout
try:
    if spec['instance']!='testing': raise RuntimeError('Not testing')
    if not re.fullmatch('[a-f0-9]{64}',spec['dispatch_id']): raise RuntimeError('Invalid dispatch')
    directory=Path(__JOURNAL_DIRECTORY__)
    directory.mkdir(mode=0o700,exist_ok=True)
    mode=directory.lstat()
    if directory.is_symlink() or mode.st_uid!=os.getuid() or stat.S_IMODE(mode.st_mode)&0o077:
        raise RuntimeError('Untrusted journal directory')
    journal=directory/(spec['dispatch_id']+'.json')
    with os.fdopen(os.open(journal,os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
        json.dump({'finished':False},stream);stream.flush();os.fsync(stream.fileno())
    journal_owned=True
    if time.time()>spec['dispatch_deadline']: raise RuntimeError('Dispatch deadline expired')
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
import argparse, contextlib, io, json, logging, os, subprocess, sys, tempfile, textwrap
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
    failure="env.cr.rollback()\\nimport json,os\\nwith os.fdopen(os.open(request['result_file'],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:\\n    json.dump({'account_id':0,'status':'fail'},stream)"
    script='request='+repr(request)+'\\ntry:\\n'+textwrap.indent(program,'    ')+'\\nexcept Exception:\\n'+textwrap.indent(failure,'    ')
    try:
        child=subprocess.run(startup._build_odoo_shell_command(settings),
            input=script.encode(),
            env=startup._odoo_environment(settings),capture_output=True,timeout=80)
        if child.returncode: raise RuntimeError('Testing account consumer failed')
        with open(output) as stream: result=json.load(stream)
    except Exception:
        result={'account_id':0,'status':'unknown'}
    print(json.dumps(result))
"""
    dispatched=True
    result=json.loads(docker('exec','-i',ids[0],'python3','-c',wrapper,
        data=json.dumps({**spec,'program':source}).encode()))
    if set(result)!={'account_id','status'}: raise RuntimeError('Invalid account receipt')
    finish(result['status']=='unknown')
    print(sys.argv[2]+json.dumps({'type':'complete',**result}),flush=True)
except Exception:
    if journal_owned and not dispatched:
        finish(False)
    print(sys.argv[2]+json.dumps({'type':'complete','status':'unknown'}),flush=True)
'''.replace("__JOURNAL_DIRECTORY__", repr(WALKTHROUGH_JOURNAL_DIRECTORY))


ACCOUNT_ODOO_PROGRAM = (
    r"""
import json, logging, os, secrets, time
logging.disable(logging.CRITICAL)
if request['instance']!='testing': raise RuntimeError('Not testing')
env.cr.execute('SELECT pg_advisory_xact_lock(hashtextextended(%s,0))',(__TASK_LOCK_PREFIX__+request['binding'],))
if time.time()>request['dispatch_deadline']: raise RuntimeError('Dispatch deadline expired')
parameters=env['ir.config_parameter'].sudo()
if parameters.get_param('__LP_COPY_WITNESS__')!=request['copy_binding']:
    raise RuntimeError('Testing copy witness changed')
users=env['res.users'].sudo().with_context(active_test=False,tracking_disable=True,
    mail_create_nosubscribe=True,no_reset_password=True)
key=__TASK_MARKER_PREFIX__+request['task_id']
raw=parameters.get_param(key)
marker=json.loads(raw) if raw else None
login=__TASK_LOGIN_PREFIX__+request['task_id']
user=users.browse(marker['account_id']).exists() if marker else users.browse([])
if marker and (marker['binding']!=request['binding'] or len(user)!=1
               or user.id<=2 or user.login!=login):
    raise RuntimeError('Testing task account ownership changed')
if marker and request['operation']=='provision' and marker['groups']!=sorted(request['groups']):
    raise RuntimeError('Testing task group request changed')
if not marker and users.search_count([('login','=',login)]):
    raise RuntimeError('Existing login cannot be adopted')
if request['operation']!='cleanup':
    env.cr.execute("SELECT EXISTS (SELECT 1 FROM res_users_log WHERE create_date >= %s::timestamptz AT TIME ZONE 'UTC' AND create_uid != %s)",(request['usage_since'],user.id if user else -1,))
    if env.cr.fetchone()[0]:
        raise RuntimeError('Testing lane has other recent successful sign-ins')
if request['operation']=='provision':
    if marker:
        if marker['state']!=__TASK_ACTIVE_STATE__ or not user.active: raise RuntimeError('Task already closed')
    else:
        groups=[env.ref(name) for name in request['groups']]
        if any(group._name!='res.groups' for group in groups): raise RuntimeError('Invalid business groups')
        field='group_ids' if 'group_ids' in users._fields else 'groups_id'
        user=users.create({'name':'TEST Launchplane walkthrough','login':login,
            'password':request['password'],'active':True,field:[(6,0,[group.id for group in groups])]})
        if user.has_group('base.group_system') or user.has_group('base.group_erp_manager'):
            raise RuntimeError('Testing task may not administer Odoo')
        marker={'account_id':user.id,'binding':request['binding'],'state':__TASK_ACTIVE_STATE__,'groups':sorted(request['groups'])}
        parameters.set_param(key,json.dumps(marker,sort_keys=True))
elif request['operation']=='cleanup':
    if marker and marker['state']==__TASK_ACTIVE_STATE__:
        user.write({'active':False,'password':secrets.token_urlsafe(48)})
        if 'res.users.apikeys' in env.registry:
            env['res.users.apikeys'].sudo().search([('user_id','=',user.id)]).unlink()
        marker['state']='closed'
        parameters.set_param(key,json.dumps(marker,sort_keys=True))
else:
    if not marker or marker['state']!=__TASK_ACTIVE_STATE__ or not user.active:
        raise RuntimeError('Task is not active')
    if user.has_group('base.group_system') or user.has_group('base.group_erp_manager'):
        raise RuntimeError('Task privileges changed')
env.cr.commit()
if request['operation']=='cleanup' and marker:
    user.invalidate_recordset(['active'])
    if user.active: raise RuntimeError('Account cleanup readback failed')
with os.fdopen(os.open(request['result_file'],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
    json.dump({'status':'pass','account_id':user.id if marker else 0},stream)
""".replace("__LP_COPY_WITNESS__", TESTING_COPY_COMPLETION_PARAMETER)
    .replace("__TASK_LOGIN_PREFIX__", repr(WALKTHROUGH_LOGIN_PREFIX))
    .replace("__TASK_MARKER_PREFIX__", repr(WALKTHROUGH_MARKER_PREFIX))
    .replace("__TASK_LOCK_PREFIX__", repr(WALKTHROUGH_LOCK_PREFIX))
    .replace("__TASK_ACTIVE_STATE__", repr(WALKTHROUGH_ACTIVE_STATE))
)
