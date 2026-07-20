from typing import Annotated, Literal, Protocol, cast

from fastapi import Depends, Path, Query
from pydantic import BaseModel, ConfigDict

from control_plane import service_status as control_plane_service_status
from control_plane.contracts.edge_endpoint_record import EdgeEndpointRecord
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingReadModel,
    EnvironmentRouteBindingRecord,
    redacted_route_binding_record,
)
from control_plane.http_routes.support import (
    LAUNCHPLANE_SERVICE_CONTEXT,
    ApiRouteRegistrar,
    ReadRouteDependencies,
)
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity


class EdgeEndpointRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: EdgeEndpointRecord


class EdgeEndpointRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    limit: int
    count: int
    records: tuple[EdgeEndpointRecord, ...]


class PrivateHealthEndpointRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: PrivateHealthEndpointRecord


class PrivateHealthEndpointRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    product: str
    context: str
    instance: str
    limit: int
    count: int
    records: tuple[PrivateHealthEndpointRecord, ...]


class RouteBindingRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: EnvironmentRouteBindingReadModel


class RouteBindingRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    product: str
    context: str
    instance: str
    limit: int
    count: int
    records: tuple[EnvironmentRouteBindingReadModel, ...]


class EdgeEndpointReadStore(Protocol):
    def read_edge_endpoint_record(self, endpoint_key: str) -> EdgeEndpointRecord: ...

    def list_edge_endpoint_records(
        self,
        *,
        provider: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EdgeEndpointRecord, ...]: ...


class PrivateHealthEndpointReadStore(Protocol):
    def read_private_health_endpoint_record(
        self, endpoint_key: str
    ) -> PrivateHealthEndpointRecord: ...

    def list_private_health_endpoint_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[PrivateHealthEndpointRecord, ...]: ...


class RouteBindingReadStore(Protocol):
    def read_route_binding_record(
        self,
        *,
        product: str,
        context_name: str,
        instance_name: str,
    ) -> EnvironmentRouteBindingRecord: ...

    def list_route_binding_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        instance_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EnvironmentRouteBindingRecord, ...]: ...


def require_edge_endpoint_read_store(record_store: object) -> EdgeEndpointReadStore:
    required_methods = (
        "read_edge_endpoint_record",
        "list_edge_endpoint_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support edge endpoint reads: {missing_summary}"
        )
    return cast(EdgeEndpointReadStore, record_store)


def require_private_health_endpoint_read_store(
    record_store: object,
) -> PrivateHealthEndpointReadStore:
    required_methods = (
        "read_private_health_endpoint_record",
        "list_private_health_endpoint_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support private health endpoint reads: "
            f"{missing_summary}"
        )
    return cast(PrivateHealthEndpointReadStore, record_store)


def require_route_binding_read_store(record_store: object) -> RouteBindingReadStore:
    required_methods = (
        "read_route_binding_record",
        "list_route_binding_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support route binding reads: {missing_summary}"
        )
    return cast(RouteBindingReadStore, record_store)


def _ensure_edge_endpoint_read_allowed(
    *,
    dependencies: ReadRouteDependencies,
    identity: LaunchplaneIdentity,
    trace_id: str,
) -> None:
    if not dependencies.authorization_allows(
        identity=identity,
        action="edge_endpoint.read",
        product="launchplane",
        context=LAUNCHPLANE_SERVICE_CONTEXT,
    ):
        raise dependencies.http_error(
            status_code=403,
            trace_id=trace_id,
            code="authorization_denied",
            message="Workflow cannot read Launchplane edge endpoint records.",
        )


def _ensure_private_health_endpoint_read_allowed(
    *,
    dependencies: ReadRouteDependencies,
    identity: LaunchplaneIdentity,
    trace_id: str,
    product: str,
    context_name: str,
) -> None:
    if not dependencies.authorization_allows(
        identity=identity,
        action="private_health_endpoint.read",
        product=product,
        context=context_name,
    ):
        raise dependencies.http_error(
            status_code=403,
            trace_id=trace_id,
            code="authorization_denied",
            message=(
                "Workflow cannot read private health endpoints for the requested product/context."
            ),
        )


