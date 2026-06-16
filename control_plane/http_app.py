from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Literal, Protocol
from uuid import uuid4

from fastapi import Depends, FastAPI, Header, HTTPException, Path, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from jwt import InvalidTokenError
from pydantic import BaseModel, ConfigDict

from control_plane import product_read_service as control_plane_product_read_service
from control_plane.contracts.authz_policy_record import (
    LaunchplaneAuthzPolicyRecord,
    authz_policy_sha256,
    build_authz_policy_record_id,
)
from control_plane.contracts.product_environment_read_model import (
    ProductEnvironmentConfigStatus,
)
from control_plane.service_auth import LaunchplaneAuthzPolicy, LaunchplaneIdentity, TokenVerifier
from control_plane.storage.factory import build_shared_record_store
from control_plane.storage.factory import storage_backend_name
from control_plane.storage.postgres import PostgresRecordStore


_BEARER_CHALLENGE_HEADER = {"WWW-Authenticate": 'Bearer realm="Launchplane API"'}


class LaunchplaneErrorDetail(BaseModel):
    model_config = ConfigDict(extra="forbid")

    code: str
    message: str


class LaunchplaneErrorResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "rejected"
    trace_id: str
    error: LaunchplaneErrorDetail


class HealthResponse(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        json_schema_extra={
            "examples": [
                {
                    "status": "ok",
                    "trace_id": "launchplane_req_00000000000000000000000000000000",
                    "storage_backend": "postgres",
                }
            ]
        },
    )

    status: Literal["ok"] = "ok"
    trace_id: str
    storage_backend: str


class ProductEnvironmentConfigStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str = "ok"
    trace_id: str
    config_status: ProductEnvironmentConfigStatus


class _RecordStoreFactory(Protocol):
    def __call__(self) -> object: ...


class LaunchplaneAuthzPolicyRuntime:
    def __init__(self, policy: LaunchplaneAuthzPolicy) -> None:
        self._policy = policy

    @property
    def policy(self) -> LaunchplaneAuthzPolicy:
        return self._policy

    def update(self, policy: LaunchplaneAuthzPolicy) -> None:
        self._policy = policy


class ResolvedLaunchplaneAuthzPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    policy: LaunchplaneAuthzPolicy
    policy_sha256: str
    source: str


def resolve_launchplane_authz_policy(
    *,
    record_store: object,
    bootstrap_policy: LaunchplaneAuthzPolicy,
    policy_source: str,
    now_timestamp: str,
) -> ResolvedLaunchplaneAuthzPolicy:
    list_records = getattr(record_store, "list_authz_policy_records", None)
    if callable(list_records):
        records = list_records(status="active", limit=1)
        if records:
            record = records[0]
            return ResolvedLaunchplaneAuthzPolicy(
                policy=record.policy,
                policy_sha256=record.policy_sha256,
                source="db",
            )

    policy_sha256 = authz_policy_sha256(bootstrap_policy)
    write_record = getattr(record_store, "write_authz_policy_record", None)
    if callable(write_record):
        record = LaunchplaneAuthzPolicyRecord(
            record_id=build_authz_policy_record_id(
                updated_at=now_timestamp,
                policy_sha256=policy_sha256,
            ),
            status="active",
            source=policy_source,
            updated_at=now_timestamp,
            policy_sha256=policy_sha256,
            policy=bootstrap_policy,
        )
        write_record(record)
        return ResolvedLaunchplaneAuthzPolicy(
            policy=record.policy,
            policy_sha256=record.policy_sha256,
            source="bootstrap_seeded_store",
        )

    return ResolvedLaunchplaneAuthzPolicy(
        policy=bootstrap_policy,
        policy_sha256=policy_sha256,
        source="bootstrap",
    )


