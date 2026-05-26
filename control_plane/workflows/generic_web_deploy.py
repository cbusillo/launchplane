from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal, Protocol

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane import dokploy as control_plane_dokploy
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.deployment_record import DeploymentRecord, ResolvedTargetEvidence
from control_plane.contracts.dokploy_target_record import DokployTargetType
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import HealthcheckEvidence, PostDeployUpdateEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.ship_request import ShipRequest
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.workflows.dokploy_deploy import execute_dokploy_artifact_deploy
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.ship import (
    build_deployment_record,
    generate_deployment_record_id,
    utc_now_timestamp,
)


class GenericWebDeployStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def write_deployment_record(self, record: DeploymentRecord) -> object: ...

    def write_environment_inventory(self, record: EnvironmentInventory) -> object: ...


class GenericWebDeployRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    product: str
    instance: str
    artifact_id: str
    source_git_ref: str
    timeout_seconds: int | None = Field(default=None, ge=1)
    no_cache: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebDeployRequest":
        if not self.product.strip():
            raise ValueError("Generic web deploy requires product.")
        if not self.instance.strip():
            raise ValueError("Generic web deploy requires instance.")
        if not self.artifact_id.strip():
            raise ValueError("Generic web deploy requires artifact_id.")
        if not self.source_git_ref.strip():
            raise ValueError("Generic web deploy requires source_git_ref.")
        return self


class GenericWebDeployResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_record_id: str
    deploy_status: Literal["pass", "fail"]
    deploy_started_at: str
    deploy_finished_at: str
    product: str
    context: str
    instance: str
    target_name: str = ""
    target_type: DokployTargetType | Literal[""] = ""
    target_id: str = ""
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
    error_message: str = ""


class GenericWebPostDeployContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: str
    deployment_record_id: str
    target_name: str
    target_type: DokployTargetType
    target_id: str
    artifact_id: str
    source_git_ref: str

    @model_validator(mode="after")
    def _validate_context(self) -> "GenericWebPostDeployContext":
        if not self.product.strip():
            raise ValueError("generic web post-deploy context requires product")
        if not self.context.strip():
            raise ValueError("generic web post-deploy context requires context")
        if not self.instance.strip():
            raise ValueError("generic web post-deploy context requires instance")
        if not self.deployment_record_id.strip():
            raise ValueError("generic web post-deploy context requires deployment_record_id")
        if not self.target_name.strip():
            raise ValueError("generic web post-deploy context requires target_name")
        if not self.target_id.strip():
            raise ValueError("generic web post-deploy context requires target_id")
        if not self.artifact_id.strip():
            raise ValueError("generic web post-deploy context requires artifact_id")
        if not self.source_git_ref.strip():
            raise ValueError("generic web post-deploy context requires source_git_ref")
        return self


GenericWebPostDeployExecutor = Callable[
    [Path, GenericWebDeployStore, GenericWebPostDeployContext], PostDeployUpdateEvidence
]


def resolve_generic_web_profile_lane(
    *, record_store: GenericWebDeployStore, request: GenericWebDeployRequest
) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile]:
    profile = record_store.read_product_profile_record(request.product)
    if not product_profile_uses_generic_web_base(profile):
        raise click.ClickException(
            f"Product {profile.product!r} is configured for driver {profile.driver_id!r}, "
            "not generic-web or a generic-web based driver."
        )
    for lane in profile.lanes:
        if lane.instance == request.instance:
            return profile, lane
    raise click.ClickException(
        f"Product {profile.product!r} has no generic-web lane for instance {request.instance!r}."
    )


def product_profile_uses_generic_web_base(profile: LaunchplaneProductProfileRecord) -> bool:
    driver_id = profile.driver_id.strip()
    if driver_id == "generic-web":
        return True
    try:
        return read_driver_descriptor(driver_id).base_driver_id == "generic-web"
    except FileNotFoundError:
        return False


def _resolve_deploy_mode(*, configured_ship_mode: str, target_type: DokployTargetType) -> str:
    if configured_ship_mode == "auto":
        return f"dokploy-{target_type}-api"
    return f"dokploy-{configured_ship_mode}-api"


def _fallback_target_name(
    *, profile: LaunchplaneProductProfileRecord, lane: ProductLaneProfile
) -> str:
    return f"{profile.product}-{lane.instance}"


