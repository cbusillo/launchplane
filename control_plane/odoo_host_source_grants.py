import json
from pathlib import Path
from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from control_plane import odoo_runtime_reads as reads
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.dokploy import api
from control_plane.dokploy import source as dokploy_source
from control_plane.dokploy.native_copy_stream import _read_remote
from control_plane.odoo_import_diagnostics import (
    DiagnosticStore,
    finish_probe,
    import_authority,
    managed_probe_input,
    reserve_probe,
)
from control_plane.odoo_source_probe import MAX_GRANT_NAMES, GrantStatus, ProbeFailure
from control_plane.secrets import _decrypt_secret_value

SOURCE = "fishbowl"
FRAME_BYTES_LIMIT = 4096
REMOTE_TIMEOUT_SECONDS = 75

GrantName = Annotated[str, StringConstraints(pattern=r"^[A-Z][A-Z ]{0,39}$")]
ContainerCleanup = Literal["not_started", "removed", "unverified"]
HostProbeFailure = Literal["image", "create", "execution"] | ProbeFailure
FailureStage = (
    Literal[
        "capacity",
        "authority",
        "binding",
        "runtime_selection",
        "target",
        "production_host",
        "settings",
        "transport",
        "result_validation",
        "cleanup",
        "drift",
    ]
    | HostProbeFailure
)

HOST_PROGRAM = r"""
import base64,json,re,subprocess,sys,uuid

spec=json.loads(base64.b64decode(sys.argv[1],validate=True));prefix=sys.argv[2]
name='lp-source-grants-'+uuid.uuid4().hex
label_key='io.launchplane.source-grant-probe'

def docker(*args,**options):
    return subprocess.run(['docker',*args],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL,check=False,**options)

def checked(*args):
    completed=docker(*args,timeout=20)
    if completed.returncode:
        raise ValueError('docker')
    return completed.stdout.decode()

def owned_containers():
    return checked('ps','-aq','--filter','name=^/'+name+'$').split()

def remove_owned_container(create_confirmed):
    try:
        found=owned_containers()
        for candidate in found:
            if json.loads(checked('inspect',candidate))[0]['Config']['Labels'].get(label_key)!=name:
                return 'unverified'
            checked('rm','-f',candidate)
        if owned_containers():
            return 'unverified'
        return 'removed' if create_confirmed or found else 'unverified'
    except Exception:
        return 'unverified'

def immutable_local_image(image):
    return (re.fullmatch(r'[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}',image) is not None
        and image in json.loads(checked('image','inspect','--format','{{json .RepoDigests}}',image)))

def well_formed(observed):
    return (isinstance(observed,dict) and set(observed)=={'grants','names','failure'}
        and observed['grants'] in ('read_only','unsafe','unknown')
        and observed['failure'] in (None,'dns','tcp','tls','timeout','auth','driver')
        and isinstance(observed['names'],list) and len(observed['names'])<=__MAX_NAMES__
        and all(isinstance(item,str) and re.fullmatch(r'[A-Z][A-Z ]{0,39}',item) for item in observed['names']))

outcome={'grants':'unknown','names':[],'failure':'image','cleanup':'not_started'}
try:
    if immutable_local_image(spec['image']):
        outcome['failure']='create'
        outcome['cleanup']='unverified'
        identifier=''
        try:
            identifier=checked('create','--interactive','--pull','never','--name',name,'--label',label_key+'='+name,
                '--network','host','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
                '--memory','128m','--cpus','0.25','--pids-limit','64','--entrypoint','python3',
                spec['image'],'-I','-c',spec['probe']).strip()
            outcome['failure']='execution'
            completed=docker('start','--attach','--interactive',identifier,input=json.dumps(spec['credentials']).encode(),timeout=30)
            if completed.returncode==0 and len(completed.stdout)<=4096:
                observed=json.loads(completed.stdout)
                if well_formed(observed):
                    outcome.update(grants=observed['grants'],names=observed['names'],failure=observed['failure'])
        finally:
            outcome['cleanup']=remove_owned_container(bool(identifier))
except Exception:
    pass
print(prefix+json.dumps({'type':'complete','result':outcome}),flush=True)
""".replace("__MAX_NAMES__", str(MAX_GRANT_NAMES))


class HostSourceGrantResult(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source: Literal["fishbowl"] = "fishbowl"
    grants: GrantStatus = "unknown"
    grant_names: tuple[GrantName, ...] = Field(default=(), max_length=MAX_GRANT_NAMES)
    failure_stage: FailureStage | None = None
    container_cleanup: ContainerCleanup = "not_started"
    rechecks: Literal["current", "changed", "not_run"] = "not_run"
    evidence_sha256: str = ""
    applied: Literal[False] = False
    values_redacted: Literal[True] = True


class _HostFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    grants: GrantStatus
    names: tuple[GrantName, ...] = Field(max_length=MAX_GRANT_NAMES)
    failure: HostProbeFailure | None
    cleanup: ContainerCleanup


class _HostRefused(Exception):
    def __init__(self, stage: FailureStage) -> None:
        super().__init__(stage)
        self.stage: FailureStage = stage


def _unknown(
    stage: FailureStage, cleanup: ContainerCleanup = "not_started"
) -> HostSourceGrantResult:
    return HostSourceGrantResult(failure_stage=stage, container_cleanup=cleanup)


def _lane_server(store: DiagnosticStore, host: str, token: str, context: str, instance: str) -> str:
    record = store.read_dokploy_target_record(context_name=context, instance_name=instance)
    target_id = store.read_dokploy_target_id_record(
        context_name=context, instance_name=instance
    ).target_id
    if record.target_type != "compose":
        raise _HostRefused("target")
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=target_id
    )
    server_id = str(payload.get("serverId") or "")
    if not server_id or str(payload.get("name") or "") != record.target_name:
        raise _HostRefused("target")
    return server_id


