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
from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import (
    FRESH_TESTING_LOCAL_CREDENTIAL_KEYS,
    LaunchplaneProductProfileRecord,
    ProductOdooLaneDataPolicy,
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

FirstEmptyIntent = Literal["data_policy", "runtime_credentials"]


class FirstEmptyInputResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: FirstEmptyIntent
    plan_sha256: str
    data: Literal["empty"] = "empty"
    provenance: Literal["service:original-target-storage"] = "service:original-target-storage"
    applied: bool = False
    current_policy: ProductOdooLaneDataPolicy
    proposed_policy: ProductOdooLaneDataPolicy | None = None


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
    intent: FirstEmptyIntent
    plan_sha256: str
    observed_at: datetime

    def validate(self, product: str, context: str, instance: str) -> None:
        if (product, context, instance) != (
            self.authority.profile.product,
            self.authority.target.context,
            "prod",
        ) or not 0 <= (datetime.now(UTC) - self.observed_at).total_seconds() < 60:
            raise ValueError("First-empty qualification is stale or belongs to another target.")


def resolve_first_empty_authority(
    store: PostgresRecordStore, product: str, context: str
) -> FirstEmptyAuthority:
    profile = store.read_product_profile_record(product)
    lane = next(
        lane for lane in profile.lanes if (lane.context, lane.instance) == (context, "prod")
    )
    if (
        profile.driver_id != "odoo"
        or not profile.is_active
        or profile.production_use == "live"
        or profile.historical_contexts
        or lane.odoo_data_policy.data_authority not in {"unknown", "resettable"}
        or lane.odoo_data_policy.upstream_source
        or product_target_owner_products(
            store.list_product_profile_records(), context=context, config_scopes=True
        )
        != frozenset({product})
    ):
        raise ValueError(
            "First-empty inputs require one unprotected original Odoo production lane."
        )
    target = store.read_dokploy_target_record(context_name=context, instance_name="prod")
    target_id = store.read_dokploy_target_id_record(context_name=context, instance_name="prod")
    provider = store.read_provider_target_record(context_name=context, instance_name="prod")
    if (
        target.target_type != "compose"
        or target.policies.staff_testing_hold is not None
        or provider.provider_id != "dokploy"
        or provider.target_category != "compose"
        or provider.target_id != target_id.target_id
        or provider.display_name != target.target_name
        or any(
            other.target_id == target_id.target_id and other != target_id
            for other in store.list_dokploy_target_id_records()
        )
        or any(
            other.target_id == provider.target_id and other != provider
            for other in store.list_physical_provider_target_records()
        )
    ):
        raise ValueError("Original target ownership is ambiguous or protected.")
    safety = store.read_disposable_lane_safety(product=product, context=context, instance="prod")
    if safety is not None and (
        safety.usage == "in_use"
        or safety.data == "only_copy"
        or safety.production_resources in {"shared", "separation_ready"}
    ):
        raise ValueError("Recorded data, use or custody protects this target.")
    if (
        store.list_deployment_records(context_name=context, instance_name="prod")
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
    payload: api.JsonObject, authority: FirstEmptyAuthority
) -> dict[str, object]:
    if (
        not {"composeId", "appName", "serverId", "env", "composeFile", "mounts", "autoDeploy"}
        <= payload.keys()
        or payload.get("composeId") != authority.target_id.target_id
        or not isinstance(payload.get("appName"), str)
        or not re.fullmatch(r"[a-zA-Z0-9][a-zA-Z0-9_.-]{0,127}", str(payload["appName"]))
        or payload.get("env") not in {None, ""}
        or payload.get("composeFile") not in {None, ""}
        or payload.get("mounts") not in (None, [])
        or payload.get("autoDeploy") is not False
    ):
        raise ValueError("Original provider target is not an unstarted empty configuration.")
    return {
        "target_id": authority.target_id.target_id,
        "project": payload["appName"],
        "server": native_copy_stream.require_registered_remote_server(payload.get("serverId")),
        "volumes": authority.volumes,
    }


def qualify_first_empty_storage(
    store: PostgresRecordStore,
    *,
    root: Path,
    product: str,
    context: str,
    intent: FirstEmptyIntent,
) -> QualifiedFirstEmptyInputs:
    authority = resolve_first_empty_authority(store, product, context)
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
        return _provider_projection(payload, authority)

    spec = provider_read()
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
        or resolve_first_empty_authority(store, product, context) != authority
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
    return QualifiedFirstEmptyInputs(authority, intent, digest, datetime.now(UTC))


def build_first_empty_data_policy_plan(
    qualified: QualifiedFirstEmptyInputs,
) -> LaunchplaneProductProfileRecord:
    profile = qualified.authority.profile
    context = qualified.authority.target.context
    qualified.validate(profile.product, context, "prod")
    replacement = profile.model_copy(
        update={
            "lanes": tuple(
                lane.model_copy(
                    update={
                        "odoo_data_policy": ProductOdooLaneDataPolicy(
                            data_authority="resettable",
                            allowed_rebuild_sources=("empty",),
                            requires_runtime_identity=lane.odoo_data_policy.requires_runtime_identity,
                        )
                    }
                )
                if (lane.context, lane.instance) == (context, "prod")
                else lane
                for lane in profile.lanes
            )
        }
    )
    return LaunchplaneProductProfileRecord.model_validate(
        replacement.model_dump(mode="json")
    ).validate_write_contract()


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
    if any(
        binding.binding_key in keys and delivery.includes(binding)
        for delivery in qualified.authority.bundle.secret_delivery_read_sets
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

    def metadata(*arguments: str) -> dict[str, Any]:
        items = json.loads(docker(*arguments))
        if not isinstance(items, list) or len(items) != 1 or not isinstance(items[0], dict):
            raise ValueError("Incomplete Docker metadata.")
        return items[0]

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
        return any(
            path == original or original in path.parents or path in original.parents
            for original in (*paths, project_root)
        )

    info = root_info
    if not isinstance(info, dict) or info.get("Swarm", {}).get("LocalNodeState") not in {
        "inactive",
        "active",
    }:
        raise ValueError("Original engine inventory is incomplete.")
    swarm = read_swarm(docker) if info["Swarm"]["LocalNodeState"] == "active" else []
    services = inventory("service", "ls", "--quiet") if swarm else []
    service_rows = {}
    for identifier in services:
        row = metadata(
            "service",
            "inspect",
            "--format",
            '[{"ID":{{json .ID}},"Name":{{json .Spec.Name}},"Labels":{{json .Spec.Labels}},"Mounts":{{json .Spec.TaskTemplate.ContainerSpec.Mounts}}}]',
            identifier,
        )
        mounts = row.get("Mounts") or []
        if row.get("ID") != identifier or not isinstance(mounts, list):
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
        service_rows[identifier] = row
    volumes = inventory("volume", "ls", "--quiet")
    containers = inventory("ps", "-aq", "--no-trunc")
    selected: dict[str, object] = {}
    for name in volumes:
        if name in storage_names:
            continue
        row = metadata("volume", "inspect", name)
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
    for name in storage_names:
        directory = volume_root / name
        if name not in volumes:
            if directory.exists() or directory.is_symlink():
                raise ValueError("Unregistered original storage has retained custody.")
            selected[name] = "absent"
            continue
        row = metadata("volume", "inspect", name)
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
        selected[name] = {
            "device": stat.st_dev,
            "inode": stat.st_ino,
            "modified": stat.st_mtime_ns,
            "metadata": row,
        }
    rows: dict[str, object] = {}
    for identifier in containers:
        row = metadata(
            "inspect",
            "--type",
            "container",
            "--format",
            '[{"Id":{{json .Id}},"Name":{{json .Name}},"Labels":{{json .Config.Labels}},"Mounts":{{json .Mounts}}}]',
            identifier,
        )
        if row.get("Id") != identifier or not isinstance(row.get("Mounts"), list):
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
        rows[identifier] = row
    return {
        "volumes": volumes,
        "containers": rows,
        "services": service_rows,
        "swarm": swarm,
        "selected": selected,
        "project_exists": project_root.exists(),
        "docker_root": str(root),
    }


def first_empty_storage_program() -> str:
    from control_plane.testing_application_capabilities import single_local_swarm_node

    return (
        "import base64,hashlib,json,re,subprocess,sys\nfrom pathlib import Path\n"
        "from typing import Any\nfrom collections.abc import Callable\n"
        + inspect.getsource(docker_daemon_authorization_denied)
        + "\n"
        + inspect.getsource(single_local_swarm_node)
        + "\n"
        + inspect.getsource(first_empty_storage_snapshot)
        + """
def docker(*arguments):
    result = subprocess.run(['docker', *arguments], capture_output=True, text=True, timeout=8)
    if result.returncode:
        if docker_daemon_authorization_denied(result.stderr):
            raise PermissionError('Docker read refused')
        raise ValueError('Docker read unavailable')
    if len(result.stdout) > 8 * 1024 * 1024:
        raise ValueError('Docker inventory exceeds bound')
    return result.stdout
def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
try:
    spec = json.loads(base64.b64decode(sys.argv[1], validate=True))
    before = first_empty_storage_snapshot(docker, spec, compose_root=Path('/etc/dokploy/compose'), read_swarm=single_local_swarm_node)
    after = first_empty_storage_snapshot(docker, spec, compose_root=Path('/etc/dokploy/compose'), read_swarm=single_local_swarm_node)
    if before != after:
        raise ValueError('Storage changed during observation')
    result = {'type':'complete', 'binding':digest(spec), 'storage':digest(before['selected'])}
except PermissionError:
    result = {'type':'denied'}
except Exception:
    result = {'type':'complete', 'status':'unknown'}
print(sys.argv[2] + json.dumps(result), flush=True)
"""
    )
