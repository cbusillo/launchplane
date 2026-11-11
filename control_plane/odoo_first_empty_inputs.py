from __future__ import annotations

from dataclasses import dataclass
from collections.abc import Callable
from datetime import UTC, datetime
import inspect
import json
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any, Literal, cast

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import (
    FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
    LaunchplaneProductProfileRecord,
    product_target_owner_products,
)
from control_plane.dokploy import api, native_copy_stream, source
from control_plane.docker_errors import docker_daemon_authorization_denied
from control_plane.runtime_platform_credentials import plain_setting_looks_like_credential
from control_plane.storage.product_authority_bundle import (
    ProductAuthorityBundle,
    ProviderTargetWrite,
    RuntimeEnvironmentSetExpectation,
    SecretDeliverySetExpectation,
)

if TYPE_CHECKING:
    from control_plane.storage.postgres import PostgresRecordStore

FirstEmptyIntent = Literal["runtime_credentials"]


class FirstEmptyInputResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: FirstEmptyIntent = "runtime_credentials"
    plan_sha256: str
    data: Literal["empty"] = "empty"
    provenance: Literal["service:original-target-storage"] = "service:original-target-storage"
    applied: bool = False


@dataclass(frozen=True)
class FirstEmptyAuthority:
    profile: LaunchplaneProductProfileRecord
    target: DokployTargetRecord
    target_id: DokployTargetIdRecord
    provider: ProviderTargetRecord
    bundle: ProductAuthorityBundle
    volumes: tuple[str, ...]

    def fingerprint(self) -> str:
        return nonvalidating_compact_sorted_json_sha256(
            {
                "profile": self.profile.model_dump(mode="json"),
                "target": self.target.model_dump(mode="json"),
                "target_id": self.target_id.model_dump(mode="json"),
                "provider": self.provider.model_dump(mode="json"),
                "expectations": self.bundle.model_dump(mode="json"),
                "volumes": self.volumes,
            }
        )


@dataclass(frozen=True)
class QualifiedFirstEmptyInputs:
    authority: FirstEmptyAuthority
    intent: FirstEmptyIntent | Literal["replacement"]
    plan_sha256: str
    observed_at: datetime
    storage_sha256: str
    provider_spec: dict[str, object]

    def validate(self, product: str, context: str, instance: str) -> None:
        if (product, context, instance) != (
            self.authority.profile.product,
            self.authority.target.context,
            "prod",
        ) or not 0 <= (datetime.now(UTC) - self.observed_at).total_seconds() < 60:
            raise ValueError("First-empty qualification is stale or belongs to another target.")


