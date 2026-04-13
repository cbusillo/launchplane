import json
import subprocess
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.promotion_record import (
    BackupGateEvidence,
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
    PromotionRecord,
    PromotionRequest,
    ReleaseStatus,
)
from control_plane.contracts.ship_request import ShipRequest
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.ui import (
    render_artifact_manifest_dashboard,
    render_backup_gate_record_dashboard,
    render_deployment_record_dashboard,
    render_environment_contract_dashboard,
    render_environment_status_dashboard,
    render_inventory_overview_dashboard,
    render_operator_site_index,
    render_promotion_record_dashboard,
)
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.promote import (
    build_executed_promotion_record,
    build_promotion_record,
    generate_promotion_record_id,
)
from control_plane.workflows.ship import (
    build_deployment_record,
    generate_deployment_record_id,
    utc_now_timestamp,
)

ARTIFACT_IMAGE_REFERENCE_ENV_KEY = "DOCKER_IMAGE_REFERENCE"
DEFAULT_DOKPLOY_SHIP_SOURCE_GIT_REF = "origin/main"
ENVIRONMENT_STATUS_HISTORY_LIMIT = 3


def _store(state_dir: Path) -> FilesystemRecordStore:
    return FilesystemRecordStore(state_dir=state_dir)


def _control_plane_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _load_json_file(input_file: Path) -> dict[str, object]:
    return json.loads(input_file.read_text(encoding="utf-8"))


def _wait_for_ship_healthcheck(*, url: str, timeout_seconds: int) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error: str = ""
    while time.monotonic() < deadline:
        try:
            request = Request(url, method="GET")
            with urlopen(request, timeout=min(5, timeout_seconds)) as response:
                if 200 <= response.status < 300:
                    return
                last_error = f"http {response.status}"
        except HTTPError as error:
            last_error = f"http {error.code}"
        except URLError as error:
            last_error = str(error.reason)
        time.sleep(1)
    raise click.ClickException(f"Healthcheck failed for {url}: {last_error or 'timeout'}")


def _verify_ship_healthchecks(*, request: ShipRequest) -> None:
    if not request.wait or not request.verify_health:
        return
    if not request.destination_health.urls:
        source_file = control_plane_dokploy.resolve_control_plane_dokploy_source_file(
            _control_plane_root()
        )
        raise click.ClickException(
            "Healthcheck verification requested but no target domain/URL was resolved. "
            f"Define domains or ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL in {source_file} or disable with --no-verify-health."
        )
    if request.destination_health.timeout_seconds is None:
        raise click.ClickException("Healthcheck verification requested without timeout_seconds.")
    for healthcheck_url in request.destination_health.urls:
        _wait_for_ship_healthcheck(
            url=healthcheck_url, timeout_seconds=request.destination_health.timeout_seconds
        )


def _resolve_dokploy_target(
    *,
    request: ShipRequest,
) -> tuple[ResolvedTargetEvidence, int]:
    control_plane_root = _control_plane_root()
    source_file = control_plane_dokploy.resolve_control_plane_dokploy_source_file(
        control_plane_root
    )
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=request.context,
        instance_name=request.instance,
    )
    if target_definition is None:
        raise click.ClickException(
            f"No Dokploy target definition found for {request.context}/{request.instance} in {source_file}."
        )
    if target_definition.target_type != request.target_type:
        raise click.ClickException(
            f"Ship request target_type does not match {source_file}. "
            f"Request={request.target_type} configured={target_definition.target_type}."
        )
    resolved_target = ResolvedTargetEvidence(
        target_type=target_definition.target_type,
        target_id=target_definition.target_id,
        target_name=target_definition.target_name.strip() or request.target_name,
    )
    deploy_timeout_seconds = control_plane_dokploy.resolve_ship_timeout_seconds(
        timeout_override_seconds=request.timeout_seconds,
        target_definition=target_definition,
    )
    return resolved_target, deploy_timeout_seconds


def _resolve_deploy_mode(*, configured_ship_mode: str, target_type: str) -> str:
    if configured_ship_mode == "auto":
        return f"dokploy-{target_type}-api"
    return f"dokploy-{configured_ship_mode}-api"


def _load_control_plane_environment_values() -> dict[str, str]:
    return control_plane_dokploy.read_control_plane_environment_values(
        control_plane_root=_control_plane_root(),
    )


def _require_dokploy_target_definition(
    *,
    source_file: Path,
    source_of_truth: control_plane_dokploy.DokploySourceOfTruth,
    context_name: str,
    instance_name: str,
    operation_name: str,
) -> control_plane_dokploy.DokployTargetDefinition:
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=context_name,
        instance_name=instance_name,
    )
    if target_definition is None:
        raise click.ClickException(
            f"{operation_name} target {context_name}/{instance_name} is missing from {source_file}."
        )
    return target_definition


