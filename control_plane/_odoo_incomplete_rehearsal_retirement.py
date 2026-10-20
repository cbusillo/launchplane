"""Fixed host disposal for an idle incomplete standalone rehearsal."""

INCOMPLETE_REHEARSAL_RETIREMENT_LIBRARY = r"""
def incomplete_retirement(spec, root):
    record=spec['record']
    namespace=record['rehearsal_id']
    if record['state']!='prepared' or record['copies'] or spec['mode'] not in {'dry-run','apply'}:
        raise RuntimeError('Only an incomplete prepared rehearsal can be disposed')
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
    binding={'intent':record['intent_sha256'],'record':spec['record_binding_sha256'],
        'attempt':spec['attempt_sha256'],'resources':spec['resources_binding_sha256']}
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
        manifest=json.loads(marker.read_text())
        if manifest['binding']!=binding: raise RuntimeError('Disposal belongs to another operation')
    else:
        if not (root/'execute.started').is_file() or (root/'execute.started').is_symlink() or (root/'execute.json').exists():
            raise RuntimeError('Original execute is not incomplete')
        database,image=current(spec)
        containers=inspection(spec,database,image)
        if containers['web'][1].get('State',{}).get('Running') is not False:
            raise RuntimeError('An active rehearsal writer cannot be retired as incomplete')
        own={item[1]['Id'] for item in containers.values()}
        volumes={namespace+suffix for suffix in ('-data','-db','-logs')}
        backings=inspect_volume_backings(volumes,all_containers,own,
            expected_binding=spec['resources_binding_sha256'])
        network_name=next(iter(containers['database'][1]['NetworkSettings']['Networks']))
        network=one_inspection(network_name)
        manifest={'binding':binding,'containers':sorted(own),'volumes':backings,
            'network_name':network_name,'network_id':network['Id'],
            'root':[root.stat().st_dev,root.stat().st_ino]}
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
        if not marker.exists(): save(marker,manifest)
        if remaining:
            docker('stop',*sorted(remaining))
            docker('rm',*sorted(remaining))
        # Docker refuses volumes still in use; never force removal or prune.
        for name in sorted(volumes): docker('volume','rm',name)
        if network_present: docker('network','rm',manifest['network_id'])
        for path in root.iterdir():
            if path.name in {'.execution.lock',marker.name}: continue
            if path.is_symlink(): path.unlink()
            elif path.is_dir(): shutil.rmtree(path)
            else: path.unlink()
    # Observe actual absence, rather than treating the deletion return as proof.
    containers_absent=not any(item['Id'] in manifest['containers'] or
        (item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')==namespace
        for item in inventory())
    volumes_absent=not set(manifest['volumes']) & set(docker('volume','ls','--format','{{.Name}}').decode().splitlines())
    network_absent=manifest['network_name'] not in set(docker('network','ls','--format','{{.Name}}').decode().splitlines())
    raw_absent=all(path.name in {'.execution.lock',marker.name} for path in root.iterdir())
    disposed=containers_absent and volumes_absent and network_absent and raw_absent
    if spec['mode']=='apply' and not disposed: raise RuntimeError('Disposal is incomplete')
    result={'qualified':True,'disposed':disposed,'binding_sha256':binding_sha256,
        'intent_sha256':record['intent_sha256'],'attempt_sha256':spec['attempt_sha256'],
        'containers_absent':containers_absent,'volumes_absent':volumes_absent,
        'network_absent':network_absent,'raw_source_absent':raw_absent,
        'public_route':False,'business_host_frozen':False}
    print('rehearsal_result='+json.dumps(result).encode().hex())
    return result
"""
