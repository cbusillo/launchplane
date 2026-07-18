from typing import Annotated, Literal, Protocol, cast

from fastapi import Depends, Path
from pydantic import BaseModel, ConfigDict

from control_plane import secrets as control_plane_secrets
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.environment_inventory import EnvironmentInventory
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.secret_record import SecretScope
from control_plane.http_routes.support import (
    LAUNCHPLANE_SERVICE_CONTEXT,
    ApiRouteRegistrar,
    ReadRouteDependencies,
)
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.storage.factory import storage_backend_name


class DeploymentRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: DeploymentRecord


class PromotionRecordResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: PromotionRecord


class EnvironmentInventoryResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    record: EnvironmentInventory


class RecentOperationsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    context: str
    storage_backend: str
    inventory: tuple[EnvironmentInventory, ...]
    recent_deployments: tuple[DeploymentRecord, ...]
    recent_promotions: tuple[PromotionRecord, ...]
    recent_previews: tuple[PreviewRecord, ...]


class SecretStatusBinding(BaseModel):
    model_config = ConfigDict(extra="forbid")

    binding_id: str
    binding_type: str
    binding_key: str
    status: str
    context: str
    instance: str
    updated_at: str


class SecretStatusAuditEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str
    event_type: str
    recorded_at: str
    actor: str
    detail: str
    metadata: dict[str, str]


class SecretStatusReadModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    secret_id: str
    scope: SecretScope
    integration: str
    name: str
    context: str
    instance: str
    policy: str
    status: str
    description: str
    created_at: str
    updated_at: str
    updated_by: str
    last_validated_at: str
    current_version_id: str
    version_count: int
    current_version_created_at: str
    binding: SecretStatusBinding | None = None
    recent_audit_events: tuple[SecretStatusAuditEvent, ...]


class SecretStatusResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    secret: SecretStatusReadModel


class SecretStatusListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    context: str
    instance: str
    secrets: tuple[SecretStatusReadModel, ...]


class DeploymentReadStore(Protocol):
    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...


class PromotionReadStore(Protocol):
    def read_promotion_record(self, record_id: str) -> PromotionRecord: ...


class EnvironmentInventoryReadStore(Protocol):
    def read_environment_inventory(
        self,
        *,
        context_name: str,
        instance_name: str,
    ) -> EnvironmentInventory: ...


