"""Fixed, original-copy-only native drill retirement and storage removal."""

NATIVE_MAINTENANCE_LIBRARY = r"""
class NativeReceiptUntrusted(ValueError):
    pass


class NativeMaintenanceAdmissionExpired(TimeoutError):
    pass


class NativeMaintenanceProviderDenied(PermissionError):
    pass


def native_host_clock():
    return {'host_boot_id':Path('/proc/sys/kernel/random/boot_id').read_text().strip(),
        'host_monotonic':time.clock_gettime(time.CLOCK_BOOTTIME),'host_observed_at':time.time()}


def native_container_observation(container,spec):
    if (container.get('Config',{}).get('Labels') or {}).get('com.docker.compose.project')!=spec['compose_project']:
        return
    if not isinstance(container.get('Mounts'),list) or not isinstance(container.get('NetworkSettings',{}).get('Networks'),dict):
        raise RuntimeError('Native container observation is unavailable')
    NativeMaintenanceHost.fingerprint(container)


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
            raise NativeMaintenanceAdmissionExpired('Original native maintenance admission expired')

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
        expected=self.spec.get('receipt_namespaces',[])
        # Directory metadata may change across remounts and may be reused after
        # replacement. The private, fsynced random marker owns this namespace.
        self.private_state()
        try:
            nonce=self.namespace_nonce(expected,directory)
        except (OSError,UnicodeDecodeError) as error:
            raise NativeReceiptUntrusted('Original namespace marker cannot be read') from error
        self.namespace_sha256=hashlib.sha256((self.namespace+':'+nonce).encode()).hexdigest()
        if expected and any(value!=self.namespace_sha256 for value in expected):
            raise NativeReceiptUntrusted('Original receipt namespace changed')
        return self

    def namespace_nonce(self,expected,directory):
        try:
            descriptor=os.open('namespace-id',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
        except FileNotFoundError:
            if expected or any(name.startswith('native-') and name.endswith('.json') for name in os.listdir(directory)):
                raise NativeReceiptUntrusted('Original receipt namespace marker is missing')
            name='.namespace-'+uuid.uuid4().hex+'.tmp'
            created=False
            try:
                descriptor=os.open(name,os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW,0o600,dir_fd=directory)
                created=True
                try:
                    remaining=os.urandom(32).hex().encode('ascii')
                    while remaining:
                        written=os.write(descriptor,remaining)
                        if written<=0:
                            raise OSError('Native namespace write did not complete')
                        remaining=remaining[written:]
                    os.fsync(descriptor)
                finally:
                    os.close(descriptor)
                try:
                    os.link(name,'namespace-id',src_dir_fd=directory,dst_dir_fd=directory,follow_symlinks=False)
                except FileExistsError:
                    pass
            except OSError as error:
                # No partial file is published under the final name. Failed
                # initialization is availability, not positive custody loss.
                raise RuntimeError('Native namespace initialization is unavailable') from error
            finally:
                if created:
                    try:
                        os.unlink(name,dir_fd=directory)
                    except FileNotFoundError:
                        pass
                    except OSError as error:
                        raise RuntimeError('Native namespace initialization is unavailable') from error
            descriptor=os.open('namespace-id',os.O_RDONLY|os.O_NOFOLLOW,dir_fd=directory)
        with os.fdopen(descriptor) as handle:
            metadata=os.fstat(handle.fileno())
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=os.geteuid() or metadata.st_mode&0o077:
                raise NativeReceiptUntrusted('Native namespace marker is unsafe')
            nonce=handle.read(65)
            if not re.fullmatch(r'[a-f0-9]{64}',nonce):
                raise NativeReceiptUntrusted('Native namespace marker is malformed')
            try:
                # A completed link whose barrier/reply was lost is adopted as
                # the same immutable winner only after finishing durability.
                os.fsync(handle.fileno());os.fsync(directory)
            except OSError as error:
                raise RuntimeError('Native namespace durability is unavailable') from error
        return nonce

    def private_state(self,containers=None):
        if containers is None:
            self.inventory()
            return
        unavailable=False
        for container in containers:
            mounts=container.get('Mounts') if isinstance(container,dict) else None
            if not isinstance(mounts,list):
                unavailable=True
                continue
            for mount in mounts:
                if not isinstance(mount,dict) or (mount.get('Source') is not None and not isinstance(mount['Source'],str)):
                    unavailable=True
                    continue
                if mount.get('RW',True) and mount.get('Source'):
                    source=Path(mount['Source']).resolve(strict=False)
                    if source==self.state_path or source.is_relative_to(self.state_path) or self.state_path.is_relative_to(source):
                        raise NativeReceiptUntrusted('Native disposal state is exposed to a container')
        if unavailable:
            raise RuntimeError('Native container mount inventory is unavailable')

    def outcome(self,result):
        return {**result,'namespace_sha256':self.namespace_sha256}

    def seal(self):
        # Both locks are held. A delayed payload must read this terminal seal
        # before it can dispatch; expiry alone never excludes daemon work.
        self.private_state()
        receipt=self.read()
        if receipt is not None:
            if receipt.get('state')=='preserved':
                return {'status':'preserved','no_stop_dispatched':True,'identity_sha256':self.digest}
            if receipt.get('protocol')!=2:
                return {'status':'protected','reason':'original_retirement_evidence_missing'}
            stops=receipt.get('retirement',{}).get('stops',{})
            if any(step.get('ever_dispatched') is not False for step in stops.values()):
                return self.inspect(receipt)
            if receipt.get('resources') or receipt.get('state') not in {'retirement'}:
                return self.inspect(receipt)
        else:
            receipt={'identity':self.identity,'protocol':2,'retirement':{},'resources':{}}
        self.current()
        receipt['state']='preserved'
        self.current()
        self.save(receipt)
        return {'status':'preserved','no_stop_dispatched':True,'identity_sha256':self.digest}

    def failure(self,*,custody):
        try:
            receipt=self.read() if self.directory is not None else None
            uncertain=receipt is not None and (
                any(item.get('state')=='dispatching' for item in receipt.get('resources',{}).values()) or
                any(item.get('ever_dispatched') is True and item.get('state')!='stopped' for item in receipt.get('retirement',{}).get('stops',{}).values()))
            unresolved=uncertain and (custody or self.invocation_excluded(receipt))
        except (OSError,ValueError,KeyError,TypeError,AttributeError,IndexError):
            # An unreadable receipt is not an empty WAL and cannot exclude work.
            uncertain=True;unresolved=True
        result={'status':'recovery_required' if unresolved else ('protected' if custody else 'observation_unavailable'),
            'reason':'original_mutation_outcome_unknown' if uncertain else ('native_copy_custody_changed' if custody else 'native_copy_observation_unavailable')}
        if hasattr(self,'namespace_sha256'):
            result['namespace_sha256']=self.namespace_sha256
        return result

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
        except OSError as error:
            raise NativeReceiptUntrusted('Original receipt cannot be read') from error
        try:
            with os.fdopen(descriptor) as handle:
                metadata=os.fstat(handle.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_uid!=os.geteuid() or metadata.st_mode&0o077:
                    raise NativeReceiptUntrusted('Native receipt is unsafe')
                receipt=json.loads(handle.read(65537))
            if not isinstance(receipt,dict) or receipt.get('identity')!=self.identity or not isinstance(receipt.get('resources'),dict):
                raise NativeReceiptUntrusted('Native receipt identity or shape changed')
            state=receipt.get('state')
            if state not in {'preserved','qualifying','retirement','retained','disposing','disposed'}:
                raise NativeReceiptUntrusted('Native receipt state is unavailable')
            if state=='retirement':
                retirement=receipt.get('retirement')
                if not isinstance(retirement,dict) or retirement and (not isinstance(retirement.get('originals'),dict) or not isinstance(retirement.get('stops'),dict) or any(not isinstance(step,dict) or step.get('state') not in {'not_dispatched','dispatching','stopped'} or type(step.get('ever_dispatched')) is not bool for step in retirement['stops'].values())):
                    raise NativeReceiptUntrusted('Native stop WAL is malformed')
            if state in {'retained','disposing','disposed'} and (any(not isinstance(receipt.get(name),dict) for name in ('containers','volumes','networks')) or type(receipt.get('qualification')) is not bool):
                raise NativeReceiptUntrusted('Native disposal WAL is malformed')
            return receipt
        except (OSError,ValueError,KeyError,TypeError,AttributeError,IndexError) as error:
            raise NativeReceiptUntrusted('Original receipt cannot be trusted') from error

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
        result=json.loads(self.docker('inspect',*identifiers)) if identifiers else []
        if not isinstance(result,list):
            raise RuntimeError('Native container inventory is unavailable')
        # Positive receipt exposure from this exact snapshot outranks unrelated
        # unavailable metadata; malformed data still cannot authorize any work.
        self.private_state(result)
        if any(not isinstance(item,dict) or not isinstance(item.get('Id'),str) for item in result):
            raise RuntimeError('Native container inventory is unavailable')
        return result

    def volume(self, name):
        volumes=json.loads(self.docker('volume','inspect',name))
        if not isinstance(volumes,list) or len(volumes)!=1 or not isinstance(volumes[0],dict):
            raise RuntimeError('Original volume inventory is unavailable')
        volume=volumes[0]
        if volume.get('Name')!=name or volume.get('Driver')!='local' or volume.get('Options') not in (None,{}):
            raise ValueError('Native volume custody changed')
        if not isinstance(volume.get('CreatedAt'),str) or not volume['CreatedAt']:
            raise RuntimeError('Native volume creation identity is unavailable')
        if (volume.get('Labels') or {}).get('io.launchplane.testing-resources')!=self.identity['resources_binding']:
            raise ValueError('Native volume allocation changed')
        path=Path(volume['Mountpoint']).resolve(strict=True)
        metadata=path.stat()
        return {'name':name,'created_at':volume.get('CreatedAt'),'labels':volume.get('Labels') or {},
            'path':str(path),'device':metadata.st_dev,'inode':metadata.st_ino}

    @staticmethod
    def fingerprint(container):
        state=container.get('State',{})
        if any(not isinstance(state.get(name),str) for name in ('StartedAt','FinishedAt','Status')) or state.get('Status') not in {'running','exited','created','paused','restarting','dead'} or type(container.get('RestartCount')) is not int or container['RestartCount']<0:
            raise RuntimeError('Native process lifetime is unavailable')
        return {'id':container['Id'],'labels':container.get('Config',{}).get('Labels') or {},
            'started_at':state.get('StartedAt'),'finished_at':state.get('FinishedAt'),
            'restart_count':container.get('RestartCount'),'state':state.get('Status')}

    def qualify(self):
        self.current()
        existing=self.read()
        if existing is not None:
            result=self.inspect(existing)
            if result['status']!='resumable_retirement':
                return result
            receipt=existing
        else:
            receipt={'identity':self.identity,'protocol':2,'state':'retirement','retirement':{},
                'qualification':False,'max_login_at':None,'resources':{}}
        receipt.update(invocation_expires_at=self.spec['expires_at'],
            invocation_boot_id=self.spec['host_boot_id'],
            invocation_host_expires_at=self.spec['host_expires_at'])
        self.save(receipt)
        containers,volumes,networks=self.retirement_resources()
        originals={item['Id']:self.fingerprint(item) for _,item in containers.values()}
        retirement=receipt['retirement']
        if retirement:
            if set(originals)!=set(retirement['originals']) or volumes!=receipt['volumes'] or networks!=receipt['networks']:
                raise ValueError('Original retirement custody changed')
            self.unchanged_lifetimes(originals,retirement['originals'])
        else:
            retirement.update(originals=originals,stops={key:{'state':'not_dispatched','ever_dispatched':False} for key in originals},history='unavailable')
            receipt.update(volumes=volumes,networks=networks,containers={})
            self.save(receipt)
        for service in ('web','script-runner'):
            self.stop_original(receipt,containers[service][1])
        database=containers['database'][1]
        if retirement['history']=='unavailable':
            self.capture_history(receipt,database)
        self.stop_original(receipt,database)
        return self.finish_retirement(receipt)

    @staticmethod
    def unchanged_lifetimes(current,originals):
        for identifier,original in originals.items():
            item=current[identifier]
            if any(item[key]!=original[key] for key in ('id','labels','started_at','restart_count')):
                raise ValueError('Original retirement process was replaced or restarted')

    def invocation_excluded(self,receipt):
        host=native_host_clock()
        return host['host_boot_id']!=receipt.get('invocation_boot_id') or host['host_monotonic']>=receipt.get('invocation_host_expires_at',float('inf'))

    def retirement_resources(self):
        try:
            containers=inspect_copy_runtime(self.spec,expected_instance='testing',receipt_namespace=self.state_path)
        except CopyRuntimeReceiptExposure as error:
            source=error.source_path
            if source==self.state_path or source.is_relative_to(self.state_path) or self.state_path.is_relative_to(source):
                raise NativeReceiptUntrusted('Original receipt state was exposed') from error
            raise
        own={value[1]['Id'] for value in containers.values()}
        # Validate native observation availability before the generic retained
        # qualifier's negative-custody errors; its other callers stay unchanged.
        for _,container in containers.values():
            self.fingerprint(container)
        all_containers=self.inventory()
        for container in all_containers:
            native_container_observation(container,self.spec)
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
            if not isinstance(network.get('Containers'),dict):
                raise RuntimeError('Native private network inventory is unavailable')
            if not set(network['Containers'])<=own:
                raise ValueError('Native private network has a foreign consumer')
            if not isinstance(network.get('Id'),str) or not network['Id']:
                raise RuntimeError('Native private network identity is unavailable')
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
        return containers,volumes,networks

    def stop_original(self,receipt,container):
        identifier=container['Id']
        current={item['Id']:item for item in self.inventory()}
        original=receipt['retirement']['originals'][identifier]
        if identifier not in current:
            raise ValueError('Original retirement process disappeared')
        self.unchanged_lifetimes({identifier:self.fingerprint(current[identifier])},{identifier:original})
        step=receipt['retirement']['stops'][identifier]
        if current[identifier]['State']['Status'] in {'created','exited'}:
            step.update(state='stopped',fingerprint=self.fingerprint(current[identifier]))
            self.save(receipt)
            return
        if step['state']!='not_dispatched' or step.get('ever_dispatched') is not False:
            raise TimeoutError('Original stop remains uncertain')
        step.update(state='dispatching',ever_dispatched=True);self.save(receipt)
        try:
            self.docker('stop','--timeout','-1',identifier,mutation=True)
        except NativeMaintenanceAdmissionExpired:
            # current() refused synchronously before command(), unlike a CLI
            # timeout or a lost daemon reply. Preserve positive no-send proof.
            step.update(state='not_dispatched',ever_dispatched=False)
            self.save(receipt)
            raise
        current={item['Id']:item for item in self.inventory()}
        if identifier not in current or current[identifier]['State']['Status'] not in {'created','exited'}:
            raise TimeoutError('Original stop remains uncertain')
        self.unchanged_lifetimes({identifier:self.fingerprint(current[identifier])},{identifier:original})
        step.update(state='stopped',fingerprint=self.fingerprint(current[identifier]))
        self.save(receipt)

    def capture_history(self,receipt,database):
        # No old database is started. A successful NULL is distinct from a
        # failed query. PostgreSQL timestamps in Odoo are naive UTC.
        try:
            current={item['Id']:item for item in self.inventory()}
            if database['Id'] not in current or current[database['Id']].get('State',{}).get('Status')!='running':
                raise ValueError('Original database is not running')
            sql=("BEGIN READ ONLY; SET LOCAL statement_timeout='5s'; SELECT json_build_object("
                "'witness',(SELECT value FROM ir_config_parameter WHERE key=:'witness_key'),"
                "'max_login_at',(SELECT MAX(create_date) FROM res_users_log WHERE create_date>=:'prepared'::timestamp)); COMMIT;\n")
            for attempt in range(2):
                self.current()
                current={item['Id']:item for item in self.inventory()}
                if database['Id'] not in current or current[database['Id']].get('State',{}).get('Status')!='running':
                    raise ValueError('Original database is not running')
                self.unchanged_lifetimes({database['Id']:self.fingerprint(current[database['Id']])},{database['Id']:receipt['retirement']['originals'][database['Id']]})
                try:
                    output=self.docker('exec','-i',database['Id'],'sh','-c',
                'psql -X -q -t -A -v ON_ERROR_STOP=1 -v witness_key="$2" -v prepared="$3" -U "$POSTGRES_USER" -d "$1"',
                        '_',self.spec['database_name'],self.spec['copy_witness_parameter'],self.spec['generation']['prepared_at'],stdin=sql.encode())
                    break
                except NativeMaintenanceProviderDenied:
                    raise
                except (RuntimeError,TimeoutError,subprocess.SubprocessError):
                    if attempt:
                        raise
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
            receipt['retirement']['history']='captured'
        except (NativeMaintenanceProviderDenied,NativeMaintenanceAdmissionExpired):
            raise
        except (ValueError,RuntimeError,TimeoutError,subprocess.SubprocessError,OSError,KeyError,TypeError,AttributeError,IndexError):
            # Ordinary admitted native retirement still preserves the copy.
            receipt['qualification']=False
            receipt['retirement']['history']='protected'
        self.save(receipt)

    def finish_retirement(self,receipt):
        current=self.inventory()
        own=set(receipt['retirement']['originals'])
        members=[item for item in current if item['Id'] in own]
        if len(members)!=len(own) or any(item.get('State',{}).get('Status') not in {'created','exited'} for item in members):
            raise ValueError('Original retirement remains unknown')
        self.unchanged_lifetimes({item['Id']:self.fingerprint(item) for item in members},receipt['retirement']['originals'])
        if {name:self.volume(name) for name in receipt['volumes']}!=receipt['volumes']:
            raise ValueError('Retired storage backing changed')
        receipt.update(state='retained',containers={item['Id']:self.fingerprint(item) for item in members},observed_at=self.clock())
        self.save(receipt)
        return self.inspect(receipt)

    def inspect_retirement(self,receipt,current):
        retirement=receipt.get('retirement')
        if receipt.get('protocol')!=2:
            return {'status':'protected','reason':'original_retirement_evidence_missing'}
        if not retirement:
            return {'status':'resumable_retirement' if self.invocation_excluded(receipt) else 'active',
                'reason':'original_retirement_not_dispatched'}
        originals=retirement['originals']
        if not set(originals)<=set(current):
            raise ValueError('Original retirement process disappeared')
        self.unchanged_lifetimes({key:self.fingerprint(current[key]) for key in originals},originals)
        for identifier,step in retirement['stops'].items():
            item=current[identifier]
            if step['state']=='stopped' and self.fingerprint(item)!=step['fingerprint']:
                raise ValueError('Stopped original retirement process changed')
            if item['State']['Status'] in {'created','exited'}:
                step.update(state='stopped',fingerprint=self.fingerprint(item))
        self.save(receipt)
        if all(step['state']=='stopped' for step in retirement['stops'].values()):
            return self.finish_retirement(receipt)
        uncertain=any(step['state']=='dispatching' for step in retirement['stops'].values())
        if not self.invocation_excluded(receipt):
            return {'status':'active','reason':'original_retirement_active'}
        return {'status':'recovery_required' if uncertain else 'resumable_retirement',
            'reason':'original_stop_outcome_unknown' if uncertain else 'original_retirement_remaining_steps'}

    def inspect(self, receipt=None, *, continuing=False):
        self.private_state()
        receipt=receipt or self.read()
        current={item['Id']:item for item in self.inventory()}
        self.private_state(list(current.values()))
        if receipt is None:
            return {'status':'missing'}
        if receipt['state']=='preserved':
            return {'status':'preserved','no_stop_dispatched':True,'identity_sha256':self.digest}
        if receipt['state']=='qualifying':
            return {'status':'protected' if self.invocation_excluded(receipt) else 'active',
                'reason':'original_retirement_evidence_missing'}
        if receipt['state']=='retirement':
            return self.inspect_retirement(receipt,current)
        if receipt['state']=='disposed':
            return {'status':'disposed','identity_sha256':self.digest}
        own=set(receipt['containers'])
        if not receipt.get('networks'):
            return {'status':'unqualified'}
        resources=receipt['resources']
        network_inventory=[json.loads(line) for line in self.docker('network','ls','--no-trunc','--format','{{json .}}').decode().splitlines()]
        if any(not isinstance(item,dict) or not item.get('ID') or not item.get('Name') for item in network_inventory):
            raise RuntimeError('Native network inventory is unavailable')
        for original in receipt['networks'].values():
            identifier=original['id']
            resource=resources.get('network:'+identifier,{'state':'not_dispatched'})
            if any(item['Name']==original['name'] and item['ID']!=identifier for item in network_inventory):
                raise ValueError('Retired private network was replaced')
            if any(item['ID']==identifier for item in network_inventory):
                network=json.loads(self.docker('inspect',identifier))[0]
                if not isinstance(network.get('Containers'),dict):
                    raise RuntimeError('Native private network inventory is unavailable')
                if network.get('Id')!=identifier or network.get('Name')!=original['name'] or network.get('Labels')!=original['labels'] or network.get('Driver')!=original['driver'] or network.get('Scope')!=original['scope'] or network.get('Ingress')!=original['ingress'] or network.get('Internal') is not True or not isinstance(network.get('Containers'),dict) or not set(network['Containers'])<=own&set(current):
                    raise ValueError('Retired private network changed or has another consumer')
                if resource['state']=='dispatching':
                    return self.uncertain_removal(receipt)
            elif resource['state'] not in {'dispatching','absent'}:
                raise ValueError('Original private network disappeared without its disposal intent')
            else:
                resources['network:'+identifier]={'state':'absent'}
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
                    return self.uncertain_removal(receipt)
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
                    return self.uncertain_removal(receipt)
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

    def uncertain_removal(self,receipt):
        return {'status':'recovery_required' if self.invocation_excluded(receipt) else 'unknown',
            'reason':'original_removal_outcome_unknown'}

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
                try:
                    self.docker(kind,'rm',name,mutation=True)
                except NativeMaintenanceAdmissionExpired:
                    resource['state']='not_dispatched'
                    self.save(receipt)
                    raise
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
            return executor.outcome(executor.qualify())
        if spec['mode']=='inspect-original':
            return executor.outcome(executor.inspect())
        if spec['mode']=='dispose-original':
            return executor.outcome(executor.dispose())
        if spec['mode']=='seal-retirement':
            return executor.outcome(executor.seal())
        raise ValueError('Unsupported native maintenance mode')
    except NativeMaintenanceProviderDenied:
        raise
    except BlockingIOError:
        return {'status':'active','reason':'native_copy_lock_busy'}
    except NativeReceiptUntrusted:
        result=executor.failure(custody=True)
        result.update(status='protected',reason='native_copy_receipt_untrusted')
        return result
    except (json.JSONDecodeError,KeyError,TypeError,AttributeError,IndexError):
        return executor.failure(custody=False)
    except (ValueError,CopyRuntimeDrift):
        return executor.failure(custody=True)
    except (OSError,TimeoutError,RuntimeError,subprocess.SubprocessError):
        return executor.failure(custody=False)
    finally:
        executor.close()
"""
