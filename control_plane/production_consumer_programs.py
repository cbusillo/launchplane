import inspect

from control_plane import odoo_source_probe
from control_plane.odoo_source_prerequisite_programs import CONTAINER_LIBRARY, HOST_LIBRARY
from control_plane.production_recipient_published_host import PUBLISHED_SOURCE_LIBRARY

CONSUMER_TABLE_ROLES = frozenset(
    {
        "CUSTOMER_TABLE",
        "CONTACT_TABLE",
        "PRODUCT_TABLE",
        "TICKET_TABLE",
        "TICKET_PROPERTIES_TABLE",
        "TICKET_COMMENT_TABLE",
        "ESTIMATE_TABLE",
        "ESTIMATE_LINE_ITEM_TABLE",
        "INVOICE_TABLE",
        "INVOICE_LINE_ITEM_TABLE",
    }
)

GRANT_LIBRARY = (
    "import re, pymysql\nGrantStatus=str\n_READ_PRIVILEGES="
    + repr(odoo_source_probe._READ_PRIVILEGES)
    + "\n_GRANT=re.compile("
    + repr(odoo_source_probe._GRANT.pattern)
    + ")\n"
    + inspect.getsource(odoo_source_probe.classify_grants)
)

CONSUMER_CONTAINER_PROGRAM = (
    CONTAINER_LIBRARY
    + GRANT_LIBRARY
    + "\nTABLE_ROLES="
    + repr(CONSUMER_TABLE_ROLES)
    + "\n"
    + r"""
import ast, copy
output=sys.stdout



def verified_connection_code(roots):
    addon=spec['addons']['repairshopr']
    canonical=Path('/opt/project/addons')/addon
    path=canonical/'services/repairshopr_sync_client.py'
    raw=bounded_bytes(path)
    if hashlib.sha256(raw).hexdigest()!=spec['files']['repairshopr'][str(path)]:
        raise ValueError()
    tree=ast.parse(raw)
    bindings=[]
    for node in tree.body:
        if isinstance(node,ast.Import) and len(node.names)==1 and node.names[0].name=='pymysql' and node.names[0].asname is None:
            bindings.append('pymysql')
            continue
        if (isinstance(node,ast.ImportFrom) and node.module=='odoo.addons.external_ids.utils.ssl_context'
            and node.level==0 and len(node.names)==1 and node.names[0].name=='build_ssl_context' and node.names[0].asname is None):
            bindings.append('build_ssl_context')
            continue
        if any((isinstance(item,ast.Name) and isinstance(item.ctx,ast.Store) and item.id in {'pymysql','build_ssl_context'})
               or (isinstance(item,(ast.FunctionDef,ast.ClassDef)) and item.name in {'pymysql','build_ssl_context'})
               or isinstance(item,(ast.Import,ast.ImportFrom)) and any((alias.asname or alias.name.split('.')[0]) in {'pymysql','build_ssl_context'} for alias in item.names)
               for item in ast.walk(node)):
            raise ValueError()
    if sorted(bindings)!=['build_ssl_context','pymysql']:
        raise ValueError()
    tables={}
    for node in tree.body:
        if isinstance(node,ast.Assign) and len(node.targets)==1 and isinstance(node.targets[0],ast.Name):
            key=node.targets[0].id
            if key in TABLE_ROLES:
                if key in tables:
                    raise ValueError()
                tables[key]=ast.literal_eval(node.value)
    if set(tables)!=TABLE_ROLES or any(not isinstance(v,str) or not re.fullmatch('[a-z][a-z0-9_]{0,127}',v) for v in tables.values()):
        raise ValueError()
    classes=[node for node in tree.body if isinstance(node,ast.ClassDef) and node.name=='RepairshoprSyncClient']
    methods=[node for node in classes[0].body if isinstance(node,ast.FunctionDef) and node.name=='_open_connection'] if len(classes)==1 else []
    if len(methods)!=1:
        raise ValueError()
    method=copy.deepcopy(methods[0])
    method.returns=None
    method.decorator_list=[]
    for argument in (*method.args.posonlyargs,*method.args.args,*method.args.kwonlyargs):
        argument.annotation=None
    tls_raw,tls_snapshot=verified_source('external_ids',Path(spec['payload_root'])/'external_ids',
        spec['files']['tls'],roots,'utils/ssl_context.py')
    tls_module=types.ModuleType('_verified_tls')
    exec(compile(tls_raw,'<verified-installed-tls>','exec'),tls_module.__dict__)
    namespace={'pymysql':types.SimpleNamespace(connect=bounded_connect),'build_ssl_context':tls_module.build_ssl_context}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[method],type_ignores=[])),'<verified-installed-client>','exec'),namespace)
    return namespace['_open_connection'],tables,tls_snapshot

def bounded_connect(**kwargs):
    if set(kwargs)!={'host','user','password','database','port','charset','autocommit','ssl'}:
        raise ValueError()
    kwargs.update(connect_timeout=3,read_timeout=40,write_timeout=3,local_infile=False,autocommit=False,
                  init_command='SET SESSION max_statement_time=35')
    return pymysql.connect(**kwargs)

def query_consumer(settings,open_connection,tables):
    connection=open_connection(types.SimpleNamespace(_settings=settings))
    try:
        with connection.cursor() as cursor:
            cursor.execute('SHOW GRANTS')
            grants=classify_grants(cursor.fetchmany(129),mariadb_authentication=True)
            if grants!='read_only':
                return {'query':'failed' if grants=='unsafe' else 'unknown','populated':'unknown'}
            cursor.execute('SET TRANSACTION READ ONLY')
            cursor.execute('START TRANSACTION READ ONLY')
            cursor.execute('SELECT DATABASE(), CONNECTION_ID()')
            database,identifier=cursor.fetchone()
            if database!=settings.database or type(identifier) is not int or not 0<identifier<2**64:
                return {'query':'failed','populated':'unknown'}
            populated=True
            for table in tables.values():
                cursor.execute('SELECT 1 FROM `'+table+'` LIMIT 1')
                rows=cursor.fetchmany(2)
                if not isinstance(rows,(list,tuple)) or len(rows)>1 or any(row!=(1,) for row in rows):
                    raise ValueError()
                populated=populated and bool(rows)
            output.write(json.dumps({'type':'connection','connection_id':identifier})+'\n')
            output.flush()
            cursor.execute("SELECT SLEEP(30), '"+spec['nonce']+"'")
            if cursor.fetchone()!=(0,spec['nonce']):
                raise ValueError()
            return {'query':'verified','populated':'verified' if populated else 'failed'}
    except pymysql.MySQLError as error:
        if not error.args or error.args[0] not in {1044,1045,1049,1142,1143,1146}:
            return {'query':'unknown','populated':'unknown'}
        return {'query':'failed','populated':'unknown'}
    finally:
        try:
            connection.rollback()
        finally:
            connection.close()

def run_consumer():
    identity=spec['identity']
    if (identity['instance']!=os.environ.get('PLATFORM_INSTANCE')
        or identity!=json.loads(os.environ[__IDENTITY_KEY__])
        or not re.fullmatch('[a-f0-9]{32}',spec['nonce'])):
        raise ValueError()
    config,options=config_snapshot()
    roots=code_roots(options)
    raw,snapshot=source_bytes('repairshopr',roots)
    module=load_producer('repairshopr',raw)
    payload_raw,payload_snapshot=payload_bytes(roots)
    producer=load_payload_producer(payload_raw)
    payload=producer.decode_payload(os.environ.get(producer.ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY,''))
    before,installed=parameters(list(module.PARAMETER_KEYS),options)
    if not installed.get(spec['addons']['repairshopr']) or not installed.get(spec['payload_addon']) or not installed.get('external_ids'):
        return {'query':'unsupported','populated':'unknown'}
    delivered=producer.resolve_config_parameters(payload,os.environ,keys=module.PARAMETER_KEYS)
    for key,digest in spec['desired_env_sha256'].items():
        actual=os.environ.get(key)
        if not isinstance(actual,str) or not hmac.compare_digest(hmac.new(bytes.fromhex(spec['nonce']),actual.encode(),hashlib.sha256).hexdigest(),digest):
            raise ValueError()
    desired=producer.resolve_config_parameters(spec['desired_payload'],os.environ,keys=module.PARAMETER_KEYS)
    applied=module.resolve_connection_settings(before)
    if applied!=module.resolve_connection_settings(delivered) or applied!=module.resolve_connection_settings(desired):
        return {'query':'unknown','populated':'unknown'}
    opener,tables,tls_snapshot=verified_connection_code(roots)
    result=query_consumer(applied,opener,tables)
    if (parameters(list(module.PARAMETER_KEYS),options)!=(before,installed)
        or config_snapshot()[0]!=config or code_roots(options)!=roots
        or source_bytes('repairshopr',roots)[1]!=snapshot
        or payload_bytes(roots)[1]!=payload_snapshot or verified_connection_code(roots)[2]!=tls_snapshot):
        raise ValueError()
    return result

try:
    with open(os.devnull,'w') as sink,contextlib.redirect_stderr(sink),contextlib.redirect_stdout(sink):
        result=run_consumer()
except BaseException:
    result={'query':'unknown','populated':'unknown'}
print(json.dumps({'type':'complete','result':result}),flush=True)
"""
)

