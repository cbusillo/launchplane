"""Bounded, redacted diagnostics from the isolated rehearsal's own custody."""

from control_plane.contracts.native_operation_evidence import NATIVE_OPERATION_RESULT_MAX_BYTES

REHEARSAL_DIAGNOSTIC_TEXT_LIMIT = 4096
REHEARSAL_DIAGNOSTIC_FILE_LIMIT = 8
REHEARSAL_DIAGNOSTIC_PROCESS_LIMIT = 1024 * 1024
REHEARSAL_DIAGNOSTICS_LIBRARY = (
    r"""
DIAGNOSTIC_TEXT_LIMIT = __TEXT_LIMIT__
DIAGNOSTIC_FILE_LIMIT = __FILE_LIMIT__
DIAGNOSTIC_PROCESS_LIMIT = __PROCESS_LIMIT__
RESULT_MAX_BYTES = __RESULT_LIMIT__
COMMAND_FAILURE_CONTEXT = None


def encoded_rehearsal_evidence(result):
    return json.dumps(result,ensure_ascii=False,separators=(',',':')).encode('utf-8')


def emit_rehearsal_result(result):
    logs=result.get('retained_upgrade_logs')
    while len(encoded_rehearsal_evidence(result))>RESULT_MAX_BYTES and logs is not None:
        logs['truncated']=True
        if logs['files']:
            logs['files'].pop()
        elif logs['runtime']:
            logs['runtime'].pop()
        else:
            result.pop('retained_upgrade_logs')
            result['retained_upgrade_logs_unavailable']=True
            logs=None
    encoded=encoded_rehearsal_evidence(result)
    if len(encoded)>RESULT_MAX_BYTES:
        raise RuntimeError('Required rehearsal evidence exceeds its transport budget')
    print('rehearsal_result='+encoded.hex(),flush=True)


def diagnostic_text(value, secrets=()):
    if isinstance(value, bytes):
        value=value.decode('utf-8',errors='replace')
    # Redact before bounding, including values that straddle the output bound.
    for secret in sorted(set(secrets),key=len,reverse=True):
        if secret:
            value=value.replace(secret,'[redacted]')
    value=re.sub(r'(?is)-----BEGIN [A-Z ]*PRIVATE KEY-----.*?(?:-----END [A-Z ]*PRIVATE KEY-----|$)','[redacted]',value)
    key=r'[A-Z0-9_]*(?:PASSWORD|PASSWD|TOKEN|SECRET|API_KEY|ACCESS_KEY|PRIVATE_KEY|CREDENTIAL|PAYLOAD_B64)[A-Z0-9_]*'
    item=r'(?:"[^"\r\n]*"|\'[^\'\r\n]*\'|[^\s,;]+)'
    value=re.sub(r'(?i)(["\'\-]*\b'+key+r'["\']?\s*[=:]\s*)'+item,r'\1[redacted]',value)
    value=re.sub(r'(?i)(--'+key+r'\s+)'+item,r'\1[redacted]',value)
    value=re.sub(r'(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]+',r'\1[redacted]',value)
    value=re.sub(r'(?i)(\b[a-z][a-z0-9+.-]*://)[^/\s@]+@',r'\1[redacted]@',value)
    # PostgreSQL statements/row DETAIL are not needed to identify an exception.
    value=re.sub(r'(?im)^.*?\b(?:DETAIL|STATEMENT):.*$', '[row detail withheld]', value)
    value=''.join(character for character in value if character in '\n\t' or ord(character)>=32 and ord(character)!=127)
    return value.encode('utf-8')[-DIAGNOSTIC_TEXT_LIMIT:].decode('utf-8',errors='ignore')


def diagnostic_secrets(values):
    return [str(value) for key,value in values.items() if value and re.search(
        r'(?i)(?:^|_)(?:PASSWORD|PASSWD|TOKEN|SECRET|KEY|CREDENTIALS?|PAYLOAD)(?:$|_)',key)]


def spec_diagnostic_secrets(spec):
    values={}
    for field in ('environment','source','workflow'):
        values.update(spec.get(field,{}))
    values.update(spec['record'].get('upgrade_environment',{}))
    return diagnostic_secrets(values)


@contextmanager
def command_failure_scope(spec,root):
    global COMMAND_FAILURE_CONTEXT
    COMMAND_FAILURE_CONTEXT=(spec,root)
    try:
        yield
    except RehearsalCommandFailure as error:
        diagnostic=error.diagnostic
        cause=error.__context__
        while cause is not None:
            if isinstance(cause,RehearsalCommandFailure):
                diagnostic=cause.diagnostic
            cause=cause.__context__
        if (root/(spec['action']+'.started')).is_file():
            try:
                retain_command_failure(spec,root,diagnostic)
            except OSError:
                # Full storage and an interrupted .pending receipt must not
                # replace or discard the failure that ended this operation.
                encoded=encoded_rehearsal_evidence(bound_command_failure(spec,diagnostic))
                if len(encoded)<=RESULT_MAX_BYTES:
                    print('rehearsal_child_failure='+encoded.hex(),flush=True)
        raise
    finally:
        COMMAND_FAILURE_CONTEXT=None


def command_diagnostic(arguments,result,environment):
    values=dict(os.environ if environment is None else environment)
    for argument in arguments:
        if '=' in argument:
            key,value=argument.split('=',1)
            values[key]=value
    # Never include argv, stdin or the environment in a diagnostic.
    secrets=diagnostic_secrets(values)
    if COMMAND_FAILURE_CONTEXT is not None:
        secrets+=spec_diagnostic_secrets(COMMAND_FAILURE_CONTEXT[0])
    stdout=diagnostic_text(result.stdout or b'',secrets)
    stderr=diagnostic_text(result.stderr or b'',secrets)
    if stdout and stderr:
        stdout=stdout.encode('utf-8')
        stderr=stderr.encode('utf-8')
        available=DIAGNOSTIC_TEXT_LIMIT-len(b'stdout:\n\nstderr:\n')
        stdout_bound=min(len(stdout),available//2)
        stderr_bound=min(len(stderr),available-stdout_bound)
        stdout_bound=min(len(stdout),available-stderr_bound)
        text='stdout:\n'+stdout[-stdout_bound:].decode('utf-8',errors='ignore')+'\nstderr:\n'+stderr[-stderr_bound:].decode('utf-8',errors='ignore')
    else:
        text=stdout or stderr
    caller=sys._getframe(2)
    while caller.f_back is not None and caller.f_code.co_name in {'docker','sql','bounded_capture'}:
        caller=caller.f_back
    return {'step':caller.f_code.co_name,
        'exit_status':result.returncode,'error_text':diagnostic_text(text,secrets)}


def command_failure_path(spec,root):
    return root/(spec['action']+'-command-failure.json')


def bound_command_failure(spec,diagnostic):
    return {'intent_sha256':spec['record']['intent_sha256'],
        'attempt_sha256':spec['attempt_sha256'],
        'record_binding_sha256':spec['record_binding_sha256'],**diagnostic}


def retain_command_failure(spec,root,diagnostic):
    path=command_failure_path(spec,root)
    if not path.exists():
        save(path,bound_command_failure(spec,diagnostic))


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
    if (not isinstance(value,dict) or set(value)!={'intent_sha256','attempt_sha256','record_binding_sha256','step','exit_status','error_text'}
            or value['intent_sha256']!=spec['record']['intent_sha256']
            or any(value[key]!=spec[key] for key in ('attempt_sha256','record_binding_sha256'))
            or not isinstance(value['step'],str) or not re.fullmatch('[A-Za-z_][A-Za-z0-9_]*',value['step'])
            or type(value['exit_status']) is not int or value['exit_status']==0
            or not isinstance(value['error_text'],str) or len(value['error_text'])>DIAGNOSTIC_TEXT_LIMIT):
        raise RuntimeError('Child diagnostic binding is unverified')
    return value


def diagnostic_process_output(arguments):
    process=subprocess.Popen(arguments,stdout=subprocess.PIPE,stderr=subprocess.STDOUT)
    deadline=time.monotonic()+30
    tail=b''
    consumed=0
    try:
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdout,selectors.EVENT_READ)
            while True:
                remaining=deadline-time.monotonic()
                if remaining<=0 or not selector.select(remaining):
                    return False,b''
                chunk=os.read(process.stdout.fileno(),65536)
                if not chunk:
                    break
                consumed+=len(chunk)
                if consumed>DIAGNOSTIC_PROCESS_LIMIT:
                    return False,b''
                tail=(tail+chunk)[-65536:]
        return process.wait(timeout=max(0.1,deadline-time.monotonic()))==0,tail
    except subprocess.TimeoutExpired:
        return False,b''
    finally:
        process.stdout.close()
        if process.poll() is None:
            process.kill()
        process.wait()


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
        available,output=diagnostic_process_output(['docker','logs','--tail','100','--since',since,identifier])
        runtime.append({'service':service,'available':available,
            'tail':diagnostic_text(output,diagnostic_secrets(values)+spec_diagnostic_secrets(spec)) if available else ''})
    root=Path(mounts[0]['Source'])
    if root.is_symlink() or root.resolve(strict=True)!=root.absolute():
        raise RuntimeError('Rehearsal log custody is aliased')
    values=dict(item.split('=',1) for item in runner['Config']['Env'] if '=' in item)
    secrets=diagnostic_secrets(values)+spec_diagnostic_secrets(spec)
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
                if directory==root and entry.is_dir(follow_symlinks=False) and re.fullmatch('[A-Za-z0-9_.-]+',entry.name):
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
""".replace("__TEXT_LIMIT__", str(REHEARSAL_DIAGNOSTIC_TEXT_LIMIT))
    .replace("__FILE_LIMIT__", str(REHEARSAL_DIAGNOSTIC_FILE_LIMIT))
    .replace("__PROCESS_LIMIT__", str(REHEARSAL_DIAGNOSTIC_PROCESS_LIMIT))
    .replace("__RESULT_LIMIT__", str(NATIVE_OPERATION_RESULT_MAX_BYTES))
)
