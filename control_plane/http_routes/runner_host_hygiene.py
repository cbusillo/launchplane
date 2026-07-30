from datetime import datetime, timezone
from typing import Annotated, Literal, Protocol, cast

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict

from control_plane import service_status as control_plane_service_status
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAction
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditRecord
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneApplyAuditStatus
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneCacheClassTelemetry
from control_plane.contracts.runner_host_hygiene import RunnerHostHygieneReport
from control_plane.contracts.runner_host_hygiene_idle import (
    RunnerHostHygieneIdleConvergence,
)
from control_plane.http_routes.support import (
    LAUNCHPLANE_SERVICE_CONTEXT,
    ApiRouteRegistrar,
    ReadRouteDependencies,
)
from control_plane.service_auth import LaunchplaneIdentity


RUNNER_HOST_HYGIENE_AUDIT_READ_ROUTE = "/v1/evidence/runner-host-hygiene/audits"
RUNNER_HOST_HYGIENE_HISTORY_READ_ROUTE = "/v1/evidence/runner-host-hygiene/history"
RUNNER_HOST_HYGIENE_AUDIT_RECORD_READ_ROUTE = "/v1/evidence/runner-host-hygiene/audits/record"


class RunnerHostHygieneReportReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["healthy", "attention"]
    host_name: str
    observed_at: str | None
    chronology_state: Literal["timestamped", "legacy_missing", "invalid"]
    free_disk_bytes: int
    docker_reclaimable_bytes: int
    runner_workdir_apparent_bytes: int
    runner_workdir_allocated_bytes: int
    generated_cache_apparent_bytes: int
    generated_cache_allocated_bytes: int
    cache_class_telemetry: tuple[RunnerHostHygieneCacheClassTelemetry, ...]
    idle_convergences: tuple[RunnerHostHygieneIdleConvergence, ...]
    image_inventory_total_count: int
    image_inventory_truncated: bool
    volume_inventory_total_count: int
    volume_inventory_truncated: bool
    finding_codes: tuple[str, ...]
    summary: str


class RunnerHostHygieneAuditSummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_record_key: str
    audit_status: RunnerHostHygieneApplyAuditStatus
    action: RunnerHostHygieneApplyAction
    host_name: str
    mutate: bool
    pre_apply_observed_at: str | None
    post_apply_observed_at: str | None
    pre_apply_status: Literal["healthy", "attention"]
    post_apply_status: Literal["healthy", "attention"] | None


class RunnerHostHygieneAuditReadRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: RunnerHostHygieneAuditSummary
    pre_apply_report: RunnerHostHygieneReportReadModel
    post_apply_report: RunnerHostHygieneReportReadModel | None


class RunnerHostHygieneHistoryPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    audit_record_key: str
    phase: Literal["pre_apply", "post_apply"]
    audit_status: RunnerHostHygieneApplyAuditStatus
    action: RunnerHostHygieneApplyAction
    mutate: bool
    report: RunnerHostHygieneReportReadModel


class RunnerHostHygieneAuditRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: RunnerHostHygieneAuditReadRecord


class RunnerHostHygieneAuditRecordsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    host_name: str
    action: RunnerHostHygieneApplyAction | None
    audit_status: RunnerHostHygieneApplyAuditStatus | None
    limit: int
    count: int
    truncated: bool
    records: tuple[RunnerHostHygieneAuditSummary, ...]


class RunnerHostHygieneHistoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    host_name: str
    cache_key: str
    limit: int
    scan_limit: int
    count: int
    scan_truncated: bool
    result_truncated: bool
    truncated: bool
    points: tuple[RunnerHostHygieneHistoryPoint, ...]


