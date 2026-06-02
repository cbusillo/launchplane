from pathlib import Path

import click

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.promotion_record import BackupGateEvidence, HealthcheckEvidence
from control_plane.contracts.promotion_record import PromotionRequest, ReleaseStatus
from control_plane.contracts.ship_request import ShipRequest

DEFAULT_DOKPLOY_SHIP_SOURCE_GIT_REF = "origin/main"


def resolve_deploy_mode(*, configured_ship_mode: str, target_type: str) -> str:
    if configured_ship_mode == "auto":
        return f"dokploy-{target_type}-api"
    return f"dokploy-{configured_ship_mode}-api"


def require_dokploy_target_definition(
    *,
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
            f"{operation_name} target {context_name}/{instance_name} is missing from the DB-backed tracked Dokploy targets."
        )
    return target_definition


def resolve_native_ship_request(
    *,
    control_plane_root: Path,
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
    environment_values: dict[str, str] | None = None,
) -> ShipRequest:
    normalized_artifact_id = artifact_id.strip()
    if not normalized_artifact_id:
        raise click.ClickException("ship request requires artifact_id")

    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    target_definition = require_dokploy_target_definition(
        source_of_truth=source_of_truth,
        context_name=context_name,
        instance_name=instance_name,
        operation_name="Ship",
    )

    resolved_environment_values = environment_values or {}
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
        environment_values=resolved_environment_values,
    )
    should_verify_health = verify_health and wait
    if should_verify_health and not destination_healthcheck_urls:
        raise click.ClickException(
            "Healthcheck verification requested but no target domain/URL was resolved. "
            "Define domains in the tracked Dokploy target record or disable with --no-verify-health."
        )

    configured_ship_mode = control_plane_dokploy.resolve_dokploy_ship_mode(
        context_name,
        instance_name,
        resolved_environment_values,
    )
    deploy_mode = resolve_deploy_mode(
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


def resolve_ship_request_for_promotion(
    *,
    control_plane_root: Path,
    request: PromotionRequest,
    environment_values: dict[str, str] | None = None,
) -> ShipRequest:
    ship_request = resolve_native_ship_request(
        control_plane_root=control_plane_root,
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
        environment_values=environment_values,
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
    if request.provider_id != ship_request.provider_id:
        raise click.ClickException(
            "Promotion request provider_id does not match resolved ship provider_id. "
            f"Request={request.provider_id} configured={ship_request.provider_id}."
        )
    if request.target_category != ship_request.target_category:
        raise click.ClickException(
            "Promotion request target_category does not match resolved ship target_category. "
            f"Request={request.target_category} configured={ship_request.target_category}."
        )
    if request.provider_target_type != ship_request.provider_target_type:
        raise click.ClickException(
            "Promotion request provider_target_type does not match resolved ship provider_target_type. "
            f"Request={request.provider_target_type} configured={ship_request.provider_target_type}."
        )
    if request.provider_deploy_mode != ship_request.provider_deploy_mode:
        raise click.ClickException(
            "Promotion request provider_deploy_mode does not match resolved ship provider_deploy_mode. "
            f"Request={request.provider_deploy_mode} configured={ship_request.provider_deploy_mode}."
        )
    return ship_request


def resolve_native_promotion_request(
    *,
    control_plane_root: Path,
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
    source_environment_values: dict[str, str] | None = None,
    destination_environment_values: dict[str, str] | None = None,
) -> PromotionRequest:
    normalized_artifact_id = artifact_id.strip()
    if not normalized_artifact_id:
        raise click.ClickException("promotion request requires artifact_id")
    normalized_backup_record_id = backup_record_id.strip()
    if not normalized_backup_record_id:
        raise click.ClickException("promotion request requires backup_record_id")

    source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
        control_plane_root=control_plane_root,
    )
    source_target_definition = require_dokploy_target_definition(
        source_of_truth=source_of_truth,
        context_name=context_name,
        instance_name=from_instance_name,
        operation_name="Promotion source",
    )
    destination_target_definition = require_dokploy_target_definition(
        source_of_truth=source_of_truth,
        context_name=context_name,
        instance_name=to_instance_name,
        operation_name="Promotion destination",
    )

    resolved_source_environment_values = source_environment_values or {}
    resolved_destination_environment_values = destination_environment_values or {}
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
        environment_values=resolved_source_environment_values,
    )
    source_health_status: ReleaseStatus = "pending" if source_healthcheck_urls else "skipped"
    destination_health_timeout_seconds = control_plane_dokploy.resolve_ship_health_timeout_seconds(
        health_timeout_override_seconds=health_timeout_override_seconds,
        target_definition=destination_target_definition,
    )
    destination_healthcheck_urls = control_plane_dokploy.resolve_ship_healthcheck_urls(
        target_definition=destination_target_definition,
        environment_values=resolved_destination_environment_values,
    )
    should_verify_destination_health = verify_health and wait
    if should_verify_destination_health and not destination_healthcheck_urls:
        raise click.ClickException(
            "Healthcheck verification requested but no target domain/URL was resolved. "
            "Define domains in the DB-backed target record or disable with --no-verify-health."
        )
    configured_ship_mode = control_plane_dokploy.resolve_dokploy_ship_mode(
        context_name,
        to_instance_name,
        resolved_destination_environment_values,
    )
    deploy_mode = resolve_deploy_mode(
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
