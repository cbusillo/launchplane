import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from functools import partial
from typing import Annotated, Any, Literal, NoReturn, Protocol

from fastapi import Depends, HTTPException, Path, Query, WebSocket, WebSocketDisconnect
from starlette.websockets import WebSocketState
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.exc import SQLAlchemyError

from control_plane.contracts.odoo_testing_source import MigratedOdooTestingSource
from control_plane.contracts.testing_candidate import TestingCandidateAllocation
from control_plane.contracts.testing_candidate_application import CandidateApplicationPhase
from control_plane.durable_operation_authorization import read_active_authz_policy_record
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
    run_in_worker_thread,
)
from control_plane.migrated_testing_source import (
    CandidateUploadHold,
    MigratedUpload,
    migrated_upload_holds,
    migrated_candidate,
    receive_migrated_part,
    upload_state,
    finish_migrated_upload,
)
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_rebuild_authorization import director_testing_rebuild_allowed
from control_plane.odoo_testing_restore import generation_binding


class UploadRoutes(ApiRouteRegistrar, Protocol):
    def add_api_websocket_route(self, path: str, endpoint: Callable[..., Any]) -> None: ...


class MigratedUploadStart(BaseModel):
    model_config = ConfigDict(extra="forbid")
    offset: int = Field(ge=0)
    prefix_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


class MigratedUploadPrerequisites(BaseModel):
    model_config = ConfigDict(extra="forbid")
    candidate_phase: CandidateApplicationPhase
    restore_workflow_started: bool | None
    original_caller: bool
    holds: list[CandidateUploadHold | Literal["original_caller_required"]]


@dataclass(frozen=True)
class MigratedUploadContext:
    store: PostgresRecordStore
    allocation: TestingCandidateAllocation
    original_caller: bool


class MigratedSourceUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    operation_id: str
    source_binding_sha256: str
    receipt_recorded: bool
    parts: dict[str, dict[str, object]]
    prerequisites: MigratedUploadPrerequisites | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


