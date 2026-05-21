from collections.abc import Callable
from dataclasses import dataclass
import json
from pathlib import Path
import subprocess
from typing import Protocol

import click

from control_plane import dokploy as control_plane_dokploy
from control_plane import live_target_runtime as control_plane_live_target_runtime
from control_plane import release_tuples as control_plane_release_tuples
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.promotion_record import PostDeployUpdateEvidence
from control_plane.contracts.ship_request import ShipRequest
from control_plane.workflows.runtime_identity_health import wait_for_healthcheck_with_retry
from control_plane.workflows.ship import build_deployment_record
from control_plane.workflows.ship import generate_deployment_record_id
from control_plane.workflows.ship import utc_now_timestamp


class ShipExecutionRecordStore(Protocol):
    def write_deployment_record(self, record: DeploymentRecord) -> Path | None: ...


@dataclass(frozen=True)
class ShipExecutionCallbacks:
    require_artifact_id: Callable[..., str]
    read_artifact_manifest: Callable[..., ArtifactIdentityManifest]
    resolve_artifact_native_execution_request: Callable[..., ShipRequest]
    resolve_dokploy_target: Callable[..., tuple[ResolvedTargetEvidence, int]]
    sync_artifact_image_reference_for_target: Callable[..., dict[str, str]]
    execute_dokploy_deploy: Callable[..., None]
    run_compose_post_deploy_update: Callable[..., None]
    skipped_destination_health: Callable[..., HealthcheckEvidence]
    verify_ship_healthchecks: Callable[..., None]
    write_environment_inventory: Callable[..., object]
    write_release_tuple_from_deployment: Callable[..., object]


def verify_ship_healthchecks(
    *,
    request: ShipRequest,
    wait_for_healthcheck: Callable[..., None],
) -> None:
    if not request.wait or not request.verify_health:
        return
    if not request.destination_health.urls:
        raise click.ClickException(
            "Healthcheck verification requested but no target domain/URL was resolved. "
            "Define domains in the tracked Dokploy target record or disable with --no-verify-health."
        )
    if request.destination_health.timeout_seconds is None:
        raise click.ClickException("Healthcheck verification requested without timeout_seconds.")
    healthcheck_errors: list[str] = []
    for healthcheck_url in request.destination_health.urls:
        try:
            wait_for_healthcheck(
                url=healthcheck_url, timeout_seconds=request.destination_health.timeout_seconds
            )
            return
        except click.ClickException as error:
            healthcheck_errors.append(str(error))
    if healthcheck_errors:
        raise click.ClickException(
            "Healthcheck verification failed for all resolved URLs:\n"
            + "\n".join(healthcheck_errors)
        )


def wait_for_ship_healthcheck(
    *,
    url: str,
    timeout_seconds: int,
    sleep: Callable[[float], None],
    monotonic: Callable[[], float],
) -> None:
    wait_for_healthcheck_with_retry(
        url=url,
        timeout_seconds=timeout_seconds,
        sleep=sleep,
        monotonic=monotonic,
    )


def execute_dokploy_deploy(
    *,
    host: str,
    token: str,
    request: ShipRequest,
    resolved_target: ResolvedTargetEvidence,
    deploy_timeout_seconds: int,
) -> None:
    if request.wait:
        control_plane_live_target_runtime.trigger_and_wait_for_dokploy_target_deploy(
            host=host,
            token=token,
            target_type=resolved_target.target_type,
            target_id=resolved_target.target_id,
            deploy_timeout_seconds=deploy_timeout_seconds,
            no_cache=request.no_cache,
        )
        return
    control_plane_dokploy.trigger_deployment(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        no_cache=request.no_cache,
    )


def execute_ship(
    *,
    record_store: ShipExecutionRecordStore,
    env_file: Path | None,
    request: ShipRequest,
    mint_release_tuple: bool,
    callbacks: ShipExecutionCallbacks,
) -> tuple[Path | None, DeploymentRecord | ShipRequest]:
    resolved_artifact_id = callbacks.require_artifact_id(requested_artifact_id=request.artifact_id)
    artifact_manifest = callbacks.read_artifact_manifest(
        record_store=record_store,
        artifact_id=resolved_artifact_id,
    )
    resolved_request = callbacks.resolve_artifact_native_execution_request(
        request=request,
        artifact_id=resolved_artifact_id,
        artifact_manifest=artifact_manifest,
    )
    if mint_release_tuple and control_plane_release_tuples.should_mint_release_tuple_for_channel(
        resolved_request.instance
    ):
        control_plane_release_tuples.repo_shas_from_artifact_manifest(
            context_name=resolved_request.context,
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
        resolved_target, deploy_timeout_seconds = callbacks.resolve_dokploy_target(
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

    runtime_source_evidence: dict[str, str] = {}
    try:
        runtime_source_evidence = callbacks.sync_artifact_image_reference_for_target(
            context_name=resolved_request.context,
            instance_name=resolved_request.instance,
            artifact_manifest=artifact_manifest,
            resolved_target=resolved_target,
        )
        callbacks.execute_dokploy_deploy(
            request=resolved_request,
            resolved_target=resolved_target,
            deploy_timeout_seconds=deploy_timeout_seconds,
        )
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="fail",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            resolved_target=resolved_target,
            runtime_source=runtime_source_evidence,
        )
        record_store.write_deployment_record(final_record)
        raise

    try:
        if resolved_request.wait and resolved_target.target_type == "compose":
            callbacks.run_compose_post_deploy_update(
                env_file=env_file,
                request=resolved_request,
            )
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="pass",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            resolved_target=resolved_target,
            runtime_source=runtime_source_evidence,
            post_deploy_update=PostDeployUpdateEvidence(
                attempted=True,
                status="fail",
                detail=(
                    "Odoo-specific post-deploy update failed through the native "
                    "control-plane Dokploy schedule workflow."
                ),
            ),
            destination_health=callbacks.skipped_destination_health(resolved_request),
        )
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
        callbacks.verify_ship_healthchecks(request=resolved_request)
        final_record = build_deployment_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="pass",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            resolved_target=resolved_target,
            runtime_source=runtime_source_evidence,
            post_deploy_update=post_deploy_update_evidence,
        )
    except (subprocess.CalledProcessError, click.ClickException):
        final_record = build_deployment_record(
            request=resolved_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="pass",
            started_at=started_at,
            finished_at=utc_now_timestamp(),
            resolved_target=resolved_target,
            runtime_source=runtime_source_evidence,
            post_deploy_update=post_deploy_update_evidence,
            destination_health=callbacks.skipped_destination_health(
                resolved_request, detail_status="fail"
            ),
        )
        record_store.write_deployment_record(final_record)
        raise

    record_store.write_deployment_record(final_record)
    if final_record.wait_for_completion and final_record.deploy.status == "pass":
        callbacks.write_environment_inventory(
            record_store=record_store,
            deployment_record=final_record,
        )
        if mint_release_tuple:
            callbacks.write_release_tuple_from_deployment(
                record_store=record_store,
                deployment_record=final_record,
                artifact_manifest=artifact_manifest,
            )
    return record_path, final_record
