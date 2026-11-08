from control_plane.contracts.odoo_walkthrough import WALKTHROUGH_FORBIDDEN_GROUPS
from typing import cast
from control_plane.dokploy.native_copy_stream import _read_remote
from control_plane.dokploy.odoo_shell import ODOO_RUNNER_PROGRAM, ODOO_SHELL_WRAPPER


CLIENT_USER_MANIFEST_KEY = "launchplane_client_user_xmlids"


def client_signin_command(
    *, host: str, token: str, server_id: str, payload: dict[str, object]
) -> dict[str, object]:
    frames: list[dict[str, object]] = []
    _read_remote(
        host=host,
        token=token,
        server_id=server_id,
        program=CLIENT_SIGNIN_HOST_PROGRAM,
        payload={
            **payload,
            "program": CLIENT_SIGNIN_ODOO_PROGRAM,
            "probe": CLIENT_SIGNIN_LIST_PROGRAM,
            "forbidden_groups": WALKTHROUGH_FORBIDDEN_GROUPS,
        },
        receive=frames.append,
        timeout_seconds=120,
    )
    if (
        len(frames) != 1
        or frames[0].get("type") != "complete"
        or not isinstance(frames[0].get("result"), dict)
    ):
        raise ValueError("Native Client sign-in is unverified.")
    return cast(dict[str, object], frames[0]["result"])


CLIENT_SIGNIN_HOST_PROGRAM = (
    ODOO_RUNNER_PROGRAM
    + r"""
import base64, json, subprocess, sys
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
def docker(*args,data=None):
    result=subprocess.run(['docker',*args],input=data,capture_output=True,timeout=100)
    if result.returncode: raise RuntimeError('Native Client sign-in command failed')
    return result.stdout
try:
    identity=spec['identity']
    if identity['instance'] in ('prod','production') or (identity['instance']!='testing' and not (identity['environment_kind']=='preview' and identity.get('preview_id') and identity.get('preview_generation_id'))):
        raise RuntimeError('Unsupported Client sign-in lane')
    runner=qualified_script_runner(spec,docker)
    if spec['operation']=='list':
        result=json.loads(docker('exec','-i',runner,'python3','-c',spec['probe'],data=json.dumps(spec).encode()))
    elif spec['operation']=='mint':
        failure="env.cr.rollback()\nraise RuntimeError('Native Client sign-in failed')"
        result=json.loads(docker('exec','-i',runner,'python3','-c',__ODOO_SHELL_WRAPPER__,
            data=json.dumps({**spec,'failure_program':failure,'fallback_result':{'status':'unverified'}}).encode()))
    else:
        raise RuntimeError('Unsupported Client sign-in operation')
    print(sys.argv[2]+json.dumps({'type':'complete','result':result}),flush=True)
except Exception:
    print(sys.argv[2]+json.dumps({'type':'complete','status':'unverified'}),flush=True)
""".replace("__ODOO_SHELL_WRAPPER__", repr(ODOO_SHELL_WRAPPER))
)


CLIENT_USER_DECLARATION_PROGRAM = r"""
import re
from pathlib import Path
import odoo.addons
from odoo.modules.module import Manifest
def declared_client_user_xmlids(installed):
    declared=[]
    for module in installed:
        paths={str((Path(root)/module/'__manifest__.py').resolve()) for root in odoo.addons.__path__ if (Path(root)/module/'__manifest__.py').is_file()}
        if len(paths)!=1:
            raise RuntimeError('Installed addon manifest is missing or ambiguous')
        manifest=Manifest.for_addon(module,display_warning=False)
        if manifest is None:
            raise RuntimeError('Installed addon manifest is unavailable')
        values=manifest.get(__MANIFEST_KEY__,[])
        if not isinstance(values,list):
            raise RuntimeError('Client user declaration must be a list')
        for xmlid in values:
            if not isinstance(xmlid,str) or not re.fullmatch(r'[A-Za-z0-9_]+\.[A-Za-z0-9_]+',xmlid) or xmlid.split('.',1)[0]!=module or xmlid in declared:
                raise RuntimeError('Client user declaration is unqualified')
            declared.append(xmlid)
    return declared
""".replace("__MANIFEST_KEY__", repr(CLIENT_USER_MANIFEST_KEY))


