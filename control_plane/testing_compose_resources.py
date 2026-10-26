import hashlib
import hmac
import inspect
import json
from pathlib import Path
import re
from typing import Any, Callable
import click

from control_plane.dokploy import api, native_copy_stream, source
from control_plane import runtime_environments
from control_plane.runtime_platform_credentials import looks_like_credential
from control_plane.docker_errors import docker_daemon_authorization_denied
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_activation import (
    TestingActivationAdapter,
    activation_record,
    preparation_resources,
    rendered_source,
    runtime_values,
    shared_read_only_binding,
    configuration_digest,
)
from control_plane.disposable_lanes import resolve_disposable_observation_binding
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.runtime_identity import (
    runtime_identity_env,
    COMPOSE_IMAGE_REFERENCE_ENV_KEYS,
    RUNTIME_IDENTITY_ENV_KEY,
    parse_runtime_identity_payload,
)


def inspect_retained_compose(spec: dict[str, Any], docker: Callable[..., str]) -> str:
    def read(*arguments: str) -> Any:
        return json.loads(docker(*arguments))

    def identifiers() -> list[str]:
        values = docker("ps", "-aq", "--no-trunc").splitlines()
        if (
            len(values) > 10000
            or len(set(values)) != len(values)
            or any(re.fullmatch(r"[a-f0-9]{64}", value) is None for value in values)
        ):
            raise ValueError("Incomplete container inventory")
        return sorted(values)

    def published_ports(value: object) -> set[int]:
        if value == "":
            return set()
        parts = str(value).split("-")
        if len(parts) not in {1, 2} or any(not part.isdecimal() for part in parts):
            raise ValueError("Port publication unavailable")
        first, last = int(parts[0]), int(parts[-1])
        if not 1 <= first <= last <= 65535:
            raise ValueError("Port publication unavailable")
        return set(range(first, last + 1))

    containers = identifiers()

    def container(identifier: str) -> dict[str, Any]:
        row = read("inspect", identifier)[0]
        return {key: row[key] for key in ("Id", "Image", "Config", "HostConfig", "Mounts")} | {
            "State": {
                key: row["State"].get(key)
                for key in ("Running", "Restarting", "Paused", "StartedAt")
            },
            "NetworkSettings": {"Ports": row["NetworkSettings"]["Ports"]},
        }

    rows = {value: container(value) for value in containers}
    volumes = {name: read("volume", "inspect", name)[0] for name in spec["volumes"]}
    docker_root = Path(read("info", "--format", "{{json .DockerRootDir}}"))
    if not docker_root.is_absolute() or docker_root.is_symlink():
        raise ValueError("Docker storage root unavailable")
    volume_root = docker_root.resolve(strict=True) / "volumes"
    if volume_root.is_symlink() or not volume_root.is_dir():
        raise ValueError("Docker volume root unavailable")
    paths: list[Path] = []
    identities: set[tuple[int, int]] = set()
    for name, volume in volumes.items():
        labels = volume.get("Labels")
        path = Path(volume["Mountpoint"])
        if (
            volume.get("Name") != name
            or volume.get("Driver") != "local"
            or volume.get("Options") not in (None, {})
            or not isinstance(labels, dict)
            or labels.get("io.launchplane.testing-resources") != spec["resources_sha256"]
            or labels.get("io.launchplane.testing-resource-role") != spec["volumes"][name]
            or path != volume_root / name / "_data"
            or not path.is_absolute()
            or path.is_symlink()
            or path.parent.is_symlink()
            or path.name != "_data"
            or path.parent.name != name
            or not path.is_dir()
        ):
            raise ValueError("Resource custody unavailable")
        path = path.resolve(strict=True)
        identity = (path.stat().st_dev, path.stat().st_ino)
        if identity in identities:
            raise ValueError("Storage aliases another allocation")
        paths.append(path)
        identities.add(identity)
    services = spec["render"]["services"]
    names = {key: value["name"] for key, value in spec["render"]["volumes"].items()}
    owned: dict[str, dict[str, Any]] = {}
    projects: set[str] = set()
    credentials = set(spec["credentials"])
    ports: set[int] = set()
    images: dict[str, dict[str, Any]] = {}
    for identifier, row in rows.items():
        if row.get("Id") != identifier:
            raise ValueError("Container identity changed")
        config, host = row["Config"], row["HostConfig"]
        labels = config.get("Labels") or {}
        project = labels.get("com.docker.compose.project", "")
        belongs = bool(re.fullmatch(re.escape(spec["app_name"]) + r"(?:-[a-z0-9]{6})?", project))
        environment = config["Env"]
        if not isinstance(environment, list) or any(
            not isinstance(value, str) or "=" not in value for value in environment
        ):
            raise ValueError("Container environment unavailable")
        values = dict(value.split("=", 1) for value in environment)
        if len(values) != len(environment):
            raise ValueError("Ambiguous environment")
        if not belongs and any(
            value
            and any(
                hmac.compare_digest(value.encode(), credential.encode())
                for credential in credentials
            )
            for value in values.values()
        ):
            raise ValueError("Credential shared outside testing recipient")
        mounts = row["Mounts"]
        if not isinstance(mounts, list):
            raise ValueError("Mount inventory unavailable")
        for mount in mounts:
            backing = Path(mount["Source"]).resolve(strict=True) if mount.get("Source") else None
            overlaps = backing is not None and any(
                backing == path or backing in path.parents or path in backing.parents
                for path in paths
            )
            aliases = (
                backing is not None and (backing.stat().st_dev, backing.stat().st_ino) in identities
            )
            if not belongs and (mount.get("Name") in volumes or overlaps or aliases):
                raise ValueError("Storage shared outside testing recipient")
        bindings = host["PortBindings"] or {}
        if not isinstance(bindings, dict):
            raise ValueError("Port inventory unavailable")
        current_ports = set().union(
            *(
                published_ports(binding["HostPort"])
                for entries in bindings.values()
                for binding in entries or []
            )
        )
        if not belongs:
            effective = row["NetworkSettings"]["Ports"]
            if effective is None and row["State"].get("Running"):
                raise ValueError("Effective foreign publication unavailable")
            if effective is not None and not isinstance(effective, dict):
                raise ValueError("Effective foreign publication unavailable")
            effective_ports = set().union(
                *(
                    published_ports(entry["HostPort"])
                    for entries in (effective or {}).values()
                    for entry in entries or []
                )
            )
            if (current_ports | effective_ports) & set(spec["ports"]):
                raise ValueError("Testing listener shared outside recipient")
            continue
        projects.add(project)
        service_name = labels.get("com.docker.compose.service")
        if (
            not isinstance(service_name, str)
            or service_name not in services
            or service_name in owned
            or str(labels.get("com.docker.compose.oneoff", "false")).lower() != "false"
        ):
            raise ValueError("Ambiguous Compose membership")
        service = services[service_name]
        if (
            row["State"].get("Running") is not True
            or row["State"].get("Restarting")
            or row["State"].get("Paused")
        ):
            raise ValueError("Retained service is not stable")
        if (
            config.get("Image") != service["image"]
            or host.get("Privileged")
            or host.get("NetworkMode") == "host"
            or host.get("PidMode") == "host"
            or host.get("IpcMode") == "host"
            or host.get("VolumesFrom")
            or host.get("CapAdd")
            or host.get("Devices")
        ):
            raise ValueError("Unqualified container capability")
        expected_env = service.get("environment", {})
        if isinstance(expected_env, list):
            expected_env = dict(value.split("=", 1) for value in expected_env)
        image = read("image", "inspect", row["Image"])[0]
        if image["Id"] != row["Image"] or (
            "@sha256:" in service["image"] and service["image"] not in image["RepoDigests"]
        ):
            raise ValueError("Actual image identity unavailable")
        defaults = image["Config"]["Env"]
        if not isinstance(defaults, list) or any(
            not isinstance(value, str) or "=" not in value for value in defaults
        ):
            raise ValueError("Image environment unavailable")
        image_env = dict(value.split("=", 1) for value in defaults)
        if len(image_env) != len(defaults) or values != image_env | {
            key: str(value) for key, value in expected_env.items()
        }:
            raise ValueError("Actual credentials or inputs changed")
        images[row["Image"]] = image
        expected_mounts = set()
        for mount in service.get("volumes", []):
            if isinstance(mount, str):
                parts = mount.split(":")
                key, destination = parts[:2]
                writable = len(parts) == 2 or parts[2] != "ro"
            else:
                key, destination = mount["source"], mount["target"]
                writable = not mount.get("read_only", False)
            expected_mounts.add((names[key], destination, writable))
        tmpfs = service.get("tmpfs", [])
        if isinstance(tmpfs, str):
            tmpfs = [tmpfs]
        if not isinstance(tmpfs, list) or any(
            not isinstance(entry, str) or not entry.startswith("/") for entry in tmpfs
        ):
            raise ValueError("Unsupported tmpfs declaration")
        expected_tmpfs = {entry.partition(":")[0]: entry.partition(":")[2] for entry in tmpfs}
        memory_mounts = [mount for mount in mounts if mount.get("Type") == "tmpfs"]
        volume_mounts = [mount for mount in mounts if mount.get("Type") == "volume"]
        if (
            len(expected_tmpfs) != len(tmpfs)
            or (host.get("Tmpfs") or {}) != expected_tmpfs
            or not {mount.get("Destination") for mount in memory_mounts} <= set(expected_tmpfs)
            or len({mount.get("Destination") for mount in memory_mounts}) != len(memory_mounts)
            or len(volume_mounts) + len(memory_mounts) != len(mounts)
            or {
                (mount.get("Name"), mount.get("Destination"), mount.get("RW"))
                for mount in volume_mounts
            }
            != expected_mounts
            or len(volume_mounts) != len(expected_mounts)
        ):
            raise ValueError("Actual mounts differ from custody")
        expected_bindings = {}
        for port in service.get("ports", []):
            if isinstance(port, (str, int)):
                published, destination = str(port).split(":")
                address = "0.0.0.0"
            else:
                published, destination = str(port["published"]), str(port["target"])
                address = port.get("host_ip", "0.0.0.0")
            expected_bindings[destination + "/tcp"] = [{"HostIp": address, "HostPort": published}]
        normalized = {
            key: [
                {"HostIp": entry["HostIp"] or "0.0.0.0", "HostPort": entry["HostPort"]}
                for entry in entries
            ]
            for key, entries in bindings.items()
        }
        if normalized != expected_bindings or ports.intersection(current_ports):
            raise ValueError("Actual listeners differ from custody")
        effective = {
            key: entries for key, entries in row["NetworkSettings"]["Ports"].items() if entries
        }
        if set(effective) != set(expected_bindings):
            raise ValueError("Effective listeners unavailable")
        for key, entries in effective.items():
            expected = expected_bindings[key][0]
            addresses = {entry["HostIp"] for entry in entries}
            allowed = {"0.0.0.0", "::"} if expected["HostIp"] == "0.0.0.0" else {expected["HostIp"]}
            if (
                expected["HostIp"] not in addresses
                or not addresses <= allowed
                or any(entry["HostPort"] != expected["HostPort"] for entry in entries)
            ):
                raise ValueError("Effective listener changed")
        ports.update(current_ports)
        owned[service_name] = row
    swarm = read("info", "--format", "{{json .Swarm.LocalNodeState}}")
    if swarm not in {"active", "inactive"}:
        raise ValueError("Service inventory unavailable")
    service_ids = (
        sorted(docker("service", "ls", "-q", "--no-trunc").splitlines())
        if swarm == "active"
        else []
    )
    if len(service_ids) > 10000 or len(set(service_ids)) != len(service_ids):
        raise ValueError("Service inventory incomplete")
    service_rows = {}
    for identifier in service_ids:
        service_row = read("service", "inspect", identifier)[0]
        if service_row["ID"] != identifier:
            raise ValueError("Service identity changed")
        service_rows[identifier] = service_row
        for template in (service_row["Spec"], service_row.get("PreviousSpec")):
            if template is None:
                continue
            container_spec = template["TaskTemplate"]["ContainerSpec"]
            for entry in container_spec.get("Env") or []:
                value = entry.split("=", 1)[1]
                if value and any(
                    hmac.compare_digest(value.encode(), credential.encode())
                    for credential in credentials
                ):
                    raise ValueError("Testing credential shared with a service")
            for mount in container_spec.get("Mounts") or []:
                source = mount.get("Source")
                if mount.get("Type") == "volume":
                    driver = (mount.get("VolumeOptions") or {}).get("DriverConfig")
                    if (
                        source in volumes
                        or driver is not None
                        and (
                            driver.get("Name") != "local" or driver.get("Options") not in (None, {})
                        )
                    ):
                        raise ValueError("Service storage isolation unavailable")
                elif mount.get("Type") == "bind":
                    backing = Path(source).resolve(strict=True)
                    if not Path(source).is_absolute() or any(
                        backing == path or backing in path.parents or path in backing.parents
                        for path in paths
                    ):
                        raise ValueError("Service overlaps testing storage")
                elif mount.get("Type") != "tmpfs":
                    raise ValueError("Service mount inventory unavailable")
            for entry in (template.get("EndpointSpec") or {}).get("Ports") or []:
                if entry.get("PublishedPort", 0) and published_ports(entry["PublishedPort"]) & set(
                    spec["ports"]
                ):
                    raise ValueError("Service shares testing listener")
        for entry in (service_row.get("Endpoint") or {}).get("Ports") or []:
            if entry.get("PublishedPort", 0) and published_ports(entry["PublishedPort"]) & set(
                spec["ports"]
            ):
                raise ValueError("Service shares effective testing listener")
    if (
        read("info", "--format", "{{json .Swarm.LocalNodeState}}") != swarm
        or (
            swarm == "active"
            and sorted(docker("service", "ls", "-q", "--no-trunc").splitlines()) != service_ids
        )
        or any(read("service", "inspect", key)[0] != row for key, row in service_rows.items())
    ):
        raise ValueError("Service inventory changed")
    if set(owned) != set(services) or len(projects) != 1 or ports != set(spec["ports"]):
        raise ValueError("Incomplete retained Compose recipient")
    if (
        identifiers() != containers
        or any(container(key) != row for key, row in rows.items())
        or any(read("volume", "inspect", key)[0] != row for key, row in volumes.items())
        or any(read("image", "inspect", key)[0] != row for key, row in images.items())
    ):
        raise ValueError("Physical inventory changed")
    return hashlib.sha256(
        json.dumps(
            {"containers": owned, "volumes": volumes, "images": images}, sort_keys=True
        ).encode()
    ).hexdigest()


