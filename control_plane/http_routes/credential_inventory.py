from collections.abc import Callable
from datetime import datetime, timezone
from typing import Annotated

from fastapi import Depends, Response
from pydantic import BaseModel

from control_plane.credential_inventory import (
    INVENTORY_ROUTE,
    CredentialCollection,
    CredentialInventoryView,
    inventory_receipt,
    inventory_view,
)
from control_plane.durable_operation_authorization import read_active_authz_policy_record
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    director_agent_identity,
)
from control_plane.storage.postgres import PostgresRecordStore


class CredentialInventoryResponse(BaseModel):
    status: str = "ok"
    trace_id: str
    inventory: CredentialInventoryView


class CredentialInventoryAccepted(BaseModel):
    status: str = "ok"
    trace_id: str
    receipt_id: str


def register_credential_inventory_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
) -> None:
    def require_inventory_access(
        identity: LaunchplaneIdentity, record_store: object, trace_id: str
    ) -> PostgresRecordStore:
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Credential inventory requires shared database storage.",
            )
        human_admin = False
        if isinstance(identity, GitHubHumanIdentity):
            try:
                human_admin = read_active_authz_policy_record(
                    record_store
                ).policy.names_administrator(identity)
            except (LookupError, TypeError):
                raise dependencies.http_error(
                    status_code=503,
                    trace_id=trace_id,
                    code="authorization_provenance_unavailable",
                    message="Administrator policy is unavailable.",
                ) from None
        if not (
            isinstance(identity, LocalAdminIdentity)
            or human_admin
            or director_agent_identity(identity)
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Credential inventory is available only to the administrator and Director agents.",
            )
        return record_store

    def read_inventory(
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> CredentialInventoryResponse:
        trace_id = dependencies.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        store = require_inventory_access(identity, record_store, trace_id)
        return CredentialInventoryResponse(
            trace_id=trace_id,
            inventory=inventory_view(
                store.read_latest_credential_inventory(),
                store.read_latest_credential_inventory(successful=True),
                now=datetime.now(timezone.utc),
            ),
        )

    def import_inventory(
        body: CredentialCollection,
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> CredentialInventoryAccepted:
        trace_id = dependencies.next_trace_id()
        response.headers["Cache-Control"] = "no-store"
        store = require_inventory_access(identity, record_store, trace_id)
        try:
            receipt = inventory_receipt(body, now=datetime.now(timezone.utc), trace_id=trace_id)
        except ValueError:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Credential collection metadata is invalid.",
            ) from None
        store.write_idempotency_record_once(receipt)
        return CredentialInventoryAccepted(trace_id=trace_id, receipt_id=receipt.record_id)

    errors = {code: {"model": dependencies.error_response_model} for code in (400, 401, 403, 503)}
    app.add_api_route(
        INVENTORY_ROUTE,
        read_inventory,
        methods=["GET"],
        response_model=CredentialInventoryResponse,
        operation_id="read_credential_inventory",
        summary="Read administrator credential metadata and drift",
        responses=errors,
    )
    app.add_api_route(
        INVENTORY_ROUTE,
        import_inventory,
        methods=["POST"],
        response_model=CredentialInventoryAccepted,
        operation_id="import_credential_inventory",
        summary="Accept metadata-only credential collection evidence",
        responses=errors,
    )
