import inspect
from typing import Any

from control_plane._odoo_copy_runtime_guard import COPY_RUNTIME_INSPECTION_LIBRARY
from control_plane.odoo_testing_restore import (
    TESTING_COPY_CREDENTIAL_LIBRARY,
    TESTING_COPY_COMPLETION_PARAMETER,
    FRESH_TESTING_STALE_LOCK_PROGRAM,
)


def offline_container_isolated(container: dict[str, Any], expected: dict[str, Any]) -> bool:
    configuration = container.get("Config") or {}
    host = container.get("HostConfig") or {}
    if (
        container.get("Image") != expected["image"]
        or configuration.get("Image") != expected["image"]
        or configuration.get("User") != expected["user"]
        or configuration.get("Entrypoint") != expected["entrypoint"]
        or configuration.get("Cmd") != expected["command"]
        or configuration.get("Labels", {}).get("io.launchplane.copy-qualification")
        != expected["operation"]
        or host.get("NetworkMode") != expected["network"]
        or host.get("Privileged") is not False
        or host.get("ReadonlyRootfs") is not True
        or host.get("RestartPolicy", {}).get("Name") != "no"
        or host.get("LogConfig", {}).get("Type") != "none"
        or host.get("LogConfig", {}).get("Config") not in (None, {})
        or set(host.get("CapDrop") or []) != {"ALL"}
        or host.get("CapAdd")
        or host.get("PidMode") not in (None, "", "private")
        or host.get("IpcMode") not in ("private", "none")
        or host.get("Devices")
        or host.get("DeviceRequests")
        or host.get("VolumesFrom")
        or host.get("Binds")
        or any((host.get("PortBindings") or {}).values())
        or set(host.get("SecurityOpt") or [])
        not in ({"no-new-privileges"}, {"no-new-privileges:true"})
    ):
        return False
    environment = configuration.get("Env")
    if not isinstance(environment, list) or any(
        not isinstance(item, str) or "=" not in item for item in environment
    ):
        return False
    parsed = dict(item.split("=", 1) for item in environment)
    if len(parsed) != len(environment) or parsed != expected["environment"]:
        return False
    mounts = container.get("Mounts")
    if not isinstance(mounts, list):
        return False
    observed = []
    for mount in mounts:
        if mount.get("Type") == "tmpfs" and mount.get("Destination") == "/tmp":
            continue
        if mount.get("Type") != "volume":
            return False
        observed.append({key: mount.get(key) for key in ("Type", "Name", "Destination", "RW")})
    temporary = host.get("Tmpfs")
    return (
        sorted(observed, key=lambda item: item["Destination"])
        == sorted(expected["mounts"], key=lambda item: item["Destination"])
        and isinstance(temporary, dict)
        and set(temporary) == {"/tmp"}
        and {"rw", "noexec", "nosuid", "nodev"} <= set(temporary["/tmp"].split(","))
    )


def original_container_stays_stopped(
    container: dict[str, Any], saved: dict[str, Any] | None
) -> bool:
    state = container.get("State") or {}
    policy = (container.get("HostConfig") or {}).get("RestartPolicy") or {}
    if (
        state.get("Running") is not False
        or state.get("Pid") != 0
        or state.get("Restarting")
        or state.get("Paused")
    ):
        return False
    if policy.get("Name") == "no":
        return True
    return (
        policy.get("Name") == "unless-stopped"
        and isinstance(saved, dict)
        and saved.get("ID") == container.get("Id")
        and saved.get("HasBeenManuallyStopped") is True
    )


