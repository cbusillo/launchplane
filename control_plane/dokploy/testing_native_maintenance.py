"""Fixed native drill maintenance over the existing managed host transport."""

from typing import Any
from collections.abc import Callable
import inspect

from control_plane._odoo_copy_runtime_guard import COPY_RUNTIME_INSPECTION_LIBRARY
from control_plane._odoo_testing_native_maintenance_program import NATIVE_MAINTENANCE_LIBRARY
from control_plane.dokploy.native_copy_stream import _read_remote
from control_plane.dokploy.testing_retained_copy import qualify_retained_copy


def native_maintenance_host_program() -> str:
    return (
        "import base64,fcntl,hashlib,json,os,re,stat,subprocess,sys,tempfile,time,uuid\n"
        "from datetime import datetime,timezone\nfrom pathlib import Path\nfrom typing import Any\n"
        + COPY_RUNTIME_INSPECTION_LIBRARY
        + inspect.getsource(qualify_retained_copy)
        + NATIVE_MAINTENANCE_LIBRARY
        + r"""
def command(*arguments, stdin=None, pass_fds=(), timeout=30):
    result=subprocess.run(['docker',*arguments],input=stdin,capture_output=True,
        pass_fds=pass_fds,timeout=timeout,check=False)
    if result.returncode:
        if any(value in result.stderr.lower() for value in (b'permission denied',b'unauthorized',b'access denied')):
            raise PermissionError('Native maintenance was refused')
        raise RuntimeError('Native maintenance command did not complete')
    return result.stdout
def docker(*arguments,stdin=None):
    return command(*arguments,stdin=stdin)
def one_inspection(identifier):
    values=json.loads(docker('inspect',identifier))
    if len(values)!=1:
        raise ValueError('Native inventory is incomplete')
    return values[0]
try:
    spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
    result=native_maintenance_main(spec,command)
    print(sys.argv[2]+json.dumps({'type':'complete',**result,**native_host_clock()}),flush=True)
except PermissionError:
    print(sys.argv[2]+json.dumps({'type':'denied'}),flush=True)
    sys.exit(1)
except Exception:
    print(sys.argv[2]+json.dumps({'type':'failed'}),flush=True)
"""
    )


def native_maintenance_operation(
    *,
    host: str,
    token: str,
    server_id: str,
    payload: dict[str, Any],
    dispatch_guard: Callable[[Callable[[], None]], None] | None = None,
) -> dict[str, Any]:
    frames: list[dict[str, Any]] = []
    _read_remote(
        host=host,
        token=token,
        server_id=server_id,
        program=native_maintenance_host_program(),
        payload=payload,
        receive=frames.append,
        timeout_seconds=150,
        dispatch_guard=dispatch_guard,
    )
    if len(frames) != 1 or frames[0].get("type") != "complete":
        raise ValueError("Original native maintenance requires observation.")
    return frames[0]
