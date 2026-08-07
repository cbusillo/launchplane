from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Path
from pydantic import BaseModel, ConfigDict

from control_plane.change_impact_service import ChangeImpactRepositoryEvidenceProvider
from control_plane.contracts.change_impact import ChangeImpactTargetReference
from control_plane.contracts.engineering_review_decision import (
    ENGINEERING_REVIEW_DECISION_CREATE_ACTION,
    ENGINEERING_REVIEW_DECISION_PROJECT_ACTION,
    ENGINEERING_REVIEW_DECISION_READ_ACTION,
    EngineeringReviewDecisionProjectionRequest,
    EngineeringReviewDecisionProjectionResult,
    EngineeringReviewDecisionRecord,
    EngineeringReviewDecisionRequest,
)
from control_plane.engineering_review_decision_projection import (
    EngineeringReviewDecisionProjectionError,
    project_engineering_review_decision,
)
from control_plane.engineering_review_decision_service import (
    EngineeringReviewDecisionConflictError,
    evaluate_engineering_review_decision,
    require_engineering_review_decision_store,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.github_app_identity import (
    GitHubAppIdentityError,
    GitHubAppInstallationToken,
    revoke_installation_token,
)
from control_plane.service_auth import LaunchplaneIdentity


class EngineeringReviewDecisionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    created: bool = False
    decision: EngineeringReviewDecisionRecord


class EngineeringReviewDecisionProjectionResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")
    status: Literal["ok"] = "ok"
    trace_id: str
    result: EngineeringReviewDecisionProjectionResult


@dataclass(frozen=True, slots=True)
class EngineeringReviewDecisionRouteDependencies:
    read_write_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: Callable[..., bool]
    http_error: Callable[..., Exception]
    error_response_model: type[BaseModel]
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider
    github_app_token: Callable[[str, str], GitHubAppInstallationToken]
    github_api: Callable[..., object]


def register_engineering_review_decision_routes(
    app: ApiRouteRegistrar,
    *,
    read_dependencies: ReadRouteDependencies,
    write_dependencies: EngineeringReviewDecisionRouteDependencies,
) -> None:
    def authorize(identity: LaunchplaneIdentity, action: str, trace_id: str) -> None:
        if not write_dependencies.authorization_allows(
            identity=identity,
            action=action,
            product="launchplane",
            context="launchplane",
        ):
            raise write_dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=f"Identity cannot perform {action}.",
            )

    async def evaluate(
        request: EngineeringReviewDecisionRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(write_dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(write_dependencies.get_record_store)],
    ) -> EngineeringReviewDecisionResponse:
        trace_id = write_dependencies.next_trace_id()
        authorize(identity, ENGINEERING_REVIEW_DECISION_CREATE_ACTION, trace_id)
        try:
            decision, created = evaluate_engineering_review_decision(
                store=require_engineering_review_decision_store(record_store),
                request=request,
                repository_evidence_provider=(write_dependencies.repository_evidence_provider),
            )
        except FileNotFoundError as error:
            raise write_dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        except EngineeringReviewDecisionConflictError as error:
            raise write_dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="engineering_review_decision_conflict",
                message=str(error),
            ) from error
        except Exception as error:
            raise write_dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="engineering_review_decision_unavailable",
                message="Engineering review decision evidence is unavailable.",
            ) from error
        return EngineeringReviewDecisionResponse(
            trace_id=trace_id, created=created, decision=decision
        )

    def read_decision(
        decision_id: Annotated[str, Path(min_length=1, pattern=r"^\S+$")],
        identity: Annotated[LaunchplaneIdentity, Depends(read_dependencies.read_identity)],
        record_store: Annotated[object, Depends(read_dependencies.get_record_store)],
    ) -> EngineeringReviewDecisionResponse:
        trace_id = read_dependencies.next_trace_id()
        if not read_dependencies.authorization_allows(
            identity=identity,
            action=ENGINEERING_REVIEW_DECISION_READ_ACTION,
            product="launchplane",
            context="launchplane",
        ):
            raise read_dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Identity cannot read engineering review decisions.",
            )
        try:
            decision = require_engineering_review_decision_store(
                record_store
            ).read_engineering_review_decision_record(decision_id)
        except FileNotFoundError as error:
            raise read_dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        return EngineeringReviewDecisionResponse(trace_id=trace_id, decision=decision)

    async def project(
        request: EngineeringReviewDecisionProjectionRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(write_dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(write_dependencies.get_record_store)],
    ) -> EngineeringReviewDecisionProjectionResponse:
        trace_id = write_dependencies.next_trace_id()
        authorize(identity, ENGINEERING_REVIEW_DECISION_PROJECT_ACTION, trace_id)
        try:
            decision_store = require_engineering_review_decision_store(record_store)
            decision = decision_store.read_engineering_review_decision_record(request.decision_id)
            latest = decision_store.list_engineering_review_decision_records(
                repository=decision.target.repository,
                pull_request_number=decision.target.pull_request_number,
                head_sha=decision.target.head_sha,
                work_request_id=decision.work_request_id,
                limit=1,
            )
            if not latest or latest[0].decision_id != decision.decision_id:
                raise EngineeringReviewDecisionProjectionError(
                    "Engineering review projection requires the latest persisted decision."
                )
            current_evidence = write_dependencies.repository_evidence_provider.resolve(
                ChangeImpactTargetReference(
                    repository=decision.target.repository,
                    pull_request_number=decision.target.pull_request_number,
                )
            )
            if current_evidence.target != decision.target:
                raise EngineeringReviewDecisionProjectionError(
                    "Engineering review projection target is stale."
                )
            installation_token = write_dependencies.github_app_token(
                decision.target.repository,
                decision.target.repository_id,
            )
            try:
                result = project_engineering_review_decision(
                    record=decision,
                    installation_token=installation_token,
                    api_request=write_dependencies.github_api,
                )
            finally:
                revoke_installation_token(
                    installation_token=installation_token,
                    api_request=write_dependencies.github_api,
                )
        except FileNotFoundError as error:
            raise write_dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=str(error),
            ) from error
        except (EngineeringReviewDecisionProjectionError, GitHubAppIdentityError) as error:
            raise write_dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="engineering_review_projection_unavailable",
                message=str(error),
            ) from error
        return EngineeringReviewDecisionProjectionResponse(trace_id=trace_id, result=result)

    app.add_api_route(
        "/v1/engineering-review-decisions/evaluate",
        evaluate,
        methods=["POST"],
        response_model=EngineeringReviewDecisionResponse,
        operation_id="evaluate_engineering_review_decision",
        tags=["engineering-review"],
        responses={
            status: {"model": write_dependencies.error_response_model}
            for status in (400, 401, 403, 404, 409, 503)
        },
    )
    app.add_api_route(
        "/v1/engineering-review-decisions/{decision_id}",
        read_decision,
        methods=["GET"],
        response_model=EngineeringReviewDecisionResponse,
        operation_id="read_engineering_review_decision",
        tags=["engineering-review"],
        responses={
            status: {"model": read_dependencies.error_response_model}
            for status in (401, 403, 404, 503)
        },
    )
    app.add_api_route(
        "/v1/engineering-review-decisions/project",
        project,
        methods=["POST"],
        response_model=EngineeringReviewDecisionProjectionResponse,
        operation_id="project_engineering_review_decision",
        tags=["engineering-review"],
        responses={
            status: {"model": write_dependencies.error_response_model}
            for status in (400, 401, 403, 404, 503)
        },
    )
