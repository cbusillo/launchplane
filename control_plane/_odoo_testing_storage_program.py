"""Fixed host maintenance library embedded behind the existing restore guard."""

STORAGE_MAINTENANCE_LIBRARY = r"""
def storage_maintenance(spec, containers):
    from datetime import datetime, timezone
    gen = spec['generation']
    maintenance = spec['storage_maintenance']
    if len(maintenance['copies']) > 4:
        raise RuntimeError('Unbounded storage maintenance')
    observed = docker('exec','-i',containers['database'][0],'sh','-c',
        'psql -X -q -t -A -v ON_ERROR_STOP=1 -v marker="$2" -U "$POSTGRES_USER" -d "$1"',
        '_', spec['database_name'], spec['copy_witness_parameter'],
        stdin=b"SELECT value FROM ir_config_parameter WHERE key=:'marker';\n").decode().strip()
    if observed != spec['copy_binding_sha256']:
        raise RuntimeError('Current testing copy lacks its committed witness')
    results = []
    for copy in maintenance['copies']:
        if datetime.now(timezone.utc).timestamp() >= maintenance['expires_at']:
            raise RuntimeError('Maintenance qualification expired')
        candidate = {**gen, **copy, 'disposal_expires_at':maintenance['expires_at']}
        try:
            discard_previous_drill(candidate, containers['database'][0], containers['script-runner'][0], spec['database_name'])
            results.append('disposed')
        except PermissionError:
            raise
        except Exception:
            results.append('deferred')
    capacity = storage_capacity(spec, containers, maintenance['budget'])
    print(sys.argv[2] + json.dumps({'type':'complete', 'disposals':results, 'capacity':capacity}), flush=True)


def storage_capacity(spec, containers, budget):
    gen = spec['generation']
    # Group demands on the same actual backing filesystem. Comparing each
    # volume independently would admit two allocations against the same free bytes.
    required = {}
    available = {}
    def demand(path, size):
        path = Path(path).resolve(strict=True)
        device = path.stat().st_dev
        stat = os.statvfs(path)
        free = stat.f_bavail * stat.f_frsize
        required[device] = required.get(device, 0) + size
        available[device] = min(available.get(device, free), free)
    for suffix, size in (('-db', budget['database_bytes']), ('-data', budget['filestore_bytes'])):
        volume = json.loads(docker('volume', 'inspect', gen['namespace'] + suffix))[0]
        demand(volume['Mountpoint'], size)
    # SSH capture stages on the host; native capture/assembly stages in the
    # runner's writable layer. Require known backing rather than guessing overlay space.
    if gen.get('source_mode', 'ssh') == 'container-export':
        driver = containers['script-runner'][1].get('GraphDriver', {})
        upper = driver.get('Data', {}).get('UpperDir')
        if not upper:
            raise RuntimeError('Runner staging backing is unavailable')
        demand(upper, budget['staging_bytes'])
    else:
        demand(tempfile.gettempdir(), budget['staging_bytes'])
    capacity = all(available[device] >= size + budget['reserve_bytes'] for device, size in required.items())
    return capacity
"""
