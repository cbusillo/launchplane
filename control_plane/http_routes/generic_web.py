import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path as FilePath
from typing import Annotated, Any, Protocol, cast

import click
from fastapi import Depends, Header, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException

from control_plane.contracts.generic_web_deploy_recovery import (
    GenericWebDeployRecoveryDryRunResponse,
)
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.drivers import native_routes
from control_plane.generic_web_deploy_http import (
    GENERIC_WEB_DEPLOY_ROUTE as _GENERIC_WEB_DEPLOY_ROUTE,
    GenericWebDeployEnvelope,
    GenericWebDeployProductMismatchError,
    GenericWebDeployRouteDependencyError,
    resolve_generic_web_deploy_lane,
)
from control_plane.generic_web_deploy_provider_adapter import (
    _GenericWebDeployProviderMutationAdapter,
)
from control_plane.generic_web_deploy_recovery_http import (
    GENERIC_WEB_DEPLOY_RECOVERY_DRY_RUN_ROUTE as _GENERIC_WEB_DEPLOY_RECOVERY_DRY_RUN_ROUTE,
    GenericWebDeployRecoveryDependencies,
    build_generic_web_deploy_recovery_dry_run_handler,
)
from control_plane.generic_web_preview_http import (
    GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE as _GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE,
    GENERIC_WEB_PREVIEW_DESTROY_ROUTE as _GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
    GENERIC_WEB_PREVIEW_INVENTORY_ROUTE as _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE,
    GENERIC_WEB_PREVIEW_READINESS_ROUTE as _GENERIC_WEB_PREVIEW_READINESS_ROUTE,
    GENERIC_WEB_PREVIEW_REFRESH_ROUTE as _GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
    GenericWebPreviewDesiredStateEnvelope,
    GenericWebPreviewDestroyEnvelope,
    GenericWebPreviewInventoryEnvelope,
    GenericWebPreviewProductMismatchError,
    GenericWebPreviewReadinessEnvelope,
    GenericWebPreviewRefreshEnvelope,
    GenericWebPreviewRouteDependencyError,
    apply_generic_web_preview_destroy_result,
    apply_generic_web_preview_inventory_result,
    apply_generic_web_preview_readiness_result,
    apply_generic_web_preview_refresh_result,
    resolve_generic_web_preview_desired_state_profile,
    resolve_generic_web_preview_profile,
    should_store_generic_web_preview_idempotency,
)
from control_plane.generic_web_promotion_http import (
    GENERIC_WEB_PROD_PROMOTION_ROUTE as _GENERIC_WEB_PROD_PROMOTION_ROUTE,
    GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE as _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
    GenericWebProdPromotionEnvelope,
    GenericWebProdPromotionResponse,
    GenericWebProdPromotionResponseResult,
    GenericWebPromotionProductMismatchError,
    GenericWebPromotionRouteDependencyError,
    GenericWebPromotionWorkflowEnvelope,
    GenericWebPromotionWorkflowResponse,
    GenericWebPromotionWorkflowResponseResult,
    dispatch_generic_web_promotion_workflow_result,
    execute_generic_web_prod_promotion_result,
    resolve_generic_web_promotion_destination_lane,
    resolve_generic_web_promotion_workflow_lane,
    should_store_generic_web_promotion_idempotency,
    validate_generic_web_prod_promotion_lanes,
)
from control_plane.generic_web_rollback_http import (
    GENERIC_WEB_ROLLBACK_PLAN_ROUTE as _GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
    GENERIC_WEB_ROLLBACK_ROUTE as _GENERIC_WEB_ROLLBACK_ROUTE,
    GenericWebRollbackEnvelope,
    GenericWebRollbackPlanEnvelope,
    GenericWebRollbackProductMismatchError,
    GenericWebRollbackRouteDependencyError,
    execute_generic_web_rollback_plan_result,
    execute_generic_web_rollback_result,
    resolve_generic_web_rollback_lane,
    should_store_generic_web_rollback_idempotency,
)
from control_plane.generic_web_verification_http import (
    GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE as _GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
    GENERIC_WEB_STABLE_VERIFICATION_ROUTE as _GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
    GenericWebPreviewVerificationEnvelope,
    GenericWebStableVerificationEnvelope,
    GenericWebVerificationProductMismatchError,
    GenericWebVerificationRouteDependencyError,
    apply_generic_web_preview_verification_result,
    apply_generic_web_stable_verification_result,
    generic_web_verification_response_records,
    resolve_generic_web_preview_verification_profile,
    resolve_generic_web_stable_verification_lane,
    should_store_generic_web_verification_idempotency,
)
from control_plane.http_routes.mutation_support import (
    AcceptedEvidenceResponse,
    accepted_evidence_response,
    idempotency_scope,
    provider_operation_response_payload as _provider_operation_response_payload,
)
from control_plane.http_routes.support import (
    ApiRouteRegistrar,
    AuthorizationAllows,
    HttpErrorFactory,
)
from control_plane.manager_preview_approval_github_webhook import (
    invalidate_manager_preview_approval_for_pr_best_effort,
    reconcile_manager_preview_approval_for_pr_best_effort,
)
from control_plane.product_promotion_http import (
    build_product_promotion_status,
    product_promotion_intent_matches,
)
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderOperationLease,
    provider_operation_title,
)
from control_plane.service_auth import (
    GitHubHumanIdentity,
    LaunchplaneIdentity,
    LocalAdminIdentity,
    LocalOperatorIdentity,
    TerminalAgentIdentity,
)
from control_plane.storage.postgres import OutboxWithIdempotencyRequest, PostgresRecordStore
from control_plane.workflows.generic_web_deploy import (
    normalize_generic_web_artifact_id,
)
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebDeployProvider,
    GenericWebResolvedDeployTarget,
    build_generic_web_provider_reconciliation_key,
    build_generic_web_provider_target_key,
    default_generic_web_deploy_provider,
)
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewProfileStore,
    discover_generic_web_preview_desired_state,
    preview_pr_number_from_slug,
    resolve_generic_web_preview_slug,
)
from control_plane.workflows.ship import utc_now_timestamp