def _resolve_native_ship_request(
    *,
    context_name: str,
    instance_name: str,
    artifact_id: str,
    source_git_ref: str,
    wait: bool,
    timeout_override_seconds: int | None,
    verify_health: bool,
    health_timeout_override_seconds: int | None,
    dry_run: bool,
    no_cache: bool,
    allow_dirty: bool,
) -> ShipRequest:
    normalized_artifact_id = artifact_id.strip()
    if not normalized_artifact_id:
        raise click.ClickException("ship request requires artifact_id")

    control_plane_root = _control_plane_root()
    source_file = control_plane_dokploy.resolve_control_plane_dokploy_source_file(
        control_plane_root
    )
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = _require_dokploy_target_definition(
        source_file=source_file,
        source_of_truth=source_of_truth,
        context_name=context_name,
        instance_name=instance_name,
        operation_name="Ship",
    )

    environment_values = _load_control_plane_environment_values()
    resolved_source_git_ref = (
        source_git_ref.strip()
        or target_definition.source_git_ref.strip()
        or DEFAULT_DOKPLOY_SHIP_SOURCE_GIT_REF
    )
    destination_health_timeout_seconds = control_plane_dokploy.resolve_ship_health_timeout_seconds(
        health_timeout_override_seconds=health_timeout_override_seconds,
        target_definition=target_definition,
    )
    destination_healthcheck_urls = control_plane_dokploy.resolve_ship_healthcheck_urls(
        target_definition=target_definition,
        environment_values=environment_values,
    )
    should_verify_health = verify_health and wait
    if should_verify_health and not destination_healthcheck_urls:
        raise click.ClickException(
            "Healthcheck verification requested but no target domain/URL was resolved. "
            f"Define domains or ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL in {source_file} or disable with --no-verify-health."
        )

    configured_ship_mode = control_plane_dokploy.resolve_dokploy_ship_mode(
        context_name,
        instance_name,
        environment_values,
    )
    deploy_mode = _resolve_deploy_mode(
        configured_ship_mode=configured_ship_mode,
        target_type=target_definition.target_type,
    )

    try:
        return ShipRequest(
            artifact_id=normalized_artifact_id,
            context=context_name,
            instance=instance_name,
            source_git_ref=resolved_source_git_ref,
            target_name=target_definition.target_name.strip() or f"{context_name}-{instance_name}",
            target_type=target_definition.target_type,
            deploy_mode=deploy_mode,
            wait=wait,
            timeout_seconds=timeout_override_seconds,
            verify_health=should_verify_health,
            health_timeout_seconds=destination_health_timeout_seconds,
            dry_run=dry_run,
            no_cache=no_cache,
            allow_dirty=allow_dirty,
            destination_health=HealthcheckEvidence(
                urls=destination_healthcheck_urls,
                timeout_seconds=destination_health_timeout_seconds,
                status="pending" if should_verify_health else "skipped",
            ),
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error


def _resolve_ship_request_for_promotion(
    *,
    request: PromotionRequest,
) -> ShipRequest:
    ship_request = _resolve_native_ship_request(
        context_name=request.context,
        instance_name=request.to_instance,
        artifact_id=request.artifact_id,
        source_git_ref=request.source_git_ref,
        wait=request.wait,
        timeout_override_seconds=request.timeout_seconds,
        verify_health=request.verify_health,
        health_timeout_override_seconds=request.health_timeout_seconds,
        dry_run=request.dry_run,
        no_cache=request.no_cache,
        allow_dirty=request.allow_dirty,
    )
    if request.target_type != ship_request.target_type:
        raise click.ClickException(
            "Promotion request target_type does not match control-plane Dokploy source-of-truth. "
            f"Request={request.target_type} configured={ship_request.target_type}."
        )
    if request.target_name != ship_request.target_name:
        raise click.ClickException(
            "Promotion request target_name does not match control-plane Dokploy source-of-truth. "
            f"Request={request.target_name} configured={ship_request.target_name}."
        )
    if request.deploy_mode != ship_request.deploy_mode:
        raise click.ClickException(
            "Promotion request deploy_mode does not match resolved Dokploy ship mode. "
            f"Request={request.deploy_mode} configured={ship_request.deploy_mode}."
        )
    return ship_request


def _resolve_native_promotion_request(
    *,
    context_name: str,
    from_instance_name: str,
    to_instance_name: str,
    artifact_id: str,
    backup_record_id: str,
    source_git_ref: str,
    wait: bool,
    timeout_override_seconds: int | None,
    verify_health: bool,
    health_timeout_override_seconds: int | None,
    dry_run: bool,
    no_cache: bool,
    allow_dirty: bool,
) -> PromotionRequest:
    normalized_artifact_id = artifact_id.strip()
    if not normalized_artifact_id:
        raise click.ClickException("promotion request requires artifact_id")
    normalized_backup_record_id = backup_record_id.strip()
    if not normalized_backup_record_id:
        raise click.ClickException("promotion request requires backup_record_id")

    control_plane_root = _control_plane_root()
    source_file = control_plane_dokploy.resolve_control_plane_dokploy_source_file(
        control_plane_root
    )
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    source_target_definition = _require_dokploy_target_definition(
        source_file=source_file,
        source_of_truth=source_of_truth,
        context_name=context_name,
        instance_name=from_instance_name,
        operation_name="Promotion source",
    )
    destination_target_definition = _require_dokploy_target_definition(
        source_file=source_file,
        source_of_truth=source_of_truth,
        context_name=context_name,
        instance_name=to_instance_name,
        operation_name="Promotion destination",
    )

    environment_values = _load_control_plane_environment_values()
    resolved_source_git_ref = (
        source_git_ref.strip()
        or source_target_definition.source_git_ref.strip()
        or DEFAULT_DOKPLOY_SHIP_SOURCE_GIT_REF
    )
    source_health_timeout_seconds = control_plane_dokploy.resolve_ship_health_timeout_seconds(
        health_timeout_override_seconds=health_timeout_override_seconds,
        target_definition=source_target_definition,
    )
    source_healthcheck_urls = control_plane_dokploy.resolve_ship_healthcheck_urls(
        target_definition=source_target_definition,
        environment_values=environment_values,
    )
    source_health_status: ReleaseStatus = "pending" if source_healthcheck_urls else "skipped"
    destination_health_timeout_seconds = control_plane_dokploy.resolve_ship_health_timeout_seconds(
        health_timeout_override_seconds=health_timeout_override_seconds,
        target_definition=destination_target_definition,
    )
    destination_healthcheck_urls = control_plane_dokploy.resolve_ship_healthcheck_urls(
        target_definition=destination_target_definition,
        environment_values=environment_values,
    )
    should_verify_destination_health = verify_health and wait
    if should_verify_destination_health and not destination_healthcheck_urls:
        raise click.ClickException(
            "Healthcheck verification requested but no target domain/URL was resolved. "
            f"Define domains or ENV_OVERRIDE_CONFIG_PARAM__WEB__BASE__URL in {source_file} or disable with --no-verify-health."
        )
    configured_ship_mode = control_plane_dokploy.resolve_dokploy_ship_mode(
        context_name,
        to_instance_name,
        environment_values,
    )
    deploy_mode = _resolve_deploy_mode(
        configured_ship_mode=configured_ship_mode,
        target_type=destination_target_definition.target_type,
    )

    try:
        return PromotionRequest(
            artifact_id=normalized_artifact_id,
            backup_record_id=normalized_backup_record_id,
            source_git_ref=resolved_source_git_ref,
            context=context_name,
            from_instance=from_instance_name,
            to_instance=to_instance_name,
            target_name=destination_target_definition.target_name.strip()
            or f"{context_name}-{to_instance_name}",
            target_type=destination_target_definition.target_type,
            deploy_mode=deploy_mode,
            wait=wait,
            timeout_seconds=timeout_override_seconds,
            verify_health=should_verify_destination_health,
            health_timeout_seconds=destination_health_timeout_seconds,
            dry_run=dry_run,
            no_cache=no_cache,
            allow_dirty=allow_dirty,
            source_health=HealthcheckEvidence(
                urls=source_healthcheck_urls,
                timeout_seconds=source_health_timeout_seconds,
                status=source_health_status,
            ),
            backup_gate=BackupGateEvidence(
                status="pass",
                evidence={"backup_record_id": normalized_backup_record_id},
            ),
            destination_health=HealthcheckEvidence(
                urls=destination_healthcheck_urls,
                timeout_seconds=destination_health_timeout_seconds,
                status="pending" if should_verify_destination_health else "skipped",
            ),
        )
    except ValueError as error:
        raise click.ClickException(str(error)) from error


def _execute_dokploy_deploy(
    *,
    request: ShipRequest,
    resolved_target: ResolvedTargetEvidence,
    deploy_timeout_seconds: int,
) -> None:
    control_plane_root = _control_plane_root()
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    latest_before = None
    if request.wait:
        latest_before = control_plane_dokploy.latest_deployment_for_target(
            host=host,
            token=token,
            target_type=resolved_target.target_type,
            target_id=resolved_target.target_id,
        )
    control_plane_dokploy.trigger_deployment(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        no_cache=request.no_cache,
    )
    if not request.wait:
        return
    control_plane_dokploy.wait_for_target_deployment(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        before_key=control_plane_dokploy.deployment_key(latest_before),
        timeout_seconds=deploy_timeout_seconds,
    )


def _run_compose_post_deploy_update(
    *,
    env_file: Path | None,
    request: ShipRequest,
) -> None:
    control_plane_root = _control_plane_root()
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = control_plane_dokploy.find_dokploy_target_definition(
        source_of_truth,
        context_name=request.context,
        instance_name=request.instance,
    )
    if target_definition is None:
        raise click.ClickException(
            f"Compose post-deploy update target {request.context}/{request.instance} is missing from the control-plane Dokploy source-of-truth."
        )
    if target_definition.target_type != "compose":
        raise click.ClickException(
            "Compose post-deploy update requires a compose target in the control-plane Dokploy source-of-truth. "
            f"Configured={target_definition.target_type}."
        )
    control_plane_dokploy.run_compose_post_deploy_update(
        host=host,
        token=token,
        target_definition=target_definition,
        env_file=env_file,
    )


def _skipped_destination_health(
    request: ShipRequest, *, detail_status: str = "skipped"
) -> HealthcheckEvidence:
    return request.destination_health.model_copy(
        update={"verified": False, "status": detail_status}
    )


def _execute_ship(
    *,
    state_dir: Path,
    env_file: Path | None,
    request: ShipRequest,
) -> tuple[Path | None, DeploymentRecord | ShipRequest]:
    record_store = _store(state_dir)
    resolved_artifact_id = _require_artifact_id(requested_artifact_id=request.artifact_id)
    artifact_manifest = _read_artifact_manifest(
        record_store=record_store,
        artifact_id=resolved_artifact_id,
    )
    resolved_request = _resolve_artifact_native_execution_request(
        request=request,
        artifact_id=resolved_artifact_id,
        artifact_manifest=artifact_manifest,
    )

    if resolved_request.dry_run:
        click.echo(json.dumps(resolved_request.model_dump(mode="json"), indent=2, sort_keys=True))
        return None, resolved_request

    record_id = generate_deployment_record_id(
        context_name=resolved_request.context,
        instance_name=resolved_request.instance,
    )
    started_at = utc_now_timestamp()
    pending_record = build_deployment_record(
        request=resolved_request,
        record_id=record_id,
        deployment_id="control-plane-dokploy",
        deployment_status="pending",
        started_at=started_at,
        finished_at="",
    )
    record_path = record_store.write_deployment_record(pending_record)

    try:
        resolved_target, deploy_timeout_seconds = _resolve_dokploy_target(
            request=resolved_request,
        )
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="fail",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
        )
        record_store.write_deployment_record(final_record)
        raise

    try:
        _sync_artifact_image_reference_for_target(
            artifact_manifest=artifact_manifest,
            resolved_target=resolved_target,
        )
        _execute_dokploy_deploy(
            request=resolved_request,
            resolved_target=resolved_target,
            deploy_timeout_seconds=deploy_timeout_seconds,
        )
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(request=resolved_request, record_id=record_id,
                                               deployment_id="control-plane-dokploy", deployment_status="fail",
                                               started_at=started_at, finished_at=utc_now_timestamp(),
                                               resolved_target=resolved_target)
        record_store.write_deployment_record(final_record)
        raise

    try:
        if resolved_request.wait and resolved_target.target_type == "compose":
            _run_compose_post_deploy_update(
                env_file=env_file,
                request=resolved_request,
            )
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(request=resolved_request, record_id=record_id,
                                               deployment_id="control-plane-dokploy", deployment_status="pass",
                                               started_at=started_at, finished_at=utc_now_timestamp(),
                                               resolved_target=resolved_target,
                                               post_deploy_update=PostDeployUpdateEvidence(
                                                   attempted=True,
                                                   status="fail",
                                                   detail=(
                                                       "Odoo-specific post-deploy update failed through the native "
                                                       "control-plane Dokploy schedule workflow."
                                                   ),
                                               ), destination_health=_skipped_destination_health(resolved_request))
        record_store.write_deployment_record(final_record)
        raise

    post_deploy_update_evidence = PostDeployUpdateEvidence()
    if resolved_request.wait and resolved_target.target_type == "compose":
        post_deploy_update_evidence = PostDeployUpdateEvidence(
            attempted=True,
            status="pass",
            detail=(
                "Odoo-specific post-deploy update completed through the native "
                "control-plane Dokploy schedule workflow."
            ),
        )

    try:
        _verify_ship_healthchecks(request=resolved_request)
        final_record = build_deployment_record(request=resolved_request, record_id=record_id,
                                               deployment_id="control-plane-dokploy", deployment_status="pass",
                                               started_at=started_at, finished_at=utc_now_timestamp(),
                                               resolved_target=resolved_target,
                                               post_deploy_update=post_deploy_update_evidence)
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(request=resolved_request, record_id=record_id,
                                               deployment_id="control-plane-dokploy", deployment_status="pass",
                                               started_at=started_at, finished_at=utc_now_timestamp(),
                                               resolved_target=resolved_target,
                                               post_deploy_update=post_deploy_update_evidence,
                                               destination_health=_skipped_destination_health(resolved_request,
                                                                                              detail_status="fail"))
        record_store.write_deployment_record(final_record)
        raise

    record_store.write_deployment_record(final_record)
    if final_record.wait_for_completion and final_record.deploy.status == "pass":
        _write_environment_inventory(record_store=record_store, deployment_record=final_record)
    return record_path, final_record