CONSUMER_HOST_PROGRAM = (
    HOST_LIBRARY
    + r"""
import selectors, time

def validate_frame(frame):
    states={'verified','failed','unknown','unsupported'}
    if frame.get('type')=='connection':
        return set(frame)=={'type','connection_id'} and type(frame['connection_id']) is int and 0<frame['connection_id']<2**64
    return (set(frame)=={'type','result'} and frame['type']=='complete' and isinstance(frame['result'],dict)
            and set(frame['result'])=={'query','populated'}
            and all(isinstance(v,str) and v in states for v in frame['result'].values()))

child=None
try:
    instance=spec['identity']['instance']
    before=inspect(instance)
    payload={**spec,'mounts':[m['Destination'] for m in before['Mounts']]}
    if len(payload['mounts'])>128:
        raise ValueError()
    child=subprocess.Popen(['docker','exec','-i',before['Id'],'timeout','--kill-after=2s','65s','python3','-I','-u','-c',spec['container_program']],
        stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,text=True)
    child.stdin.write(json.dumps(payload))
    child.stdin.close()
    child.stdin=None
    frames=[]
    deadline=time.monotonic()+70
    with selectors.DefaultSelector() as selector:
        selector.register(child.stdout,selectors.EVENT_READ)
        while len(frames)<2:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not selector.select(remaining):
                raise ValueError()
            line=child.stdout.readline(1025)
            if not line or len(line)>1024:
                raise ValueError()
            frame=json.loads(line)
            if not validate_frame(frame):
                raise ValueError()
            if frame['type']=='complete':
                frames.append(frame)
                break
            if frames:
                raise ValueError()
            frames.append(frame)
            print(prefix+json.dumps(frame),flush=True)
    child.wait(timeout=max(1,deadline-time.monotonic()))
    if child.returncode or not frames or frames[-1]['type']!='complete' or before!=inspect(instance):
        raise ValueError()
    result=frames[-1]['result']
except BaseException:
    result={'query':'unknown','populated':'unknown'}
finally:
    if child is not None and child.poll() is None:
        child.kill()
        child.wait()
print(prefix+json.dumps({'type':'complete','result':result}),flush=True)
"""
)

