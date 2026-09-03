from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal, cast

from fastapi import Depends, Header, Query
from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.production_backup_authority import (
    PRODUCTION_BACKUP_AUTHORITY_READ_ACTION,
    PRODUCTION_BACKUP_AUTHORITY_WRITE_ACTION,
    ProductionBackupAuthorityReadModel,
)
from control_plane.http_routes.mutation_support import idempotency_scope, request_fingerprint
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    AuthorizationAllows,
    HttpErrorFactory,
    ReadRouteDependencies,
)
from control_plane.production_backup_authority import (
    ProductionBackupAuthorityConflictError,
    ProductionBackupAuthoritySequenceError,
    ProductionBackupAuthorityWriteEnvelope,
    ProductionBackupAuthorityWriteResult,
    plan_production_backup_authority_write,
    require_production_backup_authority_store,
    resolve_production_backup_authority,
)
from control_plane.production_backup_migration import (
    LegacyProductionBackupMigrationRequest,
    LegacyProductionBackupMigrationStore,
    build_legacy_production_backup_authority_envelope,
)
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.storage.postgres import DbOnlyMutationRequest, PostgresRecordStore
from control_plane.workflows.ship import utc_now_timestamp


PRODUCTION_BACKUP_AUTHORITY_READ_ROUTE = "/v1/production-backup-authority"
PRODUCTION_BACKUP_AUTHORITY_APPLY_ROUTE = "/v1/production-backup-authority/apply"
PRODUCTION_BACKUP_AUTHORITY_LEGACY_MIGRATION_ROUTE = (
    "/v1/production-backup-authority/legacy-runtime-migration"
)


@dataclass(frozen=True, slots=True)
class ProductionBackupAuthorityWriteRouteDependencies:
    read_write_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: AuthorizationAllows
    http_error: HttpErrorFactory
    error_response_model: type[BaseModel]


class ProductionBackupAuthorityReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    authority: ProductionBackupAuthorityReadModel


class ProductionBackupAuthorityWriteResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    result: ProductionBackupAuthorityWriteResult
    replayed: bool | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    original_trace_id: str | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


