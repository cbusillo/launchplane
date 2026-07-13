from __future__ import annotations

from dataclasses import dataclass
import hashlib
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
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebProviderDeploymentObservation,
)
from control_plane.workflows.generic_web_deploy_provider import GenericWebResolvedDeployTarget
from control_plane.workflows.generic_web_deploy_provider import default_generic_web_deploy_provider
from control_plane.workflows.generic_web_deploy_provider import (
    generic_web_provider_deployment_succeeded,
)
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.ship import (
    build_deployment_record,
    generate_deployment_record_id,
    utc_now_timestamp,
)


class GenericWebDeployStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...

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
    provider_effect_attempted: bool = False
    post_deploy_status: Literal["pass", "fail", "skipped"] = "skipped"
    error_message: str = ""

    @model_validator(mode="after")
    def _validate_result(self) -> "GenericWebDeployResult":
        self.provider_id = self.provider_id.strip().lower()
        self.provider_target_type = self.provider_target_type.strip().lower()
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
        self.target_category = cast(DeployTargetCategory, self.target_category.strip().lower())
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


@dataclass(frozen=True)
class GenericWebPostDeployGuard:
    provider_effect_checkpoint: Callable[[str], None] | None = None
    deployment_title: str = ""

    def checkpoint_effect(self, phase: str) -> None:
        if self.provider_effect_checkpoint is not None:
            self.provider_effect_checkpoint(phase)


