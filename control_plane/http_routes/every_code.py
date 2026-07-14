from collections.abc import Callable
from typing import Annotated, Literal, Protocol, cast

from fastapi import Depends, Path, Query
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.every_code_notifications import (
    EveryCodeNotificationAttemptRecord,
)
from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.every_code_pr_feedback_record import EveryCodePrFeedbackRecord
from control_plane.contracts.every_code_summary_read_model import (
    EveryCodeSummaryReadModel,
    build_every_code_summary_read_model,
)
from control_plane.contracts.every_code_work_request import EveryCodeWorkRequestRecord
from control_plane.http_routes.every_code_support import (
    ensure_every_code_read_allowed,
    every_code_optional_int,
    every_code_pagination_value,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import LaunchplaneIdentity
from control_plane.workflows.ship import utc_now_timestamp


class EveryCodeWorkRequestRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    state: str
    repository: str
    requests: tuple[EveryCodeWorkRequestRecord, ...]


class EveryCodeWorkRequestRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    request: EveryCodeWorkRequestRecord


class EveryCodeSummaryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    summary: EveryCodeSummaryReadModel


class EveryCodePrFeedbackRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    request_id: str
    repository: str
    status_filter: str
    feedback: tuple[EveryCodePrFeedbackRecord, ...]


class EveryCodePreviewGateRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    request_id: str
    repository: str
    status_filter: str
    gates: tuple[EveryCodePreviewGateRecord, ...]


class EveryCodeNotificationAttemptRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    request_id: str
    event_filter: str
    destination_kind_filter: str
    attempts: tuple[EveryCodeNotificationAttemptRecord, ...]


class EveryCodeWorkRequestListStore(Protocol):
    def list_every_code_work_request_records(
        self,
        *,
        state: str = "",
        repository: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodeWorkRequestRecord, ...]: ...


class EveryCodeWorkRequestRecordStore(Protocol):
    def read_every_code_work_request_record(
        self, request_id: str
    ) -> EveryCodeWorkRequestRecord: ...


class EveryCodePrFeedbackReadStore(Protocol):
    def list_every_code_pr_feedback_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePrFeedbackRecord, ...]: ...


class EveryCodePreviewGateReadStore(Protocol):
    def list_every_code_preview_gate_records(
        self,
        *,
        request_id: str = "",
        repository: str = "",
        pr_number: int | None = None,
        status: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EveryCodePreviewGateRecord, ...]: ...


