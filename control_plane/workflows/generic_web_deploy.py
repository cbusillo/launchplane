from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import json
from pathlib import Path
from time import monotonic
from typing import Callable, Literal, Protocol, cast

import click
from control_plane.generic_web_source_qualification import (
    GenericWebPromotionSourceRefused,
    GenericWebSourceQualificationStore,
)
from control_plane.generic_web_testing_inputs import TestingInputSafetyRefused
from control_plane.provider_operations import ProviderMutationUnknownError
from control_plane.workflows.generic_web_execution import (
    ExecutionRecorder,
    require_recorded_execution,
)
from control_plane.lane_movement import (
    LaneBuild,
    LaneMovementRefused,
    require_forward_lane_build,
    _proved_generic_build,
    _recorded_generic_build,
    lane_build_proof,
)
from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.deploy_target import DeployTargetCategory
from control_plane.contracts.deploy_reference import (
    is_digest_pinned_image_reference,
    provider_image_reference,
    validate_provider_deploy_reference,
)
from control_plane.contracts.deployment_record import (
    DeploymentRecord,
    IntegrationKeyReadbackEvidence,
)
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import HealthcheckEvidence, PostDeployUpdateEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity
from control_plane.contracts.ship_request import ShipRequest
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.integration_key_readback import (
    evaluate_delivered_integration_keys,
    integration_key_readback,
    lane_receives_managed_runtime_bindings,
)
from control_plane.runtime_key_safety import runtime_key_safety_environment_class
from control_plane.workflows.generic_web_deploy_provider import GenericWebDeployProvider
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebDeployRuntimeArtifactProvider,
)
from control_plane.workflows.generic_web_deploy_provider import (
    evaluate_generic_web_runtime_close_out,
)
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebProviderDeploymentObservation,
    GenericWebProviderDeploymentFailed,
)
from control_plane.workflows.generic_web_deploy_provider import GenericWebResolvedDeployTarget
from control_plane.workflows.generic_web_deploy_provider import default_generic_web_deploy_provider
from control_plane.workflows.generic_web_deploy_provider import (
    generic_web_provider_deployment_succeeded,
    qualify_testing_application_observation,
)
from control_plane.workflows.inventory import build_environment_inventory
from control_plane.workflows.generic_web_application_snapshot import (
    application_snapshot_for_deploy,
    recorded_application_lifecycle,
)
from control_plane.workflows.generic_web_application_lifecycle import (
    GenericWebApplicationLifecycle,
    application_lifecycle_for_lane,
)
from control_plane.workflows.ship import (
    build_deployment_record,
    generate_deployment_record_id,
    utc_now_timestamp,
)


GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID = "control-plane-dokploy"


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
    deploy_reference: str = ""
    source_git_ref: str
    timeout_seconds: int | None = Field(default=None, ge=1)
    no_cache: bool = False
    reset: bool = False

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebDeployRequest":
        if not self.product.strip():
            raise ValueError("Generic web deploy requires product.")
        if not self.instance.strip():
            raise ValueError("Generic web deploy requires instance.")
        if not self.artifact_id.strip():
            raise ValueError("Generic web deploy requires artifact_id.")
        self.deploy_reference = self.deploy_reference.strip()
        if not self.source_git_ref.strip():
            raise ValueError("Generic web deploy requires source_git_ref.")
        return self


