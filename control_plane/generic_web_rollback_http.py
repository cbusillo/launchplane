from __future__ import annotations

from pathlib import Path
from typing import cast

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.drivers.generic_web_dispatch import (
    GenericWebRollbackEnvelope,
    GenericWebRollbackPlanEnvelope,
    _GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
    _GENERIC_WEB_ROLLBACK_ROUTE,
    _generic_web_post_deploy_executor_for_profile,
)
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.contracts.generic_web_rollback import (
    GenericWebRollbackPlanStore,
    execute_generic_web_rollback_plan,
)
from control_plane.workflows.generic_web_rollback import (
    GenericWebRollbackApplyStore,
    execute_generic_web_rollback,
)


GENERIC_WEB_DRIVER_ID = "generic-web"
GENERIC_WEB_ROLLBACK_PLAN_ROUTE = _GENERIC_WEB_ROLLBACK_PLAN_ROUTE.route_path
GENERIC_WEB_ROLLBACK_ROUTE = _GENERIC_WEB_ROLLBACK_ROUTE.route_path
GENERIC_WEB_ROLLBACK_PLAN_ACTION = "generic_web_prod_rollback.plan"
GENERIC_WEB_ROLLBACK_ACTION = "generic_web_prod_rollback.execute"

__all__ = [
    "GENERIC_WEB_ROLLBACK_ACTION",
    "GENERIC_WEB_ROLLBACK_PLAN_ACTION",
    "GENERIC_WEB_ROLLBACK_PLAN_ROUTE",
    "GENERIC_WEB_ROLLBACK_ROUTE",
    "GenericWebRollbackEnvelope",
    "GenericWebRollbackPlanEnvelope",
    "GenericWebRollbackProductMismatchError",
    "GenericWebRollbackRouteDependencyError",
    "execute_generic_web_rollback_plan_result",
    "execute_generic_web_rollback_result",
    "resolve_generic_web_rollback_lane",
    "should_store_generic_web_rollback_idempotency",
]


class GenericWebRollbackProductMismatchError(ValueError):
    pass


class GenericWebRollbackRouteDependencyError(ValueError):
    pass


def resolve_generic_web_rollback_lane(
    *, record_store: object, product: str, route_path: str, instance: str
) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile]:
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        raise ValueError("Product driver validation requires product profile storage.")
    try:
        profile = read_profile(product.strip())
    except FileNotFoundError as error:
        raise GenericWebRollbackRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    if not product_profile_supports_generic_web_route(profile=profile, route_path=route_path):
        raise GenericWebRollbackProductMismatchError(
            "Product profile is not compatible with the requested driver route."
        )
    normalized_instance = instance.strip()
    for lane in profile.lanes:
        if lane.instance.strip() == normalized_instance:
            return profile, lane
    raise GenericWebRollbackProductMismatchError(
        "Product profile does not own the requested driver lane."
    )


def product_profile_supports_generic_web_route(
    *, profile: LaunchplaneProductProfileRecord, route_path: str
) -> bool:
    profile_driver_id = profile.driver_id.strip()
    if profile_driver_id == GENERIC_WEB_DRIVER_ID:
        return True
    try:
        descriptor = read_driver_descriptor(profile_driver_id)
    except FileNotFoundError:
        return False
    return descriptor.base_driver_id == GENERIC_WEB_DRIVER_ID and route_path in {
        GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
        GENERIC_WEB_ROLLBACK_ROUTE,
    }


def execute_generic_web_rollback_plan_result(
    *, record_store: object, request: GenericWebRollbackPlanEnvelope
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_generic_web_rollback_plan(
        record_store=cast(GenericWebRollbackPlanStore, record_store),
        request=request.rollback_plan,
    )
    return {"generic_web_rollback_plan_id": driver_result.plan_id}, driver_result.model_dump(
        mode="json"
    )


def execute_generic_web_rollback_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: GenericWebRollbackEnvelope,
    profile: LaunchplaneProductProfileRecord,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_generic_web_rollback(
        control_plane_root=control_plane_root,
        record_store=cast(GenericWebRollbackApplyStore, record_store),
        request=request.rollback,
        post_deploy_executor=_generic_web_post_deploy_executor_for_profile(profile),
    )
    records: dict[str, object] = {
        "generic_web_rollback_plan_id": driver_result.plan_id,
        "deployment_record_id": driver_result.deployment_record_id,
        "rollback_status": driver_result.rollback_status,
        "deploy_status": driver_result.deploy_status,
        "post_deploy_status": driver_result.post_deploy_status,
    }
    return records, driver_result.model_dump(mode="json")


def should_store_generic_web_rollback_idempotency(driver_result: dict[str, object]) -> bool:
    if any(str(value).strip() == "blocked" for value in _status_values(driver_result)):
        return False
    if (
        str(driver_result.get("deploy_status", "")).strip() == "pass"
        and str(driver_result.get("post_deploy_status", "")).strip() == "fail"
    ):
        return True
    return not any(str(value).strip() == "fail" for value in _status_values(driver_result))


def _status_values(driver_result: dict[str, object]) -> tuple[object, ...]:
    return tuple(
        value for key, value in driver_result.items() if key.endswith("_status") or key == "status"
    )