SOURCE_WITNESS_PROGRAM = (
    r"""
import base64,hashlib,json,re,signal,subprocess,sys,time
from pathlib import Path
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
prefix=sys.argv[2]

def docker(*args):
    result=subprocess.run(['docker',*args],capture_output=True,text=True,timeout=5)
    if result.returncode or len(result.stdout)>262144:
        raise ValueError()
    return result.stdout

def digest_json(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()
"""
    + PUBLISHED_SOURCE_LIBRARY
    + r"""
def deadline(_signal,_frame):
    raise TimeoutError()
signal.signal(signal.SIGALRM,deadline)
signal.alarm(25)
result={'verified':False}
try:
    identifier=spec['connection_id']
    nonce=spec['nonce']
    if type(identifier) is not int or not 0<identifier<2**64 or not re.fullmatch('[a-f0-9]{32}',nonce):
        raise ValueError()
    before=published_source(spec)
    database=spec['database'].encode().hex()
    statement=("SELECT SLEEP(30), '"+nonce+"'").encode().hex()
    query=('SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE ID='+str(identifier)
           +' AND DB=CONVERT(0x'+database+' USING utf8mb4) AND COMMAND=\'Query\' AND INFO=CONVERT(0x'+statement+' USING utf8mb4)')
    script='export MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}"; exec timeout -s KILL 5 mariadb --no-defaults --protocol=socket --user=root --batch --skip-column-names --init-command="SET SESSION max_statement_time=3" --execute="$1"'
    matched=False
    for attempt in range(3):
        if docker('exec',before[1],'sh','-ec',script,'witness',query)=='1\n':
            matched=True
            break
        time.sleep(0.1)
    after=published_source(spec)
    result={'verified':matched and before==after}
except BaseException:
    pass
finally:
    signal.alarm(0)
print(prefix+json.dumps({'type':'complete','result':result}),flush=True)
"""
)
