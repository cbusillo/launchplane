from typing import Annotated

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

REQUEST_ATTENTION_ROUTE = "/v1/attention/requests"


def register_request_attention_routes(
    app: ApiRouteRegistrar, *, dependencies: ReadRouteDependencies, read: OpenRequestReader
) -> None:
    async def read_request_attention(
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        store: Annotated[object, Depends(dependencies.get_record_store)],
        repository: Annotated[str, Query(max_length=256)] = "",
    ) -> RequestAttentionPage:
        trace = dependencies.next_trace_id()
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
