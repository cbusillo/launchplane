import json
from typing import Any

from control_plane.dokploy.slot_maintenance import ContainerCommand
from control_plane.dokploy.cutover_restore_state import (
    RESTORE_STATE_PROGRAM,
    RESTORE_CANCEL_PROGRAM,
)
from control_plane.odoo_cutover_recovery import BackupVerificationRejected


RESTORE_PREFLIGHT_PROGRAM = r"""
import json,subprocess,sys
spec=json.load(sys.stdin)
inspected=subprocess.run(['docker','image','inspect',spec['image']],capture_output=True,timeout=30)
if inspected.returncode:
    pulled=subprocess.run(['docker','pull',spec['image']],capture_output=True,timeout=max(1,spec.get('timeout_seconds',300)-60))
    if pulled.returncode: raise ValueError('immutable restore artifact could not be acquired')
    inspected=subprocess.run(['docker','image','inspect',spec['image']],capture_output=True,timeout=30)
if inspected.returncode or len(json.loads(inspected.stdout))!=1: raise ValueError('immutable restore artifact is unavailable')
command=['docker','run','--rm','--pull','never','--network','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--entrypoint','timeout',spec['image'],'--signal=KILL','30','/bin/sh','-ec',"command -v timeout; command -v docker-entrypoint.sh; command -v postgres; command -v pg_isready; command -v pg_restore; command -v psql; command -v tar; command -v sha1sum; command -v df; pg_restore --version; timeout --signal=KILL 1 sh -c 'exit 0'"]
result=subprocess.run(command,capture_output=True,timeout=40)
if result.returncode: raise ValueError('immutable restore artifact lacks bounded native tools')
versions=[line for line in result.stdout.decode().splitlines() if line.startswith('pg_restore ')]
if len(versions)!=1 or int(versions[0].split()[2].split('.')[0])!=spec['dump_major']: raise ValueError('restore tools differ from capture PostgreSQL major')
print(json.dumps({'restore_tools':True}))
"""


