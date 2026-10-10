"""The browser and bounded admin helper share one scoped restart boundary."""

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Annotated

import click
from fastapi import Depends, Header

from control_plane.contracts.lane_service_restart import (
    SERVICE_RESTART_ROUTE,
    LaneServiceRestartRequest,
    LaneServiceRestartPlan,
    LaneServiceRestartResponse,
    LaneServiceRestartResult,
    restart_activity_scope,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    AuthorizationAllows,
    HttpErrorFactory,
)
from control_plane.drivers import native_routes
from control_plane.dokploy.api import redact_dokploy_log_line
from control_plane.lane_service_restart import (
    ServiceRestartAdapter,
    ServiceRestartRefused,
    plan_service_restart,
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
class ServiceRestartDependencies:
    read_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: AuthorizationAllows
    http_error: HttpErrorFactory
    control_plane_root: Path


def register_service_restart_route(
    app: ApiRouteRegistrar, *, dependencies: ServiceRestartDependencies
) -> None:
    async def restart_lane_service(
        payload: LaneServiceRestartRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> LaneServiceRestartResponse:
        trace = dependencies.next_trace_id()
        payload = payload.model_copy(update={"reason": redact_dokploy_log_line(payload.reason)})

        def refuse(code: str, message: str, status: int = 409) -> None:
            raise dependencies.http_error(
                status_code=status, trace_id=trace, code=code, message=message
            )

        operator = isinstance(identity, LocalOperatorIdentity | LocalAdminIdentity) or (
            isinstance(identity, GitHubHumanIdentity) and identity.role == "admin"
        )
        # Reuse the existing runtime-operation authority; no grant is added.
        if payload.mode != "dry-run":
            allowed = native_routes.native_driver_route_authorization_allows(
                endpoint=restart_lane_service,
                authorization_allows=dependencies.authorization_allows,
                identity=identity,
                product=payload.product,
                context=payload.context,
                instances=(payload.instance,),
            )
        else:
            allowed = dependencies.authorization_allows(
                identity=identity,
                action=native_routes.native_driver_route_alternate_authz_action(
                    restart_lane_service
                ),
                product=payload.product,
                context=payload.context,
                target=AuthorizationTarget(scope="instance", instances=(payload.instance,)),
            )
        if not operator or not allowed:
            refuse("authorization_denied", "Identity cannot restart this product lane.", 403)
        if not isinstance(record_store, PostgresRecordStore):
            refuse("database_required", "Service restart requires shared database storage.", 503)
        assert isinstance(record_store, PostgresRecordStore)
        actor = (
            f"github:{identity.github_id}:{identity.login}"
            if isinstance(identity, GitHubHumanIdentity)
            else f"{type(identity).__name__}:{identity.subject}"
            if isinstance(identity, LocalOperatorIdentity | LocalAdminIdentity)
            else ""
        )
        scope = restart_activity_scope(payload.product)
        fingerprint = hashlib.sha256(
            (actor + "\n" + payload.model_copy(update={"mode": "apply"}).model_dump_json()).encode()
        ).hexdigest()
        key = idempotency_key.strip()
        if payload.mode != "dry-run":
            if not key or len(key) > 200:
                refuse("idempotency_key_required", "Apply requires a bounded Idempotency-Key.", 400)
            original = record_store.read_idempotency_record(
                scope=scope, route_path=SERVICE_RESTART_ROUTE, idempotency_key=key
            )
            if payload.mode == "reconcile" and original is None:
                refuse(
                    "restart_receipt_unavailable",
                    "No original restart receipt exists; reconciliation cannot dispatch a restart.",
                    404,
                )
            if original is not None:
                if original.request_fingerprint != fingerprint:
                    refuse(
                        "idempotency_key_reused", "The key belongs to a different restart request."
                    )
                if original.state == "completed":
                    response = LaneServiceRestartResponse.model_validate(original.response_payload)
                    return response.model_copy(
                        update={
                            "trace_id": trace,
                            "original_trace_id": original.response_trace_id,
                            "replayed": True,
                        }
                    )

        def execute() -> LaneServiceRestartResponse:
            original = (
                record_store.read_idempotency_record(
                    scope=scope, route_path=SERVICE_RESTART_ROUTE, idempotency_key=key
                )
                if payload.mode != "dry-run"
                else None
            )
            if original is not None:
                if original.request_fingerprint != fingerprint:
                    refuse(
                        "idempotency_key_reused", "The key belongs to a different restart request."
                    )
                if original.state == "completed":
                    response = LaneServiceRestartResponse.model_validate(original.response_payload)
                    return response.model_copy(
                        update={
                            "trace_id": trace,
                            "original_trace_id": original.response_trace_id,
                            "replayed": True,
                        }
                    )
            recovering = original is not None
            if payload.mode == "reconcile" and not recovering:
                refuse(
                    "restart_receipt_unavailable",
                    "No original restart receipt exists; reconciliation cannot dispatch a restart.",
                    404,
                )
            try:
                with record_store.odoo_synchronous_lane_reservation(
                    product=payload.product, context=payload.context, instance=payload.instance
                ) as owner:
                    if owner is not None:
                        raise ServiceRestartRefused("A release operation holds this lane.")
                plan, expected, host, token, health_url = plan_service_restart(
                    store=record_store,
                    root=dependencies.control_plane_root,
                    request=payload,
                    actor=actor,
                )
            except FileNotFoundError:
                if recovering:
                    refuse(
                        "mutation_reconciliation_required",
                        "Original restart is unsettled; its identity is temporarily unavailable. Preserve the original request.",
                    )
                refuse(
                    "restart_identity_unavailable", "Recorded restart identity is unavailable.", 404
                )
                raise AssertionError
            except (ValueError, click.ClickException, OSError):
                if recovering:
                    refuse(
                        "mutation_reconciliation_required",
                        "Original restart is unsettled; the lane or identity is temporarily unavailable. Preserve the original request.",
                    )
                refuse(
                    "restart_refused",
                    "Restart identity is ambiguous, unsupported, or the lane is held by a release.",
                )
                raise AssertionError
            if payload.mode == "dry-run":
                return LaneServiceRestartResponse(
                    trace_id=trace,
                    result=LaneServiceRestartResult(
                        status="ready", plan=plan, plan_sha256=plan.digest()
                    ),
                )
            original = record_store.read_idempotency_record(
                scope=scope, route_path=SERVICE_RESTART_ROUTE, idempotency_key=key
            )
            if original is not None and original.request_fingerprint != fingerprint:
                refuse("idempotency_key_reused", "The key belongs to a different restart request.")
            if original is not None and original.state == "completed":
                response = LaneServiceRestartResponse.model_validate(original.response_payload)
                return response.model_copy(
                    update={
                        "trace_id": trace,
                        "replayed": True,
                        "original_trace_id": original.response_trace_id,
                    }
                )
            recovering = original is not None and original.state != "completed"
            if recovering and original is not None:
                plan = LaneServiceRestartPlan.model_validate_json(original.reconciliation_key)
            if plan.digest() != payload.reviewed_plan_sha256:
                refuse(
                    "restart_identity_changed",
                    "Service identity changed. Inspect and review again.",
                )
            adapter = ServiceRestartAdapter(
                store=record_store,
                root=dependencies.control_plane_root,
                request=payload,
                plan=plan,
                expected=expected,
                host=host,
                token=token,
                health_url=health_url,
                trace_id=trace,
            )
            try:
                result = run_durable_provider_operation(
                    store=record_store,
                    scope=scope,
                    route_path=SERVICE_RESTART_ROUTE,
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                    lease_owner=trace,
                    response_trace_id=trace,
                    adapter=adapter,
                    allow_mutation=not recovering and payload.mode == "apply",
                )
            except ServiceRestartRefused:
                refuse(
                    "restart_refused",
                    "Service identity or release ownership changed before restart.",
                )
                raise AssertionError
            if result.status in {"completed", "replayed", "adopted"}:
                response = LaneServiceRestartResponse.model_validate(result.response_payload)
                return response.model_copy(
                    update={"trace_id": trace, "replayed": result.status == "replayed"}
                )
            if result.status == "conflict":
                refuse("idempotency_key_reused", "The key belongs to a different restart request.")
            refuse(
                "mutation_reconciliation_required",
                "Restart has not settled. Inspect its activity; do not submit another restart.",
            )
            raise AssertionError

        # Keep the provider runner alive through an HTTP disconnect. Its own
        # durable effect checkpoint prevents repeating an unknown POST.
        task = asyncio.create_task(asyncio.to_thread(execute))
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            while not task.done():
                try:
                    await asyncio.shield(asyncio.wait({task}))
                except asyncio.CancelledError:
                    continue
            if not task.cancelled():
                task.exception()
            raise

    app.add_api_route(
        SERVICE_RESTART_ROUTE,
        restart_lane_service,
        methods=["POST"],
        response_model=LaneServiceRestartResponse,
        response_model_exclude_none=True,
        operation_id="restart_lane_service",
        summary="Restart one lane service on its current artifact",
    )
