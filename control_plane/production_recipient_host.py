"""Fixed destination programs; no product command, credential export or guest restore."""

import base64
import inspect
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from control_plane import odoo_source_probe
from control_plane.contracts.production_recipient import ProductionRecipientRecord
from control_plane.dokploy import api, post_deploy, source
from control_plane.production_recipient_resources import RESOURCE_LIBRARY


def run_recipient_program(
    root: Path,
    record: ProductionRecipientRecord,
    spec: dict[str, Any],
    program: str,
    checkpoint: Callable[[str], None],
) -> dict[str, str]:
    host, token = source.read_dokploy_config(control_plane_root=root)
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=record.target_id
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
    schedule_payload: api.JsonObject = {
        "name": "Launchplane isolated recipient qualification",
        "cronExpression": post_deploy.DOKPLOY_MANUAL_ONLY_CRON_EXPRESSION,
        "appName": record.namespace + "-qualification",
        "shellType": "bash",
        "scheduleType": kind,
        "command": "control-plane recipient qualification",
        "script": "#!/usr/bin/env bash\nset -euo pipefail\npython3 - "
        + encoded
        + " <<'LP_QUALIFY'\n"
        + program
        + "\nLP_QUALIFY\n",
        "serverId": server,
        "userId": lookup if kind == "dokploy-server" else None,
        "enabled": False,
        "timezone": "UTC",
    }
    try:
        return post_deploy.run_manual_data_schedule(
            host=host,
            token=token,
            target_definition=source.DokployTargetDefinition(
                context=record.context, instance="prod", target_id=record.target_id
            ),
            schedule_lookup_id=lookup,
            schedule_type=kind,
            schedule_name="Launchplane isolated recipient qualification",
            schedule_app_name=record.namespace + "-qualification",
            schedule_payload=schedule_payload,
            resolved_schedule_execution_timeout_seconds=300,
            before_provider_mutation=checkpoint,
        )
    finally:
        if spec.get("connections"):
            # A manual-only schedule must not retain a second copy of its
            # managed credential inputs after the probe. Cleanup is leased too.
            checkpoint("scrub_upstream_probe")
            api.upsert_dokploy_schedule(
                host=host,
                token=token,
                target_id=lookup,
                schedule_type=kind,
                schedule_name="Launchplane isolated recipient qualification",
                app_name=record.namespace + "-qualification",
                schedule_payload={**schedule_payload, "script": "#!/bin/sh\nexit 1\n"},
            )


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
    if len(observed)!=1 or observed[0].get("Driver")!="bridge" or observed[0].get("EnableIPv6") is not False or observed[0].get("Options") or observed[0].get("Containers") or any((observed[0].get("Labels") or {}).get(k)!=v for k,v in labels.items()):
        raise ValueError("recipient network unqualified")
    for connection in spec["connections"]:
        settings=connection["settings"]
        name="lp-upstream-"+uuid.uuid4().hex
        label="io.launchplane.upstream-probe="+name
        identifier=""
        try:
            identifier=docker("create","--name",name,"--label",label,"--network",network,
            "--read-only","--cap-drop","ALL","--security-opt","no-new-privileges",
            "--memory","128m","--cpus","0.25","--env","MYSQL_PWD",spec["image"],
            "mariadb","--batch","--raw","--skip-column-names","--connect-timeout=3",
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
import os, shutil, stat, tarfile, tempfile, time

def manifest(root):
    result=[]
    size=0
    for path in sorted(root.rglob("*")):
        info=path.lstat()
        if not (stat.S_ISREG(info.st_mode) or stat.S_ISDIR(info.st_mode)) or info.st_nlink>1 and path.is_file():
            raise ValueError("unsupported storage entry")
        size+=info.st_size
        if size>2*1024**3 or len(result)>=100000:
            raise ValueError("recovery bound exceeded")
        digest=hashlib.sha256(path.read_bytes()).hexdigest() if path.is_file() else ""
        result.append([str(path.relative_to(root)),stat.S_IMODE(info.st_mode),info.st_uid,info.st_gid,digest])
    return result

def recover(paths, spec, *, base=None):
    # Original-key exclusive archive custody survives errors and lost replies.
    base=base or Path("/var/lib/launchplane/recipient-recovery")
    if base.is_symlink() or any(path.is_symlink() for path in base.parents):
        raise ValueError("invalid custody")
    base.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination=base/spec["recovery_binding"]
    destination.mkdir(mode=0o700)
    before=[manifest(path) for path in paths]
    if before[0] and not (paths[0]/"mysql").is_dir():
        raise ValueError("storage is neither empty nor a cold MariaDB database")
    archive=destination/"capture.tar"
    with tarfile.open(archive,"x") as output:
        for role,path in zip(("database","config"),paths):
            output.add(path,arcname=role,recursive=True)
    archive_digest=hashlib.sha256(archive.read_bytes()).hexdigest()
    if before != [manifest(path) for path in paths]:
        raise ValueError("source changed during capture")
    # Re-read every mount/port and volume identity after capture, before restore.
    if paths != main():
        raise ValueError("source identity changed")
    scratch=Path(tempfile.mkdtemp(prefix="restore-",dir=destination))
    container="lp-recovery-"+spec["recovery_binding"][:32]
    created=False
    try:
        with tarfile.open(archive,"r") as captured:
            captured.extractall(scratch,filter="fully_trusted")
        if before != [manifest(scratch/role) for role in ("database","config")]:
            raise ValueError("recovered data mismatch")
        # Only the database image runs. No app, live keys, published ports,
        # product command/entrypoint or network is carried into this verifier.
        created=True
        identifier=docker("create","--name",container,"--network","none",
            "--label","io.launchplane.recipient-recovery="+spec["recovery_binding"],
            "--memory","2g","--cpus","1","--security-opt","no-new-privileges",
            "--mount","type=bind,src="+str(scratch/"database")+",dst=/var/lib/mysql",
            "--entrypoint","sh",spec["database_image"],"-ec",
            "if [ ! -d /var/lib/mysql/mysql ]; then mariadb-install-db --user=mysql --datadir=/var/lib/mysql --skip-test-db >/dev/null 2>&1; fi; "
            "exec mariadbd --user=mysql --skip-networking --skip-grant-tables --skip-slave-start --event-scheduler=OFF --skip-log-bin").strip()
        docker("start",identifier)
        deadline=time.monotonic()+90
        while True:
            status=subprocess.run(["docker","exec",identifier,"mariadb","--protocol=socket",
                "--user=root","--execute=SELECT 1"],capture_output=True,timeout=5)
            if status.returncode==0:
                break
            if time.monotonic()>=deadline:
                raise ValueError("database recovery uncertain")
            time.sleep(0.25)
        docker("exec",identifier,"mariadb-check","--protocol=socket","--user=root",
            "--all-databases","--check")
        observed=json.loads(docker("inspect",identifier))[0]
        if observed["HostConfig"]["NetworkMode"]!="none" or observed["Config"].get("Env") != json.loads(docker("image","inspect",spec["database_image"]))[0]["Config"].get("Env"):
            raise ValueError("recovery isolation unverified")
        if before != [manifest(path) for path in paths]:
            raise ValueError("source changed during recovery")
    finally:
        if created:
            observed=json.loads(docker("inspect",container))[0]
            if observed["Config"]["Labels"].get("io.launchplane.recipient-recovery")!=spec["recovery_binding"]:
                raise ValueError("cleanup ownership uncertain")
            docker("rm","-f",container)
        shutil.rmtree(scratch)
    receipt={"binding":spec["recovery_binding"],"capture_sha256":archive_digest}
    with (destination/"receipt.json").open("x") as output:
        json.dump(receipt,output)
    print("recipient_recovery_capture="+archive_digest)
    print("recipient_recovery_binding="+spec["recovery_binding"])
    print("recipient_recovery_ok=true")
"""

RECOVERY_PROGRAM = (
    RESOURCE_LIBRARY
    + RECOVERY_LIBRARY
    + r"""
try:
    spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
    if not re.fullmatch(r"[a-f0-9]{64}",spec["recovery_binding"]) or not re.fullmatch(r"[a-zA-Z0-9./:_-]+@sha256:[a-f0-9]{64}",spec["database_image"]):
        raise ValueError("invalid recovery identity")
    recover(main(),spec)
except Exception:
    print("recipient_recovery_ok=false")
    sys.exit(1)
"""
)
