"""Durable host tombstones fence queued workers before retiring their namespace."""

HOST_FENCE_LIBRARY = r"""
import base64, fcntl, json, os, re, sys
from pathlib import Path
from contextlib import contextmanager

@contextmanager
def recipient_execution(spec, *, retire=False, base=None):
    namespace=spec['namespace']
    binding=spec['binding']
    if not re.fullmatch(r'lp-prod-recipient-[a-f0-9]{32}',namespace) or not re.fullmatch(r'[a-f0-9]{64}',binding):
        raise ValueError('invalid execution identity')
    base=base or Path('/var/lib/launchplane/recipient-execution')
    if base.is_symlink() or any(p.is_symlink() for p in base.parents):
        raise ValueError('invalid execution custody')
    base.mkdir(parents=True,exist_ok=True,mode=0o700)
    directory=base/namespace
    if directory.is_symlink():
        raise ValueError('invalid execution custody')
    directory.mkdir(mode=0o700,exist_ok=True)
    descriptor=os.open(directory/'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        tombstone=directory/'retired.json'
        if retire:
            if tombstone.exists():
                if tombstone.is_symlink() or json.loads(tombstone.read_text())!={'binding':binding}:
                    raise ValueError('execution tombstone changed')
            else:
                with os.fdopen(os.open(tombstone,os.O_CREAT|os.O_EXCL|os.O_WRONLY|os.O_NOFOLLOW,0o600),'w') as output:
                    json.dump({'binding':binding},output)
                    output.flush()
                    os.fsync(output.fileno())
                parent=os.open(directory,os.O_RDONLY|os.O_DIRECTORY)
                try:
                    os.fsync(parent)
                finally:
                    os.close(parent)
        elif tombstone.exists() or tombstone.is_symlink():
            raise ValueError('recipient execution permanently retired')
        yield
    finally:
        os.close(descriptor)
"""


def fenced_program(program: str, namespace: str, binding: str) -> str:
    # Keep the lock across the entire fixed program, including its subprocesses.
    return (
        HOST_FENCE_LIBRARY
        + "\nwith recipient_execution("
        + repr({"namespace": namespace, "binding": binding})
        + "):\n    exec("
        + repr(program)
        + ")\n"
    )


RETIRE_EXECUTION_PROGRAM = (
    HOST_FENCE_LIBRARY
    + "\n_spec=json.loads(base64.b64decode(sys.argv[1],validate=True))\n"
    + "with recipient_execution(_spec,retire=True):\n"
    + "    print('recipient_execution_fenced='+_spec['binding'])\n"
)
