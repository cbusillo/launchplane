"""Fixed bounded programs for the prerequisite read; neither connects to a source DB."""

# Both ends of the provider terminal validate this closed value-free language.
_VALIDATOR = r"""
def validate_result(value, names):
    agreements={'match','mismatch','unknown'}
    origins={'explicit','empty','absent','unknown'}
    tls={'enabled_with_verification','disabled','verification_disabled','unknown'}
    enums={'producer':{'verified','unknown'}, 'addon_state':{'installed','unknown'}, 'binding':{'selected','missing_or_unsafe'},
           'secret_reference':agreements, 'selected_version_metadata':{'current','unknown'},
           'managed_password_value_agreement':{'unknown'}, 'applied_delivered_password':agreements,
           'applied_settings':{'valid','unknown'}, 'delivered_settings':{'valid','unknown'},
           'authority_tls_configuration':tls, 'delivered_tls_configuration':tls,
           'applied_tls_configuration':tls}
    if (not isinstance(value,dict) or set(value)-{'runtime','sources','applied','values_redacted'}
        or value.get('runtime') not in {'verified','unknown'}
        or value.get('applied',False) is not False or value.get('values_redacted',True) is not True
        or not isinstance(value.get('sources'),list) or len(value['sources'])!=len(names)):
        raise ValueError()
    found=[]
    for source in value['sources']:
        if not isinstance(source,dict) or set(source)-({'source','options'}|set(enums)):
            raise ValueError()
        if source.get('source') not in names:
            raise ValueError()
        found.append(source['source'])
        for key,allowed in enums.items():
            if key in source and (not isinstance(source[key],str) or source[key] not in allowed):
                raise ValueError()
        options=source.get('options',[])
        if not isinstance(options,list) or len(options)>4:
            raise ValueError()
        fields=[]
        for option in options:
            if (not isinstance(option,dict)
                or set(option)!={'field','authority','delivered','applied','authority_delivered','authority_applied','delivered_applied'}
                or option['field'] not in {'port','use_ssl','ssl_verify','batch_size'}):
                raise ValueError()
            fields.append(option['field'])
            for key in ('authority','delivered','applied'):
                if option[key] not in origins:
                    raise ValueError()
            for key in ('authority_delivered','authority_applied','delivered_applied'):
                if option[key] not in agreements:
                    raise ValueError()
        if len(fields)!=len(set(fields)):
            raise ValueError()
    if set(found)!=set(names) or len(found)!=len(set(found)):
        raise ValueError()
"""