def _require_artifact_id(*, requested_artifact_id: str) -> str:
    normalized_artifact_id = requested_artifact_id.strip()
    if not normalized_artifact_id:
        raise click.ClickException("Artifact-backed execution requires an explicit artifact_id.")
    return normalized_artifact_id


def _read_artifact_manifest(
    *,
    record_store: FilesystemRecordStore,
    artifact_id: str,
) -> ArtifactIdentityManifest:
    try:
        return record_store.read_artifact_manifest(artifact_id)
    except FileNotFoundError:
        raise click.ClickException(
            f"Ship requires stored artifact manifest '{artifact_id}'."
        ) from None


def _read_backup_gate_record(
    *,
    record_store: FilesystemRecordStore,
    record_id: str,
) -> BackupGateRecord:
    try:
        return record_store.read_backup_gate_record(record_id)
    except FileNotFoundError:
        raise click.ClickException(
            f"Promotion requires stored backup gate record '{record_id}'."
        ) from None


def _resolve_backup_gate_for_promotion(
    *,
    request: PromotionRequest,
    record_store: FilesystemRecordStore,
) -> tuple[PromotionRequest, BackupGateRecord | None]:
    if not request.backup_gate.required:
        resolved_request = request.model_copy(
            update={
                "backup_record_id": "",
                "backup_gate": {"required": False, "status": "skipped", "evidence": {}},
            }
        )
        return resolved_request, None

    normalized_record_id = request.backup_record_id.strip()
    if not normalized_record_id:
        raise click.ClickException(
            "Promotion requires backup_record_id when backup gate is required."
        )

    backup_gate_record = _read_backup_gate_record(
        record_store=record_store, record_id=normalized_record_id
    )
    if backup_gate_record.context != request.context:
        raise click.ClickException(
            "Backup gate record context does not match promotion request. "
            f"Record={backup_gate_record.context} request={request.context}."
        )
    if backup_gate_record.instance != request.to_instance:
        raise click.ClickException(
            "Backup gate record instance does not match promotion destination. "
            f"Record={backup_gate_record.instance} request={request.to_instance}."
        )
    if not backup_gate_record.required:
        raise click.ClickException(
            f"Backup gate record '{backup_gate_record.record_id}' is marked required=false and cannot satisfy promotion gating."
        )
    if backup_gate_record.status != "pass":
        raise click.ClickException(
            f"Backup gate record '{backup_gate_record.record_id}' must have status=pass before promotion."
        )

    resolved_request = request.model_copy(
        update={
            "backup_record_id": backup_gate_record.record_id,
            "backup_gate": {
                "required": backup_gate_record.required,
                "status": backup_gate_record.status,
                "evidence": backup_gate_record.evidence,
            },
        }
    )
    return resolved_request, backup_gate_record


def _resolve_artifact_native_execution_request(
    *,
    request: ShipRequest,
    artifact_id: str,
    artifact_manifest: ArtifactIdentityManifest,
) -> ShipRequest:
    if artifact_manifest.artifact_id != artifact_id:
        raise click.ClickException(
            "Artifact manifest id mismatch during ship execution: "
            f"request={artifact_id} manifest={artifact_manifest.artifact_id}."
        )
    return request.model_copy(update={"artifact_id": artifact_id})


def _artifact_image_reference_from_manifest(manifest: ArtifactIdentityManifest) -> str:
    return f"{manifest.image.repository}@{manifest.image.digest}"


def _sync_artifact_image_reference_for_target(
    *,
    artifact_manifest: ArtifactIdentityManifest | None,
    resolved_target: ResolvedTargetEvidence,
) -> None:
    control_plane_root = Path(__file__).resolve().parent.parent
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
    target_payload = control_plane_dokploy.fetch_dokploy_target_payload(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
    )
    env_map = control_plane_dokploy.parse_dokploy_env_text(str(target_payload.get("env") or ""))
    desired_image_reference = ""
    if artifact_manifest is not None:
        desired_image_reference = _artifact_image_reference_from_manifest(artifact_manifest)

    current_image_reference = env_map.get(ARTIFACT_IMAGE_REFERENCE_ENV_KEY, "")
    if current_image_reference == desired_image_reference:
        return

    if desired_image_reference:
        env_map[ARTIFACT_IMAGE_REFERENCE_ENV_KEY] = desired_image_reference
    else:
        env_map.pop(ARTIFACT_IMAGE_REFERENCE_ENV_KEY, None)

    control_plane_dokploy.update_dokploy_target_env(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        target_payload=target_payload,
        env_text=control_plane_dokploy.serialize_dokploy_env_text(env_map),
    )


def _write_environment_inventory(
    *,
    record_store: FilesystemRecordStore,
    deployment_record: DeploymentRecord,
    promotion_record_id: str = "",
    promoted_from_instance: str = "",
) -> Path:
    inventory_record = build_environment_inventory(
        deployment_record=deployment_record,
        updated_at=utc_now_timestamp(),
        promotion_record_id=promotion_record_id,
        promoted_from_instance=promoted_from_instance,
    )
    return record_store.write_environment_inventory(inventory_record)


def _artifact_id_or_empty(artifact_identity: object) -> str:
    if artifact_identity is None:
        return ""
    artifact_id = getattr(artifact_identity, "artifact_id", "")
    if isinstance(artifact_id, str):
        return artifact_id
    return ""


def _summarize_backup_gate_record(record: BackupGateRecord) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "context": record.context,
        "instance": record.instance,
        "created_at": record.created_at,
        "source": record.source,
        "required": record.required,
        "status": record.status,
        "evidence": dict(record.evidence),
    }


def _summarize_promotion_record(record: PromotionRecord) -> dict[str, object]:
    return {
        "record_id": record.record_id,
        "context": record.context,
        "from_instance": record.from_instance,
        "to_instance": record.to_instance,
        "artifact_id": _artifact_id_or_empty(record.artifact_identity),
        "backup_record_id": record.backup_record_id,
        "backup_status": record.backup_gate.status,
        "deploy_status": record.deploy.status,
        "deployment_id": record.deploy.deployment_id,
        "started_at": record.deploy.started_at,
        "finished_at": record.deploy.finished_at,
        "post_deploy_update_status": record.post_deploy_update.status,
        "source_health_status": record.source_health.status,
        "destination_health_status": record.destination_health.status,
    }


