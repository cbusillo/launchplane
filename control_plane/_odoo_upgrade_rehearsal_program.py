"""Host program for a credential-free-control-plane, privately routed rehearsal.

Only the separately owned compose and its data are writable. Source SSH is read
only, and raw failure material remains outside every application mount.
"""

ODOO_UPGRADE_REHEARSAL_PROGRAM = r"""
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


def command(arguments, *, stdin=None, stdout=None, environment=None):
    result = subprocess.run(arguments, input=stdin, stdout=stdout or subprocess.PIPE,
        stderr=subprocess.PIPE, env=environment, check=False)
    if result.returncode:
        raise RuntimeError('Rehearsal command failed; preserve its private custody')
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
    process=subprocess.Popen(arguments,stdout=subprocess.PIPE,stderr=subprocess.DEVNULL)
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
        if process.wait(timeout=30) or not size:
            raise RuntimeError('Incomplete source copy')
    finally:
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
    for key in ('ODOO_UPSTREAM_HOST','ODOO_UPSTREAM_USER','ODOO_UPSTREAM_DB_USER','ODOO_UPSTREAM_DB_NAME'):
        if not re.fullmatch(r'[A-Za-z0-9_][A-Za-z0-9_.:-]*',values[key]):
            raise RuntimeError('Unsupported source coordinate')
    filestore=values['ODOO_UPSTREAM_FILESTORE_PATH']
    if not re.fullmatch(r'/[A-Za-z0-9_./-]+',filestore) or '..' in Path(filestore).parts:
        raise RuntimeError('Unsupported source filestore')
    ssh_directory=Path(values.get('DATA_WORKFLOW_SSH_DIR') or '/home/ubuntu/.ssh')
    key=values.get('DATA_WORKFLOW_SSH_KEY','')
    for prefix in ('/root/.ssh/','/home/ubuntu/.ssh/'):
        if key.startswith(prefix):
            key=str(ssh_directory/Path(key).name)
            break
    if not key:
        key=next((str(ssh_directory/name) for name in ('id_ed25519','id_ecdsa','id_rsa') if (ssh_directory/name).is_file()),'')
    if not key or not Path(key).is_file() or not (ssh_directory/'known_hosts').is_file():
        raise RuntimeError('Existing read-only source identity is unavailable')
    ssh=['ssh','-o','BatchMode=yes','-o','StrictHostKeyChecking=yes','-o','IdentitiesOnly=yes','-o','UserKnownHostsFile='+str(ssh_directory/'known_hosts'),'-i',key]
    destination=values['ODOO_UPSTREAM_USER']+'@'+values['ODOO_UPSTREAM_HOST']
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
    validation="import hashlib,os,stat,psycopg2;from pathlib import Path\nc=psycopg2.connect(host='database',user=os.environ['ODOO_DB_USER'],password=os.environ['ODOO_DB_PASSWORD'],dbname=os.environ['ODOO_DB_NAME'])\nwith c.cursor() as q:\n q.execute('SELECT store_fname,checksum,file_size FROM ir_attachment WHERE store_fname IS NOT NULL')\n for name,checksum,size in q:\n  assert name==checksum[:2]+'/'+checksum and len(checksum)==40 and all(ch in '0123456789abcdef' for ch in checksum); p=Path('/volumes/data/filestore')/os.environ['ODOO_DB_NAME']/name; assert not p.is_symlink() and not p.parent.is_symlink() and stat.S_ISREG(p.stat().st_mode); assert p.stat().st_size==size; h=hashlib.sha1(); f=p.open('rb')\n  for chunk in iter(lambda:f.read(1048576),b''): h.update(chunk)\n  f.close(); assert h.hexdigest()==checksum\nc.close()\n"
    docker('exec','-e','ODOO_DB_NAME='+database,containers['script-runner'][0],'python3','-c',validation)
    return raw


def main():
    spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
    record=spec['record']
    namespace=record['rehearsal_id']
    if not re.fullmatch(r'lp-rehearsal-[a-f0-9]{32}',namespace):
        raise RuntimeError('Invalid rehearsal identity')
    root=Path.home()/'.local/state/launchplane-rehearsals'/namespace
    if root.is_symlink() or root.resolve()!=root.absolute():
        raise RuntimeError('Rehearsal custody path is aliased')
    action=spec['action']
    receipt=root/(action+'.json')
    if spec['mode']=='observe':
        if not receipt.is_file() or receipt.is_symlink():
            copies=[]
            for database in database_names(spec).values():
                saved=root/database/'receipt.json'
                if saved.is_file() and not saved.is_symlink():
                    copy=json.loads(saved.read_text())
                    validate_snapshots(spec,root,{},[copy],retired=True)
                    copies.append(copy)
            result={'intent_sha256':record['intent_sha256'],'state':record['state'],'copies':copies,
                'elapsed_seconds':record['elapsed_seconds'],'resources_preserved':True,'public_route':False,
                'business_host_frozen':False,'completed':False,'raw_source_retained':(root/'raw-source').is_dir()}
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
    root.mkdir(mode=0o700,parents=True,exist_ok=True)
    os.chmod(root,0o700)
    with (root/'.execution.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if receipt.exists():
            raise RuntimeError('Completed effect requires observation, never replay')
        if (root/(action+'.started')).exists():
            raise RuntimeError('Uncertain original effect retains its custody')
        (root/(action+'.started')).touch(mode=0o600)
        started=time.monotonic()
        spec['phase_elapsed']={}
        names=database_names(spec)
        database,image=current(spec)
        if action=='prepare':
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
            containers=inspection(spec,database,image)
            validate_snapshots(spec,root,containers,record['copies'])
            budget=record['storage']
            headroom={**budget,'database_bytes':budget['database_bytes']*4,
                'filestore_bytes':budget['filestore_bytes']*4,
                'staging_bytes':budget['staging_bytes']+4*(budget['database_bytes']+budget['filestore_bytes'])}
            if action in {'execute','simulate-final'} and not storage_capacity(
                {'generation':{'namespace':namespace},'staging_directory':str(root)},containers,headroom):
                raise RuntimeError('Insufficient independent backing capacity')
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
                if not {names['final_source'],names['final_upgrade']} <= {copy['database'] for copy in copies}:
                    raise RuntimeError('Both recovery states are required before retirement')
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
