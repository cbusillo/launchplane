from __future__ import annotations

from pathlib import Path
from typing import Protocol

import click
from pydantic import BaseModel, ConfigDict, Field

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.deploy_target import DeployedTargetReference
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.ship_request import ShipRequest
from control_plane.workflows.dokploy_deploy import execute_dokploy_artifact_deploy


class GenericWebResolvedDeployTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    ship_request: ShipRequest
    resolved_target: ResolvedTargetEvidence
    deployed_target: DeployedTargetReference | None = None
    deploy_timeout_seconds: int = Field(ge=1)


class GenericWebDeployProvider(Protocol):
    provider_id: str
    delegated_executor: str

    def resolve_deploy_target(
        self,
        *,
        control_plane_root: Path,
        request_artifact_id: str,
        request_source_git_ref: str,
        request_timeout_seconds: int | None,
        request_no_cache: bool,
        profile: LaunchplaneProductProfileRecord,
        lane: ProductLaneProfile,
        normalized_artifact_id: str,
        fallback_target_name: str,
    ) -> GenericWebResolvedDeployTarget: ...

    def execute_artifact_deploy(
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        runtime_identity: RuntimeIdentity,
    ) -> None: ...


class DokployGenericWebDeployProvider:
    provider_id = "dokploy"
    delegated_executor = "control-plane.dokploy"

    def resolve_deploy_target(
        self,
        *,
        control_plane_root: Path,
        request_artifact_id: str,
        request_source_git_ref: str,
        request_timeout_seconds: int | None,
        request_no_cache: bool,
        profile: LaunchplaneProductProfileRecord,
        lane: ProductLaneProfile,
        normalized_artifact_id: str,
        fallback_target_name: str,
    ) -> GenericWebResolvedDeployTarget:
        del request_artifact_id, profile
        source_of_truth = control_plane_dokploy.read_control_plane_dokploy_source_of_truth(
            control_plane_root=control_plane_root,
        )
        target_definition = control_plane_dokploy.find_dokploy_target_definition(
            source_of_truth,
            context_name=lane.context,
            instance_name=lane.instance,
        )
        if target_definition is None:
            raise click.ClickException(
                f"No Dokploy target definition found for {lane.context}/{lane.instance}."
            )

        environment_values = (
            control_plane_runtime_environments.resolve_runtime_environment_values(
                control_plane_root=control_plane_root,
                context_name=lane.context,
                instance_name=lane.instance,
            )
        )
        configured_ship_mode = control_plane_dokploy.resolve_dokploy_ship_mode(
            lane.context,
            lane.instance,
            environment_values,
        )
        deploy_mode = _resolve_dokploy_deploy_mode(
            configured_ship_mode=configured_ship_mode,
            target_type=target_definition.target_type,
        )
        target_name = target_definition.target_name.strip() or fallback_target_name
        ship_request = ShipRequest(
            artifact_id=normalized_artifact_id,
            context=lane.context,
            instance=lane.instance,
            source_git_ref=request_source_git_ref,
            target_name=target_name,
            target_type=target_definition.target_type,
            deploy_mode=deploy_mode,
            provider_id=self.provider_id,
            target_category=target_definition.target_type,
            provider_target_type=target_definition.target_type,
            provider_deploy_mode=deploy_mode,
            wait=True,
            timeout_seconds=request_timeout_seconds,
            verify_health=False,
            no_cache=request_no_cache,
            destination_health=HealthcheckEvidence(status="skipped"),
        )
        resolved_target = ResolvedTargetEvidence(
            target_type=target_definition.target_type,
            target_id=target_definition.target_id,
            target_name=target_name,
        )
        deployed_target = DeployedTargetReference(
            provider_id=self.provider_id,
            target_category=target_definition.target_type,
            target_id=target_definition.target_id,
            display_name=target_name,
            provider_target_type=target_definition.target_type,
        )
        deploy_timeout_seconds = control_plane_dokploy.resolve_ship_timeout_seconds(
            timeout_override_seconds=request_timeout_seconds,
            target_definition=target_definition,
        )
        return GenericWebResolvedDeployTarget(
            ship_request=ship_request,
            resolved_target=resolved_target,
            deployed_target=deployed_target,
            deploy_timeout_seconds=deploy_timeout_seconds,
        )

    def execute_artifact_deploy(
        self,
        *,
        control_plane_root: Path,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        runtime_identity: RuntimeIdentity,
    ) -> None:
        host, token = control_plane_dokploy.read_dokploy_config(
            control_plane_root=control_plane_root
        )
        execute_dokploy_artifact_deploy(
            host=host,
            token=token,
            ship_request=resolved_deploy_target.ship_request,
            resolved_target=resolved_deploy_target.resolved_target,
            deploy_timeout_seconds=resolved_deploy_target.deploy_timeout_seconds,
            runtime_identity=runtime_identity,
        )


def _resolve_dokploy_deploy_mode(*, configured_ship_mode: str, target_type: str) -> str:
    if configured_ship_mode == "auto":
        return f"dokploy-{target_type}-api"
    return f"dokploy-{configured_ship_mode}-api"


def default_generic_web_deploy_provider() -> GenericWebDeployProvider:
    return DokployGenericWebDeployProvider()