COPY_QUALIFICATION_CLIENT = (
    r"""
import base64, importlib.abc, json, logging, os, sys
from pathlib import Path

class NoTargetRegistry(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == 'odoo' or fullname.startswith('odoo.'):
            raise RuntimeError('SQL copy qualification cannot import Odoo')

sys.meta_path.insert(0, NoTargetRegistry())
logging.disable(logging.CRITICAL)
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
os.environ.clear()
os.environ.update(spec['environment'])
sys.argv=['copy-client','unused',spec['database_name'],spec['readback_spec'],
    base64.b64encode(spec['readback_program'].encode()).decode(),'resume',spec['copy_binding_sha256'],spec['source_mode']]
"""
    + TESTING_COPY_CREDENTIAL_LIBRARY
    + "\nCOPY_COMPLETION_PARAMETER="
    + repr(TESTING_COPY_COMPLETION_PARAMETER)
    + r"""
from run_odoo_data_workflows import BOUNDARY_COMMIT_PARAMETER
from run_odoo_data_workflows import credential_parameter_integration
if any(credential_parameter_integration(key) is not None for key in spec.get('start_parameter_keys',[])):
    raise RuntimeError('The original runner can reapply a restored credential')
runner=CopyRunner(LocalServerSettings(),None,None)
if spec.get('recover_lock'):
    lock=runner.local.data_workflow_lock_file
    if 'launchplane_copy_qualification='+spec['operation']+'\n' not in lock.read_text():
        raise RuntimeError('The interrupted workflow lock belongs to another operation')
    saved_arguments=sys.argv
    try:
        sys.argv=['stale-lock',str(lock),'true','/volumes/data','/proc',spec['database_name'],spec['runner_healthcheck_command']]
        exec(compile(spec['stale_lock_program'],'<qualified-stale-lock>','exec'),{})
    finally:
        sys.argv=saved_arguments
runner.acquire_data_workflow_lock()
try:
    with runner.local.data_workflow_lock_file.open('a') as output:
        output.write('launchplane_copy_qualification='+spec['operation']+'\n')
        output.flush()
        os.fsync(output.fileno())
    connection=runner.connect_to_db()
    with connection.cursor() as cursor:
        cursor.execute('SELECT value FROM ir_config_parameter WHERE key=%s',(COPY_COMPLETION_PARAMETER,))
        if cursor.fetchone()!=(spec['copy_binding_sha256'],):
            raise RuntimeError('The original copy witness differs')
    connection.rollback()
    runner.validate_native_attachments()
    runner.prepare_credentials_before_registry(restored_copy=True,do_sanitize=True)
    observer=runner._connect_with_retry(runner.local.db_name)
    try:
        with observer.cursor() as cursor:
            cursor.execute('SET TRANSACTION READ ONLY')
            cursor.execute('SELECT value FROM ir_config_parameter WHERE key=%s',(BOUNDARY_COMMIT_PARAMETER,))
            marker=cursor.fetchone()
            if not marker or not isinstance(marker[0],str):
                raise RuntimeError('The credential boundary witness is missing')
        observer.rollback()
    finally:
        observer.close()
    print('copy_qualification_client='+json.dumps({'credential_boundary_commit':marker[0]}),flush=True)
finally:
    runner.release_data_workflow_lock()
"""
)