def _summarize_deployment_record(record: DeploymentRecord) -> dict[str, object]:
    target_id = ""
    if record.resolved_target is not None:
        target_id = record.resolved_target.target_id
    return {
        "record_id": record.record_id,
        "context": record.context,
        "instance": record.instance,
        "artifact_id": _artifact_id_or_empty(record.artifact_identity),
        "source_git_ref": record.source_git_ref,
        "target_name": record.deploy.target_name,
        "target_type": record.deploy.target_type,
        "target_id": target_id,
        "deploy_status": record.deploy.status,
        "deployment_id": record.deploy.deployment_id,
        "started_at": record.deploy.started_at,
        "finished_at": record.deploy.finished_at,
        "post_deploy_update_status": record.post_deploy_update.status,
        "destination_health_status": record.destination_health.status,
    }


def _summarize_environment_inventory(record: EnvironmentInventory) -> dict[str, object]:
    return {
        "context": record.context,
        "instance": record.instance,
        "artifact_id": _artifact_id_or_empty(record.artifact_identity),
        "source_git_ref": record.source_git_ref,
        "updated_at": record.updated_at,
        "deployment_record_id": record.deployment_record_id,
        "promotion_record_id": record.promotion_record_id,
        "promoted_from_instance": record.promoted_from_instance,
        "deploy_status": record.deploy.status,
        "post_deploy_update_status": record.post_deploy_update.status,
        "destination_health_status": record.destination_health.status,
    }


def _build_environment_status_payload(
    *,
    record_store: FilesystemRecordStore,
    context_name: str,
    instance_name: str,
) -> dict[str, object]:
    live_inventory = record_store.read_environment_inventory(
        context_name=context_name, instance_name=instance_name
    )
    live_promotion_summary: dict[str, object] | None = None
    authorized_backup_gate_summary: dict[str, object] | None = None

    if live_inventory.promotion_record_id.strip():
        try:
            live_promotion_record = record_store.read_promotion_record(
                live_inventory.promotion_record_id
            )
        except FileNotFoundError:
            raise click.ClickException(
                "Environment inventory references missing promotion record "
                f"'{live_inventory.promotion_record_id}'."
            ) from None
        live_promotion_summary = _summarize_promotion_record(live_promotion_record)
        if live_promotion_record.backup_record_id.strip():
            try:
                live_backup_gate_record = record_store.read_backup_gate_record(
                    live_promotion_record.backup_record_id
                )
            except FileNotFoundError:
                raise click.ClickException(
                    "Promotion record references missing backup gate record "
                    f"'{live_promotion_record.backup_record_id}'."
                ) from None
            authorized_backup_gate_summary = _summarize_backup_gate_record(live_backup_gate_record)

    recent_promotion_records = record_store.list_promotion_records(
        context_name=context_name,
        to_instance_name=instance_name,
        limit=ENVIRONMENT_STATUS_HISTORY_LIMIT,
    )
    recent_deployment_records = record_store.list_deployment_records(
        context_name=context_name,
        instance_name=instance_name,
        limit=ENVIRONMENT_STATUS_HISTORY_LIMIT,
    )
    recent_promotions = tuple(
        _summarize_promotion_record(record) for record in recent_promotion_records
    )
    recent_deployments = tuple(
        _summarize_deployment_record(record) for record in recent_deployment_records
    )
    latest_promotion = recent_promotions[0] if recent_promotions else None
    latest_deployment = recent_deployments[0] if recent_deployments else None

    return {
        "context": context_name,
        "instance": instance_name,
        "live": _summarize_environment_inventory(live_inventory),
        "live_promotion": live_promotion_summary,
        "authorized_backup_gate": authorized_backup_gate_summary,
        "latest_promotion": latest_promotion,
        "latest_deployment": latest_deployment,
        "recent_promotions": recent_promotions,
        "recent_deployments": recent_deployments,
    }


def _build_environment_overview_payloads(
    *,
    record_store: FilesystemRecordStore,
    context_name: str,
) -> list[dict[str, object]]:
    inventory_records = sorted(
        (
            record
            for record in record_store.list_environment_inventory()
            if not context_name or record.context == context_name
        ),
        key=lambda record: (record.context, record.instance),
    )
    return [
        _build_environment_status_payload(
            record_store=record_store,
            context_name=inventory_record.context,
            instance_name=inventory_record.instance,
        )
        for inventory_record in inventory_records
    ]


def _build_runtime_environment_rows(
    *,
    entries: dict[str, object],
    source_name: str,
) -> list[dict[str, object]]:
    return [
        {
            "key": key_name,
            "value": str(entries[key_name]),
            "source": source_name,
            "overrides": (),
        }
        for key_name in sorted(entries)
    ]


def _build_environment_contract_payload(*, context_name: str, instance_name: str) -> dict[str, object]:
    control_plane_root = _control_plane_root()
    definition = control_plane_runtime_environments.load_runtime_environment_definition(
        control_plane_root=control_plane_root
    )
    context_definition = definition.contexts.get(context_name)
    if context_definition is None:
        raise click.ClickException(
            f"Runtime environments file has no context definition for {context_name!r}."
        )
    instance_definition = context_definition.instances.get(instance_name)
    if instance_definition is None:
        raise click.ClickException(
            f"Runtime environments file has no instance definition for {context_name}/{instance_name}."
        )

    source_file = control_plane_runtime_environments.resolve_runtime_environments_file(control_plane_root)
    resolved_environment = control_plane_runtime_environments.resolve_runtime_environment_values(
        control_plane_root=control_plane_root,
        context_name=context_name,
        instance_name=instance_name,
    )

    source_history: dict[str, list[str]] = {}
    resolved_layers: dict[str, tuple[str, str]] = {}
    for layer_name, values in (
        ("global", definition.shared_env),
        ("context", context_definition.shared_env),
        ("instance", instance_definition.env),
    ):
        for key_name, raw_value in values.items():
            prior_sources = source_history.setdefault(key_name, [])
            if key_name in resolved_layers:
                prior_sources.append(resolved_layers[key_name][0])
            resolved_layers[key_name] = (layer_name, str(raw_value))

    resolved_rows = [
        {
            "key": key_name,
            "value": value,
            "source": source_name,
            "overrides": tuple(source_history.get(key_name, [])),
        }
        for key_name, (source_name, value) in sorted(resolved_layers.items())
    ]

    return {
        "schema_version": definition.schema_version,
        "source_file": str(source_file),
        "context": context_name,
        "instance": instance_name,
        "available_contexts": [
            {
                "context": name,
                "instance_count": len(context.instances),
            }
            for name, context in sorted(definition.contexts.items())
        ],
        "available_instances": sorted(context_definition.instances),
        "layer_summaries": (
            {
                "label": "Global shared",
                "count": len(definition.shared_env),
                "note": "Applies to every context.",
            },
            {
                "label": f"{context_name} shared",
                "count": len(context_definition.shared_env),
                "note": "Applies across the selected context.",
            },
            {
                "label": f"{instance_name} instance",
                "count": len(instance_definition.env),
                "note": "Applies only to the selected instance.",
            },
            {
                "label": "Resolved",
                "count": len(resolved_environment),
                "note": "Final merged environment consumed by downstream tools.",
            },
        ),
        "global_rows": _build_runtime_environment_rows(
            entries=definition.shared_env,
            source_name="global",
        ),
        "context_rows": _build_runtime_environment_rows(
            entries=context_definition.shared_env,
            source_name="context",
        ),
        "instance_rows": _build_runtime_environment_rows(
            entries=instance_definition.env,
            source_name="instance",
        ),
        "resolved_rows": resolved_rows,
    }


def _site_environment_status_file_name(*, context_name: str, instance_name: str) -> str:
    return f"{context_name}-{instance_name}-status.html"


def _site_environment_contract_file_name(*, context_name: str, instance_name: str) -> str:
    return f"{context_name}-{instance_name}-contract.html"


def _site_deployment_record_file_name(*, record_id: str) -> str:
    return f"{record_id}.html"


def _site_promotion_record_file_name(*, record_id: str) -> str:
    return f"{record_id}.html"


def _site_backup_gate_record_file_name(*, record_id: str) -> str:
    return f"{record_id}.html"


def _site_artifact_manifest_file_name(*, artifact_id: str) -> str:
    return f"{artifact_id}.html"


def _site_deployment_record_href(*, record_id: str, relative_prefix: str = "") -> str:
    if not record_id.strip():
        return ""
    return (
        f"{relative_prefix}records/deployments/"
        f"{_site_deployment_record_file_name(record_id=record_id)}"
    )


