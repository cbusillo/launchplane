import json

from control_plane.dokploy.slot_maintenance import ContainerCommand


RESTORE_PROBE_PROGRAM = r"""
import json,subprocess,sys,time
spec=json.load(sys.stdin)
def run(argv,**kwargs):
    result=subprocess.run(['docker',*argv],capture_output=True,**kwargs)
    if result.returncode: raise RuntimeError('isolated backup restore probe failed')
    return result.stdout
name=spec['container']; owner=spec['record']
images=json.loads(run(['image','inspect',spec['image']]))
if len(images)!=1: raise ValueError('restore probe image is unavailable')
existing=json.loads(run(['ps','-a','--filter','name=^/'+name+'$','--format','{{json .}}']).decode().strip() or 'null')
if existing:
    value=json.loads(run(['inspect',name]))[0]
    if value['Config']['Labels'].get('launchplane.cutover-restore')!=owner or value['Config']['Image']!=spec['image'] or value['HostConfig']['NetworkMode']!='none' or any(mount.get('Type')!='tmpfs' for mount in value.get('Mounts',[])):
        raise ValueError('restore probe name is owned by another resource')
    run(['rm','-f',name])
run(['create','--name',name,'--label','launchplane.cutover-restore='+owner,'--network','none','--user','postgres','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges:true','--tmpfs','/var/lib/postgresql:rw,mode=1777','--tmpfs','/var/lib/postgresql/data:rw,mode=1777','--tmpfs','/tmp:rw,mode=1777','--tmpfs','/var/run/postgresql:rw,mode=1777','-e','PGDATA=/tmp/data','-e','POSTGRES_HOST_AUTH_METHOD=trust',spec['image']])
try:
    run(['start',name])
    deadline=time.monotonic()+60
    while subprocess.run(['docker','exec',name,'pg_isready','-U','postgres'],stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL).returncode:
        if time.monotonic()>=deadline: raise TimeoutError('isolated database did not become ready')
        time.sleep(.5)
    if run(['exec',spec['runner'],'sha256sum',spec['dump']]).decode().split()[0]!=spec['database_dump_sha256']: raise ValueError('restore input differs from verified capture')
    source=subprocess.Popen(['docker','exec',spec['runner'],'cat',spec['dump']],stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
    try:
        restored=subprocess.run(['docker','exec','-i',name,'pg_restore','--exit-on-error','--no-owner','--no-acl','-U','postgres','-d','postgres'],stdin=source.stdout,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=300)
        source.stdout.close()
        if source.wait(timeout=10) or restored.returncode: raise RuntimeError('logical archive could not be restored')
    finally:
        if source.poll() is None: source.kill();source.wait()
    if run(['exec',spec['runner'],'sha256sum',spec['dump']]).decode().split()[0]!=spec['database_dump_sha256']: raise ValueError('restore input changed during verification')
    observed=run(['exec',name,'psql','-U','postgres','-d','postgres','-Atc',"SELECT value FROM ir_config_parameter WHERE key='database.uuid'"]).decode().strip()
    if observed!=spec['database_uuid']: raise ValueError('restored database identity differs')
    print(json.dumps({'restored_database_uuid':observed,'database_dump_sha256':spec['database_dump_sha256']},sort_keys=True))
finally:
    run(['rm','-f',name])
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
) -> dict[str, str]:
    request = dict(
        image=image,
        runner=runner,
        record=record_id,
        container=record_id + "-restore-probe",
        dump=dump,
        database_uuid=database_uuid,
        database_dump_sha256=dump_sha256,
    )
    value: dict[str, str] = json.loads(
        execute(("python3", "-c", RESTORE_PROBE_PROGRAM), json.dumps(request))
    )
    if value != {"restored_database_uuid": database_uuid, "database_dump_sha256": dump_sha256}:
        raise ValueError("isolated restore has no exact database/capture witness")
    return value
