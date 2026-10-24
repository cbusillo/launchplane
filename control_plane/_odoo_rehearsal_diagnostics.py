from control_plane.contracts.native_operation_evidence import NATIVE_OPERATION_RESULT_MAX_BYTES

REHEARSAL_DIAGNOSTIC_TEXT_LIMIT = 4096
REHEARSAL_DIAGNOSTIC_TAIL_LIMIT = 64 * 1024
REHEARSAL_DIAGNOSTIC_FILE_LIMIT = 8
REHEARSAL_DIAGNOSTICS_LIBRARY = (
    r"""
DIAGNOSTIC_TEXT_LIMIT = __TEXT_LIMIT__
DIAGNOSTIC_TAIL_LIMIT = __TAIL_LIMIT__
RESULT_MAX_BYTES = __RESULT_LIMIT__


def encoded_rehearsal_evidence(result):
    return json.dumps(result,ensure_ascii=False,separators=(',',':')).encode('utf-8')


def emit_rehearsal_result(result):
    if result.pop('retained_upgrade_logs',None) is not None:
        result['retained_upgrade_logs_unavailable']=True
    if result.get('child_failure') is not None:
        result['child_failure']={**result['child_failure'],'error_text':''}
    encoded=encoded_rehearsal_evidence(result)
    if len(encoded)>RESULT_MAX_BYTES:
        raise RuntimeError('Required rehearsal evidence exceeds its transport budget')
    print('rehearsal_result='+encoded.hex(),flush=True)


@contextmanager
def command_failure_scope(spec,root):
    try:
        yield
    except RehearsalCommandFailure as error:
        diagnostic=error.diagnostic
        cause=error.__context__
        while cause is not None:
            if isinstance(cause,RehearsalCommandFailure):
                diagnostic=cause.diagnostic
            cause=cause.__context__
        if (root/(spec.get('receipt_action',spec['action'])+'.started')).is_file():
            try:
                retain_command_failure(spec,root,diagnostic)
            except OSError:
                # Full storage and an interrupted .pending receipt must not
                # replace or discard the failure that ended this operation.
                encoded=encoded_rehearsal_evidence(bound_command_failure(spec,diagnostic))
                if len(encoded)<=RESULT_MAX_BYTES:
                    print('rehearsal_child_failure='+encoded.hex(),flush=True)
        raise


def command_diagnostic(result):
    caller=sys._getframe(2)
    while caller.f_back is not None and caller.f_code.co_name in {'docker','sql','bounded_capture'}:
        caller=caller.f_back
    return {'step':caller.f_code.co_name,'exit_status':result.returncode,'error_text':''}


def command_failure_path(spec,root):
    return root/(spec.get('receipt_action',spec['action'])+'-command-failure.json')


def bound_command_failure(spec,diagnostic):
    return {'intent_sha256':spec['record']['intent_sha256'],
        'attempt_sha256':spec['attempt_sha256'],
        'record_binding_sha256':spec['record_binding_sha256'],
        'step':diagnostic['step'],'exit_status':diagnostic['exit_status'],'error_text':''}


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
    return {**value,'error_text':''}

""".replace("__TEXT_LIMIT__", str(REHEARSAL_DIAGNOSTIC_TEXT_LIMIT))
    .replace("__TAIL_LIMIT__", str(REHEARSAL_DIAGNOSTIC_TAIL_LIMIT))
    .replace("__RESULT_LIMIT__", str(NATIVE_OPERATION_RESULT_MAX_BYTES))
)