def resolve_first_empty_authority(
    store: PostgresRecordStore,
    product: str,
    context: str,
    *,
    pending_deployment: DeploymentRecord | None = None,
) -> FirstEmptyAuthority:
    profile = store.read_product_profile_record(product)
    lane = next(
        (lane for lane in profile.lanes if (lane.context, lane.instance) == (context, "prod")), None
    )
    if lane is None:
        raise ValueError("The product has no original production lane in this context.")
    protected_lane = "First-empty inputs require one unprotected original Odoo production lane."
    if (
        profile.driver_id != "odoo"
        or not profile.is_active
        or profile.production_use == "live"
        or lane.odoo_data_policy.data_authority not in {"unknown", "resettable"}
        or lane.odoo_data_policy.upstream_source
    ):
        raise ValueError(protected_lane)
    if profile.historical_contexts:
        raise ValueError(protected_lane)
    if product_target_owner_products(
        store.list_product_profile_records(), context=context, config_scopes=True
    ) != frozenset({product}):
        raise ValueError(protected_lane)
    target = store.read_dokploy_target_record(context_name=context, instance_name="prod")
    target_id = store.read_dokploy_target_id_record(context_name=context, instance_name="prod")
    provider = store.read_provider_target_record(context_name=context, instance_name="prod")
    protected_target = "Original target ownership is ambiguous or protected."
    if target.policies.staff_testing_hold is not None:
        raise ValueError(protected_target)
    if (
        target.target_type != "compose"
        or provider.provider_id != "dokploy"
        or provider.target_category != "compose"
        or provider.target_id != target_id.target_id
        or provider.display_name != target.target_name
    ):
        raise ValueError(protected_target)
    if any(
        other.target_id == target_id.target_id and other != target_id
        for other in store.list_dokploy_target_id_records()
    ) or any(
        other.target_id == provider.target_id and other != provider
        for other in store.list_physical_provider_target_records()
    ):
        raise ValueError(protected_target)
    safety = store.read_disposable_lane_safety(product=product, context=context, instance="prod")
    if safety is not None and (
        safety.usage == "in_use"
        or safety.data == "only_copy"
        or safety.production_resources in {"shared", "separation_ready"}
    ):
        raise ValueError("Recorded data, use or custody protects this target.")
    if (
        store.list_deployment_records(context_name=context, instance_name="prod")
        != ((pending_deployment,) if pending_deployment is not None else ())
        or store.list_promotion_records(context_name=context, to_instance_name="prod")
        or store.list_backup_gate_records(context_name=context, instance_name="prod")
    ):
        raise ValueError("Production history requires the existing recovery or release route.")
    runtime = store.list_runtime_environment_records()
    exact = [
        record
        for record in runtime
        if (record.scope, record.context, record.instance) == ("instance", context, "prod")
    ]
    if len(exact) != 1:
        raise ValueError("Record the original lane's runtime storage intent first.")
    keys = ("ODOO_DB_VOLUME", "ODOO_DATA_VOLUME", "ODOO_LOG_VOLUME")
    volumes = tuple(str(exact[0].env.get(key, "")) for key in keys)
    if (
        len(set(volumes)) != len(keys)
        or any(re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name) is None for name in volumes)
        or any(
            value in volumes
            for record in runtime
            if record != exact[0]
            for key, value in record.env.items()
            if key.endswith("_VOLUME")
        )
    ):
        raise ValueError("Original storage intent is missing or shared with another lane.")
    deliveries = tuple(
        SecretDeliverySetExpectation(
            integration="runtime_environment",
            context=selected_context,
            instance="prod",
            records=store.list_secret_records(
                integration="runtime_environment", context_name=selected_context
            ),
            bindings=store.list_secret_bindings(
                integration="runtime_environment", context_name=selected_context
            ),
        )
        for selected_context in ("", context)
    )
    runtime_read_set = RuntimeEnvironmentSetExpectation(
        contexts=(context,),
        instances=((context, "prod"),),
        include_global=True,
        records=runtime,
    )
    runtime_read_set = runtime_read_set.model_copy(
        update={
            "records": tuple(record for record in runtime if runtime_read_set.includes(record)),
        }
    )
    if any(
        plain_setting_looks_like_credential(key, str(value))
        for record in runtime_read_set.records
        for key, value in record.env.items()
    ):
        raise ValueError(
            "Move credential-shaped runtime settings through the existing managed-secret route first."
        )
    bundle = ProductAuthorityBundle(
        expected_product_profiles=(profile,),
        expected_dokploy_targets=(target,),
        expected_dokploy_target_ids=(target_id,),
        provider_target_writes=(ProviderTargetWrite(record=provider, expected_record=provider),),
        required_context_owners=((product, context),),
        required_product_config_target=(product, context, "prod"),
        secret_delivery_read_sets=deliveries,
        runtime_environment_read_sets=(runtime_read_set,),
    )
    return FirstEmptyAuthority(profile, target, target_id, provider, bundle, volumes)


