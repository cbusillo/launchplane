from typing import Annotated, Literal, Protocol, cast

from fastapi import Depends, Path, Query
from pydantic import BaseModel, ConfigDict

from control_plane import service_status as control_plane_service_status
from control_plane.contracts.ingress_canary_route_record import IngressCanaryRouteRecord
from control_plane.contracts.ingress_route_audit_record import IngressRouteAuditRecord
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import LaunchplaneIdentity

_LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"


class IngressCanaryRouteRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: IngressCanaryRouteRecord


class IngressCanaryRouteRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    limit: int
    count: int
    records: tuple[IngressCanaryRouteRecord, ...]


class IngressRouteAuditRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: IngressRouteAuditRecord


class IngressRouteAuditRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    product: str
    context: str
    limit: int
    count: int
    records: tuple[IngressRouteAuditRecord, ...]


class IngressCanaryRouteReadStore(Protocol):
    def read_ingress_canary_route_record(self, canary_key: str) -> IngressCanaryRouteRecord: ...

    def list_ingress_canary_route_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[IngressCanaryRouteRecord, ...]: ...


class IngressRouteAuditRecordReadStore(Protocol):
    def read_ingress_route_audit_record(self, record_id: str) -> IngressRouteAuditRecord: ...

    def list_ingress_route_audit_records(
        self,
        *,
        product: str = "",
        context_name: str = "",
        limit: int | None = None,
    ) -> tuple[IngressRouteAuditRecord, ...]: ...


def require_ingress_canary_route_read_store(
    record_store: object,
) -> IngressCanaryRouteReadStore:
    required_methods = (
        "read_ingress_canary_route_record",
        "list_ingress_canary_route_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support ingress canary route reads: "
            f"{missing_summary}"
        )
    return cast(IngressCanaryRouteReadStore, record_store)


def require_ingress_route_audit_record_read_store(
    record_store: object,
) -> IngressRouteAuditRecordReadStore:
    required_methods = (
        "read_ingress_route_audit_record",
        "list_ingress_route_audit_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support ingress route audit reads: "
            f"{missing_summary}"
        )
    return cast(IngressRouteAuditRecordReadStore, record_store)


def filter_ingress_route_audit_records(
    records: tuple[IngressRouteAuditRecord, ...],
    *,
    status: str = "",
    mode: str = "",
    provider_host_id: int | None = None,
    trace_id: str = "",
    idempotency_key: str = "",
) -> tuple[IngressRouteAuditRecord, ...]:
    filtered_records = records
    if status:
        filtered_records = tuple(record for record in filtered_records if record.status == status)
    if mode:
        filtered_records = tuple(record for record in filtered_records if record.mode == mode)
    if provider_host_id is not None:
        filtered_records = tuple(
            record for record in filtered_records if record.provider_host_id == provider_host_id
        )
    if trace_id:
        filtered_records = tuple(
            record for record in filtered_records if record.trace_id == trace_id
        )
    if idempotency_key:
        filtered_records = tuple(
            record for record in filtered_records if record.idempotency_key == idempotency_key
        )
    return filtered_records


def _ensure_ingress_canary_route_read_allowed(
    *,
    dependencies: ReadRouteDependencies,
    identity: LaunchplaneIdentity,
    trace_id: str,
) -> None:
    if not dependencies.authorization_allows(
        identity=identity,
        action="ingress_canary_route.read",
        product="launchplane",
        context=_LAUNCHPLANE_SERVICE_CONTEXT,
    ):
        raise dependencies.http_error(
            status_code=403,
            trace_id=trace_id,
            code="authorization_denied",
            message="Workflow cannot read Launchplane ingress canary route records.",
        )


def _ensure_ingress_route_audit_read_allowed(
    *,
    dependencies: ReadRouteDependencies,
    identity: LaunchplaneIdentity,
    trace_id: str,
    product: str,
    context_name: str,
) -> None:
    if not dependencies.authorization_allows(
        identity=identity,
        action="ingress_route.plan",
        product=product,
        context=context_name,
    ):
        raise dependencies.http_error(
            status_code=403,
            trace_id=trace_id,
            code="authorization_denied",
            message=(
                "Workflow cannot read ingress route audit records for the requested "
                "product/context."
            ),
        )


