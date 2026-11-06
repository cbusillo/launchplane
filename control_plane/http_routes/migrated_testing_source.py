import asyncio
from collections.abc import Callable
from functools import partial
from typing import Annotated, Literal, NoReturn
import hashlib

from fastapi import Depends, Header, Path, Request
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.odoo_testing_source import MigratedOdooTestingSource
from control_plane.contracts.testing_candidate import TestingCandidateAllocation
from control_plane.durable_operation_authorization import read_active_authz_policy_record
from control_plane.http_routes.mutation_support import idempotency_scope
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
    run_in_worker_thread,
)
from control_plane.migrated_testing_source import (
    MigratedUpload,
    migrated_candidate,
    receive_migrated_part,
    upload_state,
    finish_migrated_upload,
)
from control_plane.service_auth import GitHubHumanIdentity, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.testing_rebuild_authorization import director_testing_rebuild_allowed
from control_plane.odoo_testing_restore import generation_binding


class MigratedSourceUploadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    product: str
    operation_id: str
    source_binding_sha256: str
    receipt_recorded: bool
    parts: dict[str, dict[str, object]]


def register_migrated_testing_source_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
) -> None:
    def context(
        store: object,
        identity: LaunchplaneIdentity,
        product: str,
        operation_id: str,
        *,
        read_only: bool = False,
    ) -> tuple[PostgresRecordStore, TestingCandidateAllocation]:
        trace = dependencies.next_trace_id()
        if not isinstance(store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Migrated input needs the original testing operation.",
            )
        operation = store.read_odoo_stable_target_replacement_operation_record(operation_id)
        allowed = director_testing_rebuild_allowed(
            identity=identity, request=operation.request, context=operation.context, store=store
        )
        if isinstance(identity, GitHubHumanIdentity):
            allowed = read_active_authz_policy_record(store).policy.names_administrator(identity)
        if (
            not allowed
            or operation.product != product
            or (not read_only and operation.idempotency_scope != idempotency_scope(identity))
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Use the original authorized testing caller for this upload.",
            )
        return store, migrated_candidate(store, product, operation_id)

    def response(
        store: PostgresRecordStore, allocation: TestingCandidateAllocation
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
            parts=upload_state(allocation),
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
    ) -> MigratedSourceUploadResponse:
        try:
            store, allocation = await run_in_worker_thread(
                lambda: context(record_store, identity, product, operation_id, read_only=True)
            )
            return await run_in_worker_thread(lambda: response(store, allocation))
        except (ValueError, OSError) as error:
            refuse(error)

    async def upload(
        product: Annotated[str, Path(min_length=1)],
        operation_id: Annotated[str, Path(min_length=1)],
        part: Literal["database", "filestore"],
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        offset: Annotated[int, Header(alias="Upload-Offset", ge=0)] = 0,
        prefix_sha256: Annotated[
            str, Header(alias="Upload-Prefix-SHA256", pattern=r"^[a-f0-9]{64}$")
        ] = hashlib.sha256().hexdigest(),
        content_length: Annotated[int, Header(alias="Content-Length", ge=0)] = 0,
    ) -> MigratedSourceUploadResponse:
        try:
            store, allocation = await run_in_worker_thread(
                lambda: context(record_store, identity, product, operation_id)
            )
            assert allocation.application is not None
            source = allocation.application.plan.native_source
            if (
                request.headers.get("content-type", "").split(";", 1)[0]
                != "application/octet-stream"
                or content_length != getattr(source, part).size - offset
            ):
                raise ValueError("Upload body differs from the admitted remaining bytes")
            receiver = receive_migrated_part(allocation, part, offset, prefix_sha256)
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
                async for chunk in request.stream():
                    await blocking(partial(writer.write, chunk))
            except BaseException as error:
                if entered:
                    await blocking(
                        partial(receiver.__exit__, type(error), error, error.__traceback__)
                    )
                raise
            else:
                await blocking(partial(receiver.__exit__, None, None, None))
            await run_in_worker_thread(lambda: finish_migrated_upload(store, allocation))
            return await run_in_worker_thread(lambda: response(store, allocation))
        except (ValueError, OSError) as error:
            refuse(error)

    route = "/v1/products/{product}/testing-rebuild/{operation_id}/migrated-source"
    app.add_api_route(
        route, read_state, methods=["GET"], response_model=MigratedSourceUploadResponse
    )
    app.add_api_route(
        route + "/{part}",
        upload,
        methods=["POST"],
        response_model=MigratedSourceUploadResponse,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/octet-stream": {"schema": {"type": "string", "format": "binary"}}
                },
            }
        },
    )