def _provider_projection(
    payload: api.JsonObject,
    authority: FirstEmptyAuthority,
    *,
    staged_environment: str | None = None,
    staged_compose: str | None = None,
) -> dict[str, object]:
    if "composeId" not in payload:
        raise ValueError("Provider response omitted the compose identity.")
    if "appName" not in payload:
        raise ValueError("Provider response omitted the project name.")
    if "serverId" not in payload:
        raise ValueError("Provider response omitted the server identity.")
    if "env" not in payload:
        raise ValueError("Provider response omitted the environment.")
    if "composeFile" not in payload:
        raise ValueError("Provider response omitted the compose source.")
    if "mounts" not in payload:
        raise ValueError("Provider response omitted the mounts.")
    if "autoDeploy" not in payload:
        raise ValueError("Provider response omitted automatic deployment state.")
    if payload["composeId"] != authority.target_id.target_id:
        raise ValueError("Provider compose identity differs from the original target.")
    project = payload["appName"]
    if not isinstance(project, str):
        raise ValueError("Provider project name is not a string.")
    if not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", project):
        raise ValueError("Provider project name is not a supported storage project identity.")
    if staged_environment is None:
        if payload["env"] not in (None, ""):
            raise ValueError("Original provider environment is not empty.")
    elif payload["env"] != staged_environment:
        raise ValueError("Provider environment differs from the original executor's staged input.")
    if staged_compose is None:
        if payload["composeFile"] not in (None, ""):
            raise ValueError("Original provider compose source is not empty.")
    elif payload["composeFile"] != staged_compose:
        raise ValueError(
            "Provider compose source differs from the original executor's staged input."
        )
    if payload["mounts"] not in (None, []):
        raise ValueError("Original provider mounts are not empty.")
    if payload["autoDeploy"] is True:
        raise ValueError("Original provider automatic deployment is enabled.")
    if payload["autoDeploy"] is not False:
        raise ValueError("Provider automatic deployment state is not a verified boolean.")
    return {
        "target_id": authority.target_id.target_id,
        "project": project,
        "server": native_copy_stream.require_registered_remote_server(payload.get("serverId")),
        "volumes": authority.volumes,
    }


def qualify_first_empty_storage(
    store: PostgresRecordStore,
    *,
    root: Path,
    product: str,
    context: str,
    intent: FirstEmptyIntent | Literal["replacement"],
    previous: QualifiedFirstEmptyInputs | None = None,
    pending_deployment: DeploymentRecord | None = None,
    staged_environment: str | None = None,
    staged_compose: str | None = None,
) -> QualifiedFirstEmptyInputs:
    if (
        pending_deployment is not None
        or staged_environment is not None
        or staged_compose is not None
    ) and (previous is None or previous.intent != "replacement" or intent != "replacement"):
        raise ValueError("Staged inputs require the original executor qualification.")
    authority = resolve_first_empty_authority(
        store, product, context, pending_deployment=pending_deployment
    )
    if previous is not None and authority != previous.authority:
        raise ValueError("Original profile, target, runtime or binding authority changed.")
    if intent == "replacement" and not authority.profile.prelaunch_bootstrap_authorized:
        raise ValueError("Empty replacement requires explicit prelaunch bootstrap authority.")
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )

    def provider_read() -> dict[str, object]:
        payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=authority.target_id.target_id
        )
        if api.list_deployments_for_target(
            host=host,
            token=token,
            target_type="compose",
            target_id=authority.target_id.target_id,
            require_complete=True,
        ):
            raise ValueError("Provider deployment history contradicts first-empty preparation.")
        return _provider_projection(
            payload, authority, staged_environment=staged_environment, staged_compose=staged_compose
        )

    spec = provider_read()
    if previous is not None and spec != previous.provider_spec:
        raise ValueError("Original physical target changed.")
    frames: list[dict[str, object]] = []
    native_copy_stream.run_managed_server_program(
        host=host,
        token=token,
        server_id=cast(str, spec["server"]),
        program=first_empty_storage_program(),
        payload=spec,
        receive=frames.append,
        timeout_seconds=60,
    )
    expected_binding = nonvalidating_compact_sorted_json_sha256(spec)
    if any(frame.get("type") == "denied" for frame in frames):
        raise native_copy_stream.NativeSourceAuthorizationDenied("Original storage read refused.")
    storage = frames[0].get("storage") if len(frames) == 1 else None
    if (
        len(frames) != 1
        or set(frames[0]) != {"type", "binding", "storage"}
        or frames[0]["type"] != "complete"
        or frames[0]["binding"] != expected_binding
        or not isinstance(storage, str)
        or re.fullmatch(r"[a-f0-9]{64}", storage) is None
        or provider_read() != spec
        or resolve_first_empty_authority(
            store, product, context, pending_deployment=pending_deployment
        )
        != authority
        or (previous is not None and storage != previous.storage_sha256)
    ):
        raise ValueError("Fresh original-target storage qualification is incomplete or changed.")
    digest = nonvalidating_compact_sorted_json_sha256(
        {
            "authority": authority.fingerprint(),
            "provider": spec,
            "storage": frames[0]["storage"],
            "intent": intent,
        }
    )
    return QualifiedFirstEmptyInputs(authority, intent, digest, datetime.now(UTC), storage, spec)