RESTORE_PROBE_PROGRAM = (
    RESTORE_STATE_PROGRAM
    + r"""
import time
cleanup_seconds=min(30,max(2,spec['timeout_seconds']//5))
work_seconds=spec['timeout_seconds']-cleanup_seconds-2
if work_seconds<1: raise ValueError('restore budget has no reserved cleanup time')
deadline=time.monotonic()+work_seconds
container=None
allocation_started=False
cleaned=False
failure=None
result=None
class RestoreCommandFailed(RuntimeError): pass
def remaining():
    value=deadline-time.monotonic()
    if value<=0: raise TimeoutError('isolated restore work deadline expired')
    return value
def run(argv,**kwargs):
    response=subprocess.run(['docker',*argv],capture_output=True,timeout=remaining(),**kwargs)
    if response.returncode: raise RestoreCommandFailed('isolated backup restore probe failed')
    return response.stdout
def pipe(source_command,destination):
    source=subprocess.Popen(['docker',*source_command],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        response=subprocess.run(['docker',*destination],stdin=source.stdout,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=remaining())
        source.stdout.close()
        if source.wait(timeout=remaining()) or response.returncode: raise RuntimeError('isolated restore stream failed')
    finally:
        if source.poll() is None:
            source.kill()
            source.wait(timeout=cleanup_seconds)
previous=restore_outcome()
if previous is not None:
    print(json.dumps(previous));sys.exit(0)
seal_receipt('intent',dispatch_protocol=True)
try:
    source_size=int(run(['exec','-e','DATABASE_NAME='+spec['database'],spec['runner'],'/venv/bin/python','-c',"import os,psycopg2; c=psycopg2.connect(host=os.environ['ODOO_DB_HOST'],port=os.environ.get('ODOO_DB_PORT','5432'),dbname=os.environ['DATABASE_NAME'],user=os.environ['ODOO_DB_USER'],password=os.environ['ODOO_DB_PASSWORD'],connect_timeout=10); q=c.cursor();q.execute('SELECT pg_database_size(current_database()) + COALESCE(sum(file_size),0) FROM ir_attachment');print(q.fetchone()[0])"]).decode().strip())
    if seal_receipt('dispatch',state='allocating').get('state')!='allocating' or abandonment() is not None: raise RuntimeError('restore allocation was cancelled')
    allocation_started=True
    container=run(['run','-d','--rm','--pull','never','--name',spec['record']+'-restore-probe','--label','launchplane.cutover-restore='+spec['record'],'--label','launchplane.cutover-restore-request='+request_digest,'--network','none','--user','postgres','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--tmpfs','/var/lib/postgresql:rw,mode=1777','--mount','type=volume,dst=/var/lib/postgresql/data','--tmpfs','/tmp:rw,mode=1777','--tmpfs','/var/run/postgresql:rw,mode=1777','-e','PGDATA=/var/lib/postgresql/data/pgdata','-e','POSTGRES_HOST_AUTH_METHOD=trust','--entrypoint','timeout',spec['image'],'--signal=KILL',str(work_seconds),'docker-entrypoint.sh','postgres']).decode().strip()
    if not re.fullmatch('[0-9a-f]{64}',container): raise ValueError('restore allocation has no exact container identity')
    allocation=seal_receipt('allocation',container=container)
    if allocation['container']!=container: raise ValueError('restore allocation changed')
    if abandonment() is not None: raise RuntimeError('isolated restore was abandoned')
    available=int(run(['exec',container,'df','-PB1','/var/lib/postgresql/data']).decode().splitlines()[-1].split()[3])
    if source_size<=0 or available < source_size*2: raise ValueError('isolated Docker storage lacks restore staging capacity')
    while True:
        response=subprocess.run(['docker','exec',container,'pg_isready','-h','127.0.0.1','-U','postgres'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=remaining())
        if response.returncode==0: break
        time.sleep(min(.5,remaining()))
    if run(['exec',spec['runner'],'sha256sum',spec['dump']]).decode().split()[0]!=spec['database_dump_sha256']: raise ValueError('restore input differs from verified capture')
    pipe(['exec',spec['runner'],'cat',spec['dump']],['exec','-i',container,'pg_restore','--exit-on-error','--no-owner','--no-acl','-U','postgres','-d','postgres'])
    if run(['exec',spec['runner'],'sha256sum',spec['dump']]).decode().split()[0]!=spec['database_dump_sha256']: raise ValueError('restore input changed during verification')
    if run(['exec',spec['runner'],'sha256sum',spec['filestore']]).decode().split()[0]!=spec['filestore_sha256']: raise ValueError('filestore differs from verified capture')
    run(['exec',container,'mkdir','/var/lib/postgresql/data/restore-filestore'])
    pipe(['exec',spec['runner'],'cat',spec['filestore']],['exec','-i',container,'tar','--no-same-owner','--no-same-permissions','-xzf','-','-C','/var/lib/postgresql/data/restore-filestore'])
    attachments=run(['exec',container,'psql','-U','postgres','-d','postgres','-At','-F','|','-c',"SELECT store_fname,checksum FROM ir_attachment WHERE store_fname IS NOT NULL"]).decode().splitlines()
    checks=[]
    for attachment in attachments:
        remaining()
        path,checksum=attachment.split('|')
        if not re.fullmatch(r'[0-9a-f]{2}/[0-9a-f]{40}',path) or not re.fullmatch(r'[0-9a-f]{40}',checksum): raise ValueError('restored attachment has no verifiable filestore binding')
        checks.append(checksum+'  /var/lib/postgresql/data/restore-filestore/'+spec['database']+'/'+path+'\n')
    if checks: run(['exec','-i',container,'sha1sum','--check','--status'],input=''.join(checks).encode())
    if run(['exec',spec['runner'],'sha256sum',spec['filestore']]).decode().split()[0]!=spec['filestore_sha256']: raise ValueError('filestore input changed during verification')
    observed=run(['exec',container,'psql','-U','postgres','-d','postgres','-Atc',"SELECT value FROM ir_config_parameter WHERE key='database.uuid'"]).decode().strip()
    if observed!=spec['database_uuid']: raise ValueError('restored database identity differs')
    result={'restored_database_uuid':observed,'database_dump_sha256':spec['database_dump_sha256'],'filestore_archive_sha256':spec['filestore_sha256']}
except Exception as error:
    failure=error
finally:
    if container and re.fullmatch('[0-9a-f]{64}',container):
        cleanup_deadline=time.monotonic()+cleanup_seconds
        removed=subprocess.run(['docker','rm','-f','-v',container],capture_output=True,timeout=max(.1,cleanup_deadline-time.monotonic())/2)
        inventory=subprocess.run(['docker','ps','-a','--no-trunc','--filter','id='+container,'--format','{{.ID}}'],capture_output=True,timeout=max(.1,cleanup_deadline-time.monotonic()))
        cleaned=inventory.returncode==0 and not inventory.stdout.strip()
    elif container is None:
        cleaned=not allocation_started
        if allocation_started and isinstance(failure,RestoreCommandFailed):
            ids=docker_read(['ps','-a','--no-trunc','--filter','label=launchplane.cutover-restore='+spec['record'],'--format','{{.ID}}']).strip()
            if ids: cleaned=restore_outcome()=={'status':'failed','terminated':True}
            else: cleaned=True
if failure is not None:
    if cleaned: print(json.dumps(abandonment() or seal_receipt('completed',result={'status':'failed','terminated':True})['result']))
    else: raise failure
elif not cleaned:
    raise RuntimeError('isolated restore cleanup is uncertain')
else:
    print(json.dumps(abandonment() or seal_receipt('completed',result=result)['result'],sort_keys=True))
"""
)


def verify_disposable_database_restore(
    execute: ContainerCommand,
    *,
    image: str,
    runner: str,
    record_id: str,
    dump: str,
    database_uuid: str,
    dump_sha256: str,
    database: str,
    filestore: str,
    filestore_sha256: str,
    timeout_seconds: int,
    authority_directory: str,
    capture_nonce: str,
    cancel_only: bool = False,
) -> dict[str, str]:
    request = dict(
        timeout_seconds=timeout_seconds,
        authority_directory=authority_directory,
        capture_nonce=capture_nonce,
        image=image,
        runner=runner,
        record=record_id,
        dump=dump,
        database_uuid=database_uuid,
        database_dump_sha256=dump_sha256,
        database=database,
        filestore=filestore,
        filestore_sha256=filestore_sha256,
    )
    program = RESTORE_CANCEL_PROGRAM if cancel_only else RESTORE_PROBE_PROGRAM
    value: dict[str, Any] = json.loads(execute(("python3", "-c", program), json.dumps(request)))
    if cancel_only:
        if value != {"cancelled": True}:
            raise ValueError("original restore cancellation lacks termination evidence")
        return {}
    if value == {"status": "failed", "terminated": True}:
        raise BackupVerificationRejected("isolated restore failed after verified termination")
    if value.get("status") == "abandoned" and value.get("allocation_state") in {
        "not_dispatched",
        "unknown_isolated",
    }:
        raise BackupVerificationRejected(
            "isolated verification was abandoned; no restore success is claimed"
        )
    if value != {
        "restored_database_uuid": database_uuid,
        "database_dump_sha256": dump_sha256,
        "filestore_archive_sha256": filestore_sha256,
    }:
        raise ValueError("isolated restore has no exact database/capture witness")
    return {key: str(item) for key, item in value.items()}