GenericWebPostDeployExecutor = Callable[
    [
        Path,
        GenericWebDeployStore,
        GenericWebPostDeployContext,
        GenericWebPostDeployGuard,
    ],
    PostDeployUpdateEvidence,
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
    requested_instance = request.instance.strip()
    for lane in profile.lanes:
        if lane.instance.strip() == requested_instance:
            return profile, _canonical_generic_web_lane(lane)
    raise click.ClickException(
        f"Product {profile.product!r} has no generic-web lane for instance {request.instance!r}."
    )


def _canonical_generic_web_lane(lane: ProductLaneProfile) -> ProductLaneProfile:
    return lane.model_copy(
        update={
            "context": lane.context.strip(),
            "instance": lane.instance.strip(),
        }
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
    if not image_repository:
        raise click.ClickException(
            "Generic web image deploy requires product image.repository. "
            "Configure an immutable image repository before using generic-web deploy."
        )
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
    artifact_id: str = "",
) -> ShipRequest:
    provider_id = deploy_provider.provider_id.strip().lower()
    if not provider_id:
        raise click.ClickException("Generic web deploy provider requires provider_id.")
    deploy_mode = f"{provider_id}-application-api"
    resolved_artifact_id = artifact_id.strip() or normalize_generic_web_artifact_id(
        profile=profile,
        artifact_id=request.artifact_id,
    )
    return ShipRequest(
        artifact_id=resolved_artifact_id,
        context=lane.context,
        instance=lane.instance,
        source_git_ref=request.source_git_ref,
        target_name=_fallback_target_name(profile=profile, lane=lane),
        target_type="application",
        deploy_mode=deploy_mode,
        provider_id=provider_id,
        target_category="application",
        provider_target_type="application",
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
    record_store: GenericWebDeployStore,
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
        record_store=record_store,
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
    resolved_deploy_target: GenericWebResolvedDeployTarget | None = None,
    provider_operation_title: str = "",
    deployment_record_id: str = "",
    provider_effect_checkpoint: Callable[[str], None] | None = None,
) -> GenericWebDeployResult:
    normalized_provider_operation_title = provider_operation_title.strip()
    if provider_effect_checkpoint is not None and not normalized_provider_operation_title:
        raise ValueError("Durable generic web deploys require a provider operation title.")
    resolved_deploy_provider = deploy_provider or default_generic_web_deploy_provider()
    resolved_profile = profile
    resolved_lane = lane
    if resolved_profile is None or resolved_lane is None:
        resolved_profile, resolved_lane = resolve_generic_web_profile_lane(
            record_store=record_store,
            request=request,
        )
    resolved_lane = _canonical_generic_web_lane(resolved_lane)

    record_id = deployment_record_id.strip() or generate_deployment_record_id(
        context_name=resolved_lane.context,
        instance_name=resolved_lane.instance,
    )
    started_at = utc_now_timestamp()
    fallback_request: ShipRequest | None = None

    try:
        fallback_request = _fallback_ship_request(
            request=request,
            profile=resolved_profile,
            lane=resolved_lane,
            deploy_provider=resolved_deploy_provider,
        )
        prepared_deploy_target = resolved_deploy_target or _resolve_deploy_target(
            control_plane_root=control_plane_root,
            record_store=record_store,
            request=request,
            profile=resolved_profile,
            lane=resolved_lane,
            deploy_provider=resolved_deploy_provider,
        )
        ship_request = prepared_deploy_target.ship_request
        resolved_target = prepared_deploy_target.resolved_target
    except click.ClickException as exc:
        finished_at = utc_now_timestamp()
        if fallback_request is None:
            if resolved_profile.image.repository.strip():
                raise
            fallback_request = _fallback_ship_request(
                request=request,
                profile=resolved_profile,
                lane=resolved_lane,
                deploy_provider=resolved_deploy_provider,
                artifact_id=request.artifact_id,
            )
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
    provider_effect_attempted = False
    provider_deployment_observation: GenericWebProviderDeploymentObservation | None = None
    post_deploy_update = PostDeployUpdateEvidence()

    def mark_provider_effect_started() -> None:
        nonlocal provider_effect_attempted
        provider_effect_attempted = True

    try:
        resolved_deploy_provider.execute_artifact_deploy(
            control_plane_root=control_plane_root,
            resolved_deploy_target=prepared_deploy_target,
            runtime_identity=_build_runtime_identity(
                profile=resolved_profile,
                lane=resolved_lane,
                ship_request=ship_request,
                deployment_record_id=record_id,
            ),
            deployment_title=normalized_provider_operation_title,
            before_provider_mutation=(provider_effect_checkpoint or (lambda _phase: None)),
            effect_started=mark_provider_effect_started,
        )
        if normalized_provider_operation_title:
            provider_deployment_observation = resolved_deploy_provider.observe_artifact_deploy(
                control_plane_root=control_plane_root,
                resolved_deploy_target=prepared_deploy_target,
                deployment_title=normalized_provider_operation_title,
            )
            if (
                provider_deployment_observation.outcome != "present"
                or not generic_web_provider_deployment_succeeded(
                    provider_deployment_observation.deployment_status
                )
            ):
                raise click.ClickException(
                    "Generic web provider deployment did not return exact successful evidence."
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
                    resolved_deploy_target=prepared_deploy_target,
                    artifact_id=ship_request.artifact_id,
                    source_git_ref=ship_request.source_git_ref,
                ),
                post_deploy_executor=post_deploy_executor,
                guard=GenericWebPostDeployGuard(
                    provider_effect_checkpoint=provider_effect_checkpoint,
                    deployment_title=_generic_web_post_deploy_operation_title(
                        normalized_provider_operation_title
                    ),
                ),
            )
            if post_deploy_update.status == "fail":
                raise click.ClickException(
                    post_deploy_update.detail or "Generic web post-deploy extension failed."
                )
    except click.ClickException as exc:
        finished_at = utc_now_timestamp()
        deployment_status: Literal["pass", "fail"] = "pass" if deploy_completed else "fail"
        recorded_deployment_id = (
            provider_deployment_observation.deployment_id
            if provider_deployment_observation is not None
            else "control-plane-dokploy"
        )
        recorded_started_at = (
            provider_deployment_observation.started_at
            if provider_deployment_observation is not None
            else started_at
        )
        recorded_finished_at = (
            provider_deployment_observation.finished_at
            if provider_deployment_observation is not None
            else finished_at
        )
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
                deployed_at=recorded_finished_at,
            )
            if deploy_completed
            else None
        )
        deployment_record = build_deployment_record(
            request=ship_request,
            record_id=record_id,
            deployment_id=recorded_deployment_id,
            deployment_status=deployment_status,
            started_at=recorded_started_at,
            finished_at=recorded_finished_at,
            resolved_target=resolved_target,
            deployed_target=prepared_deploy_target.deployed_target,
            delegated_executor=resolved_deploy_provider.delegated_executor,
            post_deploy_update=post_deploy_update,
            runtime_identity=runtime_identity,
        )
        record_store.write_deployment_record(deployment_record)
        if deploy_completed:
            record_store.write_environment_inventory(
                build_environment_inventory(
                    deployment_record=deployment_record,
                    updated_at=recorded_finished_at,
                )
            )
        target_fields = _deploy_result_target_fields(resolved_deploy_target=prepared_deploy_target)
        return GenericWebDeployResult(
            deployment_record_id=record_id,
            deploy_status=deployment_status,
            deploy_started_at=recorded_started_at,
            deploy_finished_at=recorded_finished_at,
            product=resolved_profile.product,
            context=resolved_lane.context,
            instance=resolved_lane.instance,
            target_name=target_fields.target_name,
            target_id=target_fields.target_id,
            target_category=target_fields.target_category,
            provider_id=target_fields.provider_id,
            provider_target_type=target_fields.provider_target_type,
            provider_effect_attempted=provider_effect_attempted,
            post_deploy_status=_generic_web_deploy_post_deploy_status(post_deploy_update),
            error_message=str(exc),
        )

    finished_at = utc_now_timestamp()
    recorded_deployment_id = (
        provider_deployment_observation.deployment_id
        if provider_deployment_observation is not None
        else "control-plane-dokploy"
    )
    recorded_started_at = (
        provider_deployment_observation.started_at
        if provider_deployment_observation is not None
        else started_at
    )
    recorded_finished_at = (
        provider_deployment_observation.finished_at
        if provider_deployment_observation is not None
        else finished_at
    )
    deployment_record = build_deployment_record(
        request=ship_request,
        record_id=record_id,
        deployment_id=recorded_deployment_id,
        deployment_status="pass",
        started_at=recorded_started_at,
        finished_at=recorded_finished_at,
        resolved_target=resolved_target,
        deployed_target=prepared_deploy_target.deployed_target,
        delegated_executor=resolved_deploy_provider.delegated_executor,
        post_deploy_update=post_deploy_update,
        runtime_identity=_build_runtime_identity(
            profile=resolved_profile,
            lane=resolved_lane,
            ship_request=ship_request,
            deployment_record_id=record_id,
            deployed_at=recorded_finished_at,
        ),
    )
    record_store.write_deployment_record(deployment_record)
    record_store.write_environment_inventory(
        build_environment_inventory(
            deployment_record=deployment_record,
            updated_at=recorded_finished_at,
        )
    )
    target_fields = _deploy_result_target_fields(resolved_deploy_target=prepared_deploy_target)
    return GenericWebDeployResult(
        deployment_record_id=record_id,
        deploy_status="pass",
        deploy_started_at=recorded_started_at,
        deploy_finished_at=recorded_finished_at,
        product=resolved_profile.product,
        context=resolved_lane.context,
        instance=resolved_lane.instance,
        target_name=target_fields.target_name,
        target_id=target_fields.target_id,
        target_category=target_fields.target_category,
        provider_id=target_fields.provider_id,
        provider_target_type=target_fields.provider_target_type,
        provider_effect_attempted=provider_effect_attempted,
        post_deploy_status=_generic_web_deploy_post_deploy_status(post_deploy_update),
    )


