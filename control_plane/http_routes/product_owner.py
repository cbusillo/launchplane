from collections.abc import Callable
from dataclasses import dataclass
from typing import Annotated, Literal

from fastapi import Depends, Query
from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.product_owner import (
    PRODUCT_OWNER_POLICY_READ_ACTION,
    PRODUCT_OWNER_POLICY_WRITE_ACTION,
    PRODUCT_OWNER_REQUIREMENT_READ_ACTION,
    PRODUCT_OWNER_REQUIREMENT_WRITE_ACTION,
    PRODUCT_OWNER_ROUTING_READ_ACTION,
    PRODUCT_OWNER_ROUTING_WRITE_ACTION,
    PRODUCT_OWNER_SHADOW_READ_ACTION,
    ProductOwnerActionContext,
    ProductOwnerActorIdentity,
    ProductOwnerPolicyRecord,
    ProductOwnerRequirementRecord,
    ProductOwnerRoutingRecord,
    ProductOwnerShadowEvaluation,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    AuthorizationAllows,
    HttpErrorFactory,
    ReadRouteDependencies,
)
from control_plane.product_owner_service import (
    ProductOwnerPolicyApplyResult,
    ProductOwnerPolicyConflictError,
    ProductOwnerPolicySequenceError,
    ProductOwnerReadModel,
    ProductOwnerRequirementApplyResult,
    ProductOwnerRequirementConflictError,
    ProductOwnerRequirementSequenceError,
    ProductOwnerRoutingApplyResult,
    ProductOwnerRoutingConflictError,
    ProductOwnerRoutingSequenceError,
    apply_product_owner_policy,
    apply_product_owner_requirement,
    apply_product_owner_routing,
    evaluate_product_owner_shadow_authority,
    get_product_owner_read_model,
    require_product_owner_policy_read_store,
    require_product_owner_policy_store,
    require_product_owner_requirement_read_store,
    require_product_owner_requirement_store,
    require_product_owner_routing_read_store,
    require_product_owner_routing_store,
)
from control_plane.service_auth import AuthorizationTarget, GitHubHumanIdentity, LaunchplaneIdentity


PRODUCT_OWNER_POLICY_READ_ROUTE = "/v1/product-owner/policy"
PRODUCT_OWNER_REQUIREMENT_READ_ROUTE = "/v1/product-owner/requirement"
PRODUCT_OWNER_ROUTING_READ_ROUTE = "/v1/product-owner/routing"
PRODUCT_OWNER_SHADOW_EVALUATION_ROUTE = "/v1/product-owner/shadow-evaluation"
PRODUCT_OWNER_POLICY_APPLY_ROUTE = "/v1/product-owner/policies/apply"
PRODUCT_OWNER_REQUIREMENT_APPLY_ROUTE = "/v1/product-owner/requirements/apply"
PRODUCT_OWNER_ROUTING_APPLY_ROUTE = "/v1/product-owner/routing/apply"


@dataclass(frozen=True, slots=True)
class ProductOwnerWriteRouteDependencies:
    read_write_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: AuthorizationAllows
    http_error: HttpErrorFactory
    error_response_model: type[BaseModel]


class ProductOwnerReadResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    read_model: ProductOwnerReadModel


class ProductOwnerShadowEvaluationResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    evaluation: ProductOwnerShadowEvaluation


class ProductOwnerPolicyApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry_run", "apply"] = "apply"
    expected_current_record_id: str = ""
    expected_current_policy_digest: str = ""
    record: ProductOwnerPolicyRecord


class ProductOwnerRequirementApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry_run", "apply"] = "apply"
    expected_current_record_id: str = ""
    expected_current_requirement_digest: str = ""
    record: ProductOwnerRequirementRecord


class ProductOwnerRoutingApplyEnvelope(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    mode: Literal["dry_run", "apply"] = "apply"
    expected_current_record_id: str = ""
    expected_current_routing_digest: str = ""
    record: ProductOwnerRoutingRecord


class ProductOwnerPolicyApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    result: ProductOwnerPolicyApplyResult


class ProductOwnerRequirementApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    result: ProductOwnerRequirementApplyResult


class ProductOwnerRoutingApplyResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    trace_id: str
    result: ProductOwnerRoutingApplyResult


def register_product_owner_read_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ReadRouteDependencies,
) -> None:
    def read_product_owner(
        action: str,
        product: str,
        system: str,
        identity: LaunchplaneIdentity,
        record_store: object,
    ) -> ProductOwnerReadResponse:
        trace_id = dependencies.next_trace_id()
        try:
            normalized_product, normalized_system = _normalize_scope(
                product=product,
                system=system,
            )
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error
        if not dependencies.authorization_allows(
            identity=identity,
            action=action,
            product=normalized_product,
            context=normalized_system,
            target=AuthorizationTarget(scope="context"),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot read product Owner policy records.",
            )
        try:
            read_model = get_product_owner_read_model(
                store=record_store,
                product=normalized_product,
                system=normalized_system,
            )
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        return ProductOwnerReadResponse(trace_id=trace_id, read_model=read_model)

    def read_policy(
        product: Annotated[str, Query(...)],
        system: Annotated[str, Query(...)],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> ProductOwnerReadResponse:
        return read_product_owner(
            PRODUCT_OWNER_POLICY_READ_ACTION,
            product,
            system,
            identity,
            record_store,
        )

    def read_requirement(
        product: Annotated[str, Query(...)],
        system: Annotated[str, Query(...)],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> ProductOwnerReadResponse:
        return read_product_owner(
            PRODUCT_OWNER_REQUIREMENT_READ_ACTION,
            product,
            system,
            identity,
            record_store,
        )

    def read_routing(
        product: Annotated[str, Query(...)],
        system: Annotated[str, Query(...)],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> ProductOwnerReadResponse:
        return read_product_owner(
            PRODUCT_OWNER_ROUTING_READ_ACTION,
            product,
            system,
            identity,
            record_store,
        )

    def read_shadow_evaluation(
        product: Annotated[str, Query(...)],
        system: Annotated[str, Query(...)],
        repository_id: Annotated[str, Query(...)],
        environment: Annotated[str, Query(...)],
        action: Annotated[str, Query(...)],
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        claimed_policy_revision: Annotated[int | None, Query(ge=1)] = None,
        claimed_policy_digest: Annotated[str, Query()] = "",
        claimed_requirement_revision: Annotated[int | None, Query(ge=1)] = None,
        claimed_requirement_digest: Annotated[str, Query()] = "",
    ) -> ProductOwnerShadowEvaluationResponse:
        trace_id = dependencies.next_trace_id()
        try:
            context = ProductOwnerActionContext(
                product=product,
                system=system,
                repository_id=repository_id,
                environment=environment,
                action=action,
            )
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message=str(error),
            ) from error
        if not dependencies.authorization_allows(
            identity=identity,
            action=PRODUCT_OWNER_SHADOW_READ_ACTION,
            product=context.product,
            context=context.system,
            target=AuthorizationTarget(scope="context"),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Caller cannot read product Owner shadow evaluation.",
            )
        try:
            actor = _actor_identity(identity)
        except (TypeError, ValueError) as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_owner_actor_identity_required",
                message="Product Owner shadow evaluation requires a human GitHub identity.",
            ) from error
        try:
            policies = require_product_owner_policy_read_store(
                record_store
            ).list_product_owner_policy_records(
                product=context.product,
                system=context.system,
            )
            requirements = require_product_owner_requirement_read_store(
                record_store
            ).list_product_owner_requirement_records(
                product=context.product,
                system=context.system,
            )
            routings = require_product_owner_routing_read_store(
                record_store
            ).list_product_owner_routing_records(
                product=context.product,
                system=context.system,
            )
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Product Owner shadow storage is unavailable.",
            ) from error
        evaluation = evaluate_product_owner_shadow_authority(
            context=context,
            actor=actor,
            policies=policies,
            requirements=requirements,
            routings=routings,
            claimed_policy_revision=claimed_policy_revision,
            claimed_policy_digest=claimed_policy_digest,
            claimed_requirement_revision=claimed_requirement_revision,
            claimed_requirement_digest=claimed_requirement_digest,
        )
        return ProductOwnerShadowEvaluationResponse(trace_id=trace_id, evaluation=evaluation)

    errors = {
        400: {"model": dependencies.error_response_model},
        401: {"model": dependencies.error_response_model},
        403: {"model": dependencies.error_response_model},
        503: {"model": dependencies.error_response_model},
    }
    for path, endpoint, operation_id, summary in (
        (
            PRODUCT_OWNER_POLICY_READ_ROUTE,
            read_policy,
            "read_product_owner_policy",
            "Read the shadow product Owner policy bundle",
        ),
        (
            PRODUCT_OWNER_REQUIREMENT_READ_ROUTE,
            read_requirement,
            "read_product_owner_requirement",
            "Read the shadow product Owner requirement bundle",
        ),
        (
            PRODUCT_OWNER_ROUTING_READ_ROUTE,
            read_routing,
            "read_product_owner_routing",
            "Read non-authoritative product Owner routing",
        ),
    ):
        app.add_api_route(
            path,
            endpoint,
            methods=["GET"],
            response_model=ProductOwnerReadResponse,
            operation_id=operation_id,
            summary=summary,
            responses=errors,
        )
    app.add_api_route(
        PRODUCT_OWNER_SHADOW_EVALUATION_ROUTE,
        read_shadow_evaluation,
        methods=["GET"],
        response_model=ProductOwnerShadowEvaluationResponse,
        operation_id="read_product_owner_shadow_evaluation",
        summary="Evaluate product Owner authority without changing enforcement",
        responses=errors,
    )


