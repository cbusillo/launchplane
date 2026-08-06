from __future__ import annotations

from collections.abc import Callable
from typing import Annotated, Literal, Protocol, cast

from fastapi import Depends, Path, Query
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.engineering_review_run import (
    EngineeringReviewRunRecord,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import LaunchplaneIdentity


class EngineeringReviewRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    run: EngineeringReviewRunRecord


class EngineeringReviewRunsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    repository: str
    state_filter: str
    runs: tuple[EngineeringReviewRunRecord, ...]


class EngineeringReviewRunReadStore(Protocol):
    def read_engineering_review_run_record(self, run_id: str) -> EngineeringReviewRunRecord: ...

    def list_engineering_review_run_records(
        self,
        *,
        repository: str = "",
        pr_number: int | None = None,
        work_request_id: str = "",
        state: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EngineeringReviewRunRecord, ...]: ...


def _require_engineering_review_read_store(record_store: object) -> EngineeringReviewRunReadStore:
    missing = [
        m
        for m in ("read_engineering_review_run_record", "list_engineering_review_run_records")
        if not callable(getattr(record_store, m, None))
    ]
    if missing:
        raise TypeError(
            f"record store does not support engineering review run reads: {', '.join(missing)}"
        )
    return cast(EngineeringReviewRunReadStore, record_store)


def _ensure_engineering_review_read_allowed(
    *,
    dependencies: ReadRouteDependencies,
    identity: LaunchplaneIdentity | None,
    trace_id: str,
) -> None:
    if identity is None:
        return
    if not dependencies.authorization_allows(
        identity=identity,
        action="engineering_review_run.read",
        product="launchplane",
        context="launchplane",
    ):
        raise dependencies.http_error(
            status_code=403,
            trace_id=trace_id,
            code="forbidden",
            message="Identity cannot read engineering review runs.",
        )


def register_engineering_review_run_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    read_identity: Callable[..., LaunchplaneIdentity | None],
) -> None:
    def read_engineering_review_run(
        run_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity | None, Depends(read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> EngineeringReviewRunResponse:
        trace_id = dependencies.next_trace_id()
        _ensure_engineering_review_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
        )
        try:
            review_store = _require_engineering_review_read_store(record_store)
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        try:
            record = review_store.read_engineering_review_run_record(run_id)
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return EngineeringReviewRunResponse(trace_id=trace_id, run=record)

    def list_engineering_review_runs(
        identity: Annotated[LaunchplaneIdentity | None, Depends(read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        repository: Annotated[str, Query()] = "",
        state: Annotated[str, Query()] = "",
        work_request_id: Annotated[str, Query()] = "",
        limit: Annotated[str, Query()] = "50",
        offset: Annotated[str, Query()] = "0",
    ) -> EngineeringReviewRunsResponse:
        trace_id = dependencies.next_trace_id()
        _ensure_engineering_review_read_allowed(
            dependencies=dependencies,
            identity=identity,
            trace_id=trace_id,
        )
        try:
            review_store = _require_engineering_review_read_store(record_store)
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        try:
            resolved_limit = int(limit) if limit.strip().isdigit() else 50
            resolved_offset = int(offset) if offset.strip().isdigit() else 0
        except (ValueError, AttributeError):
            resolved_limit = 50
            resolved_offset = 0
        records = review_store.list_engineering_review_run_records(
            repository=repository.strip(),
            work_request_id=work_request_id.strip(),
            state=state.strip(),
            limit=max(1, min(resolved_limit, 200)),
            offset=max(0, resolved_offset),
        )
        return EngineeringReviewRunsResponse(
            trace_id=trace_id,
            repository=repository.strip(),
            state_filter=state.strip(),
            runs=records,
        )

    responses = {
        400: {"model": dependencies.error_response_model},
        401: {"model": dependencies.error_response_model},
        403: {"model": dependencies.error_response_model},
        404: {"model": dependencies.error_response_model},
        503: {"model": dependencies.error_response_model},
    }
    app.add_api_route(
        "/v1/engineering-review-runs/{run_id}",
        read_engineering_review_run,
        methods=["GET"],
        response_model=EngineeringReviewRunResponse,
        operation_id="read_engineering_review_run",
        summary="Read one engineering review run",
        responses=responses,
    )
    app.add_api_route(
        "/v1/engineering-review-runs",
        list_engineering_review_runs,
        methods=["GET"],
        response_model=EngineeringReviewRunsResponse,
        operation_id="list_engineering_review_runs",
        summary="List engineering review runs",
        responses=responses,
    )