class RecentOperationsReadStore(Protocol):
    def list_environment_inventory(self) -> tuple[EnvironmentInventory, ...]: ...

    def list_deployment_records(
        self,
        *,
        context_name: str = "",
        instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[DeploymentRecord, ...]: ...

    def list_promotion_records(
        self,
        *,
        context_name: str = "",
        from_instance_name: str = "",
        to_instance_name: str = "",
        limit: int | None = None,
    ) -> tuple[PromotionRecord, ...]: ...

    def list_preview_records(
        self,
        *,
        context_name: str = "",
        anchor_repo: str = "",
        anchor_pr_number: int | None = None,
        limit: int | None = None,
    ) -> tuple[PreviewRecord, ...]: ...


def require_deployment_read_store(record_store: object) -> DeploymentReadStore:
    read_record = getattr(record_store, "read_deployment_record", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support deployment record reads: "
            "read_deployment_record"
        )
    return cast(DeploymentReadStore, record_store)


def require_promotion_read_store(record_store: object) -> PromotionReadStore:
    read_record = getattr(record_store, "read_promotion_record", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support promotion record reads: "
            "read_promotion_record"
        )
    return cast(PromotionReadStore, record_store)


def require_environment_inventory_read_store(
    record_store: object,
) -> EnvironmentInventoryReadStore:
    read_record = getattr(record_store, "read_environment_inventory", None)
    if not callable(read_record):
        raise TypeError(
            "Launchplane record store does not support environment inventory reads: "
            "read_environment_inventory"
        )
    return cast(EnvironmentInventoryReadStore, record_store)


def require_recent_operations_read_store(record_store: object) -> RecentOperationsReadStore:
    required_methods = (
        "list_environment_inventory",
        "list_deployment_records",
        "list_promotion_records",
        "list_preview_records",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support recent operation reads: {missing_summary}"
        )
    return cast(RecentOperationsReadStore, record_store)


def require_secret_status_read_store(
    record_store: object,
) -> control_plane_secrets.SecretReadStore:
    required_methods = (
        "read_secret_record",
        "list_secret_records",
        "read_secret_version",
        "list_secret_versions",
        "list_secret_bindings",
        "list_secret_audit_events",
    )
    missing_methods = [
        method_name
        for method_name in required_methods
        if not callable(getattr(record_store, method_name, None))
    ]
    if missing_methods:
        missing_summary = ", ".join(missing_methods)
        raise TypeError(
            f"Launchplane record store does not support secret status reads: {missing_summary}"
        )
    return cast(control_plane_secrets.SecretReadStore, record_store)


def register_deployment_promotion_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def read_deployment_record(
        record_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> DeploymentRecordResponse:
        trace_id = dependencies.next_trace_id()
        try:
            deployment_store = require_deployment_read_store(record_store)
            deployment = deployment_store.read_deployment_record(record_id)
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
            action="deployment.read",
            product="launchplane",
            context=deployment.context,
            target=AuthorizationTarget(
                scope="instance",
                instances=(deployment.instance,),
            ),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read deployment records for the requested context.",
            )
        return DeploymentRecordResponse(trace_id=trace_id, record=deployment)

    def read_promotion_record(
        record_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> PromotionRecordResponse:
        trace_id = dependencies.next_trace_id()
        try:
            promotion_store = require_promotion_read_store(record_store)
            promotion = promotion_store.read_promotion_record(record_id)
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
            action="promotion.read",
            product="launchplane",
            context=promotion.context,
            target=AuthorizationTarget(
                scope="instance",
                instances=(promotion.from_instance, promotion.to_instance),
            ),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read promotion records for the requested context.",
            )
        return PromotionRecordResponse(trace_id=trace_id, record=promotion)

    app.add_api_route(
        "/v1/deployments/{record_id}",
        read_deployment_record,
        methods=["GET"],
        response_model=DeploymentRecordResponse,
        operation_id="read_deployment_record",
        summary="Read one deployment record",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
    app.add_api_route(
        "/v1/promotions/{record_id}",
        read_promotion_record,
        methods=["GET"],
        response_model=PromotionRecordResponse,
        operation_id="read_promotion_record",
        summary="Read one promotion record",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )


def register_inventory_operation_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def read_environment_inventory(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        instance: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> EnvironmentInventoryResponse:
        trace_id = dependencies.next_trace_id()
        if not dependencies.authorization_allows(
            identity=identity,
            action="inventory.read",
            product="launchplane",
            context=context,
            target=AuthorizationTarget(scope="instance", instances=(instance,)),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read inventory for the requested context.",
            )
        try:
            inventory_store = require_environment_inventory_read_store(record_store)
            inventory = inventory_store.read_environment_inventory(
                context_name=context,
                instance_name=instance,
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
            action="inventory.read",
            product="launchplane",
            context=inventory.context,
            target=AuthorizationTarget(
                scope="instance",
                instances=(inventory.instance,),
            ),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read inventory for the requested context.",
            )
        return EnvironmentInventoryResponse(trace_id=trace_id, record=inventory)

    def read_recent_operations(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> RecentOperationsResponse:
        trace_id = dependencies.next_trace_id()
        if not dependencies.authorization_allows(
            identity=identity,
            action="operations.read",
            product="launchplane",
            context=context,
            target=AuthorizationTarget(scope="context"),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read recent operations for the requested context.",
            )
        try:
            operations_store = require_recent_operations_read_store(record_store)
            inventory = tuple(
                record
                for record in operations_store.list_environment_inventory()
                if record.context == context
            )
            recent_deployments = operations_store.list_deployment_records(
                context_name=context,
                limit=10,
            )
            recent_promotions = operations_store.list_promotion_records(
                context_name=context,
                limit=10,
            )
            recent_previews = operations_store.list_preview_records(
                context_name=context,
                limit=10,
            )
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        stable_instances = tuple(
            sorted(
                {
                    *(record.instance for record in inventory),
                    *(record.instance for record in recent_deployments),
                    *(record.from_instance for record in recent_promotions),
                    *(record.to_instance for record in recent_promotions),
                }
            )
        )
        target = (
            AuthorizationTarget(scope="instance", instances=stable_instances)
            if stable_instances
            else AuthorizationTarget(scope="context")
        )
        if not dependencies.authorization_allows(
            identity=identity,
            action="operations.read",
            product="launchplane",
            context=context,
            target=target,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read recent operations for the requested context.",
            )
        return RecentOperationsResponse(
            trace_id=trace_id,
            context=context,
            storage_backend=storage_backend_name(record_store),
            inventory=inventory,
            recent_deployments=recent_deployments,
            recent_promotions=recent_promotions,
            recent_previews=recent_previews,
        )

    app.add_api_route(
        "/v1/inventory/{context}/{instance}",
        read_environment_inventory,
        methods=["GET"],
        response_model=EnvironmentInventoryResponse,
        operation_id="read_environment_inventory",
        summary="Read one environment inventory record",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
    app.add_api_route(
        "/v1/contexts/{context}/operations/recent",
        read_recent_operations,
        methods=["GET"],
        response_model=RecentOperationsResponse,
        operation_id="read_recent_operations",
        summary="Read recent Launchplane operations for a context",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )


def register_managed_secret_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def list_secret_statuses_for_context(
        *,
        context: str,
        instance: str,
        identity: LaunchplaneIdentity,
        record_store: object,
    ) -> SecretStatusListResponse:
        trace_id = dependencies.next_trace_id()
        if not dependencies.authorization_allows(
            identity=identity,
            action="secret.list",
            product=LAUNCHPLANE_SERVICE_CONTEXT,
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
                message=(
                    "Workflow cannot list Launchplane managed secret status for the requested "
                    "context."
                ),
            )
        try:
            secret_store = require_secret_status_read_store(record_store)
            statuses = tuple(
                SecretStatusReadModel.model_validate(status)
                for status in control_plane_secrets.list_secret_statuses(
                    secret_store,
                    context_name=context,
                    instance_name=instance,
                )
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
        return SecretStatusListResponse(
            trace_id=trace_id,
            context=context,
            instance=instance,
            secrets=statuses,
        )

    def list_context_secret_statuses(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> SecretStatusListResponse:
        return list_secret_statuses_for_context(
            context=context,
            instance="",
            identity=identity,
            record_store=record_store,
        )

    def list_instance_secret_statuses(
        context: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        instance: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> SecretStatusListResponse:
        return list_secret_statuses_for_context(
            context=context,
            instance=instance,
            identity=identity,
            record_store=record_store,
        )

    def read_secret_status(
        secret_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> SecretStatusResponse:
        trace_id = dependencies.next_trace_id()
        try:
            secret_store = require_secret_status_read_store(record_store)
            secret_status = SecretStatusReadModel.model_validate(
                control_plane_secrets.build_secret_status(
                    secret_store,
                    secret_id=secret_id,
                )
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
            action="secret.read",
            product=LAUNCHPLANE_SERVICE_CONTEXT,
            context=secret_status.context,
            target=AuthorizationTarget(
                scope="instance" if secret_status.instance else "context",
                instances=(secret_status.instance,) if secret_status.instance else (),
            ),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot read Launchplane managed secret status for the requested "
                    "context."
                ),
            )
        return SecretStatusResponse(trace_id=trace_id, secret=secret_status)

    app.add_api_route(
        "/v1/contexts/{context}/secrets",
        list_context_secret_statuses,
        methods=["GET"],
        response_model=SecretStatusListResponse,
        operation_id="list_context_secret_statuses",
        summary="List Launchplane managed secret status for a context",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
    app.add_api_route(
        "/v1/contexts/{context}/instances/{instance}/secrets",
        list_instance_secret_statuses,
        methods=["GET"],
        response_model=SecretStatusListResponse,
        operation_id="list_instance_secret_statuses",
        summary="List Launchplane managed secret status for an instance",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
    app.add_api_route(
        "/v1/secrets/{secret_id}",
        read_secret_status,
        methods=["GET"],
        response_model=SecretStatusResponse,
        operation_id="read_secret_status",
        summary="Read Launchplane managed secret status",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
