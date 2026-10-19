"""Fixed metadata reads; no captures, schedules, runtime or custody creation."""

from control_plane._odoo_rehearsal_source_reader import REHEARSAL_SOURCE_READER_LIBRARY

SOURCE_MEASUREMENTS_PROGRAM = r"""
import json, os, stat, subprocess, sys
from pathlib import Path

def measurements():
    query="BEGIN READ ONLY; SET LOCAL statement_timeout='60s'; SELECT json_build_object('database_bytes',pg_database_size(current_database()),'odoo_version',(SELECT latest_version FROM ir_module_module WHERE name='base')); COMMIT;"
    result=subprocess.run(['sudo','-n','-u',sys.argv[1],'--','psql','-X','-qAt','-v','ON_ERROR_STOP=1','-d',sys.argv[2]],
        input=query.encode(),capture_output=True,check=False,timeout=70)
    if result.returncode:
        denied=any(value in result.stderr.lower() for value in
            (b'permission denied',b'authentication failed',b'no password supplied',b'password is required',
             b'not allowed to execute',b'is not in the sudoers file',b'may not run sudo'))
        return {'type':'denied' if denied else 'failed'}
    data=json.loads(result.stdout)
    apparent=allocated=0
    # Match capture's tar -C root while still refusing links inside the tree.
    selected=Path(sys.argv[3]).resolve(strict=True)
    if not selected.is_dir():
        raise ValueError('Source filestore is unverified')
    pending=[selected]
    while pending:
        path=pending.pop()
        metadata=path.lstat()
        if not (stat.S_ISDIR(metadata.st_mode) or stat.S_ISREG(metadata.st_mode)):
            raise ValueError('Unsupported filestore member')
        apparent+=metadata.st_size
        allocated+=metadata.st_blocks*512
        if stat.S_ISDIR(metadata.st_mode):
            with os.scandir(path) as entries:
                pending.extend(Path(entry.path) for entry in entries)
    return {'type':'measurements',**data,'filestore_apparent_bytes':apparent,'filestore_allocated_bytes':allocated}

try:
    print(json.dumps(measurements()),flush=True)
except PermissionError:
    print(json.dumps({'type':'denied'}),flush=True)
except Exception:
    print(json.dumps({'type':'failed'}),flush=True)
"""

REHEARSAL_PREFLIGHT_PROGRAM = (
    REHEARSAL_SOURCE_READER_LIBRARY
    + "\nSOURCE_MEASUREMENTS_PROGRAM = "
    + repr(SOURCE_MEASUREMENTS_PROGRAM)
    + r"""
import base64, hashlib, json, os, re, shlex, socket, subprocess, sys
from datetime import datetime, timezone
from pathlib import Path

def emit(value):
    print(sys.argv[2]+json.dumps(value,sort_keys=True),flush=True)

def checked(arguments, timeout=30):
    result=subprocess.run(arguments,capture_output=True,check=False,timeout=timeout)
    if result.returncode:
        if any(value in result.stderr.lower() for value in
            (b'permission denied',b'authentication failed',b'authorization denied by plugin')):
            raise PermissionError('The existing reader refused the read')
        raise RuntimeError('Preflight read failed')
    return result.stdout

def custody_parent():
    path=Path.home()/'.local/state/launchplane-rehearsals'
    while True:
        if path.is_symlink():
            raise RuntimeError('Custody parent is aliased')
        if path.exists():
            if not path.is_dir() or path.resolve(strict=True)!=path.absolute():
                raise RuntimeError('Custody parent is unverified')
            return path
        path=path.parent

def backing_groups(server_id):
    info=json.loads(checked(['docker','info','--format','{{json .}}']))
    # These are existing allocation parents, not nonexistent rehearsal paths.
    # Only the known local-volume/overlay2 layout can supply this observation.
    if info.get('Driver')!='overlay2':
        raise RuntimeError('Writable-layer backing is unavailable')
    root=Path(info['DockerRootDir'])
    if not root.is_absolute():
        raise RuntimeError('Docker backing is unavailable')
    root=root.resolve(strict=True)
    paths={'database':root/'volumes','filestore':root/'volumes',
        'runner_scratch':root/'overlay2','database_scratch':root/'overlay2',
        'host_custody':custody_parent()}
    host=hashlib.sha256((server_id+'\n'+socket.gethostname()).encode()).hexdigest()
    groups={}
    for role,path in paths.items():
        path=path.resolve(strict=True)
        if not path.is_dir():
            raise RuntimeError('Backing allocation parent is unavailable')
        device=path.stat().st_dev
        value=os.statvfs(path)
        available=value.f_bavail*value.f_frsize
        identifier=hashlib.sha256((host+'\n'+str(device)).encode()).hexdigest()
        group=groups.setdefault(identifier,{'group_id':identifier,'available_bytes':available,'roles':[]})
        group['available_bytes']=min(group['available_bytes'],available)
        group['roles'].append(role)
    return host,list(groups.values())

def preflight(spec):
    host,groups=backing_groups(spec['server_id'])
    values=spec['source']
    ssh,destination,filestore=rehearsal_source_reader(values)
    remote=shlex.join(['python3','-c',SOURCE_MEASUREMENTS_PROGRAM,
        values['ODOO_UPSTREAM_DB_USER'],values['ODOO_UPSTREAM_DB_NAME'],filestore])
    source=json.loads(checked([*ssh,destination,remote],timeout=360))
    if source.get('type')=='denied':
        raise PermissionError('The existing source refused the read')
    if source.pop('type')!='measurements':
        raise RuntimeError('Source measurements are unavailable')
    return {**source,'reader_host_sha256':host,'backing_groups':groups,
        'backing_basis':'allocation-parent','read_only':True,
        'observed_at':datetime.now(timezone.utc).isoformat()}

def main():
    try:
        spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
        emit({'type':'complete','result':preflight(spec)})
    except PermissionError:
        emit({'type':'denied'})
    except Exception:
        emit({'type':'failed'})

main()
"""
)