class EveryCodeNotificationAttemptReadStore(Protocol):
    def list_every_code_notification_attempt_records(
        self,
        *,
        request_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[EveryCodeNotificationAttemptRecord, ...]: ...


def require_every_code_read_methods(
    record_store: object,
    *,
    required_methods: tuple[str, ...],
    capability: str,
) -> None:
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(f"record store does not support {capability}: {missing_summary}")


def require_every_code_work_request_list_store(
    record_store: object,
) -> EveryCodeWorkRequestListStore:
    require_every_code_read_methods(
        record_store,
        required_methods=("list_every_code_work_request_records",),
        capability="Every Code work request list reads",
    )
    return cast(EveryCodeWorkRequestListStore, record_store)


def require_every_code_work_request_record_store(
    record_store: object,
) -> EveryCodeWorkRequestRecordStore:
    require_every_code_read_methods(
        record_store,
        required_methods=("read_every_code_work_request_record",),
        capability="Every Code work request record reads",
    )
    return cast(EveryCodeWorkRequestRecordStore, record_store)


def require_every_code_pr_feedback_read_store(
    record_store: object,
) -> EveryCodePrFeedbackReadStore:
    require_every_code_read_methods(
        record_store,
        required_methods=("list_every_code_pr_feedback_records",),
        capability="Every Code PR feedback reads",
    )
    return cast(EveryCodePrFeedbackReadStore, record_store)


def require_every_code_preview_gate_read_store(
    record_store: object,
) -> EveryCodePreviewGateReadStore:
    require_every_code_read_methods(
        record_store,
        required_methods=("list_every_code_preview_gate_records",),
        capability="Every Code preview gate reads",
    )
    return cast(EveryCodePreviewGateReadStore, record_store)


def require_every_code_notification_attempt_read_store(
    record_store: object,
) -> EveryCodeNotificationAttemptReadStore:
    list_records = getattr(record_store, "list_every_code_notification_attempt_records", None)
    if not callable(list_records):
        raise TypeError("record store does not support Every Code notification attempt reads")
    return cast(EveryCodeNotificationAttemptReadStore, record_store)


def every_code_read_store_or_503(
    record_store: object,
    *,
    dependencies: ReadRouteDependencies,
    trace_id: str,
    capability: str,
) -> object:
    try:
        if capability == "work_request_list":
            return require_every_code_work_request_list_store(record_store)
        if capability == "work_request_record":
            return require_every_code_work_request_record_store(record_store)
        if capability == "pr_feedback":
            return require_every_code_pr_feedback_read_store(record_store)
        if capability == "preview_gate":
            return require_every_code_preview_gate_read_store(record_store)
        raise AssertionError(f"unknown Every Code read capability: {capability}")
    except TypeError as error:
        raise dependencies.http_error(
            status_code=503,
            trace_id=trace_id,
            code="database_storage_required",
            message=str(error),
        ) from error


def every_code_invalid_payload_error(
    *, dependencies: ReadRouteDependencies, trace_id: str, error: ValueError
) -> Exception:
    return dependencies.http_error(
        status_code=400,
        trace_id=trace_id,
        code="invalid_payload",
        message=str(error),
    )


def register_every_code_work_request_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    read_identity: Callable[..., LaunchplaneIdentity | None],
) -> None:
    def read_every_code_summary(
        identity: Annotated[LaunchplaneIdentity | None, Depends(read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        repository: Annotated[str, Query()] = "",
        issue_number: Annotated[str, Query()] = "",
        state: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
        offset: Annotated[str, Query()] = "0",
    ) -> EveryCodeSummaryResponse:
        trace_id = dependencies.next_trace_id()
        ensure_every_code_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
            action="every_code_work_request.read",
            message="Workflow cannot read Every Code work requests.",
        )
        every_code_store = cast(
            EveryCodeWorkRequestListStore,
            every_code_read_store_or_503(
                record_store,
                dependencies=dependencies,
                trace_id=trace_id,
                capability="work_request_list",
            ),
        )
        try:
            summary = build_every_code_summary_read_model(
                generated_at=utc_now_timestamp(),
                record_store=every_code_store,
                repository=repository.strip(),
                issue_number=every_code_optional_int(
                    issue_number,
                    "issue_number",
                    dependencies=dependencies,
                    trace_id=trace_id,
                ),
                state=state.strip(),
                limit=every_code_pagination_value(
                    limit,
                    "limit",
                    default=50,
                    dependencies=dependencies,
                    trace_id=trace_id,
                ),
                offset=every_code_pagination_value(
                    offset,
                    "offset",
                    default=0,
                    dependencies=dependencies,
                    trace_id=trace_id,
                ),
            )
        except ValueError as error:
            raise every_code_invalid_payload_error(
                dependencies=dependencies,
                trace_id=trace_id,
                error=error,
            ) from error
        return EveryCodeSummaryResponse(trace_id=trace_id, summary=summary)

    def list_every_code_work_requests(
        identity: Annotated[LaunchplaneIdentity | None, Depends(read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        state: Annotated[str, Query()] = "",
        repository: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
        offset: Annotated[str, Query()] = "0",
    ) -> EveryCodeWorkRequestRecordsResponse:
        trace_id = dependencies.next_trace_id()
        ensure_every_code_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
            action="every_code_work_request.read",
            message="Workflow cannot read Every Code work requests.",
        )
        every_code_store = cast(
            EveryCodeWorkRequestListStore,
            every_code_read_store_or_503(
                record_store,
                dependencies=dependencies,
                trace_id=trace_id,
                capability="work_request_list",
            ),
        )
        records = every_code_store.list_every_code_work_request_records(
            state=state.strip(),
            repository=repository.strip(),
            limit=every_code_pagination_value(
                limit,
                "limit",
                default=50,
                dependencies=dependencies,
                trace_id=trace_id,
            ),
            offset=every_code_pagination_value(
                offset,
                "offset",
                default=0,
                dependencies=dependencies,
                trace_id=trace_id,
            ),
        )
        return EveryCodeWorkRequestRecordsResponse(
            trace_id=trace_id,
            state=state.strip(),
            repository=repository.strip(),
            requests=records,
        )

    def read_every_code_work_request(
        request_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity | None, Depends(read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> EveryCodeWorkRequestRecordResponse:
        trace_id = dependencies.next_trace_id()
        ensure_every_code_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
            action="every_code_work_request.read",
            message="Workflow cannot read Every Code work requests.",
        )
        every_code_store = cast(
            EveryCodeWorkRequestRecordStore,
            every_code_read_store_or_503(
                record_store,
                dependencies=dependencies,
                trace_id=trace_id,
                capability="work_request_record",
            ),
        )
        try:
            record = every_code_store.read_every_code_work_request_record(request_id)
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return EveryCodeWorkRequestRecordResponse(trace_id=trace_id, request=record)

    responses = {
        400: {"model": dependencies.error_response_model},
        401: {"model": dependencies.error_response_model},
        403: {"model": dependencies.error_response_model},
        404: {"model": dependencies.error_response_model},
        503: {"model": dependencies.error_response_model},
    }
    app.add_api_route(
        "/v1/every-code/summary",
        read_every_code_summary,
        methods=["GET"],
        response_model=EveryCodeSummaryResponse,
        operation_id="read_every_code_summary",
        summary="Read Every Code work request summary",
        responses=responses,
    )
    app.add_api_route(
        "/v1/every-code/work-requests",
        list_every_code_work_requests,
        methods=["GET"],
        response_model=EveryCodeWorkRequestRecordsResponse,
        operation_id="list_every_code_work_requests",
        summary="List Every Code work requests",
        responses=responses,
    )
    app.add_api_route(
        "/v1/every-code/work-requests/{request_id}",
        read_every_code_work_request,
        methods=["GET"],
        response_model=EveryCodeWorkRequestRecordResponse,
        operation_id="read_every_code_work_request",
        summary="Read one Every Code work request",
        responses=responses,
    )


def register_every_code_feedback_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    read_identity: Callable[..., LaunchplaneIdentity | None],
) -> None:
    def list_every_code_pr_feedback(
        identity: Annotated[LaunchplaneIdentity | None, Depends(read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        request_id: Annotated[str, Query()] = "",
        repository: Annotated[str, Query()] = "",
        pr_number: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
        offset: Annotated[str, Query()] = "0",
    ) -> EveryCodePrFeedbackRecordsResponse:
        trace_id = dependencies.next_trace_id()
        ensure_every_code_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
            action="every_code_pr_feedback.read",
            message="Workflow cannot read Every Code PR feedback.",
        )
        every_code_store = cast(
            EveryCodePrFeedbackReadStore,
            every_code_read_store_or_503(
                record_store,
                dependencies=dependencies,
                trace_id=trace_id,
                capability="pr_feedback",
            ),
        )
        records = every_code_store.list_every_code_pr_feedback_records(
            request_id=request_id.strip(),
            repository=repository.strip(),
            pr_number=every_code_optional_int(
                pr_number,
                "pr_number",
                dependencies=dependencies,
                trace_id=trace_id,
            ),
            status=status.strip(),
            limit=every_code_pagination_value(
                limit,
                "limit",
                default=50,
                dependencies=dependencies,
                trace_id=trace_id,
            ),
            offset=every_code_pagination_value(
                offset,
                "offset",
                default=0,
                dependencies=dependencies,
                trace_id=trace_id,
            ),
        )
        return EveryCodePrFeedbackRecordsResponse(
            trace_id=trace_id,
            request_id=request_id.strip(),
            repository=repository.strip(),
            status_filter=status.strip(),
            feedback=records,
        )

    app.add_api_route(
        "/v1/every-code/pr-feedback",
        list_every_code_pr_feedback,
        methods=["GET"],
        response_model=EveryCodePrFeedbackRecordsResponse,
        operation_id="list_every_code_pr_feedback",
        summary="List Every Code PR feedback records",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )


def register_every_code_preview_gate_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    read_identity: Callable[..., LaunchplaneIdentity | None],
) -> None:
    def list_every_code_preview_gates(
        identity: Annotated[LaunchplaneIdentity | None, Depends(read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        request_id: Annotated[str, Query()] = "",
        repository: Annotated[str, Query()] = "",
        pr_number: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
        offset: Annotated[str, Query()] = "0",
    ) -> EveryCodePreviewGateRecordsResponse:
        trace_id = dependencies.next_trace_id()
        ensure_every_code_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
            action="every_code_preview_gate.read",
            message="Workflow cannot read Every Code preview gates.",
        )
        every_code_store = cast(
            EveryCodePreviewGateReadStore,
            every_code_read_store_or_503(
                record_store,
                dependencies=dependencies,
                trace_id=trace_id,
                capability="preview_gate",
            ),
        )
        records = every_code_store.list_every_code_preview_gate_records(
            request_id=request_id.strip(),
            repository=repository.strip(),
            pr_number=every_code_optional_int(
                pr_number,
                "pr_number",
                dependencies=dependencies,
                trace_id=trace_id,
            ),
            status=status.strip(),
            limit=every_code_pagination_value(
                limit,
                "limit",
                default=50,
                dependencies=dependencies,
                trace_id=trace_id,
            ),
            offset=every_code_pagination_value(
                offset,
                "offset",
                default=0,
                dependencies=dependencies,
                trace_id=trace_id,
            ),
        )
        return EveryCodePreviewGateRecordsResponse(
            trace_id=trace_id,
            request_id=request_id.strip(),
            repository=repository.strip(),
            status_filter=status.strip(),
            gates=records,
        )

    app.add_api_route(
        "/v1/every-code/preview-gates",
        list_every_code_preview_gates,
        methods=["GET"],
        response_model=EveryCodePreviewGateRecordsResponse,
        operation_id="list_every_code_preview_gates",
        summary="List Every Code preview gates",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )


def register_every_code_notification_attempt_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    read_identity: Callable[..., LaunchplaneIdentity | None],
) -> None:
    def list_every_code_notification_attempts(
        identity: Annotated[LaunchplaneIdentity | None, Depends(read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        request_id: Annotated[str, Query()] = "",
        event: Annotated[str, Query()] = "",
        destination_kind: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
    ) -> EveryCodeNotificationAttemptRecordsResponse:
        trace_id = dependencies.next_trace_id()
        ensure_every_code_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
            action="every_code_notification_attempt.read",
            message="Workflow cannot read Every Code notification attempts.",
        )
        try:
            notification_store = require_every_code_notification_attempt_read_store(record_store)
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        records = notification_store.list_every_code_notification_attempt_records(
            request_id=request_id.strip(),
            event=event.strip(),
            destination_kind=destination_kind.strip(),
            limit=every_code_pagination_value(
                limit,
                "limit",
                default=50,
                dependencies=dependencies,
                trace_id=trace_id,
            ),
        )
        return EveryCodeNotificationAttemptRecordsResponse(
            trace_id=trace_id,
            request_id=request_id.strip(),
            event_filter=event.strip(),
            destination_kind_filter=destination_kind.strip(),
            attempts=records,
        )

    responses = {
        400: {"model": dependencies.error_response_model},
        401: {"model": dependencies.error_response_model},
        403: {"model": dependencies.error_response_model},
        404: {"model": dependencies.error_response_model},
        503: {"model": dependencies.error_response_model},
    }
    app.add_api_route(
        "/v1/every-code/notification-attempts",
        list_every_code_notification_attempts,
        methods=["GET"],
        response_model=EveryCodeNotificationAttemptRecordsResponse,
        operation_id="list_every_code_notification_attempts",
        summary="List Every Code notification attempts",
        responses=responses,
    )