GenericWebDeploymentPublisher = Callable[[DeploymentRecord, EnvironmentInventory | None], None]


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
    provider_failure_proved: bool = False
    image_effect_attempted: bool = False
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
    authority_digest: str = ""
    previous_inventory: EnvironmentInventory | None

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
    execution_recorder: ExecutionRecorder | None = None
    exchanges: dict[str, bool] = field(default_factory=dict, compare=False)

    def begin_exchange(self, name: str) -> None:
        self.exchanges[name] = False

    def complete_exchange(self, name: str) -> None:
        self.exchanges[name] = True

    def require_completed_exchanges(self) -> None:
        if not all(self.exchanges.values()):
            raise ProviderMutationUnknownError("Testing product exchange completion is unknown.")

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
    if not profile.is_active:
        raise click.ClickException(
            f"Product {profile.product!r} is {profile.lifecycle_state} and cannot deploy."
        )
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
    *, profile: LaunchplaneProductProfileRecord, artifact_id: str, recorded: bool = False
) -> str:
    """The artifact a generic-web lane deploys, in the product's image repository.

    ``recorded`` is for an artifact Launchplane read from its own deployment,
    inventory or reservation records, never one a caller supplied: an immutable
    image there stays valid after the profile moves to another repository, so a
    rollback, a deploy recovery or promotion evidence still finds it.
    """
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
    if (
        recorded
        and "@" in normalized_artifact_id
        and is_digest_pinned_image_reference(normalized_artifact_id)
    ):
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
        image_reference=provider_image_reference(
            artifact_id=ship_request.artifact_id,
            deploy_reference=ship_request.deploy_reference,
        ),
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
    previous_inventory: EnvironmentInventory | None,
    authority_digest: str = "",
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
        authority_digest=authority_digest,
        previous_inventory=previous_inventory,
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
    include_deploy_reference: bool = True,
    recorded_artifact: bool = False,
) -> ShipRequest:
    provider_id = deploy_provider.provider_id.strip().lower()
    if not provider_id:
        raise click.ClickException("Generic web deploy provider requires provider_id.")
    deploy_mode = f"{provider_id}-application-api"
    resolved_artifact_id = artifact_id.strip() or normalize_generic_web_artifact_id(
        profile=profile,
        artifact_id=request.artifact_id,
        recorded=recorded_artifact,
    )
    deploy_reference = ""
    if include_deploy_reference and request.deploy_reference:
        deploy_reference = validate_provider_deploy_reference(
            artifact_id=resolved_artifact_id,
            deploy_reference=request.deploy_reference,
            label="Generic web deploy",
        )
    return ShipRequest(
        artifact_id=resolved_artifact_id,
        deploy_reference=deploy_reference,
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
    recorded_artifact: bool = False,
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
            recorded=recorded_artifact,
        ),
        request_deploy_reference=request.deploy_reference,
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
    post_deploy_authority_digest: str = "",
    execution_recorder: ExecutionRecorder | None = None,
    publish_deployment: GenericWebDeploymentPublisher | None = None,
    deploy_provider: GenericWebDeployProvider | None = None,
    resolved_deploy_target: GenericWebResolvedDeployTarget | None = None,
    provider_operation_title: str = "",
    deployment_record_id: str = "",
    provider_effect_checkpoint: Callable[[str], None] | None = None,
    before_records_commit: Callable[[], None] | None = None,
    before_failure_records_commit: Callable[[], None] | None = None,
    before_deployment_records: Callable[[], None] | None = None,
    recorded_artifact: bool = False,
    rollback: bool = False,
    proved_pre_image_retry: bool = False,
    rollback_source_deployment_record_id: str = "",
    rollback_target_deployment_record_id: str = "",
) -> GenericWebDeployResult:
    from control_plane.workflows.generic_web_preview import serialize_generic_web_product_operation

    with serialize_generic_web_product_operation(
        record_store=record_store, product=request.product
    ):
        current = record_store.read_product_profile_record(request.product)
        if not current.is_active:
            raise click.ClickException("Product is retiring and cannot deploy.")
        # Re-read lane authority under the existing product serializer; callers
        # cannot bypass it with a stale profile or a cached provider target.
        current_lane = next(
            (item for item in current.lanes if item.instance == request.instance), None
        )
        list_records = getattr(record_store, "list_deployment_records", None)
        retained_cutover = bool(
            current_lane
            and callable(list_records)
            and any(
                item.generic_web_cutover is not None
                and item.generic_web_cutover.step != "blocked"
                and (
                    item.generic_web_cutover.step != "rolled_back"
                    or not item.generic_web_cutover.records_committed
                )
                for item in list_records(
                    context_name=current_lane.context, instance_name=current_lane.instance
                )
            )
        )
        if current_lane is not None and (
            current_lane.generic_web_cutover is not None or retained_cutover
        ):
            from control_plane.generic_web_cutover import (
                WebCutoverAuthorityStore,
                execute_web_cutover_deployment,
            )
            from control_plane.contracts.generic_web_cutover import GenericWebCutoverPlan
            from control_plane.workflows.generic_web_cutover import WebCutoverProvider

            if lane is not None and lane.generic_web_cutover != current_lane.generic_web_cutover:
                raise ValueError("reserved traffic authority changed before execution")
            if current.driver_id != "generic-web" or current_lane.generic_web_cutover is None:
                raise ValueError(
                    "recorded traffic authority cannot fall back to in-place deployment"
                )
            if post_deploy_executor is not None:
                raise ValueError(
                    "two-slot releases cannot run an unqualified post-deploy extension"
                )
            integration_key_refusal = _non_production_integration_key_refusal(
                record_store=record_store,
                context_name=current_lane.context,
                instance_name=current_lane.instance,
            )
            if integration_key_refusal:
                raise ValueError(integration_key_refusal)
            if not rollback:
                require_forward_lane_build(
                    record_store=record_store,
                    profile=current,
                    context=current_lane.context,
                    instance=current_lane.instance,
                    desired=LaneBuild(
                        request.artifact_id,
                        request.source_git_ref,
                        request.artifact_id,
                        deploy_reference=request.deploy_reference,
                    ),
                )
            selected_provider = deploy_provider or default_generic_web_deploy_provider(
                record_store=record_store, control_plane_root=control_plane_root
            )
            factory = cast(
                Callable[[GenericWebCutoverPlan], WebCutoverProvider] | None,
                getattr(selected_provider, "cutover_provider_factory", None),
            )
            record_id = deployment_record_id or generate_deployment_record_id(
                context_name=current_lane.context, instance_name=current_lane.instance
            )
            deployment = execute_web_cutover_deployment(
                cast(WebCutoverAuthorityStore, record_store),
                product=request.product,
                lane=current_lane,
                artifact_id=request.artifact_id,
                source_git_ref=request.source_git_ref,
                deploy_reference=request.deploy_reference,
                record_id=record_id,
                factory=factory,
                rollback=rollback,
                checkpoint=provider_effect_checkpoint or (lambda _phase: None),
                before_records_commit=before_records_commit or (lambda: None),
                before_deployment_records=before_deployment_records,
            )
            return cutover_deploy_result(
                deployment,
                product=current.product,
                provider_id=selected_provider.provider_id,
                rollback=rollback,
            )
        return _execute_generic_web_deploy_unserialized(
            control_plane_root=control_plane_root,
            record_store=record_store,
            request=request,
            profile=profile or current,
            lane=lane,
            post_deploy_executor=post_deploy_executor,
            post_deploy_authority_digest=post_deploy_authority_digest,
            execution_recorder=execution_recorder,
            publish_deployment=publish_deployment,
            deploy_provider=deploy_provider,
            resolved_deploy_target=resolved_deploy_target,
            provider_operation_title=provider_operation_title,
            deployment_record_id=deployment_record_id,
            provider_effect_checkpoint=provider_effect_checkpoint,
            before_records_commit=before_records_commit,
            before_failure_records_commit=before_failure_records_commit,
            before_deployment_records=before_deployment_records,
            recorded_artifact=recorded_artifact,
            rollback=rollback,
            proved_pre_image_retry=proved_pre_image_retry,
            rollback_source_deployment_record_id=rollback_source_deployment_record_id,
            rollback_target_deployment_record_id=rollback_target_deployment_record_id,
        )


