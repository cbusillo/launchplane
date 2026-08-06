from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from control_plane.change_impact_service import (
    ChangeImpactPolicyApplyResult,
    ChangeImpactPolicyConflictError,
    ChangeImpactPolicySequenceError,
    ChangeImpactPolicyReadModel,
    ChangeImpactRepositoryEvidenceProvider,
    ChangeImpactTrustedCallerBinding,
    apply_change_impact_policy,
    evaluate_change_impact,
    get_change_impact_policy_read_model,
    load_change_impact_stored_evidence,
    require_change_impact_policy_read_store,
    require_change_impact_policy_store,
)
from control_plane.change_impact_github import (
    ChangeImpactRepositoryEvidenceError,
    ChangeImpactRepositoryEvidenceStaleError,
)
from control_plane.contracts.change_impact import (
    CHANGE_IMPACT_EVALUATION_READ_ACTION,
    CHANGE_IMPACT_POLICY_READ_ACTION,
    CHANGE_IMPACT_POLICY_WRITE_ACTION,
    ChangeImpactEvaluation,
    ChangeImpactEvaluationRequest,
    ChangeImpactPolicyRecord,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    AuthorizationAllows,
    HttpErrorFactory,
    ReadRouteDependencies,
)
from control_plane.service_auth import (
    AuthorizationTarget,
    GitHubActionsIdentity,
    LaunchplaneIdentity,
)


CHANGE_IMPACT_POLICY_READ_ROUTE = "/v1/change-impact/policy"
CHANGE_IMPACT_EVALUATION_ROUTE = "/v1/change-impact/evaluation"
CHANGE_IMPACT_POLICY_APPLY_ROUTE = "/v1/change-impact/policies/apply"


@dataclass(frozen=True, slots=True)
class ChangeImpactWriteRouteDependencies:
    read_write_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: AuthorizationAllows
    http_error: HttpErrorFactory
    error_response_model: type[BaseModel]


@dataclass(frozen=True, slots=True)
class ChangeImpactReadRouteDependencies:
    common: ReadRouteDependencies
    repository_evidence_provider: ChangeImpactRepositoryEvidenceProvider


class ChangeImpactPolicyReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    trace_id: str
    read_model: ChangeImpactPolicyReadModel


class ChangeImpactEvaluationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    trace_id: str
    evaluation: ChangeImpactEvaluation


class ChangeImpactPolicyApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry_run", "apply"] = "apply"
    expected_current_record_id: str = ""
    expected_current_policy_digest: str = ""
    record: ChangeImpactPolicyRecord


class ChangeImpactPolicyApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    status: Literal["ok"] = "ok"
    trace_id: str
    result: ChangeImpactPolicyApplyResult


def register_change_impact_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ChangeImpactReadRouteDependencies,
) -> None:
    common = dependencies.common

    def read_policy(
        repository_id: Annotated[str, Query(...)],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ChangeImpactPolicyReadResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action=CHANGE_IMPACT_POLICY_READ_ACTION,
            product="launchplane",
            context="change-impact",
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot read change-impact policy records.",
            )
        try:
            store = require_change_impact_policy_read_store(record_store)
            read_model = get_change_impact_policy_read_model(
                store=store,
                repository_id=repository_id,
            )
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except ValueError as error:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error
        return ChangeImpactPolicyReadResponse(trace_id=trace_id, read_model=read_model)

    def evaluate(
        envelope: ChangeImpactEvaluationRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> ChangeImpactEvaluationResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action=CHANGE_IMPACT_EVALUATION_READ_ACTION,
            product="launchplane",
            context="change-impact",
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot evaluate change impact.",
            )
        if isinstance(identity, GitHubActionsIdentity) and (
            identity.repository.strip().lower() != envelope.target.repository
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="GitHub Actions callers can evaluate only their OIDC repository.",
            )
        try:
            repository_evidence = dependencies.repository_evidence_provider.resolve(envelope.target)
            store = require_change_impact_policy_read_store(record_store)
            policies = store.list_change_impact_policy_records(
                repository_id=repository_evidence.target.repository_id,
            )
            stored_evidence = load_change_impact_stored_evidence(
                store=record_store,
                target=repository_evidence.target,
            )
            trusted_caller_binding = (
                ChangeImpactTrustedCallerBinding(
                    repository=identity.repository,
                    repository_id=identity.repository_id,
                    repository_owner_id=identity.repository_owner_id,
                    head_sha=identity.sha,
                    ref=identity.ref,
                )
                if isinstance(identity, GitHubActionsIdentity)
                else None
            )
            evaluation = evaluate_change_impact(
                repository_evidence=repository_evidence,
                policies=policies,
                stored_evidence=stored_evidence,
                trusted_caller_binding=trusted_caller_binding,
            )
        except ChangeImpactRepositoryEvidenceStaleError as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="change_impact_target_stale",
                message=str(error),
            ) from error
        except ChangeImpactRepositoryEvidenceError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="change_impact_repository_evidence_unavailable",
                message=str(error),
            ) from error
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return ChangeImpactEvaluationResponse(trace_id=trace_id, evaluation=evaluation)

    app.add_api_route(
        CHANGE_IMPACT_POLICY_READ_ROUTE,
        read_policy,
        methods=["GET"],
        response_model=ChangeImpactPolicyReadResponse,
        tags=["change-impact"],
        operation_id="read_change_impact_policy",
        responses={
            403: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )
    app.add_api_route(
        CHANGE_IMPACT_EVALUATION_ROUTE,
        evaluate,
        methods=["POST"],
        response_model=ChangeImpactEvaluationResponse,
        tags=["change-impact"],
        operation_id="evaluate_change_impact",
        responses={
            403: {"model": common.error_response_model},
            409: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )


def register_change_impact_write_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ChangeImpactWriteRouteDependencies,
) -> None:
    def apply_policy(
        envelope: ChangeImpactPolicyApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> ChangeImpactPolicyApplyResponse:
        trace_id = dependencies.next_trace_id()
        if not dependencies.authorization_allows(
            identity=identity,
            action=CHANGE_IMPACT_POLICY_WRITE_ACTION,
            product="launchplane",
            context="change-impact",
            target=AuthorizationTarget(scope="context"),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot write change-impact policy records.",
            )
        try:
            store = require_change_impact_policy_store(record_store)
            result = apply_change_impact_policy(
                store=store,
                record=envelope.record,
                expected_current_record_id=envelope.expected_current_record_id,
                expected_current_policy_digest=envelope.expected_current_policy_digest,
                mode=envelope.mode,
            )
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except (ChangeImpactPolicyConflictError, ChangeImpactPolicySequenceError) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="change_impact_policy_conflict",
                message=str(error),
            ) from error
        return ChangeImpactPolicyApplyResponse(trace_id=trace_id, result=result)

    app.add_api_route(
        CHANGE_IMPACT_POLICY_APPLY_ROUTE,
        apply_policy,
        methods=["POST"],
        response_model=ChangeImpactPolicyApplyResponse,
        status_code=202,
        tags=["change-impact"],
        operation_id="apply_change_impact_policy",
        responses={
            403: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