def _site_promotion_record_href(*, record_id: str, relative_prefix: str = "") -> str:
    if not record_id.strip():
        return ""
    return (
        f"{relative_prefix}records/promotions/"
        f"{_site_promotion_record_file_name(record_id=record_id)}"
    )


def _site_backup_gate_record_href(*, record_id: str, relative_prefix: str = "") -> str:
    if not record_id.strip():
        return ""
    return (
        f"{relative_prefix}records/backup-gates/"
        f"{_site_backup_gate_record_file_name(record_id=record_id)}"
    )


def _site_artifact_manifest_href(*, artifact_id: str, relative_prefix: str = "") -> str:
    if not artifact_id.strip():
        return ""
    return (
        f"{relative_prefix}records/artifacts/"
        f"{_site_artifact_manifest_file_name(artifact_id=artifact_id)}"
    )


def _with_site_record_links(
    payload: dict[str, object], *, relative_prefix: str
) -> dict[str, object]:
    linked_payload = dict(payload)

    def _link_promotion_summary(summary: dict[str, object]) -> dict[str, object]:
        linked_summary = dict(summary)
        linked_summary["artifact_href"] = _site_artifact_manifest_href(
            artifact_id=str(linked_summary.get("artifact_id", "")),
            relative_prefix=relative_prefix,
        )
        linked_summary["record_href"] = _site_promotion_record_href(
            record_id=str(linked_summary.get("record_id", "")),
            relative_prefix=relative_prefix,
        )
        linked_summary["backup_record_href"] = _site_backup_gate_record_href(
            record_id=str(linked_summary.get("backup_record_id", "")),
            relative_prefix=relative_prefix,
        )
        return linked_summary

    def _link_deployment_summary(summary: dict[str, object]) -> dict[str, object]:
        linked_summary = dict(summary)
        linked_summary["artifact_href"] = _site_artifact_manifest_href(
            artifact_id=str(linked_summary.get("artifact_id", "")),
            relative_prefix=relative_prefix,
        )
        linked_summary["record_href"] = _site_deployment_record_href(
            record_id=str(linked_summary.get("record_id", "")),
            relative_prefix=relative_prefix,
        )
        return linked_summary

    live_payload = payload.get("live")
    if isinstance(live_payload, dict):
        linked_live_payload = dict(live_payload)
        linked_live_payload["artifact_href"] = _site_artifact_manifest_href(
            artifact_id=str(linked_live_payload.get("artifact_id", "")),
            relative_prefix=relative_prefix,
        )
        linked_live_payload["deployment_record_href"] = _site_deployment_record_href(
            record_id=str(linked_live_payload.get("deployment_record_id", "")),
            relative_prefix=relative_prefix,
        )
        linked_live_payload["promotion_record_href"] = _site_promotion_record_href(
            record_id=str(linked_live_payload.get("promotion_record_id", "")),
            relative_prefix=relative_prefix,
        )
        linked_payload["live"] = linked_live_payload

    live_promotion_payload = payload.get("live_promotion")
    if isinstance(live_promotion_payload, dict):
        linked_live_promotion_payload = dict(live_promotion_payload)
        linked_live_promotion_payload["artifact_href"] = _site_artifact_manifest_href(
            artifact_id=str(linked_live_promotion_payload.get("artifact_id", "")),
            relative_prefix=relative_prefix,
        )
        linked_live_promotion_payload["record_href"] = _site_promotion_record_href(
            record_id=str(linked_live_promotion_payload.get("record_id", "")),
            relative_prefix=relative_prefix,
        )
        linked_live_promotion_payload["backup_record_href"] = _site_backup_gate_record_href(
            record_id=str(linked_live_promotion_payload.get("backup_record_id", "")),
            relative_prefix=relative_prefix,
        )
        linked_payload["live_promotion"] = linked_live_promotion_payload

    authorized_backup_gate_payload = payload.get("authorized_backup_gate")
    if isinstance(authorized_backup_gate_payload, dict):
        linked_backup_gate_payload = dict(authorized_backup_gate_payload)
        linked_backup_gate_payload["record_href"] = _site_backup_gate_record_href(
            record_id=str(linked_backup_gate_payload.get("record_id", "")),
            relative_prefix=relative_prefix,
        )
        linked_payload["authorized_backup_gate"] = linked_backup_gate_payload

    latest_promotion_payload = payload.get("latest_promotion")
    if isinstance(latest_promotion_payload, dict):
        linked_payload["latest_promotion"] = _link_promotion_summary(latest_promotion_payload)

    latest_deployment_payload = payload.get("latest_deployment")
    if isinstance(latest_deployment_payload, dict):
        linked_payload["latest_deployment"] = _link_deployment_summary(latest_deployment_payload)

    recent_promotions_payload = payload.get("recent_promotions")
    if isinstance(recent_promotions_payload, (list, tuple)):
        linked_payload["recent_promotions"] = tuple(
            _link_promotion_summary(item)
            for item in recent_promotions_payload
            if isinstance(item, dict)
        )

    recent_deployments_payload = payload.get("recent_deployments")
    if isinstance(recent_deployments_payload, (list, tuple)):
        linked_payload["recent_deployments"] = tuple(
            _link_deployment_summary(item)
            for item in recent_deployments_payload
            if isinstance(item, dict)
        )

    return linked_payload


def _build_deployment_record_payload(record: DeploymentRecord) -> dict[str, object]:
    resolved_target_summary = (
        record.resolved_target.model_dump(mode="json") if record.resolved_target is not None else {}
    )
    return {
        "record": {
            **_summarize_deployment_record(record),
            "wait_for_completion": record.wait_for_completion,
            "verify_destination_health": record.verify_destination_health,
            "no_cache": record.no_cache,
            "delegated_executor": record.delegated_executor,
        },
        "resolved_target": resolved_target_summary,
        "deploy": record.deploy.model_dump(mode="json"),
        "post_deploy_update": record.post_deploy_update.model_dump(mode="json"),
        "destination_health": record.destination_health.model_dump(mode="json"),
    }


def _build_promotion_record_payload(record: PromotionRecord) -> dict[str, object]:
    return {
        "record": _summarize_promotion_record(record),
        "source_health": record.source_health.model_dump(mode="json"),
        "backup_gate": record.backup_gate.model_dump(mode="json"),
        "deploy": record.deploy.model_dump(mode="json"),
        "post_deploy_update": record.post_deploy_update.model_dump(mode="json"),
        "destination_health": record.destination_health.model_dump(mode="json"),
    }


def _build_backup_gate_record_payload(record: BackupGateRecord) -> dict[str, object]:
    return {"record": _summarize_backup_gate_record(record)}


def _build_artifact_manifest_payload(
    manifest: ArtifactIdentityManifest,
    *,
    record_store: FilesystemRecordStore,
    relative_prefix: str,
    context_name: str = "",
) -> dict[str, object]:
    related_environments = [
        {
            "label": f"{record.context}/{record.instance}",
            "summary": f"Live inventory updated {record.updated_at or '-'}.",
            "href": f"{relative_prefix}environments/{_site_environment_status_file_name(context_name=record.context, instance_name=record.instance)}",
            "status_href": f"{relative_prefix}environments/{_site_environment_status_file_name(context_name=record.context, instance_name=record.instance)}",
        }
        for record in record_store.list_environment_inventory()
        if _artifact_id_or_empty(record.artifact_identity) == manifest.artifact_id
        and (not context_name or record.context == context_name)
    ]
    related_deployments = [
        {
            "label": record.record_id,
            "summary": f"{record.context}/{record.instance} deploy status {record.deploy.status}.",
            "href": _site_deployment_record_href(record_id=record.record_id, relative_prefix=relative_prefix),
            "status_href": f"{relative_prefix}environments/{_site_environment_status_file_name(context_name=record.context, instance_name=record.instance)}",
        }
        for record in record_store.list_deployment_records(context_name=context_name)
        if _artifact_id_or_empty(record.artifact_identity) == manifest.artifact_id
    ]
    related_promotions = [
        {
            "label": record.record_id,
            "summary": f"{record.context} {record.from_instance}->{record.to_instance} deploy status {record.deploy.status}.",
            "href": _site_promotion_record_href(record_id=record.record_id, relative_prefix=relative_prefix),
            "status_href": f"{relative_prefix}environments/{_site_environment_status_file_name(context_name=record.context, instance_name=record.to_instance)}",
        }
        for record in record_store.list_promotion_records(context_name=context_name)
        if _artifact_id_or_empty(record.artifact_identity) == manifest.artifact_id
    ]
    return {
        "manifest": manifest.model_dump(mode="json"),
        "image": manifest.image.model_dump(mode="json"),
        "openupgrade_inputs": manifest.openupgrade_inputs.model_dump(mode="json"),
        "build_flags": manifest.build_flags.model_dump(mode="json"),
        "addon_sources": tuple(source.model_dump(mode="json") for source in manifest.addon_sources),
        "related_environments": tuple(related_environments),
        "related_deployments": tuple(related_deployments),
        "related_promotions": tuple(related_promotions),
    }