def compose_custody_sha256(
    store: PostgresRecordStore, *, root: Path, product: str, context: str
) -> str:
    binding = resolve_disposable_observation_binding(
        store, product=product, context=context, instance="testing"
    )
    record = activation_record(store, product, context)
    if record is None or not record.published or not record.qualified_at:
        raise ValueError("Retained Compose custody unavailable")
    adapter = TestingActivationAdapter(
        root, store, product, record.request, record.actor, record.scope, "testing-custody-read"
    )
    adapter.require_unchanged(record)
    resources, preparation_id = preparation_resources(store, product, record.request, record.scope)
    if preparation_id != record.preparation_record_id:
        raise ValueError("Retained preparation changed")
    inventory = store.read_environment_inventory(context_name=context, instance_name="testing")
    deployment = store.read_deployment_record(inventory.deployment_record_id)
    production = [
        store.read_provider_target_record(
            context_name=lane.context, instance_name=lane.instance
        ).model_dump(mode="json")
        for lane in binding.profile.lanes
        if lane.instance == "prod"
    ]
    return canonical_json_sha256(
        {
            "binding": binding.binding_sha256,
            "activation": record.model_dump(mode="json"),
            "configuration": configuration_digest(
                store, product, context, version=record.configuration_version
            ),
            "authority": [row.model_dump(mode="json") for row in adapter.bindings()],
            "resources": resources.binding_sha256,
            "preparation": preparation_id,
            "inventory": inventory.model_dump(mode="json"),
            "deployment": deployment.model_dump(mode="json"),
            "production": production,
        }
    )