CLIENT_SIGNIN_USERS_PROGRAM = (
    CLIENT_USER_DECLARATION_PROGRAM
    + r'''
import json, os, sys, psycopg2
spec=json.loads(sys.stdin.read())
connection=psycopg2.connect(host=os.environ['ODOO_DB_HOST'],port=os.environ.get('ODOO_DB_PORT','5432'),
    dbname=spec['database'],user=os.environ['ODOO_DB_USER'],password=os.environ['ODOO_DB_PASSWORD'],connect_timeout=5)
connection.set_session(readonly=True,autocommit=False)
def read_users(cursor):
    cursor.execute("SELECT name FROM ir_module_module WHERE state='installed' ORDER BY name")
    declared=declared_client_user_xmlids([row[0] for row in cursor.fetchall()])
    if not declared:
        return []
    cursor.execute("""SELECT marker.module || '.' || marker.name
FROM ir_model_data marker JOIN res_users account ON marker.model='res.users' AND account.id=marker.res_id
WHERE marker.module || '.' || marker.name=ANY(%s)""",(declared,))
    if {row[0] for row in cursor.fetchall()}!=set(declared):
        raise RuntimeError('Declared native Client user is unresolved')
    cursor.execute("""
WITH RECURSIVE effective_groups(uid,gid) AS (
    SELECT uid,gid FROM res_groups_users_rel
    UNION
    SELECT members.uid,implied.hid FROM effective_groups members
    JOIN res_groups_implied_rel implied ON implied.gid=members.gid
)
SELECT marker.module || '.' || marker.name, partner.name, account.login
FROM ir_model_data marker JOIN res_users account ON account.id=marker.res_id
JOIN res_partner partner ON partner.id=account.partner_id
WHERE marker.model='res.users' AND marker.module || '.' || marker.name=ANY(%s)
AND account.id>2 AND account.active AND COALESCE(account.password,'')=''
AND partner.name IS NOT NULL AND partner.name<>''
AND NOT EXISTS (SELECT 1 FROM res_users sibling WHERE sibling.partner_id=account.partner_id AND sibling.id<>account.id)
AND NOT EXISTS (SELECT 1 FROM res_users_log history WHERE history.create_uid=account.id)
AND NOT EXISTS (
    SELECT 1 FROM effective_groups membership JOIN ir_model_data role
    ON role.model='res.groups' AND role.res_id=membership.gid
    WHERE membership.uid=account.id AND role.module || '.' || role.name=ANY(%s)
)
ORDER BY marker.module,marker.name
""",(declared,spec['forbidden_groups']))
    return [{'xmlid':xmlid,'name':name,'login':login} for xmlid,name,login in cursor.fetchall()]
try:
    with connection.cursor() as cursor:
        cursor.execute("SET LOCAL statement_timeout='5s'")
        users=read_users(cursor)
    print(json.dumps({'users':users}))
finally:
    connection.rollback()
    connection.close()
'''
)


CLIENT_SIGNIN_LIST_PROGRAM = (
    r"""
import argparse, contextlib, io, json, logging, os, sys, tempfile
logging.disable(logging.CRITICAL)
sys.path.insert(0,'/volumes/scripts')
import run_odoo_startup as startup
from odoo.tools import config
with tempfile.TemporaryDirectory(prefix='launchplane-client-signin-') as directory:
    with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
        settings=startup._load_settings(argparse.Namespace(config_path=os.path.join(directory,'odoo.conf')))
        startup._write_runtime_config(settings)
        os.environ.update(startup._odoo_environment(settings))
        config.parse_config(['-c',settings.config_path],setup_logging=False)
import psycopg2
"""
    + CLIENT_SIGNIN_USERS_PROGRAM
)


CLIENT_SIGNIN_ODOO_PROGRAM = (
    CLIENT_USER_DECLARATION_PROGRAM
    + r"""
import json, logging, os
logging.disable(logging.CRITICAL)
xmlid=request['xmlid']
installed=env['ir.module.module'].sudo().search([('state','=','installed')]).mapped('name')
declared=declared_client_user_xmlids(installed)
if xmlid not in declared:
    raise RuntimeError('Undeclared Client user')
for candidate in declared:
    record=env.ref(candidate,raise_if_not_found=False)
    if not record or record._name!='res.users':
        raise RuntimeError('Declared native Client user is unresolved')
user=env.ref(xmlid,raise_if_not_found=False)
if not user or user._name!='res.users' or user.id<=2:
    raise RuntimeError('Unavailable Client user')
env.cr.execute("SELECT COALESCE(password,'') <> '' FROM res_users WHERE id=%s FOR UPDATE",(user.id,))
if env.cr.fetchone()[0]:
    raise RuntimeError('Client password is already set')
user=user.with_context(active_test=False)
user.invalidate_recordset()
if not user.active or not user.name or user.login_date or any(user.has_group(group) for group in request['forbidden_groups'] if env.ref(group,raise_if_not_found=False)):
    raise RuntimeError('Client user is ineligible')
partner=user.partner_id.sudo().with_context(active_test=False)
if len(partner.user_ids)!=1:
    raise RuntimeError('Ambiguous Client partner')
partner.signup_prepare(signup_type='signup')
token=partner._generate_signup_token()
commit_started=True
env.cr.commit()
with os.fdopen(os.open(request['result_file'],os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600),'w') as stream:
    json.dump({'xmlid':xmlid,'name':user.name,'token':token},stream)
"""
)
