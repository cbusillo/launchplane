from dataclasses import dataclass
from typing import Annotated, Literal, Protocol, cast

from fastapi import Depends
from pydantic import BaseModel, ConfigDict

from control_plane.contracts.product_environment_read_model import (
    ActionAllowed,
    ProductReadModelStore,
)
from control_plane.contracts.work_graph_read_model import WorkGraphSnapshot
from control_plane.http_routes.support import (
    LAUNCHPLANE_SERVICE_CONTEXT,
    ApiRouteRegistrar,
    ReadRouteDependencies,
)
from control_plane.service_auth import LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.work_graph_issue_inbox import GitHubIssueInboxReadModel
from control_plane.work_graph_service import (
    WorkGraphIssueInboxProvider,
    WorkGraphPlanningFactsProvider,
    WorkGraphWorkRequestStore,
    build_work_graph_snapshot_service_payload,
)
from control_plane.workflows.ship import utc_now_timestamp


@dataclass(frozen=True, slots=True)
class WorkGraphReadRouteDependencies:
    common: ReadRouteDependencies
    action_allowed_for_identity: "ActionAllowedForIdentity"
    planning_facts_provider: WorkGraphPlanningFactsProvider | None
    issue_inbox_provider: WorkGraphIssueInboxProvider | None


class ActionAllowedForIdentity(Protocol):
    def __call__(self, *, identity: LaunchplaneIdentity) -> ActionAllowed: ...


class WorkGraphSnapshotResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    snapshot: WorkGraphSnapshot
    source: dict[str, object]


class WorkGraphIssueInboxResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    configured: bool
    inbox: GitHubIssueInboxReadModel


class WorkGraphSnapshotReadStore(ProductReadModelStore, WorkGraphWorkRequestStore, Protocol):
    pass


def require_read_store_methods(
    record_store: object,
    *,
    dependencies: ReadRouteDependencies,
    trace_id: str,
    method_names: tuple[str, ...],
    message: str,
) -> None:
    missing_methods = tuple(
        method_name
        for method_name in method_names
        if not callable(getattr(record_store, method_name, None))
    )
    if not missing_methods:
        if isinstance(record_store, PostgresRecordStore):
            return
        missing_methods = ("postgres_storage",)
    missing_method_list = ", ".join(missing_methods)
    raise dependencies.http_error(
        status_code=503,
        trace_id=trace_id,
        code="database_storage_required",
        message=f"{message} Missing store method(s): {missing_method_list}.",
    )


def require_work_graph_snapshot_read_store(
    record_store: object,
    *,
    dependencies: ReadRouteDependencies,
    trace_id: str,
) -> WorkGraphSnapshotReadStore:
    require_read_store_methods(
        record_store,
        dependencies=dependencies,
        trace_id=trace_id,
        method_names=(
            "list_product_profile_records",
            "list_every_code_work_request_records",
        ),
        message="Work graph snapshot reads require a database-backed record store.",
    )
    return cast(WorkGraphSnapshotReadStore, record_store)


def _require_work_graph_rank_authorization(
    *,
    dependencies: ReadRouteDependencies,
    identity: LaunchplaneIdentity,
    trace_id: str,
    message: str,
) -> None:
    if dependencies.authorization_allows(
        identity=identity,
        action="work_graph.rank",
        product="launchplane",
        context=LAUNCHPLANE_SERVICE_CONTEXT,
    ):
        return
    raise dependencies.http_error(
        status_code=403,
        trace_id=trace_id,
        code="authorization_denied",
        message=message,
    )


def register_work_graph_snapshot_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: WorkGraphReadRouteDependencies,
) -> None:
    common = dependencies.common

    def read_work_graph_snapshot(
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> WorkGraphSnapshotResponse:
        trace_id = common.next_trace_id()
        _require_work_graph_rank_authorization(
            dependencies=common,
            identity=identity,
            trace_id=trace_id,
            message="Workflow cannot read the Launchplane work graph snapshot.",
        )
        snapshot_store = require_work_graph_snapshot_read_store(
            record_store,
            dependencies=common,
            trace_id=trace_id,
        )
        payload = build_work_graph_snapshot_service_payload(
            generated_at=utc_now_timestamp(),
            product_store=snapshot_store,
            work_request_store=snapshot_store,
            action_allowed=dependencies.action_allowed_for_identity(identity=identity),
            planning_facts_provider=dependencies.planning_facts_provider,
        )
        return WorkGraphSnapshotResponse(
            trace_id=trace_id,
            snapshot=WorkGraphSnapshot.model_validate(payload["snapshot"]),
            source=cast(dict[str, object], payload["source"]),
        )

    app.add_api_route(
        "/v1/work-graph/snapshot",
        read_work_graph_snapshot,
        methods=["GET"],
        response_model=WorkGraphSnapshotResponse,
        operation_id="read_work_graph_snapshot",
        summary="Read Launchplane work graph snapshot",
        responses={
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )


def register_work_graph_issue_inbox_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: WorkGraphReadRouteDependencies,
) -> None:
    common = dependencies.common

    def read_work_graph_issue_inbox(
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
    ) -> WorkGraphIssueInboxResponse:
        trace_id = common.next_trace_id()
        _require_work_graph_rank_authorization(
            dependencies=common,
            identity=identity,
            trace_id=trace_id,
            message="Workflow cannot read the Launchplane GitHub issue inbox.",
        )
        if dependencies.issue_inbox_provider is None:
            return WorkGraphIssueInboxResponse(
                trace_id=trace_id,
                configured=False,
                inbox=GitHubIssueInboxReadModel(
                    generated_at="",
                    repository_count=0,
                    issue_count=0,
                ),
            )
        return WorkGraphIssueInboxResponse(
            trace_id=trace_id,
            configured=True,
            inbox=dependencies.issue_inbox_provider(),
        )

    app.add_api_route(
        "/v1/work-graph/github/issues",
        read_work_graph_issue_inbox,
        methods=["GET"],
        response_model=WorkGraphIssueInboxResponse,
        operation_id="read_work_graph_issue_inbox",
        summary="Read Launchplane GitHub issue inbox",
        responses={
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )
