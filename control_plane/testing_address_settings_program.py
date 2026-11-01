CANONICAL_ADDRESS_DATABASE_READ = r"""
import hashlib,json,os,sys
from urllib.parse import urlsplit
import psycopg2
spec=json.load(sys.stdin)
def require_website(condition):
    if not condition:
        raise RuntimeError('Canonical website ownership is ambiguous')
def website_host(value):
    value=str(value or '')
    host=urlsplit(value if '://' in value else 'https://'+value).hostname or ''
    return host.encode('idna').decode('ascii').lower()
def website_order(row):
    return (row[2] is None,row[2] or 0,row[0])
connection=psycopg2.connect(host='database',port='5432',dbname=os.environ['ODOO_DB_NAME'],
    user=os.environ['ODOO_DB_USER'],password=os.environ['ODOO_DB_PASSWORD'],connect_timeout=5,
    options='-c default_transaction_read_only=on -c statement_timeout=10000')
try:
    with connection.cursor() as cursor:
        cursor.execute('SET TRANSACTION READ ONLY')
        cursor.execute('SELECT id,domain,sequence FROM website ORDER BY id')
        websites=cursor.fetchall()
        require_website(bool(websites))
        website=websites[0]
        alias_fallback_required=False
        if len(websites)>1:
            require_website(not spec.get('primary_page_xmlid_set'))
            before=str(spec.get('before_url') or '')
            after=spec['canonical_url']
            after_domains={after.rstrip('/'),urlsplit(after).netloc}-{''}
            before_host,after_host=website_host(before),website_host(after)
            require_website(bool(before_host and after_host))
            alias_fallback_required=before_host!=after_host
            owners=[row for row in websites if website_host(row[1]) in {before_host,after_host}-{''}]
            require_website(len(owners)==1)
            website=owners[0]
            require_website(spec.get('website_id') or website_host(website[1])==before_host)
            candidates=after_domains|{after}
            writer=next((row for row in websites if row[1] in candidates),None)
            if writer is None:
                cursor.execute("SELECT module,model,res_id FROM ir_model_data "
                    "WHERE name='default_website' AND module IN ('base','website')")
                references=cursor.fetchall()
                defaults={module:identity for module,model,identity in references if model=='website'}
                require_website(len(defaults)==len(references))
                selected=defaults.get('base',defaults.get('website',websites[0][0]))
                writer=next((row for row in websites if row[0]==selected),None)
            require_website(writer is not None and writer[0]==website[0])
            if alias_fallback_required:
                projected=[(row[0],row[1],0 if row[0]==website[0] else row[2]) for row in websites]
                require_website(min(projected,key=website_order)[0]==website[0])
        require_website(not spec.get('website_id') or website[0]==spec['website_id'])
        cursor.execute('SELECT key,value FROM ir_config_parameter WHERE key = ANY(%s)',
            (['web.base.url','web.base.url.freeze',spec['copy_witness_parameter']],))
        parameters=dict(cursor.fetchall())
        if spec['copy_binding_sha256'] and parameters.get(spec['copy_witness_parameter'])!=spec['copy_binding_sha256']:
            raise RuntimeError('The canonical settings copy witness changed')
        values={'website_id':website[0],'domain':website[1],
            'base_url':parameters.get('web.base.url'),'frozen':parameters.get('web.base.url.freeze')}
        canonical=spec['canonical_url']
        verified=(values['base_url']==canonical and values['frozen']=='True'
            and str(values['domain'] or '').rstrip('/') in {canonical.rstrip('/'),urlsplit(canonical).netloc}
            and (not alias_fallback_required or min(websites,key=website_order)[0]==website[0]))
        print(json.dumps({'website_id':website[0],'canonical_settings_verified':verified,
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
    if mode not in {'inspect','gate','readback','observe'} or mode=='gate' and spec.get('inspection_only'):
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
    if mode in {'inspect','readback'}:
        credentials=docker('exec','-i',containers['web'][0],'python3','-c',
            base64.b64decode(spec['credential_program']).decode(),stdin=json.dumps(spec).encode(),diagnose_failure=True).decode().splitlines()
        if credentials!=['candidate_credentials_verified=true']:
            raise CopyReadbackRefused(ReadbackPredicate.CANDIDATE_READINESS)
    workflow_lock=docker('exec',runner,'python3','-c',
        'from pathlib import Path;import sys;print(Path(sys.argv[1]).exists())',spec['data_workflow_lock_path'],diagnose_failure=True).decode().strip()
    if workflow_lock!='False':
        raise CopyReadbackRefused(ReadbackPredicate.EXECUTION_LOCK)
    data=json.loads(docker('exec','-i',runner,'python3','-c',CANONICAL_ADDRESS_DATABASE_READ,
        stdin=json.dumps({**settings,'copy_binding_sha256':spec['copy_binding_sha256'],
            'copy_witness_parameter':settings['copy_witness_parameter']}).encode(),diagnose_failure=True))
    gate=docker('exec',runner,'python3','-c',COPY_WEB_GATE_READ_PROGRAM,
        spec['database_name'],spec['web_gate_path'],diagnose_failure=True).decode().strip()
    if gate not in {'True','False'}:
        raise RuntimeError('Canonical web gate observation is unavailable')
    data['canonical_gate_released']=gate=='True'
    if mode in {'inspect','gate'}:
        readback=docker('exec','-i',runner,'python3','-',spec['database_name'],spec['readback_spec'],'dry-run',
            stdin=spec['readback_program'].encode(),diagnose_failure=True).decode().splitlines()
        if 'integration_readback_ok=true' not in readback or 'integration_readback_ok=false' in readback:
            raise CopyReadbackRefused(ReadbackPredicate.INTEGRATIONS)
    if mode=='gate':
        if data['database_values_sha256']!=settings['database_values_sha256'] and data['canonical_settings_verified'] is not True:
            raise CopyReadbackRefused(ReadbackPredicate.SETTINGS)
        docker('exec','-i',runner,'python3','-c',
            "import hashlib,os,sys,tempfile;from pathlib import Path;p=Path(sys.argv[1]);"
            "assert p.is_absolute() and not p.is_symlink()\n"
            "with tempfile.TemporaryDirectory(dir=p.parent,prefix=p.name+'.') as directory:\n"
            " marker=Path(directory)/'marker'\n"
            " marker.write_text(hashlib.sha256((os.environ['ODOO_DB_NAME']+'\\n'+os.environ['ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64']).encode()).hexdigest())\n"
            " marker.chmod(0o644)\n"
            " os.replace(marker,p)\n",
            spec['web_gate_path'],diagnose_failure=True)
        data['canonical_gate_released']=True
    print(sys.argv[2]+json.dumps({'type':'complete',**data}),flush=True)
"""
)
