"""Product-scoped discussion records, separate from runtime configuration."""

from collections.abc import Callable
from typing import Annotated, Literal

from fastapi import Depends, Path
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.discussion_topic import (
    DiscussionTopicInput,
    DiscussionTopicRecord,
    TopicId,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    LAUNCHPLANE_SERVICE_CONTEXT,
    ReadRouteDependencies,
)
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    director_agent_identity,
)
from control_plane.storage.postgres import PostgresRecordStore

ROUTE = "/v1/products/{product}/discussion-topics"
Product = Annotated[str, Path(min_length=1, max_length=128, pattern=r"^\S+$")]


class DiscussionTopicsResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    topics: tuple[DiscussionTopicRecord, ...]


class DiscussionTopicResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    topic: DiscussionTopicRecord


def register_discussion_topic_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
    mutation_identity: Callable[..., LaunchplaneIdentity],
) -> None:
    def require_store(
        store: object, identity: LaunchplaneIdentity, product: str, trace: str, *, write: bool
    ) -> PostgresRecordStore:
        principal_can_write = (
            director_agent_identity(identity)
            or isinstance(identity, LocalAdminIdentity)
            or isinstance(identity, GitHubHumanIdentity)
            and identity.role == "admin"
        )
        if (write and not principal_can_write) or not dependencies.authorization_allows(
            identity=identity,
            action="discussion_topic.write" if write else "product_profile.read",
            product=product,
            context=LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace,
                code="authorization_denied",
                message="Caller cannot access these product discussion topics.",
            )
        if not isinstance(store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace,
                code="database_storage_required",
                message="Discussion topics require database storage.",
            )
        try:
            store.read_product_profile_record(product)
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace,
                code="not_found",
                message="Product was not found.",
            ) from error
        return store

    def list_topics(
        product: Product,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        store: Annotated[object, Depends(dependencies.get_record_store)],
        include_closed: bool = False,
    ) -> DiscussionTopicsResponse:
        trace = dependencies.next_trace_id()
        database = require_store(store, identity, product, trace, write=False)
        return DiscussionTopicsResponse(
            trace_id=trace,
            topics=database.list_discussion_topics(product, include_closed=include_closed),
        )

    def add_topic(
        product: Product,
        topic_id: TopicId,
        body: DiscussionTopicInput,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> DiscussionTopicResponse:
        trace = dependencies.next_trace_id()
        database = require_store(store, identity, product, trace, write=True)
        try:
            topic = database.add_discussion_topic(product, topic_id, body)
        except ValueError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace,
                code="topic_conflict",
                message="Topic id already belongs to a different discussion.",
            ) from error
        return DiscussionTopicResponse(trace_id=trace, topic=topic)

    def close_topic(
        product: Product,
        topic_id: TopicId,
        identity: Annotated[LaunchplaneIdentity, Depends(mutation_identity)],
        store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> DiscussionTopicResponse:
        trace = dependencies.next_trace_id()
        database = require_store(store, identity, product, trace, write=True)
        try:
            topic = database.close_discussion_topic(product, topic_id)
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace,
                code="not_found",
                message="Discussion topic was not found.",
            ) from error
        return DiscussionTopicResponse(trace_id=trace, topic=topic)

    for path, endpoint, method, response_model, operation_id in (
        (ROUTE, list_topics, "GET", DiscussionTopicsResponse, "list_discussion_topics"),
        (ROUTE + "/{topic_id}", add_topic, "PUT", DiscussionTopicResponse, "add_discussion_topic"),
        (
            ROUTE + "/{topic_id}/close",
            close_topic,
            "POST",
            DiscussionTopicResponse,
            "close_discussion_topic",
        ),
    ):
        app.add_api_route(
            path,
            endpoint,
            methods=[method],
            response_model=response_model,
            operation_id=operation_id,
            responses={
                code: {"model": dependencies.error_response_model}
                for code in (401, 403, 404, 409, 503)
            },
        )
