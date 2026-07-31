from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal, cast

from fastapi import Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.http_routes.mutation_support import (
    idempotency_scope,
    request_fingerprint,
)
from control_plane.http_routes.support import (
    LAUNCHPLANE_SERVICE_CONTEXT as _LAUNCHPLANE_SERVICE_CONTEXT,
    ApiRouteRegistrar,
    AuthorizationAllows,
    HttpErrorFactory,
    ReadRouteDependencies,
)
from control_plane.service_auth import LaunchplaneIdentity, TerminalAgentIdentity
from control_plane.storage.postgres import DbOnlyMutationRequest, PostgresRecordStore
from control_plane.tenant_repository_classification import (
    TenantRepositoryClassificationApplyEnvelope,
    TenantRepositoryClassificationApplyResult,
    TenantRepositoryClassificationConflictError,
    TenantRepositoryClassificationReadModel,
    TenantRepositoryClassificationSequenceError,
    apply_tenant_repository_classification,
    get_tenant_repository_classification_read_model,
    require_tenant_repository_classification_read_store,
)

TENANT_REPOSITORY_CLASSIFICATION_READ_ROUTE = (
    "/v1/work-graph/tenant-admission/repository-classification"
)
TENANT_REPOSITORY_CLASSIFICATION_APPLY_ROUTE = (
    "/v1/tenant-admission/repository-classifications/apply"
)


@dataclass(frozen=True, slots=True)
class TenantAdmissionReadRouteDependencies:
    common: ReadRouteDependencies


@dataclass(frozen=True, slots=True)
class TenantAdmissionWriteRouteDependencies:
    read_write_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: AuthorizationAllows
    http_error: HttpErrorFactory
    error_response_model: type[BaseModel]


class TenantRepositoryClassificationReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    read_model: TenantRepositoryClassificationReadModel


class TenantRepositoryClassificationApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    result: TenantRepositoryClassificationApplyResult
    replayed: bool | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    original_trace_id: str | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


