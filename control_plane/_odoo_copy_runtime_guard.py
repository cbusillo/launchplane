"""Runtime inspection shared by lane copies and standalone upgrade rehearsals."""

COPY_RUNTIME_INSPECTION_LIBRARY = r"""
def inspect_copy_runtime(spec, *, expected_instance):
    gen = spec["generation"]
    namespace, project, database = gen["namespace"], spec["compose_project"], spec["database_name"]
    if expected_instance not in {"testing", "rehearsal"} or gen["instance"] != expected_instance:
        raise RuntimeError("Unsupported copy runtime instance")
    prefix = "lp-testing-" if expected_instance == "testing" else "lp-rehearsal-"
    if not re.fullmatch(re.escape(prefix) + r"[a-f0-9]{32}", namespace):
        raise RuntimeError("Unsupported copy runtime namespace")
    volumes = {namespace + "-data", namespace + "-db", namespace + "-logs"}
    containers = {}
    all_containers = []
    for identifier in docker("ps", "-aq").decode().splitlines():
        container = one_inspection(identifier)
        all_containers.append(container)
        labels = container.get("Config", {}).get("Labels") or {}
        if labels.get("com.docker.compose.project") == project:
            service = labels.get("com.docker.compose.service")
            if service not in {"web", "database", "script-runner"} or service in containers:
                raise RuntimeError("Testing compose has an unexpected container")
            containers[service] = (identifier, container)
    if set(containers) != {"web", "database", "script-runner"}:
        raise RuntimeError("Testing compose inventory is incomplete")
    for service, (identifier, container) in containers.items():
        configuration = container["Config"]
        if container.get("HostConfig", {}).get("Privileged") or container.get("HostConfig", {}).get("NetworkMode") == "host" or container.get("HostConfig", {}).get("PidMode") not in (None, "", "private"):
            raise RuntimeError("Testing compose has host capabilities")
        if any(container.get("HostConfig", {}).get("PortBindings", {}).values()):
            raise RuntimeError("Testing compose publishes host ports")
        expected = {namespace + "-db"} if service == "database" else {namespace + "-data", namespace + "-logs"}
        mounts = container.get("Mounts")
        if not isinstance(mounts, list) or {item.get("Name") for item in mounts} != expected or any(item.get("Type") != "volume" for item in mounts):
            raise RuntimeError("Testing compose has unexpected writable or credential mounts")
        if service == "database" and mounts[0].get("Destination") != spec["database_data_directory"]:
            raise RuntimeError("Testing database storage is not mounted at its data directory")
        if service != "database":
            if configuration.get("Image") != spec["image_reference"]:
                raise RuntimeError("Testing artifact identity changed")
            environment = dict(item.split("=", 1) for item in configuration.get("Env", []) if "=" in item)
            allowed_databases = spec.get('usage_database_names',[database]) if spec.get('usage_only') else [database]
            if environment.get("PLATFORM_INSTANCE") != expected_instance or environment.get("ODOO_DB_HOST") != "database" or environment.get("ODOO_DB_NAME") not in allowed_databases:
                raise RuntimeError("Testing database endpoint is not local")
            if not (spec.get('storage_maintenance') or spec.get('usage_only') or spec.get('verification_only')) and environment.get("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", "") != spec["expected_overrides_payload"]:
                raise RuntimeError("Testing settings changed after execution preflight")
            if environment.get("ODOO_MAX_CRON_THREADS") != "0":
                raise RuntimeError("Fresh testing must have no cron workers")
            if container.get("HostConfig", {}).get("ExtraHosts"):
                raise RuntimeError("Fresh testing has host gateway aliases")
            if any(environment.get(key) for key in ("GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK", "DATA_WORKFLOW_SSH_KEY", "ODOO_KEY", "ODOO_SMTP_PASSWORD")):
                raise RuntimeError("Testing holds an execution credential")
        if service != "web":
            networks = container.get("NetworkSettings", {}).get("Networks")
            if not isinstance(networks, dict) or len(networks) != 1:
                raise RuntimeError("Testing data containers are not privately networked")
            network = one_inspection(next(iter(networks)))
            if network.get("Internal") is not True:
                raise RuntimeError("Testing data network has outbound access")
    data_networks = set(containers["database"][1]["NetworkSettings"]["Networks"])
    if set(containers["script-runner"][1]["NetworkSettings"]["Networks"]) != data_networks or set(containers["web"][1]["NetworkSettings"]["Networks"]) != data_networks | ({"dokploy-network"} if expected_instance == "testing" else set()):
        raise RuntimeError("Fresh testing has unexpected network reach")
    if expected_instance == 'rehearsal':
        own={item[1]['Id'] for item in containers.values()}
        network=one_inspection(next(iter(data_networks)))
        members=network.get('Containers')
        if not isinstance(members,dict) or not set(members)<=own:
            raise RuntimeError('Rehearsal network is shared or its membership is unknown')
    for container in all_containers:
        if container in [item[1] for item in containers.values()]:
            continue
        if any(mount.get("Name") in volumes for mount in container.get("Mounts", [])):
            raise RuntimeError("Testing storage is shared by another container")
    # Resolve actual backing paths too: a bind alias of a named volume is
    # storage sharing even when Docker reports different mount names.
    paths = []
    identities = set()
    for name in volumes:
        volume = json.loads(docker("volume", "inspect", name))[0]
        if volume.get("Driver") != "local" or volume.get("Options") not in (None, {}) or volume.get("Labels", {}).get("io.launchplane.testing-resources") != gen["resources_binding_sha256"]:
            raise RuntimeError("Testing backing storage changed")
        backing = Path(volume["Mountpoint"]).resolve(strict=True)
        identity = (backing.stat().st_dev, backing.stat().st_ino)
        if identity in identities:
            raise RuntimeError("Testing backing storage overlaps")
        identities.add(identity)
        paths.append(backing)
    own = {item[1]["Id"] for item in containers.values()}
    for container in all_containers:
        if container.get("Id") in own:
            continue
        for mount in container.get("Mounts", []):
            if not mount.get("Source"):
                continue
            source_path = Path(mount["Source"]).resolve(strict=False)
            if any(source_path == path or source_path.is_relative_to(path)
                   or (mount.get("RW", True) and path.is_relative_to(source_path))
                   for path in paths):
                raise RuntimeError("Testing storage has a bind consumer")
    private_state = Path.home().resolve(strict=True) / ('.local/state/launchplane-testing-disposal' if expected_instance == 'testing' else '.local/state/launchplane-rehearsals')
    for container in all_containers:
        for mount in container.get('Mounts', []):
            if mount.get('RW', True) and mount.get('Source'):
                source_path = Path(mount['Source']).resolve(strict=False)
                if private_state.is_relative_to(source_path) or source_path.is_relative_to(private_state):
                    raise RuntimeError('Disposal state is exposed to a container')
    return containers
"""
