from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
import inspect
import hashlib
import json
import click
from pathlib import Path
from typing import Any

from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.release_review import AbsentProduction, ReleaseReviewDecisionRecord
from control_plane.contracts.odoo_prod_rollback_operation import OdooProdRollbackResult
from control_plane.contracts.odoo_target_replacement_failures import (
    OdooProviderEffectUncertainError,
)
from control_plane.dokploy import api, source, native_copy_stream
from control_plane.dokploy.compose import DEFAULT_DATA_WORKFLOW_SSH_DIR
from control_plane.odoo_first_empty_inputs import (
    qualify_first_empty_storage,
    resolve_first_empty_authority,
    require_first_release_history,
    _provider_projection,
    first_release_custody,
    first_empty_volume_names,
    FirstEmptyAuthority,
    FIRST_RELEASE_STOPPED_KEY as FIRST_RELEASE_STOPPED_KEY,
    FIRST_RELEASE_ABSENCE_SOURCE as FIRST_RELEASE_ABSENCE_SOURCE,
)
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_application_capabilities import single_local_swarm_node
from control_plane.workflows.odoo_prod_backup_gate import (
    OdooProdBackupGateRequest,
    OdooProdBackupGateResult,
)
from control_plane.workflows.ship import utc_now_timestamp


def first_release_is_stopped(store: object, context: str, *, product: str) -> bool:
    if not isinstance(store, PostgresRecordStore):
        return False
    try:
        inventory = store.read_environment_inventory(context_name=context, instance_name="prod")
    except FileNotFoundError:
        return False
    marker = inventory.post_deploy_update.evidence.get(FIRST_RELEASE_STOPPED_KEY)
    if not marker:
        return False
    custody = first_release_custody(store, product, context)
    if (
        marker not in custody
        or inventory.artifact_identity is not None
        or inventory.runtime_identity is not None
    ):
        raise ValueError("The stopped initial-release custody is inconsistent.")
    require_first_release_history(store, None, context, product=product)
    return (
        inventory.artifact_identity is None
        and inventory.runtime_identity is None
        and inventory.deploy.status == "skipped"
        and not inventory.post_deploy_update.evidence.get("first_empty_restored_empty")
    )


def first_release_production(
    store: PostgresRecordStore, product: str, context: str
) -> AbsentProduction:
    from control_plane.release_review import ReleaseEvidenceUnavailable

    try:
        return _first_release_production(store, product, context)
    except (FileNotFoundError, ValueError, click.ClickException) as error:
        raise ReleaseEvidenceUnavailable("production_settings_unavailable") from error


def first_release_has_retained_stack(store: object, context: str, *, product: str) -> bool:
    if not first_release_is_stopped(store, context, product=product):
        return False
    assert isinstance(store, PostgresRecordStore)
    inventory = store.read_environment_inventory(context_name=context, instance_name="prod")
    containers = json.loads(
        inventory.post_deploy_update.evidence["first_empty_retained_containers"]
    )
    if not isinstance(containers, list) or any(
        not isinstance(identifier, str) or not identifier for identifier in containers
    ):
        raise ValueError("The retained original runtime witness is incomplete.")
    return bool(containers)


def _first_release_production(
    store: PostgresRecordStore, product: str, context: str
) -> AbsentProduction:
    authority = resolve_first_empty_authority(store, product, context, allow_release_history=True)
    from control_plane.workflows.odoo_stable_target_replacement import (
        _resolve_recorded_runtime_values,
    )
    from control_plane.workflows.odoo_prod_backup_gate import odoo_backup_paths_from_runtime

    runtime = _resolve_recorded_runtime_values(record_store=store, context=context, instance="prod")
    paths = odoo_backup_paths_from_runtime(runtime_values=runtime, backup_record_id="first-release")
    return AbsentProduction(
        authority_sha256=authority.fingerprint(), recovery_backup_root=paths["backup_root"]
    )