COPY_QUALIFICATION_HOST_PROGRAM = (
    "from typing import Any\n"
    + inspect.getsource(offline_container_isolated)
    + "\n"
    + inspect.getsource(original_container_stays_stopped)
    + COPY_RUNTIME_INSPECTION_LIBRARY
    + "\nCOPY_QUALIFICATION_CLIENT="
    + repr(COPY_QUALIFICATION_CLIENT)
    + "\nSTALE_LOCK_PROGRAM="
    + repr(FRESH_TESTING_STALE_LOCK_PROGRAM)
    + r"""
import base64, fcntl, hashlib, json, os, re, stat, subprocess, sys, tempfile, time
from pathlib import Path

def docker(*arguments,stdin=None,timeout=60):
    result=subprocess.run(['docker',*arguments],input=stdin,capture_output=True,timeout=timeout,check=False)
    if result.returncode:
        raise RuntimeError('copy_qualification_provider_unavailable')
    return result.stdout

def one_inspection(identifier):
    rows=json.loads(docker('inspect',identifier))
    if not isinstance(rows,list) or len(rows)!=1 or not isinstance(rows[0],dict):
        raise RuntimeError('copy_qualification_inventory_unavailable')
    return rows[0]

def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':')).encode()).hexdigest()

def owned_file(path,limit=4*1024*1024):
    if path.is_symlink() or path.resolve(strict=True)!=path.absolute():
        raise RuntimeError('copy_qualification_custody_unverified')
    metadata=path.stat()
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink!=1 or metadata.st_size>limit:
        raise RuntimeError('copy_qualification_custody_unverified')
    return path.read_bytes()

def original_snapshot(spec,temporary=None,allow_started=False):
    info=json.loads(docker('info','--format','{{json .}}'))
    if info.get('LiveRestoreEnabled') is not False:
        raise RuntimeError('copy_qualification_live_restore_unverified')
    root=Path(info['DockerRootDir']).resolve(strict=True)
    temporary=temporary or {}
    if not temporary:
        containers=inspect_copy_runtime(spec,expected_instance='testing')
    else:
        containers={service:(identifier,one_inspection(identifier))
            for service,identifier in spec['physical']['container_ids'].items()}
    all_containers=[one_inspection(identifier) for identifier in docker('ps','-aq','--no-trunc').decode().splitlines()]
    own={value[1]['Id'] for value in containers.values()}
    for service,(identifier,container) in containers.items():
        if not re.fullmatch('[a-f0-9]{64}',identifier) or container.get('Id')!=identifier:
            raise RuntimeError('copy_qualification_container_identity')
        if 'ExecIDs' not in container or container['ExecIDs'] not in (None,[]):
            raise RuntimeError('copy_qualification_active_exec')
        saved=None
        if container.get('HostConfig',{}).get('RestartPolicy',{}).get('Name')=='unless-stopped':
            saved=json.loads(owned_file(root/'containers'/identifier/'config.v2.json'))
        if not (allow_started and service in {'database','script-runner'}):
            if not original_container_stays_stopped(container,saved):
                raise RuntimeError('copy_qualification_original_stop_unverified')
    for identifier,expected in temporary.items():
        if not offline_container_isolated(one_inspection(identifier),expected):
            raise RuntimeError('copy_qualification_offline_boundary')
    volumes={spec['generation']['namespace']+suffix for suffix in ('-db','-data','-logs')}
    backings=inspect_volume_backings(volumes,all_containers,own|set(temporary),
        expected_binding=spec['generation']['resources_binding_sha256'])
    database=containers['database'][1]
    networks=database['NetworkSettings']['Networks']
    network=one_inspection(next(iter(networks)))
    if not docker_network_members_are_owned(network,all_containers,own):
        raise RuntimeError('copy_qualification_foreign_network_peer')
    identities={service:{key:container.get(key) for key in ('Id','Image','Config','HostConfig','Mounts','NetworkSettings')}
        | {'StartedAt':container.get('State',{}).get('StartedAt')}
        for service,(_,container) in containers.items()}
    environment=dict(value.split('=',1) for value in database['Config']['Env'] if '=' in value)
    user=environment.get('POSTGRES_USER','')
    if not re.fullmatch('[a-zA-Z_][a-zA-Z0-9_]{0,62}',user):
        raise RuntimeError('copy_qualification_database_user')
    result={'resources_sha256':digest({'containers':identities,'backings':backings}),
        'database_image_id':database['Image'],'application_image_id':containers['script-runner'][1]['Image'],
        'container_ids':{service:identifier for service,(identifier,_) in containers.items()},'database_user':user}
    return result,containers,backings

def private_directory(spec,create=False):
    operation=spec.get('operation','')
    if not re.fullmatch('[a-f0-9]{64}',operation):
        raise RuntimeError('copy_qualification_operation_identity')
    root=Path.home().resolve(strict=True)/'.local/state/launchplane-testing-disposal/copy-qualification'
    if any(parent.is_symlink() for parent in (root,*root.parents)):
        raise RuntimeError('copy_qualification_custody_unverified')
    if create:
        root.mkdir(parents=True,exist_ok=True,mode=0o700)
    path=root/operation
    if create:
        path.mkdir(mode=0o700,exist_ok=True)
    if path.is_symlink() or path.exists() and path.resolve()!=path.absolute():
        raise RuntimeError('copy_qualification_custody_unverified')
    return path

def save_journal(path,value):
    descriptor,name=tempfile.mkstemp(prefix='journal-',dir=path)
    temporary=Path(name)
    with os.fdopen(descriptor,'w') as output:
        json.dump(value,output,sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary,path/'journal.json')
    descriptor=os.open(path,os.O_RDONLY|os.O_DIRECTORY)
    try: os.fsync(descriptor)
    finally: os.close(descriptor)

def emit(value):
    print(sys.argv[2]+json.dumps(value,sort_keys=True),flush=True)

def inventory_ids():
    identifiers=docker('ps','-aq','--no-trunc').decode().splitlines()
    if len(set(identifiers))!=len(identifiers) or any(not re.fullmatch('[a-f0-9]{64}',value) for value in identifiers):
        raise RuntimeError('copy_qualification_inventory_unavailable')
    return set(identifiers)

def image_environment(image,required):
    rows=json.loads(docker('image','inspect',image))
    if len(rows)!=1 or rows[0].get('Id')!=image:
        raise RuntimeError('copy_qualification_image_unverified')
    raw=rows[0].get('Config',{}).get('Env') or []
    if any(not isinstance(value,str) or '=' not in value for value in raw):
        raise RuntimeError('copy_qualification_image_environment')
    return {**dict.fromkeys((value.split('=',1)[0] for value in raw),''),**required}

def create_offline(spec,journal,path,role,image,network,user,mounts,environment,entrypoint,command):
    expected={'image':image,'network':network,'user':user,'mounts':mounts,
        'environment':environment,'entrypoint':[entrypoint],'command':command,'operation':spec['operation']}
    name='lp-copy-qualification-'+spec['operation'][:24]+'-'+role
    journal.setdefault('containers',{})[role]={'name':name,'expected':expected,'id':''}
    journal['pending_create']=role
    save_journal(path,journal)
    arguments=['create','--name',name,'--pull','never','--network',network,'--restart','no',
        '--log-driver','none','--read-only','--cap-drop','ALL','--security-opt','no-new-privileges',
        '--tmpfs','/tmp:rw,noexec,nosuid,nodev,size=64m,mode=1777','--user',user,
        '--label','io.launchplane.copy-qualification='+spec['operation'],'--entrypoint',entrypoint]
    for key,value in sorted(environment.items()):
        arguments+=['--env',key+'='+value]
    for mount in mounts:
        arguments+=['--mount','type=volume,src='+mount['Name']+',dst='+mount['Destination']]
    identifier=docker(*arguments,image,*command).decode().strip()
    if not re.fullmatch('[a-f0-9]{64}',identifier):
        raise RuntimeError('copy_qualification_created_identity')
    journal['containers'][role]['id']=identifier
    journal.pop('pending_create',None)
    save_journal(path,journal)
    if not offline_container_isolated(one_inspection(identifier),expected):
        raise RuntimeError('copy_qualification_offline_boundary')
    return identifier

def temporary_owners(journal):
    owners={}
    identifiers=inventory_ids()
    for value in journal.get('containers',{}).values():
        identifier=value.get('id','')
        if not identifier:
            matches=[one_inspection(item) for item in identifiers]
            matches=[item for item in matches if item.get('Name')=='/'+value['name']]
            if not matches:
                continue
            if len(matches)!=1:
                raise RuntimeError('copy_qualification_created_identity')
            identifier=matches[0]['Id']
            value['id']=identifier
        if identifier not in identifiers:
            continue
        if not offline_container_isolated(one_inspection(identifier),value['expected']):
            raise RuntimeError('copy_qualification_offline_boundary')
        owners[identifier]=value['expected']
    return owners

def remove_temporary_containers(journal,path,allow_crashed=False):
    for role in ('client','database'):
        value=journal.get('containers',{}).get(role)
        if not value:
            continue
        temporary_owners(journal)
        identifier=value.get('id')
        if not identifier or identifier not in inventory_ids():
            continue
        container=one_inspection(identifier)
        if container.get('State',{}).get('Running') is True:
            docker('stop','--time','30',identifier,timeout=45)
        stopped=one_inspection(identifier).get('State',{})
        if stopped.get('Running') is not False or stopped.get('Pid')!=0:
            raise RuntimeError('copy_qualification_shutdown_unverified')
        if role=='database' and stopped.get('ExitCode')!=0 and not allow_crashed:
            raise RuntimeError('copy_qualification_database_shutdown_uncertain')
        docker('rm',identifier)
        if identifier in inventory_ids():
            raise RuntimeError('copy_qualification_removal_unverified')
        save_journal(path,journal)

def current_original(spec,journal,allow_started=False):
    physical,containers,backings=original_snapshot(spec,temporary_owners(journal),allow_started)
    if physical!=spec['physical']:
        raise RuntimeError('copy_qualification_review_changed')
    return containers,backings

def workflow_lock(backings,spec):
    return Path(backings[spec['generation']['namespace']+'-data']['path'])/'.data_workflow_in_progress'

def prior_attempt_is_quiescent(spec,journal,backings):
    if journal.get('pending_create') and not journal.get('containers',{}).get(journal['pending_create'],{}).get('id'):
        return False
    if temporary_owners(journal):
        return False
    lock=workflow_lock(backings,spec)
    if lock.exists() and ('launchplane_copy_qualification='+spec['operation']+'\n').encode() not in owned_file(lock,16384):
        return False
    pid=Path(backings[spec['generation']['namespace']+'-db']['path'])/'postmaster.pid'
    return not pid.exists() or hashlib.sha256(owned_file(pid,16384)).hexdigest()==journal.get('crash_pid_sha256')

def prior_attempt_can_resume(spec,journal,backings):
    owners=temporary_owners(journal)
    if journal.get('pending_create') and not journal.get('containers',{}).get(journal['pending_create'],{}).get('id'):
        return False
    for identifier in owners:
        container=one_inspection(identifier)
        state=container.get('State',{})
        if state.get('Running') is not False or state.get('Pid')!=0 or container.get('ExecIDs') not in (None,[]):
            return False
    lock=workflow_lock(backings,spec)
    if lock.exists() and ('launchplane_copy_qualification='+spec['operation']+'\n').encode() not in owned_file(lock,16384):
        return False
    pid=Path(backings[spec['generation']['namespace']+'-db']['path'])/'postmaster.pid'
    if not pid.exists():
        return True
    database=journal.get('containers',{}).get('database',{})
    return bool(database.get('id')) and owned_file(pid,16384).splitlines()[0]==b'1'

def inspect_journal(spec):
    path=private_directory(spec)
    if not (path/'journal.json').exists():
        emit({'type':'unknown'})
        return
    journal=json.loads(owned_file(path/'journal.json'))
    if journal.get('binding')!=spec['qualification_binding']:
        raise RuntimeError('copy_qualification_journal_changed')
    if journal.get('state')=='complete':
        physical,containers,_=original_snapshot(spec,allow_started=spec['qualification_mode']=='start')
        if temporary_owners(journal) or physical['resources_sha256']!=journal['runtime_resources_sha256']:
            raise RuntimeError('copy_qualification_terminal_changed')
        expected={'database','script-runner'} if spec['qualification_mode']=='start' else set()
        if {service for service,(_,item) in containers.items() if item.get('State',{}).get('Running')}!=expected:
            raise RuntimeError('copy_qualification_terminal_changed')
        emit({'type':'complete','proof':journal['proof']})
    elif journal.get('state') in {'failed_safe','running','prepared'}:
        _,backings=current_original(spec,journal)
        emit({'type':'absent','retry_safe':prior_attempt_can_resume(spec,journal,backings)})
    else:
        emit({'type':'unknown'})

def client_program(spec,environment,recover_lock):
    payload={key:spec[key] for key in ('database_name','copy_binding_sha256','source_mode','readback_spec','readback_program','operation')}
    payload.update(environment=environment,recover_lock=recover_lock,stale_lock_program=STALE_LOCK_PROGRAM,
        runner_healthcheck_command=spec['runner_healthcheck_command'],start_parameter_keys=spec.get('start_parameter_keys',[]))
    return ['-c',COPY_QUALIFICATION_CLIENT,base64.b64encode(json.dumps(payload).encode()).decode()]

def apply_qualification(spec):
    path=private_directory(spec,create=True)
    journal_path=path/'journal.json'
    journal=json.loads(owned_file(journal_path)) if journal_path.exists() else {
        'binding':spec['qualification_binding'],'state':'prepared','containers':{}}
    if journal.get('binding')!=spec['qualification_binding']:
        raise RuntimeError('copy_qualification_journal_changed')
    if journal.get('state')=='complete':
        inspect_journal(spec)
        return
    containers,backings=current_original(spec,journal)
    if journal.get('containers'):
        if not prior_attempt_can_resume(spec,journal,backings):
            raise RuntimeError('copy_qualification_prior_attempt_unsettled')
        pid=Path(backings[spec['generation']['namespace']+'-db']['path'])/'postmaster.pid'
        if pid.exists():
            journal['crash_pid_sha256']=hashlib.sha256(owned_file(pid,16384)).hexdigest()
            save_journal(path,journal)
        remove_temporary_containers(journal,path,allow_crashed=True)
        journal.pop('pending_create',None)
        save_journal(path,journal)
    if not prior_attempt_is_quiescent(spec,journal,backings):
        raise RuntimeError('copy_qualification_original_not_quiescent')
    database_path=Path(backings[spec['generation']['namespace']+'-db']['path'])
    if containers['database'][1].get('State',{}).get('ExitCode')!=0:
        raise RuntimeError('copy_qualification_original_not_quiescent')
    data_path=Path(backings[spec['generation']['namespace']+'-data']['path'])
    recover_lock=workflow_lock(backings,spec).exists()
    journal.update(state='running',containers={})
    save_journal(path,journal)
    completed=False
    try:
        database_image=spec['physical']['database_image_id']
        application_image=spec['physical']['application_image_id']
        common={'PATH':'/venv/bin:/usr/local/bin:/usr/bin:/bin','HOME':'/tmp','LANG':'C.UTF-8'}
        database_environment=image_environment(database_image,common)
        command=['-ec','printf "host all %s 127.0.0.1/32 trust\\n" "$1" >/tmp/copy-hba; '
            'exec postgres -D "$2" -c listen_addresses=127.0.0.1 -c unix_socket_directories= '
            '-c hba_file=/tmp/copy-hba -c port=5432','copy-database',spec['physical']['database_user'],spec['database_data_directory']]
        database=create_offline(spec,journal,path,'database',database_image,'none',
            str(database_path.stat().st_uid)+':'+str(database_path.stat().st_gid),
            [{'Type':'volume','Name':spec['generation']['namespace']+'-db','Destination':spec['database_data_directory'],'RW':True}],
            database_environment,'/bin/sh',command)
        current_original(spec,journal)
        docker('start',database)
        deadline=time.monotonic()+90
        while True:
            try:
                docker('exec',database,'pg_isready','-h','127.0.0.1','-U',spec['physical']['database_user'],timeout=5)
                break
            except RuntimeError:
                if time.monotonic()>=deadline:
                    raise RuntimeError('copy_qualification_database_unavailable')
                time.sleep(1)
        environment=image_environment(application_image,{**common,'PYTHONDONTWRITEBYTECODE':'1',
            'PLATFORM_INSTANCE':'testing','ODOO_DB_HOST':'127.0.0.1','ODOO_DB_PORT':'5432',
            'ODOO_DB_NAME':spec['database_name'],'ODOO_DB_USER':spec['physical']['database_user'],
            'ODOO_DB_PASSWORD':'offline-local-trust','ODOO_FILESTORE_PATH':'/volumes/data/filestore',
            'ODOO_RESTORE_KEPT_INTEGRATIONS':'','ENV_OVERRIDE_DISABLE_CRON':'true','NO_SANITIZE':'false',
            'LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED':'false','LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED':'false'})
        client=create_offline(spec,journal,path,'client',application_image,'container:'+database,
            str(data_path.stat().st_uid)+':'+str(data_path.stat().st_gid),
            [{'Type':'volume','Name':spec['generation']['namespace']+'-data','Destination':'/volumes/data','RW':True}],
            environment,'/venv/bin/python',client_program(spec,environment,recover_lock))
        current_original(spec,journal)
        output=docker('start','--attach',client,timeout=900).decode()
        state=one_inspection(client).get('State',{})
        if state.get('Running') is not False or state.get('ExitCode')!=0:
            raise RuntimeError('copy_qualification_client_failed')
        frames=[json.loads(line.split('=',1)[1]) for line in output.splitlines() if line.startswith('copy_qualification_client=')]
        if len(frames)!=1 or set(frames[0])!={'credential_boundary_commit'} or not re.fullmatch('[a-f0-9-]{36}',frames[0]['credential_boundary_commit']):
            raise RuntimeError('copy_qualification_client_unverified')
        current_original(spec,journal)
        remove_temporary_containers(journal,path)
        containers,_=current_original(spec,journal)
        if (database_path/'postmaster.pid').exists() or workflow_lock(backings,spec).exists():
            raise RuntimeError('copy_qualification_shutdown_unverified')
        if spec['qualification_mode']=='start':
            for service in ('database','script-runner'):
                docker('start',containers[service][0])
            physical,containers,_=original_snapshot(spec,allow_started=True)
            if containers['web'][1].get('State',{}).get('Running') is not False:
                raise RuntimeError('copy_qualification_web_started')
            if any(containers[service][1].get('State',{}).get('Running') is not True for service in ('database','script-runner')):
                raise RuntimeError('copy_qualification_start_unverified')
        else:
            physical=spec['physical']
        proof={'generation_binding_sha256':spec['copy_binding_sha256'],
            'resources_sha256':spec['physical']['resources_sha256'],
            'credential_boundary_commit':frames[0]['credential_boundary_commit'],
            'original_copy_verified':True,'credentials_cleared':True,'connections_cleared':True,
            'offline_containers_absent':True,'database_stopped':spec['qualification_mode']!='start',
            'runner_stopped':spec['qualification_mode']!='start','web_stopped':True}
        journal.update(state='complete',proof=proof,runtime_resources_sha256=physical['resources_sha256'])
        save_journal(path,journal)
        completed=True
        emit({'type':'complete','proof':proof})
    finally:
        if not completed:
            try:
                remove_temporary_containers(journal,path)
                _,backings=current_original(spec,journal)
                if prior_attempt_is_quiescent(spec,journal,backings):
                    journal['state']='failed_safe'
                    save_journal(path,journal)
            except Exception as error:
                raise RuntimeError('copy_qualification_cleanup_unverified') from error

def qualification_main(spec):
    if spec.get('mode')=='observe':
        inspect_journal(spec)
        return
    if spec.get('mode')!='apply':
        raise RuntimeError('copy_qualification_mode_invalid')
    lock=Path(tempfile.gettempdir())/(spec['generation']['namespace']+'-copy.lock')
    descriptor=os.open(lock,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
    try:
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        apply_qualification(spec)
    finally:
        os.close(descriptor)

def main():
    spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
    if spec.get('mode')=='inspect':
        physical,containers,backings=original_snapshot(spec)
        database_path=Path(backings[spec['generation']['namespace']+'-db']['path'])
        if (containers['database'][1].get('State',{}).get('ExitCode')!=0
            or (database_path/'postmaster.pid').exists() or workflow_lock(backings,spec).exists()):
            raise RuntimeError('copy_qualification_original_not_quiescent')
        emit({'type':'complete','physical':physical})
        return
    qualification_main(spec)

try:
    main()
except Exception:
    emit({'type':'failed','predicate':'copy_qualification_unverified'})
"""
)
