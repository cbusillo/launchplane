from __future__ import annotations

from pathlib import Path
from typing import cast

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.drivers.generic_web_dispatch import (
    GenericWebProdPromotionEnvelope,
    GenericWebPromotionWorkflowEnvelope,
    _GENERIC_WEB_PROD_PROMOTION_ROUTE,
    _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
)
from control_plane.workflows.generic_web_deploy import product_profile_uses_generic_web_base
from control_plane.workflows.generic_web_promotion import (
    GenericWebPromotionStore,
    execute_generic_web_prod_promotion,
    resolve_generic_web_promotion_lanes,
)
from control_plane.workflows.generic_web_promotion_workflow import (
    dispatch_generic_web_promotion_workflow,
)


GENERIC_WEB_PROD_PROMOTION_ROUTE = _GENERIC_WEB_PROD_PROMOTION_ROUTE.route_path
GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE = _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.route_path
GENERIC_WEB_PROD_PROMOTION_ACTION = "generic_web_prod_promotion.execute"
GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ACTION = "generic_web_prod_promotion.dispatch"

__all__ = [
    "GENERIC_WEB_PROD_PROMOTION_ACTION",
    "GENERIC_WEB_PROD_PROMOTION_ROUTE",
    "GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ACTION",
    "GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE",
    "GenericWebProdPromotionEnvelope",
    "GenericWebPromotionProductMismatchError",
    "GenericWebPromotionRouteDependencyError",
    "GenericWebPromotionWorkflowEnvelope",
    "dispatch_generic_web_promotion_workflow_result",
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
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_generic_web_prod_promotion(
        control_plane_root=control_plane_root,
        record_store=cast(GenericWebPromotionStore, record_store),
        request=request.promotion,
    )
    result = _scrub_retired_target_type_alias(driver_result.model_dump(mode="json"))
    return _prod_promotion_records(result), result


def dispatch_generic_web_promotion_workflow_result(
    *,
    control_plane_root: Path,
    request: GenericWebPromotionWorkflowEnvelope,
    profile: LaunchplaneProductProfileRecord,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = dispatch_generic_web_promotion_workflow(
        control_plane_root=control_plane_root,
        profile=profile,
        request=request.workflow,
    )
    return {}, driver_result.model_dump(mode="json")


def should_store_generic_web_promotion_idempotency(
    driver_result: dict[str, object],
) -> bool:
    statuses = _status_values(driver_result)
    if any(str(value).strip() == "blocked" for value in statuses):
        return False
    if (
        str(driver_result.get("deployment_status", "")).strip() == "pass"
        and str(driver_result.get("post_deploy_status", "")).strip() == "fail"
    ):
        return True
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
