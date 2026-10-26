import json

from control_plane.dokploy.slot_maintenance import ContainerCommand


RESTORE_PROBE_PROGRAM = r"""
import json,re,subprocess,sys,time
spec=json.load(sys.stdin)
def run(argv,**kwargs):
    result=subprocess.run(['docker',*argv],capture_output=True,**kwargs)
    if result.returncode: raise RuntimeError('isolated backup restore probe failed')
    return result.stdout
name=spec['container']; owner=spec['record']; volume=name+'-data'; deadline_seconds=spec['timeout_seconds']
images=json.loads(run(['image','inspect',spec['image']]))
if len(images)!=1: raise ValueError('restore probe image is unavailable')
existing=json.loads(run(['ps','-a','--filter','name=^/'+name+'$','--format','{{json .}}']).decode().strip() or 'null')
if existing:
    value=json.loads(run(['inspect',name]))[0]
    if value['Config']['Labels'].get('launchplane.cutover-restore')!=owner or value['Config']['Image']!=spec['image'] or value['HostConfig']['NetworkMode']!='none' or any(mount.get('Type') not in ('tmpfs','volume') or mount.get('Type')=='volume' and (mount.get('Name')!=volume or mount.get('Destination')!='/var/lib/postgresql/data') for mount in value.get('Mounts',[])):
        raise ValueError('restore probe name is owned by another resource')
    run(['rm','-f',name])
volumes=json.loads(run(['volume','ls','--filter','name=^'+volume+'$','--format','{{json .}}']).decode().strip() or 'null')
if volumes:
    value=json.loads(run(['volume','inspect',volume]))[0]
    if value.get('Labels',{}).get('launchplane.cutover-restore')!=owner: raise ValueError('restore volume belongs to another resource')
    run(['volume','rm',volume])
run(['volume','create','--label','launchplane.cutover-restore='+owner,volume])
run(['create','--name',name,'--label','launchplane.cutover-restore='+owner,'--network','none','--user','postgres','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--tmpfs','/var/lib/postgresql:rw,mode=1777','--mount','type=volume,src='+volume+',dst=/var/lib/postgresql/data','--tmpfs','/tmp:rw,mode=1777','--tmpfs','/var/run/postgresql:rw,mode=1777','-e','PGDATA=/var/lib/postgresql/data/pgdata','-e','POSTGRES_HOST_AUTH_METHOD=trust',spec['image']])
try:
    run(['start',name])
    deadline=time.monotonic()+60
    while subprocess.run(['docker','exec',name,'pg_isready','-U','postgres'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
        if time.monotonic()>=deadline: raise TimeoutError('isolated database did not become ready')
        time.sleep(.5)
    if run(['exec',spec['runner'],'sha256sum',spec['dump']]).decode().split()[0]!=spec['database_dump_sha256']: raise ValueError('restore input differs from verified capture')
    source=subprocess.Popen(['docker','exec',spec['runner'],'cat',spec['dump']],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        restored=subprocess.run(['docker','exec','-i',name,'pg_restore','--exit-on-error','--no-owner','--no-acl','-U','postgres','-d','postgres'],stdin=source.stdout,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=deadline_seconds)
        source.stdout.close()
        if source.wait(timeout=10) or restored.returncode: raise RuntimeError('logical archive could not be restored')
    finally:
        if source.poll() is None: source.kill();source.wait()
    if run(['exec',spec['runner'],'sha256sum',spec['dump']]).decode().split()[0]!=spec['database_dump_sha256']: raise ValueError('restore input changed during verification')
    if run(['exec',spec['runner'],'sha256sum',spec['filestore']]).decode().split()[0]!=spec['filestore_sha256']: raise ValueError('filestore differs from verified capture')
    run(['exec',name,'mkdir','/var/lib/postgresql/data/restore-filestore'])
    source=subprocess.Popen(['docker','exec',spec['runner'],'cat',spec['filestore']],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        extracted=subprocess.run(['docker','exec','-i',name,'tar','--no-same-owner','--no-same-permissions','-xzf','-','-C','/var/lib/postgresql/data/restore-filestore'],stdin=source.stdout,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=deadline_seconds)
        source.stdout.close()
        if source.wait(timeout=10) or extracted.returncode: raise RuntimeError('filestore archive could not be restored')
    finally:
        if source.poll() is None: source.kill();source.wait()
    attachments=run(['exec',name,'psql','-U','postgres','-d','postgres','-At','-F','|','-c',"SELECT store_fname,checksum FROM ir_attachment WHERE store_fname IS NOT NULL"]).decode().splitlines()
    checks=[]
    for attachment in attachments:
        path,checksum=attachment.split('|')
        if not re.fullmatch(r'[0-9a-f]{2}/[0-9a-f]{40}',path) or not re.fullmatch(r'[0-9a-f]{40}',checksum): raise ValueError('restored attachment has no verifiable filestore binding')
        checks.append(checksum+'  /var/lib/postgresql/data/restore-filestore/'+spec['database']+'/'+path+'\n')
    if checks: run(['exec','-i',name,'sha1sum','--check','--status'],input=''.join(checks).encode())
    if run(['exec',spec['runner'],'sha256sum',spec['filestore']]).decode().split()[0]!=spec['filestore_sha256']: raise ValueError('filestore input changed during verification')
    observed=run(['exec',name,'psql','-U','postgres','-d','postgres','-Atc',"SELECT value FROM ir_config_parameter WHERE key='database.uuid'"]).decode().strip()
    if observed!=spec['database_uuid']: raise ValueError('restored database identity differs')
    print(json.dumps({'restored_database_uuid':observed,'database_dump_sha256':spec['database_dump_sha256'],'filestore_archive_sha256':spec['filestore_sha256']},sort_keys=True))
finally:
    run(['rm','-f',name])
    run(['volume','rm',volume])
"""


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
) -> dict[str, str]:
    request = dict(
        timeout_seconds=timeout_seconds,
        image=image,
        runner=runner,
        record=record_id,
        container=record_id + "-restore-probe",
        dump=dump,
        database_uuid=database_uuid,
        database_dump_sha256=dump_sha256,
        database=database,
        filestore=filestore,
        filestore_sha256=filestore_sha256,
    )
    value: dict[str, str] = json.loads(
        execute(("python3", "-c", RESTORE_PROBE_PROGRAM), json.dumps(request))
    )
    if value != {
        "restored_database_uuid": database_uuid,
        "database_dump_sha256": dump_sha256,
        "filestore_archive_sha256": filestore_sha256,
    }:
        raise ValueError("isolated restore has no exact database/capture witness")
    return value
