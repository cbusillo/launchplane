from dataclasses import dataclass
from pathlib import Path as FilePath
from typing import Annotated, Literal, Protocol, cast

import click
from fastapi import Depends, Path, Query
from pydantic import BaseModel, ConfigDict

from control_plane import service_status as control_plane_service_status
from control_plane import tracked_target_logs as control_plane_tracked_target_logs
from control_plane.contracts.driver_descriptor import DriverContextView, DriverDescriptor
from control_plane.contracts.odoo_stable_bootstrap_operation import (
    OdooStableBootstrapOperationRecord,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.dokploy import api as dokploy_api
from control_plane.dokploy import source as dokploy_source
from control_plane.dokploy_target_inspect import (
    DokployTargetInspectRequest,
    DokployTargetInspectStore,
    inspect_dokploy_target,
)
from control_plane.drivers import native_routes
from control_plane.drivers.registry import (
    build_driver_context_view,
    list_driver_descriptors,
    read_driver_descriptor,
)
from control_plane.http_routes.support import (
    LAUNCHPLANE_SERVICE_CONTEXT,
    ApiRouteRegistrar,
    ReadRouteDependencies,
)
from control_plane.odoo_stable_bootstrap_http import ODOO_STABLE_BOOTSTRAP_ROUTE
from control_plane.odoo_target_replacement_apply_http import (
    ODOO_TARGET_REPLACEMENT_APPLY_ROUTE,
)
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore

_LAUNCHPLANE_DRIVER_READ_PRODUCT = "launchplane"
_LAUNCHPLANE_DRIVER_READ_CONTEXT = "launchplane"

ODOO_STABLE_BOOTSTRAP_OPERATION_STATUS_ROUTE = (
    "/v1/drivers/odoo/stable-bootstrap/operations/{operation_id}"
)
ODOO_STABLE_TARGET_REPLACEMENT_OPERATION_STATUS_ROUTE = (
    "/v1/drivers/odoo/target-replacement/operations/{operation_id}"
)


@dataclass(frozen=True, slots=True)
class DriverReadRouteDependencies:
    common: ReadRouteDependencies
    control_plane_root: FilePath
    database_url: str | None


class OdooStableBootstrapOperationStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    operation: dict[str, object]
    result: dict[str, object] | None = None


class OdooStableTargetReplacementOperationStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    operation: dict[str, object]
    result: dict[str, object] | None = None


class DriverDescriptorsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    drivers: tuple[DriverDescriptor, ...]


class DriverDescriptorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    driver: DriverDescriptor


class DriverContextViewResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    view: DriverContextView


class DokployTargetInspectResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    inspect: dict[str, object]


class TrackedTargetLogTargetResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target_id: str
    target_type: str
    target_name: str
    app_name: str
    server_id: str
    source_label: str


class TrackedTargetLogRequestResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: Literal["runtime", "deployment"]
    line_count: int
    since: str
    search: str
    service: str


class TrackedTargetLogLinesResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    line_count: int
    lines: tuple[str, ...]
    redacted: bool


class TrackedTargetLogDeploymentResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    deployment_id: str
    status: str
    error_message: str
    created_at: str
    started_at: str
    finished_at: str
    log_path_present: bool


class TrackedTargetLogsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    context: str
    instance: str
    target: TrackedTargetLogTargetResponse
    request: TrackedTargetLogRequestResponse
    logs: TrackedTargetLogLinesResponse
    deployment: TrackedTargetLogDeploymentResponse | None = None


class OdooStableBootstrapOperationReadStore(Protocol):
    def read_odoo_stable_bootstrap_operation_record(
        self, operation_id: str
    ) -> OdooStableBootstrapOperationRecord: ...


class OdooStableTargetReplacementOperationReadStore(Protocol):
    def read_odoo_stable_target_replacement_operation_record(
        self, operation_id: str
    ) -> OdooStableTargetReplacementOperationRecord: ...


def require_odoo_stable_bootstrap_operation_read_store(
    record_store: object,
) -> OdooStableBootstrapOperationReadStore:
    read_record = getattr(record_store, "read_odoo_stable_bootstrap_operation_record", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support Odoo stable bootstrap operation "
            "status reads: read_odoo_stable_bootstrap_operation_record"
        )
    return cast(OdooStableBootstrapOperationReadStore, record_store)


def require_odoo_stable_target_replacement_operation_read_store(
    record_store: object,
) -> OdooStableTargetReplacementOperationReadStore:
    read_record = getattr(
        record_store,
        "read_odoo_stable_target_replacement_operation_record",
        None,
    )
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support Odoo target replacement operation "
            "status reads: read_odoo_stable_target_replacement_operation_record"
        )
    return cast(OdooStableTargetReplacementOperationReadStore, record_store)


