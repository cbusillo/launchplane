from __future__ import annotations

from pathlib import Path
from typing import Callable, Literal, Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.deploy_target import DeployTargetCategory
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import HealthcheckEvidence, PostDeployUpdateEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.ship_request import ShipRequest
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.workflows.generic_web_deploy_provider import GenericWebDeployProvider
from control_plane.workflows.generic_web_deploy_provider import GenericWebResolvedDeployTarget
from control_plane.workflows.generic_web_deploy_provider import default_generic_web_deploy_provider
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
    target_id: str = ""
    target_category: DeployTargetCategory = "unknown"
    provider_id: str = ""
    provider_target_type: str = ""
    target_type: str = ""
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_result(self) -> "GenericWebDeployResult":
        self.provider_id = self.provider_id.strip().lower()
        self.provider_target_type = self.provider_target_type.strip().lower()
        self.target_type = self.target_type.strip().lower()
        if not self.target_type:
            self.target_type = self.provider_target_type or self.target_category
        return self


class GenericWebDeployTargetResultFields(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_name: str
    target_id: str
    target_category: DeployTargetCategory = "unknown"
    provider_id: str = ""
    provider_target_type: str = ""
    target_type: str = ""


class GenericWebPostDeployContext(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: str
    deployment_record_id: str
    target_name: str
    target_id: str
    target_category: DeployTargetCategory = "unknown"
    provider_id: str = ""
    provider_target_type: str = ""
    target_type: str = ""
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
        self.target_category = cast(
            DeployTargetCategory, self.target_category.strip().lower()
        )
        self.provider_id = self.provider_id.strip().lower()
        self.provider_target_type = self.provider_target_type.strip().lower()
        self.target_type = self.target_type.strip().lower()
        if not self.target_type:
            self.target_type = self.provider_target_type or self.target_category
        if self.target_category == "unknown" and self.target_type in {
            "application",
            "compose",
            "container",
            "service",
            "static",
        }:
            self.target_category = cast(DeployTargetCategory, self.target_type)
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


def _build_post_deploy_context(
    *,
    product: str,
    context: str,
    instance: str,
    deployment_record_id: str,
    resolved_deploy_target: GenericWebResolvedDeployTarget,
    artifact_id: str,
    source_git_ref: str,
) -> GenericWebPostDeployContext:
    resolved_target = resolved_deploy_target.resolved_target
    deployed_target = resolved_deploy_target.deployed_target
    target_category: DeployTargetCategory = "unknown"
    provider_id = ""
    provider_target_type = str(resolved_target.target_type)
    if deployed_target is not None:
        target_category = deployed_target.target_category
        provider_id = deployed_target.provider_id
        provider_target_type = deployed_target.provider_target_type
    return GenericWebPostDeployContext(
        product=product,
        context=context,
        instance=instance,
        deployment_record_id=deployment_record_id,
        target_name=resolved_target.target_name,
        target_id=resolved_target.target_id,
        target_category=target_category,
        provider_id=provider_id,
        provider_target_type=provider_target_type,
        target_type=resolved_target.target_type,
        artifact_id=artifact_id,
        source_git_ref=source_git_ref,
    )


def _deploy_result_target_fields(
    *, resolved_deploy_target: GenericWebResolvedDeployTarget
) -> GenericWebDeployTargetResultFields:
    resolved_target = resolved_deploy_target.resolved_target
    deployed_target = resolved_deploy_target.deployed_target
    provider_id = ""
    target_category: DeployTargetCategory = "unknown"
    provider_target_type = str(resolved_target.target_type)
    if deployed_target is not None:
        provider_id = deployed_target.provider_id
        target_category = deployed_target.target_category
        provider_target_type = deployed_target.provider_target_type
    return GenericWebDeployTargetResultFields(
        target_name=resolved_target.target_name,
        target_id=resolved_target.target_id,
        target_category=target_category,
        provider_id=provider_id,
        provider_target_type=provider_target_type,
        target_type=provider_target_type or resolved_target.target_type,
    )


def _fallback_ship_request(
    *,
    request: GenericWebDeployRequest,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    deploy_provider: GenericWebDeployProvider,
) -> ShipRequest:
    provider_id = deploy_provider.provider_id.strip().lower()
    if not provider_id:
        raise click.ClickException("Generic web deploy provider requires provider_id.")
    deploy_mode = f"{provider_id}-application-api"
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
        deploy_mode=deploy_mode,
        provider_id=provider_id,
        target_category="application",
        provider_deploy_mode=deploy_mode,
        wait=True,
        timeout_seconds=request.timeout_seconds,
        verify_health=False,
        no_cache=request.no_cache,
        destination_health=HealthcheckEvidence(status="skipped"),
    )


def _resolve_deploy_target(
    *,
    control_plane_root: Path,
    request: GenericWebDeployRequest,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    deploy_provider: GenericWebDeployProvider,
) -> GenericWebResolvedDeployTarget:
    return deploy_provider.resolve_deploy_target(
        control_plane_root=control_plane_root,
        request_artifact_id=request.artifact_id,
        request_source_git_ref=request.source_git_ref,
        request_timeout_seconds=request.timeout_seconds,
        request_no_cache=request.no_cache,
        profile=profile,
        lane=lane,
        normalized_artifact_id=normalize_generic_web_artifact_id(
            profile=profile,
            artifact_id=request.artifact_id,
        ),
        fallback_target_name=_fallback_target_name(profile=profile, lane=lane),
    )


def execute_generic_web_deploy(
    *,
    control_plane_root: Path,
    record_store: GenericWebDeployStore,
    request: GenericWebDeployRequest,
    profile: LaunchplaneProductProfileRecord | None = None,
    lane: ProductLaneProfile | None = None,
    post_deploy_executor: GenericWebPostDeployExecutor | None = None,
    deploy_provider: GenericWebDeployProvider | None = None,
) -> GenericWebDeployResult:
    resolved_deploy_provider = deploy_provider or default_generic_web_deploy_provider()
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
        deploy_provider=resolved_deploy_provider,
    )

    try:
        resolved_deploy_target = _resolve_deploy_target(
            control_plane_root=control_plane_root,
            request=request,
            profile=resolved_profile,
            lane=resolved_lane,
            deploy_provider=resolved_deploy_provider,
        )
        ship_request = resolved_deploy_target.ship_request
        resolved_target = resolved_deploy_target.resolved_target
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
                delegated_executor=resolved_deploy_provider.delegated_executor,
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
        resolved_deploy_provider.execute_artifact_deploy(
            control_plane_root=control_plane_root,
            resolved_deploy_target=resolved_deploy_target,
            runtime_identity=_build_runtime_identity(
                profile=resolved_profile,
                lane=resolved_lane,
                ship_request=ship_request,
                deployment_record_id=record_id,
            ),
        )
        deploy_completed = True
        if post_deploy_executor is not None:
            post_deploy_update = _run_post_deploy_extension(
                control_plane_root=control_plane_root,
                record_store=record_store,
                context=_build_post_deploy_context(
                    product=resolved_profile.product,
                    context=resolved_lane.context,
                    instance=resolved_lane.instance,
                    deployment_record_id=record_id,
                    resolved_deploy_target=resolved_deploy_target,
                    artifact_id=ship_request.artifact_id,
                    source_git_ref=ship_request.source_git_ref,
                ),
                post_deploy_executor=post_deploy_executor,
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
        runtime_identity = (
            _build_runtime_identity(
                profile=resolved_profile,
                lane=resolved_lane,
                ship_request=ship_request,
                deployment_record_id=record_id,
                deployed_at=finished_at,
            )
            if deploy_completed
            else None
        )
        deployment_record = build_deployment_record(
            request=ship_request,
            record_id=record_id,
            deployment_id="control-plane-dokploy",
            deployment_status=deployment_status,
            started_at=started_at,
            finished_at=finished_at,
            resolved_target=resolved_target,
            deployed_target=resolved_deploy_target.deployed_target,
            delegated_executor=resolved_deploy_provider.delegated_executor,
            post_deploy_update=post_deploy_update,
            runtime_identity=runtime_identity,
        )
        record_store.write_deployment_record(deployment_record)
        if deploy_completed:
            record_store.write_environment_inventory(
                build_environment_inventory(
                    deployment_record=deployment_record,
                    updated_at=finished_at,
                )
            )
        target_fields = _deploy_result_target_fields(
            resolved_deploy_target=resolved_deploy_target
        )
        return GenericWebDeployResult(
            deployment_record_id=record_id,
            deploy_status=deployment_status,
            deploy_started_at=started_at,
            deploy_finished_at=finished_at,
            product=resolved_profile.product,
            context=resolved_lane.context,
            instance=resolved_lane.instance,
            target_name=target_fields.target_name,
            target_id=target_fields.target_id,
            target_category=target_fields.target_category,
            provider_id=target_fields.provider_id,
            provider_target_type=target_fields.provider_target_type,
            target_type=target_fields.target_type,
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
        deployed_target=resolved_deploy_target.deployed_target,
        delegated_executor=resolved_deploy_provider.delegated_executor,
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
    target_fields = _deploy_result_target_fields(resolved_deploy_target=resolved_deploy_target)
    return GenericWebDeployResult(
        deployment_record_id=record_id,
        deploy_status="pass",
        deploy_started_at=started_at,
        deploy_finished_at=finished_at,
        product=resolved_profile.product,
        context=resolved_lane.context,
        instance=resolved_lane.instance,
        target_name=target_fields.target_name,
        target_id=target_fields.target_id,
        target_category=target_fields.target_category,
        provider_id=target_fields.provider_id,
        provider_target_type=target_fields.provider_target_type,
        target_type=target_fields.target_type,
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


def _run_post_deploy_extension(
    *,
    control_plane_root: Path,
    record_store: GenericWebDeployStore,
    context: GenericWebPostDeployContext,
    post_deploy_executor: GenericWebPostDeployExecutor,
) -> PostDeployUpdateEvidence:
    try:
        return _terminal_post_deploy_update(
            post_deploy_executor(control_plane_root, record_store, context)
        )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc
