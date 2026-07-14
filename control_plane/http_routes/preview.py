from collections.abc import Callable
from typing import Annotated, Literal, Protocol, cast

from fastapi import Depends, Path, Query
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.every_code_preview_gate_record import EveryCodePreviewGateRecord
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_pr_feedback_notifications import (
    PreviewPrFeedbackNotificationAttemptRecord,
)
from control_plane.contracts.preview_readiness_read_model import (
    PreviewReadinessReadModel,
    build_preview_readiness_read_model,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.http_routes.every_code_support import (
    ensure_every_code_read_allowed,
    every_code_optional_int,
    every_code_pagination_value,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
)
from control_plane.service_auth import LaunchplaneIdentity
from control_plane.workflows.ship import utc_now_timestamp


class PreviewReadinessResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    readiness: PreviewReadinessReadModel


class PreviewRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: PreviewRecord


class PreviewHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    preview: PreviewRecord
    generations: tuple[PreviewGenerationRecord, ...]


class PreviewPrFeedbackNotificationAttemptRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    feedback_id: str
    event_filter: str
    destination_kind_filter: str
    attempts: tuple[PreviewPrFeedbackNotificationAttemptRecord, ...]


class PreviewReadStore(Protocol):
    def read_preview_record(self, preview_id: str) -> PreviewRecord: ...


class PreviewHistoryReadStore(Protocol):
    def read_preview_record(self, preview_id: str) -> PreviewRecord: ...

    def list_preview_generation_records(
        self,
        *,
        preview_id: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewGenerationRecord, ...]: ...


class PreviewReadinessStore(Protocol):
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


class PreviewPrFeedbackNotificationAttemptReadStore(Protocol):
    def list_preview_pr_feedback_notification_attempt_records(
        self,
        *,
        feedback_id: str = "",
        event: str = "",
        destination_kind: str = "",
        limit: int | None = None,
    ) -> tuple[PreviewPrFeedbackNotificationAttemptRecord, ...]: ...


def require_preview_read_store(record_store: object) -> PreviewReadStore:
    read_record = getattr(record_store, "read_preview_record", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support preview reads: read_preview_record"
        )
    return cast(PreviewReadStore, record_store)


def require_preview_history_read_store(record_store: object) -> PreviewHistoryReadStore:
    required_methods = ("read_preview_record", "list_preview_generation_records")
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support preview history reads: {missing_summary}"
        )
    return cast(PreviewHistoryReadStore, record_store)


def require_preview_readiness_store(record_store: object) -> PreviewReadinessStore:
    list_records = getattr(record_store, "list_every_code_preview_gate_records", None)
    if not callable(list_records):
        raise TypeError(
            "record store does not support Every Code preview gate reads: "
            "list_every_code_preview_gate_records"
        )
    return cast(PreviewReadinessStore, record_store)


def require_preview_pr_feedback_notification_attempt_read_store(
    record_store: object,
) -> PreviewPrFeedbackNotificationAttemptReadStore:
    list_records = getattr(
        record_store,
        "list_preview_pr_feedback_notification_attempt_records",
        None,
    )
    if not callable(list_records):
        raise TypeError(
            "record store does not support preview PR feedback notification attempt reads"
        )
    return cast(PreviewPrFeedbackNotificationAttemptReadStore, record_store)


def register_preview_readiness_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    read_identity: Callable[..., LaunchplaneIdentity | None],
) -> None:
    def read_preview_readiness(
        identity: Annotated[LaunchplaneIdentity | None, Depends(read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        repository: Annotated[str, Query()] = "",
        pr_number: Annotated[str, Query()] = "",
        status: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
        offset: Annotated[str, Query()] = "0",
    ) -> PreviewReadinessResponse:
        trace_id = dependencies.next_trace_id()
        ensure_every_code_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
            action="every_code_preview_gate.read",
            message="Workflow cannot read Every Code preview readiness.",
        )
        try:
            readiness_store = require_preview_readiness_store(record_store)
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        try:
            readiness = build_preview_readiness_read_model(
                generated_at=utc_now_timestamp(),
                record_store=readiness_store,
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
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_payload",
                message=str(error),
            ) from error
        return PreviewReadinessResponse(trace_id=trace_id, readiness=readiness)

    app.add_api_route(
        "/v1/previews/readiness",
        read_preview_readiness,
        methods=["GET"],
        response_model=PreviewReadinessResponse,
        operation_id="read_preview_readiness",
        summary="Read preview readiness",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )


def register_preview_record_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def read_preview_record(
        preview_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> PreviewRecordResponse:
        trace_id = dependencies.next_trace_id()
        try:
            preview_store = require_preview_read_store(record_store)
            preview = preview_store.read_preview_record(preview_id)
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
            action="preview.read",
            product="launchplane",
            context=preview.context,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read previews for the requested context.",
            )
        return PreviewRecordResponse(trace_id=trace_id, record=preview)

    def read_preview_history(
        preview_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> PreviewHistoryResponse:
        trace_id = dependencies.next_trace_id()
        try:
            preview_store = require_preview_history_read_store(record_store)
            preview = preview_store.read_preview_record(preview_id)
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
            action="preview.read",
            product="launchplane",
            context=preview.context,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read previews for the requested context.",
            )
        try:
            generations = preview_store.list_preview_generation_records(
                preview_id=preview.preview_id
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
        return PreviewHistoryResponse(
            trace_id=trace_id,
            preview=preview,
            generations=generations,
        )

    app.add_api_route(
        "/v1/previews/{preview_id}",
        read_preview_record,
        methods=["GET"],
        response_model=PreviewRecordResponse,
        operation_id="read_preview_record",
        summary="Read one preview record",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        "/v1/previews/{preview_id}/history",
        read_preview_history,
        methods=["GET"],
        response_model=PreviewHistoryResponse,
        operation_id="read_preview_history",
        summary="Read one preview record with generation history",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )


def register_preview_notification_attempt_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def list_preview_pr_feedback_notification_attempts(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        feedback_id: Annotated[str, Query()] = "",
        event: Annotated[str, Query()] = "",
        destination_kind: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
    ) -> PreviewPrFeedbackNotificationAttemptRecordsResponse:
        trace_id = dependencies.next_trace_id()
        ensure_every_code_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
            action="preview_pr_feedback_notification_attempt.read",
            message="Workflow cannot read preview PR feedback notification attempts.",
        )
        try:
            notification_store = require_preview_pr_feedback_notification_attempt_read_store(
                record_store
            )
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        records = notification_store.list_preview_pr_feedback_notification_attempt_records(
            feedback_id=feedback_id.strip(),
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
        return PreviewPrFeedbackNotificationAttemptRecordsResponse(
            trace_id=trace_id,
            feedback_id=feedback_id.strip(),
            event_filter=event.strip(),
            destination_kind_filter=destination_kind.strip(),
            attempts=records,
        )

    app.add_api_route(
        "/v1/previews/pr-feedback/notification-attempts",
        list_preview_pr_feedback_notification_attempts,
        methods=["GET"],
        response_model=PreviewPrFeedbackNotificationAttemptRecordsResponse,
        operation_id="list_preview_pr_feedback_notification_attempts",
        summary="List preview PR feedback notification attempts",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
