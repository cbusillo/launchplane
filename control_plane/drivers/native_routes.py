from __future__ import annotations

from dataclasses import dataclass
from typing import Iterable, cast

from control_plane.drivers.generic_web_dispatch import (
    _GENERIC_WEB_DEPLOY_ROUTE,
    _GENERIC_WEB_PROD_PROMOTION_ROUTE,
    _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
    _GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
    _GENERIC_WEB_ROLLBACK_ROUTE,
    _GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
)
from control_plane.drivers.generic_web_preview_dispatch import (
    _GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE,
    _GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
    _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE,
    _GENERIC_WEB_PREVIEW_READINESS_ROUTE,
    _GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
    _GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
)
from control_plane.drivers.registry import list_driver_descriptors
from control_plane.odoo_app_maintenance_http import ODOO_APP_MAINTENANCE_ROUTE
from control_plane.odoo_artifact_publish_http import ODOO_ARTIFACT_PUBLISH_ROUTE
from control_plane.odoo_artifact_publish_inputs_http import ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE
from control_plane.odoo_post_deploy_http import (
    ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE,
    ODOO_POST_DEPLOY_ROUTE,
    ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE,
)
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
    ODOO_PREVIEW_APPLY_ROUTE,
)
from control_plane.odoo_prod_backup_gate_http import ODOO_PROD_BACKUP_GATE_ROUTE
from control_plane.odoo_prod_promotion_http import (
    ODOO_PROD_PROMOTION_INPUTS_ROUTE,
    ODOO_PROD_PROMOTION_ROUTE,
    ODOO_PROD_PROMOTION_RUN_ROUTE,
)
from control_plane.odoo_prod_rollback_http import ODOO_PROD_ROLLBACK_ROUTE
from control_plane.odoo_stable_bootstrap_http import ODOO_STABLE_BOOTSTRAP_ROUTE
from control_plane.odoo_target_replacement_apply_http import ODOO_TARGET_REPLACEMENT_APPLY_ROUTE
from control_plane.odoo_target_replacement_plan_http import ODOO_TARGET_REPLACEMENT_PLAN_ROUTE
from control_plane.verireel_nonprod_http import (
    _VERIREEL_APP_MAINTENANCE_ROUTE,
    _VERIREEL_TESTING_DEPLOY_ROUTE,
)
from control_plane.verireel_prod_http import (
    _VERIREEL_PROD_BACKUP_GATE_ROUTE,
    _VERIREEL_PROD_DEPLOY_ROUTE,
    _VERIREEL_PROD_PROMOTION_ROUTE,
    _VERIREEL_PROD_ROLLBACK_ROUTE,
)
from control_plane.verireel_read_http import (
    _VERIREEL_PREVIEW_DESTROY_ROUTE,
    _VERIREEL_PREVIEW_INVENTORY_ROUTE,
    _VERIREEL_PREVIEW_REFRESH_ROUTE,
    _VERIREEL_PREVIEW_VERIFICATION_ROUTE,
    _VERIREEL_RUNTIME_VERIFICATION_ROUTE,
    _VERIREEL_STABLE_ENVIRONMENT_ROUTE,
    _VERIREEL_TESTING_VERIFICATION_ROUTE,
)

