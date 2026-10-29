import asyncio
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
from pathlib import Path
import re
from typing import Annotated

from fastapi import Depends, Header
from pydantic import BaseModel

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.retained_volume import RetainedVolumeBinding
from control_plane.contracts.retained_volume_sweep import (
    RETAINED_VOLUME_SWEEP_ROUTE,
    RetainedVolumeSweepPlan,
    RetainedVolumeSweepRequest,
    RetainedVolumeSweepResponse,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    AuthorizationAllows,
    HttpErrorFactory,
)
from control_plane.provider_operations import run_durable_provider_operation
from control_plane.retained_volume_sweep_execution import (
    RetainedVolumeSweepAdapter,
    build_retained_volume_sweep,
)
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    director_agent_identity,
    launchplane_identity_actor,
)
from control_plane.storage.postgres import PostgresRecordStore


@dataclass(frozen=True)
class RetainedVolumeSweepDependencies:
    read_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: AuthorizationAllows
    http_error: HttpErrorFactory
    control_plane_root: Path
    error_response_model: type[BaseModel]


def register_retained_volume_sweep_route(
    app: ApiRouteRegistrar, *, dependencies: RetainedVolumeSweepDependencies
) -> None:
    async def retained_volume_sweep(
        payload: RetainedVolumeSweepRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> RetainedVolumeSweepResponse:
        trace_id = dependencies.next_trace_id()

        def refuse(code: str, message: str, status: int = 409) -> None:
            raise dependencies.http_error(
                status_code=status, trace_id=trace_id, code=code, message=message
            )

        administrator = isinstance(identity, LocalAdminIdentity) or (
            isinstance(identity, GitHubHumanIdentity) and identity.role == "admin"
        )
        director = director_agent_identity(identity)
        if not administrator and not director:
            refuse("authorization_denied", "Identity cannot sweep retained volumes.", 403)
        if not isinstance(record_store, PostgresRecordStore):
            refuse(
                "database_required", "Retained volume sweep requires shared database storage.", 503
            )
        assert isinstance(record_store, PostgresRecordStore)
        store: PostgresRecordStore = record_store
        key = idempotency_key.strip()
        actor = launchplane_identity_actor(identity)
        normalized = payload.model_copy(update={"mode": "apply"})
        fingerprint = hashlib.sha256(
            (actor + "\n" + normalized.model_dump_json()).encode()
        ).hexdigest()
        scope = f"retained-volume-sweep:{payload.retirement_record_id}"
        mutating = payload.mode in {"apply", "readback"}
        if mutating and (not key or len(key) > 200):
            refuse(
                "idempotency_key_required",
                "Apply and readback require a bounded Idempotency-Key.",
                400,
            )
        if mutating and not re.fullmatch(r"[0-9a-f]{64}", payload.reviewed_plan_sha256):
            refuse(
                "reviewed_plan_required",
                "Apply and readback require the original reviewed plan digest.",
                400,
            )

        def read_original() -> LaunchplaneIdempotencyRecord | None:
            return store.read_idempotency_record(
                scope=scope,
                route_path=RETAINED_VOLUME_SWEEP_ROUTE,
                idempotency_key=key,
            )

        original = read_original() if mutating else None
        if original is not None and original.request_fingerprint != fingerprint:
            refuse("idempotency_key_reused", "The key belongs to a different sweep request.")
        if original is not None and original.state == "completed":
            return RetainedVolumeSweepResponse.model_validate(original.response_payload).model_copy(
                update={"trace_id": trace_id, "replayed": True}
            )
        if payload.mode == "readback" and original is None:
            refuse("sweep_receipt_unavailable", "No original sweep receipt exists.", 404)

        def execute() -> RetainedVolumeSweepResponse:
            try:
                if original is not None:
                    private = json.loads(original.reconciliation_key)
                    plan = RetainedVolumeSweepPlan.model_validate(private["plan"])
                    bindings = tuple(
                        RetainedVolumeBinding.model_validate(value) for value in private["bindings"]
                    )
                else:
                    plan, bindings = build_retained_volume_sweep(
                        store, dependencies.control_plane_root, payload
                    )
                if payload.mode == "dry-run":
                    return RetainedVolumeSweepResponse(
                        status="accepted",
                        trace_id=trace_id,
                        records={},
                        plan_sha256=plan.digest(),
                        items=plan.items,
                    )
                if payload.mode == "apply" and plan.digest() != payload.reviewed_plan_sha256:
                    refuse(
                        "retained_volume_plan_changed",
                        "The retained volume plan changed. Review it again.",
                    )
                adapter = RetainedVolumeSweepAdapter(
                    store,
                    dependencies.control_plane_root,
                    payload,
                    plan,
                    bindings,
                    trace_id,
                )
                result = run_durable_provider_operation(
                    store=store,
                    scope=scope,
                    route_path=RETAINED_VOLUME_SWEEP_ROUTE,
                    idempotency_key=key,
                    request_fingerprint=fingerprint,
                    lease_owner=trace_id,
                    response_trace_id=trace_id,
                    adapter=adapter,
                    allow_mutation=payload.mode == "apply" and original is None,
                )
                if result.status in {"completed", "replayed", "adopted"}:
                    return RetainedVolumeSweepResponse.model_validate(
                        result.response_payload
                    ).model_copy(
                        update={"trace_id": trace_id, "replayed": result.status == "replayed"}
                    )
                if result.status == "conflict":
                    refuse(
                        "idempotency_key_reused", "The key belongs to a different sweep request."
                    )
                refuse(
                    "mutation_reconciliation_required",
                    "The retained volume sweep has not settled. Preserve the original request and key.",
                )
            except Exception as error:
                if getattr(error, "status_code", None) is not None:
                    raise
                refuse(
                    "mutation_reconciliation_required"
                    if original is not None
                    else "retained_volume_sweep_refused",
                    "The retained volume sweep could not be completed. Preserve the original request and key.",
                )
            raise AssertionError

        task = asyncio.create_task(asyncio.to_thread(execute))
        return await asyncio.shield(task)

    app.add_api_route(
        RETAINED_VOLUME_SWEEP_ROUTE,
        retained_volume_sweep,
        methods=["POST"],
        response_model=RetainedVolumeSweepResponse,
        response_model_exclude_none=True,
        operation_id="sweep_retained_volumes",
        summary="Inspect or sweep retained volumes",
        responses={
            status: {"model": dependencies.error_response_model}
            for status in (400, 401, 403, 404, 409, 503)
        },
    )
