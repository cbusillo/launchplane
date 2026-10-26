RESTORE_STATE_PROGRAM = r"""
import hashlib,json,os,re,subprocess,sys,tempfile
from pathlib import Path
spec=json.load(sys.stdin)
root=Path(spec['authority_directory'])
if not root.is_absolute() or root.resolve()!=root or not root.is_dir() or not re.fullmatch('[A-Za-z0-9_-]{1,160}',spec['record']):
    raise ValueError('restore requires its original canonical host authority directory')
binding={key:spec[key] for key in ('record','image','database','database_uuid','database_dump_sha256','filestore_sha256','capture_nonce')}
request_digest=hashlib.sha256(json.dumps(binding,sort_keys=True,separators=(',',':')).encode()).hexdigest()
def receipt_path(kind):
    path=root/(spec['record']+'-restore-'+kind+'.json')
    if path.is_symlink(): raise ValueError('restore receipt cannot be a symlink')
    return path
def read_receipt(kind):
    path=receipt_path(kind)
    if not path.exists(): return None
    value=json.loads(path.read_text())
    if value.get('request')!=request_digest: raise ValueError('restore receipt crossed its captured original request')
    return value
def seal_receipt(kind,**values):
    path=receipt_path(kind);value={'request':request_digest,**values}
    descriptor,temporary=tempfile.mkstemp(dir=root,prefix='.restore-receipt-')
    try:
        with os.fdopen(descriptor,'w') as stream:
            os.fchmod(stream.fileno(),0o600);json.dump(value,stream,sort_keys=True);stream.flush();os.fsync(stream.fileno())
        try: os.link(temporary,path)
        except FileExistsError: return read_receipt(kind)
        descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
        try: os.fsync(descriptor)
        finally: os.close(descriptor)
        return value
    finally:
        os.unlink(temporary)
def docker_read(argv):
    result=subprocess.run(['docker',*argv],capture_output=True,timeout=min(30,spec['timeout_seconds']))
    if result.returncode: raise RuntimeError('original restore observation is unavailable')
    return result.stdout
def restore_outcome():
    completed=read_receipt('completed')
    if completed is not None:
        result=completed.get('result')
        if result not in ({'status':'failed','terminated':True},{'restored_database_uuid':spec['database_uuid'],'database_dump_sha256':spec['database_dump_sha256'],'filestore_archive_sha256':spec['filestore_sha256']}): raise ValueError('restore completion witness is malformed')
        return result
    intent=read_receipt('intent'); allocation=read_receipt('allocation')
    ids=docker_read(['ps','-a','--no-trunc','--filter','label=launchplane.cutover-restore='+spec['record'],'--format','{{.ID}}']).decode().split()
    if len(ids)>1: raise ValueError('original restore allocation is ambiguous')
    if allocation is not None and ids and ids!=[allocation['container']]: raise ValueError('original restore identity changed')
    if ids:
        container=ids[0]
        if not re.fullmatch('[0-9a-f]{64}',container): raise ValueError('restore lacks its full native container identity')
        values=json.loads(docker_read(['inspect',container]))
        if len(values)!=1: raise ValueError('original restore native identity is ambiguous')
        value=values[0];config=value['Config'];host=value['HostConfig'];mounts=value.get('Mounts',[])
        if config['Image']!=spec['image'] or config.get('Labels',{}).get('launchplane.cutover-restore-request')!=request_digest or host.get('NetworkMode')!='none' or host.get('AutoRemove') is not True or host.get('ReadonlyRootfs') is not True or host.get('Privileged') or config.get('Entrypoint')!=['timeout'] or not mounts or any(m.get('Type') not in ('volume','tmpfs') or m.get('Type')=='volume' and (m.get('Destination')!='/var/lib/postgresql/data' or re.fullmatch('[0-9a-f]{64}',m.get('Name','')) is None) for m in mounts):
            raise ValueError('restore allocation is foreign or not disposable')
        if config.get('User')!='postgres' or config.get('Cmd',[None])[0]!='--signal=KILL' or len(config.get('Cmd',[]))!=4 or config['Cmd'][2:]!=['docker-entrypoint.sh','postgres'] or not str(config['Cmd'][1]).isdigit() or not 0<int(config['Cmd'][1])<=spec['timeout_seconds'] or host.get('PortBindings') or host.get('CapAdd') or set(host.get('CapDrop') or [])!={'ALL'} or 'no-new-privileges:true' not in (host.get('SecurityOpt') or []) or len([m for m in mounts if m.get('Type')=='volume'])!=1:
            raise ValueError('restore allocation lacks its bounded isolated lifetime')
        allocation=seal_receipt('allocation',container=container)
        if allocation['container']!=container: raise ValueError('restore allocation receipt differs')
        subprocess.run(['docker','rm','-f','-v',container],capture_output=True,timeout=min(30,spec['timeout_seconds']))
    elif allocation is None:
        if intent is not None: raise RuntimeError('original restore allocation remains uncertain')
        return None
    container=allocation['container']
    if not re.fullmatch('[0-9a-f]{64}',container): raise ValueError('original restore allocation witness is malformed')
    if docker_read(['ps','-a','--no-trunc','--filter','id='+container,'--format','{{.ID}}']).strip():
        raise RuntimeError('original restore termination has not been observed')
    return seal_receipt('completed',result={'status':'failed','terminated':True})['result']
"""

RESTORE_CANCEL_PROGRAM = (
    RESTORE_STATE_PROGRAM
    + r"""
outcome=restore_outcome()
if outcome is None: raise RuntimeError('original restore has no owned allocation or terminal witness')
print(json.dumps({'cancelled':True}))
"""
)
