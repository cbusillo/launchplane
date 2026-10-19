"""Fixed, original-copy-only native drill retirement and storage removal."""

NATIVE_MAINTENANCE_LIBRARY = r"""
def native_host_clock():
    return {'host_boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'host_monotonic':time.clock_gettime(time.CLOCK_BOOTTIME),'host_observed_at':time.time()}


class NativeMaintenanceHost:
    def __init__(self, spec, command, *, receipt_root=None, lock_root=None, clock=None, monotonic=None):
        self.spec, self.command = spec, command
        self.clock = clock or time.time
        self.monotonic = monotonic or time.monotonic
        self.namespace = spec['identity']['namespace']
        if not re.fullmatch(r'lp-testing-[a-f0-9]{32}', self.namespace):
            raise ValueError('Invalid original namespace')
        self.identity = spec['identity']
        self.digest = hashlib.sha256(json.dumps(self.identity,sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if spec['identity_sha256'] != self.digest:
            raise ValueError('Original maintenance identity changed')
        self.receipt_root, self.lock_root = receipt_root, lock_root
        remaining = spec.get('expires_at', 0) - self.clock()
        self.deadline = self.monotonic() + max(0, min(remaining, 120))
        self.directory = None
        self.descriptors = []

    def current(self):
        host=native_host_clock()
        if host['host_boot_id']!=self.spec.get('host_boot_id') or host['host_monotonic']>=self.spec.get('host_expires_at',0) or self.clock() >= self.spec.get('expires_at', 0) or self.monotonic() >= self.deadline:
            raise TimeoutError('Original native maintenance admission expired')

    def docker(self, *arguments, stdin=None, mutation=False):
        if mutation:
            self.current()
        return self.command(*arguments, stdin=stdin, pass_fds=tuple(self.descriptors),
            timeout=max(.01,min(30,self.deadline-self.monotonic())) if mutation else 30)

    def open(self):
        # The shared copy lock coordinates existing restore programs. It is not
        # copy authority; immutable original receipts and completed creators are.
        path = Path(self.lock_root or tempfile.gettempdir()) / (self.namespace+'-copy.lock')
        descriptor = os.open(path,os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600)
        self.descriptors.append(descriptor)
        metadata=os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=os.geteuid() or metadata.st_mode&0o022:
            raise ValueError('Native execution lock is not host-owned')
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        anchor=Path(self.receipt_root).resolve(strict=True) if self.receipt_root is not None else Path.home().resolve(strict=True)
        directory=os.open(anchor,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW)
        self.directory=directory
        components=() if self.receipt_root is not None else ('.local','state','launchplane-testing-disposal',self.namespace)
        self.state_path=anchor.joinpath(*components)
        for component in components:
            try:
                os.mkdir(component,0o700,dir_fd=directory)
            except FileExistsError:
                pass
            child=os.open(component,os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW,dir_fd=directory)
            metadata=os.fstat(child)
            if metadata.st_uid!=os.geteuid() or metadata.st_mode&0o022:
                os.close(child)
                raise ValueError('Native disposal state is not host-owned')
            os.fsync(directory);os.close(directory)
            directory=child;self.directory=child
        descriptor=os.open('native.lock',os.O_CREAT|os.O_RDWR|os.O_NOFOLLOW,0o600,dir_fd=directory)
        self.descriptors.append(descriptor)
        metadata=os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=os.geteuid() or metadata.st_mode&0o077:
            raise ValueError('Native receipt lock is unsafe')
        fcntl.flock(descriptor,fcntl.LOCK_EX|fcntl.LOCK_NB)
        return self

    def close(self):
        for descriptor in reversed(self.descriptors):
            os.close(descriptor)
        self.descriptors=[]
        if self.directory is not None:
            os.close(self.directory);self.directory=None

    def read(self):
        try:
            descriptor=os.open('native-'+self.digest+'.json',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=self.directory)
        except FileNotFoundError:
            return None
        with os.fdopen(descriptor) as handle:
            metadata=os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=os.geteuid() or metadata.st_mode&0o077:
                raise ValueError('Native receipt is unsafe')
            receipt=json.loads(handle.read(65537))
        if receipt.get('identity')!=self.identity:
            raise ValueError('Native receipt belongs to another original copy')
        return receipt

    def save(self, receipt):
        name='native-'+self.digest+'.'+uuid.uuid4().hex+'.tmp'
        descriptor=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=self.directory)
        try:
            with os.fdopen(descriptor,'w') as handle:
                json.dump(receipt,handle,sort_keys=True,separators=(',',':'))
                handle.flush();os.fsync(handle.fileno())
            os.replace(name,'native-'+self.digest+'.json',src_dir_fd=self.directory,dst_dir_fd=self.directory)
            os.fsync(self.directory)
        finally:
            try:
                os.unlink(name,dir_fd=self.directory)
            except FileNotFoundError:
                pass

    def inventory(self):
        identifiers=self.docker('ps','-aq').decode().splitlines()
        return json.loads(self.docker('inspect',*identifiers)) if identifiers else []

    def volume(self, name):
        volumes=json.loads(self.docker('volume','inspect',name))
        if len(volumes)!=1:
            raise ValueError('Original volume inventory is ambiguous')
        volume=volumes[0]
        if volume.get('Name')!=name or volume.get('Driver')!='local' or volume.get('Options') not in (None,{}):
            raise ValueError('Native volume custody changed')
        if not isinstance(volume.get('CreatedAt'),str) or not volume['CreatedAt']:
            raise ValueError('Native volume creation identity is unavailable')
        if (volume.get('Labels') or {}).get('io.launchplane.testing-resources')!=self.identity['resources_binding']:
            raise ValueError('Native volume allocation changed')
        path=Path(volume['Mountpoint']).resolve(strict=True)
        metadata=path.stat()
        return {'name':name,'created_at':volume.get('CreatedAt'),'labels':volume.get('Labels') or {},
            'path':str(path),'device':metadata.st_dev,'inode':metadata.st_ino}

    @staticmethod
    def fingerprint(container):
        state=container.get('State',{})
        if any(not isinstance(state.get(name),str) for name in ('StartedAt','FinishedAt','Status')) or type(container.get('RestartCount')) is not int or container['RestartCount']<0:
            raise ValueError('Native process lifetime is unavailable')
        return {'id':container['Id'],'labels':container.get('Config',{}).get('Labels') or {},
            'started_at':state.get('StartedAt'),'finished_at':state.get('FinishedAt'),
            'restart_count':container.get('RestartCount'),'state':state.get('Status')}

    def qualify(self):
        self.current()
        existing=self.read()
        if existing is not None:
            return self.inspect(existing)
        containers=inspect_copy_runtime(self.spec,expected_instance='testing')
        own={value[1]['Id'] for value in containers.values()}
        all_containers=self.inventory()
        logical=qualify_retained_copy(all_containers,{'project':self.spec['compose_project'],
            'context':self.identity['context'],'identity':self.spec['runtime_identity']})
        backings=inspect_volume_backings(logical['volumes'],all_containers,own)
        physical=hashlib.sha256(json.dumps({'logical':logical['resource_binding'],'backings':backings},sort_keys=True,separators=(',',':')).encode()).hexdigest()
        if physical!=self.identity['physical_binding']:
            raise ValueError('Original retirement physical backing changed')
        database=containers['database'][1]
        networks={}
        for network_name in database['NetworkSettings']['Networks']:
            network=json.loads(self.docker('inspect',network_name))[0]
            if not isinstance(network.get('Containers'),dict) or not set(network['Containers'])<=own:
                raise ValueError('Native private network has a foreign consumer')
            if not isinstance(network.get('Id'),str) or not network['Id']:
                raise ValueError('Native private network identity is unavailable')
            labels=network.get('Labels') or {}
            if network.get('Name')!=self.spec['compose_project']+'_default' or labels.get('com.docker.compose.project')!=self.spec['compose_project'] or labels.get('com.docker.compose.network')!='default' or network.get('Driver')!='bridge' or network.get('Scope')!='local' or network.get('Ingress') is not False:
                raise ValueError('Native private network is not owned by its original Compose project')
            networks[network_name]={'id':network['Id'],'name':network['Name'],'labels':labels,
                'driver':network['Driver'],'scope':network['Scope'],'ingress':network['Ingress']}
        for _,container in containers.values():
            if container['Config'].get('Image')!=self.spec['image_reference'] and container is not database:
                raise ValueError('Retirement image changed')
            if container is not database:
                environment=dict(value.split('=',1) for value in container['Config'].get('Env',[]) if '=' in value)
                if json.loads(environment.get('LAUNCHPLANE_RUNTIME_IDENTITY_JSON','null'))!=self.spec['runtime_identity']:
                    raise ValueError('Retirement runtime identity changed')
        volumes={name:self.volume(name) for name in (self.namespace+'-data',self.namespace+'-db',self.namespace+'-logs')}
        receipt={'identity':self.identity,'state':'qualifying','volumes':volumes,'containers':{},
            'networks':networks,
            'qualification':False,'max_login_at':None,'observed_at':self.clock(),'resources':{},
            'invocation_expires_at':self.spec['expires_at'],
            'invocation_boot_id':self.spec['host_boot_id'],'invocation_host_expires_at':self.spec['host_expires_at']}
        self.save(receipt)
        for service in ('web','script-runner'):
            self.docker('stop','--timeout','-1',containers[service][1]['Id'],mutation=True)
        # No old database is started. A successful NULL is distinct from a
        # failed query. PostgreSQL timestamps in Odoo are naive UTC.
        try:
            if database.get('State',{}).get('Status')!='running':
                raise ValueError('Original database is not running')
            sql=("BEGIN READ ONLY; SET LOCAL statement_timeout='5s'; SELECT json_build_object("
                "'witness',(SELECT value FROM ir_config_parameter WHERE key=:'witness_key'),"
                "'max_login_at',(SELECT MAX(create_date) FROM res_users_log WHERE create_date>=:'prepared'::timestamp)); COMMIT;\n")
            output=self.docker('exec','-i',database['Id'],'sh','-c',
                'psql -X -q -t -A -v ON_ERROR_STOP=1 -v witness_key="$2" -v prepared="$3" -U "$POSTGRES_USER" -d "$1"',
                '_',self.spec['database_name'],self.spec['copy_witness_parameter'],self.spec['generation']['prepared_at'],stdin=sql.encode())
            usage=json.loads(output.decode())
            if usage.get('witness')!=self.identity['copy_binding']:
                raise ValueError('Original database witness changed')
            login=usage.get('max_login_at')
            if login is not None:
                login=datetime.fromisoformat(login.replace('Z','+00:00'))
                if login.utcoffset() is None:
                    login=login.replace(tzinfo=timezone.utc)
                prepared=datetime.fromisoformat(self.spec['generation']['prepared_at'].replace('Z','+00:00'))
                if prepared.utcoffset() is None or not prepared<=login<=datetime.fromtimestamp(self.clock(),timezone.utc):
                    raise ValueError('Original login timestamp is unreadable')
                login=login.isoformat()
            receipt.update(qualification=True,max_login_at=login)
        except (ValueError,RuntimeError,TimeoutError,subprocess.SubprocessError):
            # Ordinary admitted native retirement still preserves the copy.
            receipt['qualification']=False
        self.docker('stop','--timeout','-1',database['Id'],mutation=True)
        current=self.inventory()
        members=[item for item in current if item['Id'] in own]
        if len(members)!=len(own) or any(item.get('State',{}).get('Status') not in {'created','exited'} for item in members):
            raise ValueError('Original retirement remains unknown')
        if {name:self.volume(name) for name in volumes}!=volumes:
            raise ValueError('Retired storage backing changed')
        receipt.update(state='retained',containers={item['Id']:self.fingerprint(item) for item in members},observed_at=self.clock())
        self.save(receipt)
        return self.inspect(receipt)

    def inspect(self, receipt=None, *, continuing=False):
        receipt=receipt or self.read()
        if receipt is None:
            return {'status':'missing'}
        if receipt['state']=='qualifying':
            return {'status':'incomplete'}
        if receipt['state']=='disposed':
            return {'status':'disposed','identity_sha256':self.digest}
        current={item['Id']:item for item in self.inventory()}
        own=set(receipt['containers'])
        if not receipt.get('networks'):
            return {'status':'unqualified'}
        resources=receipt['resources']
        network_inventory=[json.loads(line) for line in self.docker('network','ls','--no-trunc','--format','{{json .}}').decode().splitlines()]
        if any(not isinstance(item,dict) or not item.get('ID') or not item.get('Name') for item in network_inventory):
            raise ValueError('Native network inventory is unavailable')
        for original in receipt['networks'].values():
            identifier=original['id']
            resource=resources.get('network:'+identifier,{'state':'not_dispatched'})
            if any(item['Name']==original['name'] and item['ID']!=identifier for item in network_inventory):
                raise ValueError('Retired private network was replaced')
            if any(item['ID']==identifier for item in network_inventory):
                network=json.loads(self.docker('inspect',identifier))[0]
                if network.get('Id')!=identifier or network.get('Name')!=original['name'] or network.get('Labels')!=original['labels'] or network.get('Driver')!=original['driver'] or network.get('Scope')!=original['scope'] or network.get('Ingress')!=original['ingress'] or network.get('Internal') is not True or not isinstance(network.get('Containers'),dict) or not set(network['Containers'])<=own&set(current):
                    raise ValueError('Retired private network changed or has another consumer')
                if resource['state']=='dispatching':
                    return {'status':'unknown'}
            elif resource['state'] not in {'dispatching','absent'}:
                raise ValueError('Original private network disappeared without its disposal intent')
            else:
                resources['network:'+identifier]={'state':'absent'}
        for container in current.values():
            for mount in container.get('Mounts',[]):
                if mount.get('Source'):
                    source=Path(mount['Source']).resolve(strict=False)
                    if source==self.state_path or source.is_relative_to(self.state_path) or self.state_path.is_relative_to(source):
                        raise ValueError('Native disposal state is exposed to a container')
        # Replacement project members are not adopted into an original intent.
        if any((item.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')==self.spec['compose_project'] and identifier not in own for identifier,item in current.items()):
            raise ValueError('Native project contains a replacement process')
        volume_names=set(self.docker('volume','ls','--format','{{.Name}}').decode().splitlines())
        for identifier,fingerprint in receipt['containers'].items():
            resource=resources.get('container:'+identifier,{'state':'not_dispatched'})
            if identifier in current:
                if self.fingerprint(current[identifier])!=fingerprint:
                    raise ValueError('Retired process restarted or changed')
                if resource['state']=='dispatching':
                    return {'status':'unknown'}
            elif resource['state'] not in {'dispatching','absent'}:
                raise ValueError('Original container disappeared without its disposal intent')
            else:
                resources['container:'+identifier]={'state':'absent'}
        for name,original in receipt['volumes'].items():
            resource=resources.get('volume:'+name,{'state':'not_dispatched'})
            if name in volume_names:
                if self.volume(name)!=original:
                    raise ValueError('Retired volume was replaced')
                if resource['state']=='dispatching':
                    return {'status':'unknown'}
                path=Path(original['path'])
                for identifier,container in current.items():
                    for mount in container.get('Mounts',[]):
                        source=Path(mount['Source']).resolve(strict=False) if mount.get('Source') else None
                        if identifier not in own and (mount.get('Name')==name or source is not None and (source==path or source.is_relative_to(path) or path.is_relative_to(source))):
                            raise ValueError('Retired volume has another consumer')
            elif resource['state'] not in {'dispatching','absent'}:
                raise ValueError('Original volume disappeared without its disposal intent')
            else:
                resources['volume:'+name]={'state':'absent'}
        if resources and all(resources.get(kind+':'+name,{}).get('state')=='absent' for kind,names in self.original_resources(receipt) for name in names):
            receipt['state']='disposed';self.save(receipt)
            return {'status':'disposed','identity_sha256':self.digest}
        if not receipt['qualification']:
            return {'status':'unqualified'}
        login=receipt['max_login_at']
        if login is not None and datetime.fromisoformat(login).timestamp()>=self.clock()-self.spec['recent_use_seconds']:
            return {'status':'recent_use'}
        if resources and not continuing:
            host=native_host_clock()
            if host['host_boot_id']==receipt.get('invocation_boot_id') and host['host_monotonic']<receipt.get('invocation_host_expires_at',float('inf')):
                return {'status':'incomplete'}
            # Holding both original locks excludes a surviving host/CLI. All
            # dispatched resources are absent; remaining members were never
            # dispatched. A delayed old invocation must reject its own expiry.
            return {'status':'resumable'}
        return {'status':'eligible'}

    @staticmethod
    def original_resources(receipt):
        return (('container',receipt['containers']),('volume',receipt['volumes']),
            ('network',{network['id']:network for network in receipt['networks'].values()}))

    def dispose(self):
        self.current()
        receipt=self.read()
        result=self.inspect(receipt)
        if result['status'] not in {'eligible','resumable'}:
            return result
        # This is the only original resource set. Unknown dispatches never get
        # new identities or deadlines; inspect can only settle original absence.
        receipt['state']='disposing'
        receipt['invocation_expires_at']=self.spec['expires_at']
        receipt['invocation_boot_id']=self.spec['host_boot_id']
        receipt['invocation_host_expires_at']=self.spec['host_expires_at']
        for kind,names in self.original_resources(receipt):
            for name in names:
                receipt['resources'].setdefault(kind+':'+name,{'state':'not_dispatched'})
        self.save(receipt)
        for kind,names in self.original_resources(receipt):
            for name in names:
                resource=receipt['resources'][kind+':'+name]
                if resource['state']=='absent':
                    continue
                # During this admitted invocation, completed resources may be
                # absent while the next original resource is not dispatched.
                result=self.inspect(receipt,continuing=True)
                if result['status'] not in {'eligible','resumable'}:
                    return result
                self.current()
                resource['state']='dispatching';self.save(receipt)
                self.docker(kind,'rm',name,mutation=True)
                # A successful command is still checked against actual absence.
                result=self.inspect(receipt)
                if result['status']=='unknown':
                    return result
                self.save(receipt)
        return self.inspect(receipt)


def native_maintenance_main(spec, command, *, receipt_root=None, lock_root=None, clock=None, monotonic=None):
    executor=NativeMaintenanceHost(spec,command,receipt_root=receipt_root,lock_root=lock_root,clock=clock,monotonic=monotonic)
    try:
        executor.open()
        if spec['mode']=='qualify-retirement':
            return executor.qualify()
        if spec['mode']=='inspect-original':
            return executor.inspect()
        if spec['mode']=='dispose-original':
            return executor.dispose()
        raise ValueError('Unsupported native maintenance mode')
    finally:
        executor.close()
"""