def register_production_backup_authority_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def read_production_backup_authority(
        product: Annotated[str, Query(...)],
        context: Annotated[str, Query(...)],
        instance: Annotated[str, Query(...)],
        promotion_action: Annotated[str, Query(...)],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> ProductionBackupAuthorityReadResponse:
        trace_id = dependencies.next_trace_id()
        normalized_product = product.strip().lower()
        normalized_context = context.strip().lower()
        normalized_instance = instance.strip().lower()
        if not dependencies.authorization_allows(
            identity=identity,
            action=PRODUCTION_BACKUP_AUTHORITY_READ_ACTION,
            product=normalized_product,
            context=normalized_context,
            target=AuthorizationTarget(scope="instance", instances=(normalized_instance,)),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read production backup authority.",
            )
        try:
            authority = resolve_production_backup_authority(
                record_store=require_production_backup_authority_store(record_store),
                product=normalized_product,
                context=normalized_context,
                instance=normalized_instance,
                promotion_action=promotion_action,
                generated_at=utc_now_timestamp(),
            )
        except (TypeError, ValueError) as error:
            raise dependencies.http_error(
                status_code=503 if isinstance(error, TypeError) else 400,
                trace_id=trace_id,
                code="database_storage_required"
                if isinstance(error, TypeError)
                else "invalid_request",
                message=str(error),
            ) from error
        return ProductionBackupAuthorityReadResponse(trace_id=trace_id, authority=authority)

    app.add_api_route(
        PRODUCTION_BACKUP_AUTHORITY_READ_ROUTE,
        read_production_backup_authority,
        methods=["GET"],
        response_model=ProductionBackupAuthorityReadResponse,
        operation_id="read_production_backup_authority",
        summary="Read exact production backup target and policy readiness",
        responses={
            status_code: {"model": dependencies.error_response_model}
            for status_code in (400, 401, 403, 503)
        },
    )


def register_production_backup_authority_write_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ProductionBackupAuthorityWriteRouteDependencies,
) -> None:
    def write_production_backup_authority(
        envelope: ProductionBackupAuthorityWriteEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> ProductionBackupAuthorityWriteResponse:
        return _execute_write(
            dependencies=dependencies,
            identity=identity,
            record_store=record_store,
            envelope=envelope,
            route_path=PRODUCTION_BACKUP_AUTHORITY_APPLY_ROUTE,
            idempotency_key=idempotency_key,
        )

    def migrate_legacy_production_backup_authority(
        migration: LegacyProductionBackupMigrationRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> ProductionBackupAuthorityWriteResponse:
        trace_id = dependencies.next_trace_id()
        normalized_product = migration.product.strip().lower()
        normalized_context = migration.context.strip().lower()
        normalized_instance = migration.instance.strip().lower()
        _require_write_authorization(
            dependencies=dependencies,
            identity=identity,
            product=normalized_product,
            context=normalized_context,
            instance=normalized_instance,
            trace_id=trace_id,
        )
        migration_payload = cast(
            dict[str, object], migration.model_dump(mode="json", exclude_none=True)
        )
        replay = _preflight_apply(
            dependencies=dependencies,
            identity=identity,
            record_store=record_store,
            mode=migration.mode,
            route_path=PRODUCTION_BACKUP_AUTHORITY_LEGACY_MIGRATION_ROUTE,
            idempotency_key=idempotency_key,
            request_payload=migration_payload,
            trace_id=trace_id,
        )
        if replay is not None:
            return replay
        try:
            envelope = build_legacy_production_backup_authority_envelope(
                record_store=cast(LegacyProductionBackupMigrationStore, record_store),
                request=migration,
            )
        except (AttributeError, TypeError, ValueError) as error:
            raise dependencies.http_error(
                status_code=503 if isinstance(error, (AttributeError, TypeError)) else 409,
                trace_id=trace_id,
                code="database_storage_required"
                if isinstance(error, (AttributeError, TypeError))
                else "legacy_backup_migration_conflict",
                message=str(error),
            ) from error
        return _execute_write(
            dependencies=dependencies,
            identity=identity,
            record_store=record_store,
            envelope=envelope,
            route_path=PRODUCTION_BACKUP_AUTHORITY_LEGACY_MIGRATION_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            authorization_checked=True,
            request_payload=migration_payload,
        )

    app.add_api_route(
        PRODUCTION_BACKUP_AUTHORITY_APPLY_ROUTE,
        write_production_backup_authority,
        methods=["POST"],
        response_model=ProductionBackupAuthorityWriteResponse,
        operation_id="apply_production_backup_authority",
        summary="Dry-run or apply typed production backup authority",
        responses={
            status_code: {"model": dependencies.error_response_model}
            for status_code in (400, 401, 403, 409, 503)
        },
    )
    app.add_api_route(
        PRODUCTION_BACKUP_AUTHORITY_LEGACY_MIGRATION_ROUTE,
        migrate_legacy_production_backup_authority,
        methods=["POST"],
        response_model=ProductionBackupAuthorityWriteResponse,
        operation_id="migrate_legacy_production_backup_authority",
        summary="Dry-run or apply an exact legacy runtime backup migration",
        responses={
            status_code: {"model": dependencies.error_response_model}
            for status_code in (400, 401, 403, 409, 503)
        },
    )


def _execute_write(
    *,
    dependencies: ProductionBackupAuthorityWriteRouteDependencies,
    identity: LaunchplaneIdentity,
    record_store: object,
    envelope: ProductionBackupAuthorityWriteEnvelope,
    route_path: str,
    idempotency_key: str,
    trace_id: str = "",
    authorization_checked: bool = False,
    request_payload: dict[str, object] | None = None,
) -> ProductionBackupAuthorityWriteResponse:
    current_trace_id = trace_id or dependencies.next_trace_id()
    if not authorization_checked:
        _require_write_authorization(
            dependencies=dependencies,
            identity=identity,
            product=envelope.policy.product,
            context=envelope.policy.context,
            instance=envelope.policy.instance,
            trace_id=current_trace_id,
        )
    raw_payload = request_payload or cast(
        dict[str, object], envelope.model_dump(mode="json", exclude_none=True)
    )
    replay = _preflight_apply(
        dependencies=dependencies,
        identity=identity,
        record_store=record_store,
        mode=envelope.mode,
        route_path=route_path,
        idempotency_key=idempotency_key,
        request_payload=raw_payload,
        trace_id=current_trace_id,
    )
    if replay is not None:
        return replay
    try:
        authority_store = require_production_backup_authority_store(record_store)
        plan = plan_production_backup_authority_write(
            record_store=authority_store,
            envelope=envelope,
        )
    except (
        ProductionBackupAuthorityConflictError,
        ProductionBackupAuthoritySequenceError,
    ) as error:
        raise dependencies.http_error(
            status_code=409,
            trace_id=current_trace_id,
            code="production_backup_authority_conflict",
            message=str(error),
        ) from error
    except (TypeError, ValueError) as error:
        raise dependencies.http_error(
            status_code=503 if isinstance(error, TypeError) else 400,
            trace_id=current_trace_id,
            code="database_storage_required" if isinstance(error, TypeError) else "invalid_request",
            message=str(error),
        ) from error
    if envelope.mode == "dry_run":
        return ProductionBackupAuthorityWriteResponse(
            trace_id=current_trace_id,
            result=plan.result,
        )
    normalized_idempotency_key = idempotency_key.strip()
    assert normalized_idempotency_key
    assert isinstance(record_store, PostgresRecordStore)
    try:
        write_result = record_store.compare_and_apply_production_backup_authority(
            envelope=envelope,
            mutation=DbOnlyMutationRequest(
                scope=idempotency_scope(identity),
                route_path=route_path,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint=request_fingerprint(raw_payload),
                lease_owner=current_trace_id,
                response_status_code=200,
                response_trace_id=current_trace_id,
                response_payload={},
            ),
            response_payload_builder=lambda locked_result: ProductionBackupAuthorityWriteResponse(
                trace_id=current_trace_id,
                result=locked_result,
            ).model_dump(mode="json"),
        )
    except (
        ProductionBackupAuthorityConflictError,
        ProductionBackupAuthoritySequenceError,
    ) as error:
        raise dependencies.http_error(
            status_code=409,
            trace_id=current_trace_id,
            code="production_backup_authority_conflict",
            message=str(error),
        ) from error
    if write_result.status == "idempotency_conflict":
        raise dependencies.http_error(
            status_code=409,
            trace_id=current_trace_id,
            code="idempotency_key_reused",
            message="Idempotency-Key was already used for a different request.",
        )
    if write_result.status == "reservation_in_progress":
        raise dependencies.http_error(
            status_code=409,
            trace_id=current_trace_id,
            code="mutation_in_progress",
            message="Production backup authority apply is already in progress.",
        )
    if write_result.status == "reconciliation_required":
        raise dependencies.http_error(
            status_code=409,
            trace_id=current_trace_id,
            code="mutation_reconciliation_required",
            message="Production backup authority apply requires reconciliation.",
        )
    if write_result.status == "replayed" and write_result.idempotency_record is not None:
        payload = dict(write_result.idempotency_record.response_payload)
        payload["replayed"] = True
        payload["original_trace_id"] = write_result.idempotency_record.response_trace_id
        return ProductionBackupAuthorityWriteResponse.model_validate(payload)
    assert write_result.result is not None
    return ProductionBackupAuthorityWriteResponse(
        trace_id=current_trace_id,
        result=write_result.result,
    )


def _preflight_apply(
    *,
    dependencies: ProductionBackupAuthorityWriteRouteDependencies,
    identity: LaunchplaneIdentity,
    record_store: object,
    mode: str,
    route_path: str,
    idempotency_key: str,
    request_payload: dict[str, object],
    trace_id: str,
) -> ProductionBackupAuthorityWriteResponse | None:
    if mode != "apply":
        return None
    normalized_idempotency_key = idempotency_key.strip()
    if not normalized_idempotency_key:
        raise dependencies.http_error(
            status_code=400,
            trace_id=trace_id,
            code="missing_idempotency_key",
            message="Production backup authority apply requires an Idempotency-Key header.",
        )
    if not isinstance(record_store, PostgresRecordStore):
        raise dependencies.http_error(
            status_code=503,
            trace_id=trace_id,
            code="database_storage_required",
            message="Production backup authority apply requires PostgreSQL-backed storage.",
        )
    preflight = record_store.prepare_db_only_mutation(
        scope=idempotency_scope(identity),
        route_path=route_path,
        idempotency_key=normalized_idempotency_key,
        request_fingerprint=request_fingerprint(request_payload),
    )
    if preflight.status == "replayed" and preflight.record is not None:
        payload = dict(preflight.record.response_payload)
        payload["replayed"] = True
        payload["original_trace_id"] = preflight.record.response_trace_id
        return ProductionBackupAuthorityWriteResponse.model_validate(payload)
    if preflight.status == "conflict":
        raise dependencies.http_error(
            status_code=409,
            trace_id=trace_id,
            code="idempotency_key_reused",
            message="Idempotency-Key was already used for a different request.",
        )
    if preflight.status == "in_progress":
        raise dependencies.http_error(
            status_code=409,
            trace_id=trace_id,
            code="mutation_in_progress",
            message="Production backup authority apply is already in progress.",
        )
    if preflight.status == "reconcile_required":
        raise dependencies.http_error(
            status_code=409,
            trace_id=trace_id,
            code="mutation_reconciliation_required",
            message="Production backup authority apply requires reconciliation.",
        )
    return None


def _require_write_authorization(
    *,
    dependencies: ProductionBackupAuthorityWriteRouteDependencies,
    identity: LaunchplaneIdentity,
    product: str,
    context: str,
    instance: str,
    trace_id: str,
) -> None:
    if dependencies.authorization_allows(
        identity=identity,
        action=PRODUCTION_BACKUP_AUTHORITY_WRITE_ACTION,
        product=product,
        context=context,
        target=AuthorizationTarget(scope="instance", instances=(instance,)),
    ):
        return
    raise dependencies.http_error(
        status_code=403,
        trace_id=trace_id,
        code="authorization_denied",
        message="Identity cannot write production backup authority.",
    )