def _execute_generic_web_deploy_unserialized(
    *,
    control_plane_root: Path,
    record_store: GenericWebDeployStore,
    request: GenericWebDeployRequest,
    profile: LaunchplaneProductProfileRecord | None = None,
    lane: ProductLaneProfile | None = None,
    post_deploy_executor: GenericWebPostDeployExecutor | None = None,
    post_deploy_authority_digest: str = "",
    execution_recorder: ExecutionRecorder | None = None,
    publish_deployment: GenericWebDeploymentPublisher | None = None,
    deploy_provider: GenericWebDeployProvider | None = None,
    resolved_deploy_target: GenericWebResolvedDeployTarget | None = None,
    provider_operation_title: str = "",
    deployment_record_id: str = "",
    provider_effect_checkpoint: Callable[[str], None] | None = None,
    before_records_commit: Callable[[], None] | None = None,
    before_failure_records_commit: Callable[[], None] | None = None,
    before_deployment_records: Callable[[], None] | None = None,
    recorded_artifact: bool = False,
    rollback: bool = False,
    proved_pre_image_retry: bool = False,
    rollback_source_deployment_record_id: str = "",
    rollback_target_deployment_record_id: str = "",
) -> GenericWebDeployResult:
    """Deploy ``request``; ``recorded_artifact`` marks its artifact as Launchplane's own record."""
    normalized_provider_operation_title = provider_operation_title.strip()
    if provider_effect_checkpoint is not None and not normalized_provider_operation_title:
        raise ValueError("Durable generic web deploys require a provider operation title.")
    resolved_deploy_provider = deploy_provider or default_generic_web_deploy_provider(
        record_store=record_store, control_plane_root=control_plane_root
    )
    resolved_profile = profile
    resolved_lane = lane
    if resolved_profile is None or resolved_lane is None:
        resolved_profile, resolved_lane = resolve_generic_web_profile_lane(
            record_store=record_store,
            request=request,
        )
    resolved_lane = _canonical_generic_web_lane(resolved_lane)
    from control_plane.workflows.generic_web_post_deploy import (
        generic_web_post_deploy_authority_digest,
        generic_web_post_deploy_executor_for_profile,
    )

    qualification_authority = post_deploy_authority_digest
    if (
        not qualification_authority
        and post_deploy_executor is not None
        and post_deploy_executor
        is generic_web_post_deploy_executor_for_profile(
            resolved_profile, instance=resolved_lane.instance
        )
    ):
        qualification_authority = generic_web_post_deploy_authority_digest(
            record_store, resolved_profile, resolved_lane
        )

    record_id = deployment_record_id.strip() or generate_deployment_record_id(
        context_name=resolved_lane.context,
        instance_name=resolved_lane.instance,
    )
    started_at = utc_now_timestamp()
    fallback_request: ShipRequest | None = None
    post_deploy_update = PostDeployUpdateEvidence()
    verified_build_evidence: dict[str, str] = {}
    prior_attempt_exists = True
    try:
        prior_attempt = record_store.read_deployment_record(record_id)
    except FileNotFoundError:
        prior_attempt_exists = False
    else:
        # A refusal before dispatch must retain the original attempt's receipts.
        verified_build_evidence = dict(prior_attempt.runtime_source)
        if recorded_application_lifecycle(prior_attempt) is not None:
            post_deploy_update = PostDeployUpdateEvidence(
                evidence=prior_attempt.post_deploy_update.evidence
            )

    try:
        fallback_request = _fallback_ship_request(
            request=request,
            profile=resolved_profile,
            lane=resolved_lane,
            deploy_provider=resolved_deploy_provider,
            recorded_artifact=recorded_artifact,
        )
        prepared_deploy_target = resolved_deploy_target or _resolve_deploy_target(
            control_plane_root=control_plane_root,
            record_store=record_store,
            request=request,
            profile=resolved_profile,
            lane=resolved_lane,
            deploy_provider=resolved_deploy_provider,
            recorded_artifact=recorded_artifact,
        )
        ship_request = prepared_deploy_target.ship_request
        resolved_target = prepared_deploy_target.resolved_target
    except (click.ClickException, ValueError) as exc:
        finished_at = utc_now_timestamp()
        if fallback_request is None:
            if resolved_profile.image.repository.strip() and not request.deploy_reference:
                raise
            fallback_request = _fallback_ship_request(
                request=request,
                profile=resolved_profile,
                lane=resolved_lane,
                deploy_provider=resolved_deploy_provider,
                artifact_id=request.artifact_id,
                include_deploy_reference=False,
            )
        if execution_recorder is not None:
            require_recorded_execution(execution_recorder.receipt)
        if before_failure_records_commit is not None:
            before_failure_records_commit()
        record_store.write_deployment_record(
            build_deployment_record(
                request=fallback_request,
                record_id=record_id,
                deployment_id=GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID,
                deployment_status="fail",
                started_at=started_at,
                finished_at=finished_at,
                delegated_executor=resolved_deploy_provider.delegated_executor,
                post_deploy_update=post_deploy_update,
                runtime_source=verified_build_evidence,
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
    image_effect_attempted = False
    last_provider_phase = ""
    dispatch_refused = False
    provider_deployment_observation: GenericWebProviderDeploymentObservation | None = None
    key_readback: IntegrationKeyReadbackEvidence | None = None
    application_lifecycle: GenericWebApplicationLifecycle | None = None
    runtime_deadline: float | None = None
    runtime_failure_proved = False

    def write_pending(record: DeploymentRecord) -> None:
        if execution_recorder is not None:
            execution_recorder.write_pending(record)
        else:
            record_store.write_deployment_record(record)

    def checkpoint_provider(phase: str) -> None:
        nonlocal post_deploy_update, last_provider_phase, dispatch_refused
        if provider_effect_checkpoint is not None:
            provider_effect_checkpoint(phase)
        last_provider_phase = phase
        from control_plane.generic_web_testing_inputs import TESTING_DISPATCH_REFUSED_PHASE

        dispatch_refused = dispatch_refused or phase == TESTING_DISPATCH_REFUSED_PHASE
        if phase in {"provider_dispatch_prepared", "runtime_inputs_verified"}:
            from control_plane.generic_web_testing_inputs import DELIVERY_RECEIPT_KEY

            if phase == "runtime_inputs_verified":
                verified_build_evidence[DELIVERY_RECEIPT_KEY] = (
                    prepared_deploy_target.testing_runtime_verified_digest
                )
            else:
                # A retried dispatch earns delivery credit only at fresh readback.
                verified_build_evidence.pop(DELIVERY_RECEIPT_KEY, None)
            # Value-free provider references join the already reserved original
            # request/target before any env write or deploy trigger can happen.
            post_deploy_update = post_deploy_update.model_copy(
                update={
                    "evidence": {
                        **post_deploy_update.evidence,
                        "provider_dispatch": json.dumps(
                            prepared_deploy_target.provider_dispatch_evidence, sort_keys=True
                        ),
                    }
                }
            )
            write_pending(
                build_deployment_record(
                    request=ship_request,
                    record_id=record_id,
                    deployment_id=GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID,
                    deployment_status="pending",
                    started_at=started_at,
                    finished_at="",
                    resolved_target=resolved_target,
                    deployed_target=prepared_deploy_target.deployed_target,
                    delegated_executor=resolved_deploy_provider.delegated_executor,
                    post_deploy_update=post_deploy_update,
                    runtime_source=verified_build_evidence,
                )
            )

    def mark_provider_effect_started() -> None:
        nonlocal provider_effect_attempted
        provider_effect_attempted = True

    def require_cleanup_authority() -> None:
        if prepared_deploy_target.testing_runtime_digest:
            from control_plane.generic_web_testing_inputs import (
                require_testing_application_target_authority,
            )

            require_testing_application_target_authority(
                record_store,
                resolved_profile,
                resolved_lane,
                target_id=resolved_target.target_id,
                provider_id=resolved_deploy_provider.provider_id,
            )

    def lifecycle_checkpoint(phase: str) -> None:
        if dispatch_refused:
            require_cleanup_authority()
        if provider_effect_checkpoint is not None:
            provider_effect_checkpoint(phase)

    def mark_image_effect_started() -> None:
        nonlocal image_effect_attempted
        if last_provider_phase not in {"runtime_environment_update", "runtime_registry_prepare"}:
            image_effect_attempted = True
        mark_provider_effect_started()

    try:
        from control_plane.workflows.generic_web_deploy_provider import (
            GenericWebTestingRuntimePreflightProvider,
            prepare_generic_web_testing_runtime,
        )

        # Recovered targets carry captured authority, never serialized values.
        # Legacy snapshots deliberately have no authority to backfill.
        prepare_generic_web_testing_runtime(
            prepared_deploy_target, record_store, resolved_profile, resolved_lane, capture=False
        )
        if prepared_deploy_target._testing_runtime_prepare is not None:
            prepared_deploy_target._testing_runtime_prepare()
        if isinstance(resolved_deploy_provider, GenericWebTestingRuntimePreflightProvider):
            resolved_deploy_provider.preflight_testing_runtime(
                control_plane_root=control_plane_root, target=prepared_deploy_target
            )
        if request.reset:
            from control_plane.disposable_lanes import DisposableLaneStore, require_disposable_lane

            if rollback or resolved_lane.instance != "testing":
                raise click.ClickException(
                    "Application reset requires a disposable testing deploy."
                )
            if not any(
                command.on_reset for command in resolved_lane.generic_web_extensions.maintenance
            ):
                raise click.ClickException("The testing profile declares no reset command.")
            try:
                require_disposable_lane(
                    cast(DisposableLaneStore, record_store),
                    product=resolved_profile.product,
                    context=resolved_lane.context,
                    instance=resolved_lane.instance,
                )
            except (ValueError, AttributeError) as error:
                raise click.ClickException(
                    "Testing reset requires current disposable-lane evidence."
                ) from error
        desired_build = LaneBuild(
            request.artifact_id,
            request.source_git_ref,
            request.artifact_id,
            deploy_reference=request.deploy_reference,
        )
        if not rollback:
            desired_build = require_forward_lane_build(
                record_store=record_store,
                profile=resolved_profile,
                context=resolved_lane.context,
                instance=resolved_lane.instance,
                desired=desired_build,
            )
            if desired_build.source_build is None:
                # Initial creation or a retry of the identical build requires no
                # movement proof. Retain provenance when it can be verified.
                try:
                    desired_build = _proved_generic_build(
                        record_store, resolved_profile, desired_build, None
                    )
                except LaneMovementRefused:
                    pass
        else:
            desired_build = (
                _recorded_generic_build(record_store, resolved_profile, desired_build)
                or desired_build
            )
        if (
            resolved_lane.generic_web_extensions.verification_extension
            and post_deploy_executor is None
        ):
            raise click.ClickException(
                "Declared product verification has no supported post-deploy executor."
            )
        integration_key_refusal = _non_production_integration_key_refusal(
            record_store=record_store,
            context_name=resolved_lane.context,
            instance_name=resolved_lane.instance,
        )
        if integration_key_refusal:
            raise click.ClickException(integration_key_refusal)
        verified_build_evidence.update(lane_build_proof(desired_build))
        snapshot = application_snapshot_for_deploy(
            store=record_store,
            product=resolved_profile.product,
            context=resolved_lane.context,
            instance=resolved_lane.instance,
            provider_id=resolved_deploy_provider.provider_id,
            target_id=resolved_target.target_id,
            artifact_id=ship_request.artifact_id,
            source_git_ref=ship_request.source_git_ref,
            deployment_record_id=record_id,
            extensions=resolved_lane.generic_web_extensions,
            rollback=rollback,
            proved_pre_image_retry=proved_pre_image_retry,
            rollback_source_deployment_record_id=rollback_source_deployment_record_id,
            rollback_target_deployment_record_id=rollback_target_deployment_record_id,
        )
        if snapshot is not None:
            snapshot.reset = request.reset
        application_lifecycle = application_lifecycle_for_lane(
            control_plane_root=control_plane_root,
            snapshot=snapshot,
            provider=resolved_deploy_provider,
            target_type=resolved_target.target_type,
            target_id=resolved_target.target_id,
            checkpoint=lifecycle_checkpoint,
            effect_started=mark_provider_effect_started,
        )
        if application_lifecycle is not None:
            application_lifecycle.capture()
            if hasattr(application_lifecycle, "execution_recorder"):
                application_lifecycle.execution_recorder = execution_recorder
        if application_lifecycle is not None or execution_recorder is not None:
            if before_deployment_records is not None:
                before_deployment_records()
            post_deploy_update = PostDeployUpdateEvidence(
                evidence={
                    **(application_lifecycle.snapshot.evidence() if application_lifecycle else {}),
                    **(execution_recorder.evidence() if execution_recorder else {}),
                },
            )
            write_pending(
                build_deployment_record(
                    request=ship_request,
                    record_id=record_id,
                    deployment_id=GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID,
                    deployment_status="pending",
                    started_at=started_at,
                    finished_at="",
                    resolved_target=resolved_target,
                    deployed_target=prepared_deploy_target.deployed_target,
                    delegated_executor=resolved_deploy_provider.delegated_executor,
                    post_deploy_update=post_deploy_update,
                    runtime_source=verified_build_evidence,
                )
            )
        if application_lifecycle is not None:
            if provider_effect_checkpoint is not None:
                provider_effect_checkpoint("application_lifecycle_prepared")
            application_lifecycle.prepare()
        testing_application = (
            resolved_lane.instance == "testing"
            and prepared_deploy_target.resolved_target.target_type == "application"
        )
        runtime_deadline = (
            monotonic() + prepared_deploy_target.deploy_timeout_seconds
            if testing_application
            else None
        )
        untitled_deployment_observation = resolved_deploy_provider.execute_artifact_deploy(
            control_plane_root=control_plane_root,
            resolved_deploy_target=prepared_deploy_target,
            runtime_identity=_build_runtime_identity(
                profile=resolved_profile,
                lane=resolved_lane,
                ship_request=ship_request,
                deployment_record_id=record_id,
            ),
            deployment_title=normalized_provider_operation_title,
            before_provider_mutation=checkpoint_provider,
            effect_started=mark_image_effect_started,
        )
        if normalized_provider_operation_title and untitled_deployment_observation is not None:
            provider_deployment_observation = _prove_untitled_deployment_effect(
                control_plane_root=control_plane_root,
                deploy_provider=resolved_deploy_provider,
                resolved_deploy_target=prepared_deploy_target,
                observation=untitled_deployment_observation,
                deployment_record_id=record_id,
            )
        elif normalized_provider_operation_title:
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
        if testing_application:
            qualified = qualify_testing_application_observation(
                provider=resolved_deploy_provider,
                control_plane_root=control_plane_root,
                target=prepared_deploy_target,
                observation=provider_deployment_observation
                or GenericWebProviderDeploymentObservation(
                    outcome="present",
                    deployment_status="success",
                    deployment_id="control-plane-dokploy",
                    started_at=started_at,
                    finished_at=utc_now_timestamp(),
                ),
                deployment_record_id=record_id,
                wait=True,
                deadline=runtime_deadline,
            )
            if qualified.outcome != "present":
                raise click.ClickException("Application testing runtime rollout is not qualified.")
            if not generic_web_provider_deployment_succeeded(qualified.deployment_status):
                runtime_failure_proved = True
                raise GenericWebProviderDeploymentFailed(qualified)
            if (
                execution_recorder is not None
                and provider_deployment_observation is not None
                and qualified.deployment_id != GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID
            ):
                execution_recorder.record_settled_image(qualified.deployment_id)
        deploy_completed = True
        previous_inventory = None
        if post_deploy_executor is not None:
            try:
                previous_inventory = cast(
                    GenericWebSourceQualificationStore, record_store
                ).read_environment_inventory(
                    context_name=resolved_lane.context, instance_name=resolved_lane.instance
                )
            except FileNotFoundError:
                pass
        if (
            execution_recorder is not None
            and publish_deployment is not None
            and (application_lifecycle is not None or post_deploy_executor is not None)
        ):
            assert provider_deployment_observation is not None
            progress = build_deployment_record(
                request=ship_request,
                record_id=record_id,
                deployment_id=provider_deployment_observation.deployment_id,
                deployment_status="pass",
                started_at=provider_deployment_observation.started_at,
                finished_at=provider_deployment_observation.finished_at,
                resolved_target=resolved_target,
                deployed_target=prepared_deploy_target.deployed_target,
                delegated_executor=resolved_deploy_provider.delegated_executor,
                post_deploy_update=PostDeployUpdateEvidence(
                    attempted=True,
                    status="pending",
                    evidence={**post_deploy_update.evidence, **execution_recorder.evidence()},
                ),
                runtime_identity=_build_runtime_identity(
                    profile=resolved_profile,
                    lane=resolved_lane,
                    ship_request=ship_request,
                    deployment_record_id=record_id,
                    deployed_at=provider_deployment_observation.finished_at,
                ),
                runtime_source=verified_build_evidence,
            )
            publish_deployment(
                progress,
                build_environment_inventory(
                    deployment_record=progress,
                    updated_at=provider_deployment_observation.finished_at,
                ),
            )
            post_deploy_update = progress.post_deploy_update
        key_readback = integration_key_readback(
            record_store=record_store,
            context_name=resolved_lane.context,
            instance_name=resolved_lane.instance,
        )
        if application_lifecycle is not None:
            maintenance_evidence = application_lifecycle.maintain()
            post_deploy_update = PostDeployUpdateEvidence(
                attempted=True,
                status="pass",
                detail="Declared application maintenance completed.",
                evidence={**post_deploy_update.evidence, **maintenance_evidence},
            )
        if post_deploy_executor is not None:
            extension_update = _run_post_deploy_extension(
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
                    authority_digest=qualification_authority,
                    previous_inventory=previous_inventory,
                ),
                post_deploy_executor=post_deploy_executor,
                guard=GenericWebPostDeployGuard(
                    provider_effect_checkpoint=provider_effect_checkpoint,
                    deployment_title=_generic_web_post_deploy_operation_title(
                        normalized_provider_operation_title
                    ),
                    execution_recorder=execution_recorder,
                ),
            )
            post_deploy_update = extension_update.model_copy(
                update={"evidence": {**post_deploy_update.evidence, **extension_update.evidence}}
            )
            if post_deploy_update.status == "fail":
                raise click.ClickException(
                    post_deploy_update.detail or "Generic web post-deploy extension failed."
                )
        if application_lifecycle is not None:
            extension_evidence = application_lifecycle.complete()
            application_lifecycle.snapshot.state = "qualified"
            post_deploy_update = PostDeployUpdateEvidence(
                attempted=True,
                status="pass",
                detail="Declared application maintenance and schedules completed.",
                evidence={
                    **post_deploy_update.evidence,
                    **extension_evidence,
                    **application_lifecycle.snapshot.evidence(),
                },
            )
    except (click.ClickException, TestingInputSafetyRefused) as exc:
        if (
            isinstance(exc, GenericWebPromotionSourceRefused)
            and not prior_attempt_exists
            and not provider_effect_attempted
        ):
            # This first admission refusal precedes dispatch. Do not turn it
            # into a failed deployment that prevents the same accepted retest.
            raise
        provider_failure_observed = isinstance(exc, GenericWebProviderDeploymentFailed)
        if isinstance(exc, GenericWebProviderDeploymentFailed):
            provider_deployment_observation = exc.observation
            if runtime_deadline is not None:
                provider_failure_observed = runtime_failure_proved
                if not runtime_failure_proved:
                    failed = qualify_testing_application_observation(
                        provider=resolved_deploy_provider,
                        control_plane_root=control_plane_root,
                        target=prepared_deploy_target,
                        observation=exc.observation,
                        deployment_record_id=record_id,
                        wait=True,
                        deadline=runtime_deadline,
                    )
                    provider_failure_observed = failed.outcome == "present" and not (
                        generic_web_provider_deployment_succeeded(failed.deployment_status)
                    )
        if (
            execution_recorder is not None
            and runtime_deadline is not None
            and provider_failure_observed
            and not deploy_completed
            and provider_deployment_observation is not None
            and provider_deployment_observation.deployment_id
            != GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID
        ):
            execution_recorder.record_settled_image(provider_deployment_observation.deployment_id)
        if application_lifecycle is not None:
            try:
                require_cleanup_authority()
                if (
                    image_effect_attempted
                    or application_lifecycle.snapshot.resume_from_hold
                    or (
                        application_lifecycle.snapshot.mode == "rollback"
                        and not application_lifecycle.snapshot.restore_current_on_pre_image_failure
                    )
                ):
                    application_lifecycle.snapshot.state = "held"
                    application_lifecycle.hold()
                else:
                    application_lifecycle.snapshot.state = "restore_failed"
                    application_lifecycle.restore()
                    application_lifecycle.snapshot.state = "restored"
            except (
                click.ClickException,
                TestingInputSafetyRefused,
                FileNotFoundError,
            ) as restore_error:
                if dispatch_refused:
                    application_lifecycle.snapshot.state = "held"
                exc = click.ClickException(f"{exc}\n{restore_error}")
            post_deploy_update = PostDeployUpdateEvidence(
                attempted=application_lifecycle.effect_attempted,
                status="fail" if application_lifecycle.effect_attempted else "skipped",
                detail=str(exc),
                evidence={
                    **post_deploy_update.evidence,
                    **application_lifecycle.snapshot.evidence(),
                },
            )
        finished_at = utc_now_timestamp()
        deployment_status: Literal["pass", "fail"] = "pass" if deploy_completed else "fail"
        recorded_deployment_id = (
            provider_deployment_observation.deployment_id
            if provider_deployment_observation is not None
            else GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID
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
        if deploy_completed and post_deploy_update.status in {"skipped", "pending"}:
            post_deploy_update = PostDeployUpdateEvidence(
                attempted=True,
                status="fail",
                detail=str(exc),
                evidence=post_deploy_update.evidence,
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
            runtime_source=verified_build_evidence,
        ).model_copy(update={"integration_key_readback": key_readback})
        if execution_recorder is not None:
            deployment_record.post_deploy_update.evidence.update(execution_recorder.evidence())
        if isinstance(exc, LaneMovementRefused):
            deployment_record = deployment_record.model_copy(
                update={"failure": exc.record_failure()}
            )
        if execution_recorder is not None:
            require_recorded_execution(execution_recorder.receipt)
        if before_failure_records_commit is not None:
            before_failure_records_commit()
        elif before_records_commit is not None:
            before_records_commit()
        if publish_deployment is not None:
            publish_deployment(
                deployment_record,
                build_environment_inventory(
                    deployment_record=deployment_record, updated_at=recorded_finished_at
                )
                if deploy_completed
                else None,
            )
        else:
            record_store.write_deployment_record(deployment_record)
        if isinstance(exc, TestingInputSafetyRefused) and not provider_effect_attempted:
            # The durable runner releases a pre-effect refusal after publishing
            # its failed record; effects already attempted stay result-classified.
            raise exc
        if isinstance(exc, LaneMovementRefused) and exc.code == "source_order_unavailable":
            raise
        if deploy_completed and publish_deployment is None:
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
            provider_failure_proved=(
                provider_failure_observed and application_lifecycle is None and not deploy_completed
            ),
            image_effect_attempted=image_effect_attempted,
            post_deploy_status=_generic_web_deploy_post_deploy_status(post_deploy_update),
            error_message=str(exc),
        )

    finished_at = utc_now_timestamp()
    recorded_deployment_id = (
        provider_deployment_observation.deployment_id
        if provider_deployment_observation is not None
        else GENERIC_WEB_UNOBSERVED_DEPLOYMENT_ID
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
        runtime_source=verified_build_evidence,
    ).model_copy(update={"integration_key_readback": key_readback})
    if execution_recorder is not None:
        require_recorded_execution(execution_recorder.receipt)
    if before_records_commit is not None:
        before_records_commit()
    if execution_recorder is not None:
        deployment_record.post_deploy_update.evidence.update(execution_recorder.evidence())
    inventory = build_environment_inventory(
        deployment_record=deployment_record, updated_at=recorded_finished_at
    )
    if publish_deployment is not None:
        publish_deployment(deployment_record, inventory)
    else:
        record_store.write_deployment_record(deployment_record)
        record_store.write_environment_inventory(inventory)
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
        image_effect_attempted=image_effect_attempted,
        post_deploy_status=_generic_web_deploy_post_deploy_status(post_deploy_update),
    )