def register_migrated_testing_source_routes(
    app: UploadRoutes,
    *,
    dependencies: ReadRouteDependencies,
    bearer_identity: Callable[[str], LaunchplaneIdentity | None],
) -> None:
    def context(
        store: object,
        identity: LaunchplaneIdentity,
        product: str,
        operation_id: str,
        *,
        read_only: bool = False,
    ) -> MigratedUploadContext:
        trace = dependencies.next_trace_id()
        if not isinstance(store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Migrated input needs the original testing operation.",
            )
        try:
            policy = read_active_authz_policy_record(store)
        except (
            LookupError,
            OSError,
            RuntimeError,
            TypeError,
            ValueError,
            SQLAlchemyError,
        ) as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="authz_policy_unavailable",
                message="Launchplane active authz policy is unavailable.",
            ) from error
        operation = store.read_odoo_stable_target_replacement_operation_record(operation_id)
        allowed = director_testing_rebuild_allowed(
            identity=identity, request=operation.request, context=operation.context, store=store
        )
        if isinstance(identity, GitHubHumanIdentity):
            allowed = policy.policy.names_administrator(identity)
        original_caller = operation.idempotency_scope == idempotency_scope(identity)
        if not allowed or operation.product != product or (not read_only and not original_caller):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Use the original authorized testing caller for this upload.",
            )
        return MigratedUploadContext(
            store,
            migrated_candidate(store, product, operation_id, allow_complete=read_only),
            original_caller,
        )

    def response(
        store: PostgresRecordStore,
        allocation: TestingCandidateAllocation,
        prerequisites: MigratedUploadPrerequisites | None = None,
    ) -> MigratedSourceUploadResponse:
        assert (
            allocation.application is not None
            and allocation.application.plan.generation is not None
        )
        generation = allocation.application.plan.generation
        source = generation.migrated_source
        assert isinstance(source, MigratedOdooTestingSource)
        receipt = store.read_native_testing_copy_capture(generation_binding(generation))
        return MigratedSourceUploadResponse(
            product=allocation.product,
            operation_id=allocation.operation_id,
            source_binding_sha256=source.binding_sha256,
            receipt_recorded=receipt is not None,
            prerequisites=prerequisites,
            parts={
                part: {"complete": True, "size": getattr(receipt, part).size}
                for part in ("database", "filestore")
            }
            if receipt is not None
            else upload_state(allocation),
        )

    def refuse(error: Exception) -> NoReturn:
        raise dependencies.http_error(
            status_code=409,
            trace_id=dependencies.next_trace_id(),
            code="migrated_source_upload_incomplete",
            message="The original migrated upload is incomplete or changed; retain it and read its state.",
        ) from error

    async def read_state(
        product: Annotated[str, Path(min_length=1)],
        operation_id: Annotated[str, Path(min_length=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        include_prerequisites: Annotated[bool, Query()] = False,
    ) -> MigratedSourceUploadResponse:
        try:
            resolved = await run_in_worker_thread(
                lambda: context(record_store, identity, product, operation_id, read_only=True)
            )
            application = resolved.allocation.application
            assert application is not None
            holds: list[CandidateUploadHold | Literal["original_caller_required"]] = [
                *migrated_upload_holds(application)
            ]
            if not resolved.original_caller:
                holds.append("original_caller_required")
            prerequisites = (
                MigratedUploadPrerequisites(
                    candidate_phase=application.phase,
                    restore_workflow_started=application.restore_workflow_started,
                    original_caller=resolved.original_caller,
                    holds=holds,
                )
                if include_prerequisites
                else None
            )
            return await run_in_worker_thread(
                lambda: response(resolved.store, resolved.allocation, prerequisites)
            )
        except (ValueError, OSError) as error:
            refuse(error)

    async def upload(
        websocket: WebSocket,
        product: str,
        operation_id: str,
        part: Literal["database", "filestore"],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> None:
        try:
            identity = await run_in_worker_thread(
                lambda: bearer_identity(websocket.headers.get("authorization", ""))
            )
            if identity is None:
                raise ValueError("An authenticated bearer caller is required")
            resolved = await run_in_worker_thread(
                lambda: context(record_store, identity, product, operation_id)
            )
            store, allocation = resolved.store, resolved.allocation
            assert allocation.application is not None
            await websocket.accept()
            start = MigratedUploadStart.model_validate(await websocket.receive_json())
            receiver = receive_migrated_part(allocation, part, start.offset, start.prefix_sha256)
            entered = []

            def enter() -> MigratedUpload:
                writer = receiver.__enter__()
                entered.append(writer)
                return writer

            async def blocking[Result](operation: Callable[[], Result]) -> Result:
                task = asyncio.ensure_future(asyncio.to_thread(operation))
                cancelled = False
                while True:
                    try:
                        result = await asyncio.shield(task)
                        break
                    except asyncio.CancelledError:
                        cancelled = True
                if cancelled:
                    raise asyncio.CancelledError
                return result

            try:
                writer = await blocking(enter)
                await websocket.send_json({"ready": True, "offset": start.offset})
                while writer.remaining:
                    chunk = await websocket.receive_bytes()
                    if not 0 < len(chunk) <= 1024 * 1024:
                        raise ValueError("Upload frame exceeds its bound")
                    await blocking(partial(writer.write, chunk))
                if await websocket.receive_json() != {"complete": True}:
                    raise ValueError("The original upload has no completion frame")
            except BaseException as error:
                if entered:
                    await blocking(
                        partial(receiver.__exit__, type(error), error, error.__traceback__)
                    )
                raise
            else:
                await blocking(partial(receiver.__exit__, None, None, None))
            await blocking(lambda: finish_migrated_upload(store, allocation))
            completed = await blocking(lambda: response(store, allocation))
            await websocket.send_json(completed.model_dump(mode="json"))
        except (HTTPException, ValueError, OSError, WebSocketDisconnect, LookupError):
            if (
                websocket.client_state != WebSocketState.DISCONNECTED
                and websocket.application_state != WebSocketState.DISCONNECTED
            ):
                await websocket.close(code=1008)
            return
        if (
            websocket.client_state != WebSocketState.DISCONNECTED
            and websocket.application_state != WebSocketState.DISCONNECTED
        ):
            await websocket.close()

    route = "/v1/products/{product}/testing-rebuild/{operation_id}/migrated-source"
    app.add_api_route(
        route, read_state, methods=["GET"], response_model=MigratedSourceUploadResponse
    )
    app.add_api_websocket_route(route + "/{part}", upload)