def _ensure_route_binding_read_allowed(
    *,
    dependencies: ReadRouteDependencies,
    identity: LaunchplaneIdentity,
    trace_id: str,
    product: str,
    context_name: str,
    instance_name: str,
) -> None:
    if not dependencies.authorization_allows(
        identity=identity,
        action="route_binding.read",
        product=product,
        context=context_name,
        target=(
            AuthorizationTarget(scope="instance", instances=(instance_name,))
            if instance_name
            else AuthorizationTarget(scope="context")
        ),
    ):
        raise dependencies.http_error(
            status_code=403,
            trace_id=trace_id,
            code="authorization_denied",
            message="Workflow cannot read route bindings for the requested product/context.",
        )


def register_topology_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def list_route_binding_records(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
        instance: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "25",
    ) -> RouteBindingRecordsResponse:
        trace_id = dependencies.next_trace_id()
        normalized_product = product.strip()
        context_name = context.strip()
        instance_name = instance.strip()
        if not normalized_product or not context_name:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message="Route binding list requires product and context query parameters.",
            )
        _ensure_route_binding_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
            product=normalized_product,
            context_name=context_name,
            instance_name=instance_name,
        )
        try:
            normalized_limit = control_plane_service_status.query_int_value(
                limit,
                "limit",
                default=25,
                minimum=1,
                maximum=100,
            )
            assert normalized_limit is not None
            route_binding_store = require_route_binding_read_store(record_store)
            records = route_binding_store.list_route_binding_records(
                product=normalized_product,
                context_name=context_name,
                instance_name=instance_name,
                status=status.strip(),
                limit=normalized_limit,
            )
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return RouteBindingRecordsResponse(
            trace_id=trace_id,
            product=normalized_product,
            context=context_name,
            instance=instance_name,
            limit=normalized_limit,
            count=len(records),
            records=tuple(redacted_route_binding_record(record) for record in records),
        )

    def read_route_binding_record(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
        instance: Annotated[str, Query()] = "",
    ) -> RouteBindingRecordResponse:
        trace_id = dependencies.next_trace_id()
        normalized_product = product.strip()
        context_name = context.strip()
        instance_name = instance.strip()
        if not normalized_product or not context_name or not instance_name:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=(
                    "Route binding record reads require product, context, and instance "
                    "query parameters."
                ),
            )
        _ensure_route_binding_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
            product=normalized_product,
            context_name=context_name,
            instance_name=instance_name,
        )
        try:
            route_binding_store = require_route_binding_read_store(record_store)
            record = route_binding_store.read_route_binding_record(
                product=normalized_product,
                context_name=context_name,
                instance_name=instance_name,
            )
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return RouteBindingRecordResponse(
            trace_id=trace_id,
            record=redacted_route_binding_record(record),
        )

    def list_edge_endpoint_records(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        limit: Annotated[str, Query()] = "25",
        provider: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
    ) -> EdgeEndpointRecordsResponse:
        trace_id = dependencies.next_trace_id()
        _ensure_edge_endpoint_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
        )
        try:
            normalized_limit = control_plane_service_status.query_int_value(
                limit,
                "limit",
                default=25,
                minimum=1,
                maximum=100,
            )
            assert normalized_limit is not None
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        try:
            edge_endpoint_store = require_edge_endpoint_read_store(record_store)
            records = edge_endpoint_store.list_edge_endpoint_records(
                provider=provider.strip(),
                status=status.strip(),
                limit=normalized_limit,
            )
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return EdgeEndpointRecordsResponse(
            trace_id=trace_id,
            limit=normalized_limit,
            count=len(records),
            records=records,
        )

    def read_edge_endpoint_record(
        endpoint_key: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> EdgeEndpointRecordResponse:
        trace_id = dependencies.next_trace_id()
        _ensure_edge_endpoint_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
        )
        try:
            edge_endpoint_store = require_edge_endpoint_read_store(record_store)
            record = edge_endpoint_store.read_edge_endpoint_record(endpoint_key)
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return EdgeEndpointRecordResponse(trace_id=trace_id, record=record)

    def list_private_health_endpoint_records(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
        instance: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "25",
    ) -> PrivateHealthEndpointRecordsResponse:
        trace_id = dependencies.next_trace_id()
        normalized_product = product.strip()
        context_name = context.strip()
        instance_name = instance.strip()
        if not normalized_product or not context_name:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=(
                    "Private health endpoint reads require product and context query parameters."
                ),
            )
        _ensure_private_health_endpoint_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
            product=normalized_product,
            context_name=context_name,
        )
        try:
            normalized_limit = control_plane_service_status.query_int_value(
                limit,
                "limit",
                default=25,
                minimum=1,
                maximum=100,
            )
            assert normalized_limit is not None
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        try:
            private_endpoint_store = require_private_health_endpoint_read_store(record_store)
            records = private_endpoint_store.list_private_health_endpoint_records(
                product=normalized_product,
                context_name=context_name,
                instance_name=instance_name,
                status=status.strip(),
                limit=normalized_limit,
            )
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return PrivateHealthEndpointRecordsResponse(
            trace_id=trace_id,
            product=normalized_product,
            context=context_name,
            instance=instance_name,
            limit=normalized_limit,
            count=len(records),
            records=records,
        )

    def read_private_health_endpoint_record(
        endpoint_key: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
        instance: Annotated[str, Query()] = "",
    ) -> PrivateHealthEndpointRecordResponse:
        trace_id = dependencies.next_trace_id()
        normalized_product = product.strip()
        context_name = context.strip()
        instance_name = instance.strip()
        if not normalized_product or not context_name:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=(
                    "Private health endpoint reads require product and context query parameters."
                ),
            )
        _ensure_private_health_endpoint_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
            product=normalized_product,
            context_name=context_name,
        )
        try:
            private_endpoint_store = require_private_health_endpoint_read_store(record_store)
            record = private_endpoint_store.read_private_health_endpoint_record(endpoint_key)
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if (
            record.product != normalized_product
            or record.context != context_name
            or (instance_name and record.instance != instance_name)
        ):
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"Record not found: {endpoint_key}",
            )
        return PrivateHealthEndpointRecordResponse(trace_id=trace_id, record=record)

    app.add_api_route(
        "/v1/route-bindings/records",
        list_route_binding_records,
        methods=["GET"],
        response_model=RouteBindingRecordsResponse,
        operation_id="list_route_binding_records",
        summary="List provider-neutral environment route bindings",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
    app.add_api_route(
        "/v1/route-bindings/records/current",
        read_route_binding_record,
        methods=["GET"],
        response_model=RouteBindingRecordResponse,
        operation_id="read_route_binding_record",
        summary="Read one provider-neutral environment route binding",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
    app.add_api_route(
        "/v1/edge-endpoints/records",
        list_edge_endpoint_records,
        methods=["GET"],
        response_model=EdgeEndpointRecordsResponse,
        operation_id="list_edge_endpoint_records",
        summary="List edge endpoint records",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
    app.add_api_route(
        "/v1/edge-endpoints/records/{endpoint_key}",
        read_edge_endpoint_record,
        methods=["GET"],
        response_model=EdgeEndpointRecordResponse,
        operation_id="read_edge_endpoint_record",
        summary="Read one edge endpoint record",
        responses={
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
    app.add_api_route(
        "/v1/private-health-endpoints/records",
        list_private_health_endpoint_records,
        methods=["GET"],
        response_model=PrivateHealthEndpointRecordsResponse,
        operation_id="list_private_health_endpoint_records",
        summary="List private health endpoint records",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
    app.add_api_route(
        "/v1/private-health-endpoints/records/{endpoint_key}",
        read_private_health_endpoint_record,
        methods=["GET"],
        response_model=PrivateHealthEndpointRecordResponse,
        operation_id="read_private_health_endpoint_record",
        summary="Read one private health endpoint record",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
