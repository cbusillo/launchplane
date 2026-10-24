"""Declaration-bound observation using the existing revocable DB service-read grant."""

from collections.abc import Callable, Iterable
from typing import Any

from control_plane.contracts.operation_descriptor import OperationDescriptor


OBSERVATION_AUTHZ_ACTION = "launchplane_service.read"
_OPERATION_DESCRIPTOR_ATTRIBUTE = "__launchplane_operation_descriptor__"


def bind_operation_handler(
    *,
    descriptor: OperationDescriptor,
    endpoint: Callable[..., object],
    declared_methods: Iterable[object] | None,
) -> None:
    if (
        not descriptor.route_path.startswith("/v1/")
        or descriptor.route_path != descriptor.route_path.strip()
        or not descriptor.authz_action.strip()
        or descriptor.authz_action != descriptor.authz_action.strip()
        or any(not mode.strip() or mode != mode.strip() for mode in descriptor.mode_effects)
    ):
        raise ValueError("Operation requires canonical route, action and mode declarations.")
    if declared_methods is not None and frozenset(
        str(method).upper() for method in declared_methods
    ) != frozenset({descriptor.method}):
        raise ValueError("Operation methods must match the source descriptor.")
    existing = getattr(endpoint, _OPERATION_DESCRIPTOR_ATTRIBUTE, None)
    if existing is not None and existing != descriptor:
        raise ValueError("Operation handler has a conflicting source descriptor.")
    setattr(endpoint, _OPERATION_DESCRIPTOR_ATTRIBUTE, descriptor.model_copy(deep=True))


def operation_observation_is_declared(*, endpoint: object, mode: str) -> bool:
    descriptor = getattr(endpoint, _OPERATION_DESCRIPTOR_ATTRIBUTE, None)
    return (
        isinstance(descriptor, OperationDescriptor)
        and descriptor.mode_effects.get(mode) == "observation"
    )


def validate_operation_routes(app: Any) -> None:
    """Registered declarations must remain attached to the exact executable handler."""
    for route in app.routes:
        registered = getattr(route, _OPERATION_DESCRIPTOR_ATTRIBUTE, None)
        if registered is None:
            continue
        bound = getattr(route.endpoint, _OPERATION_DESCRIPTOR_ATTRIBUTE, None)
        if (
            bound != registered
            or route.path != registered.route_path
            or frozenset(route.methods) != frozenset({registered.method})
        ):
            raise ValueError("Registered operation does not match its source declaration.")


def register_operation_route(
    app: Any,
    *,
    descriptor: OperationDescriptor,
    endpoint: Callable[..., object],
    **route_options: Any,
) -> None:
    bind_operation_handler(
        descriptor=descriptor, endpoint=endpoint, declared_methods=(descriptor.method,)
    )
    app.add_api_route(descriptor.route_path, endpoint, methods=[descriptor.method], **route_options)
    setattr(app.routes[-1], _OPERATION_DESCRIPTOR_ATTRIBUTE, descriptor.model_copy(deep=True))
    validate_operation_routes(app)