def require_tracked_target_logs_store(
    record_store: object,
) -> control_plane_tracked_target_logs.TrackedTargetLogsStore:
    required_methods = (
        "read_dokploy_target_record",
        "read_dokploy_target_id_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods or not isinstance(record_store, PostgresRecordStore):
        missing_summary = ", ".join(missing_methods) or "postgres_storage"
        raise TypeError(
            f"Tracked target logs require DB-backed Launchplane storage: {missing_summary}"
        )
    return cast(control_plane_tracked_target_logs.TrackedTargetLogsStore, record_store)


def require_dokploy_target_inspect_store(record_store: object) -> DokployTargetInspectStore:
    required_methods = (
        "read_dokploy_target_record",
        "read_dokploy_target_id_record",
        "read_provider_target_record",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods or not isinstance(record_store, PostgresRecordStore):
        raise TypeError("Dokploy target inspect requires Launchplane database storage.")
    return cast(DokployTargetInspectStore, record_store)


def odoo_stable_bootstrap_operation_status_payload(
    operation: OdooStableBootstrapOperationRecord,
) -> dict[str, object]:
    payload = operation.model_dump(mode="json")
    payload["poll_url"] = ODOO_STABLE_BOOTSTRAP_OPERATION_STATUS_ROUTE.format(
        operation_id=operation.operation_id.strip()
    )
    return payload


def odoo_stable_target_replacement_operation_status_payload(
    operation: OdooStableTargetReplacementOperationRecord,
) -> dict[str, object]:
    payload = operation.model_dump(mode="json")
    payload["poll_url"] = ODOO_STABLE_TARGET_REPLACEMENT_OPERATION_STATUS_ROUTE.format(
        operation_id=operation.operation_id.strip()
    )
    return payload


def _ensure_driver_read_allowed(
    *,
    dependencies: ReadRouteDependencies,
    identity: LaunchplaneIdentity,
    trace_id: str,
    context: str = _LAUNCHPLANE_DRIVER_READ_CONTEXT,
    instance: str = "",
) -> None:
    if not dependencies.authorization_allows(
        identity=identity,
        action="driver.read",
        product=_LAUNCHPLANE_DRIVER_READ_PRODUCT,
        context=context,
        target=AuthorizationTarget(
            scope="instance" if instance else "context",
            instances=(instance,) if instance else (),
        ),
    ):
        raise dependencies.http_error(
            status_code=403,
            trace_id=trace_id,
            code="authorization_denied",
            message="Workflow cannot read driver metadata for the requested context.",
        )


def register_operation_status_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def read_odoo_stable_bootstrap_operation_status(
        operation_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> OdooStableBootstrapOperationStatusResponse:
        trace_id = dependencies.next_trace_id()
        try:
            operation_store = require_odoo_stable_bootstrap_operation_read_store(record_store)
            operation = operation_store.read_odoo_stable_bootstrap_operation_record(operation_id)
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
        if not dependencies.authorization_allows(
            identity=identity,
            action=native_routes._descriptor_driver_route_authz_action(ODOO_STABLE_BOOTSTRAP_ROUTE),
            product=operation.product,
            context=operation.context,
            target=AuthorizationTarget(scope="instance", instances=(operation.instance,)),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot read Odoo stable bootstrap operation status "
                    "for the requested product/context."
                ),
            )
        result = operation.result.model_dump(mode="json") if operation.result else None
        return OdooStableBootstrapOperationStatusResponse(
            trace_id=trace_id,
            operation=odoo_stable_bootstrap_operation_status_payload(operation),
            result=result,
        )

    def read_odoo_stable_target_replacement_operation_status(
        operation_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> OdooStableTargetReplacementOperationStatusResponse:
        trace_id = dependencies.next_trace_id()
        try:
            operation_store = require_odoo_stable_target_replacement_operation_read_store(
                record_store
            )
            operation = operation_store.read_odoo_stable_target_replacement_operation_record(
                operation_id
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
        if not dependencies.authorization_allows(
            identity=identity,
            action=native_routes._descriptor_driver_route_authz_action(
                ODOO_TARGET_REPLACEMENT_APPLY_ROUTE
            ),
            product=operation.product,
            context=operation.context,
            target=AuthorizationTarget(scope="instance", instances=(operation.instance,)),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot read Odoo target replacement operation status "
                    "for the requested product/context."
                ),
            )
        result = operation.result.model_dump(mode="json") if operation.result else None
        return OdooStableTargetReplacementOperationStatusResponse(
            trace_id=trace_id,
            operation=odoo_stable_target_replacement_operation_status_payload(operation),
            result=result,
        )

    app.add_api_route(
        ODOO_STABLE_BOOTSTRAP_OPERATION_STATUS_ROUTE,
        read_odoo_stable_bootstrap_operation_status,
        methods=["GET"],
        response_model=OdooStableBootstrapOperationStatusResponse,
        response_model_exclude_none=True,
        operation_id="read_odoo_stable_bootstrap_operation_status",
        summary="Read Odoo stable bootstrap operation status",
        responses={
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        ODOO_STABLE_TARGET_REPLACEMENT_OPERATION_STATUS_ROUTE,
        read_odoo_stable_target_replacement_operation_status,
        methods=["GET"],
        response_model=OdooStableTargetReplacementOperationStatusResponse,
        response_model_exclude_none=True,
        operation_id="read_odoo_target_replacement_operation_status",
        summary="Read Odoo target replacement operation status",
        responses={
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )


def register_driver_descriptor_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def read_driver_descriptors(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
    ) -> DriverDescriptorsResponse:
        trace_id = dependencies.next_trace_id()
        _ensure_driver_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
        )
        return DriverDescriptorsResponse(
            trace_id=trace_id,
            drivers=list_driver_descriptors(),
        )

    def read_driver_descriptor_route(
        driver_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
    ) -> DriverDescriptorResponse:
        trace_id = dependencies.next_trace_id()
        _ensure_driver_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
        )
        try:
            descriptor = read_driver_descriptor(driver_id)
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return DriverDescriptorResponse(trace_id=trace_id, driver=descriptor)

    read_driver_descriptor_route.__name__ = "read_driver_descriptor"

    def read_driver_context_view(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> DriverContextViewResponse:
        trace_id = dependencies.next_trace_id()
        _ensure_driver_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
            context=context,
        )
        view = build_driver_context_view(
            record_store=record_store,
            context_name=context,
        )
        return DriverContextViewResponse(trace_id=trace_id, view=view)

    def read_driver_instance_view(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        instance: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> DriverContextViewResponse:
        trace_id = dependencies.next_trace_id()
        _ensure_driver_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
            context=context,
            instance=instance,
        )
        view = build_driver_context_view(
            record_store=record_store,
            context_name=context,
            instance_name=instance,
        )
        return DriverContextViewResponse(trace_id=trace_id, view=view)

    app.add_api_route(
        "/v1/drivers",
        read_driver_descriptors,
        methods=["GET"],
        response_model=DriverDescriptorsResponse,
        operation_id="read_driver_descriptors",
        summary="Read Launchplane driver descriptors",
        responses={
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        "/v1/drivers/{driver_id}",
        read_driver_descriptor_route,
        methods=["GET"],
        response_model=DriverDescriptorResponse,
        operation_id="read_driver_descriptor",
        summary="Read one Launchplane driver descriptor",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/driver-view",
        read_driver_context_view,
        methods=["GET"],
        response_model=DriverContextViewResponse,
        operation_id="read_driver_context_view",
        summary="Read Launchplane driver view for a context",
        responses={
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        "/v1/contexts/{context}/instances/{instance}/driver-view",
        read_driver_instance_view,
        methods=["GET"],
        response_model=DriverContextViewResponse,
        operation_id="read_driver_instance_view",
        summary="Read Launchplane driver view for one context instance",
        responses={
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
        },
    )


def register_dokploy_target_inspect_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: DriverReadRouteDependencies,
) -> None:
    common = dependencies.common

    def read_dokploy_target_inspect(
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        context: Annotated[str, Query()] = "",
        instance: Annotated[str, Query()] = "",
        target_type: Annotated[str, Query()] = "",
        target_id: Annotated[str, Query()] = "",
    ) -> DokployTargetInspectResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action="dokploy_target.inspect",
            product="launchplane",
            context=LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot inspect Launchplane Dokploy targets.",
            )
        try:
            inspect_store = require_dokploy_target_inspect_store(record_store)
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message=str(error),
            ) from error
        try:
            inspect_request = DokployTargetInspectRequest.model_validate(
                {
                    "schema_version": 1,
                    "product": "launchplane",
                    "context": context,
                    "instance": instance,
                    "target_type": target_type,
                    "target_id": target_id,
                }
            )
            host, token = dokploy_source.read_dokploy_config(
                control_plane_root=dependencies.control_plane_root,
                database_url=dependencies.database_url,
            )
            inspect_result = inspect_dokploy_target(
                record_store=inspect_store,
                host=host,
                token=token,
                request=inspect_request,
            )
        except ValueError as error:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_dokploy_target_inspect",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise common.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return DokployTargetInspectResponse(trace_id=trace_id, inspect=inspect_result)

    app.add_api_route(
        "/v1/dokploy-targets/inspect",
        read_dokploy_target_inspect,
        methods=["GET"],
        response_model=DokployTargetInspectResponse,
        operation_id="read_dokploy_target_inspect",
        summary="Read redacted Dokploy target identity",
        responses={
            400: {"model": common.error_response_model},
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            404: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )


def register_tracked_target_log_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: DriverReadRouteDependencies,
) -> None:
    common = dependencies.common

    def read_tracked_target_logs(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        instance: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        lines: Annotated[str, Query()] = str(dokploy_api.DEFAULT_DOKPLOY_LOG_LINE_COUNT),
        since: Annotated[str, Query()] = "all",
        search: Annotated[str, Query()] = "",
        service: Annotated[str, Query()] = "",
        source: Annotated[str, Query()] = "runtime",
    ) -> TrackedTargetLogsResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action="target_logs.read",
            product="launchplane",
            context=context,
            target=AuthorizationTarget(scope="instance", instances=(instance,)),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read tracked target logs for the requested context.",
            )
        try:
            line_count = control_plane_service_status.query_int_value(
                lines,
                "lines",
                default=dokploy_api.DEFAULT_DOKPLOY_LOG_LINE_COUNT,
                minimum=1,
                maximum=dokploy_api.MAX_DOKPLOY_LOG_LINE_COUNT,
            )
            assert line_count is not None
        except ValueError as error:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        try:
            normalized_since = dokploy_api.normalize_dokploy_log_since(since)
            normalized_search = dokploy_api.normalize_dokploy_log_search(search)
            normalized_service = dokploy_api.normalize_dokploy_compose_service_name(service)
            normalized_source = (
                control_plane_tracked_target_logs.normalize_tracked_target_log_source(source)
            )
            if normalized_source == "deployment" and normalized_since != "all":
                raise ValueError("Tracked deployment logs require since='all'.")
            if normalized_source == "deployment" and normalized_search:
                raise ValueError("Tracked deployment logs do not support search.")
            if normalized_source == "deployment" and normalized_service:
                raise ValueError(
                    "Tracked deployment logs do not support compose service selection."
                )
        except (ValueError, click.ClickException) as error:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_query",
                message=str(error),
            ) from error
        try:
            target_logs_store = require_tracked_target_logs_store(record_store)
            logs_payload = control_plane_tracked_target_logs.build_tracked_target_logs_payload(
                record_store=target_logs_store,
                control_plane_root=dependencies.control_plane_root,
                context_name=context,
                instance_name=instance,
                line_count=line_count,
                since=normalized_since,
                search=normalized_search,
                service=normalized_service,
                source=normalized_source,
            )
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_required",
                message=str(error),
            ) from error
        except ValueError as error:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error
        except control_plane_tracked_target_logs.TrackedTargetLogsProviderError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="target_logs_unavailable",
                message=(
                    f"Tracked target logs are unavailable during {error.operation}: {error.detail}"
                ),
            ) from error
        except click.ClickException as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="target_logs_unavailable",
                message="Tracked target logs are unavailable from the provider.",
            ) from error
        return TrackedTargetLogsResponse.model_validate(
            {"status": "ok", "trace_id": trace_id, **logs_payload}
        )

    app.add_api_route(
        "/v1/contexts/{context}/instances/{instance}/logs",
        read_tracked_target_logs,
        methods=["GET"],
        response_model=TrackedTargetLogsResponse,
        operation_id="read_tracked_target_logs",
        summary="Read tracked target logs",
        responses={
            400: {"model": common.error_response_model},
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )
