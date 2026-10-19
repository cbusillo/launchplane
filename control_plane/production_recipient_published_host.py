"""Fixed logical capture of a serving MariaDB; only independent scratch is restored."""

from control_plane.production_recipient_host import RECOVERY_LIBRARY
from control_plane.production_recipient_resources import RESOURCE_LIBRARY


PUBLISHED_RECOVERY_LIBRARY = r"""
import resource

# Same-image canonical roundtrip checks the real captured SQL artifact, not a
# caller's row count. System/auth databases are initialized only in scratch.
DUMP_ARGUMENTS = [
    'mariadb-dump','--no-defaults','--protocol=socket','--user=root',
    '--all-databases','--ignore-database=mysql','--ignore-database=sys',
    '--ignore-database=information_schema','--ignore-database=performance_schema',
    '--lock-all-tables','--quick','--hex-blob','--routines','--events','--triggers',
    '--skip-comments','--skip-dump-date','--skip-add-locks','--skip-disable-keys',
    '--skip-extended-insert','--order-by-primary',
]

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

def logical_dump(identifier,path,*,source):
    # timeout is INSIDE the database container: killing a transport cannot leave
    # mariadb-dump's global read lock alive indefinitely. Its connection owns
    # the lock and is killed after at most 110 seconds including acquisition.
    auth=(
        'test -n "${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}" || '
        'test "${MARIADB_ALLOW_EMPTY_ROOT_PASSWORD:-${MYSQL_ALLOW_EMPTY_PASSWORD:-}}" = 1; '
        'export MYSQL_PWD="${MARIADB_ROOT_PASSWORD:-${MYSQL_ROOT_PASSWORD:-}}"; '
    ) if source else ''
    command=['docker','exec',identifier,'timeout','-s','KILL','110','sh','-ec',auth+'exec "$@"','dump',*DUMP_ARGUMENTS]
    with path.open('xb') as output:
        process=subprocess.run(command,stdout=output,stderr=subprocess.DEVNULL,timeout=120,
            preexec_fn=lambda:resource.setrlimit(resource.RLIMIT_FSIZE,(2*1024**3,2*1024**3)))
        output.flush()
        os.fsync(output.fileno())
    if process.returncode or not 0<path.stat().st_size<=2*1024**3:
        raise ValueError('logical capture unverified')

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

def emit_published(receipt):
    if receipt['state']=='passed':
        if (not all(re.fullmatch(r'[a-f0-9]{64}',receipt.get(k,'')) for k in ('source_identity_sha256','logical_data_sha256'))
            or type(receipt.get('started')) is not int or receipt['started']<=0
            or type(receipt.get('duration_ms')) is not int or not 0<receipt['duration_ms']<=120000
            or receipt.get('empty') is not False):
            raise ValueError('unqualified published witness')
    emit_outcome(receipt)
    if receipt['state']=='passed':
        print('recipient_recovery_source='+receipt['source_identity_sha256'])
        print('recipient_recovery_data='+receipt['logical_data_sha256'])
        print('recipient_recovery_started='+str(receipt['started']))
        print('recipient_recovery_duration_ms='+str(receipt['duration_ms']))

def published_recovery(spec,*,base=None):
    with recovery_directory(spec,base) as destination:
        previous=load_outcome(destination,spec)
        if previous and (spec.get('inspect') or previous['state']=='passed'):
            emit_published(previous)
            return
        if spec.get('inspect') or previous is None and any(p.name!='.lock' for p in destination.iterdir()):
            raise ValueError('no terminal published witness')
        paths,source,identity=published_source(spec)
        config_manifest=manifest(paths['config'])
        backing_size=sum(p.stat().st_size for p in paths['database'].rglob('*') if p.is_file())
        config_size=sum(p.stat().st_size for p in paths['config'].rglob('*') if p.is_file())
        if backing_size>2*1024**3 or shutil.disk_usage(destination).free<10*1024**3+3*backing_size+2*config_size:
            raise ValueError('insufficient independent recovery space')
        attempt=Path(tempfile.mkdtemp(prefix='attempt-',dir=destination))
        write_outcome_pointer(destination,{'state':'running','binding':spec['recovery_binding']})
        container='lp-recovery-'+uuid.uuid4().hex
        cleanup_verified=True
        scratch=None
        try:
            started=int(time.time()*1000)
            clock=time.monotonic()
            # Until this completes, no terminal absence/retry witness is safe:
            # a broken Docker connection can leave the bounded native dump alive.
            cleanup_verified=False
            dump=attempt/'database.sql'
            logical_dump(source,dump,source=True)
            duration=max(1,int((time.monotonic()-clock)*1000))
            if duration>120000 or published_source(spec)[2]!=identity:
                raise ValueError('capture window or serving identity changed')
            cleanup_verified=True
            data_digest=file_digest(dump)
            archive=attempt/'capture.tar'
            with tarfile.open(archive,'x') as output:
                output.add(dump,arcname='database.sql')
                output.add(paths['config'],arcname='config',recursive=True)
            if manifest(paths['config'])!=config_manifest:
                raise ValueError('config changed during capture')
            archive_digest=file_digest(archive)
            scratch=Path(tempfile.mkdtemp(prefix='restore-',dir=attempt))
            with tarfile.open(archive,'r') as captured:
                captured.extractall(scratch,filter='fully_trusted')
            if file_digest(scratch/'database.sql')!=data_digest or manifest(scratch/'config')!=config_manifest:
                raise ValueError('restored capture differs')
            (scratch/'database').mkdir()
            cleanup_verified=False
            identifier=docker('create','--name',container,'--network','none',
                '--label','io.launchplane.recipient-recovery='+spec['recovery_binding'],
                '--memory','2g','--cpus','1','--security-opt','no-new-privileges',
                '--mount','type=bind,src='+str(scratch/'database')+',dst=/var/lib/mysql',
                '--entrypoint','sh',spec['database_image'],'-ec',
                'chown mysql:mysql /var/lib/mysql; '
                'mariadb-install-db --no-defaults --user=mysql --datadir=/var/lib/mysql --skip-test-db >/dev/null 2>&1; '
                'exec mariadbd --no-defaults --datadir=/var/lib/mysql --user=mysql --skip-networking '
                '--skip-grant-tables --skip-slave-start --event-scheduler=OFF --skip-log-bin').strip()
            try:
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
                        'mariadb','--no-defaults','--protocol=socket','--user=root'],stdin=captured,
                        stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=120)
                if restored.returncode:
                    raise ValueError('isolated restore failed')
                populated_rows(identifier)
                if docker('exec',identifier,'mariadb-check','--no-defaults','--protocol=socket',
                    '--user=root','--all-databases','--check','--silent').strip():
                    raise ValueError('isolated integrity unverified')
                roundtrip=attempt/'roundtrip.sql'
                logical_dump(identifier,roundtrip,source=False)
                if file_digest(roundtrip)!=data_digest:
                    raise ValueError('recovered logical data differs from locked capture')
                observed=json.loads(docker('inspect',identifier))[0]
                if (observed['HostConfig']['NetworkMode']!='none'
                    or observed['HostConfig'].get('PortBindings')
                    or len(observed.get('Mounts',[]))!=1
                    or observed['Mounts'][0].get('Type')!='bind'
                    or Path(observed['Mounts'][0].get('Source','')).resolve()!=(scratch/'database').resolve()
                    or observed['Mounts'][0].get('Destination')!='/var/lib/mysql'
                    or observed['Config'].get('Env')!=json.loads(docker('image','inspect',spec['database_image']))[0]['Config'].get('Env')):
                    raise ValueError('recovery isolation unverified')
                if published_source(spec)[2]!=identity or manifest(paths['config'])!=config_manifest:
                    raise ValueError('serving identity or config changed during recovery')
            finally:
                owned=json.loads(docker('inspect',container))[0]
                if owned['Config']['Labels'].get('io.launchplane.recipient-recovery')!=spec['recovery_binding']:
                    raise ValueError('scratch cleanup ownership uncertain')
                docker('rm','-f',container)
                cleanup_verified=True
            shutil.rmtree(scratch)
            scratch=None
            receipt={'state':'passed','binding':spec['recovery_binding'],
                'capture_sha256':archive_digest,'archive':str(archive.relative_to(destination)),
                'source_identity_sha256':identity,'logical_data_sha256':data_digest,
                'started':started,'duration_ms':duration,'empty':False,'epoch':int(time.time()*1000)}
            save_outcome(destination,attempt,receipt)
            emit_published(receipt)
        except Exception:
            if cleanup_verified:
                if scratch is not None:
                    shutil.rmtree(scratch)
                save_outcome(destination,attempt,{'state':'failed_safe','binding':spec['recovery_binding']})
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