_NATIVE_FASTAPI_DRIVER_ROUTE_PATHS = frozenset(
    {
        _GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE.route_path,
        _GENERIC_WEB_PREVIEW_DESTROY_ROUTE.route_path,
        _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE.route_path,
        _GENERIC_WEB_PREVIEW_READINESS_ROUTE.route_path,
        _GENERIC_WEB_PREVIEW_REFRESH_ROUTE.route_path,
        _GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE.route_path,
        _GENERIC_WEB_DEPLOY_ROUTE.route_path,
        _GENERIC_WEB_PROD_PROMOTION_ROUTE.route_path,
        _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE.route_path,
        _GENERIC_WEB_ROLLBACK_PLAN_ROUTE.route_path,
        _GENERIC_WEB_ROLLBACK_ROUTE.route_path,
        _GENERIC_WEB_STABLE_VERIFICATION_ROUTE.route_path,
        _VERIREEL_PREVIEW_DESTROY_ROUTE.route_path,
        _VERIREEL_PREVIEW_INVENTORY_ROUTE.route_path,
        _VERIREEL_PREVIEW_REFRESH_ROUTE.route_path,
        _VERIREEL_PREVIEW_VERIFICATION_ROUTE.route_path,
        _VERIREEL_RUNTIME_VERIFICATION_ROUTE.route_path,
        _VERIREEL_STABLE_ENVIRONMENT_ROUTE.route_path,
        _VERIREEL_TESTING_DEPLOY_ROUTE.route_path,
        _VERIREEL_TESTING_VERIFICATION_ROUTE.route_path,
        _VERIREEL_APP_MAINTENANCE_ROUTE.route_path,
        _VERIREEL_PROD_BACKUP_GATE_ROUTE.route_path,
        _VERIREEL_PROD_DEPLOY_ROUTE.route_path,
        _VERIREEL_PROD_PROMOTION_ROUTE.route_path,
        _VERIREEL_PROD_ROLLBACK_ROUTE.route_path,
        "/v1/drivers/ingress/route-apply",
        ODOO_ARTIFACT_PUBLISH_ROUTE,
        ODOO_APP_MAINTENANCE_ROUTE,
        ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
        ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE,
        ODOO_POST_DEPLOY_ROUTE,
        ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
        ODOO_PREVIEW_APPLY_ROUTE,
        ODOO_PROD_BACKUP_GATE_ROUTE,
        ODOO_PROD_PROMOTION_INPUTS_ROUTE,
        ODOO_PROD_PROMOTION_ROUTE,
        ODOO_PROD_PROMOTION_RUN_ROUTE,
        ODOO_PROD_ROLLBACK_ROUTE,
        ODOO_STABLE_BOOTSTRAP_ROUTE,
        ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
        ODOO_TARGET_REPLACEMENT_PLAN_ROUTE,
        ODOO_WEBSITE_BOOTSTRAP_OVERRIDE_ROUTE,
    }
)


@dataclass(frozen=True)
class _DriverRouteMetadata:
    driver_id: str
    action_id: str
    method: str
    authz_action: str
    operator_visible: bool


def _fastapi_route_paths_by_method(app: object, method: str) -> frozenset[str]:
    normalized_method = method.upper()
    route_paths: set[str] = set()
    for route in cast(Iterable[object], getattr(app, "routes", ())):
        route_path = getattr(route, "path", None)
        route_methods = getattr(route, "methods", None)
        if not isinstance(route_path, str) or route_methods is None:
            continue
        methods = {
            str(route_method).upper() for route_method in cast(Iterable[object], route_methods)
        }
        if normalized_method in methods:
            route_paths.add(route_path)
    return frozenset(route_paths)


def _validate_native_fastapi_driver_route_paths(app: object) -> None:
    missing_native_routes = sorted(
        _NATIVE_FASTAPI_DRIVER_ROUTE_PATHS - _fastapi_route_paths_by_method(app, "POST")
    )
    if missing_native_routes:
        raise ValueError(
            "Native FastAPI driver routes must be registered by the FastAPI app: "
            f"{', '.join(missing_native_routes)}"
        )


def _driver_route_metadata_from_descriptors() -> dict[str, _DriverRouteMetadata]:
    route_metadata: dict[str, _DriverRouteMetadata] = {}
    for descriptor in list_driver_descriptors():
        for action in descriptor.actions:
            if not action.route_path.startswith("/v1/drivers/"):
                continue
            if not action.authz_action:
                raise ValueError(
                    f"Driver action {descriptor.driver_id}.{action.action_id} "
                    "must declare authz_action."
                )
            if action.route_path in route_metadata:
                raise ValueError(f"Duplicate driver action route path: {action.route_path}")
            route_metadata[action.route_path] = _DriverRouteMetadata(
                driver_id=descriptor.driver_id,
                action_id=action.action_id,
                method=action.method,
                authz_action=action.authz_action,
                operator_visible=action.operator_visible,
            )
    return route_metadata


def _descriptor_driver_authz_action(route_path: str) -> str:
    try:
        return _driver_route_metadata_from_descriptors()[route_path].authz_action
    except KeyError as exc:
        raise ValueError(f"Unknown descriptor-backed driver route: {route_path}") from exc


def _validate_native_descriptor_driver_routes() -> None:
    descriptor_routes = _driver_route_metadata_from_descriptors()
    post_descriptor_routes = frozenset(
        route_path
        for route_path, route_metadata in descriptor_routes.items()
        if route_metadata.method == "POST"
    )
    missing_post_descriptor_routes = sorted(
        post_descriptor_routes - _NATIVE_FASTAPI_DRIVER_ROUTE_PATHS
    )
    if missing_post_descriptor_routes:
        raise ValueError(
            "POST driver descriptor routes must be implemented as native FastAPI "
            f"routes: {', '.join(missing_post_descriptor_routes)}"
        )
