import inspect
from pathlib import Path
from control_plane.dokploy.testing_retained_copy import (
    failed_candidate_own_web,
    web_gate_unexpected,
)
from control_plane.integration_readback import INTEGRATION_READBACK_PASSED_PATH

from control_plane._odoo_clean_snapshot_program import CLEAN_TESTING_SNAPSHOT_LIBRARY
from control_plane._odoo_testing_storage_program import STORAGE_MAINTENANCE_LIBRARY
from control_plane.retained_volume_capture import (
    _batched_docker_inspect,
    retained_volume_docker_command,
    retained_volume_engine_identity,
    retained_volume_identity,
)


def staged_source_inputs(path: Path) -> bool:
    import os
    import re
    import stat

    try:
        children = list(path.iterdir())
    except OSError:
        return True
    for child in children:
        if not child.name.startswith(".lp-native-"):
            continue
        try:
            before = child.lstat()
            if (
                re.fullmatch(r"\.lp-native-[a-f0-9]{32}", child.name) is None
                or not stat.S_ISDIR(before.st_mode)
                or child.resolve(strict=True) != child
            ):
                return True
            with os.scandir(child) as contents:
                if next(contents, None) is not None:
                    return True
            after = child.lstat()
            if (before.st_dev, before.st_ino, before.st_mtime_ns, before.st_ctime_ns) != (
                after.st_dev,
                after.st_ino,
                after.st_mtime_ns,
                after.st_ctime_ns,
            ):
                return True
        except (OSError, RuntimeError):
            return True
    return False