class RunnerHostHygieneAuditReadStore(Protocol):
    def read_runner_host_hygiene_audit_record(
        self, audit_record_key: str
    ) -> RunnerHostHygieneApplyAuditRecord: ...

    def list_runner_host_hygiene_audit_records(
        self,
        *,
        host_name: str = "",
        action: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[RunnerHostHygieneApplyAuditRecord, ...]: ...


def require_runner_host_hygiene_audit_read_store(
    record_store: object,
) -> RunnerHostHygieneAuditReadStore:
    required_methods = (
        "read_runner_host_hygiene_audit_record",
        "list_runner_host_hygiene_audit_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            "Launchplane record store does not support runner host hygiene audit reads: "
            f"{missing_summary}"
        )
    return cast(RunnerHostHygieneAuditReadStore, record_store)


def _ensure_runner_host_hygiene_audit_read_allowed(
    *,
    dependencies: ReadRouteDependencies,
    identity: LaunchplaneIdentity,
    trace_id: str,
) -> None:
    if not dependencies.authorization_allows(
        identity=identity,
        action="runner_host_hygiene_audit.read",
        product="launchplane",
        context=LAUNCHPLANE_SERVICE_CONTEXT,
    ):
        raise dependencies.http_error(
            status_code=403,
            trace_id=trace_id,
            code="authorization_denied",
            message="Workflow cannot read runner host hygiene audit evidence.",
        )


def _report_read_model(report: RunnerHostHygieneReport) -> RunnerHostHygieneReportReadModel:
    observed_at, chronology_state = _read_observed_at(report.observed_at)
    return RunnerHostHygieneReportReadModel(
        status=report.status,
        host_name=report.host_name,
        observed_at=observed_at,
        chronology_state=chronology_state,
        free_disk_bytes=report.free_disk_bytes,
        docker_reclaimable_bytes=report.docker_reclaimable_bytes,
        runner_workdir_apparent_bytes=report.runner_workdir_bytes,
        runner_workdir_allocated_bytes=report.runner_workdir_allocated_bytes,
        generated_cache_apparent_bytes=report.generated_cache_apparent_bytes,
        generated_cache_allocated_bytes=report.generated_cache_allocated_bytes,
        cache_class_telemetry=report.cache_class_telemetry,
        idle_convergences=report.idle_convergences,
        image_inventory_total_count=report.image_inventory_total_count,
        image_inventory_truncated=report.image_inventory_truncated,
        volume_inventory_total_count=report.volume_inventory_total_count,
        volume_inventory_truncated=report.volume_inventory_truncated,
        finding_codes=tuple(finding.code for finding in report.findings),
        summary=report.summary,
    )


def _read_observed_at(
    value: str,
) -> tuple[str | None, Literal["timestamped", "legacy_missing", "invalid"]]:
    normalized_value = value.strip()
    if not normalized_value:
        return None, "legacy_missing"
    try:
        parsed = datetime.fromisoformat(normalized_value.replace("Z", "+00:00"))
    except ValueError:
        return None, "invalid"
    if parsed.tzinfo is None:
        return None, "invalid"
    normalized_timestamp = (
        parsed.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    )
    return normalized_timestamp, "timestamped"


def _audit_summary(record: RunnerHostHygieneApplyAuditRecord) -> RunnerHostHygieneAuditSummary:
    pre_apply_observed_at = _read_observed_at(record.pre_apply_report.observed_at)[0]
    post_apply_observed_at = (
        _read_observed_at(record.post_apply_report.observed_at)[0]
        if record.post_apply_report is not None
        else None
    )
    return RunnerHostHygieneAuditSummary(
        audit_record_key=record.audit_record_key,
        audit_status=record.status,
        action=record.request.action,
        host_name=record.request.host_name,
        mutate=record.request.mutate,
        pre_apply_observed_at=pre_apply_observed_at,
        post_apply_observed_at=post_apply_observed_at,
        pre_apply_status=record.pre_apply_report.status,
        post_apply_status=(record.post_apply_report.status if record.post_apply_report else None),
    )


def _audit_read_record(
    record: RunnerHostHygieneApplyAuditRecord,
) -> RunnerHostHygieneAuditReadRecord:
    return RunnerHostHygieneAuditReadRecord(
        summary=_audit_summary(record),
        pre_apply_report=_report_read_model(record.pre_apply_report),
        post_apply_report=(
            _report_read_model(record.post_apply_report) if record.post_apply_report else None
        ),
    )


def _history_points(
    records: tuple[RunnerHostHygieneApplyAuditRecord, ...],
    *,
    cache_key: str,
) -> tuple[RunnerHostHygieneHistoryPoint, ...]:
    points: list[RunnerHostHygieneHistoryPoint] = []
    for record in records:
        reports: tuple[tuple[Literal["pre_apply", "post_apply"], RunnerHostHygieneReport], ...] = (
            ("pre_apply", record.pre_apply_report),
        )
        if record.post_apply_report is not None:
            reports += (("post_apply", record.post_apply_report),)
        for phase, report in reports:
            read_report = _report_read_model(report)
            if cache_key:
                matching_telemetry = tuple(
                    item
                    for item in read_report.cache_class_telemetry
                    if item.cache_key == cache_key
                )
                if not matching_telemetry:
                    continue
                read_report = read_report.model_copy(
                    update={"cache_class_telemetry": matching_telemetry}
                )
            points.append(
                RunnerHostHygieneHistoryPoint(
                    audit_record_key=record.audit_record_key,
                    phase=phase,
                    audit_status=record.status,
                    action=record.request.action,
                    mutate=record.request.mutate,
                    report=read_report,
                )
            )
    return tuple(
        sorted(
            points,
            key=lambda point: (
                point.report.chronology_state == "timestamped",
                _history_timestamp(point.report.observed_at),
                point.audit_record_key,
                point.phase == "post_apply",
            ),
            reverse=True,
        )
    )


def _history_timestamp(value: str | None) -> float:
    if value is None:
        return 0.0
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def register_runner_host_hygiene_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def list_runner_host_hygiene_audit_records(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        host_name: Annotated[str, Query()] = "",
        action: Annotated[RunnerHostHygieneApplyAction | None, Query()] = None,
        audit_status: Annotated[RunnerHostHygieneApplyAuditStatus | None, Query()] = None,
        limit: Annotated[str, Query()] = "25",
    ) -> RunnerHostHygieneAuditRecordsResponse:
        trace_id = dependencies.next_trace_id()
        _ensure_runner_host_hygiene_audit_read_allowed(
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
            audit_store = require_runner_host_hygiene_audit_read_store(record_store)
            records = audit_store.list_runner_host_hygiene_audit_records(
                host_name=host_name.strip().lower(),
                action=action or "",
                status=audit_status or "",
                limit=normalized_limit + 1,
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
        limited_records = records[:normalized_limit]
        return RunnerHostHygieneAuditRecordsResponse(
            trace_id=trace_id,
            host_name=host_name.strip().lower(),
            action=action,
            audit_status=audit_status,
            limit=normalized_limit,
            count=len(limited_records),
            truncated=len(records) > normalized_limit,
            records=tuple(_audit_summary(record) for record in limited_records),
        )

    def read_runner_host_hygiene_history(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        host_name: Annotated[str, Query(min_length=1)],
        cache_key: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "25",
    ) -> RunnerHostHygieneHistoryResponse:
        trace_id = dependencies.next_trace_id()
        _ensure_runner_host_hygiene_audit_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
        )
        normalized_host_name = host_name.strip().lower()
        normalized_cache_key = cache_key.strip().lower()
        try:
            if not normalized_host_name:
                raise ValueError("host_name must not be blank")
            normalized_limit = control_plane_service_status.query_int_value(
                limit,
                "limit",
                default=25,
                minimum=1,
                maximum=100,
            )
            assert normalized_limit is not None
            audit_store = require_runner_host_hygiene_audit_read_store(record_store)
            candidate_records = audit_store.list_runner_host_hygiene_audit_records(
                host_name=normalized_host_name,
                limit=101,
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
        scan_truncated = len(candidate_records) > 100
        points = _history_points(
            candidate_records[:100],
            cache_key=normalized_cache_key,
        )
        limited_points = points[:normalized_limit]
        result_truncated = len(points) > normalized_limit
        return RunnerHostHygieneHistoryResponse(
            trace_id=trace_id,
            host_name=normalized_host_name,
            cache_key=normalized_cache_key,
            limit=normalized_limit,
            scan_limit=100,
            count=len(limited_points),
            scan_truncated=scan_truncated,
            result_truncated=result_truncated,
            truncated=scan_truncated or result_truncated,
            points=limited_points,
        )

    def read_runner_host_hygiene_audit_record(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        audit_record_key: Annotated[str, Query(min_length=1)],
    ) -> RunnerHostHygieneAuditRecordResponse:
        trace_id = dependencies.next_trace_id()
        _ensure_runner_host_hygiene_audit_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
        )
        normalized_record_key = audit_record_key.strip()
        try:
            audit_store = require_runner_host_hygiene_audit_read_store(record_store)
            record = audit_store.read_runner_host_hygiene_audit_record(normalized_record_key)
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
        return RunnerHostHygieneAuditRecordResponse(
            trace_id=trace_id,
            record=_audit_read_record(record),
        )

    app.add_api_route(
        RUNNER_HOST_HYGIENE_AUDIT_READ_ROUTE,
        list_runner_host_hygiene_audit_records,
        methods=["GET"],
        response_model=RunnerHostHygieneAuditRecordsResponse,
        operation_id="list_runner_host_hygiene_audit_records",
        summary="List runner host hygiene audit records",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
    app.add_api_route(
        RUNNER_HOST_HYGIENE_HISTORY_READ_ROUTE,
        read_runner_host_hygiene_history,
        methods=["GET"],
        response_model=RunnerHostHygieneHistoryResponse,
        operation_id="read_runner_host_hygiene_history",
        summary="Read runner host hygiene observation history",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
    app.add_api_route(
        RUNNER_HOST_HYGIENE_AUDIT_RECORD_READ_ROUTE,
        read_runner_host_hygiene_audit_record,
        methods=["GET"],
        response_model=RunnerHostHygieneAuditRecordResponse,
        operation_id="read_runner_host_hygiene_audit_record",
        summary="Read one runner host hygiene audit record",
        responses={
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