@click.group()
def main() -> None:
    """Control-plane CLI."""


@main.group()
def artifacts() -> None:
    """Artifact manifest commands."""


@artifacts.command("write")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def artifacts_write(state_dir: Path, input_file: Path) -> None:
    manifest = ArtifactIdentityManifest.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_artifact_manifest(manifest)
    click.echo(record_path)


@artifacts.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--artifact-id", required=True)
def artifacts_show(state_dir: Path, artifact_id: str) -> None:
    manifest = _store(state_dir).read_artifact_manifest(artifact_id)
    click.echo(json.dumps(manifest.model_dump(mode="json"), indent=2, sort_keys=True))


@artifacts.command("ingest")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def artifacts_ingest(state_dir: Path, input_file: Path) -> None:
    manifest = ArtifactIdentityManifest.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_artifact_manifest(manifest)
    click.echo(record_path)


@main.group("backup-gates")
def backup_gates() -> None:
    """Backup gate record commands."""


@backup_gates.command("write")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def backup_gates_write(state_dir: Path, input_file: Path) -> None:
    record = BackupGateRecord.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_backup_gate_record(record)
    click.echo(record_path)


@backup_gates.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--record-id", required=True)
def backup_gates_show(state_dir: Path, record_id: str) -> None:
    record = _store(state_dir).read_backup_gate_record(record_id)
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@backup_gates.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option("--instance", "instance_name", default="")
@click.option("--limit", type=click.IntRange(min=1), default=20, show_default=True)
def backup_gates_list(state_dir: Path, context_name: str, instance_name: str, limit: int) -> None:
    records = _store(state_dir).list_backup_gate_records(
        context_name=context_name,
        instance_name=instance_name,
        limit=limit,
    )
    click.echo(
        json.dumps(
            [_summarize_backup_gate_record(record) for record in records], indent=2, sort_keys=True
        )
    )


@main.group()
def promotions() -> None:
    """Promotion record commands."""


@promotions.command("write")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def promotions_write(state_dir: Path, input_file: Path) -> None:
    record = PromotionRecord.model_validate(_load_json_file(input_file))
    record_path = _store(state_dir).write_promotion_record(record)
    click.echo(record_path)


@promotions.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--record-id", required=True)
def promotions_show(state_dir: Path, record_id: str) -> None:
    record = _store(state_dir).read_promotion_record(record_id)
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@promotions.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option("--from-instance", "from_instance_name", default="")
@click.option("--to-instance", "to_instance_name", default="")
@click.option("--limit", type=click.IntRange(min=1), default=20, show_default=True)
def promotions_list(
    state_dir: Path,
    context_name: str,
    from_instance_name: str,
    to_instance_name: str,
    limit: int,
) -> None:
    records = _store(state_dir).list_promotion_records(
        context_name=context_name,
        from_instance_name=from_instance_name,
        to_instance_name=to_instance_name,
        limit=limit,
    )
    click.echo(
        json.dumps(
            [_summarize_promotion_record(record) for record in records], indent=2, sort_keys=True
        )
    )


@main.group()
def deployments() -> None:
    """Deployment record commands."""


@deployments.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--record-id", required=True)
def deployments_show(state_dir: Path, record_id: str) -> None:
    record = _store(state_dir).read_deployment_record(record_id)
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@deployments.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option("--instance", "instance_name", default="")
@click.option("--limit", type=click.IntRange(min=1), default=20, show_default=True)
def deployments_list(state_dir: Path, context_name: str, instance_name: str, limit: int) -> None:
    records = _store(state_dir).list_deployment_records(
        context_name=context_name,
        instance_name=instance_name,
        limit=limit,
    )
    click.echo(
        json.dumps(
            [_summarize_deployment_record(record) for record in records], indent=2, sort_keys=True
        )
    )


@main.group()
def inventory() -> None:
    """Environment inventory commands."""


@inventory.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
def inventory_show(state_dir: Path, context_name: str, instance_name: str) -> None:
    record = _store(state_dir).read_environment_inventory(
        context_name=context_name, instance_name=instance_name
    )
    click.echo(json.dumps(record.model_dump(mode="json"), indent=2, sort_keys=True))


@inventory.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
def inventory_list(state_dir: Path) -> None:
    records = _store(state_dir).list_environment_inventory()
    click.echo(
        json.dumps([record.model_dump(mode="json") for record in records], indent=2, sort_keys=True)
    )


@inventory.command("overview")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
def inventory_overview(state_dir: Path, context_name: str) -> None:
    payload = _build_environment_overview_payloads(
        record_store=_store(state_dir),
        context_name=context_name,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@main.group()
def ui() -> None:
    """Operator UI commands."""


@ui.command("inventory-overview")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option(
    "--output-file",
    type=click.Path(path_type=Path),
    default=Path("tmp/inventory-overview.html"),
    show_default=True,
)
def ui_inventory_overview(state_dir: Path, context_name: str, output_file: Path) -> None:
    payload = _build_environment_overview_payloads(
        record_store=_store(state_dir),
        context_name=context_name,
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        render_inventory_overview_dashboard(payload, context_name=context_name),
        encoding="utf-8",
    )
    click.echo(output_file)


@ui.command("environment-status")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option(
    "--output-file",
    type=click.Path(path_type=Path),
    default=Path("tmp/environment-status.html"),
    show_default=True,
)
def ui_environment_status(
    state_dir: Path, context_name: str, instance_name: str, output_file: Path
) -> None:
    payload = _build_environment_status_payload(
        record_store=_store(state_dir),
        context_name=context_name,
        instance_name=instance_name,
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        render_environment_status_dashboard(payload),
        encoding="utf-8",
    )
    click.echo(output_file)


@ui.command("environment-contract")
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", default="local", show_default=True)
@click.option(
    "--output-file",
    type=click.Path(path_type=Path),
    default=Path("tmp/environment-contract.html"),
    show_default=True,
)
def ui_environment_contract(context_name: str, instance_name: str, output_file: Path) -> None:
    payload = _build_environment_contract_payload(
        context_name=context_name,
        instance_name=instance_name,
    )
    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        render_environment_contract_dashboard(payload),
        encoding="utf-8",
    )
    click.echo(output_file)