def create_launchplane_fastapi_app(
    *,
    verifier: TokenVerifier,
    authz_policy: LaunchplaneAuthzPolicy,
    authz_policy_runtime: LaunchplaneAuthzPolicyRuntime | None = None,
    database_url: str | None = None,
    record_store_factory: _RecordStoreFactory | None = None,
) -> FastAPI:
    resolved_authz_policy_runtime = authz_policy_runtime or LaunchplaneAuthzPolicyRuntime(
        authz_policy
    )
    shared_record_store: object | None = (
        None
        if record_store_factory is not None
        else build_shared_record_store(database_url=database_url)
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI) -> AsyncIterator[None]:
        try:
            yield
        finally:
            if isinstance(shared_record_store, PostgresRecordStore):
                shared_record_store.close()

    app = FastAPI(title="Launchplane API", version="0.1.0", lifespan=lifespan)

    def next_trace_id() -> str:
        return f"launchplane_req_{uuid4().hex}"

    def get_record_store() -> object:
        if record_store_factory is not None:
            return record_store_factory()
        if shared_record_store is None:
            raise RuntimeError("Launchplane record store is not initialized.")
        return shared_record_store

    def read_identity(
        authorization: Annotated[str, Header(alias="Authorization")] = "",
    ) -> LaunchplaneIdentity:
        header = authorization.strip()
        if not header:
            raise _authentication_required_error("Authorization header is required.")
        scheme, _, token = header.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise _authentication_required_error(
                "Authorization header must use Bearer token format."
            )
        try:
            return verifier.verify(token.strip())
        except (InvalidTokenError, ValueError) as error:
            raise _authentication_required_error(str(error)) from error

    def read_product_environment_config_status(
        product: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        environment: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_identity)],
        record_store: Annotated[object, Depends(get_record_store)],
    ) -> ProductEnvironmentConfigStatusResponse:
        trace_id = next_trace_id()

        def product_action_allowed(
            requested_action: str, requested_product: str, requested_context: str
        ) -> bool:
            return resolved_authz_policy_runtime.policy.allows(
                identity=identity,
                action=requested_action,
                product=requested_product,
                context=requested_context,
            )

        try:
            product_read_store = (
                control_plane_product_read_service.require_product_environment_read_model_store(
                    record_store
                )
            )
            product_read_result = (
                control_plane_product_read_service.build_product_environment_read_service_result(
                    record_store=product_read_store,
                    params={
                        "product": product,
                        "environment": environment,
                        "config_status": "true",
                    },
                    action_allowed=product_action_allowed,
                )
            )
        except control_plane_product_read_service.ProductReadModelStoreCapabilityError as error:
            raise _launchplane_http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise _launchplane_http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        except ValueError as error:
            raise _launchplane_http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error

        if not product_action_allowed(
            "product_environment.read",
            product_read_result.authorization_product,
            product_read_result.authorization_context,
        ):
            raise _launchplane_http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=product_read_result.denial_message,
            )

        config_status = ProductEnvironmentConfigStatus.model_validate(
            product_read_result.payload["config_status"]
        )
        return ProductEnvironmentConfigStatusResponse(
            trace_id=trace_id,
            config_status=config_status,
        )

    def read_health(record_store: Annotated[object, Depends(get_record_store)]) -> HealthResponse:
        return HealthResponse(
            trace_id=next_trace_id(),
            storage_backend=storage_backend_name(record_store),
        )

    app.add_api_route(
        "/v1/health",
        read_health,
        methods=["GET"],
        response_model=HealthResponse,
        operation_id="read_launchplane_health",
        summary="Read Launchplane service health",
    )

    def launchplane_http_exception_handler(_request: Request, error: Exception) -> JSONResponse:
        if not isinstance(error, HTTPException):
            raise error
        http_error = error
        trace_id = next_trace_id()
        code = "authentication_required" if http_error.status_code == 401 else "http_error"
        if isinstance(http_error.detail, dict):
            detail = http_error.detail
            trace_id = str(detail.get("trace_id", trace_id))
            code = str(detail.get("code", code))
            message = str(detail.get("message", "Launchplane request failed."))
        else:
            message = str(http_error.detail)
        payload = LaunchplaneErrorResponse(
            trace_id=trace_id,
            error=LaunchplaneErrorDetail(code=code, message=message),
        )
        return JSONResponse(
            status_code=http_error.status_code,
            content=payload.model_dump(mode="json"),
            headers=http_error.headers,
        )

    def launchplane_request_validation_exception_handler(
        _request: Request, error: Exception
    ) -> JSONResponse:
        if not isinstance(error, RequestValidationError):
            raise error
        payload = LaunchplaneErrorResponse(
            trace_id=next_trace_id(),
            error=LaunchplaneErrorDetail(
                code="invalid_request",
                message="Launchplane request validation failed.",
            ),
        )
        return JSONResponse(
            status_code=400,
            content=payload.model_dump(mode="json"),
        )

    app.add_api_route(
        "/v1/products/{product}/environments/{environment}/config-status",
        read_product_environment_config_status,
        methods=["GET"],
        response_model=ProductEnvironmentConfigStatusResponse,
        responses={
            400: {"model": LaunchplaneErrorResponse},
            401: {"model": LaunchplaneErrorResponse},
            403: {"model": LaunchplaneErrorResponse},
            404: {"model": LaunchplaneErrorResponse},
            503: {"model": LaunchplaneErrorResponse},
        },
    )
    app.add_exception_handler(HTTPException, launchplane_http_exception_handler)
    app.add_exception_handler(
        RequestValidationError,
        launchplane_request_validation_exception_handler,
    )

    return app


def _launchplane_http_error(
    *, status_code: int, trace_id: str, code: str, message: str
) -> HTTPException:
    return HTTPException(
        status_code=status_code,
        detail={"trace_id": trace_id, "code": code, "message": message},
    )


def _authentication_required_error(message: str) -> HTTPException:
    return HTTPException(
        status_code=401,
        detail={"code": "authentication_required", "message": message},
        headers=_BEARER_CHALLENGE_HEADER,
    )
