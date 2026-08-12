from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, Query

from control_plane.change_impact_github import ChangeImpactRepositoryEvidenceError
from control_plane.change_impact_service import ChangeImpactRepositoryEvidenceProvider
from control_plane.contracts.change_impact import ChangeImpactTargetReference
from control_plane.contracts.engineering_review_decision import (
    ENGINEERING_REVIEW_DECISION_READ_ACTION,
)
from control_plane.contracts.engineering_review_run import (
    ENGINEERING_REVIEW_AUTHORITY_READ_ACTION,
    ENGINEERING_REVIEW_RUN_READ_ACTION,
)
from control_plane.contracts.governance_projection import GovernanceProjectionResponse
from control_plane.contracts.merge_train_policy import MERGE_TRAIN_POLICY_TARGETS_READ_ACTION
from control_plane.contracts.owner_acceptance import OWNER_ACCEPTANCE_READ_ACTION
from control_plane.governance_projection import (
    GovernanceCurrentReadinessProvider,
    build_governance_projection,
)
from control_plane.http_routes.support import ApiRouteRegistrar, ReadRouteDependencies
from control_plane.owner_acceptance import OwnerAcceptanceEvaluationUnavailableError
from control_plane.merge_train_policy_source import (
    MergeTrainPolicyStoreMissingError,
    resolve_merge_train_policy_record,
)
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
        normalized_base_branch = base_branch.strip()
        if not normalized_base_branch:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Governance base branch must be non-empty.",
            )
        owner_read_allowed = common.authorization_allows(
            identity=identity,
            action=OWNER_ACCEPTANCE_READ_ACTION,
            product="launchplane",
            context="owner-acceptance",
            target=AuthorizationTarget(scope="context"),
        )
        engineering_reads_allowed = all(
            common.authorization_allows(
                identity=identity,
                action=action,
                product="launchplane",
                context="launchplane",
            )
            for action in (
                ENGINEERING_REVIEW_DECISION_READ_ACTION,
                ENGINEERING_REVIEW_RUN_READ_ACTION,
                ENGINEERING_REVIEW_AUTHORITY_READ_ACTION,
            )
        )
        if not owner_read_allowed or not engineering_reads_allowed:
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot read every governance evidence facet.",
            )
        target = ChangeImpactTargetReference(
            repository=repository,
            pull_request_number=pull_request_number,
        )
        try:
            policy_record = resolve_merge_train_policy_record(record_store)
            repository_policy = policy_record.policy.find_repository_policy(
                repository=repository,
                base_branch=normalized_base_branch,
            )
        except MergeTrainPolicyStoreMissingError:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="governance_evidence_unavailable",
                message="Governance merge-train policy evidence is unavailable.",
            ) from None
        except ValueError as error:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error
        service_authz_allowed = common.authorization_allows(
            identity=identity,
            action=repository_policy.service_authz.action,
            product=repository_policy.service_authz.product,
            context=repository_policy.service_authz.context,
        )
        policy_targets_read_allowed = common.authorization_allows(
            identity=identity,
            action=MERGE_TRAIN_POLICY_TARGETS_READ_ACTION,
            product="launchplane",
            context="launchplane",
        )
        if not service_authz_allowed and not policy_targets_read_allowed:
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot read merge-train governance evidence.",
            )
        try:
            repository_evidence = dependencies.repository_evidence_provider.resolve(target)
        except (ChangeImpactRepositoryEvidenceError, LookupError, TypeError, ValueError):
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="governance_evidence_unavailable",
                message="Governance repository evidence is unavailable.",
            ) from None
        actual_base_branch = repository_evidence.base.base_ref if repository_evidence.base else ""
        if actual_base_branch != normalized_base_branch:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Governance base branch does not match current pull request evidence.",
            )
        try:
            projection = build_governance_projection(
                store=record_store,
                repository_evidence_provider=dependencies.repository_evidence_provider,
                current_readiness_provider=dependencies.current_readiness_provider,
                target=target,
                base_branch=normalized_base_branch,
                generated_at=dependencies.now(),
                repository_evidence=repository_evidence,
                github_token_env_var=repository_policy.github_token.env_var,
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