__all__ = [
    "GenericWebWriteRouteDependencies",
    "GenericWebWriteRouteHandlers",
    "build_generic_web_write_route_handlers",
    "register_generic_web_rollback_write_routes",
    "register_generic_web_write_routes",
]


class _GenericWebProdPromotionProviderMutationAdapter:
    def __init__(
        self,
        *,
        control_plane_root: FilePath,
        record_store: object,
        promotion_request: GenericWebProdPromotionEnvelope,
        profile: LaunchplaneProductProfileRecord,
        lane: ProductLaneProfile,
        trace_id: str,
        validate_before_effect: Callable[[GenericWebResolvedDeployTarget], None],
    ) -> None:
        self._control_plane_root = control_plane_root
        self._record_store = record_store
        self._promotion_request = promotion_request
        self._profile = profile
        self._lane = lane
        self._trace_id = trace_id
        self._validate_before_effect = validate_before_effect
        self._deploy_provider: GenericWebDeployProvider = default_generic_web_deploy_provider()
        self._resolved_deploy_target: GenericWebResolvedDeployTarget | None = None

    def resolve_deploy_target(self) -> GenericWebResolvedDeployTarget:
        if self._resolved_deploy_target is None:
            promotion = self._promotion_request.promotion
            self._resolved_deploy_target = self._deploy_provider.resolve_deploy_target(
                control_plane_root=self._control_plane_root,
                request_artifact_id=promotion.artifact_id,
                request_source_git_ref=promotion.source_git_ref,
                request_timeout_seconds=promotion.timeout_seconds,
                request_no_cache=promotion.no_cache,
                record_store=self._record_store,
                profile=self._profile,
                lane=self._lane,
                normalized_artifact_id=normalize_generic_web_artifact_id(
                    profile=self._profile,
                    artifact_id=promotion.artifact_id,
                ),
                fallback_target_name=f"{self._profile.product}-{self._lane.instance}",
                request_deploy_reference=promotion.deploy_reference,
            )
        return self._resolved_deploy_target

    def reconciliation_key(self) -> str:
        return build_generic_web_provider_reconciliation_key(
            self.resolve_deploy_target(),
            product=self._profile.product,
        )

    def target_key(self) -> str:
        return build_generic_web_provider_target_key(self.resolve_deploy_target())

    def observe(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
    ) -> ProviderObservation:
        del provider_operation_key, provider_effect_phase, reconciliation_key
        return ProviderObservation(outcome="unknown")

    def _deployment_record_id(self, provider_operation_key: str) -> str:
        operation_digest = hashlib.sha256(provider_operation_key.encode("utf-8")).hexdigest()[:24]
        return (
            f"deployment-provider-operation-{operation_digest}-"
            f"{self._lane.context}-{self._lane.instance}"
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        provider_effect_attempted = False

        def checkpoint_provider_effect(phase: str) -> None:
            nonlocal provider_effect_attempted
            lease.checkpoint_effect(phase)
            provider_effect_attempted = True

        try:
            self._validate_before_effect(self.resolve_deploy_target())
            records, result = execute_generic_web_prod_promotion_result(
                control_plane_root=self._control_plane_root,
                record_store=self._record_store,
                request=self._promotion_request,
                deploy_provider=self._deploy_provider,
                resolved_deploy_target=self.resolve_deploy_target(),
                provider_operation_title=provider_operation_title(provider_operation_key),
                deployment_record_id=self._deployment_record_id(provider_operation_key),
                provider_effect_checkpoint=checkpoint_provider_effect,
            )
        except StarletteHTTPException as error:
            raise ProviderMutationRejectedError(error) from error
        except (FileNotFoundError, ValueError, click.ClickException) as error:
            if provider_effect_attempted:
                raise ProviderMutationUnknownError(str(error)) from error
            raise ProviderMutationRejectedError(error) from error
        return ProviderMutationOutcome(
            response_status_code=202,
            response_payload=_provider_operation_response_payload(
                trace_id=self._trace_id,
                records=records.model_dump(mode="json"),
                result=result.model_dump(mode="json"),
            ),
            durable=should_store_generic_web_promotion_idempotency(result),
            provider_effect_performed=provider_effect_attempted,
        )


class PreviewDesiredStateWriteStore(Protocol):
    def write_preview_desired_state_record(
        self,
        record: PreviewDesiredStateRecord,
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class GenericWebWriteRouteDependencies:
    read_write_identity: Callable[..., LaunchplaneIdentity]
    read_browser_mutation_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: AuthorizationAllows
    http_error: HttpErrorFactory
    error_response_model: type[BaseModel]
    control_plane_root: FilePath
    driver_dependency_response: Callable[..., JSONResponse]
    replay_apply_idempotency: Callable[
        ..., Awaitable[tuple[str, str, AcceptedEvidenceResponse | None]]
    ]
    store_apply_idempotency: Callable[..., None]
    build_apply_idempotency_record: Callable[..., LaunchplaneIdempotencyRecord]
    run_provider_mutation: Callable[..., Awaitable[AcceptedEvidenceResponse]]
    idempotency_request_fingerprint: Callable[..., str]
    require_preview_desired_state_write_store: Callable[[object], PreviewDesiredStateWriteStore]
    openapi_model_schema: Callable[..., dict[str, Any]]


@dataclass(frozen=True, slots=True)
class GenericWebWriteRouteHandlers:
    apply_generic_web_preview_desired_state: Callable[..., Any]
    apply_generic_web_preview_inventory: Callable[..., Any]
    apply_generic_web_preview_readiness: Callable[..., Any]
    apply_generic_web_preview_refresh: Callable[..., Any]
    apply_generic_web_preview_destroy: Callable[..., Any]
    apply_generic_web_deploy: Callable[..., Any]
    dry_run_generic_web_deploy_recovery: Callable[..., Any]
    apply_generic_web_prod_promotion: Callable[..., Any]
    dispatch_generic_web_prod_promotion_workflow: Callable[..., Any]
    apply_generic_web_stable_verification: Callable[..., Any]
    apply_generic_web_preview_verification: Callable[..., Any]
    write_generic_web_rollback_plan: Callable[..., Any]
    write_generic_web_rollback: Callable[..., Any]


def _generic_web_preview_refresh_lock(
    *,
    request: Request,
    profile: LaunchplaneProductProfileRecord,
    refresh_request: GenericWebPreviewRefreshEnvelope,
) -> tuple[str, asyncio.Lock]:
    preview_slug = resolve_generic_web_preview_slug(
        profile=profile,
        preview_slug=refresh_request.refresh.preview_slug,
        anchor_pr_number=refresh_request.refresh.anchor_pr_number,
        label="Generic web preview refresh",
    )
    lock_key = f"{profile.product}:{profile.preview.context}:{preview_slug}"
    locks = getattr(request.app.state, "generic_web_preview_refresh_locks", None)
    if locks is None:
        locks = {}
        request.app.state.generic_web_preview_refresh_locks = locks
    return lock_key, locks.setdefault(lock_key, asyncio.Lock())


def build_generic_web_write_route_handlers(
    *,
    dependencies: GenericWebWriteRouteDependencies,
) -> GenericWebWriteRouteHandlers:
    dry_run_generic_web_deploy_recovery = build_generic_web_deploy_recovery_dry_run_handler(
        dependencies=GenericWebDeployRecoveryDependencies(
            read_write_identity=dependencies.read_write_identity,
            get_record_store=dependencies.get_record_store,
            next_trace_id=dependencies.next_trace_id,
            authorization_allows=dependencies.authorization_allows,
            http_error=dependencies.http_error,
            control_plane_root=dependencies.control_plane_root,
            idempotency_request_fingerprint=dependencies.idempotency_request_fingerprint,
        )
    )

    def manager_preview_pr_number(
        *,
        profile: LaunchplaneProductProfileRecord,
        anchor_pr_number: int | None,
        preview_slug: str,
    ) -> int | None:
        if anchor_pr_number is not None:
            return anchor_pr_number
        return preview_pr_number_from_slug(
            preview_slug=preview_slug,
            slug_template=profile.preview.slug_template,
        )

    def require_product_promotion_intent(
        *,
        record_store: object,
        profile: LaunchplaneProductProfileRecord,
        lane: ProductLaneProfile,
        promotion_request: GenericWebProdPromotionEnvelope,
        idempotency_key: str,
        trace_id: str,
    ) -> None:
        intent_id = promotion_request.promotion.promotion_intent_id.strip()
        if not isinstance(record_store, PostgresRecordStore) or not intent_id:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="promotion_intent_required",
                message="Live generic-web promotion requires a product-owned promotion intent.",
            )
        if idempotency_key.strip() != intent_id:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="promotion_intent_idempotency_mismatch",
                message="Live promotion must use its promotion intent as Idempotency-Key.",
            )
        try:
            intent_record = record_store.read_outbox_delivery_record(intent_id)
            current_profile, current_lane, status = build_product_promotion_status(
                record_store=record_store,
                product=profile.product,
                destination_environment=lane.instance,
                action_allowed=lambda _action, _product, _context, _instances: True,
                workflow_credentials_ready=lambda _context: True,
            )
        except (AttributeError, FileNotFoundError, ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="promotion_intent_invalid",
                message="Product-owned promotion intent is unavailable or stale.",
            ) from error
        if (
            current_profile != profile
            or current_lane != lane
            or not status.workflow_live.enabled
            or not product_promotion_intent_matches(
                record=intent_record,
                profile=current_profile,
                status=status,
                request=promotion_request.promotion,
            )
        ):
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="promotion_intent_invalid",
                message="Product-owned promotion intent does not match current evidence.",
            )

    def require_product_promotion_target_snapshot(
        *,
        record_store: object,
        lane: ProductLaneProfile,
        resolved_deploy_target: GenericWebResolvedDeployTarget,
        trace_id: str,
    ) -> None:
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Live generic-web promotion requires Launchplane database storage.",
            )
        try:
            current_target = record_store.read_provider_target_record(
                context_name=lane.context,
                instance_name=lane.instance,
            )
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="promotion_target_changed",
                message="The reviewed production provider target is no longer available.",
            ) from error
        expected_target = resolved_deploy_target.deployed_target
        resolved_target = resolved_deploy_target.resolved_target
        if (
            expected_target is None
            or current_target.to_deployed_target_reference() != expected_target
            or resolved_target.target_id != expected_target.target_id
            or resolved_target.target_name != expected_target.display_name
            or resolved_target.target_type
            != (expected_target.provider_target_type or expected_target.target_category)
        ):
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="promotion_target_changed",
                message="The production provider target changed before promotion execution.",
            )

    def require_current_manager_preview_approval(
        *,
        record_store: object,
        profile: LaunchplaneProductProfileRecord,
        lane: ProductLaneProfile,
        trace_id: str,
    ) -> None:
        try:
            current_profile, current_lane, status = build_product_promotion_status(
                record_store=record_store,
                product=profile.product,
                destination_environment=lane.instance,
                action_allowed=lambda _action, _product, _context, _instances: True,
                workflow_credentials_ready=lambda _context: True,
            )
        except (AttributeError, FileNotFoundError, ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="manager_preview_approval_unavailable",
                message="Manager preview approval could not be evaluated for live promotion.",
            ) from error
        decision = status.manager_preview_approval
        if (
            current_profile != profile
            or current_lane != lane
            or (decision is not None and decision.status != "approved")
        ):
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="manager_preview_approval_required",
                message=(
                    "Live promotion requires current manager approval for the exact testing "
                    "artifact and serving preview."
                ),
            )

    async def write_generic_web_rollback_plan(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = dependencies.next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            rollback_request = GenericWebRollbackPlanEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            _profile, lane = resolve_generic_web_rollback_lane(
                record_store=record_store,
                product=rollback_request.product,
                route_path=_GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
                instance=rollback_request.rollback_plan.instance,
            )
        except GenericWebRollbackRouteDependencyError:
            return dependencies.driver_dependency_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
            )
        except GenericWebRollbackProductMismatchError as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not native_routes._native_driver_route_authorization_allows(
            endpoint=write_generic_web_rollback_plan,
            authorization_allows=dependencies.authorization_allows,
            identity=identity,
            product=rollback_request.product,
            context=lane.context,
            instances=(lane.instance,),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot plan the generic web prod rollback"
                    " for the requested product/context."
                ),
            )
        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await dependencies.replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response
        try:
            records, driver_result = execute_generic_web_rollback_plan_result(
                record_store=record_store,
                request=rollback_request,
            )
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_GENERIC_WEB_ROLLBACK_PLAN_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={key: str(value) for key, value in records.items()},
            result=driver_result,
        )
        if should_store_generic_web_rollback_idempotency(driver_result):
            dependencies.store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def write_generic_web_rollback(
        request: Request,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = dependencies.next_trace_id()
        try:
            raw_payload = await request.json()
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        if not isinstance(raw_payload, dict):
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            )
        try:
            rollback_request = GenericWebRollbackEnvelope.model_validate(raw_payload)
        except ValidationError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request payload failed validation.",
            ) from error
        try:
            profile, lane = resolve_generic_web_rollback_lane(
                record_store=record_store,
                product=rollback_request.product,
                route_path=_GENERIC_WEB_ROLLBACK_ROUTE,
                instance=rollback_request.rollback.instance,
            )
        except GenericWebRollbackRouteDependencyError:
            return dependencies.driver_dependency_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_ROLLBACK_ROUTE,
            )
        except GenericWebRollbackProductMismatchError as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not native_routes._native_driver_route_authorization_allows(
            endpoint=write_generic_web_rollback,
            authorization_allows=dependencies.authorization_allows,
            identity=identity,
            product=rollback_request.product,
            context=lane.context,
            instances=(lane.instance,),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the generic web prod rollback"
                    " for the requested product/context."
                ),
            )
        (
            normalized_idempotency_key,
            payload_fingerprint,
            replay_response,
        ) = await dependencies.replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_GENERIC_WEB_ROLLBACK_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replay_response is not None:
            return replay_response
        try:
            records, driver_result = execute_generic_web_rollback_result(
                control_plane_root=dependencies.control_plane_root,
                record_store=record_store,
                request=rollback_request,
                profile=profile,
            )
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_GENERIC_WEB_ROLLBACK_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={key: str(value) for key, value in records.items()},
            result=driver_result,
        )
        if should_store_generic_web_rollback_idempotency(driver_result):
            dependencies.store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_ROLLBACK_ROUTE,
                idempotency_key=normalized_idempotency_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_generic_web_preview_desired_state(
        request: Request,
        desired_state_request: GenericWebPreviewDesiredStateEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse:
        trace_id = dependencies.next_trace_id()
        try:
            profile = resolve_generic_web_preview_desired_state_profile(
                record_store=record_store,
                product=desired_state_request.product,
            )
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="driver_route_dependency_not_found",
                message=(
                    "Driver route is registered, but required product or runtime records "
                    "were not found."
                ),
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not native_routes._native_driver_route_authorization_allows(
            endpoint=apply_generic_web_preview_desired_state,
            authorization_allows=dependencies.authorization_allows,
            identity=identity,
            product=profile.product,
            context=profile.preview.context,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot discover generic web preview desired state "
                    "for the requested product/context."
                ),
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await dependencies.replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=True,
        )
        if replayed_response is not None:
            return replayed_response
        try:
            desired_state_store = dependencies.require_preview_desired_state_write_store(
                record_store
            )
        except TypeError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message=str(error),
            ) from error
        driver_result = discover_generic_web_preview_desired_state(
            control_plane_root=dependencies.control_plane_root,
            record_store=cast(GenericWebPreviewProfileStore, record_store),
            request=desired_state_request.desired_state,
            discovered_at=utc_now_timestamp(),
            profile=profile,
        )
        desired_state_store.write_preview_desired_state_record(driver_result)
        response = accepted_evidence_response(
            trace_id=trace_id,
            records={"preview_desired_state_id": driver_result.desired_state_id},
            result=driver_result.model_dump(mode="json"),
        )
        if driver_result.status != "fail":
            dependencies.store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_generic_web_preview_inventory(
        inventory_request: GenericWebPreviewInventoryEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = dependencies.next_trace_id()
        try:
            profile = resolve_generic_web_preview_profile(
                record_store=record_store,
                product=inventory_request.product,
            )
        except GenericWebPreviewRouteDependencyError:
            return dependencies.driver_dependency_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_PREVIEW_INVENTORY_ROUTE,
            )
        except GenericWebPreviewProductMismatchError as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not native_routes._native_driver_route_authorization_allows(
            endpoint=apply_generic_web_preview_inventory,
            authorization_allows=dependencies.authorization_allows,
            identity=identity,
            product=profile.product,
            context=profile.preview.context,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot read generic web preview inventory"
                    " for the requested product/context."
                ),
            )
        try:
            records, result = apply_generic_web_preview_inventory_result(
                control_plane_root=dependencies.control_plane_root,
                record_store=record_store,
                request=inventory_request,
                profile=profile,
            )
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        return accepted_evidence_response(
            trace_id=trace_id,
            records=records,
            result=result,
        )

    async def apply_generic_web_preview_readiness(
        readiness_request: GenericWebPreviewReadinessEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = dependencies.next_trace_id()
        try:
            profile = resolve_generic_web_preview_profile(
                record_store=record_store,
                product=readiness_request.product,
            )
        except GenericWebPreviewRouteDependencyError:
            return dependencies.driver_dependency_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_PREVIEW_READINESS_ROUTE,
            )
        except GenericWebPreviewProductMismatchError as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not native_routes._native_driver_route_authorization_allows(
            endpoint=apply_generic_web_preview_readiness,
            authorization_allows=dependencies.authorization_allows,
            identity=identity,
            product=profile.product,
            context=profile.preview.context,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot evaluate generic web preview readiness"
                    " for the requested product/context."
                ),
            )
        try:
            records, result = apply_generic_web_preview_readiness_result(
                control_plane_root=dependencies.control_plane_root,
                record_store=record_store,
                request=readiness_request,
                profile=profile,
            )
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        return accepted_evidence_response(
            trace_id=trace_id,
            records=records,
            result=result,
        )

    async def apply_generic_web_preview_refresh(
        request: Request,
        refresh_request: GenericWebPreviewRefreshEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = dependencies.next_trace_id()
        try:
            profile = resolve_generic_web_preview_profile(
                record_store=record_store,
                product=refresh_request.product,
            )
        except GenericWebPreviewRouteDependencyError:
            return dependencies.driver_dependency_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
            )
        except GenericWebPreviewProductMismatchError as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not native_routes._native_driver_route_authorization_allows(
            endpoint=apply_generic_web_preview_refresh,
            authorization_allows=dependencies.authorization_allows,
            identity=identity,
            product=profile.product,
            context=profile.preview.context,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot refresh generic web preview state"
                    " for the requested product/context."
                ),
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await dependencies.replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            lock_key, refresh_lock = _generic_web_preview_refresh_lock(
                request=request,
                profile=profile,
                refresh_request=refresh_request,
            )
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if refresh_lock.locked():
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="mutation_in_progress",
                message=(
                    "A generic web preview refresh is already running for the requested preview. "
                    "Retry after it completes."
                ),
            )
        await refresh_lock.acquire()
        try:
            cancellation: asyncio.CancelledError | None = None
            try:
                refresh_task = asyncio.create_task(
                    asyncio.to_thread(
                        apply_generic_web_preview_refresh_result,
                        control_plane_root=dependencies.control_plane_root,
                        record_store=record_store,
                        request=refresh_request,
                        profile=profile,
                    ),
                    name=f"generic-web-preview-refresh:{trace_id}",
                )
                try:
                    records, result = await asyncio.shield(refresh_task)
                except asyncio.CancelledError as error:
                    cancellation = error
                    while not refresh_task.done():
                        try:
                            await asyncio.shield(refresh_task)
                        except asyncio.CancelledError:
                            continue
                    records, result = refresh_task.result()
            except (ValueError, click.ClickException) as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_request",
                    message="Request could not be completed.",
                ) from error
            pr_number = manager_preview_pr_number(
                profile=profile,
                anchor_pr_number=refresh_request.refresh.anchor_pr_number,
                preview_slug=refresh_request.refresh.preview_slug,
            )
            if pr_number is not None:
                reconcile_manager_preview_approval_for_pr_best_effort(
                    repository=profile.repository,
                    pr_number=pr_number,
                    record_store=record_store,
                    control_plane_root=dependencies.control_plane_root,
                )
            response = accepted_evidence_response(
                trace_id=trace_id,
                records=records,
                result=result,
            )
            if should_store_generic_web_preview_idempotency(result):
                dependencies.store_apply_idempotency(
                    record_store=record_store,
                    identity=identity,
                    route_path=_GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
                    idempotency_key=normalized_key,
                    request_fingerprint_value=payload_fingerprint,
                    trace_id=trace_id,
                    response=response,
                )
            if cancellation is not None:
                raise cancellation
            return response
        finally:
            refresh_lock.release()
            locks = request.app.state.generic_web_preview_refresh_locks
            if locks.get(lock_key) is refresh_lock:
                locks.pop(lock_key, None)

    async def apply_generic_web_preview_destroy(
        request: Request,
        destroy_request: GenericWebPreviewDestroyEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = dependencies.next_trace_id()
        try:
            profile = resolve_generic_web_preview_profile(
                record_store=record_store,
                product=destroy_request.product,
            )
        except GenericWebPreviewRouteDependencyError:
            return dependencies.driver_dependency_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
            )
        except GenericWebPreviewProductMismatchError as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not native_routes._native_driver_route_authorization_allows(
            endpoint=apply_generic_web_preview_destroy,
            authorization_allows=dependencies.authorization_allows,
            identity=identity,
            product=profile.product,
            context=profile.preview.context,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot destroy generic web preview state"
                    " for the requested product/context."
                ),
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await dependencies.replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            records, result = apply_generic_web_preview_destroy_result(
                control_plane_root=dependencies.control_plane_root,
                record_store=record_store,
                request=destroy_request,
                profile=profile,
            )
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        pr_number = manager_preview_pr_number(
            profile=profile,
            anchor_pr_number=destroy_request.destroy.anchor_pr_number,
            preview_slug=destroy_request.destroy.preview_slug,
        )
        if pr_number is not None and result.get("destroy_status") == "pass":
            invalidate_manager_preview_approval_for_pr_best_effort(
                repository=profile.repository,
                pr_number=pr_number,
                reason="The serving preview was destroyed.",
                source_event_kind="preview_destroy",
                source_event_id=(
                    f"{profile.product}:{profile.preview.context}:{pr_number}:"
                    f"{str(result.get('destroy_finished_at') or '').strip()}"
                ),
                record_store=record_store,
                control_plane_root=dependencies.control_plane_root,
            )
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=records,
            result=result,
        )
        if should_store_generic_web_preview_idempotency(result):
            dependencies.store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_generic_web_deploy(
        request: Request,
        deploy_request: GenericWebDeployEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = dependencies.next_trace_id()
        try:
            profile, lane = resolve_generic_web_deploy_lane(
                record_store=record_store,
                product=deploy_request.product,
                instance=deploy_request.deploy.instance,
            )
        except GenericWebDeployRouteDependencyError:
            return dependencies.driver_dependency_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_DEPLOY_ROUTE,
            )
        except GenericWebDeployProductMismatchError as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not native_routes._native_driver_route_authorization_allows(
            endpoint=apply_generic_web_deploy,
            authorization_allows=dependencies.authorization_allows,
            identity=identity,
            product=profile.product,
            context=lane.context,
            instances=(lane.instance,),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the generic web deploy driver"
                    " for the requested product/context."
                ),
            )
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Generic web deploy requests require an Idempotency-Key header.",
            )
        raw_payload = await request.json()
        payload_fingerprint = dependencies.idempotency_request_fingerprint(
            route_path=_GENERIC_WEB_DEPLOY_ROUTE,
            payload=cast(dict[str, object], raw_payload),
        )
        adapter = _GenericWebDeployProviderMutationAdapter(
            control_plane_root=dependencies.control_plane_root,
            record_store=record_store,
            deploy_request=deploy_request,
            profile=profile,
            lane=lane,
            trace_id=trace_id,
        )
        try:
            return await dependencies.run_provider_mutation(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_DEPLOY_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint=payload_fingerprint,
                trace_id=trace_id,
                adapter=adapter,
                in_progress_message=(
                    "A matching generic web deploy is already running. "
                    "Retry with the same Idempotency-Key."
                ),
                reconcile_message=("The generic web deploy requires reconciliation before retry."),
            )
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_GENERIC_WEB_DEPLOY_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error

    async def apply_generic_web_prod_promotion(
        request: Request,
        promotion_request: GenericWebProdPromotionEnvelope,
        identity: Annotated[
            LaunchplaneIdentity, Depends(dependencies.read_browser_mutation_identity)
        ],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> GenericWebProdPromotionResponse | JSONResponse:
        trace_id = dependencies.next_trace_id()
        if isinstance(identity, TerminalAgentIdentity):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Terminal agent credentials can only read redacted Launchplane context.",
            )
        try:
            profile, lane = resolve_generic_web_promotion_destination_lane(
                record_store=record_store,
                product=promotion_request.product,
                instance=promotion_request.promotion.to_instance,
            )
            validate_generic_web_prod_promotion_lanes(
                record_store=record_store,
                request=promotion_request,
            )
        except GenericWebPromotionRouteDependencyError:
            return dependencies.driver_dependency_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_PROD_PROMOTION_ROUTE,
            )
        except GenericWebPromotionProductMismatchError as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if (
            isinstance(identity, GitHubHumanIdentity | LocalOperatorIdentity | LocalAdminIdentity)
            and not promotion_request.promotion.dry_run
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Launchplane UI can only dry-run generic-web prod promotions.",
            )
        if not native_routes._native_driver_route_authorization_allows(
            endpoint=apply_generic_web_prod_promotion,
            authorization_allows=dependencies.authorization_allows,
            identity=identity,
            product=profile.product,
            context=lane.context.strip(),
            instances=(
                promotion_request.promotion.from_instance,
                promotion_request.promotion.to_instance,
            ),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot execute the generic web prod promotion driver"
                    " for the requested product/context."
                ),
            )
        live_promotion = not promotion_request.promotion.dry_run
        promotion_intent_id = promotion_request.promotion.promotion_intent_id.strip()
        if live_promotion:
            if not promotion_intent_id and not (
                dependencies.authorization_allows(
                    identity=identity,
                    action="generic_web_prod_promotion.execute_unreviewed",
                    product=profile.product,
                    context=lane.context.strip(),
                )
            ):
                raise dependencies.http_error(
                    status_code=403,
                    trace_id=trace_id,
                    code="promotion_intent_required",
                    message=(
                        "Live generic-web promotion requires a product-owned intent or an "
                        "explicit unreviewed-automation grant."
                    ),
                )
            if not idempotency_key.strip():
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="idempotency_key_required",
                    message="Live generic-web promotion requires an Idempotency-Key header.",
                )
        reservation_scope = (
            f"promotion-intent:{promotion_intent_id}"
            if live_promotion and promotion_intent_id
            else ""
        )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await dependencies.replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_GENERIC_WEB_PROD_PROMOTION_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
            idempotency_scope_override=reservation_scope,
        )
        if replayed_response is not None:
            return GenericWebProdPromotionResponse.model_validate(
                replayed_response.model_dump(mode="json")
            )
        if live_promotion:
            require_current_manager_preview_approval(
                record_store=record_store,
                profile=profile,
                lane=lane,
                trace_id=trace_id,
            )

            def validate_live_promotion(
                resolved_deploy_target: GenericWebResolvedDeployTarget,
            ) -> None:
                require_current_manager_preview_approval(
                    record_store=record_store,
                    profile=profile,
                    lane=lane,
                    trace_id=trace_id,
                )
                require_product_promotion_target_snapshot(
                    record_store=record_store,
                    lane=lane,
                    resolved_deploy_target=resolved_deploy_target,
                    trace_id=trace_id,
                )
                if promotion_intent_id:
                    require_product_promotion_intent(
                        record_store=record_store,
                        profile=profile,
                        lane=lane,
                        promotion_request=promotion_request,
                        idempotency_key=idempotency_key,
                        trace_id=trace_id,
                    )

            adapter = _GenericWebProdPromotionProviderMutationAdapter(
                control_plane_root=dependencies.control_plane_root,
                record_store=record_store,
                promotion_request=promotion_request,
                profile=profile,
                lane=lane,
                trace_id=trace_id,
                validate_before_effect=validate_live_promotion,
            )
            try:
                durable_response = await dependencies.run_provider_mutation(
                    record_store=record_store,
                    identity=identity,
                    route_path=_GENERIC_WEB_PROD_PROMOTION_ROUTE,
                    idempotency_key=normalized_key,
                    request_fingerprint=payload_fingerprint,
                    trace_id=trace_id,
                    adapter=adapter,
                    in_progress_message=(
                        "A matching generic web prod promotion is already running. "
                        "Retry with the same Idempotency-Key."
                    ),
                    reconcile_message=(
                        "The generic web prod promotion requires reconciliation before retry."
                    ),
                    reservation_scope=reservation_scope,
                )
            except FileNotFoundError as error:
                raise dependencies.http_error(
                    status_code=404,
                    trace_id=trace_id,
                    code="not_found",
                    message=f"No Launchplane route for {_GENERIC_WEB_PROD_PROMOTION_ROUTE}.",
                ) from error
            except (ValueError, click.ClickException) as error:
                raise dependencies.http_error(
                    status_code=400,
                    trace_id=trace_id,
                    code="invalid_request",
                    message="Request could not be completed.",
                ) from error
            return GenericWebProdPromotionResponse.model_validate(
                durable_response.model_dump(mode="json")
            )
        try:
            records, result = execute_generic_web_prod_promotion_result(
                control_plane_root=dependencies.control_plane_root,
                record_store=record_store,
                request=promotion_request,
            )
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=f"No Launchplane route for {_GENERIC_WEB_PROD_PROMOTION_ROUTE}.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        response = GenericWebProdPromotionResponse(
            trace_id=trace_id,
            records=records,
            result=GenericWebProdPromotionResponseResult.model_validate(
                result.model_dump(mode="json")
            ),
        )
        if should_store_generic_web_promotion_idempotency(result):
            dependencies.store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_PROD_PROMOTION_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def dispatch_generic_web_prod_promotion_workflow(
        request: Request,
        workflow_request: GenericWebPromotionWorkflowEnvelope,
        identity: Annotated[
            LaunchplaneIdentity, Depends(dependencies.read_browser_mutation_identity)
        ],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> GenericWebPromotionWorkflowResponse | JSONResponse:
        trace_id = dependencies.next_trace_id()
        if isinstance(identity, TerminalAgentIdentity):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message="Terminal agent credentials can only read redacted Launchplane context.",
            )
        if isinstance(identity, GitHubHumanIdentity | LocalOperatorIdentity | LocalAdminIdentity):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Operator workflow dispatches must use the product-owned promotion route."
                ),
            )
        try:
            profile, lane, affected_instances = resolve_generic_web_promotion_workflow_lane(
                record_store=record_store,
                product=workflow_request.product,
                context=workflow_request.workflow.context,
            )
        except GenericWebPromotionRouteDependencyError:
            return dependencies.driver_dependency_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
            )
        except GenericWebPromotionProductMismatchError as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not native_routes._native_driver_route_authorization_allows(
            endpoint=dispatch_generic_web_prod_promotion_workflow,
            authorization_allows=dependencies.authorization_allows,
            identity=identity,
            product=profile.product,
            context=lane.context.strip(),
            instances=affected_instances,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot dispatch the generic web prod promotion workflow"
                    " for the requested product/context."
                ),
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await dependencies.replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return GenericWebPromotionWorkflowResponse.model_validate(
                replayed_response.model_dump(mode="json")
            )
        try:
            records, result, outbox_delivery = dispatch_generic_web_promotion_workflow_result(
                request=workflow_request,
                profile=profile,
                delivery_key=(
                    f"{idempotency_scope(identity)}:{normalized_key}"
                    if normalized_key
                    else trace_id
                ),
            )
        except FileNotFoundError as error:
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="not_found",
                message=(f"No Launchplane route for {_GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE}."),
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        response = GenericWebPromotionWorkflowResponse(
            trace_id=trace_id,
            records=records,
            result=GenericWebPromotionWorkflowResponseResult.model_validate(
                result.model_dump(mode="json")
            ),
        )
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="outbox_requires_postgres",
                message="Workflow dispatch requires PostgreSQL transactional outbox storage.",
            )
        idempotency_record = None
        if normalized_key and should_store_generic_web_promotion_idempotency(result):
            idempotency_record = dependencies.build_apply_idempotency_record(
                identity=identity,
                route_path=_GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        record_store.enqueue_outbox_delivery_with_idempotency(
            OutboxWithIdempotencyRequest(
                delivery=outbox_delivery,
                idempotency_record=idempotency_record,
            )
        )
        return response

    async def apply_generic_web_stable_verification(
        request: Request,
        verification_request: GenericWebStableVerificationEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = dependencies.next_trace_id()
        try:
            profile, lane = resolve_generic_web_stable_verification_lane(
                record_store=record_store,
                product=verification_request.product,
                context=verification_request.verification.context,
                instance=verification_request.verification.instance,
            )
        except GenericWebVerificationRouteDependencyError:
            return dependencies.driver_dependency_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
            )
        except GenericWebVerificationProductMismatchError as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except ValueError as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not native_routes._native_driver_route_authorization_allows(
            endpoint=apply_generic_web_stable_verification,
            authorization_allows=dependencies.authorization_allows,
            identity=identity,
            product=profile.product,
            context=lane.context,
            instances=(lane.instance,),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write generic web stable verification"
                    " for the requested product/context."
                ),
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await dependencies.replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        try:
            result = apply_generic_web_stable_verification_result(
                record_store=record_store,
                request=verification_request,
            )
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=generic_web_verification_response_records(result),
            result=result,
        )
        if should_store_generic_web_verification_idempotency(result):
            dependencies.store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    async def apply_generic_web_preview_verification(
        request: Request,
        verification_request: GenericWebPreviewVerificationEnvelope,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")] = "",
    ) -> AcceptedEvidenceResponse | JSONResponse:
        trace_id = dependencies.next_trace_id()
        try:
            profile = resolve_generic_web_preview_verification_profile(
                record_store=record_store,
                product=verification_request.product,
                context=verification_request.verification.context,
            )
        except GenericWebVerificationRouteDependencyError:
            return dependencies.driver_dependency_response(
                trace_id=trace_id,
                route_path=_GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
            )
        except GenericWebVerificationProductMismatchError as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for the requested driver route.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not native_routes._native_driver_route_authorization_allows(
            endpoint=apply_generic_web_preview_verification,
            authorization_allows=dependencies.authorization_allows,
            identity=identity,
            product=profile.product,
            context=profile.preview.context,
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Workflow cannot write generic web preview verification"
                    " for the requested product/context."
                ),
            )
        (
            normalized_key,
            payload_fingerprint,
            replayed_response,
        ) = await dependencies.replay_apply_idempotency(
            request=request,
            record_store=record_store,
            identity=identity,
            route_path=_GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            check_replay=bool(idempotency_key.strip()),
        )
        if replayed_response is not None:
            return replayed_response
        verification_request.verification.context = profile.preview.context
        try:
            result = apply_generic_web_preview_verification_result(
                control_plane_root=dependencies.control_plane_root,
                record_store=record_store,
                request=verification_request,
            )
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        reconcile_manager_preview_approval_for_pr_best_effort(
            repository=profile.repository,
            pr_number=verification_request.verification.anchor_pr_number,
            record_store=record_store,
            control_plane_root=dependencies.control_plane_root,
        )
        response = accepted_evidence_response(
            trace_id=trace_id,
            records=generic_web_verification_response_records(result),
            result=result,
        )
        if should_store_generic_web_verification_idempotency(result):
            dependencies.store_apply_idempotency(
                record_store=record_store,
                identity=identity,
                route_path=_GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
                idempotency_key=normalized_key,
                request_fingerprint_value=payload_fingerprint,
                trace_id=trace_id,
                response=response,
            )
        return response

    return GenericWebWriteRouteHandlers(
        apply_generic_web_preview_desired_state=apply_generic_web_preview_desired_state,
        apply_generic_web_preview_inventory=apply_generic_web_preview_inventory,
        apply_generic_web_preview_readiness=apply_generic_web_preview_readiness,
        apply_generic_web_preview_refresh=apply_generic_web_preview_refresh,
        apply_generic_web_preview_destroy=apply_generic_web_preview_destroy,
        apply_generic_web_deploy=apply_generic_web_deploy,
        dry_run_generic_web_deploy_recovery=dry_run_generic_web_deploy_recovery,
        apply_generic_web_prod_promotion=apply_generic_web_prod_promotion,
        dispatch_generic_web_prod_promotion_workflow=dispatch_generic_web_prod_promotion_workflow,
        apply_generic_web_stable_verification=apply_generic_web_stable_verification,
        apply_generic_web_preview_verification=apply_generic_web_preview_verification,
        write_generic_web_rollback_plan=write_generic_web_rollback_plan,
        write_generic_web_rollback=write_generic_web_rollback,
    )


