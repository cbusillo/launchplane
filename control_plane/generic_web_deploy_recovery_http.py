from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Any

import click
from fastapi import Depends, Header, Request
from pydantic import ValidationError

from control_plane.contracts.generic_web_deploy_recovery import (
    GenericWebDeployRecoveryAction,
    GenericWebDeployRecoveryApplyRequest,
    GenericWebDeployRecoveryApplyResponse,
    GenericWebDeployRecoveryDryRunRequest,
    GenericWebDeployRecoveryDryRunResponse,
    GenericWebDeployRecoveryProviderEvidenceResponse,
    GenericWebDeployRecoveryProviderOutcome,
    build_generic_web_deploy_recovery_digest,
    generic_web_deploy_recovery_identifier_sha256,
)
from control_plane.contracts.idempotency_record import (
    LaunchplaneIdempotencyRecord,
    parse_launchplane_mutation_timestamp,
)
from control_plane.generic_web_deploy_http import (
    GENERIC_WEB_DEPLOY_ROUTE,
    GenericWebDeployProductMismatchError,
    GenericWebDeployRouteDependencyError,
    resolve_generic_web_deploy_lane,
)
from control_plane.generic_web_deploy_provider_adapter import (
    GenericWebDeployProviderMutationAdapter,
)
from control_plane.http_routes.support import AuthorizationAllows, HttpErrorFactory
from control_plane.provider_operations import (
    ProviderMutationOutcome,
    ProviderObservation,
    ProviderOperationLease,
    build_provider_operation_key,
    resume_acquired_provider_operation,
)
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
GENERIC_WEB_DEPLOY_RECOVERY_PROVIDER_EVIDENCE_ROUTE = (
    "/v1/admin/generic-web/deploy-recovery/provider-evidence"
)
GENERIC_WEB_DEPLOY_RECOVERY_APPLY_ROUTE = "/v1/admin/generic-web/deploy-recovery/apply"
GENERIC_WEB_DEPLOY_RECOVERY_PROVIDER_EVIDENCE_READ_ACTION = (
    "generic_web_deploy_recovery_provider_evidence.read"
)