def normalize_generic_web_artifact_id(
    *, profile: LaunchplaneProductProfileRecord, artifact_id: str
) -> str:
    normalized_artifact_id = artifact_id.strip()
    image_repository = profile.image.repository.strip().rstrip("/")
    if not normalized_artifact_id:
        raise click.ClickException("Generic web deploy requires artifact_id.")
    if normalized_artifact_id.startswith(
        f"{image_repository}@"
    ) or normalized_artifact_id.startswith(f"{image_repository}:"):
        return normalized_artifact_id
    if normalized_artifact_id.startswith("sha256:"):
        return f"{image_repository}@{normalized_artifact_id}"
    if "/" in normalized_artifact_id or "@" in normalized_artifact_id:
        raise click.ClickException(
            "Generic web artifact_id must use the product image repository "
            f"{image_repository!r}, or be a bare image tag."
        )
    return f"{image_repository}:{normalized_artifact_id}"


def _build_runtime_identity(
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    ship_request: ShipRequest,
    deployment_record_id: str,
    deployed_at: str = "",
) -> RuntimeIdentity:
    return RuntimeIdentity(
        product=profile.product,
        context=lane.context,
        instance=lane.instance,
        environment_kind="stable",
        deployment_record_id=deployment_record_id,
        artifact_id=ship_request.artifact_id,
        source_git_ref=ship_request.source_git_ref,
        image_reference=ship_request.artifact_id,
        deployed_at=deployed_at,
    )


def _fallback_ship_request(
    *,
    request: GenericWebDeployRequest,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
) -> ShipRequest:
    return ShipRequest(
        artifact_id=normalize_generic_web_artifact_id(
            profile=profile,
            artifact_id=request.artifact_id,
        ),
        context=lane.context,
        instance=lane.instance,
        source_git_ref=request.source_git_ref,
        target_name=_fallback_target_name(profile=profile, lane=lane),
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
    request: GenericWebDeployRequest,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
) -> tuple[ShipRequest, ResolvedTargetEvidence, int]:
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

    environment_values = control_plane_runtime_environments.resolve_runtime_environment_values(
        control_plane_root=control_plane_root,
        context_name=lane.context,
        instance_name=lane.instance,
    )
    deploy_mode = _resolve_deploy_mode(
        configured_ship_mode=control_plane_dokploy.resolve_dokploy_ship_mode(
            lane.context,
            lane.instance,
            environment_values,
        ),
        target_type=target_definition.target_type,
    )
    target_name = target_definition.target_name.strip() or _fallback_target_name(
        profile=profile, lane=lane
    )
    ship_request = ShipRequest(
        artifact_id=normalize_generic_web_artifact_id(
            profile=profile,
            artifact_id=request.artifact_id,
        ),
        context=lane.context,
        instance=lane.instance,
        source_git_ref=request.source_git_ref,
        target_name=target_name,
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
        target_name=target_name,
    )
    deploy_timeout_seconds = control_plane_dokploy.resolve_ship_timeout_seconds(
        timeout_override_seconds=request.timeout_seconds,
        target_definition=target_definition,
    )
    return ship_request, resolved_target, deploy_timeout_seconds


