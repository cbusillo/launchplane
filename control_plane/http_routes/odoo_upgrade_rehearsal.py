"""Operator access to an independent Odoo upgrade rehearsal."""

from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Annotated, NoReturn

import click
from fastapi import Depends, Header, Path as PathParameter, Query

from control_plane.contracts.odoo_upgrade_rehearsal import (
    ODOO_UPGRADE_REHEARSAL_ROUTE as ROUTE,
    ODOO_UPGRADE_REHEARSAL_PREFLIGHT_ROUTE,
    REHEARSAL_PUBLIC_FAILURE_MESSAGES,
    OdooUpgradeRehearsalRequest,
    RehearsalPrerequisite,
    rehearsal_identity,
)
from control_plane.contracts.canonical_json import canonical_json_sha256
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
from control_plane.odoo_rehearsal_preflight import rehearsal_preflight


def register_odoo_upgrade_rehearsal_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    root: Path,
    run_provider_mutation: Callable[..., Awaitable[AcceptedEvidenceResponse]],
) -> None:
    async def preflight(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^\S+$")],
        source_context: Annotated[str, Query(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()

        def refuse(status: int, code: str) -> NoReturn:
            raise dependencies.http_error(
                status_code=status,
                trace_id=trace,
                code=code,
                message="The read-only rehearsal preflight is unavailable. No rehearsal was prepared.",
            )

        if not dependencies.authorization_allows(
            identity=identity, action="operations.read", product=product, context=source_context
        ):
            refuse(403, "authorization_denied")
        if not isinstance(record_store, PostgresRecordStore):
            refuse(503, "database_storage_required")
        try:
            result = await run_in_worker_thread(
                lambda: rehearsal_preflight(root, record_store, product, source_context)
            )
            return accepted_evidence_response(
                trace_id=trace, records={"product": product}, result=result.model_dump(mode="json")
            )
        except DurableOperationAuthorizationDeniedError:
            refuse(403, "authorization_denied")
        except api.DokployRequestFailed as error:
            refuse(
                403 if error.status_code in {401, 403} else 503,
                "authorization_denied"
                if error.status_code in {401, 403}
                else "rehearsal_provider_unavailable",
            )
        except RehearsalRefused as error:
            refuse(409, error.code)
        except (ValueError, RuntimeError, OSError, click.ClickException):
            refuse(409, "rehearsal_preflight_unavailable")

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
            if canonical_json_sha256(body.intent()) != previous.intent_sha256:
                refuse(409, "rehearsal_state_unverified")
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
                    public_failure_messages=REHEARSAL_PUBLIC_FAILURE_MESSAGES,
                )
            if body.mode == "readback":
                if previous is None:
                    refuse(404, "not_found")
                assert previous is not None
                record = previous
                require_unaliased_rehearsal(record_store, record)

                def observe() -> dict[str, object]:
                    with record_store.testing_replacement_execution_lock(
                        record.rehearsal_id
                    ) as check:
                        if check is None:
                            raise RehearsalRefused(RehearsalPrerequisite.RECOVERY_REQUIRED)
                        fresh = record_store.read_odoo_upgrade_rehearsal(record.rehearsal_id)
                        if fresh != record:
                            raise RehearsalRefused(RehearsalPrerequisite.STATE_UNVERIFIED)
                        if body.action == "prepare" and record.state == "planned":
                            try:
                                journal = adapter.prepare_readback(record, idempotency_key)
                            except RehearsalRefused as error:
                                if not record.compose_id or error.code not in {
                                    RehearsalPrerequisite.STATE_UNVERIFIED,
                                    RehearsalPrerequisite.OPERATION_UNAVAILABLE,
                                }:
                                    raise
                                return {
                                    **adapter.host(record, "observe", check),
                                    "original_key_required": True,
                                }
                            if not record.compose_id:
                                return journal
                            observation = adapter.host(record, "observe", check)
                            if journal[
                                "operation_state"
                            ] == "reconcile_required" and adapter.can_continue_prepare_host(
                                record, observation, str(journal["provider_effect_phase"])
                            ):
                                journal.update(
                                    {
                                        "recovery_disposition": "continue_original_prepare",
                                        "recovery_reason": "prepare_host_not_started",
                                    }
                                )
                            return {
                                **observation,
                                **{
                                    key: journal[key]
                                    for key in (
                                        "operation_state",
                                        "provider_effect_phase",
                                        "provider_effect_started_at",
                                        "recovery_disposition",
                                        "recovery_reason",
                                    )
                                },
                            }
                        return adapter.native_readback(record, idempotency_key, check)

                result = await run_in_worker_thread(observe)
                public_record = adapter.public(record)
                if result.get("host_observation_unavailable"):
                    public_record.pop("copies")
                result = {
                    **public_record,
                    **{
                        key: result[key]
                        for key in (
                            "operation_state",
                            "provider_effect_phase",
                            "provider_effect_started_at",
                            "recovery_disposition",
                            "recovery_reason",
                            "original_key_required",
                            "operation_journal_status",
                            "host_observation_unavailable",
                        )
                        if key in result
                    },
                    **({"observed_state": result["state"]} if "state" in result else {}),
                    **({"copies": result["copies"]} if "copies" in result else {}),
                    "completed": result["completed"],
                    **(
                        {"raw_source_retained": result["raw_source_retained"]}
                        if "raw_source_retained" in result
                        else {}
                    ),
                    "prepare_not_started": result.get("prepare_not_started", False),
                    **({"custody": result["custody"]} if result.get("custody") else {}),
                    **(
                        {"failure_events_unavailable": True}
                        if result.get("failure_events_unavailable")
                        else {}
                    ),
                    **(
                        {
                            "failure_events_after_checkpoint": result[
                                "failure_events_after_checkpoint"
                            ]
                        }
                        if "failure_events_after_checkpoint" in result
                        else {}
                    ),
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
            if error.prerequisite is not None:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace,
                    code=error.code,
                    message="No rehearsal action started. Prerequisite: "
                    + error.prerequisite.value,
                )
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
    app.add_api_route(
        ODOO_UPGRADE_REHEARSAL_PREFLIGHT_ROUTE,
        preflight,
        methods=["GET"],
        response_model=AcceptedEvidenceResponse,
    )
