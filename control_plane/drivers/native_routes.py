from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Protocol, cast

from control_plane.contracts.driver_descriptor import DriverActionScope
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
from control_plane.drivers.route_paths import INGRESS_ROUTE_APPLY_ROUTE
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
from control_plane.odoo_prod_backup_gate_http import (
    ODOO_PROD_BACKUP_GATE_ROUTE,
    ODOO_PROD_BACKUP_VERIFICATION_ROUTE,
)
from control_plane.odoo_prod_backup_restore_http import (
    ODOO_PROD_BACKUP_RESTORE_APPLY_ROUTE,
    ODOO_PROD_BACKUP_RESTORE_PLAN_ROUTE,
)
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
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity

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
        INGRESS_ROUTE_APPLY_ROUTE,
        ODOO_ARTIFACT_PUBLISH_ROUTE,
        ODOO_APP_MAINTENANCE_ROUTE,
        ODOO_ARTIFACT_PUBLISH_INPUTS_ROUTE,
        ODOO_CONFIG_PARAMETER_OVERRIDE_ROUTE,
        ODOO_POST_DEPLOY_ROUTE,
        ODOO_PREVIEW_APPLY_INPUTS_ROUTE,
        ODOO_PREVIEW_APPLY_ROUTE,
        ODOO_PROD_BACKUP_GATE_ROUTE,
        ODOO_PROD_BACKUP_VERIFICATION_ROUTE,
        ODOO_PROD_BACKUP_RESTORE_APPLY_ROUTE,
        ODOO_PROD_BACKUP_RESTORE_PLAN_ROUTE,
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

_NATIVE_FASTAPI_DRIVER_ROUTE_PATHS_WITH_ALTERNATE_AUTHZ = frozenset({INGRESS_ROUTE_APPLY_ROUTE})
_NATIVE_DRIVER_ROUTE_METADATA_ATTRIBUTE = "__launchplane_native_driver_route_metadata__"


@dataclass(frozen=True)
class _DriverRouteMetadata:
    driver_id: str
    action_id: str
    route_path: str
    method: str
    authz_action: str
    alternate_authz_actions: tuple[str, ...]
    scope: DriverActionScope
    operator_visible: bool


class _AuthorizationAllows(Protocol):
    def __call__(
        self,
        *,
        identity: LaunchplaneIdentity,
        action: str,
        product: str,
        context: str,
        target: AuthorizationTarget | None,
    ) -> bool: ...


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


def _driver_route_metadata_from_descriptors() -> dict[str, _DriverRouteMetadata]:
    route_metadata: dict[str, _DriverRouteMetadata] = {}
    for descriptor in list_driver_descriptors():
        for action in descriptor.actions:
            route_path = action.route_path.strip()
            if route_path != action.route_path or not route_path.startswith("/v1/drivers/"):
                raise ValueError(
                    f"Driver action {descriptor.driver_id}.{action.action_id} "
                    "must declare a canonical /v1/drivers/ route_path."
                )
            method = str(action.method).strip().upper()
            if method not in {"GET", "POST"}:
                raise ValueError(
                    f"Driver action {descriptor.driver_id}.{action.action_id} "
                    "must declare method GET or POST."
                )
            authz_action = action.authz_action.strip()
            if not authz_action:
                raise ValueError(
                    f"Driver action {descriptor.driver_id}.{action.action_id} "
                    "must declare authz_action."
                )
            alternate_authz_actions = tuple(
                alternate_authz_action.strip()
                for alternate_authz_action in action.alternate_authz_actions
            )
            if any(
                not alternate_authz_action for alternate_authz_action in alternate_authz_actions
            ):
                raise ValueError(
                    f"Driver action {descriptor.driver_id}.{action.action_id} "
                    "must not declare a blank alternate_authz_action."
                )
            all_authz_actions = (authz_action, *alternate_authz_actions)
            if len(set(all_authz_actions)) != len(all_authz_actions):
                raise ValueError(
                    f"Driver action {descriptor.driver_id}.{action.action_id} "
                    "must declare unique authorization actions."
                )
            if route_path in route_metadata:
                raise ValueError(f"Duplicate driver action route path: {route_path}")
            route_metadata[route_path] = _DriverRouteMetadata(
                driver_id=descriptor.driver_id,
                action_id=action.action_id,
                route_path=route_path,
                method=method,
                authz_action=authz_action,
                alternate_authz_actions=alternate_authz_actions,
                scope=action.scope,
                operator_visible=action.operator_visible,
            )
    return route_metadata