@ui.command("build-site")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
@click.option(
    "--output-dir",
    type=click.Path(path_type=Path),
    default=Path("tmp/operator-ui"),
    show_default=True,
)
def ui_build_site(state_dir: Path, context_name: str, output_dir: Path) -> None:
    record_store = _store(state_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    environments_dir = output_dir / "environments"
    contracts_dir = output_dir / "contracts"
    records_dir = output_dir / "records"
    artifact_records_dir = records_dir / "artifacts"
    deployment_records_dir = records_dir / "deployments"
    promotion_records_dir = records_dir / "promotions"
    backup_gate_records_dir = records_dir / "backup-gates"
    environments_dir.mkdir(parents=True, exist_ok=True)
    contracts_dir.mkdir(parents=True, exist_ok=True)
    artifact_records_dir.mkdir(parents=True, exist_ok=True)
    deployment_records_dir.mkdir(parents=True, exist_ok=True)
    promotion_records_dir.mkdir(parents=True, exist_ok=True)
    backup_gate_records_dir.mkdir(parents=True, exist_ok=True)

    overview_payloads = _build_environment_overview_payloads(
        record_store=record_store,
        context_name=context_name,
    )
    linked_overview_payloads: list[dict[str, object]] = []
    site_environment_entries_by_key: dict[tuple[str, str], dict[str, object]] = {}
    for payload in overview_payloads:
        payload_context = str(payload.get("context", ""))
        payload_instance = str(payload.get("instance", ""))
        status_file_name = _site_environment_status_file_name(
            context_name=payload_context,
            instance_name=payload_instance,
        )
        contract_file_name = _site_environment_contract_file_name(
            context_name=payload_context,
            instance_name=payload_instance,
        )
        status_page_href = f"environments/{status_file_name}"
        contract_page_href = f"contracts/{contract_file_name}"
        overview_payload = {
            **_with_site_record_links(payload, relative_prefix=""),
            "status_page_href": status_page_href,
            "contract_page_href": contract_page_href,
        }
        status_payload = {
            **_with_site_record_links(payload, relative_prefix="../"),
            "home_page_href": "../index.html",
            "inventory_overview_href": "../inventory-overview.html",
            "contract_page_href": f"../contracts/{contract_file_name}",
        }
        linked_overview_payloads.append(overview_payload)
        status_output_file = environments_dir / status_file_name
        status_output_file.write_text(
            render_environment_status_dashboard(status_payload),
            encoding="utf-8",
        )
        live_payload = payload.get("live")
        live_summary = live_payload if isinstance(live_payload, dict) else {}
        site_environment_entries_by_key[(payload_context, payload_instance)] = {
            "context": payload_context,
            "instance": payload_instance,
            "summary": (
                f"Artifact {live_summary.get('artifact_id', '') or '-'} with "
                f"deploy status {live_summary.get('deploy_status', '') or 'skipped'}."
            ),
            "status_href": status_page_href,
            "contract_href": contract_page_href,
        }

    overview_output_file = output_dir / "inventory-overview.html"
    overview_output_file.write_text(
        render_inventory_overview_dashboard(
            linked_overview_payloads,
            context_name=context_name,
            home_page_href="index.html",
        ),
        encoding="utf-8",
    )

    definition = control_plane_runtime_environments.load_runtime_environment_definition(
        control_plane_root=_control_plane_root()
    )
    contract_entries: list[dict[str, object]] = []
    for payload_context, context_definition in sorted(definition.contexts.items()):
        if context_name and payload_context != context_name:
            continue
        for payload_instance in sorted(context_definition.instances):
            contract_payload = _build_environment_contract_payload(
                context_name=payload_context,
                instance_name=payload_instance,
            )
            status_file_name = _site_environment_status_file_name(
                context_name=payload_context,
                instance_name=payload_instance,
            )
            contract_file_name = _site_environment_contract_file_name(
                context_name=payload_context,
                instance_name=payload_instance,
            )
            status_output_file = environments_dir / status_file_name
            contract_output_file = contracts_dir / contract_file_name
            if not status_output_file.exists():
                status_output_file.write_text(
                    render_environment_status_dashboard(
                        {
                            "context": payload_context,
                            "instance": payload_instance,
                            "live": {},
                            "live_promotion": {},
                            "authorized_backup_gate": {},
                            "latest_promotion": {},
                            "latest_deployment": {},
                            "home_page_href": "../index.html",
                            "inventory_overview_href": "../inventory-overview.html",
                            "contract_page_href": f"../contracts/{contract_file_name}",
                        }
                    ),
                    encoding="utf-8",
                )
            contract_output_file.write_text(
                render_environment_contract_dashboard(
                    {
                        **contract_payload,
                        "home_page_href": "../index.html",
                        "inventory_overview_href": "../inventory-overview.html",
                        "status_page_href": f"../environments/{status_file_name}",
                    }
                ),
                encoding="utf-8",
            )
            contract_entries.append(
                {
                    "context": payload_context,
                    "instance": payload_instance,
                    "href": f"contracts/{contract_file_name}",
                }
            )
            site_environment_entries_by_key.setdefault(
                (payload_context, payload_instance),
                {
                    "context": payload_context,
                    "instance": payload_instance,
                    "summary": "No live inventory record yet. Use the contract page to inspect environment truth first.",
                    "status_href": f"environments/{status_file_name}",
                    "contract_href": f"contracts/{contract_file_name}",
                },
            )

    site_environment_entries = [
        site_environment_entries_by_key[key]
        for key in sorted(site_environment_entries_by_key)
    ]

    relevant_artifact_ids = {
        _artifact_id_or_empty(record.artifact_identity)
        for record in record_store.list_environment_inventory()
        if _artifact_id_or_empty(record.artifact_identity)
        and (not context_name or record.context == context_name)
    }
    relevant_artifact_ids.update(
        _artifact_id_or_empty(record.artifact_identity)
        for record in record_store.list_deployment_records(context_name=context_name)
        if _artifact_id_or_empty(record.artifact_identity)
    )
    relevant_artifact_ids.update(
        _artifact_id_or_empty(record.artifact_identity)
        for record in record_store.list_promotion_records(context_name=context_name)
        if _artifact_id_or_empty(record.artifact_identity)
    )

    for artifact_manifest in record_store.list_artifact_manifests():
        if context_name and artifact_manifest.artifact_id not in relevant_artifact_ids:
            continue
        artifact_payload = {
            **_build_artifact_manifest_payload(
                artifact_manifest,
                record_store=record_store,
                relative_prefix="../../",
                context_name=context_name,
            ),
            "home_page_href": "../../index.html",
            "inventory_overview_href": "../../inventory-overview.html",
        }
        (artifact_records_dir / _site_artifact_manifest_file_name(artifact_id=artifact_manifest.artifact_id)).write_text(
            render_artifact_manifest_dashboard(artifact_payload),
            encoding="utf-8",
        )

    for deployment_record in record_store.list_deployment_records(
        context_name=context_name,
    ):
        deployment_payload = {
            **_build_deployment_record_payload(deployment_record),
            "home_page_href": "../../index.html",
            "inventory_overview_href": "../../inventory-overview.html",
            "artifact_href": _site_artifact_manifest_href(
                artifact_id=_artifact_id_or_empty(deployment_record.artifact_identity),
                relative_prefix="../../",
            ),
            "status_page_href": (
                f"../../environments/{_site_environment_status_file_name(context_name=deployment_record.context, instance_name=deployment_record.instance)}"
            ),
            "contract_page_href": (
                f"../../contracts/{_site_environment_contract_file_name(context_name=deployment_record.context, instance_name=deployment_record.instance)}"
            ),
        }
        (deployment_records_dir / _site_deployment_record_file_name(record_id=deployment_record.record_id)).write_text(
            render_deployment_record_dashboard(deployment_payload),
            encoding="utf-8",
        )

    for promotion_record in record_store.list_promotion_records(
        context_name=context_name,
    ):
        promotion_payload = {
            **_build_promotion_record_payload(promotion_record),
            "home_page_href": "../../index.html",
            "inventory_overview_href": "../../inventory-overview.html",
            "artifact_href": _site_artifact_manifest_href(
                artifact_id=_artifact_id_or_empty(promotion_record.artifact_identity),
                relative_prefix="../../",
            ),
            "source_status_page_href": (
                f"../../environments/{_site_environment_status_file_name(context_name=promotion_record.context, instance_name=promotion_record.from_instance)}"
            ),
            "destination_status_page_href": (
                f"../../environments/{_site_environment_status_file_name(context_name=promotion_record.context, instance_name=promotion_record.to_instance)}"
            ),
            "backup_record_href": _site_backup_gate_record_href(
                record_id=promotion_record.backup_record_id,
                relative_prefix="../../",
            ),
        }
        (promotion_records_dir / _site_promotion_record_file_name(record_id=promotion_record.record_id)).write_text(
            render_promotion_record_dashboard(promotion_payload),
            encoding="utf-8",
        )

    for backup_gate_record in record_store.list_backup_gate_records(
        context_name=context_name,
    ):
        backup_gate_payload = {
            **_build_backup_gate_record_payload(backup_gate_record),
            "home_page_href": "../../index.html",
            "inventory_overview_href": "../../inventory-overview.html",
            "status_page_href": (
                f"../../environments/{_site_environment_status_file_name(context_name=backup_gate_record.context, instance_name=backup_gate_record.instance)}"
            ),
            "contract_page_href": (
                f"../../contracts/{_site_environment_contract_file_name(context_name=backup_gate_record.context, instance_name=backup_gate_record.instance)}"
            ),
        }
        (backup_gate_records_dir / _site_backup_gate_record_file_name(record_id=backup_gate_record.record_id)).write_text(
            render_backup_gate_record_dashboard(backup_gate_payload),
            encoding="utf-8",
        )

    index_output_file = output_dir / "index.html"
    index_output_file.write_text(
        render_operator_site_index(
            {
                "context": context_name,
                "inventory_overview_href": "inventory-overview.html",
                "environments": site_environment_entries,
                "contracts": contract_entries,
            }
        ),
        encoding="utf-8",
    )
    click.echo(index_output_file)


@inventory.command("status")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
def inventory_status(state_dir: Path, context_name: str, instance_name: str) -> None:
    payload = _build_environment_status_payload(
        record_store=_store(state_dir),
        context_name=context_name,
        instance_name=instance_name,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@main.group()
def environments() -> None:
    """Runtime environment contract commands."""


@environments.command("resolve")
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", default="local", show_default=True)
@click.option("--json-output", is_flag=True, default=False)
def environments_resolve(context_name: str, instance_name: str, json_output: bool) -> None:
    environment_values = control_plane_runtime_environments.resolve_runtime_environment_values(
        control_plane_root=_control_plane_root(),
        context_name=context_name,
        instance_name=instance_name,
    )
    if json_output:
        click.echo(
            json.dumps(
                {
                    "context": context_name,
                    "instance": instance_name,
                    "environment": environment_values,
                },
                indent=2,
                sort_keys=True,
            )
        )
        return
    for environment_key in sorted(environment_values):
        click.echo(f"{environment_key}={environment_values[environment_key]}")


@main.group()
def promote() -> None:
    """Promotion workflow commands."""


@promote.command("record")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--record-id", required=True)
@click.option("--artifact-id", required=True)
@click.option("--backup-record-id", default="", show_default=False)
@click.option("--context", "context_name", required=True)
@click.option("--from-instance", "from_instance_name", required=True)
@click.option("--to-instance", "to_instance_name", required=True)
@click.option("--target-name", required=True)
@click.option("--target-type", type=click.Choice(["compose", "application"]), required=True)
@click.option("--deploy-mode", required=True)
@click.option("--deployment-id", default="", show_default=False)
def promote_record(
    state_dir: Path,
    record_id: str,
    artifact_id: str,
    backup_record_id: str,
    context_name: str,
    from_instance_name: str,
    to_instance_name: str,
    target_name: str,
    target_type: str,
    deploy_mode: str,
    deployment_id: str,
) -> None:
    record = build_promotion_record(
        record_id=record_id,
        artifact_id=artifact_id,
        backup_record_id=backup_record_id,
        context_name=context_name,
        from_instance_name=from_instance_name,
        to_instance_name=to_instance_name,
        target_name=target_name,
        target_type=target_type,
        deploy_mode=deploy_mode,
        deployment_id=deployment_id,
    )
    record_path = _store(state_dir).write_promotion_record(record)
    click.echo(record_path)


@promote.command("resolve")
@click.option("--context", "context_name", required=True)
@click.option("--from-instance", "from_instance_name", required=True)
@click.option("--to-instance", "to_instance_name", required=True)
@click.option("--artifact-id", required=True)
@click.option("--backup-record-id", required=True)
@click.option("--source-ref", "source_git_ref", default="")
@click.option("--wait/--no-wait", default=True, show_default=True)
@click.option("--timeout", "timeout_override_seconds", type=int, default=None)
@click.option("--verify-health/--no-verify-health", default=True)
@click.option("--health-timeout", "health_timeout_override_seconds", type=int, default=None)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--no-cache", is_flag=True, default=False)
@click.option("--allow-dirty", is_flag=True, default=False)
def promote_resolve(
    context_name: str,
    from_instance_name: str,
    to_instance_name: str,
    artifact_id: str,
    backup_record_id: str,
    source_git_ref: str,
    wait: bool,
    timeout_override_seconds: int | None,
    verify_health: bool,
    health_timeout_override_seconds: int | None,
    dry_run: bool,
    no_cache: bool,
    allow_dirty: bool,
) -> None:
    request = _resolve_native_promotion_request(
        context_name=context_name,
        from_instance_name=from_instance_name,
        to_instance_name=to_instance_name,
        artifact_id=artifact_id,
        backup_record_id=backup_record_id,
        source_git_ref=source_git_ref,
        wait=wait,
        timeout_override_seconds=timeout_override_seconds,
        verify_health=verify_health,
        health_timeout_override_seconds=health_timeout_override_seconds,
        dry_run=dry_run,
        no_cache=no_cache,
        allow_dirty=allow_dirty,
    )
    click.echo(json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True))


@promote.command("execute")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--env-file", type=click.Path(exists=True, path_type=Path), default=None)
def promote_execute(
    state_dir: Path,
    input_file: Path,
    env_file: Path | None,
) -> None:
    request = PromotionRequest.model_validate(_load_json_file(input_file))
    record_store = _store(state_dir)
    resolved_artifact_id = _require_artifact_id(requested_artifact_id=request.artifact_id)
    _read_artifact_manifest(
        record_store=record_store,
        artifact_id=resolved_artifact_id,
    )
    normalized_request = request.model_copy(update={"artifact_id": resolved_artifact_id})
    resolved_request, _backup_gate_record = _resolve_backup_gate_for_promotion(
        request=normalized_request,
        record_store=record_store,
    )
    record_id = generate_promotion_record_id(
        context_name=resolved_request.context,
        from_instance_name=resolved_request.from_instance,
        to_instance_name=resolved_request.to_instance,
    )
    if resolved_request.dry_run:
        _resolve_ship_request_for_promotion(request=resolved_request)
        click.echo(
            json.dumps(
                build_executed_promotion_record(
                    request=resolved_request,
                    record_id=record_id,
                    deployment_id="",
                    deployment_status="pending",
                ).model_dump(mode="json"),
                indent=2,
                sort_keys=True,
            )
        )
        return

    pending_record = build_executed_promotion_record(
        request=resolved_request,
        record_id=record_id,
        deployment_id="",
        deployment_status="pending",
    )
    record_path = record_store.write_promotion_record(pending_record)

    try:
        ship_request = _resolve_ship_request_for_promotion(request=resolved_request)
        _record_path, deployment_record = _execute_ship(
            state_dir=state_dir,
            env_file=env_file,
            request=ship_request,
        )
        if not isinstance(deployment_record, DeploymentRecord):
            raise click.ClickException(
                "Ship execution returned an unexpected non-record payload during promotion."
            )
        final_record = build_executed_promotion_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id=deployment_record.deploy.deployment_id,
            deployment_status=deployment_record.deploy.status,
        )
    except (subprocess.CalledProcessError, click.ClickException, json.JSONDecodeError):
        final_record = build_executed_promotion_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="fail",
        )
        record_store.write_promotion_record(final_record)
        raise

    record_store.write_promotion_record(final_record)
    if deployment_record.wait_for_completion and deployment_record.deploy.status == "pass":
        _write_environment_inventory(
            record_store=record_store,
            deployment_record=deployment_record,
            promotion_record_id=final_record.record_id,
            promoted_from_instance=final_record.from_instance,
        )
    click.echo(record_path)


