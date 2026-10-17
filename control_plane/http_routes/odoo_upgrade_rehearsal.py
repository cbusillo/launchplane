"""Operator access to an independent Odoo upgrade rehearsal."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, NoReturn

import click
from fastapi import Depends, Header, Path as PathParameter

from control_plane.contracts.odoo_upgrade_rehearsal import (
    ODOO_UPGRADE_REHEARSAL_ROUTE as ROUTE,
    OdooUpgradeRehearsalRequest,
    rehearsal_identity,
)
from control_plane.durable_operation_authorization import DurableOperationAuthorizationDeniedError
from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    idempotency_scope,
    request_fingerprint,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
    run_in_worker_thread,
)
from control_plane.odoo_upgrade_rehearsal import (
    OdooRehearsalAdapter,
    RehearsalRefused,
    rehearsal_inputs,
    require_rehearsal_transition,
    require_unaliased_rehearsal,
)
from control_plane.service_auth import LaunchplaneIdentity, director_agent_identity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.dokploy import api


def register_odoo_upgrade_rehearsal_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    root: Path,
    run_provider_mutation: Callable[..., Awaitable[AcceptedEvidenceResponse]],
) -> None:
    async def rehearsal(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^\S+$")],
        body: OdooUpgradeRehearsalRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()

        def refuse(status: int, code: str) -> NoReturn:
            raise dependencies.http_error(
                status_code=status,
                trace_id=trace,
                code=code,
                message="The isolated rehearsal prerequisite is unverified. Preserve its original request and custody.",
            )

        if body.mode == "apply":
            if not director_agent_identity(identity):
                refuse(403, "authorization_denied")
            if not idempotency_key.strip() or not body.reviewed_plan_sha256:
                refuse(400, "review_required")
        elif not dependencies.authorization_allows(
            identity=identity,
            action="operations.read",
            product=product,
            context=body.source_context,
        ):
            refuse(403, "authorization_denied")
        if not isinstance(record_store, PostgresRecordStore):
            refuse(503, "database_storage_required")
        assert isinstance(record_store, PostgresRecordStore)
        if body.mode == "apply" and record_store.database_dialect_name != "postgresql":
            refuse(503, "postgresql_required")
        principal = idempotency_scope(identity)
        if not body.rehearsal_id:
            if body.action != "prepare":
                refuse(400, "rehearsal_identity_required")
            try:
                identifier = rehearsal_identity(product, principal, idempotency_key)
            except ValueError:
                refuse(400, "idempotency_key_required")
            body = body.model_copy(update={"rehearsal_id": identifier})
        previous = record_store.read_odoo_upgrade_rehearsal(body.rehearsal_id)
        if previous is not None:
            if previous.product != product:
                refuse(404, "not_found")
            if body.mode == "apply" and previous.principal != principal:
                refuse(403, "authorization_denied")
            principal = previous.principal
        adapter = OdooRehearsalAdapter(root, record_store, product, body, principal, trace)
        try:
            if body.mode == "apply":
                return await run_provider_mutation(
                    record_store=record_store,
                    identity=identity,
                    route_path=ROUTE.format(product=product),
                    idempotency_key=idempotency_key,
                    request_fingerprint=request_fingerprint(
                        {"product": product, **body.model_dump(mode="json")}
                    ),
                    trace_id=trace,
                    adapter=adapter,
                    in_progress_message="This rehearsal retains the original operation; do not replay an uncertain effect.",
                    reconcile_message="The rehearsal outcome is uncertain. Observe the original operation before continuing.",
                )
            if body.mode == "readback":
                if previous is None:
                    refuse(404, "not_found")
                assert previous is not None
                record = previous
                require_unaliased_rehearsal(record_store, record)
                result = await run_in_worker_thread(
                    lambda: adapter.host(record, "observe", lambda: None)
                )
                result = {
                    **adapter.public(record),
                    "observed_state": result["state"],
                    "copies": result["copies"],
                    "completed": result["completed"],
                    "raw_source_retained": result["raw_source_retained"],
                }
            else:
                record, _, _ = await run_in_worker_thread(
                    lambda: rehearsal_inputs(root, record_store, product, body, principal)
                )
                require_unaliased_rehearsal(record_store, record)
                require_rehearsal_transition(record, body.action)
                if previous is None:
                    await run_in_worker_thread(
                        lambda: record_store.compare_and_write_odoo_upgrade_rehearsal(None, record)
                    )
                result = {
                    **adapter.public(record),
                    "plan_sha256": record.binding_sha256,
                    "action": body.action,
                    "applied": False,
                }
            return accepted_evidence_response(
                trace_id=trace, records={"rehearsal": body.rehearsal_id}, result=result
            )
        except RehearsalRefused as error:
            refuse(409, error.code)
        except DurableOperationAuthorizationDeniedError:
            refuse(403, "authorization_denied")
        except api.DokployRequestFailed as error:
            refuse(
                403 if error.status_code in {401, 403} else 503,
                "authorization_denied"
                if error.status_code in {401, 403}
                else "rehearsal_provider_unavailable",
            )
        except (ValueError, FileNotFoundError, RuntimeError, OSError, click.ClickException):
            refuse(409, "rehearsal_state_unverified")

    app.add_api_route(
        ROUTE, rehearsal, methods=["POST"], response_model=AcceptedEvidenceResponse, status_code=202
    )
