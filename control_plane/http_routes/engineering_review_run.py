from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, cast

from fastapi import Depends, Path, Query
from pydantic import BaseModel, ConfigDict

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.engineering_review_run import (
    ENGINEERING_REVIEW_AUTHORITY_READ_ACTION,
    ENGINEERING_REVIEW_AUTHORITY_WRITE_ACTION,
    ENGINEERING_REVIEW_RUN_CREATE_ACTION,
    ENGINEERING_REVIEW_RUN_READ_ACTION,
    EngineeringReviewAuthorityApplyEnvelope,
    EngineeringReviewAuthorityRecord,
    EngineeringReviewConflictError,
    EngineeringReviewRunAssignment,
    EngineeringReviewRunClaimRequest,
    EngineeringReviewRunCreateRequest,
    EngineeringReviewRunFailure,
    EngineeringReviewRunRecord,
    EngineeringReviewRunSubmission,
    EngineeringReviewRunView,
    EngineeringReviewRunWorkerFailure,
    EngineeringReviewRunWorkerUpdate,
    EngineeringReviewSequenceError,
    EngineeringReviewUnavailableError,
    engineering_review_run_view,
)
from control_plane.engineering_review_service import (
    EngineeringReviewAuthorityApplyResult,
    EngineeringReviewTargetResolver,
    apply_engineering_review_authority,
    create_engineering_review_runs,
    require_engineering_review_authority_store,
    require_engineering_review_run_create_store,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import LaunchplaneIdentity


class EngineeringReviewRunResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    run: EngineeringReviewRunView


class EngineeringReviewRunsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    repository: str = ""
    state_filter: str = ""
    runs: tuple[EngineeringReviewRunView, ...]


class EngineeringReviewAuthorityResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    authority: EngineeringReviewAuthorityRecord


class EngineeringReviewAuthorityApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    result: EngineeringReviewAuthorityApplyResult


class EngineeringReviewRunCreateResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    runs: tuple[EngineeringReviewRunView, ...]


class EngineeringReviewRunClaimResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    assignment: EngineeringReviewRunAssignment


class EngineeringReviewRunMaintenanceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    expired_count: int


class EngineeringReviewStore(Protocol):
    def read_engineering_review_run_record(self, run_id: str) -> EngineeringReviewRunRecord: ...
    def list_engineering_review_run_records(
        self,
        *,
        repository: str = "",
        pr_number: int | None = None,
        work_request_id: str = "",
        worker_runtime_id: str = "",
        worker_host: str = "",
        state: str = "",
        limit: int | None = None,
        offset: int = 0,
    ) -> tuple[EngineeringReviewRunRecord, ...]: ...
    def list_engineering_review_authority_records(
        self,
        *,
        repository: str = "",
        status: str = "",
        limit: int | None = None,
    ) -> tuple[EngineeringReviewAuthorityRecord, ...]: ...
    def claim_engineering_review_run_record(
        self,
        *,
        run_id: str,
        worker_runtime_id: str,
        worker_host: str,
    ) -> EngineeringReviewRunRecord: ...
    def start_engineering_review_run_record(
        self,
        *,
        run_id: str,
        worker_runtime_id: str,
        worker_host: str,
        fencing_token: int,
    ) -> EngineeringReviewRunRecord: ...
    def submit_engineering_review_run_record(
        self,
        submission: EngineeringReviewRunSubmission,
    ) -> EngineeringReviewRunRecord: ...
    def fail_engineering_review_run_record(
        self,
        *,
        run_id: str,
        worker_runtime_id: str,
        worker_host: str,
        fencing_token: int,
        failure: EngineeringReviewRunFailure,
    ) -> EngineeringReviewRunRecord: ...
    def expire_stale_engineering_review_run_records(
        self,
        *,
        limit: int = 50,
    ) -> tuple[EngineeringReviewRunRecord, ...]: ...


@dataclass(frozen=True, slots=True)
class EngineeringReviewWriteRouteDependencies:
    read_write_identity: Callable[..., LaunchplaneIdentity]
    require_worker_token: Callable[..., None]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: Callable[..., bool]
    http_error: Callable[..., Exception]
    error_response_model: type[BaseModel]
    target_resolver: EngineeringReviewTargetResolver


def _store(record_store: object) -> EngineeringReviewStore:
    required = (
        "read_engineering_review_run_record",
        "list_engineering_review_run_records",
        "list_engineering_review_authority_records",
        "claim_engineering_review_run_record",
        "start_engineering_review_run_record",
        "submit_engineering_review_run_record",
        "fail_engineering_review_run_record",
        "expire_stale_engineering_review_run_records",
    )
    missing = [name for name in required if not callable(getattr(record_store, name, None))]
    if missing:
        raise TypeError(
            "record store does not support engineering review runs: " + ", ".join(missing)
        )
    return cast(EngineeringReviewStore, record_store)


def _read_store(record_store: object) -> EngineeringReviewStore:
    required = ("read_engineering_review_run_record", "list_engineering_review_run_records")
    missing = [name for name in required if not callable(getattr(record_store, name, None))]
    if missing:
        raise TypeError(
            "record store does not support engineering review reads: " + ", ".join(missing)
        )
    return cast(EngineeringReviewStore, record_store)


def register_engineering_review_routes(
    app: ApiRouteRegistrar,
    *,
    read_dependencies: ReadRouteDependencies,
    write_dependencies: EngineeringReviewWriteRouteDependencies,
    read_identity: Callable[..., LaunchplaneIdentity | None],
) -> None:
    def authorized(identity: LaunchplaneIdentity, action: str, trace_id: str) -> None:
        if not write_dependencies.authorization_allows(
            identity=identity, action=action, product="launchplane", context="launchplane"
        ):
            raise write_dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="forbidden",
                message=f"Identity cannot perform {action}.",
            )

    def route_error(trace_id: str, error: Exception) -> Exception:
        if isinstance(error, FileNotFoundError):
            return write_dependencies.http_error(
                status_code=404, trace_id=trace_id, code="not_found", message=str(error)
            )
        if isinstance(error, (EngineeringReviewConflictError, EngineeringReviewSequenceError)):
            return write_dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="engineering_review_conflict",
                message=str(error),
            )
        return write_dependencies.http_error(
            status_code=503,
            trace_id=trace_id,
            code="engineering_review_unavailable",
            message=str(error),
        )

    def read_authority(
        repository: Annotated[str, Query(min_length=3)],
        identity: Annotated[LaunchplaneIdentity, Depends(write_dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(write_dependencies.get_record_store)],
    ) -> EngineeringReviewAuthorityResponse:
        trace_id = write_dependencies.next_trace_id()
        authorized(identity, ENGINEERING_REVIEW_AUTHORITY_READ_ACTION, trace_id)
        authorities = _store(record_store).list_engineering_review_authority_records(
            repository=repository, status="active", limit=2
        )
        if len(authorities) != 1:
            raise route_error(
                trace_id,
                EngineeringReviewUnavailableError(
                    "Exactly one active engineering review authority is required."
                ),
            )
        return EngineeringReviewAuthorityResponse(trace_id=trace_id, authority=authorities[0])

    async def apply_authority(
        envelope: EngineeringReviewAuthorityApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(write_dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(write_dependencies.get_record_store)],
    ) -> EngineeringReviewAuthorityApplyResponse:
        trace_id = write_dependencies.next_trace_id()
        authorized(identity, ENGINEERING_REVIEW_AUTHORITY_WRITE_ACTION, trace_id)
        try:
            result = apply_engineering_review_authority(
                store=require_engineering_review_authority_store(record_store),
                record=envelope.record,
                expected_current_authority_id=envelope.expected_current_authority_id,
                expected_current_authority_digest=envelope.expected_current_authority_digest,
                mode=envelope.mode,
            )
        except (TypeError, EngineeringReviewConflictError, EngineeringReviewSequenceError) as error:
            raise route_error(trace_id, error) from error
        return EngineeringReviewAuthorityApplyResponse(trace_id=trace_id, result=result)

    async def create_runs(
        request: EngineeringReviewRunCreateRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(write_dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(write_dependencies.get_record_store)],
    ) -> EngineeringReviewRunCreateResponse:
        trace_id = write_dependencies.next_trace_id()
        authorized(identity, ENGINEERING_REVIEW_RUN_CREATE_ACTION, trace_id)
        try:
            result = create_engineering_review_runs(
                store=require_engineering_review_run_create_store(record_store),
                work_request_id=request.work_request_id,
                target_resolver=write_dependencies.target_resolver,
            )
        except (
            TypeError,
            FileNotFoundError,
            EngineeringReviewConflictError,
            EngineeringReviewUnavailableError,
        ) as error:
            raise route_error(trace_id, error) from error
        return EngineeringReviewRunCreateResponse(
            trace_id=trace_id,
            runs=tuple(engineering_review_run_view(run) for run in result.runs),
        )

    def list_pending(
        worker_runtime_id: Annotated[str, Query(min_length=1)],
        worker_host: Annotated[str, Query(min_length=1)],
        _token: Annotated[None, Depends(write_dependencies.require_worker_token)],
        record_store: Annotated[object, Depends(write_dependencies.get_record_store)],
    ) -> EngineeringReviewRunsResponse:
        trace_id = write_dependencies.next_trace_id()
        runs = _store(record_store).list_engineering_review_run_records(
            worker_runtime_id=worker_runtime_id,
            worker_host=worker_host,
            state="pending",
            limit=50,
        )
        return EngineeringReviewRunsResponse(
            trace_id=trace_id,
            state_filter="pending",
            runs=tuple(engineering_review_run_view(run) for run in runs),
        )

    async def claim_run(
        request: EngineeringReviewRunClaimRequest,
        _token: Annotated[None, Depends(write_dependencies.require_worker_token)],
        record_store: Annotated[object, Depends(write_dependencies.get_record_store)],
    ) -> EngineeringReviewRunClaimResponse:
        trace_id = write_dependencies.next_trace_id()
        try:
            review_store = _store(record_store)
            before = review_store.read_engineering_review_run_record(request.run_id)
            credential = control_plane_secrets._decrypt_secret_value(
                before.credential_ciphertext, before.credential_key_id
            )
            claimed = review_store.claim_engineering_review_run_record(
                run_id=request.run_id,
                worker_runtime_id=request.worker_runtime_id,
                worker_host=request.worker_host,
            )
        except Exception as error:
            raise route_error(trace_id, error) from error
        return EngineeringReviewRunClaimResponse(
            trace_id=trace_id,
            assignment=EngineeringReviewRunAssignment(
                run=engineering_review_run_view(claimed),
                credential=credential,
                worker_host=claimed.worker_host,
                executable_path=claimed.executable_path,
            ),
        )

    async def start_run(
        request: EngineeringReviewRunWorkerUpdate,
        _token: Annotated[None, Depends(write_dependencies.require_worker_token)],
        record_store: Annotated[object, Depends(write_dependencies.get_record_store)],
    ) -> EngineeringReviewRunResponse:
        trace_id = write_dependencies.next_trace_id()
        try:
            run = _store(record_store).start_engineering_review_run_record(**request.model_dump())
        except Exception as error:
            raise route_error(trace_id, error) from error
        return EngineeringReviewRunResponse(trace_id=trace_id, run=engineering_review_run_view(run))

    async def fail_run(
        request: EngineeringReviewRunWorkerFailure,
        _token: Annotated[None, Depends(write_dependencies.require_worker_token)],
        record_store: Annotated[object, Depends(write_dependencies.get_record_store)],
    ) -> EngineeringReviewRunResponse:
        trace_id = write_dependencies.next_trace_id()
        payload = request.model_dump(exclude={"error_code", "summary"})
        try:
            run = _store(record_store).fail_engineering_review_run_record(
                **payload,
                failure=EngineeringReviewRunFailure(
                    error_code=request.error_code, summary=request.summary
                ),
            )
        except Exception as error:
            raise route_error(trace_id, error) from error
        return EngineeringReviewRunResponse(trace_id=trace_id, run=engineering_review_run_view(run))

    async def complete_run(
        submission: EngineeringReviewRunSubmission,
        record_store: Annotated[object, Depends(write_dependencies.get_record_store)],
    ) -> EngineeringReviewRunResponse:
        trace_id = write_dependencies.next_trace_id()
        try:
            run = _store(record_store).submit_engineering_review_run_record(submission)
        except Exception as error:
            raise route_error(trace_id, error) from error
        return EngineeringReviewRunResponse(trace_id=trace_id, run=engineering_review_run_view(run))

    async def expire_runs(
        _token: Annotated[None, Depends(write_dependencies.require_worker_token)],
        record_store: Annotated[object, Depends(write_dependencies.get_record_store)],
    ) -> EngineeringReviewRunMaintenanceResponse:
        trace_id = write_dependencies.next_trace_id()
        expired = _store(record_store).expire_stale_engineering_review_run_records()
        return EngineeringReviewRunMaintenanceResponse(
            trace_id=trace_id, expired_count=len(expired)
        )

    def read_run(
        run_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity | None, Depends(read_identity)],
        record_store: Annotated[object, Depends(read_dependencies.get_record_store)],
    ) -> EngineeringReviewRunResponse:
        trace_id = read_dependencies.next_trace_id()
        if identity is not None and not read_dependencies.authorization_allows(
            identity=identity,
            action=ENGINEERING_REVIEW_RUN_READ_ACTION,
            product="launchplane",
            context="launchplane",
        ):
            raise read_dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="forbidden",
                message="Identity cannot read engineering review runs.",
            )
        try:
            run = _read_store(record_store).read_engineering_review_run_record(run_id)
        except Exception as error:
            raise route_error(trace_id, error) from error
        return EngineeringReviewRunResponse(trace_id=trace_id, run=engineering_review_run_view(run))

    def list_runs(
        identity: Annotated[LaunchplaneIdentity | None, Depends(read_identity)],
        record_store: Annotated[object, Depends(read_dependencies.get_record_store)],
        repository: Annotated[str, Query()] = "",
        state: Annotated[str, Query()] = "",
        work_request_id: Annotated[str, Query()] = "",
    ) -> EngineeringReviewRunsResponse:
        trace_id = read_dependencies.next_trace_id()
        if identity is not None and not read_dependencies.authorization_allows(
            identity=identity,
            action=ENGINEERING_REVIEW_RUN_READ_ACTION,
            product="launchplane",
            context="launchplane",
        ):
            raise read_dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="forbidden",
                message="Identity cannot read engineering review runs.",
            )
        records = _read_store(record_store).list_engineering_review_run_records(
            repository=repository.strip(),
            state=state.strip(),
            work_request_id=work_request_id.strip(),
            limit=200,
        )
        return EngineeringReviewRunsResponse(
            trace_id=trace_id,
            repository=repository.strip(),
            state_filter=state.strip(),
            runs=tuple(engineering_review_run_view(record) for record in records),
        )

    errors = {
        400: {"model": write_dependencies.error_response_model},
        401: {"model": write_dependencies.error_response_model},
        403: {"model": write_dependencies.error_response_model},
        404: {"model": write_dependencies.error_response_model},
        409: {"model": write_dependencies.error_response_model},
        503: {"model": write_dependencies.error_response_model},
    }
    routes = (
        (
            "/v1/engineering-review-authority",
            read_authority,
            ["GET"],
            EngineeringReviewAuthorityResponse,
            "read_engineering_review_authority",
        ),
        (
            "/v1/engineering-review-authorities/apply",
            apply_authority,
            ["POST"],
            EngineeringReviewAuthorityApplyResponse,
            "apply_engineering_review_authority",
        ),
        (
            "/v1/engineering-review-runs/create",
            create_runs,
            ["POST"],
            EngineeringReviewRunCreateResponse,
            "create_engineering_review_runs",
        ),
        (
            "/v1/engineering-review-runs/pending",
            list_pending,
            ["GET"],
            EngineeringReviewRunsResponse,
            "list_pending_engineering_review_runs",
        ),
        (
            "/v1/engineering-review-runs/claim",
            claim_run,
            ["POST"],
            EngineeringReviewRunClaimResponse,
            "claim_engineering_review_run",
        ),
        (
            "/v1/engineering-review-runs/start",
            start_run,
            ["POST"],
            EngineeringReviewRunResponse,
            "start_engineering_review_run",
        ),
        (
            "/v1/engineering-review-runs/fail",
            fail_run,
            ["POST"],
            EngineeringReviewRunResponse,
            "fail_engineering_review_run",
        ),
        (
            "/v1/engineering-review-runs/complete",
            complete_run,
            ["POST"],
            EngineeringReviewRunResponse,
            "complete_engineering_review_run",
        ),
        (
            "/v1/engineering-review-runs/expire-stale",
            expire_runs,
            ["POST"],
            EngineeringReviewRunMaintenanceResponse,
            "expire_stale_engineering_review_runs",
        ),
        (
            "/v1/engineering-review-runs",
            list_runs,
            ["GET"],
            EngineeringReviewRunsResponse,
            "list_engineering_review_runs",
        ),
        (
            "/v1/engineering-review-runs/{run_id}",
            read_run,
            ["GET"],
            EngineeringReviewRunResponse,
            "read_engineering_review_run",
        ),
    )
    for path, endpoint, methods, response_model, operation_id in routes:
        app.add_api_route(
            path,
            endpoint,
            methods=methods,
            response_model=response_model,
            operation_id=operation_id,
            responses=errors,
        )