def build_observed_generic_web_deploy(
    *,
    record_store: GenericWebDeployStore,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    resolved_deploy_target: GenericWebResolvedDeployTarget,
    observation: GenericWebProviderDeploymentObservation,
    deployment_record_id: str,
    post_deploy_unobserved: bool,
) -> tuple[DeploymentRecord, dict[str, object], dict[str, object]]:
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
    receipt = recorded_application_lifecycle(existing_record) if existing_record else None
    post_deploy_unobserved = post_deploy_unobserved or (
        receipt is not None and receipt.state != "qualified"
    )
    if post_deploy_unobserved and post_deploy_update.status in {"skipped", "pending"}:
        post_deploy_update = PostDeployUpdateEvidence(
            attempted=True,
            status="fail",
            detail="Post-deploy extension was not durably observed after provider recovery.",
            evidence=post_deploy_update.evidence,
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
        runtime_source=existing_record.runtime_source if existing_record is not None else {},
    )
    records, result = generic_web_record_result(
        record=deployment_record,
        product=profile.product,
        target=resolved_deploy_target,
        observation=observation,
    )
    return deployment_record, records, result


def generic_web_record_result(
    *,
    record: DeploymentRecord,
    product: str,
    target: GenericWebResolvedDeployTarget,
    observation: GenericWebProviderDeploymentObservation,
) -> tuple[dict[str, object], dict[str, object]]:
    if record.deploy.status not in {"pass", "fail"}:
        raise ValueError("Original deployment has no final image result.")
    fields = _deploy_result_target_fields(resolved_deploy_target=target)
    result = GenericWebDeployResult(
        deployment_record_id=record.record_id,
        deploy_status=record.deploy.status,
        deploy_started_at=record.deploy.started_at or observation.started_at,
        deploy_finished_at=record.deploy.finished_at or observation.finished_at,
        product=product,
        context=record.context,
        instance=record.instance,
        target_name=fields.target_name,
        target_id=fields.target_id,
        target_category=fields.target_category,
        provider_id=fields.provider_id,
        provider_target_type=fields.provider_target_type,
        post_deploy_status=_generic_web_deploy_post_deploy_status(record.post_deploy_update),
        error_message=record.post_deploy_update.detail
        if record.post_deploy_update.status == "fail"
        else observation.error_message,
    )
    return {"deployment_record_id": record.record_id}, result.model_dump(mode="json")


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
    record, records, result = build_observed_generic_web_deploy(
        record_store=record_store,
        profile=profile,
        lane=lane,
        resolved_deploy_target=resolved_deploy_target,
        observation=observation,
        deployment_record_id=deployment_record_id,
        post_deploy_unobserved=post_deploy_unobserved,
    )
    record_store.write_deployment_record(record)
    if record.deploy.status == "pass":
        record_store.write_environment_inventory(
            build_environment_inventory(
                deployment_record=record,
                updated_at=record.deploy.finished_at or observation.finished_at,
            )
        )
    return records, result


