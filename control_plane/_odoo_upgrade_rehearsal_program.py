"""Host program for a credential-free-control-plane, privately routed rehearsal.

Only the separately owned compose and its data are writable. Source SSH is read
only, and raw failure material remains outside every application mount.
"""

from control_plane._odoo_rehearsal_source_reader import REHEARSAL_SOURCE_READER_LIBRARY
from control_plane._odoo_rehearsal_attachment_validation import (
    ATTACHMENT_MISMATCH_LIBRARY,
    ATTACHMENT_VALIDATION_PROGRAM,
)
from control_plane._odoo_rehearsal_capture_comparison import REHEARSAL_CAPTURE_COMPARISON_LIBRARY
from control_plane._odoo_rehearsal_diagnostics import REHEARSAL_DIAGNOSTICS_LIBRARY

ODOO_UPGRADE_REHEARSAL_PROGRAM = (
    REHEARSAL_SOURCE_READER_LIBRARY
    + REHEARSAL_CAPTURE_COMPARISON_LIBRARY
    + ATTACHMENT_MISMATCH_LIBRARY
    + REHEARSAL_DIAGNOSTICS_LIBRARY
    + "\nATTACHMENT_VALIDATION_PROGRAM = "
    + repr(ATTACHMENT_VALIDATION_PROGRAM)
    + "\n"
    + r"""
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import tarfile
import time
from datetime import datetime, timezone


class RehearsalCommandFailure(RuntimeError):
    def __init__(self, diagnostic=None):
        self.diagnostic=diagnostic
        super().__init__('Rehearsal command failed; preserve its private custody')


def command(arguments, *, stdin=None, stdout=None, environment=None):
    result = subprocess.run(arguments, input=stdin, stdout=stdout or subprocess.PIPE,
        stderr=subprocess.PIPE, env=environment, check=False)
    if result.returncode:
        diagnostic=command_diagnostic(arguments,result,environment)
        raise RehearsalCommandFailure(diagnostic)
    return result.stdout if stdout is None else b''


def docker(*arguments, stdin=None):
    return command(['docker', *arguments], stdin=stdin)


def one_inspection(identifier):
    rows=json.loads(docker('inspect',identifier))
    if not isinstance(rows,list) or len(rows)!=1:
        raise RuntimeError('Incomplete rehearsal inspection')
    return rows[0]


def digest(path):
    value=hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):
            value.update(chunk)
    return value.hexdigest()


def bounded_capture(arguments,path,bound):
    errors=tempfile.TemporaryFile()
    process=subprocess.Popen(arguments,stdout=subprocess.PIPE,stderr=errors)
    try:
        size=0
        with path.open('xb') as output:
            os.chmod(path,0o600)
            while True:
                chunk=process.stdout.read(1024*1024)
                if not chunk: break
                size+=len(chunk)
                if size>bound: raise RuntimeError('Source copy exceeds its reviewed storage bound')
                output.write(chunk)
        status=process.wait(timeout=30)
        if status:
            errors.seek(max(0,errors.seek(0,os.SEEK_END)-65536))
            diagnostic=command_diagnostic(arguments,subprocess.CompletedProcess(
                arguments,status,stdout=b'',stderr=errors.read(65536)),None)
            raise RehearsalCommandFailure(diagnostic)
        if not size:
            raise RuntimeError('Incomplete source copy')
    finally:
        errors.close()
        process.stdout.close()
        if process.poll() is None:
            process.terminate()
            try: process.wait(timeout=15)
            except subprocess.TimeoutExpired:
                process.kill();process.wait()


def save(path,payload):
    temporary=path.with_suffix('.pending')
    with temporary.open('x') as output:
        os.chmod(temporary,0o600)
        json.dump(payload,output,sort_keys=True)
        output.flush()
        os.fsync(output.fileno())
    os.replace(temporary,path)


def compose(spec,root,*,image,database):
    version=spec['record']['source_odoo_version'] if database.endswith('_source') else spec['versions'][image]
    environment={**spec['environment'],'ODOO_DB_NAME':database,'ODOO_VERSION':version}
    environment['LAUNCHPLANE_RUNTIME_IDENTITY_JSON']=json.dumps({'product':spec['record']['product'],'context':spec['record']['rehearsal_id'],'instance':'rehearsal','environment_kind':'rehearsal','deployment_record_id':spec['record']['rehearsal_id'],'artifact_id':spec['artifacts'][image],'source_git_ref':spec['refs'][image],'image_reference':image})
    (root/'compose.yml').write_text(spec['compose'][image])
    os.chmod(root/'compose.yml',0o600)
    # Environment belongs only to this subprocess; no ambient product settings.
    command(['docker','compose','--project-directory',str(root),'--env-file','/dev/null','-p',spec['record']['rehearsal_id'],'-f',str(root/'compose.yml'),'up','-d'],
        environment={'PATH':os.environ['PATH'],'HOME':os.environ['HOME'],**environment})


def inspection(spec,database,image):
    namespace=spec['record']['rehearsal_id']
    return inspect_copy_runtime({
        'generation':{'namespace':namespace,'instance':'rehearsal','resources_binding_sha256':spec['resources_binding_sha256']},
        'compose_project':namespace,'database_name':database,'image_reference':image,
        'database_data_directory':spec['database_data_directory'],'storage_maintenance':True,
    },expected_instance='rehearsal')


def sql(containers,statement,database='postgres'):
    return docker('exec','-i',containers['database'][0],'sh','-c',
        'psql -X -qAt -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$1"','_',database,
        stdin=statement.encode()).decode().strip()


def database_names(spec):
    prefix=spec['record']['rehearsal_id'].replace('-','_')
    return {role:prefix+'_'+role for role in ('source','upgrade','final_source','final_upgrade')}


def current(spec):
    names=database_names(spec)
    state=spec['record']['state']
    database=names['source'] if state in {'planned','prepared'} else names['upgrade'] if state=='upgraded' else names['final_upgrade']
    image=spec['source_image'] if state in {'planned','prepared'} else spec['target_image']
    return database,image


def witness(spec,database):
    return hashlib.sha256((spec['record']['intent_sha256']+'\n'+database).encode()).hexdigest()


def stamp(spec,containers,database):
    expected=witness(spec,database)
    sql(containers,"INSERT INTO ir_config_parameter(key,value) VALUES ('launchplane.testing_copy.restore_complete','"+expected+"') ON CONFLICT(key) DO UPDATE SET value=EXCLUDED.value",database)


def snapshot(spec,root,containers,database):
    version=sql(containers,"SELECT latest_version FROM ir_module_module WHERE name='base'",database)
    expected=spec['record']['source_odoo_version'] if database.endswith('_source') else spec['versions'][spec['target_image']]
    if version.split('.')[0]!=expected.split('.')[0]:
        raise RuntimeError('Recovery snapshot has the wrong database version')
    path=root/database
    if path.exists():
        raise RuntimeError('Snapshot already exists without its completed operation receipt')
    path.mkdir(mode=0o700)
    bounded_capture(['docker','exec',containers['database'][0],'sh','-c',
        'pg_dump -Fc --no-owner -U "$POSTGRES_USER" -- "$1"','_',database],path/'database.dump',spec['record']['storage']['database_bytes'])
    bounded_capture(['docker','exec',containers['script-runner'][0],'tar','-cf','-','-C','/volumes/data/filestore',database],
        path/'filestore.tar',spec['record']['storage']['filestore_bytes'])
    for filename in ('database.dump','filestore.tar'):
        os.chmod(path/filename,0o600)
    receipt={'database':database,'witness_sha256':witness(spec,database),
        'database_sha256':digest(path/'database.dump'),'filestore_sha256':digest(path/'filestore.tar'),
        'source_capture_sha256':digest(root/'capture.json'),
        'sanitized':True,'odoo_version':version,'captured_at':datetime.now(timezone.utc).isoformat()}
    save(path/'receipt.json',receipt)
    return receipt


def validate_snapshots(spec,root,containers,copies,*,retired=False):
    for receipt in copies:
        database=receipt['database']
        if database not in database_names(spec).values() or receipt['witness_sha256']!=witness(spec,database):
            raise RuntimeError('Copy witness differs from its rehearsal')
        if digest(root/'capture.json')!=receipt['source_capture_sha256'] or json.loads((root/'capture.json').read_text())['source_binding_sha256']!=spec['record']['source_binding_sha256']:
            raise RuntimeError('Copy source lineage changed')
        for filename,key in (('database.dump','database_sha256'),('filestore.tar','filestore_sha256')):
            path=root/database/filename
            if path.is_symlink() or digest(path)!=receipt[key]:
                raise RuntimeError('Preserved snapshot changed')
        if not retired:
            marker=sql(containers,"SELECT value FROM ir_config_parameter WHERE key='launchplane.testing_copy.restore_complete'",database)
            if marker!=receipt['witness_sha256']:
                raise RuntimeError('Preserved database witness changed')


def clone(spec,containers,source_database,destination):
    if sql(containers,"SELECT count(*) FROM pg_database WHERE datname='"+destination+"'")!='0':
        raise RuntimeError('Clone destination appeared; never overwrite a copy')
    sql(containers,'CREATE DATABASE "'+destination+'" TEMPLATE "'+source_database+'"')
    sql(containers,'ALTER DATABASE "'+destination+'" SET default_transaction_read_only=off')
    docker('exec',containers['script-runner'][0],'cp','-a','/volumes/data/filestore/'+source_database,
        '/volumes/data/filestore/'+destination)
    stamp(spec,containers,destination)


def restore_preserved_snapshot(spec,root,containers,source_database,destination):
    if sql(containers,"SELECT count(*) FROM pg_database WHERE datname='"+destination+"'")!='0':
        raise RuntimeError('Final-copy destination already exists')
    sql(containers,'CREATE DATABASE "'+destination+'" TEMPLATE template0')
    dump='/tmp/'+spec['record']['rehearsal_id']+'.dump'
    docker('cp',str(root/source_database/'database.dump'),containers['database'][0]+':'+dump)
    docker('exec',containers['database'][0],'sh','-c',
        'pg_restore --no-owner --no-acl -U "$POSTGRES_USER" -d "$1" "$2"','_',destination,dump)
    docker('exec',containers['database'][0],'rm','-f',dump)
    with tempfile.TemporaryDirectory(prefix='restore-',dir=root) as temporary:
        with tarfile.open(root/source_database/'filestore.tar','r:') as archive:
            for member in archive:
                if not member.isfile() and not member.isdir():
                    raise RuntimeError('Preserved filestore has unsafe members')
                archive.extract(member,path=temporary,filter='data')
        docker('exec','-u','root',containers['script-runner'][0],'mkdir','-p','/volumes/data/filestore/'+destination)
        docker('cp',str(Path(temporary)/source_database)+'/.',containers['script-runner'][0]+':/volumes/data/filestore/'+destination)
    uid=docker('exec',containers['script-runner'][0],'id','-u').decode().strip()
    gid=docker('exec',containers['script-runner'][0],'id','-g').decode().strip()
    if not uid.isdigit() or not gid.isdigit():
        raise RuntimeError('Unknown copy runner user')
    docker('exec','-u','root',containers['script-runner'][0],'chown','-R',uid+':'+gid,'/volumes/data/filestore/'+destination)
    stamp(spec,containers,destination)


def copy_runner(spec,containers,database,*,fresh,dump_path=None,sanitize_only=False):
    arguments=['exec','-i']
    workflow={**spec['workflow'],'ODOO_DB_NAME':database,'ODOO_VERSION':spec['record']['source_odoo_version'] if sanitize_only else spec['versions'][spec['target_image']],
        'ODOO_FILESTORE_PATH':'/volumes/data/filestore','ENV_OVERRIDE_DISABLE_CRON':'true',
        'ODOO_RESTORE_KEPT_INTEGRATIONS':'','NO_SANITIZE':'false'}
    for key,value in workflow.items():
        arguments+=['-e',key+'='+value]
    runner=spec['sanitize_runner'] if sanitize_only else spec['upgrade_runner']
    result=docker(*arguments,containers['script-runner'][0],'python3','-',dump_path or '/tmp/unused',database,
        spec['readback_spec'],base64.b64encode(spec['readback_program'].encode()).decode(),
        'fresh' if fresh else 'resume',witness(spec,database),stdin=runner.encode()).decode()
    markers=dict(line.split('=',1) for line in result.splitlines() if '=' in line)
    required=['testing_copy_sanitized','testing_copy_pre_registry_sanitized']
    if not sanitize_only:
        required+=['odoo_module_update_completed','odoo_module_update_modules_configured']
    if any(markers.get(key)!='true' for key in required):
        raise RuntimeError('Copy lacks its sanitization or upgrade proof')


def before_web(spec,containers,database):
    result=docker('exec','-i','-e','ODOO_DB_NAME='+database,containers['script-runner'][0],'python3','-',database,
        spec['readback_spec'],stdin=spec['readback_program'].encode()).decode()
    if 'integration_readback_ok=true' not in result.splitlines():
        raise RuntimeError('Before-web connection readback failed')
    docker('exec',containers['script-runner'][0],'python3','-c',
        "import hashlib,os;from pathlib import Path;Path("+repr(spec['web_gate_path'])+").write_text(hashlib.sha256((os.environ['ODOO_DB_NAME']+chr(10)+os.environ.get('ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64','')).encode()).hexdigest())")
    docker('start',containers['web'][0])


def private_health(spec,containers):
    expected=spec['record']['target_artifact']['source_commit']
    program="import json,sys,time,urllib.request\nfor attempt in range(60):\n try:\n  response=urllib.request.urlopen('http://web:8069/launchplane/health',timeout=5); body=json.load(response); identity=body.get('runtime_identity',body.get('launchplane_runtime_identity',{})); assert identity.get('source_git_ref',identity.get('source_revision'))==sys.argv[1]; assert identity.get('instance')=='rehearsal'; assert identity.get('context')==sys.argv[2]; assert identity.get('artifact_id')==sys.argv[3]; break\n except Exception:\n  if attempt==59: raise\n  time.sleep(1)\nresponse=urllib.request.urlopen('http://web:8069/web/login',timeout=5); assert response.status==200\n"
    docker('exec',containers['script-runner'][0],'python3','-c',program,expected,spec['record']['rehearsal_id'],spec['record']['target_artifact']['artifact_id'])


def sanitize_capture(spec,containers,database,raw):
    try:
        docker('cp',str(raw/'database.dump'),containers['script-runner'][0]+':/tmp/rehearsal.dump')
        docker('exec','-u','root',containers['script-runner'][0],'chmod','644','/tmp/rehearsal.dump')
        uid=docker('exec',containers['script-runner'][0],'id','-u').decode().strip()
        gid=docker('exec',containers['script-runner'][0],'id','-g').decode().strip()
        if not uid.isdigit() or not gid.isdigit():
            raise RuntimeError('Unknown copy runner user')
        docker('exec','-u','root',containers['script-runner'][0],'chown','-R',uid+':'+gid,'/volumes/data','/volumes/logs','/tmp/rehearsal.dump')
        copy_runner(spec,containers,database,fresh=True,dump_path='/tmp/rehearsal.dump',sanitize_only=True)
    finally:
        docker('exec','-u','root',containers['script-runner'][0],'rm','-f','/tmp/rehearsal.dump')


def capture(spec,root,containers,database):
    started=time.monotonic()
    raw=root/'raw-source'
    if raw.exists():
        raise RuntimeError('Partial source capture is retained; do not recapture')
    raw.mkdir(mode=0o700)
    values=spec['source']
    ssh,destination,filestore=rehearsal_source_reader(values)
    remote=shlex.join(['sudo','-u',values['ODOO_UPSTREAM_DB_USER'],'pg_dump','-Fc','--',values['ODOO_UPSTREAM_DB_NAME']])
    budget=spec['record']['storage']
    bounded_capture([*ssh,destination,remote],raw/'database.dump',budget['database_bytes'])
    remote=shlex.join(['tar','-cf','-','-C',filestore,'.'])
    bounded_capture([*ssh,destination,remote],raw/'filestore.tar',budget['filestore_bytes'])
    (raw/'filestore').mkdir(mode=0o700)
    unpacked=0
    with tarfile.open(raw/'filestore.tar','r:') as archive:
        for member in archive:
            if not member.isfile() and not member.isdir():
                raise RuntimeError('Source filestore contains a link or special file')
            unpacked+=member.size
            if unpacked>budget['filestore_bytes']:
                raise RuntimeError('Filestore extraction exceeds its reviewed bound')
            archive.extract(member,path=raw/'filestore',filter='data')
    save(root/'capture.json',{'source_binding_sha256':spec['record']['source_binding_sha256'],
        'database_sha256':digest(raw/'database.dump'),'filestore_sha256':digest(raw/'filestore.tar')})
    spec.setdefault('phase_elapsed',{})['source-capture']=time.monotonic()-started
    docker('exec','-u','root',containers['script-runner'][0],'mkdir','-p','/volumes/data/filestore/'+database)
    docker('cp',str(raw/'filestore')+'/.',containers['script-runner'][0]+':/volumes/data/filestore/'+database)
    started=time.monotonic()
    sanitize_capture(spec,containers,database,raw)
    spec['phase_elapsed']['source-sanitization']=time.monotonic()-started
    version=sql(containers,"SELECT latest_version FROM ir_module_module WHERE name='base'",database)
    if version.split('.')[0] != spec['record']['source_odoo_version'].split('.')[0]:
        raise RuntimeError('Captured source version differs from the selected source version')
    # Check the dump's attachment set against the copied bytes, so a concurrent
    # source deletion or incomplete rsync cannot be called a consistent capture.
    validation=attachment_validation_payload(containers,database,'validate')
    reason=validation['reason']
    if reason!='verified':
        if 'mismatch' in validation:
            save(root/ATTACHMENT_MISMATCH_RECEIPT_NAME,{
                'intent_sha256':spec['record']['intent_sha256'],
                'attempt_sha256':spec['attempt_sha256'],
                'record_binding_sha256':spec['record_binding_sha256'],
                'attachment':validation['mismatch']})
        raise RuntimeError('Attachment validation refused: '+reason)
    return raw


def rehearsal_storage_capacity(spec, root, containers, action):
    budget=spec['record']['storage']
    database=budget['database_bytes']; filestore=budget['filestore_bytes']
    # Free space already excludes every completed phase's working/recovery
    # copies. Charge only this phase's new allocations, including transients.
    working=2 if action in {'execute','simulate-final'} else 0
    host=database+filestore+budget['staging_bytes']
    if action=='execute':
        # Raw dump + tar + extracted filestore remain until upgrade succeeds,
        # overlapping the sanitized source snapshot and both working copies.
        host+=database+2*filestore
    elif action=='simulate-final':
        # Restoring the retained source extracts on the host and copies its
        # dump into the database container, not the runner's writable layer.
        host+=filestore
    elif action!='verify':
        raise RuntimeError('Unsupported storage phase')
    return storage_capacity(
        {'generation':{'namespace':spec['record']['rehearsal_id']},'staging_directory':str(root)},
        containers,{**budget,'database_bytes':working*database,'filestore_bytes':working*filestore},
        runner_staging_bytes=database if action=='execute' else 0,
        host_staging_bytes=host,
        database_staging_bytes=database if action=='simulate-final' else 0)


def refusal_receipt(spec, root, code):
    attempt=spec.get('attempt_sha256','')
    binding=spec.get('record_binding_sha256','')
    if not all(re.fullmatch(r'[a-f0-9]{64}',value) for value in (attempt,binding)):
        return None
    kind={'rehearsal_storage_capacity_insufficient':'capacity',
        'rehearsal_precheck_refused':'precheck'}[code]
    return root/(spec['action']+'.refused-'+attempt+'-'+binding+'-'+kind)


def refusal_result(spec, code):
    record=spec['record']
    return {'intent_sha256':record['intent_sha256'],'state':record['state'],
        'copies':record['copies'],'elapsed_seconds':record['elapsed_seconds'],
        'resources_preserved':True,'public_route':False,'business_host_frozen':False,
        'completed':False,'refusal_code':code,'attempt_sha256':spec['attempt_sha256']}


def read_refusal(spec, root):
    result=None
    for code in ('rehearsal_storage_capacity_insufficient','rehearsal_precheck_refused'):
        path=refusal_receipt(spec,root,code)
        if path is None:
            continue
        try:
            metadata=path.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size!=0
            or stat.S_IMODE(metadata.st_mode)!=0o600 or result is not None):
            raise RuntimeError('Refusal custody is unverified')
        result=refusal_result(spec,code)
    return result


def custody_observation(spec, root, action):
    def size(path):
        try:
            metadata=path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(metadata.st_mode):
            raise RuntimeError('Rehearsal custody file is aliased')
        return metadata.st_size
    def directory(path):
        try:
            metadata=path.lstat()
        except FileNotFoundError:
            return False
        if not stat.S_ISDIR(metadata.st_mode):
            raise RuntimeError('Rehearsal custody directory is aliased')
        return True
    raw=root/'raw-source'
    directory(raw)
    return {'effect_started':size(root/(action+'.started')) is not None,
        'raw_database_bytes':size(raw/'database.dump'),
        'raw_filestore_bytes':size(raw/'filestore.tar'),
        'raw_filestore_extracted':directory(raw/'filestore'),
        'capture_receipt_present':size(root/'capture.json') is not None,
        'capture_pending_present':size(root/'capture.pending') is not None,
        'source_snapshot_directory_present':directory(root/database_names(spec)['source'])}


def capture_attachment_observation(spec,root,observations=None):
    database=database_names(spec)['source']
    containers=inspection(spec,database,spec['source_image'])
    if containers['web'][1].get('State',{}).get('Running') is not False:
        return 'writer_active'
    value=attachment_validation_payload(containers,database,'compare')
    if value['reason']=='attachment_size_and_hash_mismatch':
        comparison=compare_retained_attachment(spec,root,value['reference'])
        if comparison=='capture_attachment_matches_copy_reference_differs' and observations is not None:
            observations['capture_database_reference_validation']=compare_captured_database_reference(
                spec,root,containers,value['reference'])
        return comparison
    return value['reason']


def attachment_validation_payload(containers, database, mode):
    result=docker('exec','-i','-e','PYTHONDONTWRITEBYTECODE=1','-e','ODOO_DB_NAME='+database,
        containers['script-runner'][0],'python3','-',mode,
        stdin=ATTACHMENT_VALIDATION_PROGRAM.encode()).decode()
    rows=[line.split('=',1)[1] for line in result.splitlines() if line.startswith('attachment_validation=')]
    if len(rows)!=1:
        raise RuntimeError('Attachment observation is unverified')
    value=json.loads(bytes.fromhex(rows[0]))
    if not isinstance(value,dict) or not isinstance(value.get('reason'),str) or value['reason'] not in {
        'dependency_unavailable','environment_unavailable','database_unavailable',
        'attachment_query_unavailable','attachment_metadata_invalid','attachment_path_unsafe',
        'attachment_missing','attachment_unreadable','attachment_size_mismatch',
        'attachment_hash_mismatch','attachment_size_and_hash_mismatch',
        'attachment_content_matches_checksum_size_mismatch','verified','incomplete'}:
        raise RuntimeError('Attachment observation is unverified')
    private=mode=='compare' and value['reason']=='attachment_size_and_hash_mismatch'
    mismatch=mode=='validate' and value['reason'] in {'attachment_size_mismatch','attachment_hash_mismatch'}
    if set(value)!=({'reason','reference'} if private else {'reason','mismatch'} if mismatch else {'reason'}):
        raise RuntimeError('Attachment observation is unverified')
    if private and not valid_attachment_reference(value['reference']):
        raise RuntimeError('Attachment observation is unverified')
    if mismatch and (not valid_attachment_mismatch(value['mismatch'])
            or value['mismatch']['reason']!=value['reason']):
        raise RuntimeError('Attachment observation is unverified')
    return value


def read_attachment_mismatch(spec,root):
    path=root/ATTACHMENT_MISMATCH_RECEIPT_NAME
    try:
        descriptor=os.open(path,os.O_RDONLY|os.O_NOFOLLOW|os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(descriptor,'r') as stream:
        metadata=os.fstat(stream.fileno())
        if (not stat.S_ISREG(metadata.st_mode) or metadata.st_size>ATTACHMENT_EVIDENCE_MAX_BYTES
                or stat.S_IMODE(metadata.st_mode)!=0o600):
            raise RuntimeError('Attachment evidence custody is unverified')
        value=json.load(stream)
    if (not isinstance(value,dict) or set(value)!=
            {'intent_sha256','attempt_sha256','record_binding_sha256','attachment'}
            or value['intent_sha256']!=spec['record']['intent_sha256']
            or any(not re.fullmatch('[a-f0-9]{64}',str(value[key])) or value[key]!=spec.get(key)
                for key in ('attempt_sha256','record_binding_sha256'))
            or not valid_attachment_mismatch(value['attachment'])):
        raise RuntimeError('Attachment evidence binding is unverified')
    return value


def observe(spec, root, receipt, lock_held=False):
    record=spec['record']
    namespace=record['rehearsal_id']
    action=spec['action']
    if not receipt.is_file() or receipt.is_symlink():
        if not (root/(action+'.started')).exists():
            refused=read_refusal(spec,root)
            if refused is not None:
                print('rehearsal_result='+json.dumps(refused).encode().hex())
                return
        copies=[]
        for database in database_names(spec).values():
            saved=root/database/'receipt.json'
            if saved.is_file() and not saved.is_symlink():
                copy=json.loads(saved.read_text())
                validate_snapshots(spec,root,{},[copy],retired=True)
                copies.append(copy)
        prepare_not_started=False
        marker=root/(action+'.started')
        raw=root/'raw-source'
        if (action=='prepare' and record['state']=='planned' and not copies
                and not marker.exists() and not marker.is_symlink()
                and not receipt.exists() and not receipt.is_symlink()
                and not raw.exists() and not raw.is_symlink()):
            try:
                prepare_not_started=not docker('ps','-a','--filter',
                    'label=com.docker.compose.project='+namespace,'--format','{{.ID}}').strip()
            except Exception:
                pass
        result={'intent_sha256':record['intent_sha256'],'state':record['state'],'copies':copies,
            'elapsed_seconds':record['elapsed_seconds'],'resources_preserved':True,'public_route':False,
            'business_host_frozen':False,'completed':False,'raw_source_retained':raw.is_dir(),
            'prepare_not_started':prepare_not_started,
            'custody':custody_observation(spec,root,action)}
        custody=result['custody']
        if lock_held and spec.get('inspect_capture') is True and custody['effect_started']:
            failure=read_command_failure(spec,root)
            if failure is not None:
                result['child_failure']=failure
            names=database_names(spec)
            if (action=='execute' and record['state']=='prepared'
                    and {copy['database'] for copy in copies}=={names['source']}):
                try:
                    containers=inspection(spec,names['upgrade'],spec['target_image'])
                    if containers['web'][1].get('State',{}).get('Running') is not False:
                        raise RuntimeError('Failed upgrade writer is active')
                    result['retained_upgrade_logs']=retained_upgrade_logs(spec,containers,root)
                except (RuntimeError,OSError,ValueError,KeyError):
                    result['retained_upgrade_logs_unavailable']=True
            if result.get('child_failure') is not None or result.get('retained_upgrade_logs') is not None:
                result['diagnostics_private']=True
        mismatch=None
        if (lock_held and spec.get('inspect_capture') is True and action=='execute'
                and record['state']=='prepared' and not copies and custody['effect_started']):
            mismatch=read_attachment_mismatch(spec,root)
            if mismatch is not None:
                result['attachment_mismatch']=mismatch
                result['capture_attachment_validation']=mismatch['attachment']['reason']
        if (mismatch is None and lock_held and spec.get('inspect_capture') is True and action=='execute'
                and record['state']=='prepared' and not copies and custody['effect_started']
                and custody['raw_database_bytes'] and custody['raw_filestore_bytes']
                and custody['raw_filestore_extracted'] and custody['capture_receipt_present']
                and not custody['capture_pending_present']
                and not custody['source_snapshot_directory_present']):
            try:
                result['capture_attachment_validation']=capture_attachment_observation(spec,root,result)
            except RehearsalCommandFailure:
                result['capture_attachment_validation']='runtime_unavailable'
        print('rehearsal_result='+json.dumps(result).encode().hex())
        return
    result=json.loads(receipt.read_text())
    if result['intent_sha256']!=record['intent_sha256']:
        raise RuntimeError('Another rehearsal owns this receipt')
    states=['planned','prepared','upgraded','switched','verified','retired']
    historical=states.index(record['state'])>states.index(result['state'])
    observed_state=record['state'] if historical else result['state']
    database,image=current({**spec,'record':{**record,'state':observed_state}})
    containers=inspection(spec,database,image)
    validate_snapshots(spec,root,containers,result['copies'],retired=historical or observed_state=='retired')
    if observed_state=='retired':
        if any(item[1].get('State',{}).get('Running') is not False for item in containers.values()):
            raise RuntimeError('Rehearsal runtime retirement is not observed')
    elif observed_state in {'upgraded','switched'}:
        private_health(spec,containers)
    print('rehearsal_result='+json.dumps(result).encode().hex())
    return


def main():
    global COMMAND_FAILURE_CONTEXT
    COMMAND_FAILURE_CONTEXT=None
    spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
    record=spec['record']
    namespace=record['rehearsal_id']
    if not re.fullmatch(r'lp-rehearsal-[a-f0-9]{32}',namespace):
        raise RuntimeError('Invalid rehearsal identity')
    root=Path.home()/'.local/state/launchplane-rehearsals'/namespace
    if root.is_symlink() or root.resolve()!=root.absolute():
        raise RuntimeError('Rehearsal custody path is aliased')
    action=spec['action']
    if action=='retire-incomplete':
        incomplete_retirement(spec,root)
        return
    receipt=root/(action+'.json')
    if spec['mode']=='observe':
        lock_path=root/'.execution.lock'
        if lock_path.is_symlink():
            raise RuntimeError('Rehearsal execution lock is aliased')
        try:
            lock=lock_path.open('r')
        except FileNotFoundError:
            observe(spec,root,receipt)
        else:
            with lock:
                try:
                    fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                except BlockingIOError:
                    print('rehearsal_result='+json.dumps({
                        'intent_sha256':record['intent_sha256'],'host_busy':True}).encode().hex())
                else:
                    observe(spec,root,receipt,lock_held=True)
        return
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(root,0o700)
    with (root/'.execution.lock').open('a') as lock,command_failure_scope(spec,root):
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if receipt.exists():
            raise RuntimeError('Completed effect requires observation, never replay')
        if (root/(action+'.started')).exists():
            raise RuntimeError('Uncertain original effect retains its custody')
        refused=read_refusal(spec,root)
        if refused is not None:
            print('rehearsal_result='+json.dumps(refused).encode().hex())
            return
        started=time.monotonic()
        spec['phase_elapsed']={}
        names=database_names(spec)
        database,image=current(spec)
        if action=='prepare':
            (root/(action+'.started')).touch(mode=0o600)
            compose(spec,root,image=spec['source_image'],database=names['source'])
            deadline=time.monotonic()+120
            while True:
                try:
                    containers=inspection(spec,names['source'],spec['source_image'])
                    sql(containers,'SELECT 1')
                    break
                except Exception:
                    if time.monotonic()>deadline: raise
                    time.sleep(1)
            docker('stop',containers['web'][0])
            # Prove both the native SQL-before-registry boundary and the source
            # modules exist before reading any business data.
            docker('exec',containers['script-runner'][0],'python3','-c',"import sys;sys.path.insert(0,'/volumes/scripts');from run_odoo_data_workflows import OdooDataWorkflowRunner as R;assert all(callable(getattr(R,k,None)) for k in ('prepare_credentials_before_registry','_prepare_restored_database','module_update_witnesses','_module_states_by_name'))")
            state='prepared'; copies=[]
        else:
            code=''
            try:
                containers=inspection(spec,database,image)
                validate_snapshots(spec,root,containers,record['copies'])
                if action in {'execute','simulate-final','verify'} and not rehearsal_storage_capacity(
                    spec,root,containers,action):
                    code='rehearsal_storage_capacity_insufficient'
                if action=='retire' and not {names['final_source'],names['final_upgrade']} <= {copy['database'] for copy in record['copies']}:
                    raise RuntimeError('Both recovery states are required before retirement')
            except Exception:
                # These checks are read-only and no effect marker exists yet;
                # malformed provider observations are determinate refusals too.
                code='rehearsal_precheck_refused'
            if code:
                path=refusal_receipt(spec,root,code)
                if path is None:
                    raise RuntimeError('Missing refusal attempt identity')
                # Creation itself is the complete no-effect proof. There is no
                # partial JSON/rename window to strand a killed refusal writer.
                descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                descriptor=os.open(root,os.O_RDONLY|os.O_DIRECTORY)
                try:
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                result=refusal_result(spec,code)
                print('rehearsal_result='+json.dumps(result).encode().hex())
                return
            (root/(action+'.started')).touch(mode=0o600)
            copies=list(record['copies'])
            if action=='execute':
                raw=capture(spec,root,containers,names['source'])
                copies.append(snapshot(spec,root,containers,names['source']))
                clone(spec,containers,names['source'],names['upgrade'])
                sql(containers,'ALTER DATABASE "'+names['source']+'" SET default_transaction_read_only=on')
                compose(spec,root,image=spec['target_image'],database=names['upgrade'])
                containers=inspection(spec,names['upgrade'],spec['target_image'])
                docker('stop',containers['web'][0])
                sql(containers,'ALTER DATABASE "'+names['upgrade']+'" SET default_transaction_read_only=off')
                upgrade_started=time.monotonic()
                copy_runner(spec,containers,names['upgrade'],fresh=False)
                spec['phase_elapsed']['initial-upgrade']=time.monotonic()-upgrade_started
                before_web(spec,containers,names['upgrade'])
                private_health(spec,containers)
                shutil.rmtree(raw)
                state='upgraded'
            elif action=='simulate-final':
                # Freeze/capture/switch affects the already sanitized source
                # copy only. No second read or fence touches the business host.
                docker('stop',containers['web'][0])
                restore_preserved_snapshot(spec,root,containers,names['source'],names['final_source'])
                sql(containers,'ALTER DATABASE "'+names['final_source']+'" SET default_transaction_read_only=off')
                copies.append(snapshot(spec,root,containers,names['final_source']))
                clone(spec,containers,names['final_source'],names['final_upgrade'])
                spec['phase_elapsed']['final-capture-simulation']=time.monotonic()-started
                sql(containers,'ALTER DATABASE "'+names['final_source']+'" SET default_transaction_read_only=on')
                compose(spec,root,image=spec['target_image'],database=names['final_upgrade'])
                containers=inspection(spec,names['final_upgrade'],spec['target_image'])
                docker('stop',containers['web'][0])
                sql(containers,'ALTER DATABASE "'+names['final_upgrade']+'" SET default_transaction_read_only=off')
                upgrade_started=time.monotonic()
                copy_runner(spec,containers,names['final_upgrade'],fresh=False)
                spec['phase_elapsed']['final-upgrade']=time.monotonic()-upgrade_started
                switch_started=time.monotonic()
                before_web(spec,containers,names['final_upgrade'])
                private_health(spec,containers)
                spec['phase_elapsed']['private-switch']=time.monotonic()-switch_started
                state='switched'
            elif action=='verify':
                private_health(spec,containers)
                # Fence the disposable writer before preserving its post-write
                # state. Verification remains private and web stays stopped.
                docker('stop',containers['web'][0])
                copies.append(snapshot(spec,root,containers,names['final_upgrade']))
                state='verified'
            elif action=='retire':
                docker('stop',*[item[0] for item in containers.values()])
                state='retired'
            else:
                raise RuntimeError('Unsupported rehearsal effect')
        elapsed={**record['elapsed_seconds'],**spec.get('phase_elapsed',{}),action:time.monotonic()-started}
        result={'intent_sha256':record['intent_sha256'],'state':state,'copies':copies,'elapsed_seconds':elapsed,
            'resources_preserved':True,'public_route':False,'business_host_frozen':False}
        save(receipt,result)
        print('rehearsal_result='+json.dumps(result).encode().hex())

main()
"""
)
