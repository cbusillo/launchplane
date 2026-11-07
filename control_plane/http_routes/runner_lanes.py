from typing import Annotated, cast

import click
from fastapi import Depends, Response
from sqlalchemy.exc import SQLAlchemyError

from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.runner_lane_service import (
    RUNNER_LANE_SERVICE_ROUTE,
    SERVICE_CONTEXT,
    RunnerLaneServiceRequest,
    RunnerLaneServiceResponse,
    RunnerLaneStore,
    execute_runner_lane_service_request,
)
from control_plane.service_auth import GitHubActionsIdentity, LaunchplaneIdentity
from control_plane.merge_train_github import MergeTrainGitHubError


def register_runner_lane_service_routes(
    app: ApiRouteRegistrar, *, dependencies: ReadRouteDependencies
) -> None:
    def execute(
        request: RunnerLaneServiceRequest,
        response: Response,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> RunnerLaneServiceResponse:
        trace_id = dependencies.next_trace_id()
        workflow = isinstance(identity, GitHubActionsIdentity)
        if isinstance(identity, GitHubActionsIdentity) and (
            identity.event_name != "workflow_dispatch"
            or identity.ref_type != "branch"
            or identity.workflow_ref
            != f"{identity.repository}/.github/workflows/runner-lane-registration.yml@{identity.ref}"
            or identity.job_workflow_ref not in {"", identity.workflow_ref}
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Runner operations require the retained lifecycle workflow.",
            )
        action = (
            "runner_lane_registration_audit.write" if workflow else "runner_lane_inventory.read"
        )
        if (
            not workflow and request.operation != "inventory"
        ) or not dependencies.authorization_allows(
            identity=identity, action=action, product=SERVICE_CONTEXT, context=SERVICE_CONTEXT
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Only the authorized runner lifecycle workflow may execute runner operations.",
            )
        response.headers["Cache-Control"] = "no-store"
        try:
            return execute_runner_lane_service_request(cast(RunnerLaneStore, record_store), request)
        except (
            LookupError,
            OSError,
            TypeError,
            ValueError,
            click.ClickException,
            MergeTrainGitHubError,
            SQLAlchemyError,
        ) as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="runner_lifecycle_unavailable",
                message="The runner lifecycle App operation is unavailable. Inspect its configured identity and target scope.",
            ) from error

    app.add_api_route(
        RUNNER_LANE_SERVICE_ROUTE,
        execute,
        methods=["POST"],
        response_model=RunnerLaneServiceResponse,
        operation_id="execute_runner_lane_service_operation",
    )