def register_product_owner_write_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: ProductOwnerWriteRouteDependencies,
) -> None:
    async def apply_policy(
        envelope: ProductOwnerPolicyApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> ProductOwnerPolicyApplyResponse:
        trace_id = dependencies.next_trace_id()
        _require_write_authorization(
            dependencies=dependencies,
            identity=identity,
            action=PRODUCT_OWNER_POLICY_WRITE_ACTION,
            product=envelope.record.product,
            system=envelope.record.system,
            trace_id=trace_id,
        )
        try:
            result = apply_product_owner_policy(
                store=require_product_owner_policy_store(record_store),
                record=envelope.record,
                expected_current_record_id=envelope.expected_current_record_id,
                expected_current_policy_digest=envelope.expected_current_policy_digest,
                mode=envelope.mode,
            )
        except (ProductOwnerPolicyConflictError, ProductOwnerPolicySequenceError) as error:
            raise _apply_error(dependencies, trace_id, error) from error
        return ProductOwnerPolicyApplyResponse(trace_id=trace_id, result=result)

    async def apply_requirement(
        envelope: ProductOwnerRequirementApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> ProductOwnerRequirementApplyResponse:
        trace_id = dependencies.next_trace_id()
        _require_write_authorization(
            dependencies=dependencies,
            identity=identity,
            action=PRODUCT_OWNER_REQUIREMENT_WRITE_ACTION,
            product=envelope.record.product,
            system=envelope.record.system,
            trace_id=trace_id,
        )
        try:
            result = apply_product_owner_requirement(
                store=require_product_owner_requirement_store(record_store),
                record=envelope.record,
                expected_current_record_id=envelope.expected_current_record_id,
                expected_current_requirement_digest=(envelope.expected_current_requirement_digest),
                mode=envelope.mode,
            )
        except (
            ProductOwnerRequirementConflictError,
            ProductOwnerRequirementSequenceError,
        ) as error:
            raise _apply_error(dependencies, trace_id, error) from error
        return ProductOwnerRequirementApplyResponse(trace_id=trace_id, result=result)

    async def apply_routing(
        envelope: ProductOwnerRoutingApplyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> ProductOwnerRoutingApplyResponse:
        trace_id = dependencies.next_trace_id()
        _require_write_authorization(
            dependencies=dependencies,
            identity=identity,
            action=PRODUCT_OWNER_ROUTING_WRITE_ACTION,
            product=envelope.record.product,
            system=envelope.record.system,
            trace_id=trace_id,
        )
        try:
            result = apply_product_owner_routing(
                store=require_product_owner_routing_store(record_store),
                record=envelope.record,
                expected_current_record_id=envelope.expected_current_record_id,
                expected_current_routing_digest=envelope.expected_current_routing_digest,
                mode=envelope.mode,
            )
        except (ProductOwnerRoutingConflictError, ProductOwnerRoutingSequenceError) as error:
            raise _apply_error(dependencies, trace_id, error) from error
        return ProductOwnerRoutingApplyResponse(trace_id=trace_id, result=result)

    errors = {
        400: {"model": dependencies.error_response_model},
        401: {"model": dependencies.error_response_model},
        403: {"model": dependencies.error_response_model},
        409: {"model": dependencies.error_response_model},
        503: {"model": dependencies.error_response_model},
    }
    for path, endpoint, response_model, operation_id, summary in (
        (
            PRODUCT_OWNER_POLICY_APPLY_ROUTE,
            apply_policy,
            ProductOwnerPolicyApplyResponse,
            "apply_product_owner_policy",
            "Apply or dry-run a product Owner policy revision",
        ),
        (
            PRODUCT_OWNER_REQUIREMENT_APPLY_ROUTE,
            apply_requirement,
            ProductOwnerRequirementApplyResponse,
            "apply_product_owner_requirement",
            "Apply or dry-run a product Owner requirement revision",
        ),
        (
            PRODUCT_OWNER_ROUTING_APPLY_ROUTE,
            apply_routing,
            ProductOwnerRoutingApplyResponse,
            "apply_product_owner_routing",
            "Apply or dry-run non-authoritative product Owner routing",
        ),
    ):
        app.add_api_route(
            path,
            endpoint,
            methods=["POST"],
            status_code=202,
            response_model=response_model,
            operation_id=operation_id,
            summary=summary,
            responses=errors,
        )


def _normalize_scope(*, product: str, system: str) -> tuple[str, str]:
    normalized_product = product.strip()
    normalized_system = system.strip()
    if not normalized_product or not normalized_system:
        raise ValueError("Product Owner scope requires product and system.")
    return normalized_product, normalized_system


def _actor_identity(identity: LaunchplaneIdentity) -> ProductOwnerActorIdentity:
    if not isinstance(identity, GitHubHumanIdentity):
        raise TypeError("Product Owner actors must be human GitHub identities.")
    return ProductOwnerActorIdentity(
        provider="github",
        provider_subject_id=str(identity.github_id),
    )


def _require_write_authorization(
    *,
    dependencies: ProductOwnerWriteRouteDependencies,
    identity: LaunchplaneIdentity,
    action: str,
    product: str,
    system: str,
    trace_id: str,
) -> None:
    if dependencies.authorization_allows(
        identity=identity,
        action=action,
        product=product,
        context=system,
        target=AuthorizationTarget(scope="context"),
    ):
        return
    raise dependencies.http_error(
        status_code=403,
        trace_id=trace_id,
        code="authorization_denied",
        message="Caller cannot administer product Owner records.",
    )


def _apply_error(
    dependencies: ProductOwnerWriteRouteDependencies,
    trace_id: str,
    error: ValueError,
) -> Exception:
    is_sequence_error = isinstance(
        error,
        (
            ProductOwnerPolicySequenceError,
            ProductOwnerRequirementSequenceError,
            ProductOwnerRoutingSequenceError,
        ),
    )
    return dependencies.http_error(
        status_code=409,
        trace_id=trace_id,
        code=(
            "product_owner_revision_sequence_error"
            if is_sequence_error
            else "product_owner_revision_conflict"
        ),
        message=str(error),
    )
