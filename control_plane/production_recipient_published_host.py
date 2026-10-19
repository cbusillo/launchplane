"""Fixed logical capture of a serving MariaDB; only independent scratch is restored."""

from control_plane.production_recipient_host import RECOVERY_LIBRARY
from control_plane.production_recipient_resources import RESOURCE_LIBRARY


PUBLISHED_RECOVERY_LIBRARY = r"""
import resource
import selectors

# Same-image canonical roundtrip checks the real captured SQL artifact, not a
# caller's row count. System/auth databases are initialized only in scratch.
DUMP_ARGUMENTS = [
    'mariadb-dump','--no-defaults','--protocol=socket','--user=root',
    '--all-databases','--ignore-database=mysql','--ignore-database=sys',
    '--ignore-database=information_schema','--ignore-database=performance_schema',
    '--skip-lock-tables','--no-autocommit','--max-statement-time=90','--quick','--hex-blob','--routines','--events','--triggers',
    '--skip-comments','--skip-dump-date','--skip-add-locks','--skip-disable-keys',
    '--skip-extended-insert','--order-by-primary',
]

# All native SQL launches share one nonce lock. A terminal inspector cancels
# that nonce before proving server quiescence, fencing a delayed Docker exec.
NATIVE_PREAMBLE = r'''
set -eu
operation=$1
nonce=$2
shift 2
umask 077
directory=/tmp/launchplane-capture-$operation-$nonce
test ! -L "$directory"
mkdir -m 700 "$directory" 2>/dev/null || test -d "$directory"
test "$(stat -c '%u:%a' "$directory")" = 0:700
test ! -L "$directory/lock"
exec 9>"$directory/lock"
flock -n 9 || exit 75
publish_exit() {
    printf '%s:%s:%s\n' "$nonce" "$1" "$2" >"$directory/exit"
    sync "$directory/exit" "$directory"
}
sql() {
    timeout -s KILL 5 mariadb --no-defaults --protocol=socket --user=root --batch --skip-column-names --init-command="SET SESSION max_statement_time=5" --execute="$1"
}
auth() {
    test -n "${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}" || test "${MARIADB_ALLOW_EMPTY_ROOT_PASSWORD:-${MYSQL_ALLOW_EMPTY_PASSWORD:-}}" = 1
    export MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}"
}
'''

NATIVE_CAPTURE_SCRIPT = NATIVE_PREAMBLE + r'''
test ! -e "$directory/cancelled" || exit 76
holder_timeout=$1
dump_timeout=$2
shift 2
phase=preflight
trap 'code=$?; if [ "$phase" = preflight ]; then publish_exit refused "$code"; fi' EXIT
# No SQL launches before this exact original host's post-READY permission.
# A queued exec after container restart cannot consume prebuffered GO.
printf '%s:READY\n' "$nonce" >&2
go=$(timeout -s KILL 5 sh -c 'IFS= read -r value || exit 1; printf "%s\n" "$value"')
test "$go" = "$nonce:GO"
auth
command -v timeout >/dev/null
command -v mariadb-dump >/dev/null
test "$(sql "SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE USER='root' AND ID<>CONNECTION_ID()")" = 0
phase=capture
printf '%s:active\n' "$nonce" >"$directory/state"
mkfifo -m 600 "$directory/input"
exec 3<>"$directory/input"
# The holder is idle after FTWRL; the dump's potentially long queries run on
# another connection and never own the global read lock themselves.
timeout -s KILL "$holder_timeout" mariadb --no-defaults --protocol=socket --user=root --batch --skip-column-names --unbuffered <"$directory/input" >"$directory/holder" 2>/dev/null &
holder=$!
printf "SET SESSION lock_wait_timeout=5;\nSET SESSION max_statement_time=5;\nSELECT GET_LOCK('lp-capture-%s',0);\nSELECT CONNECTION_ID();\nFLUSH TABLES WITH READ LOCK;\nSELECT '%s:locked';\n" "$nonce" "$nonce" >&3
locked=false
while kill -0 "$holder" 2>/dev/null; do
    if grep -Fx "$nonce:locked" "$directory/holder" >/dev/null; then locked=true; break; fi
    sleep 0.1
done
status=1
if [ "$locked" = true ] && [ "$(head -n 1 "$directory/holder")" = 1 ]; then
    connection=$(sed -n '2p' "$directory/holder")
    printf '%s\n' "$connection" >"$directory/connection"
    if timeout -s KILL "$dump_timeout" "$@" 2>/dev/null; then status=0; else status=$?; fi
    # If the holder deadline expired during the dump, this is not one snapshot.
    if [ "$(sql "SELECT IS_USED_LOCK('lp-capture-$nonce')")" != "$connection" ]; then status=1; fi
fi
printf 'UNLOCK TABLES;\nquit\n' >&3
exec 3>&-
holder_status=0
wait "$holder" || holder_status=$?
if [ "$holder_status" != 0 ]; then status=1; fi
# Client exit alone is insufficient: server work can outlive a killed client.
test "$(sql "SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE USER='root' AND ID<>CONNECTION_ID()")" = 0
publish_exit done "$status"
exit "$status"
'''

NATIVE_INSPECT_SCRIPT = NATIVE_PREAMBLE + r'''
# The lock is also inherited by both native clients. Once it can be acquired,
# cancellation fences every late launcher of this nonce before the SQL read.
: >"$directory/cancelled"
sync "$directory/cancelled" "$directory"
if [ -f "$directory/exit" ] && grep -E "^$nonce:refused:[0-9]+$" "$directory/exit" >/dev/null; then
    cat "$directory/exit"
    exit 0
fi
auth
test "$(sql "SELECT COUNT(*) FROM information_schema.PROCESSLIST WHERE USER='root' AND ID<>CONNECTION_ID()")" = 0
if [ -f "$directory/exit" ] && grep -E "^$nonce:done:[0-9]+$" "$directory/exit" >/dev/null; then
    cat "$directory/exit"
else
    printf '%s:cancelled:1\n' "$nonce"
fi
'''

def native_capture_status(native):
    nonce=native['nonce']
    if not re.fullmatch(r'[a-f0-9]{32}',nonce) or not re.fullmatch(r'[a-f0-9]{64}',native.get('operation','')):
        raise ValueError('invalid native attempt identity')
    result=subprocess.run(['docker','exec',native['source'],'sh','-ec',NATIVE_INSPECT_SCRIPT,
        'capture-inspect',native['operation'],nonce],capture_output=True,text=True,timeout=15)
    if result.returncode or not re.fullmatch(re.escape(nonce)+r':(?:refused|done|cancelled):[0-9]+\n?',result.stdout):
        raise ValueError('native capture completion remains unknown')
    return result.stdout.strip().split(':')[1:]

class NativeCaptureFinishedError(ValueError):
    pass

def digest_json(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def published_source(spec):
    namespace=spec['namespace']
    if not re.fullmatch(r'lp-prod-recipient-[a-f0-9]{32}',namespace):
        raise ValueError('invalid namespace')
    if not re.fullmatch(re.escape(namespace)+r'(?:-[a-z0-9]{6})?',spec['project']):
        raise ValueError('invalid project')
    if spec['volumes']!={'database':namespace+'-db','config':namespace+'-config'}:
        raise ValueError('invalid storage')
    paths={}
    volume_rows={}
    for role,name in spec['volumes'].items():
        rows=json.loads(docker('volume','inspect',name))
        if len(rows)!=1:
            raise ValueError('ambiguous storage')
        row=rows[0]
        if (row.get('Name')!=name or row.get('Driver')!='local' or row.get('Options') not in (None,{})
            or row.get('Labels',{}).get('io.launchplane.production-recipient')!=spec['binding']
            or row.get('Labels',{}).get('io.launchplane.resource-role')!=role):
            raise ValueError('foreign storage')
        path=Path(row['Mountpoint'])
        if not path.is_absolute() or path.is_symlink():
            raise ValueError('invalid backing')
        resolved=path.resolve(strict=True)
        if resolved!=path or path.name!='_data' or path.parent.name!=name or not path.is_dir():
            raise ValueError('invalid backing')
        paths[role]=path
        volume_rows[role]=[name,str(path),path.stat().st_dev,path.stat().st_ino]
    if paths['database']==paths['config']:
        raise ValueError('aliased storage')
    services={}
    source_rows=[]
    for identifier in docker('ps','-aq').splitlines():
        inspected=json.loads(docker('inspect',identifier))
        if len(inspected)!=1:
            raise ValueError('incomplete container inventory')
        row=inspected[0]
        labels=row.get('Config',{}).get('Labels') or {}
        ours=labels.get('com.docker.compose.project')==spec['project']
        service=labels.get('com.docker.compose.service')
        if ours:
            if service not in (spec['database_service'],spec['application_service']) or service in services:
                raise ValueError('unqualified serving containers')
            expected=spec['database_image'] if service==spec['database_service'] else spec['application_image']
            if row['Config'].get('Image')!=expected or row.get('State',{}).get('Running') is not True:
                raise ValueError('serving image or running state changed')
            if not row.get('Id') or not row.get('Image'):
                raise ValueError('missing immutable container identity')
            services[service]=row['Id']
            source_rows.append({key:row[key] for key in ('Id','Image','Mounts')}|{'service':service,'image':expected})
        found=set()
        for mount in row.get('Mounts',[]):
            source=mount.get('Source')
            if not source:
                continue
            source=Path(source).resolve()
            for role,path in paths.items():
                if source==path or path in source.parents or source in path.parents:
                    owner=spec['database_service'] if role=='database' else spec['application_service']
                    if (not ours or service!=owner or mount.get('Type')!='volume'
                        or mount.get('Name')!=spec['volumes'][role] or source!=path
                        or mount.get('RW') is not (not spec['mounts'][role]['read_only'])
                        or mount.get('Destination')!=spec['mounts'][role]['destination']):
                        raise ValueError('storage has another consumer')
                    found.add(role)
        if ours and found!=({'database'} if service==spec['database_service'] else {'config'}):
            raise ValueError('serving container lacks exact storage')
    if set(services)!={spec['database_service'],spec['application_service']}:
        raise ValueError('serving containers unavailable')
    identity=digest_json({'volumes':volume_rows,'containers':sorted(source_rows,key=lambda r:r['Id'])})
    return paths,services[spec['database_service']],identity

def read_native_ready(process,nonce):
    if process.stderr is None or process.stdin is None:
        raise ValueError('private native handshake unavailable')
    deadline=time.monotonic()+15
    value=b''
    with selectors.DefaultSelector() as selector:
        selector.register(process.stderr,selectors.EVENT_READ)
        while b'\n' not in value:
            remaining=deadline-time.monotonic()
            if remaining<=0 or not selector.select(remaining):
                raise ValueError('native readiness unverified')
            part=os.read(process.stderr.fileno(),4096)
            if not part or len(value)+len(part)>4096:
                raise ValueError('native readiness stream invalid')
            value+=part
    if value!= (nonce+':READY\n').encode():
        raise ValueError('native readiness identity changed')
def await_native_ready(process,nonce):
    read_native_ready(process,nonce)
    # This is the only GO writer. The caller still holds the original host
    # attempt lock; nothing was buffered before exact native readiness.
    process.stdin.write((nonce+':GO\n').encode())
    process.stdin.flush()
    process.stdin.close()
    process.stdin=None

def logical_dump(identifier,path,*,source,native=None):
    command=(['docker','exec','-i',identifier,'sh','-ec',NATIVE_CAPTURE_SCRIPT,'capture',native['operation'],native['nonce'],'110','90',*DUMP_ARGUMENTS]
        if source else ['docker','exec',identifier,'timeout','-s','KILL','90',*DUMP_ARGUMENTS])
    try:
        with path.open('xb') as output:
            if source:
                process=subprocess.Popen(command,stdout=output,stdin=subprocess.PIPE,stderr=subprocess.PIPE,
                    preexec_fn=lambda:resource.setrlimit(resource.RLIMIT_FSIZE,(2*1024**3,2*1024**3)))
                try:
                    await_native_ready(process,native['nonce'])
                    process.wait(timeout=120)
                finally:
                    if process.stdin is not None and not process.stdin.closed:
                        process.stdin.close()
                    if process.stderr is not None:
                        process.stderr.close()
            else:
                process=subprocess.run(command,stdout=output,stderr=subprocess.DEVNULL,timeout=120,
                    preexec_fn=lambda:resource.setrlimit(resource.RLIMIT_FSIZE,(2*1024**3,2*1024**3)))
            output.flush()
            os.fsync(output.fileno())
        if source and native_capture_status(native)!=['done','0']:
            raise ValueError('native capture failed or was cancelled')
        if process.returncode or not 0<path.stat().st_size<=2*1024**3:
            raise ValueError('logical capture unverified')
    except Exception as error:
        if source:
            try:
                native_capture_status(native)
            except Exception:
                raise error
            raise NativeCaptureFinishedError('native capture ended without qualification') from error
        raise

def populated_rows(identifier):
    names=docker('exec',identifier,'mariadb','--no-defaults','--protocol=socket','--user=root',
        '--batch','--skip-column-names','--execute='
        "SELECT HEX(TABLE_SCHEMA),HEX(TABLE_NAME) FROM information_schema.TABLES "
        "WHERE TABLE_TYPE='BASE TABLE' AND TABLE_SCHEMA NOT IN ('mysql','sys','information_schema','performance_schema') "
        'ORDER BY TABLE_SCHEMA,TABLE_NAME').splitlines()
    if not names or len(names)>10000:
        raise ValueError('business tables unavailable')
    count=0
    for line in names:
        schema,table=(bytes.fromhex(item).decode() for item in line.split('\t'))
        qualified='`'+schema.replace('`','``')+'`.`'+table.replace('`','``')+'`'
        value=docker('exec',identifier,'mariadb','--no-defaults','--protocol=socket','--user=root',
            '--batch','--skip-column-names','--execute=SELECT COUNT(*) FROM '+qualified).strip()
        if not value.isdecimal():
            raise ValueError('business data unverified')
        count+=int(value)
    if count<=0:
        raise ValueError('empty substituted business data')

def validate_published_identity(value,spec):
    if (value.get('binding')!=spec['recovery_binding']
        or value.get('operation')!=spec['operation']):
        raise ValueError('published attempt ownership changed')

def emit_published(receipt):
    print('recipient_recovery_binding='+receipt['binding'])
    print('recipient_recovery_operation='+receipt['operation'])
    if receipt['state'] in ('failed_safe','failed_retained'):
        print('recipient_recovery_retry_safe=true')
        if receipt.get('retained'):
            print('recipient_recovery_retained='+json.dumps(receipt['retained'],sort_keys=True).encode().hex())
        return
    if (receipt['state']!='passed'
        or not all(re.fullmatch(r'[a-f0-9]{64}',receipt.get(k,''))
            for k in ('capture_sha256','source_identity_sha256','logical_data_sha256'))
        or type(receipt.get('started')) is not int or receipt['started']<=0
        or type(receipt.get('epoch')) is not int or receipt['epoch']<receipt['started']
        or type(receipt.get('duration_ms')) is not int or not 0<receipt['duration_ms']<=120000
        or receipt.get('empty') is not False):
        raise ValueError('unqualified published witness')
    print('recipient_recovery_ok=true')
    print('recipient_recovery_empty=false')
    for marker,key in (('capture','capture_sha256'),('source','source_identity_sha256'),
        ('data','logical_data_sha256'),('started','started'),('duration_ms','duration_ms'),('epoch','epoch')):
        print('recipient_recovery_'+marker+'='+str(receipt[key]))

def remove_attempt_files(attempt):
    for name in ('database.sql','roundtrip.sql','capture.tar'):
        (attempt/name).unlink(missing_ok=True)

def finish_published(destination,receipt):
    # The value-only immutable witness is durable before any artifact removal.
    terminal=destination/'receipt.json'
    if terminal.is_symlink():
        raise ValueError('invalid terminal witness')
    if terminal.exists():
        if json.loads(terminal.read_text())!=receipt:
            raise ValueError('terminal published witness changed')
    else:
        temporary=destination/('terminal-'+uuid.uuid4().hex+'.tmp')
        with temporary.open('x') as output:
            json.dump(receipt,output)
            output.flush()
            os.fsync(output.fileno())
        # Never publish a partially written terminal receipt after parent death.
        os.link(temporary,terminal)
        temporary.unlink()
    write_outcome_pointer(destination,receipt)
    if receipt.get('attempt') and receipt['attempt']!='attempt-'+receipt['operation'][:32]:
        raise ValueError('terminal attempt path changed')
    if receipt.get('attempt') and receipt['state'] in ('passed','failed_safe'):
        attempt=destination/receipt['attempt']
        remove_attempt_files(attempt)
        if receipt['state']=='failed_safe' and (attempt/'restore').exists():
            if (attempt/'restore').is_symlink():
                raise ValueError('foreign restore custody')
            shutil.rmtree(attempt/'restore')
    return receipt

def scratch_create_arguments(journal,spec):
    if not re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}',journal['database_image']):
        raise ValueError('immutable scratch image required')
    return ['create','--name',journal['container'],'--network','none','--restart','no',
        '--label','io.launchplane.recipient-recovery='+spec['recovery_binding'],
        '--label','io.launchplane.recipient-recovery-operation='+spec['operation'],
        '--memory','2g','--cpus','1','--security-opt','no-new-privileges',
        '--mount','type=bind,src='+str(Path(journal['scratch'])/'database')+',dst=/var/lib/mysql',
        '--entrypoint','sh',journal['database_image'],'-ec',
        'chown mysql:mysql /var/lib/mysql; '
        'mariadb-install-db --no-defaults --user=mysql --datadir=/var/lib/mysql --skip-test-db >/dev/null 2>&1; '
        'exec mariadbd --no-defaults --datadir=/var/lib/mysql --user=mysql --skip-networking '
        '--skip-grant-tables --skip-slave-start --event-scheduler=OFF --skip-log-bin']

def scratch_isolation(journal,spec):
    scratch=Path(journal['scratch'])
    if scratch.is_symlink() or not scratch.is_dir():
        raise ValueError('scratch custody changed')
    observed=json.loads(docker('inspect',journal['container']))
    if len(observed)!=1:
        raise ValueError('scratch identity uncertain')
    row=observed[0]
    if (not re.fullmatch(r'[a-f0-9]{64}',row.get('Id',''))
        or row['Config']['Labels'].get('io.launchplane.recipient-recovery')!=spec['recovery_binding']
        or row['Config']['Labels'].get('io.launchplane.recipient-recovery-operation')!=spec['operation']
        or row['Config'].get('Image')!=journal['database_image']
        or row['HostConfig']['NetworkMode']!='none' or row['HostConfig'].get('PortBindings')
        or row['HostConfig'].get('RestartPolicy',{}).get('Name')!='no'
        or not journal.get('isolated') and row.get('State',{}).get('Running') is not False
        or len(row.get('Mounts',[]))!=1
        or row['Mounts'][0].get('Type')!='bind'
        or Path(row['Mounts'][0].get('Source','')).resolve()!=(scratch/'database').resolve()
        or row['Mounts'][0].get('Destination')!='/var/lib/mysql'
        or row['Config'].get('Env')!=json.loads(docker('image','inspect',journal['database_image']))[0]['Config'].get('Env')):
        raise ValueError('scratch isolation unverified')
    return {'container':journal['container'],'id':row['Id'],'path':str(scratch)}

def settle_published(destination,spec):
    terminal=destination/'receipt.json'
    pointer=destination/'outcome.json'
    if terminal.is_symlink() or pointer.is_symlink():
        raise ValueError('invalid published journal')
    if terminal.exists():
        receipt=json.loads(terminal.read_text())
        validate_published_identity(receipt,spec)
        return finish_published(destination,receipt)
    if not pointer.exists():
        # Holding the same attempt lock fences a queued worker even when it
        # never wrote a pointer. No source effects precede that durable pointer.
        return finish_published(destination,{'state':'failed_safe',
            'binding':spec['recovery_binding'],'operation':spec['operation']})
    journal=json.loads(pointer.read_text())
    validate_published_identity(journal,spec)
    if journal.get('state')!='running':
        raise ValueError('nonterminal published journal changed')
    if journal.get('attempt')!='attempt-'+spec['operation'][:32]:
        raise ValueError('original attempt path changed')
    phase=journal.get('phase')
    if phase in ('create_pending','scratch') and (journal.get('container')!='lp-recovery-'+spec['operation'][:32]
        or journal.get('scratch')!=str(destination/journal['attempt']/'restore')):
        raise ValueError('original scratch placement changed')
    if phase=='capture':
        native_capture_status(journal['native'])
    elif phase not in ('preflight','source_closed','create_pending','scratch'):
        raise ValueError('source closure remains uncertain')
    if phase in ('source_closed','create_pending','scratch') and journal.get('source_closed') is not True:
        raise ValueError('positive source closure missing')
    retained=None
    if phase in ('create_pending','scratch'):
        if journal.get('create_arguments')!=scratch_create_arguments(journal,spec):
            raise ValueError('source closure or fixed scratch create unproved')
        if phase=='create_pending':
            if journal.get('isolated'):
                raise ValueError('create-only phase regressed')
            # Docker CREATE cannot start code. A late create remains stopped;
            # START is a separate ID-only call after durable isolation below.
            retained={'container':journal['container'],'id':'','path':journal['scratch'],
                'disposition':'stopped_create_unconfirmed'}
        else:
            isolated=journal.get('isolated',{})
            if (set(isolated)!={'container','id','path'}
                or isolated['container']!=journal['container']
                or isolated['path']!=journal['scratch']
                or not re.fullmatch(r'[a-f0-9]{64}',isolated['id'])):
                raise ValueError('durable pre-START isolation missing')
            retained={**isolated,'disposition':'retained_or_cleanup_unconfirmed'}
    receipt={'state':'failed_retained' if retained else 'failed_safe',
        'binding':spec['recovery_binding'],'operation':spec['operation']}
    if retained:
        receipt['retained']=retained
    else:
        receipt['attempt']=journal['attempt']
    return finish_published(destination,receipt)

def published_recovery(spec,*,base=None):
    if (not re.fullmatch(r'[a-f0-9]{64}',spec.get('operation',''))
        or not re.fullmatch(r'[a-f0-9]{64}',spec.get('recovery_binding',''))):
        raise ValueError('original operation identity required')
    with recovery_directory(spec,base) as destination:
        if spec.get('inspect') or (destination/'outcome.json').exists() or (destination/'receipt.json').exists():
            emit_published(settle_published(destination,spec))
            return
        # Exactly one original attempt; no retry loop or second attempt directory.
        attempt=destination/('attempt-'+spec['operation'][:32])
        journal={'state':'running','binding':spec['recovery_binding'],
            'operation':spec['operation'],'attempt':attempt.name,'phase':'preflight'}
        write_outcome_pointer(destination,journal)
        attempt.mkdir(mode=0o700)
        container='lp-recovery-'+spec['operation'][:32]
        try:
            paths,source,identity=published_source(spec)
            docker('exec',source,'sh','-ec','command -v flock >/dev/null; command -v timeout >/dev/null; command -v sync >/dev/null; command -v stat >/dev/null; command -v mariadb >/dev/null; command -v mariadb-dump >/dev/null')
            config_manifest=manifest(paths['config'])
            backing_size=sum(p.stat().st_size for p in paths['database'].rglob('*') if p.is_file())
            config_size=sum(p.stat().st_size for p in paths['config'].rglob('*') if p.is_file())
            if backing_size>2*1024**3 or shutil.disk_usage(destination).free<10*1024**3+3*backing_size+2*config_size:
                raise ValueError('insufficient independent recovery space')
            started=int(time.time()*1000)
            clock=time.monotonic()
            dump=attempt/'database.sql'
            native={'nonce':uuid.uuid4().hex,'source':source,'operation':spec['operation']}
            journal.update(phase='capture',native=native)
            write_outcome_pointer(destination,journal)
            logical_dump(source,dump,source=True,native=native)
            duration=max(1,int((time.monotonic()-clock)*1000))
            if duration>120000 or published_source(spec)[2]!=identity:
                raise ValueError('capture window or serving identity changed')
            data_digest=file_digest(dump)
            archive=attempt/'capture.tar'
            with tarfile.open(archive,'x') as output:
                output.add(dump,arcname='database.sql')
                output.add(paths['config'],arcname='config',recursive=True)
            if manifest(paths['config'])!=config_manifest:
                raise ValueError('config changed during capture')
            archive_digest=file_digest(archive)
            with archive.open('rb') as captured:
                os.fsync(captured.fileno())
            parent=os.open(attempt,os.O_RDONLY|os.O_DIRECTORY)
            try:
                os.fsync(parent)
            finally:
                os.close(parent)
            # READY/GO and nonce cancellation fence native SQL launchers before
            # this source-closed boundary, including across container restart.
            if native_capture_status(native)!=['done','0']:
                raise ValueError('native source closure unverified')
            journal.update(phase='source_closed',source_closed=True,capture_sha256=archive_digest)
            write_outcome_pointer(destination,journal)
            scratch=attempt/'restore'
            scratch.mkdir(mode=0o700)
            with tarfile.open(archive,'r') as captured:
                captured.extractall(scratch,filter='fully_trusted')
            if file_digest(scratch/'database.sql')!=data_digest or manifest(scratch/'config')!=config_manifest:
                raise ValueError('restored capture differs')
            (scratch/'database').mkdir()
            # Persist dispatch intent BEFORE create. No object after a lost
            # create reply is not evidence of absence or isolation.
            journal.update(phase='create_pending',scratch=str(scratch),container=container,
                database_image=spec['database_image'])
            journal['create_arguments']=scratch_create_arguments(journal,spec)
            write_outcome_pointer(destination,journal)
            identifier=docker(*journal['create_arguments']).strip()
            isolated=scratch_isolation(journal,spec)
            if isolated['id']!=identifier:
                raise ValueError('create response identity changed')
            journal.update(phase='scratch',isolated=isolated)
            write_outcome_pointer(destination,journal)
            docker('start',identifier)
            deadline=time.monotonic()+90
            while True:
                status=subprocess.run(['docker','exec',identifier,'mariadb','--no-defaults',
                    '--protocol=socket','--user=root','--execute=SELECT 1'],capture_output=True,timeout=5)
                if status.returncode==0:
                    break
                if time.monotonic()>=deadline:
                    raise ValueError('isolated database unavailable')
                time.sleep(.25)
            with (scratch/'database.sql').open('rb') as captured:
                restored=subprocess.run(['docker','exec','-i',identifier,'timeout','-s','KILL','110',
                    'mariadb','--no-defaults','--protocol=socket','--user=root','--init-command=SET SESSION autocommit=0'],stdin=captured,
                    stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
            if restored.returncode:
                raise ValueError('isolated restore failed')
            populated_rows(identifier)
            if docker('exec',identifier,'mariadb-check','--no-defaults','--protocol=socket',
                '--user=root','--all-databases','--check','--silent').strip():
                raise ValueError('isolated integrity unverified')
            roundtrip=attempt/'roundtrip.sql'
            logical_dump(identifier,roundtrip,source=False)
            if file_digest(roundtrip)!=data_digest or file_digest(archive)!=archive_digest:
                raise ValueError('recovered logical data differs from locked capture')
            if scratch_isolation(journal,spec)!=isolated:
                raise ValueError('scratch changed during verification')
            receipt={'state':'passed','binding':spec['recovery_binding'],'operation':spec['operation'],
                'attempt':attempt.name,'capture_sha256':archive_digest,
                'source_identity_sha256':identity,'logical_data_sha256':data_digest,
                'started':started,'duration_ms':duration,'empty':False,'epoch':int(time.time()*1000)}
            # Durable success does not depend on retaining a second backup.
            # Keep isolation custody until cleanup, so a crash can settle safely.
            docker('rm','-f',identifier)
            shutil.rmtree(scratch)
            finish_published(destination,receipt)
            emit_published(receipt)
        except Exception:
            # The same original lock is held here. Settlement never starts work,
            # and may preserve unknown rather than disguise a failed source fence.
            try:
                settle_published(destination,spec)
            except Exception:
                pass
            raise

"""

PUBLISHED_RECOVERY_PROGRAM = (
    RESOURCE_LIBRARY
    + RECOVERY_LIBRARY
    + PUBLISHED_RECOVERY_LIBRARY
    + r"""
try:
    spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
    if not re.fullmatch(r'[a-f0-9]{64}',spec['recovery_binding']) or not re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}',spec['database_image']):
        raise ValueError('invalid published recovery identity')
    published_recovery(spec)
except Exception:
    print('recipient_recovery_ok=false')
    sys.exit(1)
"""
)

PUBLISHED_RECOVERY_INSPECT_PROGRAM = (
    RESOURCE_LIBRARY
    + RECOVERY_LIBRARY
    + PUBLISHED_RECOVERY_LIBRARY
    + r"""
try:
    spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
    if set(spec)!={'namespace','binding','recovery_binding','operation','inspect'} or spec['inspect'] is not True:
        raise ValueError('only terminal recovery inspection is allowed')
    published_recovery(spec)
except Exception:
    print('recipient_recovery_ok=false')
    sys.exit(1)
"""
)