__all__ = [
    "GENERIC_WEB_DEPLOY_RECOVERY_DRY_RUN_ROUTE",
    "GENERIC_WEB_DEPLOY_RECOVERY_PROVIDER_EVIDENCE_ROUTE",
    "GENERIC_WEB_DEPLOY_RECOVERY_PROVIDER_EVIDENCE_READ_ACTION",
    "GENERIC_WEB_DEPLOY_RECOVERY_APPLY_ROUTE",
    "GenericWebDeployRecoveryDependencies",
    "build_generic_web_deploy_recovery_apply_handler",
    "build_generic_web_deploy_recovery_dry_run_handler",
    "build_generic_web_deploy_recovery_provider_evidence_handler",
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
    return value.strip()[:limit] if isinstance(value, str) else ""


def _recovery_authorization_allows(
    *,
    dependencies: GenericWebDeployRecoveryDependencies,
    identity: LaunchplaneIdentity,
    actions: tuple[str, ...],
    product: str,
    context: str,
    instance: str,
) -> bool:
    target = AuthorizationTarget(scope="instance", instances=(instance,))
    return any(
        dependencies.authorization_allows(
            identity=identity,
            action=action,
            product=product,
            context=context,
            target=target,
        )
        for action in actions
    )


@dataclass(frozen=True, slots=True)
class _GenericWebDeployRecoveryInspection:
    request: GenericWebDeployRecoveryDryRunRequest
    product: str
    context: str
    instance: str
    reservation: LaunchplaneIdempotencyRecord
    observed_at: str
    original_fingerprint: str
    idempotency_key: str
    provider_operation_key: str
    provider_outcome: GenericWebDeployRecoveryProviderOutcome
    provider_status: str
    retry_safe: bool
    proposed_action: GenericWebDeployRecoveryAction
    provider_observation_payload: dict[str, object]
    legacy_correlation_digest_evidence: dict[str, object] | None = None
    adapter: GenericWebDeployProviderMutationAdapter | None = None
    provider_inspection: Any | None = None

    @property
    def recovery_digest(self) -> str:
        return build_generic_web_deploy_recovery_digest(self.digest_payload())

    def digest_payload(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "schema_version": 1,
            "request": self.request.model_dump(
                mode="json",
                exclude={"expected_recovery_digest"},
            ),
            "original_route": GENERIC_WEB_DEPLOY_ROUTE,
            "idempotency_key": self.idempotency_key,
            "request_fingerprint": self.original_fingerprint,
            "reservation": self.reservation.model_dump(mode="json"),
            "provider_operation_key": self.provider_operation_key,
            "provider_observation": self.provider_observation_payload,
            "retry_safe": self.retry_safe,
            "proposed_action": self.proposed_action,
        }
        if self.legacy_correlation_digest_evidence is not None:
            payload["legacy_correlation"] = self.legacy_correlation_digest_evidence
        return payload

    def dry_run_response(self) -> GenericWebDeployRecoveryDryRunResponse:
        return GenericWebDeployRecoveryDryRunResponse(
            product=self.product,
            context=self.context,
            instance=self.instance,
            reservation_state=self.reservation.state,
            reservation_attempt=self.reservation.attempt,
            reservation_created_at=self.reservation.created_at,
            reservation_updated_at=self.reservation.updated_at,
            reservation_lease_expires_at=self.reservation.lease_expires_at,
            observed_at=self.observed_at,
            reconciliation_key_sha256=generic_web_deploy_recovery_identifier_sha256(
                self.reservation.reconciliation_key
            ),
            provider_target_key_sha256=generic_web_deploy_recovery_identifier_sha256(
                self.reservation.provider_target_key
            ),
            provider_effect_phase=_bounded_recovery_value(self.reservation.provider_effect_phase),
            provider_outcome=self.provider_outcome,
            provider_status=self.provider_status,
            retry_safe=self.retry_safe,
            proposed_action=self.proposed_action,
            recovery_digest=self.recovery_digest,
        )


class _GenericWebRecoveryApplyAdapter:
    def __init__(
        self,
        *,
        delegate: GenericWebDeployProviderMutationAdapter,
        recovery_metadata: dict[str, object],
    ) -> None:
        self._delegate = delegate
        self._recovery_metadata = recovery_metadata

    def target_key(self) -> str:
        return self._delegate.target_key()

    def reconciliation_key(self) -> str:
        return self._delegate.reconciliation_key()

    def observe(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
    ) -> ProviderObservation:
        return self._delegate.observe(
            provider_operation_key,
            provider_effect_phase,
            reconciliation_key,
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        outcome = self._delegate.apply(provider_operation_key, lease)
        response_payload = dict(outcome.response_payload)
        response_payload["recovery"] = self._recovery_metadata
        return ProviderMutationOutcome(
            response_status_code=outcome.response_status_code,
            response_payload=response_payload,
            durable=outcome.durable,
            provider_effect_performed=outcome.provider_effect_performed,
        )


def _stored_recovery_metadata(
    reservation: LaunchplaneIdempotencyRecord,
) -> tuple[str, GenericWebDeployRecoveryAction] | None:
    stored_recovery = reservation.response_payload.get("recovery")
    if not isinstance(stored_recovery, dict) or set(stored_recovery) != {
        "schema_version",
        "recovery_digest",
        "recovery_action",
    }:
        return None
    if stored_recovery.get("schema_version") != 1:
        return None
    digest = str(stored_recovery.get("recovery_digest") or "").strip()
    action = str(stored_recovery.get("recovery_action") or "").strip()
    if len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest):
        return None
    match action:
        case (
            "replay_completed"
            | "wait_for_active_lease"
            | "adopt_observed"
            | "retry_original_operation"
            | "hold_unknown"
        ):
            return digest, action
        case _:
            return None


def _recovery_metadata(
    inspection: _GenericWebDeployRecoveryInspection,
) -> dict[str, object]:
    return {
        "schema_version": 1,
        "recovery_digest": inspection.recovery_digest,
        "recovery_action": inspection.proposed_action,
    }


def _apply_response(
    *,
    trace_id: str,
    inspection: _GenericWebDeployRecoveryInspection,
    reservation: LaunchplaneIdempotencyRecord,
) -> GenericWebDeployRecoveryApplyResponse:
    metadata = _stored_recovery_metadata(reservation)
    action = inspection.proposed_action
    digest = inspection.recovery_digest
    if metadata is not None:
        digest, action = metadata
    if reservation.state not in {"completed", "reconcile_required"}:
        raise RuntimeError("Generic web recovery apply response requires terminal state.")
    return GenericWebDeployRecoveryApplyResponse(
        trace_id=trace_id,
        product=inspection.product,
        context=inspection.context,
        instance=inspection.instance,
        reservation_state=reservation.state,
        reservation_attempt=reservation.attempt,
        recovery_action=action,
        recovery_digest=digest,
        provider_outcome=inspection.provider_outcome,
        provider_status=inspection.provider_status,
        retry_safe=inspection.retry_safe,
    )


def _apply_replay_response(
    *,
    trace_id: str,
    request: GenericWebDeployRecoveryApplyRequest,
    reservation: LaunchplaneIdempotencyRecord,
    context: str,
) -> GenericWebDeployRecoveryApplyResponse:
    metadata = _stored_recovery_metadata(reservation)
    if metadata is None:
        raise RuntimeError("Recovered generic web reservation replay requires metadata.")
    digest, action = metadata
    return GenericWebDeployRecoveryApplyResponse(
        trace_id=trace_id,
        product=request.product,
        context=context,
        instance=request.instance,
        reservation_state="completed",
        reservation_attempt=reservation.attempt,
        recovery_action=action,
        recovery_digest=digest,
        provider_outcome="not_inspected",
        provider_status="",
        retry_safe=False,
    )


def _transition_expired_running_recovery(
    *,
    inspection: _GenericWebDeployRecoveryInspection,
    store: PostgresRecordStore,
    trace_id: str,
    dependencies: GenericWebDeployRecoveryDependencies,
) -> _GenericWebDeployRecoveryInspection:
    if inspection.reservation.state != "running":
        return inspection
    transition = store.mark_mutation_reconcile_required(
        reservation=inspection.reservation,
        reconciliation_key=inspection.reservation.reconciliation_key,
    )
    if transition.status != "updated" or transition.record is None:
        raise dependencies.http_error(
            status_code=409,
            trace_id=trace_id,
            code="reservation_changed",
            message="Generic web deploy recovery reservation changed before apply.",
        )
    return _GenericWebDeployRecoveryInspection(
        request=inspection.request,
        product=inspection.product,
        context=inspection.context,
        instance=inspection.instance,
        reservation=transition.record,
        observed_at=inspection.observed_at,
        original_fingerprint=inspection.original_fingerprint,
        idempotency_key=inspection.idempotency_key,
        provider_operation_key=inspection.provider_operation_key,
        provider_outcome=inspection.provider_outcome,
        provider_status=inspection.provider_status,
        retry_safe=inspection.retry_safe,
        proposed_action=inspection.proposed_action,
        provider_observation_payload=inspection.provider_observation_payload,
        adapter=inspection.adapter,
        provider_inspection=inspection.provider_inspection,
    )


async def _inspect_generic_web_deploy_recovery(
    *,
    request: Request,
    recovery_request: GenericWebDeployRecoveryDryRunRequest,
    identity: LaunchplaneIdentity,
    record_store: object,
    idempotency_key: str,
    trace_id: str,
    dependencies: GenericWebDeployRecoveryDependencies,
    authorization_actions: tuple[str, ...] = ("generic_web_deploy.execute",),
) -> _GenericWebDeployRecoveryInspection:
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
    if not _recovery_authorization_allows(
        dependencies=dependencies,
        identity=identity,
        actions=authorization_actions,
        product=profile.product,
        context=lane.context,
        instance=lane.instance,
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
    original_payload = raw_payload.get("original_deploy") if isinstance(raw_payload, dict) else None
    if not isinstance(original_payload, dict):
        raise dependencies.http_error(
            status_code=400,
            trace_id=trace_id,
            code="invalid_request",
            message="Generic web deploy recovery requires the exact original deploy payload.",
        )
    original_fingerprint = dependencies.idempotency_request_fingerprint(
        route_path=GENERIC_WEB_DEPLOY_ROUTE,
        payload=original_payload,
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
    operation_context: str
    operation_instance = recovery_request.original_deploy.deploy.instance.strip()
    authoritative_lane = lane
    legacy_provider_effect_started_at = ""

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
                request_deploy_reference=recovery_request.original_deploy.deploy.deploy_reference,
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
        if (
            legacy_snapshot_without_product
            and reservation.provider_effect_phase == "deploy_trigger"
        ):
            try:
                parse_launchplane_mutation_timestamp(
                    reservation.provider_effect_started_at,
                    field_name="provider_effect_started_at",
                )
            except ValueError:
                legacy_provider_effect_started_at = ""
            else:
                legacy_provider_effect_started_at = reservation.provider_effect_started_at

    if not _recovery_authorization_allows(
        dependencies=dependencies,
        identity=identity,
        actions=authorization_actions,
        product=operation_product,
        context=operation_context,
        instance=operation_instance,
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

    adapter = None
    provider_inspection = None
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
            adapter = GenericWebDeployProviderMutationAdapter(
                control_plane_root=dependencies.control_plane_root,
                record_store=record_store,
                deploy_request=recovery_request.original_deploy,
                profile=profile,
                lane=authoritative_lane,
                trace_id=trace_id,
            )
            provider_inspection = adapter.inspect(
                provider_operation_key=provider_operation_key,
                provider_effect_phase=reservation.provider_effect_phase,
                reconciliation_key=reservation.reconciliation_key,
                expected_provider_target_key=reservation.provider_target_key,
                legacy_provider_effect_started_at=legacy_provider_effect_started_at,
            )
            if not provider_inspection.identity_matches:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="reservation_target_conflict",
                    message="Stored generic web deploy recovery target identities conflict.",
                )
            provider_outcome = provider_inspection.observation.outcome
            provider_status = _bounded_recovery_value(
                provider_inspection.observation.deployment_status
            )
            retry_safe = provider_inspection.retry_safe
            legacy_correlation_digest_evidence = (
                provider_inspection.legacy_correlation_digest_evidence
            )
            if legacy_correlation_digest_evidence is None:
                provider_observation_payload = provider_inspection.observation.model_dump(
                    mode="json"
                )
            else:
                provider_observation_payload = {
                    "outcome": provider_outcome,
                    "deployment_status": provider_status,
                }
            if provider_outcome == "present":
                proposed_action = "adopt_observed"
            elif provider_outcome == "absent" and retry_safe:
                proposed_action = "retry_original_operation"
            else:
                proposed_action = "hold_unknown"

    return _GenericWebDeployRecoveryInspection(
        request=recovery_request,
        product=operation_product,
        context=operation_context,
        instance=operation_instance,
        reservation=reservation,
        observed_at=lookup.observed_at,
        original_fingerprint=original_fingerprint,
        idempotency_key=normalized_key,
        provider_operation_key=provider_operation_key,
        provider_outcome=provider_outcome,
        provider_status=provider_status,
        retry_safe=retry_safe,
        proposed_action=proposed_action,
        provider_observation_payload=provider_observation_payload,
        legacy_correlation_digest_evidence=(
            provider_inspection.legacy_correlation_digest_evidence
            if provider_inspection is not None
            else None
        ),
        adapter=adapter,
        provider_inspection=provider_inspection,
    )


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
        inspection = await _inspect_generic_web_deploy_recovery(
            request=request,
            recovery_request=recovery_request,
            identity=identity,
            record_store=record_store,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            dependencies=dependencies,
        )
        return inspection.dry_run_response()

    return dry_run_generic_web_deploy_recovery


def build_generic_web_deploy_recovery_provider_evidence_handler(
    *, dependencies: GenericWebDeployRecoveryDependencies
) -> Callable[..., Any]:
    async def inspect_generic_web_deploy_recovery_provider_evidence(
        request: Request,
        recovery_request: GenericWebDeployRecoveryDryRunRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> GenericWebDeployRecoveryProviderEvidenceResponse:
        trace_id = dependencies.next_trace_id()
        inspection = await _inspect_generic_web_deploy_recovery(
            request=request,
            recovery_request=recovery_request,
            identity=identity,
            record_store=record_store,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            dependencies=dependencies,
            authorization_actions=(
                GENERIC_WEB_DEPLOY_RECOVERY_PROVIDER_EVIDENCE_READ_ACTION,
                "generic_web_deploy.execute",
            ),
        )
        provider_inspection = inspection.provider_inspection
        return GenericWebDeployRecoveryProviderEvidenceResponse(
            reservation_state=inspection.reservation.state,
            reservation_attempt=inspection.reservation.attempt,
            observed_at=inspection.observed_at,
            reconciliation_key_sha256=generic_web_deploy_recovery_identifier_sha256(
                inspection.reservation.reconciliation_key
            ),
            provider_target_key_sha256=generic_web_deploy_recovery_identifier_sha256(
                inspection.reservation.provider_target_key
            ),
            provider_effect_phase=_bounded_recovery_value(
                inspection.reservation.provider_effect_phase
            ),
            provider_evidence=(
                provider_inspection.provider_evidence
                if provider_inspection is not None
                else "not_inspected"
            ),
            provider_status=inspection.provider_status,
            provider_read_error_class=(
                provider_inspection.provider_read_error_class
                if provider_inspection is not None
                else ""
            ),
        )

    return inspect_generic_web_deploy_recovery_provider_evidence


def build_generic_web_deploy_recovery_apply_handler(
    *, dependencies: GenericWebDeployRecoveryDependencies
) -> Callable[..., Any]:
    async def apply_generic_web_deploy_recovery(
        request: Request,
        recovery_request: GenericWebDeployRecoveryApplyRequest,
        identity: Annotated[LaunchplaneIdentity, Depends(dependencies.read_write_identity)],
        record_store: Annotated[object, Depends(dependencies.get_record_store)],
        idempotency_key: Annotated[str, Header(alias="Idempotency-Key")],
    ) -> GenericWebDeployRecoveryApplyResponse:
        trace_id = dependencies.next_trace_id()
        inspection = await _inspect_generic_web_deploy_recovery(
            request=request,
            recovery_request=recovery_request,
            identity=identity,
            record_store=record_store,
            idempotency_key=idempotency_key,
            trace_id=trace_id,
            dependencies=dependencies,
        )
        if not isinstance(record_store, PostgresRecordStore):
            raise dependencies.http_error(
                status_code=503,
                trace_id=trace_id,
                code="database_storage_required",
                message="Generic web deploy recovery apply requires database storage.",
            )
        if inspection.reservation.state == "completed":
            metadata = _stored_recovery_metadata(inspection.reservation)
            if metadata is not None and metadata[0] == recovery_request.expected_recovery_digest:
                return _apply_replay_response(
                    trace_id=trace_id,
                    request=recovery_request,
                    reservation=inspection.reservation,
                    context=inspection.context,
                )
            if inspection.recovery_digest != recovery_request.expected_recovery_digest:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="stale_recovery_digest",
                    message="Reviewed generic web deploy recovery digest no longer matches.",
                )
            return _apply_response(
                trace_id=trace_id,
                inspection=inspection,
                reservation=inspection.reservation,
            )
        if inspection.recovery_digest != recovery_request.expected_recovery_digest:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="stale_recovery_digest",
                message="Reviewed generic web deploy recovery digest no longer matches.",
            )
        if inspection.proposed_action not in {"adopt_observed", "retry_original_operation"}:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="recovery_not_actionable",
                message="Generic web deploy recovery inspection is not safely actionable.",
            )
        if inspection.adapter is None:
            raise RuntimeError("Generic web deploy recovery apply requires an adapter.")
        recovery_metadata = _recovery_metadata(inspection)
        action_inspection = _transition_expired_running_recovery(
            inspection=inspection,
            store=record_store,
            trace_id=trace_id,
            dependencies=dependencies,
        )

        if inspection.proposed_action == "adopt_observed":
            if action_inspection.provider_inspection is None:
                raise RuntimeError("Generic web deploy recovery adoption requires inspection.")
            provider_observation = inspection.adapter.provider_observation_from_inspection(
                provider_operation_key=inspection.provider_operation_key,
                inspection=action_inspection.provider_inspection,
            )
            if provider_observation.outcome != "present":
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="recovery_not_actionable",
                    message="Generic web deploy recovery provider evidence is not adoptable.",
                )
            response_payload = dict(provider_observation.response_payload)
            response_payload["recovery"] = recovery_metadata
            adoption = record_store.adopt_reconciled_mutation(
                reservation=action_inspection.reservation,
                response_status_code=provider_observation.response_status_code,
                response_trace_id=trace_id,
                response_payload=response_payload,
            )
            if adoption.status == "replayed" and adoption.record is not None:
                return _apply_response(
                    trace_id=trace_id,
                    inspection=inspection,
                    reservation=adoption.record,
                )
            if adoption.status != "adopted" or adoption.record is None:
                raise dependencies.http_error(
                    status_code=409,
                    trace_id=trace_id,
                    code="reservation_changed",
                    message="Generic web deploy recovery reservation changed before adoption.",
                )
            return _apply_response(
                trace_id=trace_id,
                inspection=inspection,
                reservation=adoption.record,
            )

        retry = record_store.retry_reconciled_mutation(
            reservation=action_inspection.reservation,
            lease_owner=trace_id,
        )
        if retry.status == "replayed" and retry.record is not None:
            return _apply_response(
                trace_id=trace_id,
                inspection=inspection,
                reservation=retry.record,
            )
        if retry.status != "acquired" or retry.record is None:
            raise dependencies.http_error(
                status_code=409,
                trace_id=trace_id,
                code="reservation_changed",
                message="Generic web deploy recovery reservation changed before retry.",
            )
        result = resume_acquired_provider_operation(
            store=record_store,
            reservation=retry.record,
            response_trace_id=trace_id,
            adapter=_GenericWebRecoveryApplyAdapter(
                delegate=inspection.adapter,
                recovery_metadata=recovery_metadata,
            ),
        )
        if result.status in {"completed", "adopted", "replayed"} and result.record is not None:
            return _apply_response(
                trace_id=trace_id,
                inspection=inspection,
                reservation=result.record,
            )
        raise dependencies.http_error(
            status_code=409,
            trace_id=trace_id,
            code="mutation_reconciliation_required",
            message="Generic web deploy recovery retry requires reconciliation.",
        )

    return apply_generic_web_deploy_recovery
