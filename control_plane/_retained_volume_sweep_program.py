RETAINED_VOLUME_SWEEP_HOST_PROGRAM = r"""
import base64,fcntl,hashlib,json,os,re,stat,subprocess,sys,time
from pathlib import Path

prefix=sys.argv[2]

def emit(status,reason):
    print(prefix+json.dumps({'type':'complete','result':{'status':status,'reason_code':reason}},sort_keys=True,separators=(',',':')),flush=True)

try:
    spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
except BaseException:
    emit('unknown','invalid_request')
    raise SystemExit

deadline=time.monotonic()+75
name=spec.get('volume')
mode=spec.get('mode')
captured=spec.get('identity')
operation=spec.get('operation')
if (mode not in {'dry-run','apply','readback'} or not isinstance(name,str)
    or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,127}',name) is None
    or not isinstance(captured,str) or re.fullmatch(r'[a-f0-9]{64}',captured) is None
    or not isinstance(operation,str) or not operation or len(operation)>512):
    emit('unknown','invalid_request')
    raise SystemExit

class Unknown(Exception):
    pass

class Held(Exception):
    pass

def docker(*arguments,allow_failure=False):
    remaining=deadline-time.monotonic()
    if remaining<=0:
        raise Unknown()
    try:
        result=subprocess.run(['docker',*arguments],capture_output=True,timeout=min(10,remaining),check=False)
    except (OSError,subprocess.SubprocessError):
        raise Unknown() from None
    if len(result.stdout)>8*1024*1024 or len(result.stderr)>1024*1024:
        raise Unknown()
    if result.returncode and not allow_failure:
        raise Unknown()
    return result

def decoded(result):
    try:
        return result.stdout.decode('utf-8',errors='strict')
    except UnicodeError:
        raise Unknown() from None

def identifiers(*arguments):
    result=docker(*arguments)
    values=decoded(result).splitlines()
    if len(values)>10000 or len(values)!=len(set(values)) or any(not value or len(value)>256 for value in values):
        raise Unknown()
    return values

def json_value(*arguments):
    try:
        return json.loads(decoded(docker(*arguments)))
    except (ValueError,TypeError):
        raise Unknown() from None

def one_volume(target):
    value=json_value('volume','inspect',target)
    if not isinstance(value,list) or len(value)!=1 or not isinstance(value[0],dict):
        raise Unknown()
    return value[0]

def engine():
    value=json_value('info','--format','{{json .}}')
    identifier=value.get('ID') if isinstance(value,dict) else None
    root=value.get('DockerRootDir') if isinstance(value,dict) else None
    if not isinstance(identifier,str) or not identifier or not isinstance(root,str) or not root.startswith('/'):
        raise Unknown()
    return identifier,Path(root)

def binding(volume,root):
    if (volume.get('Name')!=name or volume.get('Driver')!='local' or volume.get('Options') not in (None,{})
        or not isinstance(volume.get('CreatedAt'),str) or not volume['CreatedAt']):
        raise Held()
    expected=root/'volumes'/name/'_data'
    mountpoint=volume.get('Mountpoint')
    if not isinstance(mountpoint,str) or Path(mountpoint)!=expected:
        raise Held()
    try:
        metadata=os.lstat(expected)
        resolved=expected.resolve(strict=True)
        resolved_metadata=os.stat(resolved)
    except OSError:
        raise Held() from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(resolved_metadata.st_mode) or resolved!=expected:
        raise Held()
    return resolved,resolved_metadata

def identity(volume,engine_id,metadata):
    value={'volume':volume,'engine_id':engine_id,'backing_device':metadata.st_dev,'backing_inode':metadata.st_ino}
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()

def path_overlap(first,second):
    return first==second or first in second.parents or second in first.parents

def mounts_overlap(backing):
    try:
        lines=Path('/proc/self/mountinfo').read_text().splitlines()
    except OSError:
        raise Unknown() from None
    if len(lines)>100000:
        raise Unknown()
    for line in lines:
        fields=line.split()
        if len(fields)<10 or '-' not in fields:
            raise Unknown()
        raw=fields[4]
        for encoded,plain in (('\\040',' '),('\\011','\t'),('\\012','\n'),('\\134','\\')):
            raw=raw.replace(encoded,plain)
        try:
            mounted=Path(raw).resolve(strict=False)
        except (OSError,RuntimeError):
            raise Unknown() from None
        if mounted==backing or mounted.is_relative_to(backing):
            raise Held()
        try:
            mounted_stat=mounted.stat()
            backing_stat=backing.stat()
        except OSError:
            raise Unknown() from None
        if (mounted_stat.st_dev,mounted_stat.st_ino)==(backing_stat.st_dev,backing_stat.st_ino):
            raise Held()
        source_root=Path(fields[3])
        if source_root!=Path('/') and path_overlap(source_root,backing):
            raise Held()

def mount_reference(mount,backing):
    if not isinstance(mount,dict):
        raise Unknown()
    kind=mount.get('Type')
    source=mount.get('Name') if kind=='volume' and mount.get('Name') else mount.get('Source')
    if kind=='volume' and source==name:
        raise Held()
    if kind=='bind':
        if not isinstance(source,str) or not source.startswith('/'):
            raise Unknown()
        try:
            if path_overlap(Path(source).resolve(strict=False),backing):
                raise Held()
        except (OSError,RuntimeError):
            raise Unknown() from None

def inventory(backing,root):
    containers=identifiers('ps','-aq','--no-trunc')
    for container_id in containers:
        value=json_value('inspect',container_id)
        if not isinstance(value,list) or len(value)!=1 or value[0].get('Id')!=container_id or not isinstance(value[0].get('Mounts'),list):
            raise Unknown()
        for mount in value[0]['Mounts']:
            mount_reference(mount,backing)
    services=identifiers('service','ls','-q','--no-trunc')
    for service_id in services:
        value=json_value('service','inspect',service_id)
        if not isinstance(value,list) or len(value)!=1 or value[0].get('ID')!=service_id:
            raise Unknown()
        try:
            mounts=value[0]['Spec']['TaskTemplate']['ContainerSpec'].get('Mounts') or []
        except (KeyError,TypeError):
            raise Unknown() from None
        if not isinstance(mounts,list):
            raise Unknown()
        for mount in mounts:
            mount_reference(mount,backing)
        tasks=identifiers('service','ps','-q','--no-trunc',service_id)
        for task_id in tasks:
            task=json_value('inspect','--type','task',task_id)
            if not isinstance(task,list) or len(task)!=1 or task[0].get('ID')!=task_id or task[0].get('ServiceID')!=service_id:
                raise Unknown()
            try:
                task_mounts=task[0]['Spec']['ContainerSpec'].get('Mounts') or []
            except (KeyError,TypeError):
                raise Unknown() from None
            if not isinstance(task_mounts,list):
                raise Unknown()
            for mount in task_mounts:
                mount_reference(mount,backing)
    info=json_value('info','--format','{{json .}}')
    swarm=info.get('Swarm') if isinstance(info,dict) else None
    if not isinstance(swarm,dict):
        raise Unknown()
    state=swarm.get('LocalNodeState')
    if state=='active':
        if swarm.get('ControlAvailable') is not True:
            raise Held()
        nodes=identifiers('node','ls','-q','--no-trunc')
        if len(nodes)!=1:
            raise Held()
    else:
        raise Held()
    volumes=identifiers('volume','ls','-q')
    if name not in volumes:
        raise Unknown()
    for other in volumes:
        if other==name:
            continue
        candidate=one_volume(other)
        point=candidate.get('Mountpoint')
        if candidate.get('Name')!=other or not isinstance(point,str) or not point.startswith('/'):
            raise Unknown()
        try:
            other_path=Path(point).resolve(strict=True)
            other_stat=other_path.stat()
        except OSError:
            raise Unknown() from None
        if (other_stat.st_dev,other_stat.st_ino)==(backing.stat().st_dev,backing.stat().st_ino) or path_overlap(other_path,backing):
            raise Held()
    mounts_overlap(backing)

def empty(backing):
    try:
        with os.scandir(backing) as entries:
            if next(entries,None) is not None:
                raise Held()
    except Held:
        raise
    except OSError:
        raise Unknown() from None

def custody():
    directory=Path.home()/'.local'/'state'/'launchplane-retained-volume-sweep'
    current=Path.home()
    for part in directory.relative_to(current).parts:
        current=current/part
        try:
            metadata=os.lstat(current)
        except FileNotFoundError:
            current.mkdir(mode=0o700)
            metadata=os.lstat(current)
        if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode) or metadata.st_uid!=os.getuid():
            raise Unknown()
    key=hashlib.sha256((operation+'\0'+captured).encode()).hexdigest()
    return directory,directory/(key+'.json')

def read_receipt(path):
    try:
        metadata=os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=os.getuid() or metadata.st_size>4096:
        raise Unknown()
    try:
        value=json.loads(path.read_text())
    except (OSError,ValueError):
        raise Unknown() from None
    if (not isinstance(value,dict) or set(value)!={'operation','identity','volume','started','engine_id'}
        or value['operation']!=operation or value['identity']!=captured or value['volume']!=name
        or value['started'] is not True or not isinstance(value['engine_id'],str) or not value['engine_id']):
        raise Unknown()
    return value

def persist(path,value):
    temporary=path.with_suffix('.tmp-'+str(os.getpid()))
    descriptor=os.open(temporary,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600)
    with os.fdopen(descriptor,'w') as handle:
        json.dump(value,handle,sort_keys=True,separators=(',',':'))
        handle.flush();os.fsync(handle.fileno())
    os.replace(temporary,path)
    directory=os.open(path.parent,os.O_RDONLY|os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)

def absence(engine_id):
    current_id,_=engine()
    if current_id!=engine_id:
        raise Unknown()
    names=identifiers('volume','ls','-q')
    if name in names:
        return False
    return True

try:
    directory,receipt_path=custody()
    lock_descriptor=os.open(directory/'lock',os.O_RDWR|os.O_CREAT|os.O_NOFOLLOW,0o600)
    with os.fdopen(lock_descriptor,'r+') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        receipt=read_receipt(receipt_path)
        engine_id,root=engine()
        if receipt is not None:
            if receipt['engine_id']!=engine_id:
                raise Unknown()
            if absence(engine_id):
                emit('absent','removed')
            else:
                emit('unknown','effect_unknown')
            raise SystemExit
        if mode=='readback':
            emit('held','present' if not absence(engine_id) else 'never_started')
            raise SystemExit
        volume=one_volume(name)
        backing,metadata=binding(volume,root)
        if identity(volume,engine_id,metadata)!=captured:
            raise Held()
        inventory(backing,root)
        empty(backing)
        if mode=='dry-run':
            emit('eligible','qualified')
            raise SystemExit
        volume=one_volume(name)
        backing,metadata=binding(volume,root)
        if identity(volume,engine_id,metadata)!=captured:
            raise Held()
        inventory(backing,root)
        empty(backing)
        persist(receipt_path,{'operation':operation,'identity':captured,'volume':name,'started':True,'engine_id':engine_id})
        result=docker('volume','rm',name,allow_failure=True)
        if result.returncode==0 and absence(engine_id):
            emit('removed','removed')
        else:
            emit('unknown','effect_unknown')
except Held:
    emit('held','safety_check')
except Unknown:
    emit('unknown','inventory_unavailable')
except SystemExit:
    raise
except BaseException:
    emit('unknown','inventory_unavailable')
"""
