from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, cast

import click
from fastapi import Depends, Header, Query, Request
from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.merge_train_controller_state import (
    MergeTrainControllerAdoptionRejectedError,
    MergeTrainControllerLeaseHeldError,
    MergeTrainControllerLeaseLostError,
)
from control_plane.contracts.merge_train_policy import MergeTrainMergeMethod
from control_plane.http_routes.mutation_support import (
    idempotency_scope,
    request_fingerprint,
)
from control_plane.http_routes.support import (
    LAUNCHPLANE_SERVICE_CONTEXT as _LAUNCHPLANE_SERVICE_CONTEXT,
    ApiRouteRegistrar,
    AuthorizationAllows,
    HttpErrorFactory,
    ReadRouteDependencies,
)
from control_plane.contracts.repository_human_admission import (
    REPOSITORY_HUMAN_ROLE_POLICY_READ_ACTION,
    REPOSITORY_HUMAN_ROLE_POLICY_WRITE_ACTION,
)
from control_plane.contracts.trusted_maintenance import (
    TRUSTED_MAINTENANCE_POLICY_READ_ACTION,
    TRUSTED_MAINTENANCE_POLICY_WRITE_ACTION,
)
from control_plane.contracts.tenant_merge_eligibility import TenantMergeCandidate
from control_plane.repository_human_admission import (
    RepositoryHumanRolePolicyApplyEnvelope,
    RepositoryHumanRolePolicyApplyResult,
    RepositoryHumanRolePolicyConflictError,
    RepositoryHumanRolePolicyReadModel,
    RepositoryHumanRolePolicySequenceError,
    TenantTechnicalHumanWaiverApplyEnvelope,
    TenantTechnicalHumanWaiverApplyResult,
    TenantTechnicalHumanWaiverAuthorizationError,
    TenantTechnicalHumanWaiverEventConflictError,
    TenantTechnicalHumanWaiverRevokeCurrentError,
    TenantTechnicalHumanWaiverStaleAuthorityError,
    apply_tenant_technical_human_waiver,
    apply_repository_human_role_policy,
    get_repository_human_role_policy_read_model,
    normalize_repository_human_role_policy_lookup_scope,
    require_repository_human_role_policy_read_store,
    require_tenant_technical_human_waiver_authority_read_store,
)
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    TerminalAgentIdentity,
)
from control_plane.service_auth import AuthorizationTarget
from control_plane.storage.postgres import DbOnlyMutationRequest, PostgresRecordStore
from control_plane.merge_train_github import MergeTrainGitHubError
from control_plane.tenant_admission_controller import (
    TENANT_ADMISSION_CONTROLLER_RUN_ONCE_ACTION,
    TenantAdmissionControllerError,
    TenantAdmissionControllerReconciliationError,
    TenantAdmissionControllerRunOnceEnvelope,
    TenantAdmissionControllerRunOnceResult,
    TenantAdmissionControllerStaleCandidateError,
    evaluate_tenant_admission_candidate,
    execute_tenant_admission_controller_run_once,
    require_tenant_admission_controller_store,
)
from control_plane.tenant_admission_context import (
    TenantAdmissionEvaluationReadModel,
    build_tenant_admission_evaluation_read_model,
)
from control_plane.tenant_repository_classification import (
    TenantRepositoryClassificationApplyEnvelope,
    TenantRepositoryClassificationApplyResult,
    TenantRepositoryClassificationConflictError,
    TenantRepositoryClassificationReadModel,
    TenantRepositoryClassificationSequenceError,
    apply_tenant_repository_classification,
    get_tenant_repository_classification_read_model,
    require_tenant_repository_classification_read_store,
)
from control_plane.tenant_admission_projection import (
    TENANT_ADMISSION_STATUS_RECONCILE_ACTION,
    TenantAdmissionGitHubProjection,
    TenantAdmissionProjectionError,
    TenantAdmissionProjectionStaleCandidateError,
    TenantAdmissionProjectionWriteResult,
    TenantAdmissionStatusReconcileEnvelope,
    build_tenant_admission_projection,
    read_current_tenant_admission_candidate,
    write_tenant_admission_projection,
)
from control_plane.tenant_admission_status import (
    TENANT_ADMISSION_STATUS_READ_ACTION,
    TenantAdmissionStatusReadModel,
    get_tenant_admission_status,
    require_tenant_admission_status_store,
)
from control_plane.trusted_maintenance import (
    TrustedMaintenancePolicyApplyEnvelope,
    TrustedMaintenancePolicyApplyResult,
    TrustedMaintenancePolicyConflictError,
    TrustedMaintenancePolicyReadModel,
    TrustedMaintenancePolicySequenceError,
    apply_trusted_maintenance_policy,
    get_trusted_maintenance_policy_read_model,
    require_trusted_maintenance_policy_read_store,
)
from control_plane.workflows.ship import utc_now_timestamp

TENANT_REPOSITORY_CLASSIFICATION_READ_ROUTE = (
    "/v1/work-graph/tenant-admission/repository-classification"
)
TENANT_REPOSITORY_CLASSIFICATION_APPLY_ROUTE = (
    "/v1/tenant-admission/repository-classifications/apply"
)
REPOSITORY_HUMAN_ROLE_POLICY_READ_ROUTE = (
    "/v1/work-graph/tenant-admission/repository-human-role-policy"
)
REPOSITORY_HUMAN_ROLE_POLICY_APPLY_ROUTE = (
    "/v1/tenant-admission/repository-human-role-policies/apply"
)
TRUSTED_MAINTENANCE_POLICY_READ_ROUTE = "/v1/work-graph/tenant-admission/trusted-maintenance-policy"
TRUSTED_MAINTENANCE_POLICY_APPLY_ROUTE = "/v1/tenant-admission/trusted-maintenance-policies/apply"
TENANT_TECHNICAL_HUMAN_WAIVER_APPLY_ROUTE = "/v1/tenant-admission/technical-human-waivers/apply"
TENANT_ADMISSION_STATUS_READ_ROUTE = "/v1/work-graph/tenant-admission/status"
TENANT_ADMISSION_EVALUATION_READ_ROUTE = "/v1/work-graph/tenant-admission/evaluation"
TENANT_ADMISSION_STATUS_RECONCILE_ROUTE = "/v1/tenant-admission/status/reconcile"
TENANT_ADMISSION_CONTROLLER_RUN_ONCE_ROUTE = "/v1/work-graph/tenant-admission/controller/run-once"