def first_release_backup_runtime(
    store: object, decision: ReleaseReviewDecisionRecord | None, context: str
) -> dict[str, str] | None:
    if (
        not isinstance(store, PostgresRecordStore)
        or decision is None
        or not isinstance(decision.checklist.production, AbsentProduction)
    ):
        return None
    from control_plane.workflows.odoo_stable_target_replacement import (
        _resolve_recorded_runtime_values,
    )

    current = first_release_production(store, decision.product, context)
    if current != decision.checklist.production:
        raise ValueError("The accepted first-release recovery inputs changed.")
    runtime = _resolve_recorded_runtime_values(record_store=store, context=context, instance="prod")
    return runtime


def capture_first_release_absence(
    store: object,
    root: Path,
    decision: ReleaseReviewDecisionRecord | None,
    request: OdooProdBackupGateRequest,
    *,
    operation_id: str,
    operation_lease_owner: str,
) -> OdooProdBackupGateResult:
    if not isinstance(store, PostgresRecordStore) or decision is None:
        raise ValueError(
            "An original-absence recovery witness requires its durable accepted release."
        )
    operation = store.read_odoo_prod_promotion_operation_record(operation_id)
    if (
        operation.authorization.release_decision_record_id != decision.record_id
        or operation.status != "running"
        or operation.lease_owner != operation_lease_owner
        or not operation_lease_owner
        or datetime.fromisoformat(operation.lease_expires_at) <= datetime.now(UTC)
    ):
        raise ValueError(
            "Original-absence recovery proof requires the original current operation lease."
        )
    qualified = qualify_first_empty_storage(
        store,
        root=root,
        product=decision.product,
        context=request.context,
        intent="replacement",
        release_decision=decision,
    )
    store.write_backup_gate_record(
        BackupGateRecord(
            record_id=request.backup_record_id,
            context=request.context,
            instance="prod",
            created_at=utc_now_timestamp(),
            source=FIRST_RELEASE_ABSENCE_SOURCE,
            status="pass",
            evidence={
                "recovery_kind": "original_absence",
                "release_decision_record_id": decision.record_id,
                "operation_id": operation_id,
                "plan_sha256": qualified.plan_sha256,
                "storage_sha256": qualified.storage_sha256,
                "authority_sha256": qualified.authority.fingerprint(),
                "original_provider_sha256": nonvalidating_compact_sorted_json_sha256(
                    qualified.provider_spec
                ),
                "infrastructure_backup_record_id": operation.request.infrastructure_backup_record_id,
            },
        )
    )
    return OdooProdBackupGateResult(
        context=request.context,
        instance="prod",
        backup_record_id=request.backup_record_id,
        backup_status="pass",
    )


