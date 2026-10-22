"""The supported opaque testing account/browser API, using existing identities."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, Header
from pydantic import BaseModel

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_walkthrough import (
    WALKTHROUGH_ROUTE,
    WALKTHROUGH_RECONCILIATION_REQUIRED,
    OdooWalkthroughRequest,
    OdooWalkthroughResponse,
    OdooWalkthroughResult,
    walkthrough_activity_scope,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    AuthorizationAllows,
    HttpErrorFactory,
    run_in_worker_thread,
)
from control_plane.drivers import native_routes
from control_plane.odoo_walkthrough import (
    WalkthroughAdapter,
    WalkthroughPrerequisiteError,
    InterruptedWalkthroughAdapter,
    plan_walkthrough,
)
from control_plane.provider_operations import run_durable_provider_operation
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
)
from control_plane.storage.postgres import PostgresRecordStore


@dataclass(frozen=True)
class WalkthroughDependencies:
    read_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: AuthorizationAllows
    http_error: HttpErrorFactory
    control_plane_root: Path
    error_response_model: type[BaseModel]


def walkthrough_actor(identity: LaunchplaneIdentity) -> str:
    if isinstance(identity, GitHubHumanIdentity):
        return f"github:{identity.github_id}"
    if isinstance(identity, LocalOperatorIdentity | LocalAdminIdentity):
        return f"{type(identity).__name__}:{identity.subject}"
    return ""


def walkthrough_fingerprint(actor: str, request: OdooWalkthroughRequest) -> str:
    return canonical_json_sha256([actor, request.model_dump(mode="json")])


def register_walkthrough_route(
    app: ApiRouteRegistrar, *, dependencies: WalkthroughDependencies
) -> None:
    async def odoo_testing_walkthrough(
        payload: OdooWalkthroughRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> OdooWalkthroughResponse:
        trace = dependencies.next_trace_id()

        def refuse(code: str, message: str, status: int = 409) -> None:
            raise dependencies.http_error(
                status_code=status, trace_id=trace, code=code, message=message
            )

        operator = isinstance(identity, LocalOperatorIdentity | LocalAdminIdentity) or (
            isinstance(identity, GitHubHumanIdentity) and identity.role == "admin"
        )
        if payload.mode == "apply":
            allowed = native_routes.native_driver_route_authorization_allows(
                endpoint=odoo_testing_walkthrough,
                authorization_allows=dependencies.authorization_allows,
                identity=identity,
                product=payload.product,
                context=payload.context,
                instances=("testing",),
            )
        else:
            allowed = dependencies.authorization_allows(
                identity=identity,
                action=native_routes.native_driver_route_alternate_authz_action(
                    odoo_testing_walkthrough
                ),
                product=payload.product,
                context=payload.context,
                target=AuthorizationTarget(scope="instance", instances=("testing",)),
            )
        if not operator or not allowed:
            refuse("authorization_denied", "Identity cannot operate this testing task.", 403)
        if not isinstance(record_store, PostgresRecordStore):
            refuse("database_required", "Testing tasks require shared database storage.", 503)
        assert isinstance(record_store, PostgresRecordStore)
        actor = walkthrough_actor(identity)
        key = idempotency_key.strip()
        if payload.mode == "apply" and (not key or len(key) > 200):
            refuse("idempotency_key_required", "Apply requires a bounded Idempotency-Key.", 400)
        scope = walkthrough_activity_scope(payload.product)
        fingerprint = walkthrough_fingerprint(actor, payload)

        def execute() -> OdooWalkthroughResponse:
            if payload.mode == "apply":
                original = record_store.read_idempotency_record(
                    scope=scope,
                    route_path=WALKTHROUGH_ROUTE,
                    idempotency_key=key,
                )
                if original is not None:
                    if original.request_fingerprint != fingerprint:
                        refuse(
                            "idempotency_key_reused", "This key belongs to another task request."
                        )
                    if original.state == "completed":
                        return OdooWalkthroughResponse.model_validate(original.response_payload)
                    try:
                        recovery = run_durable_provider_operation(
                            store=record_store,
                            scope=scope,
                            route_path=WALKTHROUGH_ROUTE,
                            idempotency_key=key,
                            request_fingerprint=fingerprint,
                            lease_owner=trace,
                            response_trace_id=trace,
                            adapter=InterruptedWalkthroughAdapter(
                                original,
                                payload,
                                trace,
                                record_store,
                                dependencies.control_plane_root,
                            ),
                            allow_mutation=False,
                        )
                    except ValueError:
                        refuse(
                            WALKTHROUGH_RECONCILIATION_REQUIRED,
                            "The original dispatch evidence is unavailable; preserve its key and task account.",
                        )
                        raise AssertionError
                    if recovery.status in {"completed", "replayed", "adopted"}:
                        return OdooWalkthroughResponse.model_validate(recovery.response_payload)
                    refuse(
                        WALKTHROUGH_RECONCILIATION_REQUIRED,
                        "The original dispatch is still active or unobservable; preserve its key and task account.",
                    )
            with record_store.odoo_synchronous_lane_reservation(
                product=payload.product,
                context=payload.context,
                instance="testing",
            ) as owner:
                if owner is not None:
                    refuse("testing_lane_busy", "Another operation holds this testing lane.")
                try:
                    plan = plan_walkthrough(
                        record_store, dependencies.control_plane_root, payload, actor
                    )
                except WalkthroughPrerequisiteError as error:
                    refuse(error.code.value, "The testing task prerequisite did not pass.")
                    raise AssertionError
                except Exception:
                    refuse(
                        "testing_task_refused",
                        "Testing identity, isolation, use or custody is unavailable.",
                    )
                    raise AssertionError
                if payload.mode == "dry-run":
                    return OdooWalkthroughResponse(
                        trace_id=trace,
                        result=OdooWalkthroughResult(
                            status="ready",
                            task_id=payload.task_id,
                            operation=payload.operation,
                            plan_sha256=plan.digest,
                            product=payload.product,
                            context=payload.context,
                            deployment_record_id=plan.connection.selection.identity.deployment_record_id,
                            artifact_id=plan.connection.selection.identity.artifact_id,
                        ),
                    )
                if plan.digest != payload.reviewed_plan_sha256:
                    refuse(
                        "testing_task_plan_changed", "Review the current testing task plan again."
                    )
                adapter = WalkthroughAdapter(
                    store=record_store,
                    root=dependencies.control_plane_root,
                    request=payload,
                    actor=actor,
                    plan=plan,
                    trace=trace,
                )
                try:
                    result = run_durable_provider_operation(
                        store=record_store,
                        scope=scope,
                        route_path=WALKTHROUGH_ROUTE,
                        idempotency_key=key,
                        request_fingerprint=fingerprint,
                        lease_owner=trace,
                        response_trace_id=trace,
                        adapter=adapter,
                        original_intent=adapter.original_intent(),
                    )
                except ValueError:
                    refuse(
                        "testing_task_plan_changed", "Review the current testing task plan again."
                    )
                    raise AssertionError
                if result.status in {"completed", "replayed", "adopted"}:
                    response = OdooWalkthroughResponse.model_validate(result.response_payload)
                    if result.status == "completed":
                        response.result.screenshots = adapter.transient_screenshots
                    return response
                refuse(
                    WALKTHROUGH_RECONCILIATION_REQUIRED,
                    "Retry only this original key; then clean up its task account.",
                )
                raise AssertionError

        return await run_in_worker_thread(execute)

    native_routes.bind_native_fastapi_driver_handler(
        route_path=WALKTHROUGH_ROUTE,
        endpoint=odoo_testing_walkthrough,
        declared_methods=("POST",),
    )
    app.add_api_route(
        WALKTHROUGH_ROUTE,
        odoo_testing_walkthrough,
        methods=["POST"],
        response_model=OdooWalkthroughResponse,
        operation_id="odoo_testing_walkthrough",
        summary="Provision, run or clean up an opaque Odoo testing task",
        responses={
            status: {"model": dependencies.error_response_model}
            for status in (400, 401, 403, 409, 503)
        },
    )