@dataclass(frozen=True, slots=True)
class TenantAdmissionReadRouteDependencies:
    common: ReadRouteDependencies
    control_plane_root: Path
    github_token: Callable[..., str]


@dataclass(frozen=True, slots=True)
class TenantAdmissionWriteRouteDependencies:
    read_write_identity: Callable[..., LaunchplaneIdentity]
    read_browser_mutation_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: AuthorizationAllows
    http_error: HttpErrorFactory
    error_response_model: type[BaseModel]
    control_plane_root: Path
    github_token: Callable[..., str]
    github_api: Callable[..., object]


class TenantRepositoryClassificationReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    read_model: TenantRepositoryClassificationReadModel


class TenantRepositoryClassificationApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    result: TenantRepositoryClassificationApplyResult
    replayed: bool | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    original_trace_id: str | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class RepositoryHumanRolePolicyReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    read_model: RepositoryHumanRolePolicyReadModel


class RepositoryHumanRolePolicyApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    result: RepositoryHumanRolePolicyApplyResult
    replayed: bool | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    original_trace_id: str | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class TrustedMaintenancePolicyReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    read_model: TrustedMaintenancePolicyReadModel


class TrustedMaintenancePolicyApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    result: TrustedMaintenancePolicyApplyResult
    replayed: bool | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    original_trace_id: str | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class TenantTechnicalHumanWaiverApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    result: TenantTechnicalHumanWaiverApplyResult
    replayed: bool | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    original_trace_id: str | None = Field(
        default=None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )


class TenantAdmissionStatusReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    read_model: TenantAdmissionStatusReadModel


class TenantAdmissionEvaluationReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    read_model: TenantAdmissionEvaluationReadModel


class TenantAdmissionStatusReconcileResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    read_model: TenantAdmissionStatusReadModel
    projection: TenantAdmissionGitHubProjection
    write_result: TenantAdmissionProjectionWriteResult


class TenantAdmissionStatusReconcileResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    result: TenantAdmissionStatusReconcileResult


class TenantAdmissionControllerRunOnceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    result: TenantAdmissionControllerRunOnceResult


