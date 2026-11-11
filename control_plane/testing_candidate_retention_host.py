import inspect

from control_plane._odoo_clean_snapshot_program import CLEAN_TESTING_SNAPSHOT_LIBRARY
from control_plane._odoo_testing_storage_program import STORAGE_MAINTENANCE_LIBRARY
from control_plane.retained_volume_capture import (
    _batched_docker_inspect,
    retained_volume_docker_command,
    retained_volume_engine_identity,
    retained_volume_identity,
)


NATIVE_TESTING_RETENTION_INSPECTION = (
    "from collections.abc import Callable,Mapping,Sequence\nfrom typing import Any\n"
    "import base64,hashlib,json,os,re,subprocess,sys,tempfile,time\nfrom pathlib import Path\n"
    + inspect.getsource(_batched_docker_inspect)
    + inspect.getsource(retained_volume_docker_command)
    + inspect.getsource(retained_volume_identity)
    + inspect.getsource(retained_volume_engine_identity)
    + CLEAN_TESTING_SNAPSHOT_LIBRARY
    + STORAGE_MAINTENANCE_LIBRARY
    + r"""
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
prefix=sys.argv[2]
receipt_path=None
effect_started=False

class Refused(ValueError):
    pass

def save_receipt(result):
    if receipt_path is None:
        return
    with tempfile.NamedTemporaryFile(mode='w',dir=receipt_path.parent,delete=False) as output:
        json.dump(result,output,sort_keys=True)
        temporary=Path(output.name)
    os.replace(temporary,receipt_path)

def docker(*arguments):
    result=subprocess.run(retained_volume_docker_command(*arguments),capture_output=True,timeout=60,check=False)
    if result.returncode:
        raise RuntimeError('docker_request_failed')
    return result.stdout.decode('utf-8',errors='strict')

def load(*arguments):
    return json.loads(docker(*arguments))

def identifiers(*arguments):
    return docker(*arguments).splitlines()

def inspect_candidate():
    global receipt_path,effect_started
    resources=spec['resources']
    namespace=resources['namespace']
    if (resources['instance']!='testing' or re.fullmatch(r'lp-testing-[a-f0-9]{32}',namespace) is None
        or spec.get('mode','dry-run') not in {'dry-run','apply','readback'}):
        raise Refused('testing_scope_required')
    digest=hashlib.sha256(json.dumps(resources,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    engine=load('info','--format','{{json .}}')
    engine_id=engine['ID']
    root=Path(engine['DockerRootDir'])
    if not root.is_absolute() or root.resolve(strict=True)!=root:
        raise Refused('production_boundary')
    mode=spec.get('mode','dry-run')
    if mode!='dry-run':
        key=spec.get('operation_key')
        if not isinstance(key,str) or not key:
            raise Refused('release_key_required')
        directory=root/'launchplane-state'/'testing-candidate-release'
        if directory.is_symlink() or directory.parent.is_symlink():
            raise Refused('release_receipt_boundary')
        if mode=='apply':
            directory.mkdir(mode=0o700,parents=True,exist_ok=True)
        if directory.exists():
            if directory.is_symlink() or directory.parent.is_symlink():
                raise Refused('release_receipt_boundary')
            receipt_path=directory/hashlib.sha256((digest+'\n'+key).encode()).hexdigest()
            if receipt_path.exists():
                if receipt_path.is_symlink():
                    raise Refused('release_receipt_boundary')
                saved=json.loads(receipt_path.read_text())
                if saved.get('release_status') in {'held','released'}:
                    return saved
                if mode=='apply':
                    raise RuntimeError('original_release_requires_readback')
            if mode=='apply':
                save_receipt({'release_status':'active'})
    expected={'ODOO_DB_VOLUME':namespace+'-db','ODOO_DATA_VOLUME':namespace+'-data','ODOO_LOG_VOLUME':namespace+'-logs'}
    existing=set(identifiers('volume','ls','-q'))
    volumes=_batched_docker_inspect('volume',sorted(existing & set(expected.values())),load)
    all_containers=_batched_docker_inspect('container',identifiers('ps','-aq','--no-trunc'),load)
    project=re.compile(re.escape(namespace)+r'(?:-[a-z0-9]{6})?')
    own={identifier:row for identifier,row in all_containers.items()
        if project.fullmatch(str((row.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project','')))}
    serving=any(row.get('HostConfig',{}).get('PortBindings') for row in own.values())
    if serving and spec.get('mode')=='apply':
        raise Refused('candidate_release_selected')
    result=[]
    pools=storage_backing_pools()
    for role,name in expected.items():
        volume=volumes.get(name)
        if volume is None:
            result.append({'role':role,'name':name,'status':'absent','reason':'provider_absent'})
            continue
        path=Path(volume['Mountpoint'])
        labels=volume.get('Labels') or {}
        if (volume.get('Name')!=name or volume.get('Driver')!='local' or volume.get('Options') not in (None,{})
            or labels.get('io.launchplane.testing-resources')!=digest
            or labels.get('io.launchplane.testing-resource-role')!=role
            or 'io.launchplane.production-recipient' in labels
            or path!=root/'volumes'/name/'_data' or path.resolve(strict=True)!=path or not path.is_dir()):
            raise Refused('production_boundary')
        metadata=path.stat();space=os.statvfs(path)
        result.append({'role':role,'name':name,'status':'observed','reason':'',
            'identity':retained_volume_identity(volume,engine_id,metadata.st_dev,metadata.st_ino,backing_filesystem_id=space.f_fsid if space.f_fsid>0 else None),
            'engine_identity':retained_volume_engine_identity(engine_id),
            'free_bytes':space.f_bavail*space.f_frsize,
            'backing':['zfs',pools[metadata.st_dev]] if metadata.st_dev in pools else ['device',metadata.st_dev]})
    paths=[Path(volume['Mountpoint']) for volume in volumes.values()]
    for identifier,container in all_containers.items():
        if identifier in own:
            continue
        for mount in container.get('Mounts') or ():
            if mount.get('Name') in volumes:
                raise Refused('production_boundary')
            if mount.get('Type')=='tmpfs' or mount.get('RW') is False:
                continue
            source=mount.get('Source')
            if not isinstance(source,str) or not source.startswith('/'):
                continue
            source=Path(source).resolve()
            if any(source==path or source in path.parents or path in source.parents for path in paths):
                raise Refused('production_boundary')
    for container in own.values():
        if 'io.launchplane.production-recipient' in (container.get('Config',{}).get('Labels') or {}):
            raise Refused('production_boundary')
        for mount in container.get('Mounts') or ():
            if mount.get('Type')=='tmpfs' or mount.get('RW') is False:
                continue
            if mount.get('Type')!='volume' or mount.get('Name') not in volumes:
                raise Refused('production_boundary')
    mode=spec.get('mode','dry-run')
    physical={}
    if mode=='apply':
        before={tuple(row['backing']):row['free_bytes'] for row in result if 'backing' in row}
        effect_started=True
        if own:
            docker('stop',*own)
            docker('rm',*own)
        for row in sorted(result,key=lambda item:item['role']=='ODOO_DATA_VOLUME'):
            if row['status']=='absent':
                continue
            path=Path(volumes[row['name']]['Mountpoint'])
            docker('volume','rm',row['name'])
            remaining=set(identifiers('volume','ls','-q'))
            if row['name'] in remaining:
                raise RuntimeError('volume_absence_unverified')
            probe=path
            while not probe.exists():
                probe=probe.parent
            space=os.statvfs(probe)
            after=space.f_bavail*space.f_frsize
            backing=tuple(row['backing'])
            physical[row['identity']]={'before_bytes':before[backing],'after_bytes':after,
                'observed_change_bytes':after-before[backing]}
            before[backing]=after
            row['status']='absent'
            row['reason']='removed'
    projects={(row.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project') for row in own.values()}
    if len(projects)==1:
        spec['project']=next(iter(projects))
    capacity=[]
    capacity_refusal='receiver_generation_unavailable'
    generation=spec.get('generation')
    if generation is not None:
        try:
            if not isinstance(generation.get('drill_storage_budget'),dict):
                raise ValueError('receiver storage requirement unavailable')
            if any(row['status']!='observed' for row in result):
                raise ValueError('receiver storage unavailable')
            containers={}
            for identifier,row in all_containers.items():
                labels=row.get('Config',{}).get('Labels') or {}
                if labels.get('com.docker.compose.project')!=spec['project']:
                    continue
                role=labels.get('com.docker.compose.service')
                if role in containers:
                    raise ValueError('receiver role is ambiguous')
                containers[role]=(identifier,row)
            if 'script-runner' not in containers:
                raise ValueError('receiver runner is unavailable')
            data_root=Path(volumes[namespace+'-data']['Mountpoint'])
            migrated=generation.get('migrated_source')
            spec['preview_volumes']={'database':namespace+'-db','data':namespace+'-data'}
            arguments={}
            if migrated is None:
                raise ValueError('receiver staged requirement unavailable')
            if migrated is not None:
                if re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,62}',spec['database_name']) is None:
                    raise ValueError('receiver database identity unavailable')
                arguments={'runner_staging_bytes':migrated['database']['size'],
                    'filestore_existing_bytes':populated_filestore_bytes(data_root/'filestore'/spec['database_name']),
                    'data_staging_bytes':remaining_migrated_staging(data_root,generation['source_binding_sha256'][:32],migrated)}
            storage_capacity(spec,containers,generation['drill_storage_budget'],probe_clones=False,observations=capacity,**arguments)
            capacity_refusal=''
        except (KeyError,ValueError,RuntimeError,OSError):
            capacity=[]
            capacity_refusal='receiver_backing_or_requirement_unknown'
    response={'volumes':result,'capacity':capacity,'capacity_refusal':capacity_refusal,
        'physical_readback':physical,'serving':bool(serving),'container_count':0 if mode=='apply' else len(own)}
    if mode=='apply':
        response['release_status']='released'
        save_receipt(response)
    return response

try:
    response=inspect_candidate()
except Refused as error:
    response={'volumes':[],'capacity':[],'capacity_refusal':'physical_inventory_unknown',
        'release_status':'held','reason_code':str(error)}
    save_receipt(response)
except Exception:
    response={'volumes':[],'capacity':[],'capacity_refusal':'physical_inventory_unknown',
        'release_status':'unknown' if effect_started else 'held','reason_code':'host_read_unavailable'}
    if not effect_started:
        save_receipt(response)
print(prefix+json.dumps({'type':'complete',**response},sort_keys=True),flush=True)
"""
)
