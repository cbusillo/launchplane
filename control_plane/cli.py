import json
import subprocess
import time
from json import JSONDecodeError
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

import click
from pydantic import ValidationError

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.backup_gate_record import BackupGateRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.github_pull_request_event import GitHubPullRequestEvent
from control_plane.contracts.github_webhook_replay_envelope import GitHubWebhookReplayEnvelope
from control_plane.contracts.preview_mutation_request import (
    HarborPullRequestMutationIntent,
    PreviewDestroyMutationRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.preview_manifest import HarborResolvedPreviewManifest
from control_plane.contracts.preview_request_metadata import HarborPreviewRequestParseResult
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
from control_plane.workflows.harbor import (
    adapt_github_webhook_pull_request_event,
    apply_generation_failed_transition,
    apply_generation_ready_transition,
    apply_generation_requested_transition,
    apply_preview_destroyed_transition,
    build_pull_request_feedback_payload,
    build_preview_generation_record_from_request,
    build_preview_history_payload,
    build_preview_inventory_payload,
    build_pull_request_event_action_payload,
    build_preview_record_from_request,
    build_preview_status_payload,
    deliver_pull_request_feedback,
    find_preview_record,
    harbor_anchor_repo_context,
    resolve_harbor_github_webhook_secret,
    verify_github_webhook_signature,
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


def _load_github_webhook_json_bytes(
    raw_payload_bytes: bytes,
    *,
    description: str = "GitHub webhook payload",
) -> dict[str, object]:
    try:
        webhook_payload = json.loads(raw_payload_bytes.decode("utf-8"))
    except (UnicodeDecodeError, JSONDecodeError) as exc:
        raise click.ClickException(f"{description} must be valid UTF-8 JSON: {exc}") from exc
    if not isinstance(webhook_payload, dict):
        raise click.ClickException(f"{description} must decode to a JSON object.")
    return webhook_payload


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


@main.group("harbor-previews")
def harbor_previews() -> None:
    """Harbor preview record and read-model commands."""


@harbor_previews.command("write-preview")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def harbor_previews_write_preview(state_dir: Path, input_file: Path) -> None:
    request = PreviewMutationRequest.model_validate(_load_json_file(input_file))
    record = build_preview_record_from_request(
        control_plane_root=_control_plane_root(),
        record_store=_store(state_dir),
        request=request,
    )
    record_path = _store(state_dir).write_preview_record(record)
    click.echo(record_path)


@harbor_previews.command("write-generation")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def harbor_previews_write_generation(state_dir: Path, input_file: Path) -> None:
    request = PreviewGenerationMutationRequest.model_validate(_load_json_file(input_file))
    record = build_preview_generation_record_from_request(
        record_store=_store(state_dir),
        request=request,
    )
    record_path = _store(state_dir).write_preview_generation_record(record)
    click.echo(record_path)


@harbor_previews.command("request-generation")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option(
    "--preview-input-file", type=click.Path(exists=True, path_type=Path), required=True
)
@click.option(
    "--generation-input-file", type=click.Path(exists=True, path_type=Path), required=True
)
def harbor_previews_request_generation(
    state_dir: Path,
    preview_input_file: Path,
    generation_input_file: Path,
) -> None:
    record_store = _store(state_dir)
    preview_request = PreviewMutationRequest.model_validate(_load_json_file(preview_input_file))
    generation_request = PreviewGenerationMutationRequest.model_validate(
        _load_json_file(generation_input_file)
    )
    result_payload = _apply_harbor_request_generation(
        control_plane_root=_control_plane_root(),
        record_store=record_store,
        preview_request=preview_request,
        generation_request=generation_request,
    )
    click.echo(json.dumps(result_payload, indent=2, sort_keys=True))


@harbor_previews.command("mark-generation-ready")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def harbor_previews_mark_generation_ready(state_dir: Path, input_file: Path) -> None:
    record_store = _store(state_dir)
    request = PreviewGenerationMutationRequest.model_validate(_load_json_file(input_file))
    if not request.generation_id.strip():
        raise click.ClickException("Ready-generation transition requires generation_id.")
    preview_record = _read_harbor_preview_or_fail(
        record_store=record_store,
        context_name=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
    )
    _read_harbor_generation_or_fail(
        record_store=record_store,
        preview_id=preview_record.preview_id,
        generation_id=request.generation_id,
    )
    generation_record = build_preview_generation_record_from_request(
        record_store=record_store,
        request=request,
    )
    transitioned_preview = apply_generation_ready_transition(
        preview=preview_record,
        generation=generation_record,
    )
    generation_path = record_store.write_preview_generation_record(generation_record)
    preview_path = record_store.write_preview_record(transitioned_preview)
    click.echo(
        json.dumps(
            {
                "generation_id": generation_record.generation_id,
                "generation_path": str(generation_path),
                "preview_id": transitioned_preview.preview_id,
                "preview_path": str(preview_path),
            },
            indent=2,
            sort_keys=True,
        )
    )


@harbor_previews.command("mark-generation-failed")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def harbor_previews_mark_generation_failed(state_dir: Path, input_file: Path) -> None:
    record_store = _store(state_dir)
    request = PreviewGenerationMutationRequest.model_validate(_load_json_file(input_file))
    if not request.generation_id.strip():
        raise click.ClickException("Failed-generation transition requires generation_id.")
    preview_record = _read_harbor_preview_or_fail(
        record_store=record_store,
        context_name=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
    )
    _read_harbor_generation_or_fail(
        record_store=record_store,
        preview_id=preview_record.preview_id,
        generation_id=request.generation_id,
    )
    generation_record = build_preview_generation_record_from_request(
        record_store=record_store,
        request=request,
    )
    transitioned_preview = apply_generation_failed_transition(
        preview=preview_record,
        generation=generation_record,
    )
    generation_path = record_store.write_preview_generation_record(generation_record)
    preview_path = record_store.write_preview_record(transitioned_preview)
    click.echo(
        json.dumps(
            {
                "generation_id": generation_record.generation_id,
                "generation_path": str(generation_path),
                "preview_id": transitioned_preview.preview_id,
                "preview_path": str(preview_path),
            },
            indent=2,
            sort_keys=True,
        )
    )


@harbor_previews.command("destroy-preview")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
def harbor_previews_destroy_preview(state_dir: Path, input_file: Path) -> None:
    record_store = _store(state_dir)
    request = PreviewDestroyMutationRequest.model_validate(_load_json_file(input_file))
    result_payload = _apply_harbor_destroy_preview(
        record_store=record_store,
        request=request,
    )
    click.echo(result_payload["preview_path"])


@harbor_previews.command("ingest-pr-event")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--apply", "apply_intent", is_flag=True)
@click.option("--deliver-feedback", is_flag=True)
def harbor_previews_ingest_pr_event(
    state_dir: Path, input_file: Path, apply_intent: bool, deliver_feedback: bool
) -> None:
    event = GitHubPullRequestEvent.model_validate(_load_json_file(input_file))
    payload = _ingest_harbor_pr_event_payload(
        state_dir=state_dir,
        event=event,
        apply_intent=apply_intent,
        deliver_feedback=deliver_feedback,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@harbor_previews.command("ingest-github-webhook")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--event-name", default="pull_request", show_default=True)
@click.option("--delivery-id", default="", help="Optional GitHub delivery id for traceability.")
@click.option("--signature-256", default="", help="Raw X-Hub-Signature-256 header value.")
@click.option("--allow-unsigned", is_flag=True, help="Explicit local/manual bypass for signature verification.")
@click.option("--apply", "apply_intent", is_flag=True)
@click.option("--deliver-feedback", is_flag=True)
def harbor_previews_ingest_github_webhook(
    state_dir: Path,
    input_file: Path,
    event_name: str,
    delivery_id: str,
    signature_256: str,
    allow_unsigned: bool,
    apply_intent: bool,
    deliver_feedback: bool,
) -> None:
    raw_payload_bytes, webhook_payload = _load_github_webhook_json_file(input_file)
    payload = _ingest_harbor_github_webhook_payload(
        state_dir=state_dir,
        event_name=event_name,
        raw_payload_bytes=raw_payload_bytes,
        webhook_payload=webhook_payload,
        delivery_id=delivery_id,
        delivery_source="github-webhook",
        signature_256=signature_256,
        allow_unsigned=allow_unsigned,
        apply_intent=apply_intent,
        deliver_feedback=deliver_feedback,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _load_json_object_file(input_file: Path, *, description: str) -> dict[str, object]:
    try:
        payload = json.loads(input_file.read_text(encoding="utf-8"))
    except (UnicodeDecodeError, JSONDecodeError) as exc:
        raise click.ClickException(f"{description} must be valid UTF-8 JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise click.ClickException(f"{description} must decode to a JSON object.")
    return payload


def _load_github_webhook_capture_headers(input_file: Path) -> dict[str, str]:
    payload = _load_json_object_file(input_file, description="GitHub webhook headers file")
    headers: dict[str, str] = {}
    for header_name, header_value in payload.items():
        if not isinstance(header_value, str):
            raise click.ClickException(
                "GitHub webhook headers file must map header names to string values."
            )
        headers[str(header_name)] = header_value
    return headers


def _github_webhook_capture_header_value(headers: dict[str, str], name: str) -> str:
    normalized_name = name.strip().lower()
    if not normalized_name:
        return ""
    for header_name, header_value in headers.items():
        if header_name.strip().lower() == normalized_name:
            return header_value.strip()
    return ""


def _split_http_capture_text(http_capture_text: str) -> tuple[str, str]:
    for delimiter in ("\r\n\r\n", "\n\n"):
        if delimiter in http_capture_text:
            header_text, body_text = http_capture_text.split(delimiter, 1)
            return header_text, body_text
    raise click.ClickException(
        "GitHub webhook HTTP capture must contain headers, a blank line, and a JSON body."
    )


def _parse_github_webhook_http_capture(input_file: Path) -> tuple[str, dict[str, str], bytes]:
    try:
        http_capture_text = input_file.read_text(encoding="utf-8")
    except UnicodeDecodeError as exc:
        raise click.ClickException(f"GitHub webhook HTTP capture must be valid UTF-8 text: {exc}") from exc
    header_text, body_text = _split_http_capture_text(http_capture_text)
    header_lines = header_text.splitlines()
    if not header_lines:
        raise click.ClickException("GitHub webhook HTTP capture is missing the request line.")
    request_line = header_lines[0].strip()
    if not request_line.startswith("POST ") or "HTTP/" not in request_line:
        raise click.ClickException(
            "GitHub webhook HTTP capture must start with a POST request line such as 'POST /github-webhook HTTP/1.1'."
        )
    headers: dict[str, str] = {}
    for header_line in header_lines[1:]:
        if not header_line.strip():
            continue
        if ":" not in header_line:
            raise click.ClickException(
                "GitHub webhook HTTP capture headers must use the 'Name: value' format."
            )
        header_name, header_value = header_line.split(":", 1)
        normalized_name = header_name.strip()
        if not normalized_name:
            raise click.ClickException("GitHub webhook HTTP capture contains a blank header name.")
        headers[normalized_name] = header_value.strip()
    if not headers:
        raise click.ClickException("GitHub webhook HTTP capture must include at least one header.")
    for override_header_name in ("X-HTTP-Method-Override", "X-Method-Override"):
        override_method = _github_webhook_capture_header_value(headers, override_header_name)
        if override_method and override_method.upper() != "POST":
            raise click.ClickException(
                f"GitHub webhook HTTP capture {override_header_name} must not conflict with the captured POST request."
            )
    declared_transfer_encoding = _github_webhook_capture_header_value(headers, "Transfer-Encoding")
    if declared_transfer_encoding and declared_transfer_encoding.lower() != "identity":
        raise click.ClickException(
            "GitHub webhook HTTP capture Transfer-Encoding is unsupported for the saved-capture replay path."
        )
    declared_content_type = _github_webhook_capture_header_value(headers, "Content-Type")
    if declared_content_type:
        normalized_media_type = declared_content_type.split(";", 1)[0].strip().lower()
        if normalized_media_type not in {"application/json"} and not normalized_media_type.endswith(
            "+json"
        ):
            raise click.ClickException(
                "GitHub webhook HTTP capture Content-Type must be JSON when present."
            )
    body_bytes = body_text.encode("utf-8")
    declared_content_length = _github_webhook_capture_header_value(headers, "Content-Length")
    if declared_content_length:
        try:
            expected_content_length = int(declared_content_length)
        except ValueError as exc:
            raise click.ClickException(
                "GitHub webhook HTTP capture Content-Length header must be an integer when present."
            ) from exc
        if expected_content_length < 0:
            raise click.ClickException(
                "GitHub webhook HTTP capture Content-Length header must not be negative."
            )
        if expected_content_length != len(body_bytes):
            raise click.ClickException(
                "GitHub webhook HTTP capture Content-Length does not match the saved body bytes."
            )
    return request_line, headers, body_bytes


def _merge_github_webhook_capture_evidence(
    *,
    evidence_payload: dict[str, object] | None,
    request_line: str,
) -> dict[str, object] | None:
    if not request_line.strip():
        return evidence_payload

    merged_evidence = dict(evidence_payload) if evidence_payload is not None else {}
    http_request_payload = merged_evidence.get("http_request")
    if http_request_payload is None:
        merged_evidence["http_request"] = {"request_line": request_line}
        return merged_evidence
    if not isinstance(http_request_payload, dict):
        raise click.ClickException(
            "GitHub webhook evidence file field 'http_request' must be a JSON object when provided."
        )

    request_line_payload = http_request_payload.get("request_line")
    if request_line_payload is not None:
        if not isinstance(request_line_payload, str):
            raise click.ClickException(
                "GitHub webhook evidence file field 'http_request.request_line' must be a string when provided."
            )
        if request_line_payload != request_line:
            raise click.ClickException(
                "GitHub webhook evidence file request_line conflicts with the saved HTTP capture request line."
            )
        return merged_evidence

    merged_http_request_payload = dict(http_request_payload)
    merged_http_request_payload["request_line"] = request_line
    merged_evidence["http_request"] = merged_http_request_payload
    return merged_evidence


@harbor_previews.command("build-github-webhook-replay-envelope")
@click.option("--payload-file", type=click.Path(exists=True, path_type=Path))
@click.option("--http-capture-file", type=click.Path(exists=True, path_type=Path))
@click.option("--headers-file", type=click.Path(exists=True, path_type=Path))
@click.option("--event-name", default="", help="Optional GitHub event name override.")
@click.option("--signature-256", default="", help="Optional X-Hub-Signature-256 override.")
@click.option("--delivery-id", default="", help="Optional GitHub delivery id override.")
@click.option("--delivery-source", default="", help="Optional top-level delivery source override.")
@click.option("--allow-unsigned", is_flag=True, help="Emit an explicit unsigned replay envelope.")
@click.option("--recorded-at", default="", help="Optional capture timestamp for traceability.")
@click.option("--capture-source", default="", help="Optional capture source label for replay metadata.")
@click.option("--evidence-file", type=click.Path(exists=True, path_type=Path))
@click.option("--output-file", type=click.Path(path_type=Path))
def harbor_previews_build_github_webhook_replay_envelope(
    payload_file: Path | None,
    http_capture_file: Path | None,
    headers_file: Path | None,
    event_name: str,
    signature_256: str,
    delivery_id: str,
    delivery_source: str,
    allow_unsigned: bool,
    recorded_at: str,
    capture_source: str,
    evidence_file: Path | None,
    output_file: Path | None,
) -> None:
    if (payload_file is None) == (http_capture_file is None):
        raise click.ClickException(
            "Provide exactly one of --payload-file or --http-capture-file when building a GitHub replay envelope."
        )
    if http_capture_file is not None and headers_file is not None:
        raise click.ClickException(
            "--headers-file cannot be combined with --http-capture-file because the HTTP capture already carries headers."
        )

    capture_headers: dict[str, str] = {}
    captured_request_line = ""
    if http_capture_file is not None:
        captured_request_line, capture_headers, raw_payload_bytes = _parse_github_webhook_http_capture(
            http_capture_file
        )
        _load_github_webhook_json_bytes(raw_payload_bytes, description="GitHub webhook HTTP capture body")
    else:
        assert payload_file is not None
        raw_payload_bytes, _ = _load_github_webhook_json_file(payload_file)
        capture_headers = (
            _load_github_webhook_capture_headers(headers_file) if headers_file is not None else {}
        )
    capture_event_name = _github_webhook_capture_header_value(capture_headers, "X-GitHub-Event")
    evidence_payload = (
        _load_json_object_file(evidence_file, description="GitHub webhook evidence file")
        if evidence_file is not None
        else None
    )
    evidence_payload = _merge_github_webhook_capture_evidence(
        evidence_payload=evidence_payload,
        request_line=captured_request_line,
    )
    raw_payload_text = raw_payload_bytes.decode("utf-8")

    capture_payload: dict[str, object] | None = None
    if capture_headers or recorded_at.strip() or capture_source.strip() or evidence_payload is not None:
        capture_payload = {}
        if recorded_at.strip():
            capture_payload["recorded_at"] = recorded_at.strip()
        if capture_source.strip():
            capture_payload["source"] = capture_source.strip()
        if capture_headers:
            capture_payload["headers"] = capture_headers
        if evidence_payload is not None:
            capture_payload["evidence"] = evidence_payload

    resolved_event_name = event_name.strip() or capture_event_name or "pull_request"
    top_level_event_name = event_name.strip() or ("" if capture_event_name else resolved_event_name)
    top_level_signature_256 = signature_256.strip()
    top_level_delivery_id = delivery_id.strip()
    envelope = GitHubWebhookReplayEnvelope.model_validate(
        {
            "schema_version": 1,
            "adapter": "github_webhook",
            "event_name": top_level_event_name,
            "signature_256": top_level_signature_256,
            "allow_unsigned": allow_unsigned,
            "delivery_id": top_level_delivery_id,
            "delivery_source": delivery_source.strip(),
            "payload_text": raw_payload_text,
            "capture": capture_payload,
        }
    )
    envelope_json = json.dumps(
        envelope.model_dump(mode="json", exclude_none=True),
        indent=2,
        sort_keys=True,
    )
    if output_file is not None:
        output_file.write_text(f"{envelope_json}\n", encoding="utf-8")
    click.echo(envelope_json)


@harbor_previews.command("replay-github-webhook")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--input-file", type=click.Path(exists=True, path_type=Path), required=True)
@click.option("--apply", "apply_intent", is_flag=True)
@click.option("--deliver-feedback", is_flag=True)
def harbor_previews_replay_github_webhook(
    state_dir: Path,
    input_file: Path,
    apply_intent: bool,
    deliver_feedback: bool,
) -> None:
    try:
        envelope = GitHubWebhookReplayEnvelope.model_validate(_load_json_file(input_file))
    except ValidationError as exc:
        raise click.ClickException(f"Invalid GitHub webhook replay envelope: {exc}") from exc
    resolved_event_name = envelope.resolved_event_name()
    resolved_delivery_id = envelope.resolved_delivery_id()
    resolved_delivery_source = envelope.resolved_delivery_source()
    raw_payload_bytes, webhook_payload = _load_github_webhook_replay_envelope(envelope)
    payload = _ingest_harbor_github_webhook_payload(
        state_dir=state_dir,
        event_name=resolved_event_name,
        raw_payload_bytes=raw_payload_bytes,
        webhook_payload=webhook_payload,
        delivery_id=resolved_delivery_id,
        delivery_source=resolved_delivery_source,
        signature_256=envelope.resolved_signature_256(),
        allow_unsigned=envelope.allow_unsigned,
        apply_intent=apply_intent,
        deliver_feedback=deliver_feedback,
    )
    payload["webhook_replay"] = {
        "adapter": envelope.adapter,
        "event_name": resolved_event_name,
        "delivery_id": resolved_delivery_id,
        "delivery_source": resolved_delivery_source,
    }
    replay_capture_payload = envelope.replay_capture_payload()
    if replay_capture_payload is not None:
        payload["webhook_replay"]["capture"] = replay_capture_payload
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _ingest_harbor_pr_event_payload(
    *,
    state_dir: Path,
    event: GitHubPullRequestEvent,
    apply_intent: bool,
    deliver_feedback: bool,
) -> dict[str, object]:
    control_plane_root = _control_plane_root()
    record_store = _store(state_dir)
    payload = build_pull_request_event_action_payload(
        control_plane_root=control_plane_root,
        record_store=record_store,
        event=event,
    )
    if apply_intent:
        payload["apply"] = _apply_harbor_pr_event_intent(
            control_plane_root=control_plane_root,
            record_store=record_store,
            payload=payload,
        )
        decision_payload = payload.get("decision")
        request_metadata_payload = payload.get("request_metadata")
        action = decision_payload.get("action", "") if isinstance(decision_payload, dict) else ""
        payload["feedback"] = build_pull_request_feedback_payload(
            record_store=record_store,
            event=event,
            action=action if isinstance(action, str) else "",
            preview=None,
            request_metadata=HarborPreviewRequestParseResult.model_validate(request_metadata_payload),
            resolved_manifest=(
                HarborResolvedPreviewManifest.model_validate(payload["manifest"])
                if isinstance(payload.get("manifest"), dict)
                else None
            ),
            apply_result=payload["apply"],
        )
    if deliver_feedback:
        decision_payload = payload.get("decision")
        resolved_context = (
            decision_payload.get("resolved_context", "") if isinstance(decision_payload, dict) else ""
        )
        feedback_payload = payload.get("feedback")
        if not isinstance(feedback_payload, dict):
            raise click.ClickException("Harbor feedback payload is missing before delivery.")
        payload["feedback_delivery"] = deliver_pull_request_feedback(
            control_plane_root=control_plane_root,
            record_store=record_store,
            event=event,
            resolved_context=resolved_context if isinstance(resolved_context, str) else "",
            feedback_payload=feedback_payload,
        )
    return payload


def _ingest_harbor_github_webhook_payload(
    *,
    state_dir: Path,
    event_name: str,
    raw_payload_bytes: bytes,
    webhook_payload: dict[str, object],
    delivery_id: str,
    delivery_source: str,
    signature_256: str,
    allow_unsigned: bool,
    apply_intent: bool,
    deliver_feedback: bool,
) -> dict[str, object]:
    control_plane_root = _control_plane_root()
    signature_verification = _verify_harbor_github_webhook_signature(
        control_plane_root=control_plane_root,
        event_name=event_name,
        webhook_payload=webhook_payload,
        raw_payload_bytes=raw_payload_bytes,
        signature_256=signature_256,
        allow_unsigned=allow_unsigned,
    )
    event = adapt_github_webhook_pull_request_event(
        event_name=event_name,
        webhook_payload=webhook_payload,
    )
    payload = _ingest_harbor_pr_event_payload(
        state_dir=state_dir,
        event=event,
        apply_intent=apply_intent,
        deliver_feedback=deliver_feedback,
    )
    payload["webhook"] = {
        "event_name": event_name,
        "adapter": "github_pull_request",
        "delivery": {
            "delivery_id": delivery_id.strip(),
            "delivery_source": delivery_source.strip() or "github-webhook",
        },
        "signature_verification": signature_verification,
    }
    return payload


def _load_github_webhook_json_file(input_file: Path) -> tuple[bytes, dict[str, object]]:
    raw_payload_bytes = input_file.read_bytes()
    webhook_payload = _load_github_webhook_json_bytes(
        raw_payload_bytes,
        description="GitHub webhook input file",
    )
    return raw_payload_bytes, webhook_payload


def _load_github_webhook_replay_envelope(
    envelope: GitHubWebhookReplayEnvelope,
) -> tuple[bytes, dict[str, object]]:
    if envelope.payload_text.strip():
        raw_payload_bytes = envelope.payload_text.encode("utf-8")
        try:
            webhook_payload = json.loads(envelope.payload_text)
        except JSONDecodeError as exc:
            raise click.ClickException(
                f"GitHub webhook replay envelope payload_text must be valid JSON: {exc}"
            ) from exc
        if not isinstance(webhook_payload, dict):
            raise click.ClickException(
                "GitHub webhook replay envelope payload_text must decode to a JSON object."
            )
        return raw_payload_bytes, webhook_payload
    if envelope.payload is None:
        raise click.ClickException("GitHub webhook replay envelope is missing payload content.")
    return json.dumps(envelope.payload).encode("utf-8"), envelope.payload


def _verify_harbor_github_webhook_signature(
    *,
    control_plane_root: Path,
    event_name: str,
    webhook_payload: dict[str, object],
    raw_payload_bytes: bytes,
    signature_256: str,
    allow_unsigned: bool,
) -> dict[str, object]:
    if allow_unsigned:
        return {
            "mode": "bypass",
            "verified": False,
            "reason": "allow_unsigned",
        }

    context_name = _resolve_harbor_github_webhook_context(
        event_name=event_name,
        webhook_payload=webhook_payload,
    )
    if not context_name:
        raise click.ClickException(
            "GitHub webhook signature verification could not resolve a Harbor context from the raw payload."
        )
    secret = resolve_harbor_github_webhook_secret(
        control_plane_root=control_plane_root,
        context_name=context_name,
    )
    if not secret:
        raise click.ClickException(
            f"Runtime environments file is missing GITHUB_WEBHOOK_SECRET for Harbor context {context_name!r}."
        )
    verify_github_webhook_signature(
        payload_bytes=raw_payload_bytes,
        signature_header=signature_256,
        secret=secret,
    )
    return {
        "mode": "verified",
        "verified": True,
        "context": context_name,
    }


def _resolve_harbor_github_webhook_context(*, event_name: str, webhook_payload: dict[str, object]) -> str:
    if event_name.strip() != "pull_request":
        return ""
    repository_payload = webhook_payload.get("repository")
    if not isinstance(repository_payload, dict):
        return ""
    repo_name = repository_payload.get("name")
    if not isinstance(repo_name, str) or not repo_name.strip():
        return ""
    return harbor_anchor_repo_context(repo=repo_name)


@harbor_previews.command("list")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", default="")
def harbor_previews_list(state_dir: Path, context_name: str) -> None:
    payload = build_preview_inventory_payload(
        record_store=_store(state_dir),
        context_name=context_name,
    )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@harbor_previews.command("show")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", required=True)
@click.option("--anchor-repo", required=True)
@click.option("--pr-number", "anchor_pr_number", type=click.IntRange(min=1), required=True)
def harbor_previews_show(
    state_dir: Path,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> None:
    payload = build_preview_status_payload(
        record_store=_store(state_dir),
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    if payload is None:
        raise click.ClickException(
            f"No Harbor preview found for {context_name}/{anchor_repo}/pr-{anchor_pr_number}."
        )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


@harbor_previews.command("history")
@click.option(
    "--state-dir", type=click.Path(path_type=Path), default=Path("state"), show_default=True
)
@click.option("--context", "context_name", required=True)
@click.option("--anchor-repo", required=True)
@click.option("--pr-number", "anchor_pr_number", type=click.IntRange(min=1), required=True)
def harbor_previews_history(
    state_dir: Path,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> None:
    payload = build_preview_history_payload(
        record_store=_store(state_dir),
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    if payload is None:
        raise click.ClickException(
            f"No Harbor preview found for {context_name}/{anchor_repo}/pr-{anchor_pr_number}."
        )
    click.echo(json.dumps(payload, indent=2, sort_keys=True))


def _read_harbor_preview_or_fail(
    *,
    record_store: FilesystemRecordStore,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
):
    preview_record = find_preview_record(
        record_store=record_store,
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    if preview_record is None:
        raise click.ClickException(
            f"No Harbor preview found for {context_name}/{anchor_repo}/pr-{anchor_pr_number}."
        )
    return preview_record


def _read_harbor_generation_or_fail(
    *,
    record_store: FilesystemRecordStore,
    preview_id: str,
    generation_id: str,
):
    generations = record_store.list_preview_generation_records(preview_id=preview_id)
    for generation_record in generations:
        if generation_record.generation_id == generation_id:
            return generation_record
    raise click.ClickException(
        f"No Harbor preview generation found for {preview_id} generation {generation_id}."
    )


def _apply_harbor_request_generation(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    preview_request: PreviewMutationRequest,
    generation_request: PreviewGenerationMutationRequest,
) -> dict[str, object]:
    existing_preview = find_preview_record(
        record_store=record_store,
        context_name=preview_request.context,
        anchor_repo=preview_request.anchor_repo,
        anchor_pr_number=preview_request.anchor_pr_number,
    )
    preview_record = (
        existing_preview.model_copy(
            update={
                "anchor_pr_url": preview_request.anchor_pr_url,
                "updated_at": preview_request.updated_at.strip() or existing_preview.updated_at,
                "eligible_at": preview_request.eligible_at.strip() or existing_preview.eligible_at,
                "paused_at": preview_request.paused_at or existing_preview.paused_at,
                "destroy_after": preview_request.destroy_after or existing_preview.destroy_after,
            }
        )
        if existing_preview is not None
        else build_preview_record_from_request(
            control_plane_root=control_plane_root,
            record_store=record_store,
            request=preview_request,
        )
    )
    record_store.write_preview_record(preview_record)
    generation_record = build_preview_generation_record_from_request(
        record_store=record_store,
        request=generation_request,
    )
    transitioned_preview = apply_generation_requested_transition(
        preview=preview_record,
        generation=generation_record,
    )
    generation_path = record_store.write_preview_generation_record(generation_record)
    preview_path = record_store.write_preview_record(transitioned_preview)
    return {
        "generation_id": generation_record.generation_id,
        "generation_path": str(generation_path),
        "preview_id": transitioned_preview.preview_id,
        "preview_path": str(preview_path),
    }


def _apply_harbor_destroy_preview(
    *,
    record_store: FilesystemRecordStore,
    request: PreviewDestroyMutationRequest,
) -> dict[str, object]:
    preview_record = _read_harbor_preview_or_fail(
        record_store=record_store,
        context_name=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
    )
    transitioned_preview = apply_preview_destroyed_transition(
        preview=preview_record,
        destroyed_at=request.destroyed_at,
        destroy_reason=request.destroy_reason,
    )
    preview_path = record_store.write_preview_record(transitioned_preview)
    return {
        "preview_id": transitioned_preview.preview_id,
        "preview_path": str(preview_path),
    }


def _apply_harbor_pr_event_intent(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    payload: dict[str, object],
) -> dict[str, object]:
    mutation_payload = payload.get("mutation")
    if not isinstance(mutation_payload, dict):
        return {
            "applied": False,
            "reason": "no_mutation_intent",
        }
    intent = HarborPullRequestMutationIntent.model_validate(mutation_payload)
    if intent.command == "request-generation":
        if intent.preview_request is None:
            raise click.ClickException("Resolved Harbor PR-event request-generation intent is missing preview_request.")
        if intent.generation_request is None:
            return {
                "applied": False,
                "reason": "manifest_resolution_required",
            }
        result_payload = _apply_harbor_request_generation(
            control_plane_root=control_plane_root,
            record_store=record_store,
            preview_request=intent.preview_request,
            generation_request=intent.generation_request,
        )
        return {
            "applied": True,
            "command": intent.command,
            "result": result_payload,
        }
    if intent.destroy_request is None:
        raise click.ClickException("Resolved Harbor PR-event destroy intent is missing destroy_request.")
    result_payload = _apply_harbor_destroy_preview(
        record_store=record_store,
        request=intent.destroy_request,
    )
    return {
        "applied": True,
        "command": intent.command,
        "result": result_payload,
    }


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