def park_first_release_web(docker: Callable[..., str], spec: dict[str, Any]) -> dict[str, Any]:
    swarm = json.loads(docker("info", "--format", "{{json .Swarm}}"))
    if not isinstance(swarm, dict) or swarm.get("LocalNodeState") not in {"active", "inactive"}:
        raise ValueError("The original engine scope is unavailable.")
    nodes = single_local_swarm_node(docker) if swarm["LocalNodeState"] == "active" else []
    identifiers = docker("ps", "-aq", "--no-trunc").splitlines()
    if len(identifiers) > 10000 or len(set(identifiers)) != len(identifiers):
        raise ValueError("Container inventory is incomplete.")
    rows = json.loads(docker("inspect", *identifiers)) if identifiers else []
    if not isinstance(rows, list) or {row.get("Id") for row in rows} != set(identifiers):
        raise ValueError("Container ownership is incomplete.")
    project = spec["project"]
    volumes = set(spec["volumes"])
    volume_names = docker("volume", "ls", "--quiet").splitlines()
    if len(volume_names) > 10000 or len(set(volume_names)) != len(volume_names):
        raise ValueError("Retained volume inventory is incomplete.")
    retained_volumes = volumes.intersection(volume_names)

    def volume_snapshot() -> dict[str, dict[str, Any]]:
        selected = (
            json.loads(docker("volume", "inspect", *sorted(retained_volumes)))
            if retained_volumes
            else []
        )
        if (
            not isinstance(selected, list)
            or {row.get("Name") for row in selected} != retained_volumes
        ):
            raise ValueError("Retained volume identity is incomplete.")
        if any(
            row.get("Driver") != "local"
            or row.get("Options") not in (None, {})
            or not isinstance(row.get("Mountpoint"), str)
            or not row["Mountpoint"].startswith("/")
            for row in selected
        ):
            raise ValueError("Retained storage ownership is unverified.")
        return {
            row["Name"]: {
                key: row.get(key)
                for key in ("Name", "Driver", "Options", "Mountpoint", "CreatedAt", "Labels")
            }
            for row in selected
        }

    storage = volume_snapshot()
    mountpoints = [row["Mountpoint"].rstrip("/") for row in storage.values()]

    def uses_retained_storage(mount: dict[str, Any]) -> bool:
        source_path = str(mount.get("Source") or "")
        return mount.get("Name") in volumes or any(
            source_path == point or source_path.startswith(point + "/") for point in mountpoints
        )

    if nodes:
        services = docker("service", "ls", "--quiet").splitlines()
        if len(services) > 10000 or len(services) != len(set(services)):
            raise ValueError("Service inventory is incomplete.")
        service_rows = json.loads(docker("service", "inspect", *services)) if services else []
        if not isinstance(service_rows, list) or {row.get("ID") for row in service_rows} != set(
            services
        ):
            raise ValueError("Service ownership is incomplete.")
        for row in service_rows:
            service = row["Spec"]
            if (
                (service.get("Labels") or {}).get("com.docker.stack.namespace") == project
                or str(service.get("Name", "")).startswith(project + "_")
                or any(
                    mount.get("Source") in volumes or uses_retained_storage(mount)
                    for mount in service["TaskTemplate"]["ContainerSpec"].get("Mounts", [])
                )
            ):
                raise ValueError("A service retains the original target.")
    owned = []
    for row in rows:
        labels = row["Config"].get("Labels") or {}
        ours = labels.get("com.docker.compose.project") == project
        mounts = row["Mounts"]
        if not isinstance(mounts, list):
            raise ValueError("Container storage is incomplete.")
        if not ours:
            if any(uses_retained_storage(mount) for mount in mounts):
                raise ValueError("Another container holds original storage.")
            continue
        service = labels.get("com.docker.compose.service")
        if service not in {"web", "script-runner", "database"}:
            raise ValueError("Unreviewed service owns original storage.")
        if any(
            not (
                mount.get("Type") == "volume"
                and mount.get("Name") in volumes
                or service == "script-runner"
                and mount.get("Type") == "bind"
                and mount.get("Source") == spec.get("workflow_ssh_source")
                and mount.get("Destination") in {"/root/.ssh", "/home/ubuntu/.ssh"}
                and mount.get("RW") is False
            )
            for mount in mounts
            if mount.get("Type") != "tmpfs"
        ):
            raise ValueError("Unreviewed storage is mounted by the release.")
        if any(
            mount.get("Type") == "volume" and mount.get("Name") not in retained_volumes
            for mount in mounts
        ):
            raise ValueError("Mounted storage is missing from the retained inventory.")
        if service in {"web", "script-runner"}:
            environment = dict(item.split("=", 1) for item in row["Config"]["Env"] if "=" in item)
            deployment = spec["deployments"].get(
                environment.get("LAUNCHPLANE_DEPLOYMENT_RECORD_ID")
            )
            if deployment is None:
                raise ValueError("A foreign deployment owns the current service.")
            if (
                environment.get("LAUNCHPLANE_ARTIFACT_ID") != deployment["artifact"]
                or row["Config"].get("Image") != deployment["image"]
            ):
                raise ValueError("A different artifact owns the current service.")
        if service == "script-runner" and row["Config"].get("Cmd") != ["tail", "-f", "/dev/null"]:
            raise ValueError("The retained script runner is not idle.")
        owned.append(row)
    webs = [row for row in owned if row["Config"]["Labels"]["com.docker.compose.service"] == "web"]
    if len(webs) > 1:
        raise ValueError("Web ownership is ambiguous.")
    for web in webs:
        docker("update", "--restart=no", web["Id"])
        if web["State"]["Running"]:
            docker("stop", "--time", "30", web["Id"])
        after = json.loads(docker("inspect", web["Id"]))
        if (
            len(after) != 1
            or after[0]["Id"] != web["Id"]
            or after[0]["State"]["Running"] is not False
            or after[0]["HostConfig"]["RestartPolicy"]["Name"] != "no"
        ):
            raise ValueError("The original web is not positively stopped.")
    current = docker("ps", "-aq", "--no-trunc").splitlines()
    if set(current) != set(identifiers):
        raise ValueError("Container ownership changed during parking.")
    if nodes and single_local_swarm_node(docker) != nodes:
        raise ValueError("The original engine scope changed during parking.")
    if volume_snapshot() != storage:
        raise ValueError("Retained storage identity changed during parking.")
    return {
        "web_stopped": True,
        "retained_volumes": sorted(retained_volumes),
        "retained_containers": [row["Id"] for row in owned],
        "retained_storage_sha256": hashlib.sha256(
            json.dumps(storage, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest(),
    }


def first_release_park_program() -> str:
    return (
        "import base64,hashlib,json,subprocess,sys\nfrom typing import Any\nfrom collections.abc import Callable\n"
        + inspect.getsource(single_local_swarm_node)
        + "\n"
        + inspect.getsource(park_first_release_web)
        + """
def docker(*args):
    result = subprocess.run(['docker','--host','unix:///var/run/docker.sock',*args], capture_output=True, timeout=45)
    if result.returncode or len(result.stdout)>8*1024*1024:
        raise ValueError('Native release parking is unavailable')
    return result.stdout.decode()
spec=json.loads(base64.b64decode(sys.argv[1],validate=True))
result=park_first_release_web(docker,spec)
binding=hashlib.sha256(json.dumps(spec,sort_keys=True,separators=(',',':')).encode()).hexdigest()
print(sys.argv[2]+json.dumps({'type':'complete','binding':binding,'result':result}),flush=True)
"""
    )


def require_first_release_placement(
    store: PostgresRecordStore,
    decision: ReleaseReviewDecisionRecord,
    context: str,
    payload: api.JsonObject,
    *,
    authority: FirstEmptyAuthority | None = None,
) -> None:
    authority = authority or resolve_first_empty_authority(
        store, decision.product, context, release_decision=decision
    )
    custody = first_release_custody(store, decision.product, context, decision)
    environment, compose_source = payload.get("env"), payload.get("composeFile")
    projection = _provider_projection(
        payload,
        authority,
        staged_environment=environment if isinstance(environment, str) else None,
        staged_compose=compose_source if isinstance(compose_source, str) else None,
    )
    gates = [
        gate
        for gate in store.list_backup_gate_records(context_name=context, instance_name="prod")
        if gate.source == FIRST_RELEASE_ABSENCE_SOURCE
        and gate.status == "pass"
        and gate.evidence.get("release_decision_record_id") in custody
    ]
    expected = nonvalidating_compact_sorted_json_sha256(projection)
    if not gates or any(
        gate.evidence.get("original_provider_sha256") != expected for gate in gates
    ):
        raise ValueError("The initial release's original storage or physical placement changed.")


def first_release_rendered_inputs(
    *, host: str, token: str, payload: api.JsonObject
) -> dict[str, str]:
    from control_plane.dokploy.compose_inventory import read_cached_compose_source

    observed = read_cached_compose_source(
        host=host,
        token=token,
        server_id=native_copy_stream.require_registered_remote_server(payload.get("serverId")),
        request={
            "app_name": payload.get("appName"),
            "files": ["docker-compose.yml"],
            "hash_environment": True,
            "env_file": ".env",
        },
    )
    if observed.project_absent or not observed.environment_sha256:
        raise ValueError("The original executor's rendered configuration is unavailable.")
    return {
        "first_empty_rendered_env_sha256": observed.environment_sha256,
        "first_empty_rendered_compose_sha256": observed.files["docker-compose.yml"].sha256,
    }


def stop_first_release(
    store: object,
    root: Path,
    decision: ReleaseReviewDecisionRecord,
    context: str,
    checkpoint: Callable[[str], None] | None,
) -> OdooProdRollbackResult:
    if not isinstance(store, PostgresRecordStore) or checkpoint is None:
        raise ValueError("Original-absence rollback requires the existing durable release worker.")
    authority = resolve_first_empty_authority(
        store, decision.product, context, inverse_decision=decision
    )
    deployment_ids = require_first_release_history(store, decision, context)
    custody = first_release_custody(store, decision.product, context, decision)
    deployments = tuple(
        record
        for record in store.list_deployment_records(context_name=context, instance_name="prod")
        if record.runtime_source.get("first_empty_release_decision") in custody
    )
    if not deployments:
        raise ValueError("The original release has no retained deployment custody.")
    try:
        original_inventory = store.read_environment_inventory(
            context_name=context, instance_name="prod"
        )
    except FileNotFoundError:
        original_inventory = None
    host, token = source.read_dokploy_config(
        control_plane_root=root, database_url=store.database_url
    )
    payload = api.fetch_dokploy_target_payload(
        host=host, token=token, target_type="compose", target_id=authority.target_id.target_id
    )
    history = api.list_deployments_for_target(
        host=host,
        token=token,
        target_type="compose",
        target_id=authority.target_id.target_id,
        require_complete=True,
    )
    if any(
        not api.deployment_key(record)
        or api.deployment_key(record) not in deployment_ids
        or not api.deployment_is_terminal(record)
        for record in history
    ):
        raise ValueError("A foreign provider deployment protects this original target.")
    if (
        payload.get("composeId") != authority.target_id.target_id
        or payload.get("composeType", "docker-compose") != "docker-compose"
        or payload.get("autoDeploy") is not False
        or not isinstance(payload.get("appName"), str)
    ):
        raise ValueError("The original release's provider binding or automatic deployment changed.")
    environment = payload.get("env")
    compose_source = payload.get("composeFile")
    if not isinstance(environment, str) or not isinstance(compose_source, str):
        raise ValueError("The original provider inputs are unavailable.")
    require_first_release_placement(store, decision, context, payload, authority=authority)
    current_deployment = max(
        deployments, key=lambda record: (record.deploy.started_at, record.record_id)
    )
    for field, key in (
        ("env", "first_empty_provider_env_sha256"),
        ("composeFile", "first_empty_provider_compose_sha256"),
    ):
        expected = {
            record.runtime_source[key] for record in deployments if record.runtime_source.get(key)
        }
        if (
            not expected
            or hashlib.sha256(str(payload.get(field) or "").encode()).hexdigest() not in expected
        ):
            raise ValueError("The first release's staged provider input changed.")
    retained_volumes = list(first_empty_volume_names(authority.volumes))
    spec = {
        "project": payload["appName"],
        "volumes": retained_volumes,
        "deployments": {
            record.record_id: {
                "artifact": record.artifact_identity.artifact_id,
                "image": record.runtime_identity.image_reference,
            }
            for record in deployments
            if record.artifact_identity is not None and record.runtime_identity is not None
        },
        "workflow_ssh_source": api.parse_dokploy_env_text(environment).get("DATA_WORKFLOW_SSH_DIR")
        or DEFAULT_DATA_WORKFLOW_SSH_DIR,
    }
    frames: list[dict[str, Any]] = []
    record_store = store

    def recheck_inverse() -> None:
        current = resolve_first_empty_authority(
            record_store, decision.product, context, inverse_decision=decision
        )
        current_payload = api.fetch_dokploy_target_payload(
            host=host, token=token, target_type="compose", target_id=current.target_id.target_id
        )
        require_first_release_placement(
            record_store, decision, context, current_payload, authority=current
        )
        if (
            current_payload.get("env") != environment
            or current_payload.get("composeFile") != compose_source
        ):
            raise ValueError("The original inverse's owned provider inputs changed.")
        owned = require_first_release_history(record_store, decision, context)
        if any(
            api.deployment_key(row) not in owned or not api.deployment_is_terminal(row)
            for row in api.list_deployments_for_target(
                host=host,
                token=token,
                target_type="compose",
                target_id=current.target_id.target_id,
                require_complete=True,
            )
        ):
            raise ValueError("An unowned or unfinished provider deployment protects the inverse.")

    checkpoint("first_release_web_park")
    try:

        def dispatch_guard(dispatch: Callable[[], None]) -> None:
            recheck_inverse()
            from control_plane.workflows.odoo_stable_target_replacement import (
                TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE,
            )

            checkpoint(TARGET_REPLACEMENT_FIRST_PROVIDER_WRITE)
            dispatch()

        native_copy_stream.run_managed_server_program(
            host=host,
            token=token,
            server_id=native_copy_stream.require_registered_remote_server(payload.get("serverId")),
            program=first_release_park_program(),
            payload=spec,
            receive=frames.append,
            timeout_seconds=90,
            dispatch_guard=dispatch_guard,
        )
        if (
            len(frames) != 1
            or frames[0].get("type") != "complete"
            or frames[0].get("binding") != nonvalidating_compact_sorted_json_sha256(spec)
            or frames[0].get("result", {}).get("web_stopped") is not True
            or not isinstance(frames[0]["result"].get("retained_volumes"), list)
            or not set(frames[0]["result"]["retained_volumes"]) <= set(retained_volumes)
            or not isinstance(frames[0]["result"].get("retained_containers"), list)
            or any(
                not isinstance(identifier, str) or not identifier
                for identifier in frames[0]["result"]["retained_containers"]
            )
            or not isinstance(frames[0]["result"].get("retained_storage_sha256"), str)
            or len(frames[0]["result"]["retained_storage_sha256"]) != 64
        ):
            raise ValueError("The original release's stopped/retained witness is incomplete.")
        recheck_inverse()
    except (ValueError, OSError, RuntimeError, KeyError, TypeError, AttributeError) as error:
        raise OdooProviderEffectUncertainError(
            "Original release parking requires reconciliation."
        ) from error
    from control_plane.workflows.inventory import build_environment_inventory

    inventory = original_inventory or build_environment_inventory(
        deployment_record=current_deployment, updated_at=utc_now_timestamp()
    )
    parked = inventory.model_copy(
        update={
            "artifact_identity": None,
            "runtime_identity": None,
            "destination_health": inventory.destination_health.model_copy(
                update={
                    "status": "skipped",
                    "verified": False,
                    "runtime_identity_status": "unchecked",
                    "observed_runtime_identity": None,
                }
            ),
            "deploy": inventory.deploy.model_copy(update={"status": "skipped"}),
            "post_deploy_update": inventory.post_deploy_update.model_copy(
                update={
                    "evidence": {
                        **inventory.post_deploy_update.evidence,
                        FIRST_RELEASE_STOPPED_KEY: decision.record_id,
                        "first_empty_retained_volumes": json.dumps(
                            frames[0]["result"]["retained_volumes"]
                        ),
                        "first_empty_retained_containers": json.dumps(
                            frames[0]["result"]["retained_containers"]
                        ),
                        "first_empty_retained_storage_sha256": frames[0]["result"][
                            "retained_storage_sha256"
                        ],
                        "first_empty_release_operation": current_deployment.runtime_source[
                            "first_empty_release_operation"
                        ],
                    }
                }
            ),
            "updated_at": utc_now_timestamp(),
        }
    )
    try:
        if not store.compare_and_write_environment_inventory(
            expected_record=original_inventory, replacement_record=parked
        ):
            raise ValueError("Current production publication changed while parking.")
    except Exception as error:
        raise OdooProviderEffectUncertainError(
            "The stopped release's publication needs reconciliation."
        ) from error
    return OdooProdRollbackResult(
        context=context,
        instance="prod",
        source_channel="original-absence-storage-retained",
        artifact_id="",
        promotion_record_id=inventory.promotion_record_id,
        rollback_status="pass",
    )