def record_observed_generic_web_deploy(
    *,
    record_store: GenericWebDeployStore,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    resolved_deploy_target: GenericWebResolvedDeployTarget,
    observation: GenericWebProviderDeploymentObservation,
    deployment_record_id: str,
    post_deploy_unobserved: bool,
) -> tuple[dict[str, object], dict[str, object]]:
    if observation.outcome != "present":
        raise ValueError("Observed generic web deploy evidence must be present.")
    lane = _canonical_generic_web_lane(lane)
    deploy_status: Literal["pass", "fail"] = (
        "pass"
        if generic_web_provider_deployment_succeeded(observation.deployment_status)
        else "fail"
    )
    ship_request = resolved_deploy_target.ship_request
    try:
        existing_record = record_store.read_deployment_record(deployment_record_id)
    except FileNotFoundError:
        existing_record = None
    if existing_record is not None and (
        existing_record.context != lane.context
        or existing_record.instance != lane.instance
        or existing_record.source_git_ref != ship_request.source_git_ref
    ):
        raise ValueError("Observed generic web deployment record identity does not match.")
    started_at = observation.started_at
    finished_at = observation.finished_at
    deployment_id = observation.deployment_id
    post_deploy_update = (
        existing_record.post_deploy_update
        if existing_record is not None
        else PostDeployUpdateEvidence()
    )
    if post_deploy_unobserved and post_deploy_update.status == "skipped":
        post_deploy_update = PostDeployUpdateEvidence(
            attempted=True,
            status="fail",
            detail="Post-deploy extension was not durably observed after provider recovery.",
        )
    runtime_identity = existing_record.runtime_identity if existing_record is not None else None
    if deploy_status == "pass" and runtime_identity is None:
        runtime_identity = _build_runtime_identity(
            profile=profile,
            lane=lane,
            ship_request=ship_request,
            deployment_record_id=deployment_record_id,
            deployed_at=finished_at,
        )
    if deploy_status == "fail":
        runtime_identity = None
    deployment_record = build_deployment_record(
        request=ship_request,
        record_id=deployment_record_id,
        deployment_id=deployment_id,
        deployment_status=deploy_status,
        started_at=started_at,
        finished_at=finished_at,
        resolved_target=resolved_deploy_target.resolved_target,
        deployed_target=resolved_deploy_target.deployed_target,
        delegated_executor=(
            existing_record.delegated_executor
            if existing_record is not None
            else "control-plane.dokploy"
        ),
        post_deploy_update=post_deploy_update,
        runtime_identity=runtime_identity,
    )
    record_store.write_deployment_record(deployment_record)
    if deploy_status == "pass":
        record_store.write_environment_inventory(
            build_environment_inventory(
                deployment_record=deployment_record,
                updated_at=deployment_record.deploy.finished_at or finished_at,
            )
        )
    target_fields = _deploy_result_target_fields(resolved_deploy_target=resolved_deploy_target)
    result = GenericWebDeployResult(
        deployment_record_id=deployment_record_id,
        deploy_status=deploy_status,
        deploy_started_at=deployment_record.deploy.started_at or started_at,
        deploy_finished_at=deployment_record.deploy.finished_at or finished_at,
        product=profile.product,
        context=lane.context,
        instance=lane.instance,
        target_name=target_fields.target_name,
        target_id=target_fields.target_id,
        target_category=target_fields.target_category,
        provider_id=target_fields.provider_id,
        provider_target_type=target_fields.provider_target_type,
        post_deploy_status=_generic_web_deploy_post_deploy_status(
            deployment_record.post_deploy_update
        ),
        error_message=(
            deployment_record.post_deploy_update.detail
            if deployment_record.post_deploy_update.status == "fail"
            else observation.error_message
        ),
    )
    return {"deployment_record_id": deployment_record_id}, result.model_dump(mode="json")


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
    guard: GenericWebPostDeployGuard,
) -> PostDeployUpdateEvidence:
    try:
        guard.checkpoint_effect("post_deploy_started")
        return _terminal_post_deploy_update(
            post_deploy_executor(control_plane_root, record_store, context, guard)
        )
    except click.ClickException:
        raise
    except Exception as exc:
        raise click.ClickException(str(exc)) from exc


def _generic_web_post_deploy_operation_title(provider_operation_title: str) -> str:
    normalized_title = provider_operation_title.strip()
    if not normalized_title:
        return ""
    digest = hashlib.sha256(normalized_title.encode("utf-8")).hexdigest()[:24]
    return f"Launchplane post-deploy {digest}"