CONTAINER_PROGRAM = (
    _VALIDATOR
    + r"""
import configparser, contextlib, hashlib, hmac, itertools, json, os, re, sys, types
from pathlib import Path
import psycopg2
spec=json.loads(sys.stdin.read(262145))
names=tuple(spec['addons'])
unknown={'runtime':'unknown','sources':[{'source':name} for name in names]}

def safe_path(raw):
    path=Path(raw)
    if not path.is_absolute() or '..' in path.parts or path.resolve()!=path:
        raise ValueError()
    return path

def bounded_bytes(path):
    path=safe_path(path)
    with path.open('rb') as stream:
        value=stream.read(262145)
    if len(value)>262144:
        raise ValueError()
    return value

def overlaps(left,right):
    return left==right or left in right.parents or right in left.parents

def config_snapshot():
    raw=bounded_bytes(__CONFIG_PATH__)
    config=configparser.ConfigParser(interpolation=None)
    config.read_string(raw.decode())
    options=config['options']
    if options.get('db_name')!=os.environ['ODOO_DB_NAME'] or options.get('db_host')!='database':
        raise ValueError()
    return raw,options

def code_roots(options):
    roots=[safe_path(value.strip()) for value in options['addons_path'].split(',') if value.strip()]
    if not roots or len(roots)>32 or len(roots)!=len(set(roots)) or Path('/opt/project/addons') not in roots:
        raise ValueError()
    roots.append(safe_path('/opt/launchplane/addons'))
    # The image owns this source layout. Isolated Python does not necessarily
    # have /odoo on sys.path, so package importability supplies no proof here.
    package=safe_path('/odoo/odoo')
    if not safe_path(package/'__init__.py').is_file():
        raise ValueError()
    roots.extend((package/'addons',package.parent/'addons'))
    mounts=[safe_path(value) for value in spec['mounts']]
    protected=[Path(value) for value in ('/odoo','/opt/project','/opt/launchplane','/venv','/usr','/lib','/bin')]
    if any(overlaps(root,mount) for root in protected for mount in mounts):
        raise ValueError()
    for root in roots:
        safe_path(root)
        if any(overlaps(root,mount) for mount in mounts):
            raise ValueError()
    data=safe_path(options['data_dir'])/'addons'
    safe_path(data)
    if data.exists():
        with os.scandir(data) as entries:
            versions=[Path(entry.path) for entry in itertools.islice(entries,33)]
        if len(versions)>32:
            raise ValueError()
        for version in versions:
            safe_path(version)
            if version.is_dir():
                roots.append(version)
    return tuple(dict.fromkeys(roots))

def source_bytes(name, roots):
    addon=spec['addons'][name]
    canonical=Path('/opt/project/addons')/addon
    for root in roots:
        candidate=root/addon
        # Broken symlinks and any second copy are ambiguous too.
        if candidate.is_symlink() or (candidate.exists() and candidate!=canonical):
            raise ValueError()
    observed={}
    producer=None
    for path,digest in spec['files'][name].items():
        if not re.fullmatch('[0-9a-f]{64}',digest):
            raise ValueError()
        raw=bounded_bytes(path)
        if hashlib.sha256(raw).hexdigest()!=digest:
            raise ValueError()
        observed[path]=digest
        if safe_path(path)==canonical/'services/connection_settings.py':
            producer=raw
    if producer is None:
        raise ValueError()
    return producer,observed

def load_producer(name,raw):
    module_name='_launchplane_verified_options_'+name
    module=types.ModuleType(module_name)
    sys.modules[module_name]=module
    # No producer output or exception is part of the transport contract.
    with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
        exec(compile(raw,'<verified-installed-producer>','exec'),module.__dict__)
    keys=module.PARAMETER_KEYS
    optional=module.OPTION_PARAMETER_KEYS
    prefix=spec['prefixes'][name]+'.'
    allowed={'host','user','password','name','db',*spec['option_fields']}
    if (not isinstance(keys,tuple) or not 1<=len(keys)<=16 or len(keys)!=len(set(keys))
        or any(not isinstance(key,str) or not key.startswith(prefix) or key[len(prefix):] not in allowed for key in keys)
        or not isinstance(optional,tuple) or not 1<=len(optional)<=4 or len(optional)!=len(set(optional))
        or not set(optional)<=set(keys)
        or any(key[len(prefix):] not in spec['option_fields'] for key in optional)
        or prefix+'password' not in keys
        or not callable(module.resolve_connection_settings) or not callable(module.resolve_connection_options)):
        raise ValueError()
    return module

def parameters(keys,options):
    connection=psycopg2.connect(host='database',port=options.get('db_port') or '5432',
        user=options['db_user'],password=options['db_password'],dbname=os.environ['ODOO_DB_NAME'],
        connect_timeout=3,options='-c default_transaction_read_only=on -c statement_timeout=3000')
    try:
        connection.set_session(readonly=True,autocommit=False)
        with connection.cursor() as cursor:
            cursor.execute('SHOW transaction_read_only')
            if cursor.fetchone()!=('on',):
                raise ValueError()
            cursor.execute('SELECT key, value FROM ir_config_parameter WHERE key = ANY(%s)',(keys,))
            rows=cursor.fetchmany(len(keys)+1)
            if len(rows)>len(keys):
                raise ValueError()
            result={}
            for key,value in rows:
                if key not in keys or key in result or not isinstance(value,str) or len(value)>16384:
                    raise ValueError()
                result[key]=value
            addon_names=list(spec['addons'].values())
            cursor.execute('SELECT name, state FROM ir_module_module WHERE name = ANY(%s)',(addon_names,))
            states=cursor.fetchmany(len(addon_names)+1)
            if len(states)>len(addon_names):
                raise ValueError()
            installed={}
            for name,state in states:
                if name not in addon_names or name in installed or not isinstance(state,str):
                    raise ValueError()
                installed[name]=state=='installed'
            return result,installed
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()

def origin(mapping,key):
    return 'absent' if key not in mapping else 'empty' if not mapping[key] else 'explicit'

def projection(value):
    result={}
    for field in spec['option_fields']:
        if hasattr(value,field):
            item=getattr(value,field)
            if type(item) is not (int if field in ('port','batch_size') else bool):
                raise ValueError()
            result[field]=item
    if not {'port','use_ssl','ssl_verify'}<=set(result):
        raise ValueError()
    return result

def options_projection(module,raw):
    try:
        values={key:raw[key] for key in module.OPTION_PARAMETER_KEYS if key in raw}
        return projection(module.resolve_connection_options(values))
    except Exception:
        return None

def full_settings(module,raw):
    try:
        value=module.resolve_connection_settings(raw)
        password=value.password
        if not isinstance(password,str) or not password:
            raise ValueError()
        return value
    except Exception:
        return None

def tls(value):
    if value is None:
        return 'unknown'
    return ('disabled' if not value['use_ssl'] else
            'enabled_with_verification' if value['ssl_verify'] else 'verification_disabled')

def agreement(left,right,field):
    return ('unknown' if left is None or right is None or field not in left or field not in right
            else 'match' if left[field]==right[field] else 'mismatch')

def source_result(name,module,applied):
    delivered={}
    for key in module.PARAMETER_KEYS:
        env_key=(spec['password_env_keys'][name] if key.endswith('.password')
                 else spec['runtime_env_prefix']+key.upper().replace('.','__'))
        if env_key in os.environ:
            delivered[key]=os.environ[env_key]
    authority={}
    authority_origins={}
    known=True
    for key in module.OPTION_PARAMETER_KEYS:
        fact=spec['authority'][name].get(key)
        if fact is None or not fact['known']:
            known=False
            authority_origins[key]='unknown'
        else:
            if fact['present']:
                authority[key]=fact['value']
            authority_origins[key]=origin(authority,key)
    a=options_projection(module,authority) if known else None
    d=options_projection(module,delivered)
    p=options_projection(module,applied)
    full_d=full_settings(module,delivered)
    full_p=full_settings(module,applied)
    try:
        if full_d is None or projection(full_d)!=d:
            full_d=None
            d=None
    except Exception:
        full_d=None
        d=None
    try:
        if full_p is None or projection(full_p)!=p:
            full_p=None
            p=None
    except Exception:
        full_p=None
        p=None
    password='unknown'
    if full_d is not None and full_p is not None:
        password='match' if hmac.compare_digest(full_d.password.encode(),full_p.password.encode()) else 'mismatch'
    evidence=[]
    # Field projection is fixed. Raw keys/defaults come exclusively from the producer.
    for field in spec['option_fields']:
        if field=='batch_size' and spec['prefixes'][name]+'.'+field not in module.OPTION_PARAMETER_KEYS:
            continue
        key=spec['prefixes'][name]+'.'+field
        consumed=key in module.OPTION_PARAMETER_KEYS
        evidence.append({'field':field,'authority':authority_origins.get(key,'absent') if consumed else 'absent',
            'delivered':origin(delivered,key) if consumed else 'absent',
            'applied':origin(applied,key) if consumed else 'absent',
            'authority_delivered':agreement(a,d,field), 'authority_applied':agreement(a,p,field),
            'delivered_applied':agreement(d,p,field)})
    return {'source':name,'producer':'verified','addon_state':'installed','options':evidence,
            'authority_tls_configuration':tls(a),'delivered_tls_configuration':tls(d),'applied_tls_configuration':tls(p),
            'delivered_settings':'valid' if full_d is not None else 'unknown',
            'applied_settings':'valid' if full_p is not None else 'unknown',
            'applied_delivered_password':password,**spec['metadata'][name]}

def run():
    if os.environ.get('PLATFORM_INSTANCE')!='testing' or json.loads(os.environ[__IDENTITY_KEY__])!=spec['identity']:
        raise ValueError()
    config,options=config_snapshot()
    roots=code_roots(options)
    modules={}
    snapshots={}
    for name in names:
        try:
            raw,snapshots[name]=source_bytes(name,roots)
            modules[name]=load_producer(name,raw)
        except Exception:
            pass
    keys=list(dict.fromkeys(key for module in modules.values() for key in module.PARAMETER_KEYS))
    before,installed=parameters(keys,options) if keys else ({},{})
    results=[]
    for name in names:
        if name not in modules:
            results.append({'source':name})
            continue
        if not installed.get(spec['addons'][name]):
            results.append({'source':name,'producer':'verified'})
            continue
        try:
            with open(os.devnull,'w') as sink,contextlib.redirect_stdout(sink),contextlib.redirect_stderr(sink):
                results.append(source_result(name,modules[name],before))
        except Exception:
            results.append({'source':name})
    after=parameters(keys,options) if keys else ({},{})
    if (after!=(before,installed) or config_snapshot()[0]!=config or code_roots(options)!=roots):
        raise ValueError()
    for index,name in enumerate(names):
        if name in modules:
            try:
                if source_bytes(name,roots)[1]!=snapshots[name]:
                    raise ValueError()
            except Exception:
                results[index]={'source':name}
    return {'runtime':'verified','sources':results}

try:
    result=run()
    validate_result(result,names)
except BaseException:
    result=unknown
print(json.dumps(result))
"""
)

