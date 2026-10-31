import asyncio
from collections.abc import AsyncIterator
from contextlib import aclosing
from typing import Annotated

from fastapi.responses import StreamingResponse

from fastapi import Depends, Query

from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    ReadRouteDependencies,
    run_in_worker_thread,
)
from control_plane.request_attention import (
    OpenRequestReader,
    RequestAttentionPage,
    attention_sources,
    build_request_attention_page,
)
from control_plane.merge_train_policy_source import resolve_merge_train_policy_record
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    director_agent_identity,
)
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.timestamps import utc_now_timestamp
from control_plane.request_attention_events import attention_event_lines

REQUEST_ATTENTION_ROUTE = "/v1/attention/requests"


def require_attention_access(
    identity: LaunchplaneIdentity, dependencies: ReadRouteDependencies, trace: str
) -> None:
    admin = (
        isinstance(identity, LocalAdminIdentity)
        or isinstance(identity, GitHubHumanIdentity)
        and identity.role == "admin"
        or director_agent_identity(identity)
    )
    if not admin or not dependencies.authorization_allows(
        identity=identity,
        action="repository_inventory.read",
        product="launchplane",
        context="launchplane",
    ):
        raise dependencies.http_error(
            status_code=403,
            trace_id=trace,
            code="authorization_denied",
            message="Cross-product request attention requires administrator access.",
        )


def attention_database_url(store: object) -> str | None:
    return (
        store.database_url
        if isinstance(store, PostgresRecordStore) and store.database_dialect_name == "postgresql"
        else None
    )


def register_request_attention_routes(
    app: ApiRouteRegistrar, *, dependencies: ReadRouteDependencies, read: OpenRequestReader
) -> None:
    subscriptions = 0
    subscription_limit = 4

    async def read_attention_events(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> StreamingResponse:
        nonlocal subscriptions
        trace = dependencies.next_trace_id()
        require_attention_access(identity, dependencies, trace)
        database_url = attention_database_url(store)
        if database_url is None:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Attention subscriptions require PostgreSQL.",
            )
        if subscriptions >= subscription_limit:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="attention_subscribers_full",
                message="Attention subscriber limit reached.",
            )
        subscriptions += 1

        async def lines() -> AsyncIterator[bytes]:
            nonlocal subscriptions
            try:
                async with aclosing(attention_event_lines(database_url)) as events:
                    async for line in events:
                        yield line
            except asyncio.CancelledError:
                raise
            except Exception:
                yield b'{"type":"disconnected"}\n'
            finally:
                subscriptions -= 1

        return StreamingResponse(
            lines(),
            media_type="application/x-ndjson",
            headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"},
        )

    app.add_api_route(
        REQUEST_ATTENTION_ROUTE + "/events",
        read_attention_events,
        methods=["GET"],
        operation_id="read_request_attention_events",
        response_class=StreamingResponse,
        responses={code: {"model": dependencies.error_response_model} for code in (401, 403, 503)},
    )

    async def read_request_attention(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        store: Annotated[object, Depends(dependencies.get_record_store)],
        repository: Annotated[str, Query(max_length=256)] = "",
    ) -> RequestAttentionPage:
        trace = dependencies.next_trace_id()
        require_attention_access(identity, dependencies, trace)
        if not isinstance(store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Request attention requires database-backed inventory.",
            )
        sources = attention_sources(store)
        try:
            repository = repository.strip().casefold()
            author_ids = (
                tuple(
                    author_id
                    for policy in resolve_merge_train_policy_record(store).policy.policies
                    if policy.repository.casefold() == repository
                    for author_id in policy.enqueue.trusted_automation_github_user_ids
                )
                if repository
                else ()
            )
            return await run_in_worker_thread(
                lambda: build_request_attention_page(
                    sources=sources,
                    repository=repository,
                    trusted_author_ids=author_ids,
                    read=read,
                    trace_id=trace,
                    observed_at=utc_now_timestamp(),
                )
            )
        except LookupError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace,
                code="not_found",
                message="Request source is not in tracked inventory.",
            ) from error
        except Exception as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="request_attention_unavailable",
                message="Waiting requests could not be read from their source.",
            ) from error

    app.add_api_route(
        REQUEST_ATTENTION_ROUTE,
        read_request_attention,
        methods=["GET"],
        response_model=RequestAttentionPage,
        operation_id="read_request_attention",
        responses={
            code: {"model": dependencies.error_response_model} for code in (401, 403, 404, 503)
        },
    )
