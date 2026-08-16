from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any, cast

import click
from fastapi import Depends, Header, Request
from pydantic import ValidationError

from control_plane.contracts.generic_web_deploy_recovery import (
    GenericWebDeployRecoveryAction,
    GenericWebDeployRecoveryDryRunRequest,
    GenericWebDeployRecoveryDryRunResponse,
    GenericWebDeployRecoveryProviderOutcome,
    build_generic_web_deploy_recovery_digest,
    generic_web_deploy_recovery_identifier_sha256,
)
from control_plane.contracts.idempotency_record import parse_launchplane_mutation_timestamp
from control_plane.generic_web_deploy_http import (
    GENERIC_WEB_DEPLOY_ROUTE,
    GenericWebDeployProductMismatchError,
    GenericWebDeployRouteDependencyError,
    resolve_generic_web_deploy_lane,
)
from control_plane.generic_web_deploy_provider_adapter import (
    _GenericWebDeployProviderMutationAdapter,
)
from control_plane.http_routes.support import AuthorizationAllows, HttpErrorFactory
from control_plane.provider_operations import build_provider_operation_key
from control_plane.service_auth import AuthorizationTarget, LaunchplaneIdentity
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployResult,
    normalize_generic_web_artifact_id,
)
from control_plane.workflows.generic_web_deploy_provider import (
    build_generic_web_provider_target_key,
    decode_generic_web_provider_reconciliation_target,
    resolve_generic_web_provider_reconciliation_target,
)


GENERIC_WEB_DEPLOY_RECOVERY_DRY_RUN_ROUTE = "/v1/admin/generic-web/deploy-recovery/dry-run"

__all__ = [
    "GENERIC_WEB_DEPLOY_RECOVERY_DRY_RUN_ROUTE",
    "GenericWebDeployRecoveryDependencies",
    "build_generic_web_deploy_recovery_dry_run_handler",
]


@dataclass(frozen=True, slots=True)
class GenericWebDeployRecoveryDependencies:
    read_write_identity: Callable[..., LaunchplaneIdentity]
    get_record_store: Callable[[], object]
    next_trace_id: Callable[[], str]
    authorization_allows: AuthorizationAllows
    http_error: HttpErrorFactory
    control_plane_root: Path
    idempotency_request_fingerprint: Callable[..., str]


def _bounded_recovery_value(value: object, *, limit: int = 128) -> str:
    return str(value or "").strip()[:limit]