def _non_production_integration_key_refusal(
    *, record_store: object, context_name: str, instance_name: str
) -> str:
    """Why a non-production deploy must stop before the provider, or "" to proceed.

    Each deploy path raises the refusal in its own pre-execution style.

    Production keeps only the recorded read-back: refusing there would block a
    release. Without a readable policy, a lane that receives managed bindings is
    refused rather than deployed unchecked.
    """
    if runtime_key_safety_environment_class(instance_name) == "prod":
        return ""
    evidence = evaluate_delivered_integration_keys(
        record_store=record_store, context_name=context_name, instance_name=instance_name
    )
    if evidence.status == "unavailable" and lane_receives_managed_runtime_bindings(
        record_store=record_store, context_name=context_name, instance_name=instance_name
    ):
        return (
            "Generic web deploy refused before the provider deploy: the lane receives "
            "managed runtime keys but no active runtime key-safety policy is readable."
        )
    if evidence.status != "fail":
        return ""
    findings = ", ".join(
        f"{finding.binding_key or '-'}: {finding.code}" for finding in evidence.findings
    )
    return (
        "Generic web deploy refused before the provider deploy: integration key safety "
        f"failed for {context_name}/{instance_name} ({findings})."
    )


def _prove_untitled_deployment_effect(
    *,
    control_plane_root: Path,
    deploy_provider: GenericWebDeployProvider,
    resolved_deploy_target: GenericWebResolvedDeployTarget,
    observation: GenericWebProviderDeploymentObservation,
    deployment_record_id: str,
) -> GenericWebProviderDeploymentObservation:
    """Accept an untitled provider deployment only when its effect is proven.

    The deployment must have succeeded, the target must be configured for and
    running exactly the original immutable image (the recovery close-out
    checks), and those containers must carry this operation's deployment record
    id, so an earlier or unrelated deploy of the same image cannot stand in for
    this one. Anything less keeps the outcome unknown.
    """

    if observation.outcome != "present" or not generic_web_provider_deployment_succeeded(
        observation.deployment_status
    ):
        raise click.ClickException(
            "Generic web provider deployment did not return exact successful evidence."
        )
    if not isinstance(deploy_provider, GenericWebDeployRuntimeArtifactProvider):
        raise click.ClickException(
            "Generic web provider cannot prove an untitled deployment from runtime evidence."
        )
    try:
        evaluate_generic_web_runtime_close_out(
            observation=deploy_provider.observe_runtime_artifact(
                control_plane_root=control_plane_root,
                resolved_deploy_target=resolved_deploy_target,
            ),
            expected_artifact_reference=resolved_deploy_target.ship_request.artifact_id,
            expected_deployment_record_id=deployment_record_id,
        )
    except (FileNotFoundError, ValueError) as error:
        raise click.ClickException(
            "Untitled generic web provider deployment is not proven by runtime evidence."
        ) from error
    return observation


