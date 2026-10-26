CANONICAL_ADDRESS_DATABASE_READ = r"""
import hashlib,json,os,sys
from urllib.parse import urlsplit
import psycopg2
spec=json.load(sys.stdin)
connection=psycopg2.connect(host='database',port='5432',dbname=os.environ['ODOO_DB_NAME'],
    user=os.environ['ODOO_DB_USER'],password=os.environ['ODOO_DB_PASSWORD'],connect_timeout=5,
    options='-c default_transaction_read_only=on -c statement_timeout=10000')
try:
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION READ ONLY')
        cursor.execute('SELECT id,domain FROM website ORDER BY id')
        websites=cursor.fetchall()
        if len(websites)!=1 or (spec.get('website_id') and websites[0][0]!=spec['website_id']):
            raise RuntimeError('Canonical website ownership is ambiguous')
        cursor.execute('SELECT key,value FROM ir_config_parameter WHERE key = ANY(%s)',
            (['web.base.url','web.base.url.freeze',spec['copy_witness_parameter']],))
        parameters=dict(cursor.fetchall())
        if spec['copy_binding_sha256'] and parameters.get(spec['copy_witness_parameter'])!=spec['copy_binding_sha256']:
            raise RuntimeError('The canonical settings copy witness changed')
        values={'website_id':websites[0][0],'domain':websites[0][1],
            'base_url':parameters.get('web.base.url'),'frozen':parameters.get('web.base.url.freeze')}
        canonical=spec['canonical_url']
        verified=(values['base_url']==canonical and values['frozen']=='True'
            and str(values['domain'] or '').rstrip('/') in {canonical.rstrip('/'),urlsplit(canonical).netloc})
        print(json.dumps({'website_id':websites[0][0],'canonical_settings_verified':verified,
            'database_values_sha256':hashlib.sha256(json.dumps(values,sort_keys=True,separators=(',',':')).encode()).hexdigest()}))
finally:
    connection.rollback()
    connection.close()
"""


CANONICAL_ADDRESS_HOST_LIBRARY = (
    "\nCANONICAL_ADDRESS_DATABASE_READ="
    + repr(CANONICAL_ADDRESS_DATABASE_READ)
    + "\n"
    + r"""
def canonical_address_settings(spec, containers):
    settings=spec['canonical_settings']
    mode=settings['mode']
    if mode not in {'inspect','gate','readback'} or mode=='gate' and spec.get('inspection_only'):
        raise RuntimeError('Unsupported canonical settings mode')
    for service in ('web','database','script-runner'):
        if containers[service][1].get('State',{}).get('Status')!='running':
            raise CopyReadbackRefused(ReadbackPredicate.RUNTIME)
    own={item[1]['Id'] for item in containers.values()}
    network=one_inspection(next(iter(containers['database'][1]['NetworkSettings']['Networks'])))
    members=network.get('Containers')
    if not isinstance(members,dict) or set(members)!=own:
        raise CopyReadbackRefused(ReadbackPredicate.NETWORK_MEMBERS)
    for service in ('web','script-runner'):
        environment=dict(value.split('=',1) for value in containers[service][1]['Config'].get('Env',[]) if '=' in value)
        identity=json.loads(environment.get('LAUNCHPLANE_RUNTIME_IDENTITY_JSON','null'))
        if not isinstance(identity,dict):
            raise CopyReadbackRefused(ReadbackPredicate.RUNTIME_IDENTITY)
        identity.pop('deployed_at',None)
        if identity!=settings['runtime_identity'] or environment.get('ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64')!=settings['payload']:
            raise CopyReadbackRefused(ReadbackPredicate.SETTINGS)
    runner=containers['script-runner'][0]
    if mode!='gate':
        credentials=docker('exec','-i',containers['web'][0],'python3','-c',
            base64.b64decode(spec['credential_program']).decode(),stdin=json.dumps(spec).encode()).decode().splitlines()
        if credentials!=['candidate_credentials_verified=true']:
            raise CopyReadbackRefused(ReadbackPredicate.CANDIDATE_READINESS)
    workflow_lock=docker('exec',runner,'python3','-c',
        'from pathlib import Path;import sys;print(Path(sys.argv[1]).exists())',spec['data_workflow_lock_path']).decode().strip()
    if workflow_lock!='False':
        raise CopyReadbackRefused(ReadbackPredicate.EXECUTION_LOCK)
    data=json.loads(docker('exec','-i',runner,'python3','-c',CANONICAL_ADDRESS_DATABASE_READ,
        stdin=json.dumps({**settings,'copy_binding_sha256':spec['copy_binding_sha256'],
            'copy_witness_parameter':settings['copy_witness_parameter']}).encode()))
    gate=docker('exec',runner,'python3','-c',COPY_WEB_GATE_READ_PROGRAM,
        spec['database_name'],spec['web_gate_path']).decode().strip()
    if gate not in {'True','False'}:
        raise RuntimeError('Canonical web gate observation is unavailable')
    data['canonical_gate_released']=gate=='True'
    if mode=='gate':
        if data['database_values_sha256']!=settings['database_values_sha256'] and data['canonical_settings_verified'] is not True:
            raise CopyReadbackRefused(ReadbackPredicate.SETTINGS)
        readback=docker('exec','-i',runner,'python3','-',spec['database_name'],spec['readback_spec'],'dry-run',
            stdin=spec['readback_program'].encode()).decode().splitlines()
        if 'integration_readback_ok=true' not in readback or 'integration_readback_ok=false' in readback:
            raise CopyReadbackRefused(ReadbackPredicate.INTEGRATIONS)
        docker('exec','-i',runner,'python3','-c',
            "import hashlib,os,sys;from pathlib import Path;p=Path(sys.argv[1]);"
            "assert p.is_absolute() and not p.is_symlink();"
            "p.write_text(hashlib.sha256((os.environ['ODOO_DB_NAME']+'\\n'+os.environ['ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64']).encode()).hexdigest())",
            spec['web_gate_path'])
        data['canonical_gate_released']=True
    print(sys.argv[2]+json.dumps({'type':'complete',**data}),flush=True)
"""
)