def _descriptor_driver_route_metadata(route_path: str) -> _DriverRouteMetadata:
    try:
        return _driver_route_metadata_from_descriptors()[route_path]
    except KeyError as exc:
        raise ValueError(f"Unknown descriptor-backed driver route: {route_path}") from exc


def _descriptor_driver_route_authz_action(route_path: str) -> str:
    return _descriptor_driver_route_metadata(route_path).authz_action


def _is_native_fastapi_driver_route_path(route_path: str) -> bool:
    return route_path in _NATIVE_FASTAPI_DRIVER_ROUTE_PATHS


def _bind_native_fastapi_driver_handler(
    *,
    route_path: str,
    endpoint: Callable[..., object],
    declared_methods: Iterable[object] | None,
) -> _DriverRouteMetadata:
    if route_path not in _NATIVE_FASTAPI_DRIVER_ROUTE_PATHS:
        raise ValueError(f"Unknown native FastAPI driver route: {route_path}")
    route_metadata = _descriptor_driver_route_metadata(route_path)
    if declared_methods is not None:
        normalized_methods = frozenset(str(method).upper() for method in declared_methods)
        if normalized_methods != frozenset({route_metadata.method}):
            actual_methods = ", ".join(sorted(normalized_methods)) or "<none>"
            raise ValueError(
                "Native FastAPI driver handler method must match descriptor metadata: "
                f"{route_path} expects {route_metadata.method}, got {actual_methods}"
            )
    existing_metadata = getattr(endpoint, _NATIVE_DRIVER_ROUTE_METADATA_ATTRIBUTE, None)
    if existing_metadata is not None and existing_metadata != route_metadata:
        raise ValueError(
            "Native FastAPI driver handler cannot be bound to conflicting descriptor "
            f"metadata: {route_path}"
        )
    setattr(endpoint, _NATIVE_DRIVER_ROUTE_METADATA_ATTRIBUTE, route_metadata)
    return route_metadata


def _native_driver_route_metadata_for_handler(endpoint: object) -> _DriverRouteMetadata:
    route_metadata = getattr(endpoint, _NATIVE_DRIVER_ROUTE_METADATA_ATTRIBUTE, None)
    if not isinstance(route_metadata, _DriverRouteMetadata):
        raise ValueError("Native FastAPI driver handler is not bound to descriptor route metadata.")
    return route_metadata


def _native_driver_route_authz_action(endpoint: object) -> str:
    return _native_driver_route_metadata_for_handler(endpoint).authz_action


def _native_driver_route_authorization_allows(
    *,
    endpoint: object,
    authorization_allows: _AuthorizationAllows,
    identity: LaunchplaneIdentity,
    product: str,
    context: str,
    instances: tuple[str, ...] = (),
) -> bool:
    route_metadata = _native_driver_route_metadata_for_handler(endpoint)
    try:
        target = AuthorizationTarget(scope=route_metadata.scope, instances=instances)
    except ValueError:
        return False
    return authorization_allows(
        identity=identity,
        action=route_metadata.authz_action,
        product=product,
        context=context,
        target=target,
    )


def _native_driver_route_alternate_authz_action(endpoint: object) -> str:
    alternate_authz_actions = _native_driver_route_metadata_for_handler(
        endpoint
    ).alternate_authz_actions
    if len(alternate_authz_actions) != 1:
        raise ValueError(
            "Native FastAPI driver handler requires exactly one alternate descriptor "
            "authorization action."
        )
    return alternate_authz_actions[0]


