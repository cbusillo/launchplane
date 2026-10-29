from __future__ import annotations

import hashlib
import inspect
import json
import re
from collections.abc import Mapping

from control_plane.contracts.retained_volume import RetainedVolumeBinding
from control_plane.dokploy import native_copy_stream
from control_plane.testing_application_capabilities import single_local_swarm_node


_SAFE_NAME = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}")


def retained_volume_identity(
    volume: Mapping[str, object], engine_id: str, backing_device: int, backing_inode: int
) -> str:
    payload = {
        "backing_device": backing_device,
        "backing_inode": backing_inode,
        "engine_id": engine_id,
        "volume": volume,
    }
    canonical = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode()).hexdigest()


def capture_retained_volumes(
    *,
    host: str,
    token: str,
    payload: Mapping[str, object],
) -> tuple[tuple[RetainedVolumeBinding, ...], bool]:
    application_id = str(payload.get("applicationId") or "").strip()
    application_name = str(payload.get("appName") or "").strip()
    raw_server = payload.get("serverId")
    server_id = (
        raw_server if isinstance(raw_server, str) and _SAFE_NAME.fullmatch(raw_server) else ""
    )
    mounts = payload.get("mounts")
    if any(_SAFE_NAME.fullmatch(value) is None for value in (application_id, application_name)):
        return (), False
    if not isinstance(mounts, list) or len(mounts) > 10000:
        return (), False
    configured: set[str] = set()
    for mount in mounts:
        if not isinstance(mount, dict):
            return (), False
        if mount.get("type") != "volume":
            continue
        name = mount.get("volumeName")
        if not isinstance(name, str) or _SAFE_NAME.fullmatch(name) is None:
            return (), False
        configured.add(name)
    unknown = tuple(
        RetainedVolumeBinding(
            volume_name=name,
            server_id=server_id,
            application_id=application_id,
            application_name=application_name,
        )
        for name in sorted(configured)
    )
    frames: list[dict[str, object]] = []
    try:
        native_copy_stream.require_registered_remote_server(server_id)
        native_copy_stream.run_managed_server_program(
            host=host,
            token=token,
            server_id=server_id,
            program=_RETAINED_VOLUME_CAPTURE_PROGRAM,
            payload={
                "application_id": application_id,
                "application_name": application_name,
                "configured": sorted(configured),
            },
            receive=frames.append,
            timeout_seconds=90,
        )
    except native_copy_stream.NativeSourceUnavailable:
        return unknown, False
    if len(frames) != 1 or frames[0].get("type") != "complete":
        return unknown, False
    result = frames[0].get("result")
    if not isinstance(result, dict) or result.get("complete") is not True:
        return unknown, False
    rows = result.get("volumes")
    if not isinstance(rows, list) or len(rows) > 10000:
        return unknown, False
    bindings: list[RetainedVolumeBinding] = []
    for row in rows:
        if not isinstance(row, dict):
            return unknown, False
        volume = row.get("volume")
        engine_id = row.get("engine_id")
        device = row.get("backing_device")
        inode = row.get("backing_inode")
        if (
            not isinstance(volume, dict)
            or not isinstance(engine_id, str)
            or not engine_id
            or type(device) is not int
            or type(inode) is not int
            or device < 0
            or inode < 1
        ):
            return unknown, False
        bindings.append(
            RetainedVolumeBinding(
                volume_name=str(volume.get("Name") or ""),
                server_id=server_id,
                application_id=application_id,
                application_name=application_name,
                volume_identity_sha256=retained_volume_identity(volume, engine_id, device, inode),
            )
        )
    if not configured.issubset(binding.volume_name for binding in bindings):
        return unknown, False
    return tuple(sorted(bindings, key=lambda item: item.volume_name)), True