def register_generic_web_write_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: GenericWebWriteRouteDependencies,
    handlers: GenericWebWriteRouteHandlers,
) -> None:
    app.add_api_route(
        _GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE,
        handlers.apply_generic_web_preview_desired_state,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        operation_id="apply_generic_web_preview_desired_state",
        summary="Discover generic web preview desired state",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_PREVIEW_INVENTORY_ROUTE,
        handlers.apply_generic_web_preview_inventory,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": dependencies.openapi_model_schema(
                            GenericWebPreviewInventoryEnvelope
                        )
                    }
                },
            }
        },
        operation_id="apply_generic_web_preview_inventory",
        summary="Read generic web preview inventory",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_PREVIEW_READINESS_ROUTE,
        handlers.apply_generic_web_preview_readiness,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": dependencies.openapi_model_schema(
                            GenericWebPreviewReadinessEnvelope
                        )
                    }
                },
            }
        },
        operation_id="apply_generic_web_preview_readiness",
        summary="Evaluate generic web preview readiness",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_PREVIEW_REFRESH_ROUTE,
        handlers.apply_generic_web_preview_refresh,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": dependencies.openapi_model_schema(
                            GenericWebPreviewRefreshEnvelope
                        )
                    }
                },
            }
        },
        operation_id="apply_generic_web_preview_refresh",
        summary="Refresh generic web preview",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_PREVIEW_DESTROY_ROUTE,
        handlers.apply_generic_web_preview_destroy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": dependencies.openapi_model_schema(
                            GenericWebPreviewDestroyEnvelope
                        )
                    }
                },
            }
        },
        operation_id="apply_generic_web_preview_destroy",
        summary="Destroy generic web preview",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_DEPLOY_ROUTE,
        handlers.apply_generic_web_deploy,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": dependencies.openapi_model_schema(GenericWebDeployEnvelope)
                    }
                },
            }
        },
        operation_id="apply_generic_web_deploy",
        summary="Execute generic web deploy",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_DEPLOY_RECOVERY_DRY_RUN_ROUTE,
        handlers.dry_run_generic_web_deploy_recovery,
        methods=["POST"],
        response_model=GenericWebDeployRecoveryDryRunResponse,
        response_model_exclude_none=True,
        operation_id="dry_run_generic_web_deploy_recovery",
        summary="Inspect an existing generic web deploy reservation without writing",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_PROD_PROMOTION_ROUTE,
        handlers.apply_generic_web_prod_promotion,
        methods=["POST"],
        status_code=202,
        response_model=GenericWebProdPromotionResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": dependencies.openapi_model_schema(GenericWebProdPromotionEnvelope)
                    }
                },
            }
        },
        operation_id="apply_generic_web_prod_promotion",
        summary="Execute generic web prod promotion",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_PROD_PROMOTION_WORKFLOW_ROUTE,
        handlers.dispatch_generic_web_prod_promotion_workflow,
        methods=["POST"],
        status_code=202,
        response_model=GenericWebPromotionWorkflowResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": dependencies.openapi_model_schema(
                            GenericWebPromotionWorkflowEnvelope
                        )
                    }
                },
            }
        },
        operation_id="dispatch_generic_web_prod_promotion_workflow",
        summary="Dispatch generic web prod promotion workflow",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_STABLE_VERIFICATION_ROUTE,
        handlers.apply_generic_web_stable_verification,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": dependencies.openapi_model_schema(
                            GenericWebStableVerificationEnvelope
                        )
                    }
                },
            }
        },
        operation_id="apply_generic_web_stable_verification",
        summary="Write generic web stable verification",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE,
        handlers.apply_generic_web_preview_verification,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": dependencies.openapi_model_schema(
                            GenericWebPreviewVerificationEnvelope
                        )
                    }
                },
            }
        },
        operation_id="apply_generic_web_preview_verification",
        summary="Write generic web preview verification",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )


