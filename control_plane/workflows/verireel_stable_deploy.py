from __future__ import annotations

from pathlib import Path
from typing import Literal

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.ship_request import ShipRequest
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.workflows.ship import build_deployment_record, generate_deployment_record_id, utc_now_timestamp


StableInstanceName = Literal["testing", "prod"]


class VeriReelStableDeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str = "verireel"
    instance: StableInstanceName = "testing"
    artifact_id: str
    source_git_ref: str
    timeout_seconds: int | None = Field(default=None, ge=1)
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelStableDeployRequest":
        if self.context != "verireel":
            raise ValueError("VeriReel stable deploy requires context 'verireel'.")
        if self.instance not in {"testing", "prod"}:
            raise ValueError("VeriReel stable deploy requires instance 'testing' or 'prod'.")
        if not self.artifact_id.strip():
            raise ValueError("VeriReel stable deploy requires artifact_id.")
        if not self.source_git_ref.strip():
            raise ValueError("VeriReel stable deploy requires source_git_ref.")
        return self


class VeriReelStableDeployResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_record_id: str
    deploy_status: Literal["pass", "fail"]
    deploy_started_at: str
    deploy_finished_at: str
    target_name: str
    target_type: str
    target_id: str
    error_message: str = ""


def _resolve_deploy_mode(*, configured_ship_mode: str, target_type: str) -> str:
    if configured_ship_mode == "auto":
        return f"dokploy-{target_type}-api"
    return f"dokploy-{configured_ship_mode}-api"


def _default_target_name_for_instance(instance_name: StableInstanceName) -> str:
    return {
        "testing": "ver-testing-app",
        "prod": "ver-prod-app",
    }[instance_name]


def _fallback_ship_request(request: VeriReelStableDeployRequest) -> ShipRequest:
    return ShipRequest(
        artifact_id=request.artifact_id,
        context=request.context,
        instance=request.instance,
        source_git_ref=request.source_git_ref,
        target_name=_default_target_name_for_instance(request.instance),
        target_type="application",
        deploy_mode="dokploy-application-api",
        wait=True,
        timeout_seconds=request.timeout_seconds,
        verify_health=False,
        no_cache=request.no_cache,
        destination_health=HealthcheckEvidence(status="skipped"),
    )


def _resolve_ship_request(
    *,
    control_plane_root: Path,
    request: VeriReelStableDeployRequest,
) -> tuple[ShipRequest, ResolvedTargetEvidence, int]:
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
            f"No Dokploy target definition found for {request.context}/{request.instance}."
        )

    environment_values = control_plane_dokploy.read_control_plane_environment_values(
        control_plane_root=control_plane_root,
    )
    deploy_mode = _resolve_deploy_mode(
        configured_ship_mode=control_plane_dokploy.resolve_dokploy_ship_mode(
            request.context,
            request.instance,
            environment_values,
        ),
        target_type=target_definition.target_type,
    )
    ship_request = ShipRequest(
        artifact_id=request.artifact_id,
        context=request.context,
        instance=request.instance,
        source_git_ref=request.source_git_ref,
        target_name=target_definition.target_name.strip() or _default_target_name_for_instance(request.instance),
        target_type=target_definition.target_type,
        deploy_mode=deploy_mode,
        wait=True,
        timeout_seconds=request.timeout_seconds,
        verify_health=False,
        no_cache=request.no_cache,
        destination_health=HealthcheckEvidence(status="skipped"),
    )
    resolved_target = ResolvedTargetEvidence(
        target_type=target_definition.target_type,
        target_id=target_definition.target_id,
        target_name=target_definition.target_name.strip() or ship_request.target_name,
    )
    deploy_timeout_seconds = control_plane_dokploy.resolve_ship_timeout_seconds(
        timeout_override_seconds=request.timeout_seconds,
        target_definition=target_definition,
    )
    return ship_request, resolved_target, deploy_timeout_seconds


def _execute_dokploy_deploy(
    *,
    control_plane_root: Path,
    ship_request: ShipRequest,
    resolved_target: ResolvedTargetEvidence,
    deploy_timeout_seconds: int,
) -> None:
    host, token = control_plane_dokploy.read_dokploy_config(control_plane_root=control_plane_root)
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
        no_cache=ship_request.no_cache,
    )
    control_plane_dokploy.wait_for_target_deployment(
        host=host,
        token=token,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        before_key=control_plane_dokploy.deployment_key(latest_before),
        timeout_seconds=deploy_timeout_seconds,
    )


def execute_verireel_stable_deploy(
    *,
    control_plane_root: Path,
    record_store: FilesystemRecordStore,
    request: VeriReelStableDeployRequest,
) -> VeriReelStableDeployResult:
    record_id = generate_deployment_record_id(
        context_name=request.context,
        instance_name=request.instance,
    )
    started_at = utc_now_timestamp()
    fallback_request = _fallback_ship_request(request)

    try:
        ship_request, resolved_target, deploy_timeout_seconds = _resolve_ship_request(
            control_plane_root=control_plane_root,
            request=request,
        )
    except click.ClickException as exc:
        finished_at = utc_now_timestamp()
        record_store.write_deployment_record(
            build_deployment_record(
                request=fallback_request,
                record_id=record_id,
                deployment_id="control-plane-dokploy",
                deployment_status="fail",
                started_at=started_at,
                finished_at=finished_at,
            )
        )
        return VeriReelStableDeployResult(
            deployment_record_id=record_id,
            deploy_status="fail",
            deploy_started_at=started_at,
            deploy_finished_at=finished_at,
            target_name=fallback_request.target_name,
            target_type=fallback_request.target_type,
            target_id="",
            error_message=str(exc),
        )

    record_store.write_deployment_record(
        build_deployment_record(
            request=ship_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="pending",
            started_at=started_at,
            finished_at="",
            resolved_target=resolved_target,
        )
    )

    try:
        _execute_dokploy_deploy(
            control_plane_root=control_plane_root,
            ship_request=ship_request,
            resolved_target=resolved_target,
            deploy_timeout_seconds=deploy_timeout_seconds,
        )
    except click.ClickException as exc:
        finished_at = utc_now_timestamp()
        record_store.write_deployment_record(
            build_deployment_record(
                request=ship_request,
                record_id=record_id,
                deployment_id="control-plane-dokploy",
                deployment_status="fail",
                started_at=started_at,
                finished_at=finished_at,
                resolved_target=resolved_target,
            )
        )
        return VeriReelStableDeployResult(
            deployment_record_id=record_id,
            deploy_status="fail",
            deploy_started_at=started_at,
            deploy_finished_at=finished_at,
            target_name=resolved_target.target_name,
            target_type=resolved_target.target_type,
            target_id=resolved_target.target_id,
            error_message=str(exc),
        )

    finished_at = utc_now_timestamp()
    record_store.write_deployment_record(
        build_deployment_record(
            request=ship_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status="pass",
            started_at=started_at,
            finished_at=finished_at,
            resolved_target=resolved_target,
        )
    )
    return VeriReelStableDeployResult(
        deployment_record_id=record_id,
        deploy_status="pass",
        deploy_started_at=started_at,
        deploy_finished_at=finished_at,
        target_name=resolved_target.target_name,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
    )
