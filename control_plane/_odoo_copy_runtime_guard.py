"""Runtime inspection shared by lane copies and standalone upgrade rehearsals."""

VOLUME_BACKING_INSPECTION_LIBRARY = r"""
class CopyReadbackRefused(RuntimeError):
    def __init__(self, predicate):
        self.predicate = predicate
        super().__init__(predicate)


def inspect_volume_backings(volumes, all_containers, own, *, expected_binding=None):
    paths, identities, backings = [], set(), {}
    for name in sorted(volumes):
        volume = json.loads(docker("volume", "inspect", name))[0]
        if volume.get("Driver") != "local" or volume.get("Options") not in (None, {}):
            raise CopyReadbackRefused("backing_storage_valid")
        if expected_binding is not None and (volume.get("Labels") or {}).get("io.launchplane.testing-resources") != expected_binding:
            raise CopyReadbackRefused("backing_storage_valid")
        backing = Path(volume["Mountpoint"]).resolve(strict=True)
        identity = (backing.stat().st_dev, backing.stat().st_ino)
        if expected_binding is not None and identity in identities:
            raise CopyReadbackRefused("backing_storage_distinct")
        identities.add(identity)
        paths.append(backing)
        backings[name] = {"path": str(backing), "device": identity[0], "inode": identity[1]}
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
                raise CopyReadbackRefused("storage_consumers_exclusive")
    return backings
"""

COPY_RUNTIME_INSPECTION_LIBRARY = (
    VOLUME_BACKING_INSPECTION_LIBRARY
    + r"""
def inspect_copy_runtime(spec, *, expected_instance):
    gen = spec["generation"]
    namespace, project, database = gen["namespace"], spec["compose_project"], spec["database_name"]
    if expected_instance not in {"testing", "rehearsal"} or gen["instance"] != expected_instance:
        raise CopyReadbackRefused("copy_instance_valid")
    prefix = "lp-testing-" if expected_instance == "testing" else "lp-rehearsal-"
    if not re.fullmatch(re.escape(prefix) + r"[a-f0-9]{32}", namespace):
        raise CopyReadbackRefused("copy_namespace_valid")
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
                raise CopyReadbackRefused("copy_services_exact")
            containers[service] = (identifier, container)
    if set(containers) != {"web", "database", "script-runner"}:
        raise CopyReadbackRefused("copy_services_exact")
    for service, (identifier, container) in containers.items():
        configuration = container["Config"]
        if container.get("HostConfig", {}).get("Privileged") or container.get("HostConfig", {}).get("NetworkMode") == "host" or container.get("HostConfig", {}).get("PidMode") not in (None, "", "private"):
            raise CopyReadbackRefused("copy_host_capabilities_absent")
        if any(container.get("HostConfig", {}).get("PortBindings", {}).values()):
            raise CopyReadbackRefused("copy_host_ports_absent")
        expected = {namespace + "-db"} if service == "database" else {namespace + "-data", namespace + "-logs"}
        mounts = container.get("Mounts")
        if not isinstance(mounts, list) or {item.get("Name") for item in mounts} != expected or any(item.get("Type") != "volume" for item in mounts):
            raise CopyReadbackRefused("copy_mounts_exact")
        if service == "database" and mounts[0].get("Destination") != spec["database_data_directory"]:
            raise CopyReadbackRefused("database_storage_mounted")
        if service != "database":
            if configuration.get("Image") != spec["image_reference"]:
                raise CopyReadbackRefused("copy_image_matches")
            environment = dict(item.split("=", 1) for item in configuration.get("Env", []) if "=" in item)
            allowed_databases = spec.get('usage_database_names',[database]) if spec.get('usage_only') else [database]
            if environment.get("PLATFORM_INSTANCE") != expected_instance or environment.get("ODOO_DB_HOST") != "database" or environment.get("ODOO_DB_NAME") not in allowed_databases:
                raise CopyReadbackRefused("database_endpoint_local")
            if not (spec.get('storage_maintenance') or spec.get('usage_only') or spec.get('verification_only')) and environment.get("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", "") != spec["expected_overrides_payload"]:
                raise CopyReadbackRefused("copy_settings_match")
            if environment.get("ODOO_MAX_CRON_THREADS") != "0":
                raise CopyReadbackRefused("copy_cron_disabled")
            if container.get("HostConfig", {}).get("ExtraHosts"):
                raise CopyReadbackRefused("copy_host_aliases_absent")
            if any(environment.get(key) for key in ("GITHUB_TOKEN", "GH_TOKEN", "SSH_AUTH_SOCK", "DATA_WORKFLOW_SSH_KEY", "ODOO_KEY", "ODOO_SMTP_PASSWORD")):
                raise CopyReadbackRefused("copy_execution_credentials_absent")
        if service != "web":
            networks = container.get("NetworkSettings", {}).get("Networks")
            if not isinstance(networks, dict) or len(networks) != 1:
                raise CopyReadbackRefused("copy_network_private")
            network = one_inspection(next(iter(networks)))
            if network.get("Internal") is not True:
                raise CopyReadbackRefused("copy_network_private")
    data_networks = set(containers["database"][1]["NetworkSettings"]["Networks"])
    if set(containers["script-runner"][1]["NetworkSettings"]["Networks"]) != data_networks or set(containers["web"][1]["NetworkSettings"]["Networks"]) != data_networks | ({"dokploy-network"} if expected_instance == "testing" else set()):
        raise CopyReadbackRefused("copy_network_reach_exact")
    if expected_instance == 'rehearsal':
        own={item[1]['Id'] for item in containers.values()}
        network=one_inspection(next(iter(data_networks)))
        members=network.get('Containers')
        if not isinstance(members,dict) or not set(members)<=own:
            raise CopyReadbackRefused("copy_network_members_exclusive")
    for container in all_containers:
        if container in [item[1] for item in containers.values()]:
            continue
        if any(mount.get("Name") in volumes for mount in container.get("Mounts", [])):
            raise CopyReadbackRefused("copy_storage_exclusive")
    inspect_volume_backings(volumes, all_containers, {item[1]["Id"] for item in containers.values()}, expected_binding=gen["resources_binding_sha256"])
    private_state = Path.home().resolve(strict=True) / ('.local/state/launchplane-testing-disposal' if expected_instance == 'testing' else '.local/state/launchplane-rehearsals')
    for container in all_containers:
        for mount in container.get('Mounts', []):
            if (expected_instance == 'rehearsal' or mount.get('RW', True)) and mount.get('Source'):
                source_path = Path(mount['Source']).resolve(strict=False)
                if private_state.is_relative_to(source_path) or source_path.is_relative_to(private_state):
                    raise CopyReadbackRefused("copy_private_state_unmounted")
    return containers
"""
)
