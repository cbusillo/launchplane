"""Fixed destination programs; no product command, credential export or guest restore."""

import base64
import hashlib
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from control_plane import odoo_source_probe
from control_plane.contracts.production_recipient import ProductionRecipientRecord
from control_plane.dokploy import api, post_deploy, source
from control_plane.production_recipient_resources import RESOURCE_LIBRARY
from control_plane.production_recipient_fence import fenced_program
from control_plane.production_recipient_evidence import EXECUTION_FENCE_MARKER


class RecipientProbeCleanupError(RuntimeError):
    """The private provider probe script may still contain managed inputs."""


def run_recipient_program(
    root: Path,
    record: ProductionRecipientRecord,
    spec: dict[str, Any],
    program: str,
    checkpoint: Callable[[str], None],
    *,
    scrub_guard: Callable[[], None] | None = None,
    retire_execution: bool = False,
    custodied_read: bool = False,
) -> dict[str, str]:
    if retire_execution:
        from control_plane.production_recipient_fence import RETIRE_EXECUTION_PROGRAM

        if program != RETIRE_EXECUTION_PROGRAM or spec != {
            "namespace": record.namespace,
            "binding": record.plan_sha256,
        }:
            raise ValueError("Only the fixed namespace retirement fence can bypass execution.")
    if custodied_read:
        from control_plane.production_recipient_cutover import (
            WRITER_RECEIPT_PROGRAM,
            WRITER_PROGRAM,
        )

        if (
            retire_execution
            or not spec.get("inspect")
            or not (
                program == WRITER_RECEIPT_PROGRAM
                and spec.get("action") in {"receipt_pause", "receipt_resume"}
                or program == WRITER_PROGRAM
                and spec.get("action") == "parked"
            )
        ):
            raise ValueError("Custodied host access permits only fixed writer reads.")
    host, token = source.read_dokploy_config(control_plane_root=root)
    payload: api.JsonObject = (
        {
            "composeId": record.target_id,
            "serverId": record.request.destination_server_id,
            "appName": record.app_name,
        }
        if retire_execution or custodied_read
        else api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=record.target_id
        )
    )
    if str(payload.get("serverId") or "") != record.request.destination_server_id:
        raise ValueError("Recipient destination changed.")
    kind, lookup, _, server = post_deploy._resolve_dokploy_schedule_runtime(
        host=host,
        token=token,
        compose_id=record.target_id,
        compose_name=record.namespace,
        target_payload=payload,
    )
    encoded = base64.b64encode(json.dumps(spec).encode()).decode()
    # Readback dispatches before mutation admission. It must never replace the
    # script that a concurrently leased apply is about to trigger.
    schedule_name = (
        "Launchplane isolated recipient readback"
        if spec.get("inspect")
        else "Launchplane isolated recipient qualification"
    )
    schedule_app_name = (
        "lp-rjob-"
        + record.namespace.rsplit("-", 1)[-1]
        + "-"
        + hashlib.sha256(
            json.dumps(
                [
                    record.namespace,
                    record.target_id,
                    "inspect" if spec.get("inspect") else "apply",
                    "retire" if retire_execution else "qualify",
                    hashlib.sha256(program.encode()).hexdigest(),
                    spec.get("action", ""),
                    spec.get("binding", ""),
                ]
            ).encode()
        ).hexdigest()[:16]
    )
    schedule_payload: api.JsonObject = {
        "name": schedule_name,
        "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "appName": schedule_app_name,
        "shellType": "bash",
        "scheduleType": kind,
        "command": "control-plane recipient qualification",
        "script": "#!/usr/bin/env bash\nset -euo pipefail\npython3 - <<'LP_QUALIFY'\nimport sys\nsys.argv=['recipient',"
        + repr(encoded)
        + "]\n"
        + (
            program
            if retire_execution or custodied_read
            else fenced_program(program, record.namespace, record.plan_sha256)
        )
        + "\nLP_QUALIFY\n",
        "serverId": server,
        "userId": lookup if kind == "dokploy-server" else None,
        "enabled": False,
        "timezone": "UTC",
    }
    try:
        evidence = post_deploy.run_manual_data_schedule(
            host=host,
            token=token,
            target_definition=source.DokployTargetDefinition(
                context=record.context, instance="prod", target_id=record.target_id
            ),
            schedule_lookup_id=lookup,
            schedule_type=kind,
            schedule_name=schedule_name,
            schedule_app_name=schedule_app_name,
            schedule_payload=schedule_payload,
            resolved_schedule_execution_timeout_seconds=300,
            before_provider_mutation=checkpoint,
        )
        if retire_execution and evidence.get(EXECUTION_FENCE_MARKER) == record.plan_sha256:
            for old in api.list_dokploy_schedules(
                host=host, token=token, target_id=lookup, schedule_type=kind
            ):
                old_app = str(old.get("appName") or "")
                prefix = "lp-rjob-" + record.namespace.rsplit("-", 1)[-1] + "-"
                if not old_app.startswith(prefix) or old_app == schedule_app_name:
                    continue
                checkpoint("scrub_retained_recipient_program")
                old_name = str(old.get("name") or "")
                if not old_name or not api.schedule_key(old):
                    raise RecipientProbeCleanupError("Retained program identity is unavailable.")
                scrub_script = "#!/bin/sh\nexit 1\n"
                scrubbed = api.upsert_dokploy_schedule(
                    host=host,
                    token=token,
                    target_id=lookup,
                    schedule_type=kind,
                    schedule_name=old_name,
                    app_name=old_app,
                    schedule_payload={
                        **schedule_payload,
                        "name": old_name,
                        "appName": old_app,
                        "script": scrub_script,
                    },
                )
                if scrubbed.get("script") != scrub_script or scrubbed.get("enabled") is not False:
                    raise RecipientProbeCleanupError(
                        "Retained recipient program scrub is unverified."
                    )
        return evidence
    finally:
        if spec.get("connections"):
            # A manual-only schedule must not retain a second copy of its
            # managed credential inputs after the probe. Cleanup is leased too.
            try:
                if scrub_guard is None:
                    raise ValueError("Private probe cleanup requires its execution lease.")
                scrub_guard()
                scrub_script = "#!/bin/sh\nexit 1\n"
                scrubbed = api.upsert_dokploy_schedule(
                    host=host,
                    token=token,
                    target_id=lookup,
                    schedule_type=kind,
                    schedule_name=schedule_name,
                    app_name=schedule_app_name,
                    schedule_payload={**schedule_payload, "script": scrub_script},
                )
                if scrubbed.get("script") != scrub_script or scrubbed.get("enabled") is not False:
                    raise ValueError("Private probe scrub readback did not match.")
            except Exception as error:
                raise RecipientProbeCleanupError(
                    "recipient_probe_cleanup_unverified: retain the original operation and private provider evidence."
                ) from error