HOST_PROGRAM = (
    _VALIDATOR
    + r"""
import base64,json,re,subprocess,sys
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
prefix=sys.argv[2]
names=tuple(spec['addons'])
result={'runtime':'unknown','sources':[{'source':name} for name in names]}

def inspect():
    raw=subprocess.run(['docker','inspect','--type','container',spec['container']],capture_output=True,check=True,timeout=5)
    if len(raw.stdout)>262144:
        raise ValueError()
    values=json.loads(raw.stdout)
    if not isinstance(values,list) or len(values)!=1:
        raise ValueError()
    value=values[0]
    config=value['Config']
    labels=config['Labels']
    env=dict(item.split('=',1) for item in config['Env'] if '=' in item)
    if (not re.fullmatch('[0-9a-f]{12,64}',spec['container'])
        or not re.fullmatch('[0-9a-f]{64}',value['Id']) or not value['Id'].startswith(spec['container'])
        or value['State'].get('Running') is not True
        or config['Image']!=spec['identity']['image_reference']
        or labels.get('com.docker.compose.project')!=spec['project']
        or labels.get('com.docker.compose.service')!='web' or env.get('PLATFORM_INSTANCE')!='testing'
        or json.loads(env[__IDENTITY_KEY__])!=spec['identity']):
        raise ValueError()
    image=value['Image']
    if not re.fullmatch('sha256:[0-9a-f]{64}',image):
        raise ValueError()
    raw_image=subprocess.run(['docker','image','inspect',image],capture_output=True,check=True,timeout=5)
    if len(raw_image.stdout)>262144:
        raise ValueError()
    images=json.loads(raw_image.stdout)
    if (not isinstance(images,list) or len(images)!=1 or images[0].get('Id')!=image
        or spec['identity']['image_reference'] not in (images[0].get('RepoDigests') or [])):
        raise ValueError()
    return {key:value.get(key) for key in ('Id','Image','Config','HostConfig','Mounts','NetworkSettings','RestartCount')} | {
        'State':{key:value['State'].get(key) for key in ('Running','StartedAt','Pid')},
        'VerifiedImage':{'Id':image,'RepoDigests':images[0]['RepoDigests']}}

try:
    before=inspect()
    mounts=before['Mounts']
    if not isinstance(mounts,list) or len(mounts)>128:
        raise ValueError()
    payload={key:spec[key] for key in ('identity','files','addons','prefixes','option_fields','authority','metadata',
                                      'runtime_env_prefix','password_env_keys')}
    payload['mounts']=[mount['Destination'] for mount in mounts]
    child=subprocess.run(['docker','exec','-i',before['Id'],'timeout','--kill-after=2s','20s','python3','-I','-c',spec['container_program']],
        input=json.dumps(payload),text=True,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,timeout=25,check=False)
    after=inspect()
    if before!=after or child.returncode!=0 or len(child.stdout)>16384:
        raise ValueError()
    observed=json.loads(child.stdout)
    validate_result(observed,names)
    result=observed
except Exception:
    pass
print(prefix+json.dumps({'type':'complete','result':result}),flush=True)
"""
)