def build_generic_web_runtime_close_out_result(
    *,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    resolved_deploy_target: GenericWebResolvedDeployTarget,
    provider_effect_started_at: str,
) -> dict[str, object]:
    """Describe a deploy closed out from runtime evidence.

    No provider deployment record was observed, so this writes no deployment or
    inventory record and leaves the deployment record id and finish time empty.
    """

    lane = _canonical_generic_web_lane(lane)
    target_fields = _deploy_result_target_fields(resolved_deploy_target=resolved_deploy_target)
    return GenericWebDeployResult(
        deployment_record_id="",
        deploy_status="pass",
        deploy_started_at=provider_effect_started_at.strip(),
        deploy_finished_at="",
        product=profile.product,
        context=lane.context,
        instance=lane.instance,
        target_name=target_fields.target_name,
        target_id=target_fields.target_id,
        target_category=target_fields.target_category,
        provider_id=target_fields.provider_id,
        provider_target_type=target_fields.provider_target_type,
        provider_effect_attempted=True,
        image_effect_attempted=True,
    ).model_dump(mode="json")


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
    recorder = guard.execution_recorder
    index = (
        recorder.begin(
            "product_executor",
            getattr(post_deploy_executor, "__name__", type(post_deploy_executor).__name__),
            context.target_id,
            {"authority": context.authority_digest},
        )
        if recorder
        else -1
    )
    try:
        guard.checkpoint_effect("post_deploy_started")
        result = _terminal_post_deploy_update(
            post_deploy_executor(control_plane_root, record_store, context, guard)
        )
        guard.require_completed_exchanges()
        if recorder is not None:
            recorder.update(index, acknowledgement="completed", terminal="executor_completed")
        return result
    except ProviderMutationUnknownError:
        raise
    except click.ClickException:
        guard.require_completed_exchanges()
        if recorder is not None:
            recorder.update(index, acknowledgement="completed", terminal="executor_completed")
        raise
    except Exception as exc:
        guard.require_completed_exchanges()
        if recorder is not None:
            recorder.update(index, acknowledgement="completed", terminal="executor_completed")
        raise click.ClickException(str(exc)) from exc