def register_tenant_admission_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: TenantAdmissionReadRouteDependencies,
) -> None:
    common = dependencies.common

    def read_tenant_admission_evaluation(
        product: Annotated[str, Query(..., alias="product")],
        context: Annotated[str, Query(..., alias="context")],
        repository_id: Annotated[str, Query(..., alias="repository_id")],
        repository_owner_id: Annotated[str, Query(..., alias="repository_owner_id")],
        repository: Annotated[str, Query(..., alias="repository")],
        pull_request_number: Annotated[int, Query(..., alias="pull_request_number", ge=1)],
        head_sha: Annotated[str, Query(..., alias="head_sha")],
        base_branch: Annotated[str, Query(..., alias="base_branch")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
        merge_method: Annotated[MergeTrainMergeMethod, Query(alias="merge_method")] = "merge",
    ) -> TenantAdmissionEvaluationReadResponse:
        trace_id = common.next_trace_id()
        try:
            request = TenantAdmissionControllerRunOnceEnvelope(
                candidate=TenantMergeCandidate(
                    product=product,
                    context=context,
                    repository_id=repository_id,
                    repository_owner_id=repository_owner_id,
                    repository=repository,
                    pull_request_number=pull_request_number,
                    head_sha=head_sha,
                ),
                base_branch=base_branch,
                merge_method=merge_method,
                mutate=False,
            )
        except ValueError as error:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error
        candidate = request.candidate
        if not common.authorization_allows(
            identity=identity,
            action=TENANT_ADMISSION_STATUS_READ_ACTION,
            product=candidate.product,
            context=candidate.context,
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot evaluate tenant admission for this context.",
            )
        try:
            store = require_tenant_admission_status_store(record_store)
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="record_storage_unavailable",
                message=str(error),
            ) from error
        try:
            token = dependencies.github_token(
                control_plane_root=dependencies.control_plane_root,
                context_name=candidate.context,
            ).strip()
        except click.ClickException as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_unavailable",
                message="Tenant admission evaluation cannot resolve a GitHub token.",
            ) from error
        if not token:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_unavailable",
                message="Tenant admission evaluation cannot resolve a GitHub token.",
            )
        try:
            evaluation = evaluate_tenant_admission_candidate(
                request=request,
                store=store,
                token=token,
            )
        except TenantAdmissionControllerStaleCandidateError as error:
            raise common.http_error(
                status_code=409,
                trace_id=trace_id,
                code="tenant_admission_stale_candidate",
                message=str(error),
            ) from error
        except (
            TenantAdmissionControllerError,
            MergeTrainGitHubError,
            LookupError,
            TypeError,
            ValueError,
        ) as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="tenant_admission_evaluation_unavailable",
                message="Tenant admission evaluation could not verify the exact pull request.",
            ) from error
        return TenantAdmissionEvaluationReadResponse(
            trace_id=trace_id,
            read_model=build_tenant_admission_evaluation_read_model(
                evaluation=evaluation,
            ),
        )

    def read_tenant_admission_status(
        product: Annotated[str, Query(..., alias="product")],
        context: Annotated[str, Query(..., alias="context")],
        repository_id: Annotated[str, Query(..., alias="repository_id")],
        repository_owner_id: Annotated[str, Query(..., alias="repository_owner_id")],
        repository: Annotated[str, Query(..., alias="repository")],
        pull_request_number: Annotated[int, Query(..., alias="pull_request_number", ge=1)],
        head_sha: Annotated[str, Query(..., alias="head_sha")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> TenantAdmissionStatusReadResponse:
        trace_id = common.next_trace_id()
        try:
            candidate = TenantMergeCandidate(
                product=product,
                context=context,
                repository_id=repository_id,
                repository_owner_id=repository_owner_id,
                repository=repository,
                pull_request_number=pull_request_number,
                head_sha=head_sha,
            )
        except ValueError as error:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error
        if not common.authorization_allows(
            identity=identity,
            action=TENANT_ADMISSION_STATUS_READ_ACTION,
            product=candidate.product,
            context=candidate.context,
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read tenant admission status for this context.",
            )
        try:
            store = require_tenant_admission_status_store(record_store)
            read_model = get_tenant_admission_status(store=store, candidate=candidate)
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="record_storage_unavailable",
                message=str(error),
            ) from error
        return TenantAdmissionStatusReadResponse(
            trace_id=trace_id,
            read_model=read_model,
        )

    def read_tenant_repository_classification(
        repository_id: Annotated[str, Query(..., alias="repository_id")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> TenantRepositoryClassificationReadResponse:
        trace_id = common.next_trace_id()
        if not common.authorization_allows(
            identity=identity,
            action="tenant_repository_classification.read",
            product=_LAUNCHPLANE_SERVICE_CONTEXT,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read tenant repository classifications.",
            )

        try:
            store = require_tenant_repository_classification_read_store(record_store)
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        try:
            read_model = get_tenant_repository_classification_read_model(
                repository_id=repository_id,
                store=store,
            )
        except ValueError as error:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error

        return TenantRepositoryClassificationReadResponse(
            trace_id=trace_id,
            read_model=read_model,
        )

    def read_repository_human_role_policy(
        repository_id: Annotated[str, Query(..., alias="repository_id")],
        product: Annotated[str, Query(..., alias="product")],
        context: Annotated[str, Query(..., alias="context")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> RepositoryHumanRolePolicyReadResponse:
        trace_id = common.next_trace_id()
        try:
            normalized_repository_id, normalized_product, normalized_context = (
                normalize_repository_human_role_policy_lookup_scope(
                    repository_id=repository_id,
                    product=product,
                    context=context,
                )
            )
        except ValueError as error:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error

        if not common.authorization_allows(
            identity=identity,
            action=REPOSITORY_HUMAN_ROLE_POLICY_READ_ACTION,
            product=normalized_product,
            context=normalized_context,
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read repository human role policies.",
            )

        try:
            store = require_repository_human_role_policy_read_store(record_store)
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        read_model = get_repository_human_role_policy_read_model(
            repository_id=normalized_repository_id,
            product=normalized_product,
            context=normalized_context,
            store=store,
        )
        return RepositoryHumanRolePolicyReadResponse(
            trace_id=trace_id,
            read_model=read_model,
        )

    def read_trusted_maintenance_policy(
        repository_id: Annotated[str, Query(..., alias="repository_id")],
        product: Annotated[str, Query(..., alias="product")],
        context: Annotated[str, Query(..., alias="context")],
        identity: Annotated[LaunchplaneIdentity, Depends(common.read_identity)],
        record_store: Annotated[object, Depends(common.get_record_store)],
    ) -> TrustedMaintenancePolicyReadResponse:
        trace_id = common.next_trace_id()
        try:
            normalized_repository_id, normalized_product, normalized_context = (
                normalize_repository_human_role_policy_lookup_scope(
                    repository_id=repository_id,
                    product=product,
                    context=context,
                )
            )
        except ValueError as error:
            raise common.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error

        if not common.authorization_allows(
            identity=identity,
            action=TRUSTED_MAINTENANCE_POLICY_READ_ACTION,
            product=normalized_product,
            context=normalized_context,
            target=AuthorizationTarget(scope="context"),
        ):
            raise common.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Workflow cannot read trusted-maintenance policies.",
            )

        try:
            store = require_trusted_maintenance_policy_read_store(record_store)
        except TypeError as error:
            raise common.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        read_model = get_trusted_maintenance_policy_read_model(
            repository_id=normalized_repository_id,
            product=normalized_product,
            context=normalized_context,
            store=store,
        )
        return TrustedMaintenancePolicyReadResponse(
            trace_id=trace_id,
            read_model=read_model,
        )

    app.add_api_route(
        TENANT_ADMISSION_EVALUATION_READ_ROUTE,
        read_tenant_admission_evaluation,
        methods=["GET"],
        response_model=TenantAdmissionEvaluationReadResponse,
        operation_id="read_tenant_admission_evaluation",
        summary="Read exact pull-request tenant admission and technical readiness",
        responses={
            400: {"model": common.error_response_model},
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            409: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )

    app.add_api_route(
        TENANT_ADMISSION_STATUS_READ_ROUTE,
        read_tenant_admission_status,
        methods=["GET"],
        response_model=TenantAdmissionStatusReadResponse,
        operation_id="read_tenant_admission_status",
        summary="Read current exact-head tenant admission status",
        responses={
            400: {"model": common.error_response_model},
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )

    app.add_api_route(
        TENANT_REPOSITORY_CLASSIFICATION_READ_ROUTE,
        read_tenant_repository_classification,
        methods=["GET"],
        response_model=TenantRepositoryClassificationReadResponse,
        operation_id="read_tenant_repository_classification",
        summary="Read current tenant repository classification read model",
        responses={
            400: {"model": common.error_response_model},
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )

    app.add_api_route(
        REPOSITORY_HUMAN_ROLE_POLICY_READ_ROUTE,
        read_repository_human_role_policy,
        methods=["GET"],
        response_model=RepositoryHumanRolePolicyReadResponse,
        operation_id="read_repository_human_role_policy",
        summary="Read current repository human role-policy read model",
        responses={
            400: {"model": common.error_response_model},
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )

    app.add_api_route(
        TRUSTED_MAINTENANCE_POLICY_READ_ROUTE,
        read_trusted_maintenance_policy,
        methods=["GET"],
        response_model=TrustedMaintenancePolicyReadResponse,
        operation_id="read_trusted_maintenance_policy",
        summary="Read current trusted-maintenance policy read model",
        responses={
            400: {"model": common.error_response_model},
            401: {"model": common.error_response_model},
            403: {"model": common.error_response_model},
            503: {"model": common.error_response_model},
        },
    )


def register_tenant_admission_write_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: TenantAdmissionWriteRouteDependencies,
) -> None:
    def _human_waiver_idempotency_scope(identity: GitHubHumanIdentity) -> str:
        return f"github-human-id|{identity.github_id}"

    async def run_tenant_admission_controller_once_route(
        envelope: TenantAdmissionControllerRunOnceEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> TenantAdmissionControllerRunOnceResponse:
        trace_id = dependencies.next_trace_id()
        candidate = envelope.candidate
        if not dependencies.authorization_allows(
            identity=identity,
            action=TENANT_ADMISSION_CONTROLLER_RUN_ONCE_ACTION,
            product=candidate.product,
            context=candidate.context,
            target=AuthorizationTarget(scope="context"),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot run the tenant admission controller for this context.",
            )
        try:
            store = require_tenant_admission_controller_store(record_store)
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="record_storage_unavailable",
                message=str(error),
            ) from error
        token = dependencies.github_token(
            control_plane_root=dependencies.control_plane_root,
            context_name=candidate.context,
        ).strip()
        if not token:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_unavailable",
                message="Tenant admission controller cannot resolve a GitHub token.",
            )
        try:
            result = execute_tenant_admission_controller_run_once(
                request=envelope,
                store=store,
                token=token,
                trace_id=trace_id,
            )
        except TenantAdmissionControllerStaleCandidateError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="tenant_admission_stale_candidate",
                message=str(error),
            ) from error
        except MergeTrainControllerLeaseHeldError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="tenant_admission_controller_busy",
                message=str(error),
            ) from error
        except MergeTrainControllerAdoptionRejectedError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="tenant_admission_reconciliation_required",
                message=str(error),
            ) from error
        except TenantAdmissionControllerReconciliationError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="tenant_admission_reconciliation_required",
                message=str(error),
            ) from error
        except (
            TenantAdmissionControllerError,
            MergeTrainControllerLeaseLostError,
            MergeTrainGitHubError,
            LookupError,
            TypeError,
            ValueError,
        ) as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="tenant_admission_controller_unavailable",
                message="Tenant admission controller could not verify or complete the exact merge.",
            ) from error
        return TenantAdmissionControllerRunOnceResponse(
            trace_id=trace_id,
            result=result,
        )

    async def reconcile_tenant_admission_status_route(
        envelope: TenantAdmissionStatusReconcileEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> TenantAdmissionStatusReconcileResponse:
        trace_id = dependencies.next_trace_id()
        candidate = envelope.candidate
        if isinstance(identity, TerminalAgentIdentity):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Terminal agent credentials cannot reconcile tenant admission status.",
            )
        if not dependencies.authorization_allows(
            identity=identity,
            action=TENANT_ADMISSION_STATUS_RECONCILE_ACTION,
            product=candidate.product,
            context=candidate.context,
            target=AuthorizationTarget(scope="context"),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot reconcile tenant admission status for this context.",
            )
        try:
            store = require_tenant_admission_status_store(record_store)
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="record_storage_unavailable",
                message=str(error),
            ) from error
        token = dependencies.github_token(
            control_plane_root=dependencies.control_plane_root,
            context_name=candidate.context,
        ).strip()
        if not token:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="github_token_unavailable",
                message="Tenant admission reconciliation cannot resolve a GitHub token.",
            )
        try:
            pull_request_url = read_current_tenant_admission_candidate(
                expected=candidate,
                token=token,
                api_request=dependencies.github_api,
            )
        except TenantAdmissionProjectionStaleCandidateError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="tenant_admission_stale_candidate",
                message=str(error),
            ) from error
        except (
            TenantAdmissionProjectionError,
            click.ClickException,
            LookupError,
            ValueError,
        ) as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="tenant_admission_projection_unavailable",
                message="Tenant admission GitHub facts could not be verified.",
            ) from error
        read_model = get_tenant_admission_status(store=store, candidate=candidate)
        projection = build_tenant_admission_projection(
            read_model=read_model,
            candidate=candidate,
            pull_request_url=pull_request_url,
        )
        try:
            write_result = write_tenant_admission_projection(
                projection=projection,
                token=token,
                api_request=dependencies.github_api,
            )
        except (
            TenantAdmissionProjectionError,
            click.ClickException,
            LookupError,
            ValueError,
        ) as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="tenant_admission_projection_unavailable",
                message="Tenant admission status projection could not be delivered.",
            ) from error
        return TenantAdmissionStatusReconcileResponse(
            trace_id=trace_id,
            result=TenantAdmissionStatusReconcileResult(
                read_model=read_model,
                projection=projection,
                write_result=write_result,
            ),
        )

    def _map_waiver_apply_write_result(
        *,
        trace_id: str,
        write_result: object,
    ) -> TenantTechnicalHumanWaiverApplyResponse:
        status = getattr(write_result, "status", "")
        idempotency_record = getattr(write_result, "idempotency_record", None)
        if status == "idempotency_conflict":
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message=(
                    "Idempotency-Key was already used for a different Launchplane "
                    "request payload on this route."
                ),
            )
        if status == "replayed":
            if idempotency_record is None:
                raise dependencies.http_error(
                    status_code=500,
                    trace_id=trace_id,
                    code="internal_error",
                    message="Tenant technical human waiver replay missing stored response.",
                )
            payload = dict(idempotency_record.response_payload)
            payload["replayed"] = True
            payload["original_trace_id"] = idempotency_record.response_trace_id
            return TenantTechnicalHumanWaiverApplyResponse.model_validate(payload)
        if status == "reservation_in_progress":
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_in_progress",
                message="Tenant technical human waiver apply is already in progress.",
            )
        if status == "reconciliation_required":
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_reconciliation_required",
                message="Tenant technical human waiver apply requires reconciliation before retry.",
            )
        if status in {"written", "exact_replay"}:
            result = getattr(write_result, "result", None)
            if not isinstance(result, TenantTechnicalHumanWaiverApplyResult):
                raise dependencies.http_error(
                    status_code=500,
                    trace_id=trace_id,
                    code="internal_error",
                    message="Tenant technical human waiver apply missing result.",
                )
            return TenantTechnicalHumanWaiverApplyResponse(
                trace_id=trace_id,
                result=result,
            )
        raise dependencies.http_error(
            status_code=500,
            trace_id=trace_id,
            code="internal_error",
            message="Tenant technical human waiver apply returned an unknown result.",
        )

    def _map_trusted_maintenance_policy_write_result(
        *,
        trace_id: str,
        write_result: object,
    ) -> TrustedMaintenancePolicyApplyResponse:
        status = getattr(write_result, "status", "")
        idempotency_record = getattr(write_result, "idempotency_record", None)
        if status == "idempotency_conflict":
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="idempotency_key_reused",
                message=(
                    "Idempotency-Key was already used for a different Launchplane "
                    "request payload on this route."
                ),
            )
        if status in {"replayed", "exact_replay"}:
            if idempotency_record is None:
                raise dependencies.http_error(
                    status_code=500,
                    trace_id=trace_id,
                    code="internal_error",
                    message="Trusted-maintenance policy replay missing stored response.",
                )
            payload = dict(idempotency_record.response_payload)
            if status == "replayed":
                payload["replayed"] = True
                payload["original_trace_id"] = idempotency_record.response_trace_id
            return TrustedMaintenancePolicyApplyResponse.model_validate(payload)
        if status == "reservation_in_progress":
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_in_progress",
                message="Trusted-maintenance policy apply is already in progress.",
            )
        if status == "reconciliation_required":
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_reconciliation_required",
                message="Trusted-maintenance policy apply requires reconciliation before retry.",
            )
        if status == "written":
            idempotency_record = getattr(write_result, "idempotency_record", None)
            if idempotency_record is None:
                raise dependencies.http_error(
                    status_code=500,
                    trace_id=trace_id,
                    code="internal_error",
                    message="Trusted-maintenance policy apply missing stored response.",
                )
            return TrustedMaintenancePolicyApplyResponse.model_validate(
                idempotency_record.response_payload
            )
        raise dependencies.http_error(
            status_code=500,
            trace_id=trace_id,
            code="internal_error",
            message="Trusted-maintenance policy apply returned an unknown result.",
        )

    async def apply_tenant_technical_human_waiver_route(
        request: Request,
        envelope: TenantTechnicalHumanWaiverApplyEnvelope,
        identity: Annotated[
            LaunchplaneIdentity,
            Depends(dependencies.read_browser_mutation_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> TenantTechnicalHumanWaiverApplyResponse:
        trace_id = dependencies.next_trace_id()
        if not isinstance(identity, GitHubHumanIdentity) or identity.github_id < 1:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Tenant technical human waiver requires a browser GitHub human session.",
            )

        if envelope.mode == "apply":
            normalized_idempotency_key = idempotency_key.strip()
            if not normalized_idempotency_key:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="idempotency_key_required",
                    message="Apply operation requires an Idempotency-Key header.",
                )
            if (
                not isinstance(record_store, PostgresRecordStore)
                or record_store.database_dialect_name != "postgresql"
            ):
                raise dependencies.http_error(
                    status_code=503,
                    trace_id=trace_id,
                    code="database_storage_required",
                    message=(
                        "Tenant technical human waiver apply requires PostgreSQL-backed "
                        "Launchplane storage."
                    ),
                )
            raw_payload = await request.json()
            payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
            response_payload: dict[str, object] = {"status": "ok", "trace_id": trace_id}
            try:
                write_result = record_store.compare_and_write_tenant_technical_human_waiver_event(
                    identity=identity,
                    envelope=envelope,
                    mutation=DbOnlyMutationRequest(
                        scope=_human_waiver_idempotency_scope(identity),
                        route_path=TENANT_TECHNICAL_HUMAN_WAIVER_APPLY_ROUTE,
                        idempotency_key=normalized_idempotency_key,
                        request_fingerprint=payload_fingerprint,
                        lease_owner=trace_id,
                        response_status_code=202,
                        response_trace_id=trace_id,
                        response_payload=response_payload,
                    ),
                )
            except TenantTechnicalHumanWaiverAuthorizationError as error:
                raise dependencies.http_error(
                    status_code=403,
                    trace_id=trace_id,
                    code="authorization_denied",
                    message=str(error),
                ) from error
            except TenantTechnicalHumanWaiverStaleAuthorityError as error:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="stale_authority",
                    message=str(error),
                ) from error
            except TenantTechnicalHumanWaiverRevokeCurrentError as error:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="revoke_not_current",
                    message=str(error),
                ) from error
            except TenantTechnicalHumanWaiverEventConflictError as error:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="waiver_lifecycle_conflict",
                    message=str(error),
                ) from error
            except ValueError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_request",
                    message=str(error),
                ) from error
            return _map_waiver_apply_write_result(
                trace_id=trace_id,
                write_result=write_result,
            )

        try:
            read_store = require_tenant_technical_human_waiver_authority_read_store(record_store)
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        try:
            result = apply_tenant_technical_human_waiver(
                store=read_store,
                identity=identity,
                envelope=envelope,
                observed_at=utc_now_timestamp(),
            )
        except TenantTechnicalHumanWaiverAuthorizationError as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=str(error),
            ) from error
        except TenantTechnicalHumanWaiverStaleAuthorityError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="stale_authority",
                message=str(error),
            ) from error
        except TenantTechnicalHumanWaiverRevokeCurrentError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="revoke_not_current",
                message=str(error),
            ) from error
        except TenantTechnicalHumanWaiverEventConflictError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="waiver_lifecycle_conflict",
                message=str(error),
            ) from error
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error
        return TenantTechnicalHumanWaiverApplyResponse(
            trace_id=trace_id,
            result=result,
        )

    async def apply_tenant_repository_classification_route(
        request: Request,
        envelope: TenantRepositoryClassificationApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> TenantRepositoryClassificationApplyResponse:
        trace_id = dependencies.next_trace_id()

        if isinstance(identity, TerminalAgentIdentity):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Terminal agent credentials cannot apply repository classifications.",
            )

        if not dependencies.authorization_allows(
            identity=identity,
            action="tenant_repository_classification.write",
            product=_LAUNCHPLANE_SERVICE_CONTEXT,
            context=_LAUNCHPLANE_SERVICE_CONTEXT,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller lacks permission to apply tenant repository classifications.",
            )

        if envelope.mode == "apply":
            normalized_idempotency_key = idempotency_key.strip()
            if not normalized_idempotency_key:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="idempotency_key_required",
                    message="Apply operation requires an Idempotency-Key header.",
                )

            if (
                not isinstance(record_store, PostgresRecordStore)
                or record_store.database_dialect_name != "postgresql"
            ):
                raise dependencies.http_error(
                    status_code=503,
                    trace_id=trace_id,
                    code="database_storage_required",
                    message=(
                        "Tenant repository classification apply requires PostgreSQL-backed "
                        "Launchplane storage."
                    ),
                )

            store = record_store
            normalized_scope = idempotency_scope(identity)
            raw_payload = await request.json()
            payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
            result = TenantRepositoryClassificationApplyResult(
                status="applied",
                mode="apply",
                repository_id=envelope.record.repository_id,
                classification_revision=envelope.record.classification_revision,
                record_id=envelope.record.record_id,
                classification_digest=envelope.record.classification_digest,
                supersedes_record_id=envelope.record.supersedes_record_id,
                applied_at=envelope.record.classified_at,
            )
            response = TenantRepositoryClassificationApplyResponse(
                trace_id=trace_id,
                result=result,
            )
            try:
                write_result = store.compare_and_write_tenant_repository_classification_record(
                    record=envelope.record,
                    expected_current_record_id=envelope.expected_current_record_id,
                    mutation=DbOnlyMutationRequest(
                        scope=normalized_scope,
                        route_path=TENANT_REPOSITORY_CLASSIFICATION_APPLY_ROUTE,
                        idempotency_key=normalized_idempotency_key,
                        request_fingerprint=payload_fingerprint,
                        lease_owner=trace_id,
                        response_status_code=200,
                        response_trace_id=trace_id,
                        response_payload=response.model_dump(mode="json"),
                    ),
                )
            except TenantRepositoryClassificationConflictError as error:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="classification_conflict",
                    message=str(error),
                ) from error
            except TenantRepositoryClassificationSequenceError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_sequence",
                    message=str(error),
                ) from error
            except ValueError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_request",
                    message=str(error),
                ) from error

            if write_result.status == "idempotency_conflict":
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message=(
                        "Idempotency-Key was already used for a different "
                        "Launchplane request payload on this route."
                    ),
                )
            if write_result.status == "replayed" and write_result.idempotency_record is not None:
                payload = dict(write_result.idempotency_record.response_payload)
                payload["replayed"] = True
                payload["original_trace_id"] = write_result.idempotency_record.response_trace_id
                return TenantRepositoryClassificationApplyResponse.model_validate(payload)
            if write_result.status == "reservation_in_progress":
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_in_progress",
                    message="Tenant repository classification apply is already in progress.",
                )
            if write_result.status == "reconciliation_required":
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_reconciliation_required",
                    message=(
                        "Tenant repository classification apply requires reconciliation "
                        "before retry."
                    ),
                )
            if write_result.status == "written":
                return response
            raise dependencies.http_error(
                status_code=500,
                trace_id=trace_id,
                code="internal_error",
                message="Tenant repository classification apply returned an unknown result.",
            )
        else:
            try:
                read_store = require_tenant_repository_classification_read_store(record_store)
            except TypeError as error:
                raise dependencies.http_error(
                    status_code=503,
                    trace_id=trace_id,
                    code="database_storage_required",
                    message=str(error),
                ) from error

            try:
                result = apply_tenant_repository_classification(
                    store=read_store,
                    record=envelope.record,
                    expected_current_record_id=envelope.expected_current_record_id,
                    mode="dry_run",
                )
            except TenantRepositoryClassificationConflictError as error:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="classification_conflict",
                    message=str(error),
                ) from error
            except TenantRepositoryClassificationSequenceError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_sequence",
                    message=str(error),
                ) from error
            except ValueError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_request",
                    message=str(error),
                ) from error

            return TenantRepositoryClassificationApplyResponse(
                trace_id=trace_id,
                result=result,
            )

    async def apply_trusted_maintenance_policy_route(
        request: Request,
        envelope: TrustedMaintenancePolicyApplyEnvelope,
        identity: Annotated[
            LaunchplaneIdentity,
            Depends(dependencies.read_browser_mutation_identity),
        ],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> TrustedMaintenancePolicyApplyResponse:
        trace_id = dependencies.next_trace_id()

        if not isinstance(identity, GitHubHumanIdentity) or identity.github_id < 1:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Trusted-maintenance policy apply requires a browser GitHub human session."
                ),
            )

        if not dependencies.authorization_allows(
            identity=identity,
            action=TRUSTED_MAINTENANCE_POLICY_WRITE_ACTION,
            product=envelope.record.product,
            context=envelope.record.context,
            target=AuthorizationTarget(scope="context"),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller lacks permission to apply trusted-maintenance policies.",
            )

        if envelope.mode == "apply":
            normalized_idempotency_key = idempotency_key.strip()
            if not normalized_idempotency_key:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="idempotency_key_required",
                    message="Apply operation requires an Idempotency-Key header.",
                )

            if (
                not isinstance(record_store, PostgresRecordStore)
                or record_store.database_dialect_name != "postgresql"
            ):
                raise dependencies.http_error(
                    status_code=503,
                    trace_id=trace_id,
                    code="database_storage_required",
                    message=(
                        "Trusted-maintenance policy apply requires PostgreSQL-backed "
                        "Launchplane storage."
                    ),
                )

            store = record_store
            normalized_scope = idempotency_scope(identity)
            raw_payload = await request.json()
            payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
            applied_result = TrustedMaintenancePolicyApplyResult(
                status="applied",
                mode="apply",
                repository_id=envelope.record.repository_id,
                product=envelope.record.product,
                context=envelope.record.context,
                policy_revision=envelope.record.policy_revision,
                record_id=envelope.record.record_id,
                policy_digest=envelope.record.policy_digest,
                supersedes_record_id=envelope.record.supersedes_record_id,
                effective_at=envelope.record.effective_at,
            )
            replayed_result = applied_result.model_copy(update={"status": "replayed"})
            response = TrustedMaintenancePolicyApplyResponse(
                trace_id=trace_id,
                result=applied_result,
            )
            replay_response = TrustedMaintenancePolicyApplyResponse(
                trace_id=trace_id,
                result=replayed_result,
            )
            try:
                write_result = store.compare_and_write_trusted_maintenance_policy_record(
                    record=envelope.record,
                    expected_current_record_id=envelope.expected_current_record_id,
                    expected_current_policy_digest=envelope.expected_current_policy_digest,
                    mutation=DbOnlyMutationRequest(
                        scope=normalized_scope,
                        route_path=TRUSTED_MAINTENANCE_POLICY_APPLY_ROUTE,
                        idempotency_key=normalized_idempotency_key,
                        request_fingerprint=payload_fingerprint,
                        lease_owner=trace_id,
                        response_status_code=202,
                        response_trace_id=trace_id,
                        response_payload=response.model_dump(mode="json"),
                        replay_response_payload=replay_response.model_dump(mode="json"),
                    ),
                )
            except TrustedMaintenancePolicyConflictError as error:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="trusted_maintenance_policy_conflict",
                    message=str(error),
                ) from error
            except TrustedMaintenancePolicySequenceError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_sequence",
                    message=str(error),
                ) from error
            except ValueError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_request",
                    message=str(error),
                ) from error

            return _map_trusted_maintenance_policy_write_result(
                trace_id=trace_id,
                write_result=write_result,
            )

        try:
            read_store = require_trusted_maintenance_policy_read_store(record_store)
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        try:
            result = apply_trusted_maintenance_policy(
                store=read_store,
                record=envelope.record,
                expected_current_record_id=envelope.expected_current_record_id,
                expected_current_policy_digest=envelope.expected_current_policy_digest,
                mode="dry_run",
            )
        except TrustedMaintenancePolicyConflictError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="trusted_maintenance_policy_conflict",
                message=str(error),
            ) from error
        except TrustedMaintenancePolicySequenceError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_sequence",
                message=str(error),
            ) from error
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error

        return TrustedMaintenancePolicyApplyResponse(
            trace_id=trace_id,
            result=result,
        )

    async def apply_repository_human_role_policy_route(
        request: Request,
        envelope: RepositoryHumanRolePolicyApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> RepositoryHumanRolePolicyApplyResponse:
        trace_id = dependencies.next_trace_id()

        if isinstance(identity, TerminalAgentIdentity):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Terminal agent credentials cannot apply repository human role policies.",
            )

        if not dependencies.authorization_allows(
            identity=identity,
            action=REPOSITORY_HUMAN_ROLE_POLICY_WRITE_ACTION,
            product=envelope.record.product,
            context=envelope.record.context,
            target=AuthorizationTarget(scope="context"),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller lacks permission to apply repository human role policies.",
            )

        if envelope.mode == "apply":
            normalized_idempotency_key = idempotency_key.strip()
            if not normalized_idempotency_key:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="idempotency_key_required",
                    message="Apply operation requires an Idempotency-Key header.",
                )

            if (
                not isinstance(record_store, PostgresRecordStore)
                or record_store.database_dialect_name != "postgresql"
            ):
                raise dependencies.http_error(
                    status_code=503,
                    trace_id=trace_id,
                    code="database_storage_required",
                    message=(
                        "Repository human role-policy apply requires PostgreSQL-backed "
                        "Launchplane storage."
                    ),
                )

            store = record_store
            normalized_scope = idempotency_scope(identity)
            raw_payload = await request.json()
            payload_fingerprint = request_fingerprint(cast(dict[str, object], raw_payload))
            applied_result = RepositoryHumanRolePolicyApplyResult(
                status="applied",
                mode="apply",
                repository_id=envelope.record.repository_id,
                product=envelope.record.product,
                context=envelope.record.context,
                role_policy_revision=envelope.record.role_policy_revision,
                record_id=envelope.record.record_id,
                role_policy_digest=envelope.record.role_policy_digest,
                supersedes_record_id=envelope.record.supersedes_record_id,
                effective_at=envelope.record.effective_at,
            )
            replayed_result = applied_result.model_copy(update={"status": "replayed"})
            response = RepositoryHumanRolePolicyApplyResponse(
                trace_id=trace_id,
                result=applied_result,
            )
            replay_response = RepositoryHumanRolePolicyApplyResponse(
                trace_id=trace_id,
                result=replayed_result,
            )
            try:
                write_result = store.compare_and_write_repository_human_role_policy_record(
                    record=envelope.record,
                    expected_current_record_id=envelope.expected_current_record_id,
                    expected_current_role_policy_digest=(
                        envelope.expected_current_role_policy_digest
                    ),
                    mutation=DbOnlyMutationRequest(
                        scope=normalized_scope,
                        route_path=REPOSITORY_HUMAN_ROLE_POLICY_APPLY_ROUTE,
                        idempotency_key=normalized_idempotency_key,
                        request_fingerprint=payload_fingerprint,
                        lease_owner=trace_id,
                        response_status_code=202,
                        response_trace_id=trace_id,
                        response_payload=response.model_dump(mode="json"),
                        replay_response_payload=replay_response.model_dump(mode="json"),
                    ),
                )
            except RepositoryHumanRolePolicyConflictError as error:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="role_policy_conflict",
                    message=str(error),
                ) from error
            except RepositoryHumanRolePolicySequenceError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_sequence",
                    message=str(error),
                ) from error
            except ValueError as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_request",
                    message=str(error),
                ) from error

            if write_result.status == "idempotency_conflict":
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="idempotency_key_reused",
                    message=(
                        "Idempotency-Key was already used for a different "
                        "Launchplane request payload on this route."
                    ),
                )
            if write_result.status in {"replayed", "exact_replay"}:
                if write_result.idempotency_record is None:
                    raise dependencies.http_error(
                        status_code=500,
                        trace_id=trace_id,
                        code="internal_error",
                        message="Repository human role-policy replay missing stored response.",
                    )
                payload = dict(write_result.idempotency_record.response_payload)
                if write_result.status == "replayed":
                    payload["replayed"] = True
                    payload["original_trace_id"] = write_result.idempotency_record.response_trace_id
                return RepositoryHumanRolePolicyApplyResponse.model_validate(payload)
            if write_result.status == "reservation_in_progress":
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_in_progress",
                    message="Repository human role-policy apply is already in progress.",
                )
            if write_result.status == "reconciliation_required":
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="mutation_reconciliation_required",
                    message=(
                        "Repository human role-policy apply requires reconciliation before retry."
                    ),
                )
            if write_result.status == "written":
                return response
            raise dependencies.http_error(
                status_code=500,
                trace_id=trace_id,
                code="internal_error",
                message="Repository human role-policy apply returned an unknown result.",
            )

        try:
            read_store = require_repository_human_role_policy_read_store(record_store)
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error

        try:
            result = apply_repository_human_role_policy(
                store=read_store,
                record=envelope.record,
                expected_current_record_id=envelope.expected_current_record_id,
                expected_current_role_policy_digest=(envelope.expected_current_role_policy_digest),
                mode="dry_run",
            )
        except RepositoryHumanRolePolicyConflictError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="role_policy_conflict",
                message=str(error),
            ) from error
        except RepositoryHumanRolePolicySequenceError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_sequence",
                message=str(error),
            ) from error
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error

        return RepositoryHumanRolePolicyApplyResponse(
            trace_id=trace_id,
            result=result,
        )

    app.add_api_route(
        TENANT_ADMISSION_CONTROLLER_RUN_ONCE_ROUTE,
        run_tenant_admission_controller_once_route,
        methods=["POST"],
        status_code=202,
        response_model=TenantAdmissionControllerRunOnceResponse,
        operation_id="run_tenant_admission_controller_once",
        summary="Re-evaluate and merge one exact admitted tenant pull request",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        TENANT_ADMISSION_STATUS_RECONCILE_ROUTE,
        reconcile_tenant_admission_status_route,
        methods=["POST"],
        status_code=202,
        response_model=TenantAdmissionStatusReconcileResponse,
        operation_id="reconcile_tenant_admission_status",
        summary="Recompute and project exact-head tenant admission status",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        TENANT_TECHNICAL_HUMAN_WAIVER_APPLY_ROUTE,
        apply_tenant_technical_human_waiver_route,
        methods=["POST"],
        status_code=202,
        response_model=TenantTechnicalHumanWaiverApplyResponse,
        operation_id="apply_tenant_technical_human_waiver",
        summary="Apply or dry-run a tenant technical human waiver event",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        TENANT_REPOSITORY_CLASSIFICATION_APPLY_ROUTE,
        apply_tenant_repository_classification_route,
        methods=["POST"],
        response_model=TenantRepositoryClassificationApplyResponse,
        operation_id="apply_tenant_repository_classification",
        summary="Apply or dry-run a tenant repository classification record",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        REPOSITORY_HUMAN_ROLE_POLICY_APPLY_ROUTE,
        apply_repository_human_role_policy_route,
        methods=["POST"],
        status_code=202,
        response_model=RepositoryHumanRolePolicyApplyResponse,
        operation_id="apply_repository_human_role_policy",
        summary="Apply or dry-run a repository human role-policy record",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        TRUSTED_MAINTENANCE_POLICY_APPLY_ROUTE,
        apply_trusted_maintenance_policy_route,
        methods=["POST"],
        status_code=202,
        response_model=TrustedMaintenancePolicyApplyResponse,
        operation_id="apply_trusted_maintenance_policy",
        summary="Apply or dry-run a trusted-maintenance policy record",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