def collect_compose_resource_facts(
    store: PostgresRecordStore, *, root: Path, product: str, context: str
) -> dict[str, str]:
    try:
        return _collect_compose_resource_facts(store, root=root, product=product, context=context)
    except native_copy_stream.NativeSourceAuthorizationDenied:
        raise
    except api.DokployRequestFailed as error:
        if error.status_code in {401, 403}:
            raise native_copy_stream.NativeSourceAuthorizationDenied(
                "The provider refused Compose qualification."
            ) from None
        raise ValueError("Compose qualification unavailable") from error
    except (KeyError, TypeError, OSError, click.ClickException) as error:
        raise ValueError("Compose qualification incomplete") from error


def _collect_compose_resource_facts(
    store: PostgresRecordStore, *, root: Path, product: str, context: str
) -> dict[str, str]:
    binding = resolve_disposable_observation_binding(
        store, product=product, context=context, instance="testing"
    )
    if (
        binding.profile.driver_id != "generic-web"
        or binding.target is None
        or binding.target.target_type != "compose"
    ):
        return {}
    record = activation_record(store, product, context)
    if (
        record is None
        or not record.published
        or not record.qualified_at
        or record.request.application is not None
        or binding.target_id is None
        or record.target_id != binding.target_id.target_id
    ):
        raise ValueError("Retained Compose custody unavailable")
    adapter = TestingActivationAdapter(
        root,
        store,
        product,
        record.request,
        record.actor,
        record.scope,
        "testing-resource-collection",
    )
    adapter.require_unchanged(record)
    authority = adapter.bindings()
    resources, preparation_id = preparation_resources(store, product, record.request, record.scope)
    if preparation_id != record.preparation_record_id:
        raise ValueError("Retained allocation changed")
    inventory = store.read_environment_inventory(context_name=context, instance_name="testing")
    deployment = store.read_deployment_record(inventory.deployment_record_id)
    if (
        inventory.runtime_identity is None
        or deployment.runtime_identity != inventory.runtime_identity
        or deployment.artifact_identity is None
        or deployment.artifact_identity != inventory.artifact_identity
        or deployment.artifact_identity.artifact_id != inventory.runtime_identity.artifact_id
        or deployment.deployed_target is None
        or deployment.deployed_target.target_id != record.target_id
        or (
            inventory.runtime_identity.product,
            inventory.runtime_identity.context,
            inventory.runtime_identity.instance,
            inventory.runtime_identity.deployment_record_id,
            inventory.runtime_identity.source_git_ref,
        )
        != (product, context, "testing", inventory.deployment_record_id, inventory.source_git_ref)
        or (deployment.context, deployment.instance, deployment.source_git_ref)
        != (context, "testing", inventory.source_git_ref)
        or deployment.deployed_target
        != store.read_provider_target_record(
            context_name=context, instance_name="testing"
        ).to_deployed_target_reference()
    ):
        raise ValueError("Current testing artifact unavailable")
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=record.target_id
    )
    provider_values = api.parse_dokploy_env_text(str(payload.get("env") or ""))
    observed_identity = parse_runtime_identity_payload(
        provider_values.get(RUNTIME_IDENTITY_ENV_KEY)
    )
    if observed_identity is None or observed_identity.model_dump(
        exclude={"deployed_at"}
    ) != inventory.runtime_identity.model_dump(exclude={"deployed_at"}):
        raise ValueError("Provider runtime identity differs from current inventory")
    current = record.model_copy(
        update={"image_reference": deployment.artifact_identity.artifact_id.strip()}
    )
    values = runtime_values(store, root, current, resources)
    values.update(runtime_identity_env(observed_identity))
    values[RUNTIME_IDENTITY_ENV_KEY] = provider_values[RUNTIME_IDENTITY_ENV_KEY]
    values.update({key: current.image_reference for key in COMPOSE_IMAGE_REFERENCE_ENV_KEYS})
    adapter.verify_compose_provider(current, values=values)
    render = rendered_source(current, values, resources)
    bindings = runtime_environments.site_delivered_secret_bindings_from_store(
        record_store=store, context_name=context, instance_name="testing"
    )
    shared_keys = {key for key, binding in bindings.items() if shared_read_only_binding(binding)}
    spec: dict[str, object] = {
        "app_name": record.app_name,
        "render": render,
        "volumes": {value: key for key, value in resources.volumes.items()},
        "resources_sha256": resources.binding_sha256,
        "ports": sorted(int(values[key]) for key in record.request.port_keys),
        "credentials": sorted(
            {
                value
                for key, value in values.items()
                if key not in shared_keys and looks_like_credential(key, value)
            }
        ),
    }
    frames: list[dict[str, object]] = []
    native_copy_stream._read_remote(
        host=host,
        token=token,
        server_id=native_copy_stream.require_registered_remote_server(payload.get("serverId")),
        program=COMPOSE_RESOURCE_PROGRAM,
        payload=spec,
        receive=frames.append,
        timeout_seconds=90,
    )
    if (
        len(frames) != 1
        or set(frames[0]) != {"type", "sha256"}
        or frames[0]["type"] != "complete"
        or re.fullmatch(r"[a-f0-9]{64}", str(frames[0]["sha256"])) is None
    ):
        raise ValueError("Physical Compose qualification unavailable")
    adapter.require_unchanged(record)
    adapter.verify_compose_provider(current, values=values)
    if (
        authority != adapter.bindings()
        or inventory
        != store.read_environment_inventory(context_name=context, instance_name="testing")
        or binding
        != resolve_disposable_observation_binding(
            store, product=product, context=context, instance="testing"
        )
        or payload
        != api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=record.target_id
        )
    ):
        raise ValueError("Compose qualification authority changed")
    if activation_record(store, product, context) != record:
        raise ValueError("Retained custody changed during inspection")
    reference = "service:testing-resource-inspection:" + canonical_json_sha256(
        {
            "activation": record.model_dump(mode="json"),
            "allocation": resources.binding_sha256,
            "binding": binding.binding_sha256,
            "inventory": inventory.model_dump(mode="json"),
            "physical": frames[0]["sha256"],
            "provider": adapter.recipient_configuration_sha256(payload),
        }
    )
    return {
        "data": "reproducible",
        "data_evidence": reference,
        "production_resources": "isolated",
        "isolation_evidence": reference,
    }


COMPOSE_RESOURCE_PROGRAM = (
    "import base64,hashlib,hmac,json,re,subprocess,sys,time\nfrom pathlib import Path\nfrom typing import Any,Callable\n"
    + inspect.getsource(docker_daemon_authorization_denied)
    + "\n"
    + inspect.getsource(inspect_retained_compose)
    + r"""
deadline = time.monotonic() + 70
def docker(*arguments):
    result = subprocess.run(['docker', *arguments], capture_output=True, timeout=max(0.1, min(10, deadline - time.monotonic())))
    if result.returncode and docker_daemon_authorization_denied(result.stderr):
        print(sys.argv[2] + json.dumps({'type':'denied'}), flush=True)
        sys.exit(0)
    if result.returncode or len(result.stdout) > 8 * 1024 * 1024 or time.monotonic() > deadline:
        raise ValueError('Incomplete physical inventory')
    return result.stdout.decode('utf-8', errors='strict')
try:
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    digest = inspect_retained_compose(spec, docker)
    print(sys.argv[2] + json.dumps({'type':'complete','sha256':digest}), flush=True)
except Exception:
    print(sys.argv[2] + json.dumps({'type':'failed'}), flush=True)
"""
)
