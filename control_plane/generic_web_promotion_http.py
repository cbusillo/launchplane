from __future__ import annotations

import hashlib
from collections.abc import Callable
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.outbox_delivery import (
    OutboxDeliveryRecord,
    build_outbox_dedupe_key,
    build_outbox_delivery_id,
)
from control_plane.drivers.generic_web_dispatch import (
    GenericWebProdPromotionEnvelope,
    GenericWebPromotionWorkflowEnvelope,
    _GENERIC_WEB_PROD_PROMOTION_ROUTE,
    _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
)
from control_plane.workflows.generic_web_deploy import product_profile_uses_generic_web_base
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebDeployProvider,
    GenericWebResolvedDeployTarget,
)
from control_plane.workflows.generic_web_promotion import (
    GenericWebPromotionStore,
    GenericWebProdPromotionResult,
    execute_generic_web_prod_promotion,
    resolve_generic_web_promotion_lanes,
)
from control_plane.workflows.generic_web_promotion_workflow import (
    GenericWebPromotionWorkflowResult,
    _normalize_bump,
)
from control_plane.workflows.ship import utc_now_timestamp


GENERIC_WEB_PROD_PROMOTION_ROUTE = _GENERIC_WEB_PROD_PROMOTION_ROUTE.route_path
GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE = _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.route_path

__all__ = [
    "GENERIC_WEB_PROD_PROMOTION_ROUTE",
    "GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE",
    "GenericWebProdPromotionEnvelope",
    "GenericWebProdPromotionResponse",
    "GenericWebProdPromotionResponseResult",
    "GenericWebPromotionProductMismatchError",
    "GenericWebPromotionRouteDependencyError",
    "GenericWebPromotionWorkflowEnvelope",
    "GenericWebPromotionWorkflowResponse",
    "GenericWebPromotionWorkflowResponseResult",
    "dispatch_generic_web_promotion_workflow_result",
    "build_generic_web_promotion_workflow_outbox_delivery",
    "execute_generic_web_prod_promotion_result",
    "resolve_generic_web_promotion_destination_lane",
    "resolve_generic_web_promotion_workflow_lane",
    "should_store_generic_web_promotion_idempotency",
    "validate_generic_web_prod_promotion_lanes",
]


class GenericWebPromotionProductMismatchError(ValueError):
    pass


class GenericWebPromotionRouteDependencyError(ValueError):
    pass


class GenericWebProdPromotionRecords(BaseModel):
    model_config = ConfigDict(extra="forbid")

    promotion_record_id: str = ""
    deployment_record_id: str = ""
    backup_record_id: str = ""
    inventory_record_id: str = ""
    promotion_status: str = ""
    deployment_status: str = ""
    backup_status: str = ""
    source_health_status: str = ""
    destination_health_status: str = ""
    release_status: str = ""
    release_tag: str = ""
    release_url: str = ""
    dry_run: str = ""


class GenericWebProdPromotionResponseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = ""
    context: str = ""
    from_instance: str = ""
    to_instance: str = ""
    artifact_id: str = ""
    source_git_ref: str = ""
    backup_record_id: str = ""
    promotion_record_id: str = ""
    deployment_record_id: str = ""
    inventory_record_id: str = ""
    promotion_status: Literal["pending", "pass", "fail"] = "pending"
    deployment_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    backup_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    source_health_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    destination_health_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    release_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    release_tag: str = ""
    release_url: str = ""
    target_name: str = ""
    target_id: str = ""
    target_category: Literal[
        "application", "compose", "container", "service", "static", "unknown"
    ] = "unknown"
    provider_id: str = ""
    provider_target_type: str = ""
    dry_run: bool = False
    error_message: str = ""


class GenericWebProdPromotionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: GenericWebProdPromotionRecords
    result: GenericWebProdPromotionResponseResult
    replayed: bool | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    original_trace_id: str | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class GenericWebPromotionWorkflowResponseResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str = ""
    context: str = ""
    repository: str = ""
    workflow_id: str = ""
    ref: str = ""
    dry_run: bool = False
    bump: Literal["patch", "minor", "major"] = "patch"
    dispatch_status: Literal["pending", "dispatched"] = "pending"
    run_id: int = 0
    run_url: str = ""
    run_status: str = "pending"
    run_conclusion: str = ""


class GenericWebPromotionWorkflowResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: dict[str, str] = Field(default_factory=dict)
    result: GenericWebPromotionWorkflowResponseResult
    replayed: bool | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    original_trace_id: str | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


def resolve_generic_web_promotion_destination_lane(
    *, record_store: object, product: str, instance: str
) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile]:
    profile = _read_generic_web_promotion_profile(
        record_store=record_store,
        product=product,
    )
    normalized_instance = instance.strip()
    for lane in profile.lanes:
        if lane.instance.strip() == normalized_instance:
            return profile, lane
    raise GenericWebPromotionProductMismatchError(
        "Product profile does not own the requested driver lane."
    )


def resolve_generic_web_promotion_workflow_lane(
    *, record_store: object, product: str, context: str
) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile]:
    profile = _read_generic_web_promotion_profile(
        record_store=record_store,
        product=product,
    )
    normalized_context = context.strip()
    for lane in profile.lanes:
        if lane.context.strip() == normalized_context:
            return profile, lane
    raise GenericWebPromotionProductMismatchError(
        "Product profile does not own the requested driver lane."
    )


def validate_generic_web_prod_promotion_lanes(
    *, record_store: object, request: GenericWebProdPromotionEnvelope
) -> None:
    resolve_generic_web_promotion_lanes(
        record_store=cast(GenericWebPromotionStore, record_store),
        request=request.promotion,
    )


def execute_generic_web_prod_promotion_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: GenericWebProdPromotionEnvelope,
    deploy_provider: GenericWebDeployProvider | None = None,
    resolved_deploy_target: GenericWebResolvedDeployTarget | None = None,
    provider_operation_title: str = "",
    deployment_record_id: str = "",
    provider_effect_checkpoint: Callable[[str], None] | None = None,
) -> tuple[GenericWebProdPromotionRecords, GenericWebProdPromotionResult]:
    driver_result = execute_generic_web_prod_promotion(
        control_plane_root=control_plane_root,
        record_store=cast(GenericWebPromotionStore, record_store),
        request=request.promotion,
        deploy_provider=deploy_provider,
        resolved_deploy_target=resolved_deploy_target,
        provider_operation_title=provider_operation_title,
        deployment_record_id=deployment_record_id,
        provider_effect_checkpoint=provider_effect_checkpoint,
    )
    result = GenericWebProdPromotionResult.model_validate(
        _scrub_retired_target_type_alias(driver_result.model_dump(mode="json"))
    )
    return GenericWebProdPromotionRecords.model_validate(
        _prod_promotion_records(result.model_dump(mode="json"))
    ), result


def dispatch_generic_web_promotion_workflow_result(
    *,
    request: GenericWebPromotionWorkflowEnvelope,
    profile: LaunchplaneProductProfileRecord,
    delivery_key: str,
) -> tuple[dict[str, str], GenericWebPromotionWorkflowResult, OutboxDeliveryRecord]:
    delivery = build_generic_web_promotion_workflow_outbox_delivery(
        request=request,
        profile=profile,
        delivery_key=delivery_key,
    )
    workflow = profile.promotion_workflow
    bump = _normalize_bump(
        request.workflow.bump if request.workflow.bump is not None else workflow.default_bump
    )
    result = GenericWebPromotionWorkflowResult(
        product=profile.product,
        context=request.workflow.context,
        repository=profile.repository,
        workflow_id=workflow.workflow_id.strip(),
        ref=workflow.ref.strip(),
        dry_run=request.workflow.dry_run,
        bump=bump,
        dispatch_status="pending",
    )
    return {"outbox_delivery_id": delivery.delivery_id}, result, delivery