def register_tenant_admission_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: TenantAdmissionReadRouteDependencies,
) -> None:
    common = dependencies.common

    def read_tenant_repository_classification(
        repository_id: Annotated[str, Query(..., alias="repository_id")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> TenantRepositoryClassificationReadResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action="tenant_repository_classification.read",
            product=_LAUNCHPLANE_SERVICE_CONTEXT,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read tenant repository classifications.",
            )

        try:
            store = require_tenant_repository_classification_read_store(record_store)
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        try:
            read_model = get_tenant_repository_classification_read_model(
                repository_id=repository_id,
                store=store,
            )
        except ValueError as error:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error

        return TenantRepositoryClassificationReadResponse(
            trace_id=trace_id,
            read_model=read_model,
        )

    app.add_api_route(
        TENANT_REPOSITORY_CLASSIFICATION_READ_ROUTE,
        read_tenant_repository_classification,
        methods=["GET"],
        response_model=TenantRepositoryClassificationReadResponse,
        operation_id="read_tenant_repository_classification",
        summary="Read current tenant repository classification read model",
        responses={
            400: {"model": common.error_response_model},
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )


def register_tenant_admission_write_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: TenantAdmissionWriteRouteDependencies,
) -> None:
    async def apply_tenant_repository_classification_route(
        request: Request,
        envelope: TenantRepositoryClassificationApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> TenantRepositoryClassificationApplyResponse:
        trace_id = dependencies.next_trace_id()

        if isinstance(identity, TerminalAgentIdentity):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Terminal agent credentials cannot apply repository classifications.",
            )

        if not dependencies.authorization_allows(
            identity=identity,
            action="tenant_repository_classification.write",
            product=_LAUNCHPLANE_SERVICE_CONTEXT,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller lacks permission to apply tenant repository classifications.",
            )

        if envelope.mode == "apply":
            normalized_idempotency_key = idempotency_key.strip()
            if not normalized_idempotency_key:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="idempotency_key_required",
                    message="Apply operation requires an Idempotency-Key header.",
                )

            if (
                not isinstance(record_store, PostgresRecordStore)
                or record_store.database_dialect_name != "postgresql"
            ):
                raise dependencies.http_error(
                    status_code=503,
                    trace_id=trace_id,
                    code="database_storage_required",
                    message=(
                        "Tenant repository classification apply requires PostgreSQL-backed "
                        "Launchplane storage."
                    ),
                )

            store = record_store
            normalized_scope = idempotency_scope(identity)
            raw_payload = await request.json()
            payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
            result = TenantRepositoryClassificationApplyResult(
                status="applied",
                mode="apply",
                repository_id=envelope.record.repository_id,
                classification_revision=envelope.record.classification_revision,
                record_id=envelope.record.record_id,
                classification_digest=envelope.record.classification_digest,
                supersedes_record_id=envelope.record.supersedes_record_id,
                applied_at=envelope.record.classified_at,
            )
            response = TenantRepositoryClassificationApplyResponse(
                trace_id=trace_id,
                result=result,
            )
            try:
                write_result = store.compare_and_write_tenant_repository_classification_record(
                    record=envelope.record,
                    expected_current_record_id=envelope.expected_current_record_id,
                    mutation=DbOnlyMutationRequest(
                        scope=normalized_scope,
                        route_path=TENANT_REPOSITORY_CLASSIFICATION_APPLY_ROUTE,
                        idempotency_key=normalized_idempotency_key,
                        request_fingerprint=payload_fingerprint,
                        lease_owner=trace_id,
                        response_status_code=200,
                        response_trace_id=trace_id,
                        response_payload=response.model_dump(mode="json"),
                    ),
                )
            except TenantRepositoryClassificationConflictError as error:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="classification_conflict",
                    message=str(error),
                ) from error
            except TenantRepositoryClassificationSequenceError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_sequence",
                    message=str(error),
                ) from error
            except ValueError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_request",
                    message=str(error),
                ) from error

            if write_result.status == "idempotency_conflict":
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message=(
                        "Idempotency-Key was already used for a different "
                        "Launchplane request payload on this route."
                    ),
                )
            if write_result.status == "replayed" and write_result.idempotency_record is not None:
                payload = dict(write_result.idempotency_record.response_payload)
                payload["replayed"] = True
                payload["original_trace_id"] = write_result.idempotency_record.response_trace_id
                return TenantRepositoryClassificationApplyResponse.model_validate(payload)
            if write_result.status == "reservation_in_progress":
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_in_progress",
                    message="Tenant repository classification apply is already in progress.",
                )
            if write_result.status == "reconciliation_required":
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_reconciliation_required",
                    message=(
                        "Tenant repository classification apply requires reconciliation "
                        "before retry."
                    ),
                )
            if write_result.status == "written":
                return response
            raise dependencies.http_error(
                status_code=500,
                trace_id=trace_id,
                code="internal_error",
                message="Tenant repository classification apply returned an unknown result.",
            )
        else:
            try:
                read_store = require_tenant_repository_classification_read_store(record_store)
            except TypeError as error:
                raise dependencies.http_error(
                    status_code=503,
                    trace_id=trace_id,
                    code="database_storage_required",
                    message=str(error),
                ) from error

            try:
                result = apply_tenant_repository_classification(
                    store=read_store,
                    record=envelope.record,
                    expected_current_record_id=envelope.expected_current_record_id,
                    mode="dry_run",
                )
            except TenantRepositoryClassificationConflictError as error:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="classification_conflict",
                    message=str(error),
                ) from error
            except TenantRepositoryClassificationSequenceError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_sequence",
                    message=str(error),
                ) from error
            except ValueError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_request",
                    message=str(error),
                ) from error

            return TenantRepositoryClassificationApplyResponse(
                trace_id=trace_id,
                result=result,
            )

    app.add_api_route(
        TENANT_REPOSITORY_CLASSIFICATION_APPLY_ROUTE,
        apply_tenant_repository_classification_route,
        methods=["POST"],
        response_model=TenantRepositoryClassificationApplyResponse,
        operation_id="apply_tenant_repository_classification",
        summary="Apply or dry-run a tenant repository classification record",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