def upstream_program() -> str:
    """Use the same conservative grant classifier as the existing source diagnostic."""
    return (
        "import base64,json,os,re,subprocess,sys\nfrom typing import Literal\n"
        "GrantStatus=Literal['read_only','unsafe','unknown']\n"
        + "_READ_PRIVILEGES=frozenset("
        + repr(tuple(odoo_source_probe._READ_PRIVILEGES))
        + ")\n"
        + "_GRANT=re.compile("
        + repr(odoo_source_probe._GRANT.pattern)
        + ")\n"
        + inspect.getsource(odoo_source_probe.classify_grants)
        + UPSTREAM_PROGRAM
    )


UPSTREAM_PROGRAM = r"""
import uuid

def docker(*args, **kwargs):
    result=subprocess.run(["docker",*args],capture_output=True,text=True,timeout=20,**kwargs)
    if result.returncode or len(result.stdout)>1024*1024:
        raise ValueError("probe failed")
    return result.stdout

try:
    spec=json.loads(base64.b64decode(sys.argv[1], validate=True))
    if not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}", spec["image"]):
        raise ValueError("immutable client required")
    if not re.fullmatch(r"lp-prod-recipient-[a-f0-9]{32}(?:-[a-z0-9]{6})?", spec["project"]):
        raise ValueError("invalid recipient network")
    network=spec["project"]+"_default"
    labels={"com.docker.compose.project":spec["project"],"com.docker.compose.network":"default",
        "io.launchplane.production-recipient":spec["binding"]}
    names=docker("network","ls","--format","{{.Name}}").splitlines()
    if network not in names:
        args=["network","create","--driver","bridge","--ipv6=false"]
        for key,value in labels.items():
            args.extend(["--label",key+"="+value])
        docker(*args,network)
    observed=json.loads(docker("network","inspect",network))
    if len(observed)!=1 or observed[0].get("Driver")!="bridge" or observed[0].get("EnableIPv6") is not False or observed[0].get("Options") or any((observed[0].get("Labels") or {}).get(k)!=v for k,v in labels.items()):
        raise ValueError("recipient network unqualified")
    for container in (observed[0].get("Containers") or {}):
        rows=json.loads(docker("inspect",container))
        if len(rows)!=1 or rows[0].get("Config",{}).get("Labels",{}).get("com.docker.compose.project")!=spec["project"] or rows[0].get("Config",{}).get("Labels",{}).get("com.docker.compose.service") not in spec.get("allow_services",[]):
            raise ValueError("recipient network has an unqualified consumer")
    for connection in spec["connections"]:
        settings=connection["settings"]
        name="lp-upstream-"+uuid.uuid4().hex
        label="io.launchplane.upstream-probe="+name
        identifier=""
        try:
            identifier=docker("create","--name",name,"--label",label,"--network",network,
            "--read-only","--cap-drop","ALL","--security-opt","no-new-privileges",
            "--memory","128m","--cpus","0.25","--env","MYSQL_PWD","--entrypoint","mariadb",spec["image"],
            "--no-defaults","--protocol=TCP","--batch","--raw","--skip-column-names","--connect-timeout=3",
            "--ssl-verify-server-cert","--host="+settings["host"],
            "--port="+settings["port"],"--user="+settings["user"],
            "--database="+settings["database"],"--execute=SHOW GRANTS",
            env={**os.environ,"MYSQL_PWD":settings["password"]}).strip()
            grants=docker("start","--attach",identifier)
            status=json.loads(docker("inspect",identifier))[0]
            if status["State"]["ExitCode"]!=0 or classify_grants([(line,) for line in grants.splitlines()]) != "read_only":
                raise ValueError("unqualified grants")
        finally:
            # Even a lost create reply retains an exact unguessable owned name.
            rows=docker("ps","-aq","--filter","name=^/"+name+"$").splitlines()
            for candidate in rows:
                owned=json.loads(docker("inspect",candidate))[0]
                if owned["Config"]["Labels"].get("io.launchplane.upstream-probe")!=name:
                    raise ValueError("probe cleanup uncertain")
                docker("rm","-f",candidate)
    print("recipient_upstreams_binding="+spec["binding"])
    print("recipient_upstreams_ok=true")
except Exception:
    print("recipient_upstreams_ok=false")
    sys.exit(1)
"""