def build_generic_web_promotion_workflow_outbox_delivery(
    *,
    request: GenericWebPromotionWorkflowEnvelope,
    profile: LaunchplaneProductProfileRecord,
    delivery_key: str,
) -> OutboxDeliveryRecord:
    _repository_parts(profile.repository)
    workflow = profile.promotion_workflow
    workflow_id = workflow.workflow_id.strip()
    ref = workflow.ref.strip()
    bump = _normalize_bump(
        request.workflow.bump if request.workflow.bump is not None else workflow.default_bump
    )
    created_at = utc_now_timestamp()
    dedupe_key = build_outbox_dedupe_key(
        kind="github_workflow_dispatch",
        parts=(
            profile.product,
            request.workflow.context,
            workflow_id,
            ref,
            str(request.workflow.dry_run),
            bump,
            hashlib.sha256(delivery_key.strip().encode("utf-8")).hexdigest()[:20],
        ),
    )
    delivery_id = build_outbox_delivery_id(
        kind="github_workflow_dispatch",
        dedupe_key=dedupe_key,
    )
    inputs = {
        workflow.dry_run_input.strip(): str(request.workflow.dry_run).lower(),
        workflow.bump_input.strip(): bump,
        workflow.promotion_intent_input.strip(): delivery_id,
        **(
            {workflow.artifact_id_input.strip(): request.workflow.artifact_id}
            if request.workflow.artifact_id
            else {}
        ),
        **(
            {workflow.source_git_ref_input.strip(): request.workflow.source_git_ref}
            if request.workflow.source_git_ref
            else {}
        ),
    }
    return OutboxDeliveryRecord(
        delivery_id=delivery_id,
        kind="github_workflow_dispatch",
        aggregate_type="generic_web_promotion_workflow",
        aggregate_id=f"{profile.product}:{request.workflow.context}",
        dedupe_key=dedupe_key,
        created_at=created_at,
        updated_at=created_at,
        next_attempt_at=created_at,
        payload={
            "repository": profile.repository,
            "workflow_id": workflow_id,
            "ref": ref,
            "inputs": inputs,
            "credential_context": request.workflow.context,
            "observe_timeout_seconds": request.workflow.observe_timeout_seconds,
            "promotion_evidence_fingerprint": request.workflow.evidence_fingerprint,
        },
    )


def _repository_parts(repository: str) -> tuple[str, str]:
    owner, separator, repo = repository.strip().partition("/")
    owner = owner.strip()
    repo = repo.strip()
    if not separator or not owner or not repo or "/" in repo:
        raise GenericWebPromotionRouteDependencyError(
            "GitHub repository must use owner/repo format."
        )
    return owner, repo


def should_store_generic_web_promotion_idempotency(
    driver_result: GenericWebProdPromotionResult | GenericWebPromotionWorkflowResult,
) -> bool:
    payload = driver_result.model_dump(mode="json")
    statuses = _status_values(payload)
    if any(str(value).strip() == "blocked" for value in statuses):
        return False
    return not any(str(value).strip() == "fail" for value in statuses)


def _read_generic_web_promotion_profile(
    *, record_store: object, product: str
) -> LaunchplaneProductProfileRecord:
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        raise GenericWebPromotionRouteDependencyError(
            "Product driver validation requires product profile storage."
        )
    try:
        profile = read_profile(product.strip())
    except FileNotFoundError as error:
        raise GenericWebPromotionRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    if not product_profile_uses_generic_web_base(profile):
        raise GenericWebPromotionProductMismatchError(
            "Product profile is not compatible with the requested driver route."
        )
    return profile


def _prod_promotion_records(driver_result: dict[str, object]) -> dict[str, object]:
    keys = (
        "promotion_record_id",
        "deployment_record_id",
        "backup_record_id",
        "inventory_record_id",
        "promotion_status",
        "deployment_status",
        "backup_status",
        "source_health_status",
        "destination_health_status",
        "release_status",
        "release_tag",
        "release_url",
        "dry_run",
    )
    return {key: str(driver_result[key]) for key in keys if key in driver_result}


def _status_values(driver_result: dict[str, object]) -> tuple[object, ...]:
    return tuple(
        value for key, value in driver_result.items() if key.endswith("_status") or key == "status"
    )


def _scrub_retired_target_type_alias(driver_result: dict[str, object]) -> dict[str, object]:
    result = dict(driver_result)
    result.pop("target_type", None)
    return result
