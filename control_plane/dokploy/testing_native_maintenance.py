"""Fixed native drill maintenance over the existing managed host transport."""

from typing import Any, Literal
from collections.abc import Callable
import inspect
from control_plane.docker_errors import docker_daemon_authorization_denied
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from control_plane._odoo_copy_runtime_guard import COPY_RUNTIME_INSPECTION_LIBRARY
from control_plane._odoo_testing_native_maintenance_program import NATIVE_MAINTENANCE_LIBRARY
from control_plane.dokploy.native_copy_stream import _read_remote
from control_plane.dokploy.testing_retained_copy import qualify_retained_copy
from control_plane.dokploy.native_copy_stream import NativeSourceUnavailable


class NativeMaintenanceOutcome(BaseModel):
    """Closed, value-free evidence from the fixed host protocol."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["complete"]
    status: Literal[
        "missing",
        "preserved",
        "eligible",
        "recent_use",
        "unqualified",
        "disposed",
        "incomplete",
        "resumable",
        "unknown",
        "protected",
        "active",
        "resumable_retirement",
        "recovery_required",
        "observation_unavailable",
    ]
    reason: (
        Literal[
            "original_retirement_evidence_missing",
            "original_retirement_not_dispatched",
            "original_retirement_active",
            "original_stop_outcome_unknown",
            "original_retirement_remaining_steps",
            "original_removal_outcome_unknown",
            "native_copy_lock_busy",
            "native_copy_custody_changed",
            "native_copy_receipt_untrusted",
            "original_mutation_outcome_unknown",
            "native_copy_observation_unavailable",
        ]
        | None
    ) = None
    no_stop_dispatched: bool | None = None
    namespace_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")
    host_boot_id: str = Field(min_length=1, max_length=128)
    host_monotonic: float = Field(ge=0, allow_inf_nan=False)
    host_observed_at: float = Field(ge=0, allow_inf_nan=False)
    identity_sha256: str | None = Field(default=None, pattern=r"^[a-f0-9]{64}$")


def native_maintenance_host_program() -> str:
    return (
        "import base64,fcntl,hashlib,json,os,re,stat,subprocess,sys,tempfile,time,uuid\n"
        "from datetime import datetime,timezone\nfrom pathlib import Path\nfrom typing import Any\n"
        + COPY_RUNTIME_INSPECTION_LIBRARY
        + inspect.getsource(qualify_retained_copy)
        + inspect.getsource(docker_daemon_authorization_denied)
        + NATIVE_MAINTENANCE_LIBRARY
        + r"""
def command(*arguments, stdin=None, pass_fds=(), timeout=30):
    result=subprocess.run(['docker',*arguments],input=stdin,capture_output=True,
        pass_fds=pass_fds,timeout=timeout,check=False)
    if result.returncode:
        if docker_daemon_authorization_denied(result.stderr):
            raise NativeMaintenanceProviderDenied('Native maintenance was refused')
        raise RuntimeError('Native maintenance command did not complete')
    return result.stdout
def docker(*arguments,stdin=None):
    return command(*arguments,stdin=stdin)
def one_inspection(identifier):
    values=json.loads(docker('inspect',identifier))
    if not isinstance(values,list) or len(values)!=1 or not isinstance(values[0],dict):
        raise RuntimeError('Native inventory is unavailable')
    native_container_observation(values[0],spec)
    return values[0]
try:
    spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
    result=native_maintenance_main(spec,command)
    print(sys.argv[2]+json.dumps({'type':'complete',**result,**native_host_clock()}),flush=True)
except NativeMaintenanceProviderDenied:
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
        raise NativeSourceUnavailable("Original native maintenance requires observation.")
    try:
        return NativeMaintenanceOutcome.model_validate(frames[0]).model_dump(exclude_none=True)
    except ValidationError as error:
        raise NativeSourceUnavailable(
            "Original native maintenance returned an invalid outcome."
        ) from error