RECOVERY_LIBRARY = r"""
import fcntl, os, shutil, stat, tarfile, tempfile, time, uuid
from contextlib import contextmanager

def file_digest(path):
    digest=hashlib.sha256()
    with path.open('rb') as source:
        for block in iter(lambda: source.read(65536), b''):
            digest.update(block)
    return digest.hexdigest()

def manifest(root):
    result=[]
    size=0
    for path in sorted(root.rglob('*')):
        info=path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or (stat.S_ISREG(info.st_mode) and info.st_nlink>1):
            raise ValueError('unsupported storage entry')
        size+=info.st_size
        if size>2*1024**3 or len(result)>=100000:
            raise ValueError('recovery bound exceeded')
        digest=file_digest(path) if path.is_file() else ''
        result.append([str(path.relative_to(root)),stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid,digest])
    return result

def manifest_digest(paths):
    return hashlib.sha256(json.dumps([manifest(path) for path in paths],sort_keys=True,separators=(',',':')).encode()).hexdigest()

@contextmanager
def recovery_directory(spec,base=None):
    base=base or Path('/var/lib/launchplane/recipient-recovery')
    if base.is_symlink() or any(path.is_symlink() for path in base.parents):
        raise ValueError('invalid custody')
    base.mkdir(mode=0o700,parents=True,exist_ok=True)
    destination=base/spec['recovery_binding']
    if destination.is_symlink():
        raise ValueError('invalid custody')
    destination.mkdir(mode=0o700,exist_ok=True)
    descriptor=os.open(destination/'.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        yield destination
    finally:
        os.close(descriptor)

def load_outcome(destination,spec):
    path=destination/'outcome.json'
    if not path.exists():
        return None
    if path.is_symlink():
        raise ValueError('invalid outcome')
    receipt=json.loads(path.read_text())
    if receipt.get('binding')!=spec['recovery_binding']:
        raise ValueError('outcome binding changed')
    if receipt.get('state')=='passed':
        relative=receipt.get('archive','')
        if not re.fullmatch(r'attempt-[a-z0-9_]+/capture\.tar',relative):
            raise ValueError('invalid capture reference')
        archive=destination/relative
        if archive.is_symlink() or archive.parent.is_symlink() or file_digest(archive)!=receipt['capture_sha256']:
            raise ValueError('capture changed')
        if type(receipt.get('empty')) is not bool or type(receipt.get('epoch')) is not int or receipt['epoch']<=0:
            raise ValueError('invalid recovery evidence')
    elif receipt.get('state')!='failed_safe':
        raise ValueError('recovery remains uncertain')
    return receipt

def emit_outcome(receipt):
    print('recipient_recovery_binding='+receipt['binding'])
    if receipt['state']=='failed_safe':
        print('recipient_recovery_retry_safe=true')
        return
    print('recipient_recovery_capture='+receipt['capture_sha256'])
    print('recipient_recovery_empty='+str(receipt['empty']).lower())
    print('recipient_recovery_epoch='+str(receipt['epoch']))
    print('recipient_recovery_ok=true')

def save_outcome(destination,attempt,receipt):
    with (attempt/'receipt.json').open('x') as output:
        json.dump(receipt,output)
        output.flush()
        os.fsync(output.fileno())
    write_outcome_pointer(destination,receipt)

def write_outcome_pointer(destination,receipt):
    temporary=destination/('outcome-'+uuid.uuid4().hex+'.tmp')
    with temporary.open('x') as output:
        json.dump(receipt,output)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary,destination/'outcome.json')
    descriptor=os.open(destination,os.O_RDONLY|os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)

def inspect_recovery(spec,*,base=None):
    with recovery_directory(spec,base) as destination:
        receipt=load_outcome(destination,spec)
        if receipt is None:
            raise ValueError('no terminal recovery witness')
        emit_outcome(receipt)

def recover(paths,spec,*,base=None):
    with recovery_directory(spec,base) as destination:
        previous=load_outcome(destination,spec)
        if previous and previous['state']=='passed':
            if manifest_digest(paths)!=previous['source_manifest_sha256']:
                raise ValueError('captured source changed')
            emit_outcome(previous)
            return
        if previous is None and any(path.name!='.lock' for path in destination.iterdir()):
            # No inference from age or absence after a possibly interrupted writer.
            raise ValueError('unwitnessed recovery attempt')
        attempt=Path(tempfile.mkdtemp(prefix='attempt-',dir=destination))
        # Invalidate an earlier failed-safe witness before this attempt can have
        # effects. A crash must never expose a previous attempt's retry permission.
        write_outcome_pointer(destination,{'state':'running','binding':spec['recovery_binding']})
        scratch=None
        container='lp-recovery-'+uuid.uuid4().hex
        cleanup_verified=True
        try:
            before=[manifest(path) for path in paths]
            if before[0] and not (paths[0]/'mysql').is_dir():
                raise ValueError('storage is neither empty nor cold MariaDB')
            source_manifest=manifest_digest(paths)
            size=sum(path.stat().st_size for root in paths for path in root.rglob('*') if path.is_file())
            required=2*size+2048*sum(len(rows) for rows in before)+2*1024**3
            if shutil.disk_usage(destination).free<required:
                raise ValueError('insufficient independent recovery space')
            archive=attempt/'capture.tar'
            with tarfile.open(archive,'x') as output:
                for role,path in zip(('database','config'),paths):
                    output.add(path,arcname=role,recursive=True)
            archive_digest=file_digest(archive)
            if source_manifest!=manifest_digest(paths) or paths!=main():
                raise ValueError('source changed during capture')
            scratch=Path(tempfile.mkdtemp(prefix='restore-',dir=attempt))
            with tarfile.open(archive,'r') as captured:
                captured.extractall(scratch,filter='fully_trusted')
            if before!=[manifest(scratch/role) for role in ('database','config')]:
                raise ValueError('recovered data mismatch')
            cleanup_verified=False
            identifier=docker('create','--name',container,'--network','none',
                '--label','io.launchplane.recipient-recovery='+spec['recovery_binding'],
                '--memory','2g','--cpus','1','--security-opt','no-new-privileges',
                '--mount','type=bind,src='+str(scratch/'database')+',dst=/var/lib/mysql',
                '--entrypoint','sh',spec['database_image'],'-ec',
                'if [ ! -d /var/lib/mysql/mysql ]; then mariadb-install-db --no-defaults --user=mysql --datadir=/var/lib/mysql --skip-test-db >/dev/null 2>&1; fi; '
                'exec mariadbd --no-defaults --datadir=/var/lib/mysql --user=mysql --skip-networking --skip-grant-tables --skip-slave-start --event-scheduler=OFF --skip-log-bin').strip()
            try:
                docker('start',identifier)
                deadline=time.monotonic()+90
                while True:
                    status=subprocess.run(['docker','exec',identifier,'mariadb','--no-defaults','--protocol=socket','--user=root','--execute=SELECT 1'],capture_output=True,timeout=5)
                    if status.returncode==0:
                        break
                    if time.monotonic()>=deadline:
                        raise ValueError('database recovery uncertain')
                    time.sleep(.25)
                # CHECK TABLE can report corruption while mariadb-check exits zero.
                catalog=docker('exec',identifier,'mariadb','--no-defaults','--protocol=socket','--user=root',
                    '--batch','--skip-column-names','--execute=SELECT COUNT(*) FROM mysql.global_priv').strip()
                if not catalog.isdecimal() or int(catalog)<=0:
                    raise ValueError('database initialization is incomplete')
                if docker('exec',identifier,'mariadb-check','--no-defaults','--protocol=socket','--user=root','--all-databases','--check','--silent').strip():
                    raise ValueError('database integrity unverified')
                observed=json.loads(docker('inspect',identifier))[0]
                if observed['HostConfig']['NetworkMode']!='none' or observed['Config'].get('Env')!=json.loads(docker('image','inspect',spec['database_image']))[0]['Config'].get('Env'):
                    raise ValueError('recovery isolation unverified')
                if source_manifest!=manifest_digest(paths):
                    raise ValueError('source changed during recovery')
            finally:
                owned=json.loads(docker('inspect',container))[0]
                if owned['Config']['Labels'].get('io.launchplane.recipient-recovery')!=spec['recovery_binding']:
                    raise ValueError('cleanup ownership uncertain')
                docker('rm','-f',container)
                cleanup_verified=True
            shutil.rmtree(scratch)
            scratch=None
            receipt={'state':'passed','binding':spec['recovery_binding'],'capture_sha256':archive_digest,
                'archive':str(archive.relative_to(destination)),'source_manifest_sha256':source_manifest,
                'empty':not bool(before[0]),'epoch':int(time.time())}
            save_outcome(destination,attempt,receipt)
            emit_outcome(receipt)
        except Exception:
            if cleanup_verified:
                if scratch is not None:
                    shutil.rmtree(scratch)
                save_outcome(destination,attempt,{'state':'failed_safe','binding':spec['recovery_binding']})
            # Uncertain containers/captures retain custody without a retry witness.
            raise
"""

RECOVERY_PROGRAM = (
    RESOURCE_LIBRARY
    + RECOVERY_LIBRARY
    + r"""
try:
    spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
    if not re.fullmatch(r"[a-f0-9]{64}",spec["recovery_binding"]) or not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}",spec["database_image"]):
        raise ValueError("invalid recovery identity")
    if spec.get('inspect', False):
        inspect_recovery(spec)
    else:
        recover(main(),spec)
except Exception:
    print("recipient_recovery_ok=false")
    sys.exit(1)
"""
)