def register_ingress_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def list_ingress_canary_route_records(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        limit: Annotated[str, Query()] = "25",
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
    ) -> IngressCanaryRouteRecordsResponse:
        trace_id = dependencies.next_trace_id()
        _ensure_ingress_canary_route_read_allowed(
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
            canary_store = require_ingress_canary_route_read_store(record_store)
            records = canary_store.list_ingress_canary_route_records(
                product=product.strip(),
                context_name=context.strip(),
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
        return IngressCanaryRouteRecordsResponse(
            trace_id=trace_id,
            limit=normalized_limit,
            count=len(records),
            records=records,
        )

    def read_ingress_canary_route_record(
        canary_key: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> IngressCanaryRouteRecordResponse:
        trace_id = dependencies.next_trace_id()
        _ensure_ingress_canary_route_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
        )
        try:
            canary_store = require_ingress_canary_route_read_store(record_store)
            record = canary_store.read_ingress_canary_route_record(canary_key)
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
        return IngressCanaryRouteRecordResponse(trace_id=trace_id, record=record)

    def list_ingress_route_audit_records(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        mode: Annotated[str, Query()] = "",
        provider_host_id: Annotated[str, Query()] = "",
        trace_id: Annotated[str, Query()] = "",
        idempotency_key: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "25",
    ) -> IngressRouteAuditRecordsResponse:
        request_trace_id = dependencies.next_trace_id()
        normalized_product = product.strip()
        context_name = context.strip()
        if not normalized_product or not context_name:
            raise dependencies.http_error(
                status_code=400,
                trace_id=request_trace_id,
                code="invalid_query",
                message="Ingress route audit list requires product and context query parameters.",
            )
        _ensure_ingress_route_audit_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=request_trace_id,
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
            normalized_provider_host_id = control_plane_service_status.query_int_value(
                provider_host_id,
                "provider_host_id",
                minimum=1,
            )
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=request_trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        try:
            audit_store = require_ingress_route_audit_record_read_store(record_store)
            records = audit_store.list_ingress_route_audit_records(
                product=normalized_product,
                context_name=context_name,
                limit=None,
            )
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=request_trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        records = filter_ingress_route_audit_records(
            records,
            status=status.strip(),
            mode=mode.strip(),
            provider_host_id=normalized_provider_host_id,
            trace_id=trace_id.strip(),
            idempotency_key=idempotency_key.strip(),
        )
        limited_records = records[:normalized_limit]
        return IngressRouteAuditRecordsResponse(
            trace_id=request_trace_id,
            product=normalized_product,
            context=context_name,
            limit=normalized_limit,
            count=len(limited_records),
            records=limited_records,
        )

    def read_ingress_route_audit_record(
        record_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        product: Annotated[str, Query()] = "",
        context: Annotated[str, Query()] = "",
    ) -> IngressRouteAuditRecordResponse:
        request_trace_id = dependencies.next_trace_id()
        normalized_product = product.strip()
        context_name = context.strip()
        if not normalized_product or not context_name:
            raise dependencies.http_error(
                status_code=400,
                trace_id=request_trace_id,
                code="invalid_query",
                message=(
                    "Ingress route audit record reads require product and context query parameters."
                ),
            )
        _ensure_ingress_route_audit_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=request_trace_id,
            product=normalized_product,
            context_name=context_name,
        )
        try:
            audit_store = require_ingress_route_audit_record_read_store(record_store)
            record = audit_store.read_ingress_route_audit_record(record_id)
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=request_trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=request_trace_id,
                code="not_found",
                message=str(error),
            ) from error
        if record.product != normalized_product or record.context != context_name:
            raise dependencies.http_error(
                status_code=404,
                trace_id=request_trace_id,
                code="not_found",
                message=f"Record not found: {record_id}",
            )
        return IngressRouteAuditRecordResponse(trace_id=request_trace_id, record=record)

    app.add_api_route(
        "/v1/ingress/canary-routes/records",
        list_ingress_canary_route_records,
        methods=["GET"],
        response_model=IngressCanaryRouteRecordsResponse,
        operation_id="list_ingress_canary_route_records",
        summary="List ingress canary route records",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
    app.add_api_route(
        "/v1/ingress/canary-routes/records/{canary_key}",
        read_ingress_canary_route_record,
        methods=["GET"],
        response_model=IngressCanaryRouteRecordResponse,
        operation_id="read_ingress_canary_route_record",
        summary="Read one ingress canary route record",
        responses={
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
    app.add_api_route(
        "/v1/ingress/route-audits/records",
        list_ingress_route_audit_records,
        methods=["GET"],
        response_model=IngressRouteAuditRecordsResponse,
        operation_id="list_ingress_route_audit_records",
        summary="List ingress route audit records",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
    app.add_api_route(
        "/v1/ingress/route-audits/records/{record_id}",
        read_ingress_route_audit_record,
        methods=["GET"],
        response_model=IngressRouteAuditRecordResponse,
        operation_id="read_ingress_route_audit_record",
        summary="Read one ingress route audit record",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
