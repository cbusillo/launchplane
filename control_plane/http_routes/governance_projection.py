from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query

from control_plane.change_impact_service import ChangeImpactRepositoryEvidenceProvider
from control_plane.contracts.change_impact import ChangeImpactTargetReference
from control_plane.contracts.governance_projection import GovernanceProjectionResponse
from control_plane.contracts.owner_acceptance import OWNER_ACCEPTANCE_READ_ACTION
from control_plane.governance_projection import (
    GovernanceCurrentReadinessProvider,
    build_governance_projection,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.owner_acceptance import OwnerAcceptanceEvaluationUnavailableError
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity


GOVERNANCE_PROJECTION_ROUTE = "/v1/governance/projection"


@dataclass(frozen=True, slots=True)
class GovernanceProjectionRouteDependencies:
    common: ReadRouteDependencies
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider
    current_readiness_provider: GovernanceCurrentReadinessProvider
    now: Callable[[], str]


def register_governance_projection_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: GovernanceProjectionRouteDependencies,
) -> None:
    common = dependencies.common

    def read_governance_projection(
        repository: Annotated[
            str,
            Query(min_length=3, max_length=256, pattern=r"^[^/\s]+/[^/\s]+$"),
        ],
        pull_request_number: Annotated[int, Query(ge=1)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        base_branch: Annotated[str, Query(min_length=1, max_length=256)] = "main",
    ) -> GovernanceProjectionResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_READ_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot read governance evidence.",
            )
        try:
            projection = build_governance_projection(
                store=record_store,
                repository_evidence_provider=dependencies.repository_evidence_provider,
                current_readiness_provider=dependencies.current_readiness_provider,
                target=ChangeImpactTargetReference(
                    repository=repository,
                    pull_request_number=pull_request_number,
                ),
                base_branch=base_branch.strip(),
                generated_at=dependencies.now(),
            )
        except (LookupError, OwnerAcceptanceEvaluationUnavailableError, TypeError, ValueError):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="governance_evidence_unavailable",
                message="Governance evidence is unavailable.",
            ) from None
        return GovernanceProjectionResponse(trace_id=trace_id, projection=projection)

    app.add_api_route(
        GOVERNANCE_PROJECTION_ROUTE,
        read_governance_projection,
        methods=["GET"],
        response_model=GovernanceProjectionResponse,
        tags=["governance"],
        operation_id="read_governance_projection",
        summary="Read independent governance facets for one pull request",
        responses={
            status: {"model": common.error_response_model} for status in (400, 401, 403, 503)
        },
    )