def register_generic_web_rollback_write_routes(
    app: ApiRouteRegistrar,
    *,
    dependencies: GenericWebWriteRouteDependencies,
    handlers: GenericWebWriteRouteHandlers,
) -> None:
    app.add_api_route(
        _GENERIC_WEB_ROLLBACK_PLAN_ROUTE,
        handlers.write_generic_web_rollback_plan,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": dependencies.openapi_model_schema(GenericWebRollbackPlanEnvelope)
                    }
                },
            }
        },
        operation_id="write_generic_web_rollback_plan",
        summary="Plan generic web prod rollback",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )

    app.add_api_route(
        _GENERIC_WEB_ROLLBACK_ROUTE,
        handlers.write_generic_web_rollback,
        methods=["POST"],
        status_code=202,
        response_model=AcceptedEvidenceResponse,
        response_model_exclude_none=True,
        openapi_extra={
            "requestBody": {
                "required": True,
                "content": {
                    "application/json": {
                        "schema": dependencies.openapi_model_schema(GenericWebRollbackEnvelope)
                    }
                },
            }
        },
        operation_id="write_generic_web_rollback",
        summary="Execute generic web prod rollback",
        responses={
            400: {"model": dependencies.error_response_model},
            401: {"model": dependencies.error_response_model},
            403: {"model": dependencies.error_response_model},
            404: {"model": dependencies.error_response_model},
            409: {"model": dependencies.error_response_model},
            503: {"model": dependencies.error_response_model},
        },
    )