def build_generic_web_deploy_recovery_dry_run_handler(
    *, dependencies: GenericWebDeployRecoveryDependencies
) -> Callable[..., Any]:
    async def dry_run_generic_web_deploy_recovery(
        request: Request,
        recovery_request: GenericWebDeployRecoveryDryRunRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> GenericWebDeployRecoveryDryRunResponse:
        trace_id = dependencies.next_trace_id()
        try:
            profile, lane = resolve_generic_web_deploy_lane(
                record_store=record_store,
                product=recovery_request.product,
                instance=recovery_request.instance,
            )
        except GenericWebDeployRouteDependencyError as error:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="storage_unavailable",
                message="Generic web deploy recovery requires database-backed profile storage.",
            ) from error
        except GenericWebDeployProductMismatchError as error:
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="product_driver_mismatch",
                message="Product is not configured for generic web deploy recovery.",
            ) from error
        except (ValueError, click.ClickException) as error:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Request could not be completed.",
            ) from error
        if not dependencies.authorization_allows(
            identity=identity,
            action="generic_web_deploy.execute",
            product=profile.product,
            context=lane.context,
            target=AuthorizationTarget(scope="instance", instances=(lane.instance,)),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Identity cannot inspect generic web deploy recovery for the requested "
                    "product/context."
                ),
            )
        normalized_key = idempotency_key.strip()
        if not normalized_key:
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="idempotency_key_required",
                message="Generic web deploy recovery requires an Idempotency-Key header.",
            )
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Generic web deploy recovery requires database storage.",
            )
        raw_payload = await request.json()
        original_payload = (
            raw_payload.get("original_deploy") if isinstance(raw_payload, dict) else None
        )
        if not isinstance(original_payload, dict):
            raise dependencies.http_error(
                status_code=400,
                trace_id=trace_id,
                code="invalid_request",
                message="Generic web deploy recovery requires the exact original deploy payload.",
            )
        original_fingerprint = dependencies.idempotency_request_fingerprint(
            route_path=GENERIC_WEB_DEPLOY_ROUTE,
            payload=cast(dict[str, object], original_payload),
        )
        lookup = record_store.lookup_existing_mutation_reservation(
            route_path=GENERIC_WEB_DEPLOY_ROUTE,
            idempotency_key=normalized_key,
            request_fingerprint=original_fingerprint,
        )
        if lookup.status == "missing":
            raise dependencies.http_error(
                status_code=404,
                trace_id=trace_id,
                code="reservation_not_found",
                message="No matching generic web deploy reservation exists.",
            )
        if lookup.status in {"conflict", "ambiguous"}:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code=f"reservation_{lookup.status}",
                message="Generic web deploy recovery reservation identity is not unique and exact.",
            )
        if lookup.status == "hold_unknown":
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="hold_unknown",
                message="Generic web deploy recovery lease timing is not authoritative.",
            )
        reservation = lookup.record
        if reservation is None:
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="reservation_lookup_failed",
                message="Generic web deploy recovery could not inspect the reservation.",
            )
        if (
            reservation.route_path != GENERIC_WEB_DEPLOY_ROUTE
            or reservation.idempotency_key != normalized_key
            or reservation.request_fingerprint != original_fingerprint
        ):
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="reservation_conflict",
                message="Generic web deploy recovery reservation identity is not exact.",
            )

        provider_outcome: GenericWebDeployRecoveryProviderOutcome = "not_inspected"
        provider_status = ""
        retry_safe = False
        proposed_action: GenericWebDeployRecoveryAction = "hold_unknown"
        provider_operation_key = ""
        provider_observation_payload: dict[str, object] = {"outcome": provider_outcome}
        operation_product = recovery_request.original_deploy.product.strip()
        operation_context = ""
        operation_instance = recovery_request.original_deploy.deploy.instance.strip()
        authoritative_lane = lane
        if reservation.state == "completed":
            stored_result = reservation.response_payload.get("result")
            try:
                completed_result = GenericWebDeployResult.model_validate(stored_result)
            except ValidationError as error:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="reservation_target_conflict",
                    message="Completed generic web deploy recovery context is not authoritative.",
                ) from error
            completed_product = completed_result.product.strip()
            completed_context = completed_result.context.strip()
            completed_instance = completed_result.instance.strip()
            if (
                not completed_product
                or not completed_context
                or not completed_instance
                or completed_product != operation_product
                or completed_instance != operation_instance
            ):
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="reservation_target_conflict",
                    message="Completed generic web deploy recovery context is not authoritative.",
                )
            operation_context = completed_context
            proposed_action = "replay_completed"
            provider_status = _bounded_recovery_value(completed_result.deploy_status)
        else:
            if not reservation.reconciliation_key or not reservation.provider_target_key:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="reservation_target_conflict",
                    message="Stored generic web deploy recovery target identity is incomplete.",
                )
            try:
                stored_target = decode_generic_web_provider_reconciliation_target(
                    reservation.reconciliation_key
                )
            except ValueError as error:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="reservation_target_conflict",
                    message="Stored generic web deploy recovery target identity is invalid.",
                ) from error
            operation_context = stored_target.context.strip()
            stored_instance = stored_target.instance.strip()
            stored_product = stored_target.product.strip()
            legacy_snapshot_without_product = "product" not in stored_target.model_fields_set
            if (
                not operation_context
                or (not legacy_snapshot_without_product and stored_product != operation_product)
                or stored_instance != operation_instance
            ):
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="reservation_target_conflict",
                    message="Stored generic web deploy recovery target identity conflicts.",
                )
            authoritative_lane = lane.model_copy(
                update={"context": operation_context, "instance": stored_instance}
            )
            try:
                resolved_stored_target = resolve_generic_web_provider_reconciliation_target(
                    reconciliation_key=reservation.reconciliation_key,
                    request_artifact_id=recovery_request.original_deploy.deploy.artifact_id,
                    request_source_git_ref=recovery_request.original_deploy.deploy.source_git_ref,
                    request_timeout_seconds=recovery_request.original_deploy.deploy.timeout_seconds,
                    request_no_cache=recovery_request.original_deploy.deploy.no_cache,
                    normalized_artifact_id=normalize_generic_web_artifact_id(
                        profile=profile,
                        artifact_id=recovery_request.original_deploy.deploy.artifact_id,
                    ),
                    request_deploy_reference=(
                        recovery_request.original_deploy.deploy.deploy_reference
                    ),
                    lane=authoritative_lane,
                )
            except (ValueError, click.ClickException) as error:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="reservation_target_conflict",
                    message="Stored generic web deploy recovery target identity is invalid.",
                ) from error
            if (
                resolved_stored_target.ship_request.context != operation_context
                or resolved_stored_target.ship_request.instance != stored_instance
                or build_generic_web_provider_target_key(resolved_stored_target)
                != reservation.provider_target_key
            ):
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="reservation_target_conflict",
                    message="Stored generic web deploy recovery target identity conflicts.",
                )

        if not dependencies.authorization_allows(
            identity=identity,
            action="generic_web_deploy.execute",
            product=operation_product,
            context=operation_context,
            target=AuthorizationTarget(scope="instance", instances=(operation_instance,)),
        ):
            raise dependencies.http_error(
                status_code=403,
                trace_id=trace_id,
                code="authorization_denied",
                message=(
                    "Identity cannot inspect generic web deploy recovery for the stored "
                    "product/context."
                ),
            )

        if reservation.state != "completed":
            lease_is_active = False
            if reservation.state == "running":
                try:
                    lease_is_active = parse_launchplane_mutation_timestamp(
                        reservation.lease_expires_at,
                        field_name="lease_expires_at",
                    ) > parse_launchplane_mutation_timestamp(
                        lookup.observed_at,
                        field_name="observed_at",
                    )
                except ValueError as error:
                    raise dependencies.http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="hold_unknown",
                        message="Generic web deploy recovery lease timing is not authoritative.",
                    ) from error
                if lease_is_active:
                    proposed_action = "wait_for_active_lease"
            if reservation.state != "running" or not lease_is_active:
                try:
                    provider_operation_key = build_provider_operation_key(
                        scope=reservation.scope,
                        route_path=reservation.route_path,
                        idempotency_key=reservation.idempotency_key,
                        request_fingerprint=reservation.request_fingerprint,
                        reconciliation_key=reservation.reconciliation_key,
                    )
                except ValueError as error:
                    raise dependencies.http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="reservation_target_conflict",
                        message="Generic web deploy recovery reservation identity is incomplete.",
                    ) from error
                adapter = _GenericWebDeployProviderMutationAdapter(
                    control_plane_root=dependencies.control_plane_root,
                    record_store=record_store,
                    deploy_request=recovery_request.original_deploy,
                    profile=profile,
                    lane=authoritative_lane,
                    trace_id=trace_id,
                )
                inspection = adapter.inspect(
                    provider_operation_key=provider_operation_key,
                    provider_effect_phase=reservation.provider_effect_phase,
                    reconciliation_key=reservation.reconciliation_key,
                    expected_provider_target_key=reservation.provider_target_key,
                )
                if not inspection.identity_matches:
                    raise dependencies.http_error(
                        status_code=409,
                        trace_id=trace_id,
                        code="reservation_target_conflict",
                        message="Stored generic web deploy recovery target identities conflict.",
                    )
                provider_outcome = inspection.observation.outcome
                provider_status = _bounded_recovery_value(inspection.observation.deployment_status)
                retry_safe = inspection.retry_safe
                provider_observation_payload = inspection.observation.model_dump(mode="json")
                if provider_outcome == "present":
                    proposed_action = "adopt_observed"
                elif provider_outcome == "absent" and retry_safe:
                    proposed_action = "retry_original_operation"
                else:
                    proposed_action = "hold_unknown"

        digest_payload: dict[str, object] = {
            "schema_version": 1,
            "mode": "dry-run",
            "request": recovery_request.model_dump(mode="json"),
            "original_route": GENERIC_WEB_DEPLOY_ROUTE,
            "idempotency_key": normalized_key,
            "request_fingerprint": original_fingerprint,
            "reservation": reservation.model_dump(mode="json"),
            "observed_at": lookup.observed_at,
            "provider_operation_key": provider_operation_key,
            "provider_observation": provider_observation_payload,
            "retry_safe": retry_safe,
            "proposed_action": proposed_action,
        }
        return GenericWebDeployRecoveryDryRunResponse(
            product=operation_product,
            context=operation_context,
            instance=operation_instance,
            reservation_state=reservation.state,
            reservation_attempt=reservation.attempt,
            reservation_created_at=reservation.created_at,
            reservation_updated_at=reservation.updated_at,
            reservation_lease_expires_at=reservation.lease_expires_at,
            observed_at=lookup.observed_at,
            reconciliation_key_sha256=generic_web_deploy_recovery_identifier_sha256(
                reservation.reconciliation_key
            ),
            provider_target_key_sha256=generic_web_deploy_recovery_identifier_sha256(
                reservation.provider_target_key
            ),
            provider_effect_phase=_bounded_recovery_value(reservation.provider_effect_phase),
            provider_outcome=provider_outcome,
            provider_status=provider_status,
            retry_safe=retry_safe,
            proposed_action=proposed_action,
            recovery_digest=build_generic_web_deploy_recovery_digest(digest_payload),
        )

    return dry_run_generic_web_deploy_recovery
