from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Literal

from control_plane.contracts.odoo_testing_source import (
    NATIVE_TESTING_COPY_CHUNK_BYTES,
    NativeTestingCopyArtifact,
)


def write_native_testing_stream(
    *,
    host: str,
    token: str,
    server_id: str,
    container_id: str,
    capture_id: str,
    part: Literal["database", "filestore"],
    path: Path,
    artifact: NativeTestingCopyArtifact,
) -> None:
    from control_plane.dokploy.native_copy_stream import (
        run_managed_server_program,
        NativeSourceUnavailable,
    )

    frames: list[dict[str, object]] = []
    with path.open("rb") as stream:
        initialized = False

        def send(frame: dict[str, object]) -> list[bytes]:
            nonlocal initialized
            offset, count = frame.get("offset"), frame.get("chunks")
            if (
                type(offset) is not int
                or type(count) is not int
                or not 0 <= offset < artifact.size
                or not 1 <= count <= 16
            ):
                raise NativeSourceUnavailable("The migrated receiver requested an invalid window.")
            if not initialized:
                digest = hashlib.sha256()
                remaining = offset
                while remaining:
                    data = stream.read(min(remaining, NATIVE_TESTING_COPY_CHUNK_BYTES))
                    if not data:
                        raise NativeSourceUnavailable("The original migrated input is truncated.")
                    digest.update(data)
                    remaining -= len(data)
                if frame.get("prefix_sha256") != digest.hexdigest():
                    raise NativeSourceUnavailable(
                        "The receiver prefix differs from the original migrated input."
                    )
                initialized = True
            if stream.tell() != offset:
                raise NativeSourceUnavailable("The migrated receiver changed its original offset.")
            result = []
            for _ in range(count):
                data = stream.read(
                    min(NATIVE_TESTING_COPY_CHUNK_BYTES, artifact.size - stream.tell())
                )
                if not data:
                    raise NativeSourceUnavailable("The migrated input window is incomplete.")
                result.append(data)
            return result

        run_managed_server_program(
            host=host,
            token=token,
            server_id=server_id,
            program=NATIVE_TESTING_STREAM_WRITE_PROGRAM,
            payload={
                "container": container_id,
                "capture_id": capture_id,
                "part": part,
                "artifact": artifact.model_dump(),
            },
            receive=frames.append,
            timeout_seconds=90,
            input_frames=send,
        )
    if frames != [{"type": "complete", **artifact.model_dump()}]:
        raise NativeSourceUnavailable("The migrated stream has no exact receiver witness.")


NATIVE_TESTING_STREAM_WRITE_PROGRAM = r'''
import base64,hashlib,json,os,re,subprocess,sys
spec=json.loads(base64.b64decode(sys.argv[1],validate=True));prefix=sys.argv[2]
def emit(value):
    print(prefix+json.dumps(value,sort_keys=True),flush=True)
try:
    if not re.fullmatch(r'[a-f0-9]{64}',spec['container']) or not re.fullmatch(r'[a-f0-9]{32}',spec['capture_id']) or spec['part'] not in {'database','filestore'}:
        raise RuntimeError('Invalid migrated receiver')
    if type(spec['artifact']['size']) is not int or spec['artifact']['size']<=0 or not re.fullmatch(r'[a-f0-9]{64}',spec['artifact']['sha256']):
        raise RuntimeError('Invalid migrated artifact')
    writer=r"""
import hashlib,json,os,shutil,stat,sys
from pathlib import Path
spec=json.loads(sys.argv[1]);expected=spec['artifact']
def hash_prefix(handle):
    digest=hashlib.sha256();total=0
    while data:=handle.read(1024*1024):
        digest.update(data);total+=len(data)
        if total%(64*1024*1024)==0:
            print(json.dumps({'progress':True}),flush=True)
    return digest
root=Path('/volumes/data')/('.lp-native-'+spec['capture_id'])
if root.is_symlink():
    raise RuntimeError('Unsafe migrated staging root')
root.mkdir(mode=0o700,exist_ok=True)
if root.resolve()!=root.absolute():
    raise RuntimeError('Unsafe migrated staging root')
name=spec['part']+'.artifact'
complete=root/name;partial=root/(name+'.partial')
if complete.is_symlink() or partial.is_symlink():
    raise RuntimeError('Unsafe migrated staging path')
if complete.exists():
    with complete.open('rb') as handle:
        digest=hash_prefix(handle).hexdigest()
    if complete.stat().st_size!=expected['size'] or digest!=expected['sha256']:
        raise RuntimeError('Existing migrated artifact differs')
    print(json.dumps({'offset':expected['size'],'prefix_sha256':digest}),flush=True)
else:
    descriptor=os.open(partial,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW|os.O_NONBLOCK,0o600)
    with os.fdopen(descriptor,'r+b') as handle:
        if not stat.S_ISREG(os.fstat(handle.fileno()).st_mode):
            raise RuntimeError('Migrated staging is not regular')
        digest=hash_prefix(handle)
        offset=handle.tell()
        if offset>expected['size']:
            raise RuntimeError('Existing migrated prefix is too long')
        if shutil.disk_usage(root).free < expected['size']-offset:
            raise RuntimeError('Insufficient space for migrated staging')
        print(json.dumps({'offset':offset,'prefix_sha256':digest.hexdigest()}),flush=True)
        remaining=expected['size']-offset
        while remaining:
            data=sys.stdin.buffer.read(min(remaining,65536))
            if not data:
                raise RuntimeError('Migrated stream is incomplete')
            handle.write(data);digest.update(data);remaining-=len(data)
        handle.flush();os.fsync(handle.fileno())
        if digest.hexdigest()!=expected['sha256']:
            raise RuntimeError('Migrated stream digest differs')
    os.link(partial,complete,follow_symlinks=False);partial.unlink()
    directory=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)
print(json.dumps(expected),flush=True)
"""
    child=subprocess.Popen(['docker','exec','-i','-u','root',spec['container'],'python3','-u','-c',writer,json.dumps(spec)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE)
    try:
        while True:
            start=json.loads(child.stdout.readline())
            if start=={'progress':True}:
                emit({'type':'progress'})
            else:
                break
        offset=start['offset'];total=spec['artifact']['size']
        with os.fdopen(os.dup(0),'rb') as source:
            while offset<total:
                count=min(16,(total-offset+65535)//65536)
                emit({'type':'input','offset':offset,'chunks':count,'prefix_sha256':start['prefix_sha256']})
                for _ in range(count):
                    line=source.readline(87386)
                    if not line.endswith(b'\n'):
                        raise RuntimeError('Migrated input window is incomplete')
                    data=base64.b64decode(line.strip(),validate=True)
                    if len(data)!=min(65536,total-offset):
                        raise RuntimeError('Migrated input window size differs')
                    child.stdin.write(data);child.stdin.flush();offset+=len(data)
        child.stdin.close()
        result=json.loads(child.stdout.readline())
        if child.wait()!=0 or result!=spec['artifact']:
            raise RuntimeError('Migrated receiver did not complete')
        emit({'type':'complete',**result})
    finally:
        if child.poll() is None:
            child.stdin.close();child.wait()
except Exception:
    emit({'type':'failed'})
'''