def validate_first_empty_generation(
    qualified: QualifiedFirstEmptyInputs,
    *,
    product: str,
    context: str,
    instance: str,
    entries: tuple[dict[str, object], ...],
) -> LaunchplaneProductProfileRecord:
    qualified.validate(product, context, instance)
    if qualified.intent != "runtime_credentials" or not entries:
        raise ValueError("Runtime credentials require their reviewed first-empty intent.")
    keys = [cast(str, entry["binding_key"]) for entry in entries]
    if (
        len(set(keys)) != len(keys)
        or not set(keys) <= FRESH_TESTING_LOCAL_CREDENTIAL_KEYS
        or any(entry["name"] != entry["binding_key"] for entry in entries)
    ):
        raise ValueError(
            "First-empty generation accepts only exact Odoo runtime credential purposes."
        )
    product_deliveries = tuple(
        delivery
        for delivery in qualified.authority.bundle.secret_delivery_read_sets
        if delivery.context == context
    )
    if any(
        binding.binding_key in keys and delivery.includes(binding)
        for delivery in product_deliveries
        for binding in delivery.bindings
    ):
        raise ValueError("Existing runtime credential bindings cannot be replaced.")
    return qualified.authority.profile


def first_empty_storage_snapshot(
    docker: Callable[..., str],
    spec: dict[str, Any],
    *,
    compose_root: Path,
    read_swarm: Callable[[Callable[..., str]], list[dict[str, Any]]],
) -> dict[str, object]:
    def inventory(*arguments: str) -> list[str]:
        values = docker(*arguments).splitlines()
        if (
            len(values) > 10000
            or len(set(values)) != len(values)
            or any(
                re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", value) is None for value in values
            )
        ):
            raise ValueError("Incomplete Docker inventory.")
        return sorted(values)

    def metadata(identifiers: list[str], *arguments: str, key: str) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for offset in range(0, len(identifiers), 128):
            batch = identifiers[offset : offset + 128]
            items = [json.loads(line) for line in docker(*arguments, *batch).splitlines()]
            if (
                len(items) != len(batch)
                or any(not isinstance(item, dict) for item in items)
                or {item.get(key) for item in items} != set(batch)
            ):
                raise ValueError("Incomplete Docker metadata.")
            result.update({item[key]: item for item in items})
        return result

    project = spec["project"]
    names = spec["volumes"]
    if (
        not isinstance(project, str)
        or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", project) is None
        or not isinstance(names, (list, tuple))
        or len(names) != 3
        or len(set(names)) != 3
        or any(
            not isinstance(name, str)
            or re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", name) is None
            for name in names
        )
    ):
        raise ValueError("Invalid original storage specification.")
    project_root = compose_root / project
    if any(path.is_symlink() for path in (project_root, *project_root.parents)):
        raise ValueError("Original project path is aliased.")
    if project_root.exists() and (not project_root.is_dir() or any(project_root.iterdir())):
        raise ValueError("Original project has retained files.")
    root_info = json.loads(docker("info", "--format", "{{json .}}"))
    root = Path(root_info["DockerRootDir"])
    if not root.is_absolute() or any(path.is_symlink() for path in (root, *root.parents)):
        raise ValueError("Docker storage root is unqualified.")
    volume_root = root / "volumes"
    if not volume_root.is_dir() or volume_root.is_symlink():
        raise ValueError("Docker volume storage is unavailable.")
    storage_names = (*names, names[1] + "-filestore")
    paths = [volume_root / name for name in storage_names]

    def overlaps_original(value: str) -> bool:
        path = Path(value)
        if not path.is_absolute():
            raise ValueError("Backing path is unknown.")
        path = path.resolve()
        if any(
            path == original or original in path.parents or path in original.parents
            for original in (*paths, project_root)
        ):
            return True

        def path_identity(candidate: Path) -> tuple[int, int] | None:
            if not candidate.exists():
                return None
            observed = candidate.stat()
            return observed.st_dev, observed.st_ino

        mounted_identity = path_identity(path)
        for original in (*paths, *(storage_path / "_data" for storage_path in paths), project_root):
            original_identity = path_identity(original)
            if original_identity is not None and any(
                path_identity(parent) == original_identity for parent in (path, *path.parents)
            ):
                return True
            if mounted_identity is not None and any(
                path_identity(parent) == mounted_identity for parent in original.parents
            ):
                return True
        return False

    info = root_info
    if not isinstance(info, dict) or info.get("Swarm", {}).get("LocalNodeState") not in {
        "inactive",
        "active",
    }:
        raise ValueError("Original engine inventory is incomplete.")
    swarm = read_swarm(docker) if info["Swarm"]["LocalNodeState"] == "active" else []
    services = inventory("service", "ls", "--quiet", "--no-trunc") if swarm else []
    service_rows = metadata(
        services,
        "service",
        "inspect",
        "--format",
        '{"ID":{{json .ID}},"Name":{{json .Spec.Name}},"Labels":{{json .Spec.Labels}},"Mounts":{{json .Spec.TaskTemplate.ContainerSpec.Mounts}}}',
        key="ID",
    )
    for row in service_rows.values():
        mounts = row.get("Mounts") or []
        if not isinstance(mounts, list):
            raise ValueError("Service inventory is incomplete.")
        if (
            row.get("Name") == project
            or str(row.get("Name", "")).startswith(project + "_")
            or any(mount.get("Source") in storage_names for mount in mounts)
        ):
            raise ValueError("Original resources are referenced by a service.")
        for mount in mounts:
            if mount.get("Type") not in {"volume", "bind", "tmpfs"}:
                raise ValueError("Service backing storage is unknown.")
            if mount.get("Type") == "bind" and overlaps_original(str(mount.get("Source", ""))):
                raise ValueError("Original storage is referenced by a service.")
    volumes = inventory("volume", "ls", "--quiet")
    containers = inventory("ps", "-aq", "--no-trunc")
    volume_rows = metadata(volumes, "volume", "inspect", "--format", "{{json .}}", key="Name")
    selected: dict[str, object] = {}
    for name in volumes:
        if name in storage_names:
            continue
        row = volume_rows[name]
        labels = row.get("Labels") or {}
        options = row.get("Options") or {}
        if (
            row.get("Name") != name
            or not isinstance(labels, dict)
            or not isinstance(options, dict)
            or labels.get("com.docker.compose.project") == project
            or name.startswith(project + "_")
            or (
                str(options.get("device", "")).startswith("/")
                and overlaps_original(str(options["device"]))
            )
        ):
            raise ValueError(
                "Other storage retains original project custody or aliases its backing path."
            )
    selected_identities: set[tuple[int, int]] = set()
    for name in storage_names:
        directory = volume_root / name
        if name not in volumes:
            if directory.exists() or directory.is_symlink():
                raise ValueError("Unregistered original storage has retained custody.")
            selected[name] = "absent"
            continue
        row = volume_rows[name]
        backing = directory / "_data"
        if (
            row.get("Name") != name
            or row.get("Driver") != "local"
            or row.get("Options") not in (None, {})
            or row.get("Labels") not in (None, {})
            or row.get("Mountpoint") != str(backing)
            or any(path.is_symlink() for path in (directory, backing))
            or not backing.is_dir()
            or any(backing.iterdir())
        ):
            raise ValueError("Original storage is nonempty, aliased or unavailable.")
        stat = backing.stat()
        identity = stat.st_dev, stat.st_ino
        if identity in selected_identities:
            raise ValueError("Original storage purposes alias the same backing directory.")
        selected_identities.add(identity)
        selected[name] = {
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "modified": stat.st_mtime_ns,
            "metadata": row,
        }
    rows = metadata(
        containers,
        "inspect",
        "--type",
        "container",
        "--format",
        '{"Id":{{json .Id}},"Name":{{json .Name}},"Labels":{{json .Config.Labels}},"Mounts":{{json .Mounts}}}',
        key="Id",
    )
    for row in rows.values():
        if not isinstance(row.get("Mounts"), list):
            raise ValueError("Container storage inventory is incomplete.")
        labels = row.get("Labels") or {}
        if not isinstance(labels, dict) or labels.get("com.docker.compose.project") == project:
            raise ValueError("Original project is in use.")
        for mount in row["Mounts"]:
            if not isinstance(mount, dict) or mount.get("Type") not in {"volume", "bind", "tmpfs"}:
                raise ValueError("Container storage ownership is unknown.")
            if mount.get("Type") == "tmpfs":
                continue
            if mount.get("Name") in selected or overlaps_original(str(mount.get("Source", ""))):
                raise ValueError("Original storage is referenced by a container.")
    return {
        "volumes": volumes,
        "containers": rows,
        "services": service_rows,
        "swarm": swarm,
        "selected": selected,
        "project_exists": project_root.exists(),
        "docker_root": str(root),
    }