def _validate_native_descriptor_driver_routes() -> None:
    descriptor_routes = _driver_route_metadata_from_descriptors()
    descriptor_route_paths = frozenset(descriptor_routes)
    post_descriptor_routes = frozenset(
        route_path
        for route_path, metadata in descriptor_routes.items()
        if metadata.method == "POST"
    )
    missing_post_descriptor_routes = sorted(
        post_descriptor_routes - _NATIVE_FASTAPI_DRIVER_ROUTE_PATHS
    )
    if missing_post_descriptor_routes:
        raise ValueError(
            "POST driver descriptor routes must be implemented as native FastAPI "
            f"routes: {', '.join(missing_post_descriptor_routes)}"
        )
    missing_non_post_descriptor_routes = sorted(
        descriptor_route_paths - post_descriptor_routes - _NATIVE_FASTAPI_DRIVER_ROUTE_PATHS
    )
    if missing_non_post_descriptor_routes:
        raise ValueError(
            "Driver descriptor routes must be implemented as native FastAPI routes: "
            f"{', '.join(missing_non_post_descriptor_routes)}"
        )
    routes_missing_descriptors = sorted(_NATIVE_FASTAPI_DRIVER_ROUTE_PATHS - descriptor_route_paths)
    if routes_missing_descriptors:
        raise ValueError(
            "Native FastAPI driver routes must declare descriptor metadata: "
            f"{', '.join(routes_missing_descriptors)}"
        )
    for route_path, route_metadata in descriptor_routes.items():
        expected_alternate_count = (
            1 if route_path in _NATIVE_FASTAPI_DRIVER_ROUTE_PATHS_WITH_ALTERNATE_AUTHZ else 0
        )
        if len(route_metadata.alternate_authz_actions) != expected_alternate_count:
            raise ValueError(
                "Native FastAPI driver handler alternate authorization behavior must match "
                f"descriptor metadata: {route_path}"
            )


def _validate_native_fastapi_driver_routes(app: object) -> None:
    _validate_native_descriptor_driver_routes()
    descriptor_routes = _driver_route_metadata_from_descriptors()
    route_registrations: dict[str, list[object]] = {
        route_path: [] for route_path in _NATIVE_FASTAPI_DRIVER_ROUTE_PATHS
    }
    for route in cast(Iterable[object], getattr(app, "routes", ())):
        route_path = getattr(route, "path", None)
        if isinstance(route_path, str) and route_path in route_registrations:
            route_registrations[route_path].append(route)

    missing_native_routes = sorted(
        route_path for route_path, registrations in route_registrations.items() if not registrations
    )
    if missing_native_routes:
        raise ValueError(
            "Native FastAPI driver routes must be registered by the FastAPI app: "
            f"{', '.join(missing_native_routes)}"
        )

    duplicate_native_routes = sorted(
        route_path
        for route_path, registrations in route_registrations.items()
        if len(registrations) > 1
    )
    if duplicate_native_routes:
        raise ValueError(
            "Native FastAPI driver routes must be registered exactly once: "
            f"{', '.join(duplicate_native_routes)}"
        )

    for route_path, registrations in route_registrations.items():
        route = registrations[0]
        route_metadata = descriptor_routes[route_path]
        route_methods = getattr(route, "methods", None)
        normalized_methods = (
            frozenset()
            if route_methods is None
            else frozenset(
                str(route_method).upper() for route_method in cast(Iterable[object], route_methods)
            )
        )
        if normalized_methods != frozenset({route_metadata.method}):
            actual_methods = ", ".join(sorted(normalized_methods)) or "<none>"
            raise ValueError(
                "Native FastAPI driver route methods must match descriptor metadata: "
                f"{route_path} expects {route_metadata.method}, got {actual_methods}"
            )
        handler_metadata = getattr(
            getattr(route, "endpoint", None),
            _NATIVE_DRIVER_ROUTE_METADATA_ATTRIBUTE,
            None,
        )
        if not isinstance(handler_metadata, _DriverRouteMetadata):
            raise ValueError(
                f"Native FastAPI driver handlers must bind descriptor route metadata: {route_path}"
            )
        if (
            handler_metadata.authz_action != route_metadata.authz_action
            or handler_metadata.alternate_authz_actions != route_metadata.alternate_authz_actions
        ):
            raise ValueError(
                "Native FastAPI driver handler authorization metadata must match descriptor "
                f"metadata: {route_path}"
            )
        if handler_metadata != route_metadata:
            raise ValueError(
                "Native FastAPI driver handler route metadata must match descriptor metadata: "
                f"{route_path}"
            )
