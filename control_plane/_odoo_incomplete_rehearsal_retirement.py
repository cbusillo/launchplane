"""Fixed host disposal for an idle incomplete or completed later-phase standalone rehearsal."""

INCOMPLETE_REHEARSAL_RETIREMENT_LIBRARY = r"""
def incomplete_initial_snapshot(spec,root,*,allow_partial):
    names=database_names(spec)
    for role in ('upgrade','final_source','final_upgrade'):
        path=root/names[role]
        if path.exists() or path.is_symlink():
            raise RuntimeError('A later recovery snapshot cannot be disposed as initial failure')
    path=root/names['source']
    if not path.exists() and not path.is_symlink(): return None
    if not stat.S_ISDIR(path.lstat().st_mode):
        raise RuntimeError('Initial snapshot directory is aliased')
    receipt_path=path/'receipt.json'
    if allow_partial and not receipt_path.exists() and not receipt_path.is_symlink():
        return None
    for item in (root/'capture.json',path/'receipt.json',path/'database.dump',path/'filestore.tar'):
        if not stat.S_ISREG(item.lstat().st_mode):
            raise RuntimeError('Initial snapshot custody is aliased')
    if (path/'receipt.json').stat().st_size>16384:
        raise RuntimeError('Initial snapshot receipt is unbounded')
    receipt=json.loads((path/'receipt.json').read_text())
    if (not isinstance(receipt,dict) or set(receipt)!={'database','witness_sha256',
            'database_sha256','filestore_sha256','source_capture_sha256',
            'sanitized','odoo_version','captured_at'}
            or receipt['database']!=names['source'] or receipt['sanitized'] is not True
            or not isinstance(receipt['odoo_version'],str)
            or not re.fullmatch(r'[0-9]+(?:\.[0-9]+)*',receipt['odoo_version'])
            or receipt['odoo_version'].split('.')[0]!=spec['record']['source_odoo_version'].split('.')[0]
            or not isinstance(receipt['captured_at'],str)
            or datetime.fromisoformat(receipt['captured_at']).utcoffset() is None
            or any(not isinstance(receipt[key],str) or not re.fullmatch('[a-f0-9]{64}',receipt[key])
                for key in ('witness_sha256','database_sha256','filestore_sha256','source_capture_sha256'))):
        raise RuntimeError('Initial snapshot receipt is unverified')
    validate_snapshots(spec,root,{},[receipt],retired=True)
    return receipt


LATER_PHASE_ROLES={'upgraded':('source',),'switched':('source','final_source')}


def same_receipt(saved,recorded):
    # Launchplane and the host serialize the same capture instant differently.
    return (isinstance(saved,dict) and isinstance(recorded,dict)
        and {key:value for key,value in saved.items() if key!='captured_at'}
            =={key:value for key,value in recorded.items() if key!='captured_at'}
        and isinstance(saved.get('captured_at'),str) and isinstance(recorded.get('captured_at'),str)
        and datetime.fromisoformat(saved['captured_at'])==datetime.fromisoformat(recorded['captured_at']))


def later_phase_snapshots(spec,root):
    # Bind exactly the recorded recovery snapshots of a settled later phase.
    record=spec['record']
    names=database_names(spec)
    copies=record['copies']
    roles=LATER_PHASE_ROLES[record['state']]
    if [copy['database'] for copy in copies]!=[names[role] for role in roles]:
        raise RuntimeError('Recorded recovery copies do not match the rehearsal phase')
    for role,database in names.items():
        if role not in roles and ((root/database).exists() or (root/database).is_symlink()):
            raise RuntimeError('An unrecorded recovery snapshot cannot be disposed')
    if (root/'capture.pending').exists() or (root/'capture.pending').is_symlink():
        raise RuntimeError('A pending capture is not a settled phase')
    for copy in copies:
        path=root/copy['database']
        for item in (path,root/'capture.json',path/'database.dump',path/'filestore.tar'):
            if item.is_symlink() or not item.exists():
                raise RuntimeError('Recovery snapshot custody is missing or aliased')
    # Witness, lineage and bytes must still be exactly the recorded tuples.
    validate_snapshots(spec,root,{},copies,retired=True)
    # Every started effect settled with its receipt; none may still be writing.
    receipts=[]
    for marker in root.iterdir():
        if marker.name.endswith('.started'):
            receipt=root/(marker.name[:-len('.started')]+'.json')
            if receipt.is_symlink() or not receipt.is_file():
                raise RuntimeError('Uncertain original effect retains its custody')
            receipts.append(json.loads(receipt.read_text()))
    if not any(isinstance(saved,dict) and saved.get('intent_sha256')==record['intent_sha256']
            and saved.get('state')==record['state'] and isinstance(saved.get('copies'),list)
            and len(saved['copies'])==len(copies)
            and all(same_receipt(left,right) for left,right in zip(saved['copies'],copies))
            for saved in receipts):
        raise RuntimeError('No settled operation published this phase')
    return copies


def incomplete_retirement(spec, root):
    record=spec['record']
    namespace=record['rehearsal_id']
    if spec['mode'] not in {'dry-run','apply'} or not (
            (record['state']=='prepared' and not record['copies'])
            or (record['state'] in LATER_PHASE_ROLES and record['copies'])):
        raise RuntimeError('Only an incomplete prepared or settled later-phase rehearsal can be disposed')
    if not root.is_dir() or root.is_symlink() or root.resolve()!=root.absolute():
        raise RuntimeError('Missing private rehearsal custody')
    lock_path=root/'.execution.lock'
    if lock_path.is_symlink() or not lock_path.is_file():
        raise RuntimeError('Missing original execution fence')
    with lock_path.open('r') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return incomplete_retirement_locked(spec,root)


def incomplete_retirement_locked(spec,root):
    record=spec['record']
    namespace=record['rehearsal_id']
    marker=root/'incomplete-retirement.json'
    refusals=root/'incomplete-retirement-refusals.json'
    binding={'intent':record['intent_sha256'],'record':spec['record_binding_sha256'],
        'attempt':spec['attempt_sha256'],'resources':spec['resources_binding_sha256']}
    progress=record.get('incomplete_retirement') or {}
    review=progress.get('reviewed_plan_sha256')
    def retirement_save(path,payload):
        # The original flock excludes all writers. A killed save may leave its
        # uncommitted temporary file; never let it strand the admitted attempt.
        path.with_suffix('.pending').unlink(missing_ok=True)
        save(path,payload)
    if refusals.is_symlink(): raise RuntimeError('Aliased refusal receipt')
    rejected={'binding':binding,'reviews':[]}
    if refusals.exists():
        rejected=json.loads(refusals.read_text())
        if rejected['binding']!=binding: raise RuntimeError('Refusal belongs to another operation')
    if spec['mode']=='apply' and (not review or review in rejected['reviews']):
        raise RuntimeError('Missing or previously refused retirement review')
    def inventory():
        return [one_inspection(identifier) for identifier in docker('ps','-aq').decode().splitlines()]
    all_containers=inventory()
    # Reuse the copy guard's host-custody exclusion, including read-only mounts.
    for container in all_containers:
        mounts=container.get('Mounts')
        if not isinstance(mounts,list): raise RuntimeError('Incomplete mount inventory')
        for mount in mounts:
            if mount.get('Source'):
                path=Path(mount['Source']).resolve()
                if root.is_relative_to(path) or path.is_relative_to(root):
                    raise RuntimeError('Raw custody has a container consumer')
    if marker.is_symlink(): raise RuntimeError('Aliased disposal receipt')
    if marker.exists():
        admitted=json.loads(marker.read_text())
        manifest=admitted['manifest']
        if manifest['binding']!=binding: raise RuntimeError('Disposal belongs to another operation')
        if admitted['reviewed_plan_sha256']!=review: raise RuntimeError('Disposal review changed')
    elif record['copies']:
        snapshots=later_phase_snapshots(spec,root)
        database,image=current(spec)
        containers=inspection(spec,database,image)
        # The private web may serve the settled copy; an exec is an unsettled writer.
        if any('ExecIDs' not in item[1] or item[1]['ExecIDs'] not in (None,[])
                for item in containers.values()):
            raise RuntimeError('An active rehearsal exec cannot be retired')
    else:
        if not (root/'execute.started').is_file() or (root/'execute.started').is_symlink() or (root/'execute.json').exists():
            raise RuntimeError('Original execute is not incomplete')
        names=database_names(spec)
        runners=[item for item in all_containers if
            (item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')==namespace
            and (item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.service')=='script-runner']
        if len(runners)!=1: raise RuntimeError('Incomplete runtime selection is unverified')
        environment=dict(value.split('=',1) for value in runners[0]['Config'].get('Env',[]) if '=' in value)
        database=environment.get('ODOO_DB_NAME')
        snapshot=incomplete_initial_snapshot(spec,root,allow_partial=database==names['source'])
        if database==names['source']:
            image=spec['source_image']
        elif database==names['upgrade'] and snapshot is not None:
            image=spec['target_image']
            if (root/'capture.pending').exists() or (root/'capture.pending').is_symlink():
                raise RuntimeError('Initial capture is still pending')
        else:
            raise RuntimeError('Incomplete runtime selection is unverified')
        containers=inspection(spec,database,image)
        if containers['web'][1].get('State',{}).get('Running') is not False:
            raise RuntimeError('An active rehearsal writer cannot be retired as incomplete')
    if not marker.exists():
        own={item[1]['Id'] for item in containers.values()}
        volumes={namespace+suffix for suffix in ('-data','-db','-logs')}
        backings=inspect_volume_backings(volumes,all_containers,own,
            expected_binding=spec['resources_binding_sha256'])
        network_name=next(iter(containers['database'][1]['NetworkSettings']['Networks']))
        network=one_inspection(network_name)
        manifest={'binding':binding,'containers':sorted(own),'volumes':backings,
            'network_name':network_name,'network_id':network['Id'],
            'root':[root.stat().st_dev,root.stat().st_ino]}
        if record['copies']:
            manifest['initial_source_snapshot']=None
            manifest['recovery_snapshots']=snapshots
        else:
            manifest['initial_source_snapshot']=snapshot
    if manifest['root']!=[root.stat().st_dev,root.stat().st_ino]:
        raise RuntimeError('Custody directory changed')
    binding_sha256=hashlib.sha256(json.dumps(manifest,sort_keys=True,separators=(',',':')).encode()).hexdigest()
    remaining={item['Id']:item for item in all_containers if item['Id'] in manifest['containers']}
    if any((item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')==namespace
        and item['Id'] not in manifest['containers'] for item in all_containers):
        raise RuntimeError('Another runtime appeared in the retired namespace')
    for item in remaining.values():
        if (item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')!=namespace:
            raise RuntimeError('Container ownership changed')
    names=set(docker('volume','ls','--format','{{.Name}}').decode().splitlines())
    volumes=set(manifest['volumes']) & names
    backings=inspect_volume_backings(volumes,all_containers,set(remaining),
        expected_binding=spec['resources_binding_sha256'])
    if any(backings[name]!=manifest['volumes'][name] for name in volumes):
        raise RuntimeError('Storage identity changed')
    networks=set(docker('network','ls','--format','{{.Name}}').decode().splitlines())
    network_present=manifest['network_name'] in networks
    if network_present:
        network=one_inspection(manifest['network_name'])
        if network.get('Id')!=manifest['network_id'] or network.get('Internal') is not True or not isinstance(network.get('Containers'),dict) or not set(network['Containers'])<=set(remaining):
            raise RuntimeError('Network ownership changed')
    # No uninspected mount, even in an owned container, may acquire disposable storage.
    for item in all_containers:
        for mount in item['Mounts']:
            if mount.get('Name') in manifest['volumes'] and item['Id'] not in remaining:
                raise RuntimeError('Storage has another consumer')
    if spec['mode']=='apply':
        if progress.get('host_binding_sha256')!=binding_sha256:
            if not marker.exists():
                rejected['reviews']=sorted(set(rejected['reviews'])|{review})
                retirement_save(refusals,rejected)
            raise RuntimeError('Reviewed inventory changed; obtain a fresh dry-run')
        if not marker.exists():
            retirement_save(marker,{'manifest':manifest,'reviewed_plan_sha256':review})
        if remaining:
            docker('stop',*sorted(remaining))
            docker('rm',*sorted(remaining))
        # Docker refuses volumes still in use; never force removal or prune.
        for name in sorted(volumes): docker('volume','rm',name)
        if network_present: docker('network','rm',manifest['network_id'])
        # Retained recovery material outlives uncertain provider deletion. A
        # successful deletion reply is insufficient; observe all resources gone
        # before touching the raw capture or the verified initial snapshot.
        if (any(item['Id'] in manifest['containers'] or
                (item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')==namespace
                for item in inventory())
                or set(manifest['volumes']) & set(docker('volume','ls','--format','{{.Name}}').decode().splitlines())
                or manifest['network_name'] in set(docker('network','ls','--format','{{.Name}}').decode().splitlines())):
            raise RuntimeError('Resource absence is unverified; preserve recovery custody')
        for path in root.iterdir():
            if path.name in {'.execution.lock',marker.name,refusals.name}: continue
            if path.is_symlink(): path.unlink()
            elif path.is_dir(): shutil.rmtree(path)
            else: path.unlink()
    # Observe actual absence, rather than treating the deletion return as proof.
    containers_absent=not any(item['Id'] in manifest['containers'] or
        (item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')==namespace
        for item in inventory())
    volumes_absent=not set(manifest['volumes']) & set(docker('volume','ls','--format','{{.Name}}').decode().splitlines())
    network_absent=manifest['network_name'] not in set(docker('network','ls','--format','{{.Name}}').decode().splitlines())
    raw_absent=all(path.name in {'.execution.lock',marker.name,refusals.name} for path in root.iterdir())
    disposed=containers_absent and volumes_absent and network_absent and raw_absent
    if spec['mode']=='apply' and not disposed: raise RuntimeError('Disposal is incomplete')
    result={'qualified':True,'disposed':disposed,'binding_sha256':binding_sha256,
        'intent_sha256':record['intent_sha256'],'attempt_sha256':spec['attempt_sha256'],
        'refused_reviews':rejected['reviews'],'disposal_started':marker.exists(),
        'containers_absent':containers_absent,'volumes_absent':volumes_absent,
        'network_absent':network_absent,'raw_source_absent':raw_absent,
        'initial_source_snapshot':manifest.get('initial_source_snapshot'),
        'recovery_snapshots':manifest.get('recovery_snapshots',[]),
        'public_route':False,'business_host_frozen':False}
    emit_rehearsal_result(result)
    return result
"""