def execute_generic_web_deploy(
    *,
    control_plane_root: Path,
    record_store: GenericWebDeployStore,
    request: GenericWebDeployRequest,
    profile: LaunchplaneProductProfileRecord | None = None,
    lane: ProductLaneProfile | None = None,
    post_deploy_executor: GenericWebPostDeployExecutor | None = None,
) -> GenericWebDeployResult:
    resolved_profile = profile
    resolved_lane = lane
    if resolved_profile is None or resolved_lane is None:
        resolved_profile, resolved_lane = resolve_generic_web_profile_lane(
            record_store=record_store,
            request=request,
        )

    record_id = generate_deployment_record_id(
        context_name=resolved_lane.context,
        instance_name=resolved_lane.instance,
    )
    started_at = utc_now_timestamp()
    fallback_request = _fallback_ship_request(
        request=request,
        profile=resolved_profile,
        lane=resolved_lane,
    )

    try:
        ship_request, resolved_target, deploy_timeout_seconds = _resolve_ship_request(
            control_plane_root=control_plane_root,
            request=request,
            profile=resolved_profile,
            lane=resolved_lane,
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
        return GenericWebDeployResult(
            deployment_record_id=record_id,
            deploy_status="fail",
            deploy_started_at=started_at,
            deploy_finished_at=finished_at,
            product=resolved_profile.product,
            context=resolved_lane.context,
            instance=resolved_lane.instance,
            error_message=str(exc),
        )

    deploy_completed = False
    post_deploy_update = PostDeployUpdateEvidence()
    try:
        host, token = control_plane_dokploy.read_dokploy_config(
            control_plane_root=control_plane_root
        )
        execute_dokploy_artifact_deploy(
            host=host,
            token=token,
            ship_request=ship_request,
            resolved_target=resolved_target,
            deploy_timeout_seconds=deploy_timeout_seconds,
            runtime_identity=_build_runtime_identity(
                profile=resolved_profile,
                lane=resolved_lane,
                ship_request=ship_request,
                deployment_record_id=record_id,
            ),
        )
        deploy_completed = True
        if post_deploy_executor is not None:
            post_deploy_update = _terminal_post_deploy_update(
                post_deploy_executor(
                    control_plane_root,
                    record_store,
                    GenericWebPostDeployContext(
                        product=resolved_profile.product,
                        context=resolved_lane.context,
                        instance=resolved_lane.instance,
                        deployment_record_id=record_id,
                        target_name=resolved_target.target_name,
                        target_type=resolved_target.target_type,
                        target_id=resolved_target.target_id,
                        artifact_id=ship_request.artifact_id,
                        source_git_ref=ship_request.source_git_ref,
                    ),
                )
            )
            if post_deploy_update.status == "fail":
                raise click.ClickException(
                    post_deploy_update.detail or "Generic web post-deploy extension failed."
                )
    except click.ClickException as exc:
        finished_at = utc_now_timestamp()
        deployment_status: Literal["pass", "fail"] = "pass" if deploy_completed else "fail"
        if deploy_completed and post_deploy_update.status == "skipped":
            post_deploy_update = PostDeployUpdateEvidence(
                attempted=True,
                status="fail",
                detail=str(exc),
            )
        record_store.write_deployment_record(
            build_deployment_record(
                request=ship_request,
                record_id=record_id,
                deployment_id="control-plane-dokploy",
                deployment_status=deployment_status,
                started_at=started_at,
                finished_at=finished_at,
                resolved_target=resolved_target,
                post_deploy_update=post_deploy_update,
            )
        )
        return GenericWebDeployResult(
            deployment_record_id=record_id,
            deploy_status=deployment_status,
            deploy_started_at=started_at,
            deploy_finished_at=finished_at,
            product=resolved_profile.product,
            context=resolved_lane.context,
            instance=resolved_lane.instance,
            target_name=resolved_target.target_name,
            target_type=resolved_target.target_type,
            target_id=resolved_target.target_id,
            post_deploy_status=_generic_web_deploy_post_deploy_status(post_deploy_update),
            error_message=str(exc),
        )

    finished_at = utc_now_timestamp()
    deployment_record = build_deployment_record(
        request=ship_request,
        record_id=record_id,
        deployment_id="control-plane-dokploy",
        deployment_status="pass",
        started_at=started_at,
        finished_at=finished_at,
        resolved_target=resolved_target,
        post_deploy_update=post_deploy_update,
        runtime_identity=_build_runtime_identity(
            profile=resolved_profile,
            lane=resolved_lane,
            ship_request=ship_request,
            deployment_record_id=record_id,
            deployed_at=finished_at,
        ),
    )
    record_store.write_deployment_record(deployment_record)
    record_store.write_environment_inventory(
        build_environment_inventory(
            deployment_record=deployment_record,
            updated_at=finished_at,
        )
    )
    return GenericWebDeployResult(
        deployment_record_id=record_id,
        deploy_status="pass",
        deploy_started_at=started_at,
        deploy_finished_at=finished_at,
        product=resolved_profile.product,
        context=resolved_lane.context,
        instance=resolved_lane.instance,
        target_name=resolved_target.target_name,
        target_type=resolved_target.target_type,
        target_id=resolved_target.target_id,
        post_deploy_status=_generic_web_deploy_post_deploy_status(post_deploy_update),
    )


def _generic_web_deploy_post_deploy_status(
    post_deploy_update: PostDeployUpdateEvidence,
) -> Literal["pass", "fail", "skipped"]:
    if post_deploy_update.status == "pending":
        return "fail"
    return post_deploy_update.status


def _terminal_post_deploy_update(
    post_deploy_update: PostDeployUpdateEvidence,
) -> PostDeployUpdateEvidence:
    if post_deploy_update.status == "pending":
        raise click.ClickException(
            "Generic web post-deploy extensions must return terminal evidence."
        )
    return post_deploy_update