@main.group()
def ship() -> None:
    """Ship workflow commands."""


@ship.command("plan")
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def ship_plan(input_file: Path) -> None:
    request = ShipRequest.model_validate(_load_json_file(input_file))
    click.echo(json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True))


@ship.command("resolve")
@click.option("--context", "context_name", required=True)
@click.option("--instance", "instance_name", required=True)
@click.option("--artifact-id", required=True)
@click.option("--source-ref", "source_git_ref", default="")
@click.option("--wait/--no-wait", default=True, show_default=True)
@click.option("--timeout", "timeout_override_seconds", type=int, default=None)
@click.option("--verify-health/--no-verify-health", default=True)
@click.option("--health-timeout", "health_timeout_override_seconds", type=int, default=None)
@click.option("--dry-run", is_flag=True, default=False)
@click.option("--no-cache", is_flag=True, default=False)
@click.option("--allow-dirty", is_flag=True, default=False)
def ship_resolve(
    context_name: str,
    instance_name: str,
    artifact_id: str,
    source_git_ref: str,
    wait: bool,
    timeout_override_seconds: int | None,
    verify_health: bool,
    health_timeout_override_seconds: int | None,
    dry_run: bool,
    no_cache: bool,
    allow_dirty: bool,
) -> None:
    request = _resolve_native_ship_request(
        context_name=context_name,
        instance_name=instance_name,
        artifact_id=artifact_id,
        source_git_ref=source_git_ref,
        wait=wait,
        timeout_override_seconds=timeout_override_seconds,
        verify_health=verify_health,
        health_timeout_override_seconds=health_timeout_override_seconds,
        dry_run=dry_run,
        no_cache=no_cache,
        allow_dirty=allow_dirty,
    )
    click.echo(json.dumps(request.model_dump(mode="json"), indent=2, sort_keys=True))


@ship.command("execute")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--env-file", type=click.Path(exists=True, path_type=Path), default=None)
def ship_execute(
    state_dir: Path,
    input_file: Path,
    env_file: Path | None,
) -> None:
    request = ShipRequest.model_validate(_load_json_file(input_file))
    record_path, _record = _execute_ship(
        state_dir=state_dir,
        env_file=env_file,
        request=request,
    )
    if record_path is not None:
        click.echo(record_path)
