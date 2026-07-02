from __future__ import annotations

from pathlib import Path
from typing import cast

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.drivers.generic_web_dispatch import (
    GenericWebDeployEnvelope,
    _GENERIC_WEB_DEPLOY_ROUTE,
    _generic_web_post_deploy_executor_for_profile,
)
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployStore,
    execute_generic_web_deploy,
    product_profile_uses_generic_web_base,
)


GENERIC_WEB_DEPLOY_ROUTE = _GENERIC_WEB_DEPLOY_ROUTE.route_path
GENERIC_WEB_DEPLOY_ACTION = "generic_web_deploy.execute"

__all__ = [
    "GENERIC_WEB_DEPLOY_ACTION",
    "GENERIC_WEB_DEPLOY_ROUTE",
    "GenericWebDeployEnvelope",
    "GenericWebDeployProductMismatchError",
    "GenericWebDeployRouteDependencyError",
    "execute_generic_web_deploy_result",
    "resolve_generic_web_deploy_lane",
    "should_store_generic_web_deploy_idempotency",
]


class GenericWebDeployProductMismatchError(ValueError):
    pass


class GenericWebDeployRouteDependencyError(ValueError):
    pass


def resolve_generic_web_deploy_lane(
    *, record_store: object, product: str, instance: str
) -> tuple[LaunchplaneProductProfileRecord, ProductLaneProfile]:
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        raise GenericWebDeployRouteDependencyError(
            "Product driver validation requires product profile storage."
        )
    try:
        profile = read_profile(product.strip())
    except FileNotFoundError as error:
        raise GenericWebDeployRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    if not product_profile_uses_generic_web_base(profile):
        raise GenericWebDeployProductMismatchError(
            "Product profile is not compatible with the requested driver route."
        )
    normalized_instance = instance.strip()
    for lane in profile.lanes:
        if lane.instance.strip() == normalized_instance:
            return profile, lane
    raise GenericWebDeployProductMismatchError(
        "Product profile does not own the requested driver lane."
    )


def execute_generic_web_deploy_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: GenericWebDeployEnvelope,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_generic_web_deploy(
        control_plane_root=control_plane_root,
        record_store=cast(GenericWebDeployStore, record_store),
        request=request.deploy,
        profile=profile,
        lane=lane,
        post_deploy_executor=_generic_web_post_deploy_executor_for_profile(profile),
    )
    result = _scrub_retired_target_type_alias(driver_result.model_dump(mode="json"))
    return {"deployment_record_id": driver_result.deployment_record_id}, result


def should_store_generic_web_deploy_idempotency(driver_result: dict[str, object]) -> bool:
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


def _scrub_retired_target_type_alias(driver_result: dict[str, object]) -> dict[str, object]:
    result = dict(driver_result)
    result.pop("target_type", None)
    return result
