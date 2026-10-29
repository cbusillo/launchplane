"""Create independent testing storage without releasing a retained interlock."""

from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Header, Path as PathParameter
import click

from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    request_fingerprint,
    idempotency_scope,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
    run_in_worker_thread,
)
from control_plane.service_auth import LaunchplaneIdentity, director_agent_identity
from control_plane.provider_operations import build_provider_operation_key
from control_plane.runtime_platform_credentials import PlatformCredentialRefusedError
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_separation_resources import (
    ROUTE,
    SeparationResourcesAdapter,
    SeparationResourcesRefused,
    SeparationResourcesRequest,
    parse_preparation_receipt,
    SeparationResourcesStatus,
    SeparationResourcesInspection,
)


def register_testing_separation_resource_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
    actor: Callable[[LaunchplaneIdentity], str],
    root: Path,
    run_provider_mutation: Callable[..., Awaitable[AcceptedEvidenceResponse]],
) -> None:
    async def prepare(
        product: Annotated[str, PathParameter(min_length=1, pattern=r"^\S+$")],
        body: SeparationResourcesRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace = dependencies.next_trace_id()
        if not director_agent_identity(identity):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Independent testing preparation requires the Director agent.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Independent preparation requires database storage.",
            )
        adapter = SeparationResourcesAdapter(
            root, record_store, product, body, actor(identity), trace
        )
        try:
            if body.mode in {"receipt", "inspect"}:
                if not idempotency_key.strip():
                    raise ValueError("Readback requires the original preparation key.")
                row = record_store.read_idempotency_record(
                    scope=idempotency_scope(identity),
                    route_path=ROUTE.replace("{product}", product),
                    idempotency_key=idempotency_key.strip(),
                )
                if row is None or row.state != "completed":
                    if row is not None and row.provider_target_key != adapter.target_key():
                        raise ValueError("The original preparation belongs to another lane.")
                    observed = SeparationResourcesStatus(
                        product=product,
                        context=body.context,
                        state=row.state if row else "not_found",
                        plan_sha256=row.reconciliation_key if row else "",
                        provider_effect_phase=row.provider_effect_phase if row else "",
                        created_at=row.created_at if row else "",
                        updated_at=row.updated_at if row else "",
                        lease_expires_at=row.lease_expires_at if row else "",
                        provider_effect_started_at=row.provider_effect_started_at if row else "",
                    )
                    if body.mode == "inspect" and row is not None:
                        original = body.model_copy(
                            update={"mode": "apply", "reviewed_plan_sha256": row.reconciliation_key}
                        )
                        if (
                            request_fingerprint(
                                {"product": product, **original.model_dump(mode="json")}
                            )
                            != row.request_fingerprint
                        ):
                            raise ValueError(
                                "Inspection requires the original preparation request."
                            )
                        adapter.request = original
                        operation_key = build_provider_operation_key(
                            scope=row.scope,
                            route_path=row.route_path,
                            idempotency_key=row.idempotency_key,
                            request_fingerprint=row.request_fingerprint,
                            reconciliation_key=row.reconciliation_key,
                        )
                        native = await run_in_worker_thread(
                            lambda: adapter.inspect_original(operation_key)
                        )
                        observed = SeparationResourcesInspection.model_validate(
                            {
                                **observed.model_dump(mode="json"),
                                **native,
                                "inspected_at": datetime.now(timezone.utc),
                            }
                        )
                    return accepted_evidence_response(
                        trace_id=trace,
                        records={},
                        result=observed.model_dump(mode="json"),
                    )
                result = parse_preparation_receipt(row.response_payload.get("result"))
                if (result.product, result.context) != (product, body.context):
                    raise ValueError("The original preparation belongs to another lane.")
                return accepted_evidence_response(
                    trace_id=trace,
                    records={"preparation": row.record_id},
                    result=result.model_dump(mode="json"),
                )
            if body.mode == "dry-run":
                return accepted_evidence_response(
                    trace_id=trace, records={}, result=await run_in_worker_thread(adapter.plan)
                )
            if record_store.database_dialect_name != "postgresql":
                raise dependencies.http_error(
                    status_code=503,
                    trace_id=trace,
                    code="postgresql_required",
                    message="Independent preparation requires PostgreSQL execution guards.",
                )
            if not idempotency_key.strip() or not body.reviewed_plan_sha256:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace,
                    code="review_required",
                    message="Apply requires saved review evidence and its original key.",
                )
            return await run_provider_mutation(
                record_store=record_store,
                identity=identity,
                route_path=ROUTE.replace("{product}", product),
                idempotency_key=idempotency_key.strip(),
                request_fingerprint=request_fingerprint(
                    {"product": product, **body.model_dump(mode="json")}
                ),
                trace_id=trace,
                adapter=adapter,
                in_progress_message="Independent testing storage preparation is in progress.",
                reconcile_message="Preparation outcome is unknown; retain its original key and both fences.",
            )
        except (
            PlatformCredentialRefusedError,
            ValueError,
            FileNotFoundError,
            SeparationResourcesRefused,
        ) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="testing_resources_refused",
                message="Retained testing preparation inputs changed or remain unproved.",
            ) from error
        except click.ClickException as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="testing_resources_unavailable",
                message="Testing preparation provider evidence is unavailable.",
            ) from error

    app.add_api_route(
        ROUTE,
        prepare,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="prepare_independent_testing_storage",
        summary="Prepare independent empty testing storage while preserving the retained lane",
        responses={
            code: {"model": dependencies.error_response_model}
            for code in (400, 401, 403, 404, 409, 503)
        },
    )