def _production_servers(
    store: DiagnosticStore, profile: LaunchplaneProductProfileRecord, host: str, token: str
) -> set[str]:
    servers: set[str] = set()
    for lane in profile.lanes:
        if lane.instance != "prod":
            continue
        try:
            servers.add(_lane_server(store, host, token, lane.context, lane.instance))
        except FileNotFoundError:
            continue
    return servers


def _testing_server(
    store: DiagnosticStore, profile: LaunchplaneProductProfileRecord, host: str, token: str
) -> str:
    lane = next(lane for lane in profile.lanes if lane.instance == "testing")
    server_id = _lane_server(store, host, token, lane.context, lane.instance)
    if server_id in _production_servers(store, profile, host, token):
        raise _HostRefused("production_host")
    return server_id


def _read_host_frame(frames: list[dict[str, object]]) -> _HostFrame:
    if (
        len(frames) != 1
        or set(frames[0]) != {"type", "result"}
        or frames[0]["type"] != "complete"
        or len(json.dumps(frames)) > FRAME_BYTES_LIMIT
    ):
        raise ValueError("Invalid host frame")
    return _HostFrame.model_validate_json(json.dumps(frames[0]["result"]))


def _frame_result(frame: _HostFrame, *, evidence_sha256: str) -> HostSourceGrantResult:
    if frame.cleanup == "unverified":
        return _unknown("cleanup", frame.cleanup)
    return HostSourceGrantResult(
        grants=frame.grants,
        grant_names=frame.names,
        failure_stage=frame.failure,
        container_cleanup=frame.cleanup,
        rechecks="current",
        evidence_sha256=evidence_sha256,
    )


def diagnose_host_source_grants(
    *, store: DiagnosticStore, product: str, control_plane_root: Path, database_url: str | None
) -> HostSourceGrantResult:
    stage: FailureStage = "authority"
    cleanup: ContainerCleanup = "not_started"
    reserved: tuple[str, str, str, str, str] | None = None
    result = _unknown(stage)
    try:
        authority = import_authority(store, product)
        source = authority.sources.get(SOURCE)
        if source is None:
            return _unknown("binding")
        stage = "runtime_selection"
        selection = reads.select_stable_runtime(store, authority.profile, "testing")
        if selection.slot_target_id:
            return _unknown("target")
        host, token = dokploy_source.read_dokploy_config(
            control_plane_root=control_plane_root, database_url=database_url
        )
        stage = "target"
        server_id = _testing_server(store, authority.profile, host, token)
        stage = "settings"
        credentials = managed_probe_input(
            {
                **source.settings,
                "password": _decrypt_secret_value(source.version.ciphertext, source.version.key_id),
            }
        )
        if credentials is None:
            return _unknown("settings")
        stage = "capacity"
        key = (source.binding.binding_id, source.version.version_id, server_id, "host", SOURCE)
        if not reserve_probe(key):
            return _unknown("capacity")
        reserved = key
        stage = "transport"
        cleanup = "unverified"
        frames: list[dict[str, object]] = []
        _read_remote(
            host=host,
            token=token,
            server_id=server_id,
            program=HOST_PROGRAM,
            payload={
                "image": selection.identity.image_reference,
                "probe": Path(__file__).with_name("odoo_source_probe.py").read_text(),
                "credentials": {**credentials, "report_names": "1"},
            },
            receive=frames.append,
            timeout_seconds=REMOTE_TIMEOUT_SECONDS,
        )
        stage = "result_validation"
        frame = _read_host_frame(frames)
        cleanup = frame.cleanup
        stage = "drift"
        reads.assert_selection_current(store, selection)
        if (
            import_authority(store, product).private_snapshot != authority.private_snapshot
            or _testing_server(store, authority.profile, host, token) != server_id
        ):
            return _unknown("drift", cleanup).model_copy(update={"rechecks": "changed"})
        result = _frame_result(
            frame,
            evidence_sha256=nonvalidating_compact_sorted_json_sha256(
                {
                    "server": server_id,
                    "image": selection.identity.image_reference,
                    "binding": source.binding.binding_id,
                    "version": source.version.version_id,
                }
            ),
        )
        return result
    except _HostRefused as refusal:
        return _unknown(refusal.stage, cleanup)
    except Exception:
        # Provider, driver and decryption messages may hold private values.
        return _unknown(stage, cleanup)
    finally:
        if reserved is not None:
            finish_probe(reserved, failed=result.grants == "unknown")