NATIVE_TESTING_RETENTION_INSPECTION = (
    "from collections.abc import Callable,Mapping,Sequence\nfrom typing import Any\n"
    "import base64,hashlib,json,os,re,subprocess,sys,time\nfrom pathlib import Path\n"
    + inspect.getsource(_batched_docker_inspect)
    + inspect.getsource(retained_volume_docker_command)
    + inspect.getsource(retained_volume_identity)
    + inspect.getsource(retained_volume_engine_identity)
    + inspect.getsource(staged_source_inputs)
    + inspect.getsource(web_gate_unexpected)
    + inspect.getsource(failed_candidate_own_web)
    + CLEAN_TESTING_SNAPSHOT_LIBRARY
    + STORAGE_MAINTENANCE_LIBRARY
    + "\nWEB_GATE_PATH="
    + repr(INTEGRATION_READBACK_PASSED_PATH)
    + "\n"
    + r"""
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
prefix=sys.argv[2]
deadline=time.monotonic()+70

def docker(*arguments):
    remaining=deadline-time.monotonic()
    if remaining<=0:
        raise ValueError('inventory deadline')
    result=subprocess.run(retained_volume_docker_command(*arguments),capture_output=True,timeout=min(10,remaining),check=False)
    if result.returncode or len(result.stdout)>8*1024*1024:
        raise ValueError('inventory unavailable')
    return result.stdout.decode('utf-8',errors='strict')

def load(*arguments):
    return json.loads(docker(*arguments))

def identifiers(*arguments):
    rows=docker(*arguments).splitlines()
    if len(rows)>10000 or len(rows)!=len(set(rows)) or any(not row for row in rows):
        raise ValueError('inventory incomplete')
    return rows

def inspect_candidate():
    resources=spec['resources']
    namespace=resources['namespace']
    if resources['instance']!='testing' or re.fullmatch(r'lp-testing-[a-f0-9]{32}',namespace) is None:
        raise ValueError('testing ownership required')
    digest=hashlib.sha256(json.dumps(resources,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    engine=load('info','--format','{{json .}}')
    engine_id=engine['ID']
    root=Path(engine['DockerRootDir'])
    if not engine_id or not root.is_absolute() or root.resolve(strict=True)!=root:
        raise ValueError('engine identity unavailable')
    expected={'ODOO_DB_VOLUME':namespace+'-db','ODOO_DATA_VOLUME':namespace+'-data','ODOO_LOG_VOLUME':namespace+'-logs'}
    existing=set(identifiers('volume','ls','-q'))
    volumes=_batched_docker_inspect('volume',sorted(existing & set(expected.values())),load)
    all_containers=_batched_docker_inspect('container',identifiers('ps','-aq','--no-trunc'),load)
    use_reason=''
    for container in all_containers.values():
        labels=container.get('Config',{}).get('Labels') or {}
        if labels.get('com.docker.compose.project')==spec['project']:
            if labels.get('com.docker.compose.service')=='web' and container.get('State',{}).get('Running') is not False and not failed_candidate_own_web(container,spec,lambda identifier:docker('top',identifier,'-eo','comm').splitlines()[1:]):
                use_reason='candidate_web_active_or_unknown'
            if container.get('HostConfig',{}).get('PortBindings'):
                use_reason='candidate_published_ports'
    completed=spec.get('failed_candidate',{}).get('completed') is True
    gate_unexpected=True
    result=[]
    pools=storage_backing_pools()
    for role,name in expected.items():
        volume=volumes.get(name)
        if volume is None:
            result.append({'role':role,'name':name,'status':'absent','reason':'provider_absent'})
            continue
        path=Path(volume['Mountpoint'])
        labels=volume.get('Labels')
        if (volume.get('Name')!=name or volume.get('Driver')!='local' or volume.get('Options') not in (None,{})
            or not isinstance(labels,dict) or labels.get('io.launchplane.testing-resources')!=digest
            or labels.get('io.launchplane.testing-resource-role')!=role
            or 'io.launchplane.production-recipient' in labels
            or path!=root/'volumes'/name/'_data' or path.resolve(strict=True)!=path or not path.is_dir()):
            result.append({'role':role,'name':name,'status':'held','reason':'physical_ownership_unknown'})
            continue
        metadata=path.stat();space=os.statvfs(path)
        identity=retained_volume_identity(volume,engine_id,metadata.st_dev,metadata.st_ino,backing_filesystem_id=space.f_fsid if space.f_fsid>0 else None)
        with os.scandir(path) as children:
            empty=next(children,None) is None
        sources=staged_source_inputs(path)
        referenced=False
        for container in all_containers.values():
            mounts=container.get('Mounts')
            if not isinstance(mounts,list):
                raise ValueError('container mounts unavailable')
            referenced=referenced or any(mount.get('Name')==name for mount in mounts)
        if role=='ODOO_DATA_VOLUME':
            gate=path/Path(WEB_GATE_PATH).relative_to('/volumes/data')
            gate_unexpected=web_gate_unexpected(gate,completed)
        result.append({'role':role,'name':name,'status':'observed','reason':use_reason or ('source_inputs_preserved' if sources else 'container_reference' if referenced else ''),
            'identity':identity,'engine_identity':retained_volume_engine_identity(engine_id),
            'empty':empty,'source_inputs':sources,'referenced':referenced,'free_bytes':space.f_bavail*space.f_frsize,
            'backing':['zfs',pools[metadata.st_dev]] if metadata.st_dev in pools else ['device',metadata.st_dev],
            'boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
            'monotonic_deadline':time.monotonic()+300})
    for row in result:
        if row['status']=='observed':
            row['gate_unexpected']=not row['empty'] and gate_unexpected
        if row['status']=='observed' and not row['empty'] and gate_unexpected and row['reason'] in ('','container_reference'):
            row['reason']='completed_or_unknown_web_gate'
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
    return {'volumes':result,'capacity':capacity,'capacity_refusal':capacity_refusal}

try:
    print(prefix+json.dumps({'type':'complete',**inspect_candidate()},sort_keys=True),flush=True)
except Exception:
    print(prefix+json.dumps({'type':'complete','volumes':[],'capacity':[],'capacity_refusal':'physical_inventory_unknown'}),flush=True)
"""
)