_RETAINED_VOLUME_CAPTURE_PROGRAM = (
    "from collections.abc import Callable\nfrom typing import Any\nimport json\n"
    + inspect.getsource(single_local_swarm_node)
    + r"""
import os,re,subprocess,sys,time
from pathlib import Path

spec=json.loads(__import__('base64').b64decode(__import__('sys').argv[1],validate=True))
deadline=time.monotonic()+70

def emit(value):
    print(sys.argv[2]+json.dumps(value,sort_keys=True),flush=True)

def docker(*args):
    remaining=deadline-time.monotonic()
    if remaining <= 0:
        raise ValueError('deadline')
    result=subprocess.run(['docker',*args],capture_output=True,check=False,timeout=min(10,remaining))
    if result.returncode or len(result.stdout)>8*1024*1024:
        raise ValueError('docker inventory unavailable')
    return result.stdout.decode('utf-8',errors='strict')

def inspect(kind,identifier):
    rows=json.loads(docker('inspect','--type',kind,identifier))
    if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict):
        raise ValueError('inspect unavailable')
    return rows[0]

def mounts(value,referenced):
    if not isinstance(value,list) or len(value)>10000:
        raise ValueError('mount inventory unavailable')
    for mount in value:
        if not isinstance(mount,dict):
            raise ValueError('mount unavailable')
        kind=mount.get('Type') or mount.get('type')
        name=mount.get('Name') or mount.get('Source') or mount.get('volumeName')
        if kind=='volume':
            if not isinstance(name,str) or re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',name) is None:
                raise ValueError('volume identity unavailable')
            referenced.add(name)

def identifiers(*arguments):
    values=docker(*arguments).splitlines()
    if len(values)>10000 or len(set(values))!=len(values) or any(
        re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}',value) is None for value in values
    ):
        raise ValueError('identity inventory unavailable')
    return sorted(values)

def capture():
    nodes=single_local_swarm_node(docker)
    service=inspect('service',spec['application_name'])
    if service.get('Spec',{}).get('Name')!=spec['application_name']:
        raise ValueError('service identity unavailable')
    referenced=set(spec['configured'])
    mounts(service.get('Spec',{}).get('TaskTemplate',{}).get('ContainerSpec',{}).get('Mounts',[]),referenced)
    task_ids=identifiers('service','ps','--no-trunc','--quiet',service['ID'])
    task_rows={}
    for task_id in task_ids:
        task=inspect('task',task_id)
        if task.get('ServiceID')!=service['ID'] or task.get('NodeID') not in ('',nodes[0]['ID']):
            raise ValueError('task attribution unavailable')
        mounts(task.get('Spec',{}).get('ContainerSpec',{}).get('Mounts',[]),referenced)
        task_rows[task_id]=task
    container_ids=identifiers('ps','-aq','--no-trunc')
    container_rows={}
    for container_id in container_ids:
        container=inspect('container',container_id)
        if container.get('Id')!=container_id:
            raise ValueError('container identity unavailable')
        labels=container.get('Config',{}).get('Labels') or {}
        if not isinstance(labels,dict):
            raise ValueError('container attribution unavailable')
        if labels.get('com.docker.swarm.service.id')==service['ID']:
            mounts(container.get('Mounts',[]),referenced)
            container_rows[container_id]=(labels.get('com.docker.swarm.service.id'),container.get('Mounts'))
    engine=json.loads(docker('info','--format','{{json .ID}}'))
    root=Path(json.loads(docker('info','--format','{{json .DockerRootDir}}')))
    if not isinstance(engine,str) or not engine or not root.is_absolute() or root.is_symlink():
        raise ValueError('engine identity unavailable')
    parent=root.resolve(strict=True)/'volumes'
    if parent.is_symlink() or not parent.is_dir():
        raise ValueError('volume root unavailable')
    available=identifiers('volume','ls','--quiet')
    if not referenced.issubset(available):
        raise ValueError('volume inventory unavailable')
    volumes=[]
    for name in sorted(referenced):
        volume=inspect('volume',name)
        if volume.get('Name')!=name or volume.get('Driver')!='local' or volume.get('Options') not in (None,{}) or not volume.get('CreatedAt'):
            raise ValueError('volume metadata unavailable')
        expected=parent/name/'_data'
        if volume.get('Mountpoint')!=str(expected) or expected.parent.is_symlink() or expected.is_symlink() or not expected.is_dir():
            raise ValueError('volume backing unavailable')
        stat=expected.stat()
        volumes.append({'volume':volume,'engine_id':engine,'backing_device':stat.st_dev,'backing_inode':stat.st_ino})
    if single_local_swarm_node(docker)!=nodes or inspect('service',spec['application_name']).get('Spec')!=service.get('Spec'):
        raise ValueError('service inventory changed')
    if identifiers('service','ps','--no-trunc','--quiet',service['ID'])!=task_ids:
        raise ValueError('resource membership changed')
    for task_id,task in task_rows.items():
        current=inspect('task',task_id)
        if any(current.get(key)!=task.get(key) for key in ('ServiceID','NodeID','Spec')):
            raise ValueError('task storage changed')
    current_containers={}
    for container_id in identifiers('ps','-aq','--no-trunc'):
        current=inspect('container',container_id)
        if current.get('Id')!=container_id:
            raise ValueError('container identity unavailable')
        labels=current.get('Config',{}).get('Labels') or {}
        if not isinstance(labels,dict):
            raise ValueError('container attribution unavailable')
        if labels.get('com.docker.swarm.service.id')==service['ID']:
            current_containers[container_id]=(labels.get('com.docker.swarm.service.id'),current.get('Mounts'))
    if current_containers!=container_rows:
        raise ValueError('container storage changed')
    if json.loads(docker('info','--format','{{json .ID}}'))!=engine:
        raise ValueError('engine identity changed')
    for row in volumes:
        volume=row['volume']
        current=inspect('volume',volume['Name'])
        stat=Path(volume['Mountpoint']).stat()
        if current!=volume or (stat.st_dev,stat.st_ino)!=(row['backing_device'],row['backing_inode']):
            raise ValueError('volume identity changed')
    return {'complete':True,'volumes':volumes}

try:
    result=capture()
except Exception:
    result={'complete':False,'volumes':[]}
emit({'type':'complete','result':result})
"""
)