def _generic_web_post_deploy_operation_title(provider_operation_title: str) -> str:
    normalized_title = provider_operation_title.strip()
    if not normalized_title:
        return ""
    digest = hashlib.sha256(normalized_title.encode("utf-8")).hexdigest()[:24]
    return f"Launchplane post-deploy {digest}"


def cutover_deploy_result(
    deployment: DeploymentRecord, *, product: str, provider_id: str, rollback: bool = False
) -> GenericWebDeployResult:
    target = deployment.resolved_target
    if target is None:
        raise ValueError("cutover deployment lacks its exact target")
    return GenericWebDeployResult(
        deployment_record_id=deployment.record_id,
        deploy_status="pass" if deployment.deploy.status == "pass" else "fail",
        deploy_started_at=deployment.deploy.started_at,
        deploy_finished_at=deployment.deploy.finished_at,
        product=product,
        context=deployment.context,
        instance=deployment.instance,
        target_name=target.target_name,
        target_id=target.target_id,
        target_category=target.target_type,
        provider_id=provider_id,
        provider_target_type=target.target_type,
        provider_effect_attempted=rollback
        or (
            deployment.generic_web_cutover is not None
            and deployment.generic_web_cutover.step != "blocked"
        ),
        image_effect_attempted=not rollback
        and deployment.generic_web_cutover is not None
        and deployment.generic_web_cutover.step != "blocked",
        post_deploy_status="pass" if deployment.deploy.status == "pass" else "fail",
    )