def first_empty_storage_program(*, compose_root: Path = Path("/etc/dokploy/compose")) -> str:
    from control_plane.testing_application_capabilities import single_local_swarm_node

    return (
        "import base64,hashlib,json,re,subprocess,sys\nfrom pathlib import Path\n"
        "from typing import Any\nfrom collections.abc import Callable\n"
        + f"NATIVE_PROGRAM_SOURCE_SITE_LIMIT={native_copy_stream.NATIVE_PROGRAM_SOURCE_SITE_LIMIT}\n"
        + inspect.getsource(docker_daemon_authorization_denied)
        + "\n"
        + inspect.getsource(single_local_swarm_node)
        + "\n"
        + inspect.getsource(first_empty_storage_snapshot)
        + "\nCOMPOSE_ROOT=Path("
        + repr(str(compose_root))
        + ")\n"
        + """
def docker(*arguments):
    result = subprocess.run(['docker', '--host', 'unix:///var/run/docker.sock', *arguments], capture_output=True, timeout=8)
    if result.returncode:
        if docker_daemon_authorization_denied(result.stderr):
            raise PermissionError('Docker read refused')
        raise ValueError('Docker read unavailable')
    if len(result.stdout) > 8 * 1024 * 1024:
        raise ValueError('Docker inventory exceeds bound')
    return result.stdout.decode('utf-8')
def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
try:
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    before = first_empty_storage_snapshot(docker, spec, compose_root=COMPOSE_ROOT, read_swarm=single_local_swarm_node)
    after = first_empty_storage_snapshot(docker, spec, compose_root=COMPOSE_ROOT, read_swarm=single_local_swarm_node)
    if before != after:
        raise ValueError('Storage changed during observation')
    result = {'type':'complete', 'binding':digest(spec), 'storage':digest(before['selected'])}
except PermissionError:
    result = {'type':'denied'}
except Exception as error:
    sites=[]
    failure_traceback=error.__traceback__
    while failure_traceback is not None:
        if failure_traceback.tb_frame.f_code.co_filename=='<string>': sites.append(failure_traceback.tb_lineno)
        failure_traceback=failure_traceback.tb_next
    result = {'type':'failed', 'sites':sites[-NATIVE_PROGRAM_SOURCE_SITE_LIMIT:]}
print(sys.argv[2] + json.dumps(result), flush=True)
"""
    )
