"""Bounded, redacted diagnostics from the isolated rehearsal's own custody."""

REHEARSAL_DIAGNOSTIC_TEXT_LIMIT = 4096
REHEARSAL_DIAGNOSTIC_FILE_LIMIT = 8
REHEARSAL_DIAGNOSTICS_LIBRARY = r"""
DIAGNOSTIC_TEXT_LIMIT = __TEXT_LIMIT__
DIAGNOSTIC_FILE_LIMIT = __FILE_LIMIT__
COMMAND_FAILURE_CONTEXT = None


def diagnostic_text(value, secrets=()):
    if isinstance(value, bytes):
        value=value.decode('utf-8',errors='replace')
    # Redact before bounding, including values that straddle the output bound.
    for secret in sorted(set(secrets),key=len,reverse=True):
        if secret:
            value=value.replace(secret,'[redacted]')
    value=re.sub(r'(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)','[redacted]',value)
    key=r'[A-Z0-9_]*(?:PASSWORD|PASSWD|TOKEN|SECRET|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL|PAYLOAD_B64)[A-Z0-9_]*'
    value=re.sub(r'(?i)(["\'\-]*\b'+key+r'["\']?\s*(?:[=:]|\s)\s*)(?:"[^"\r\n]*"|\'[^\'\r\n]*\'|[^\s,;]+)',r'\1[redacted]',value)
    value=re.sub(r'(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+',r'\1[redacted]',value)
    value=re.sub(r'(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s@]+@',r'\1[redacted]@',value)
    value=''.join(character for character in value if character in '\n\t' or ord(character)>=32 and ord(character)!=127)
    return value[-DIAGNOSTIC_TEXT_LIMIT:]


def diagnostic_secrets(values):
    return [str(value) for key,value in values.items() if value and re.search(
        r'(?i)PASSWORD|PASSWD|TOKEN|SECRET|KEY|CREDENTIAL|PAYLOAD',key)]


def command_diagnostic(arguments,result,environment):
    values=dict(os.environ if environment is None else environment)
    if COMMAND_FAILURE_CONTEXT is not None:
        spec,_=COMMAND_FAILURE_CONTEXT
        for field in ('environment','source','workflow'):
            values.update(spec.get(field,{}))
    for argument in arguments:
        if '=' in argument:
            key,value=argument.split('=',1)
            values[key]=value
    # Never include argv, stdin or the environment in a diagnostic.
    text=(result.stdout or b'')+b'\n'+(result.stderr or b'')
    caller=sys._getframe(2)
    if caller.f_code.co_name=='docker':
        caller=caller.f_back
    return {'step':caller.f_code.co_name,
        'exit_status':result.returncode,'error_text':diagnostic_text(text,diagnostic_secrets(values))}


def command_failure_path(spec,root):
    return root/(spec['action']+'-command-failure.json')


def retain_command_failure(spec,root,diagnostic):
    path=command_failure_path(spec,root)
    if not path.exists():
        save(path,{'intent_sha256':spec['record']['intent_sha256'],
            'attempt_sha256':spec['attempt_sha256'],
            'record_binding_sha256':spec['record_binding_sha256'],**diagnostic})


def read_command_failure(spec,root):
    path=command_failure_path(spec,root)
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor,'r') as stream:
        metadata=os.fstat(stream.fileno())
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size>32768 or stat.S_IMODE(metadata.st_mode)!=0o600:
            raise RuntimeError('Child diagnostic custody is unverified')
        value=json.load(stream)
    if (set(value)!={'intent_sha256','attempt_sha256','record_binding_sha256','step','exit_status','error_text'}
            or value['intent_sha256']!=spec['record']['intent_sha256']
            or any(value[key]!=spec[key] for key in ('attempt_sha256','record_binding_sha256'))
            or not isinstance(value['step'],str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',value['step'])
            or type(value['exit_status']) is not int or value['exit_status']==0
            or not isinstance(value['error_text'],str) or len(value['error_text'])>DIAGNOSTIC_TEXT_LIMIT):
        raise RuntimeError('Child diagnostic binding is unverified')
    return value


def retained_upgrade_logs(spec,containers,root):
    # The complete runtime guard has already proved these volumes exclusive.
    runner=containers['script-runner'][1]
    mounts=[item for item in runner['Mounts'] if item['Name']==spec['record']['rehearsal_id']+'-logs']
    if len(mounts)!=1:
        raise RuntimeError('Rehearsal log custody is unverified')
    started=(root/(spec['action']+'.started')).stat().st_mtime
    since=datetime.fromtimestamp(started,timezone.utc).isoformat()
    runtime=[]
    for service in ('script-runner','database'):
        identifier,container=containers[service]
        values=dict(item.split('=',1) for item in container['Config']['Env'] if '=' in item)
        try:
            result=subprocess.run(['docker','logs','--tail','100','--since',since,identifier],
                stdout=subprocess.PIPE,stderr=subprocess.STDOUT,check=False,timeout=30)
        except subprocess.TimeoutExpired:
            runtime.append({'service':service,'available':False,'tail':''})
        else:
            runtime.append({'service':service,'available':result.returncode==0,
                'tail':diagnostic_text(result.stdout,diagnostic_secrets(values)) if result.returncode==0 else ''})
    root=Path(mounts[0]['Source'])
    if root.is_symlink() or root.resolve(strict=True)!=root.absolute():
        raise RuntimeError('Rehearsal log custody is aliased')
    values=dict(item.split('=',1) for item in runner['Config']['Env'] if '=' in item)
    secrets=diagnostic_secrets(values)
    candidates=[]
    # Fixed, shallow, bounded inventory; no caller-supplied path or shell.
    pending=[root]
    scanned=0
    while pending:
        directory=pending.pop()
        with os.scandir(directory) as entries:
            for entry in entries:
                scanned+=1
                if scanned>128:
                    raise RuntimeError('Rehearsal log inventory exceeds its bound')
                if entry.is_symlink():
                    continue
                if directory==root and entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
                elif entry.is_file(follow_symlinks=False) and re.fullmatch(r'[A-Za-z0-9_.-]+\.log(?:\.[0-9]+)?',entry.name):
                    metadata=entry.stat(follow_symlinks=False)
                    candidates.append((metadata.st_mtime_ns,Path(entry.path)))
    logs=[]
    for _,path in sorted(candidates,reverse=True)[:DIAGNOSTIC_FILE_LIMIT]:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
        with os.fdopen(descriptor,'rb') as stream:
            before=os.fstat(stream.fileno())
            if not stat.S_ISREG(before.st_mode):
                raise RuntimeError('Rehearsal log is not regular')
            stream.seek(max(0,before.st_size-65536))
            tail=stream.read(65536)
            after=os.fstat(stream.fileno())
            if (before.st_ino,before.st_size,before.st_mtime_ns)!=(after.st_ino,after.st_size,after.st_mtime_ns):
                raise RuntimeError('Rehearsal log changed during observation')
        logs.append({'file':str(path.relative_to(root)),'bytes':before.st_size,
            'modified_at':datetime.fromtimestamp(before.st_mtime,timezone.utc).isoformat(),
            'tail':diagnostic_text(tail,secrets)})
    return {'files':logs,'runtime':runtime,'since':since,'truncated':len(candidates)>DIAGNOSTIC_FILE_LIMIT}
""".replace("__TEXT_LIMIT__", str(REHEARSAL_DIAGNOSTIC_TEXT_LIMIT)).replace(
    "__FILE_LIMIT__", str(REHEARSAL_DIAGNOSTIC_FILE_LIMIT)
)
