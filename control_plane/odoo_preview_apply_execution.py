"""In-process execution of a service-issued Odoo preview apply or destroy.

The HTTP route validates the request, resolves the product profile, checks
authorization, and verifies the service-issued plan. Everything after that
point -- the durable provider operation, its reservation, provider
observation/reconciliation, and lifecycle evidence -- lives here so that a
worker can run the same operation without a FastAPI request or a
``LaunchplaneIdentity``. Callers pass the reservation scope explicitly.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, cast

import click

from control_plane.contracts.idempotency_record import (
    build_launchplane_mutation_reservation_id,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.odoo_preview_apply_http import (
    ODOO_PREVIEW_APPLY_ROUTE,
    OdooPreviewApplyConfigError,
    OdooPreviewApplyEnvelope,
    apply_odoo_preview_lifecycle_evidence,
    build_odoo_preview_runtime_identity,
    driver_result_contains_status,
    execute_odoo_preview_apply_result,
    observe_odoo_preview_apply_result,
    odoo_preview_supersession_is_quiescent,
)
from control_plane.provider_operations import (
    DurableProviderOperationResult,
    DurableProviderOperationStore,
    ProviderMutationOutcome,
    ProviderMutationRejectedError,
    ProviderMutationUnknownError,
    ProviderObservation,
    ProviderObservationOutcome,
    ProviderOperationLease,
    ProviderTargetSupersession,
    provider_operation_response_payload,
    provider_operation_title,
    run_durable_provider_operation,
)
from control_plane.workflows.odoo_preview_runtime import (
    ODOO_PREVIEW_SUPERSESSION_GRACE_SECONDS,
    OdooPreviewApplyInputsResult,
    OdooPreviewDokployApplyResult,
)

ExecuteOdooPreviewApply = Callable[..., dict[str, object]]
ObserveOdooPreviewApply = Callable[..., tuple[str, dict[str, object] | None, bool]]
OdooPreviewSupersessionQuiescenceCheck = Callable[..., bool]


def _string_value(value: Any) -> str:
    return str(value)


class OdooPreviewProviderMutationAdapter:
    def __init__(
        self,
        *,
        control_plane_root: Path,
        record_store: object,
        profile: LaunchplaneProductProfileRecord,
        apply_request: OdooPreviewApplyEnvelope,
        issued_plan: OdooPreviewApplyInputsResult,
        database_url: str | None,
        trace_id: str,
        deployment_record_id: str,
        execute_apply: ExecuteOdooPreviewApply = execute_odoo_preview_apply_result,
        observe_apply: ObserveOdooPreviewApply = observe_odoo_preview_apply_result,
    ) -> None:
        self._control_plane_root = control_plane_root
        self._record_store = record_store
        self._profile = profile
        self._apply_request = apply_request
        self._issued_plan = issued_plan
        self._database_url = database_url
        self._trace_id = trace_id
        self._deployment_record_id = deployment_record_id
        self._execute_apply = execute_apply
        self._observe_apply = observe_apply
        self._runtime_identity = (
            build_odoo_preview_runtime_identity(
                profile=profile,
                issued_plan=issued_plan,
                deployment_record_id=deployment_record_id,
            )
            if issued_plan.operation == "refresh"
            else None
        )

    def reconciliation_key(self) -> str:
        plan = self._apply_request.apply.dry_run_plan
        return (
            f"dokploy:compose:{self._profile.preview.context.strip()}:{plan.compose_name.strip()}"
        )

    def target_key(self) -> str:
        reconciliation_key = self.reconciliation_key()
        return f"dokploy-provider-target:{hashlib.sha256(reconciliation_key.encode()).hexdigest()}"

    def _finalize_successful_result(
        self,
        driver_result: dict[str, object],
    ) -> tuple[dict[str, object], dict[str, object], int]:
        lifecycle_records = apply_odoo_preview_lifecycle_evidence(
            control_plane_root_path=self._control_plane_root,
            record_store=self._record_store,
            profile=self._profile,
            issued_plan=self._issued_plan,
            driver_result=driver_result,
            runtime_identity=self._runtime_identity,
        )
        lifecycle_status = _string_value(
            lifecycle_records.get("lifecycle_evidence_status") or ""
        ).strip()
        if lifecycle_status == "stale":
            blocked_result = OdooPreviewDokployApplyResult.model_validate(driver_result).model_copy(
                update={
                    "status": "blocked",
                    "error_message": (
                        "The Odoo preview operation completed at the provider but was "
                        "superseded by newer Launchplane lifecycle authority."
                    ),
                }
            )
            return blocked_result.model_dump(mode="json"), lifecycle_records, 409
        if lifecycle_status not in {"applied", "replayed"}:
            raise ValueError("Successful Odoo preview apply requires lifecycle evidence.")
        return driver_result, lifecycle_records, 202

    def observe(
        self,
        provider_operation_key: str,
        provider_effect_phase: str,
        reconciliation_key: str,
    ) -> ProviderObservation:
        del reconciliation_key
        observation_outcome, driver_result, retry_safe = self._observe_apply(
            control_plane_root_path=self._control_plane_root,
            profile=self._profile,
            request=self._apply_request,
            database_url=self._database_url,
            provider_operation_title=provider_operation_title(provider_operation_key),
            provider_effect_phase=provider_effect_phase,
        )
        if driver_result is None:
            return ProviderObservation(
                outcome=cast(ProviderObservationOutcome, observation_outcome),
                retry_safe=retry_safe,
            )
        driver_result.pop("provider_effect_attempted", None)
        response_status_code = 202
        records: dict[str, object] = {}
        if _string_value(driver_result.get("status", "")).strip() == "pass":
            driver_result, records, response_status_code = self._finalize_successful_result(
                driver_result
            )
        terminal_failure = _string_value(driver_result.get("status", "")).strip() == "fail"
        return ProviderObservation(
            outcome="present",
            response_status_code=502 if terminal_failure else response_status_code,
            response_payload=provider_operation_response_payload(
                trace_id=self._trace_id,
                records=records,
                result=driver_result,
            ),
        )

    def apply(
        self, provider_operation_key: str, lease: ProviderOperationLease
    ) -> ProviderMutationOutcome:
        try:
            driver_result = self._execute_apply(
                control_plane_root_path=self._control_plane_root,
                record_store=self._record_store,
                profile=self._profile,
                request=self._apply_request,
                issued_plan=self._issued_plan,
                database_url=self._database_url,
                provider_operation_title=provider_operation_title(provider_operation_key),
                provider_effect_checkpoint=lease.checkpoint_effect,
                provider_lease_check=lease.assert_current,
                deployment_record_id=self._deployment_record_id,
                runtime_identity=self._runtime_identity,
            )
        except (
            OdooPreviewApplyConfigError,
            FileNotFoundError,
            ValueError,
            click.ClickException,
        ) as error:
            raise ProviderMutationRejectedError(error)
        provider_effect_attempted = driver_result.pop("provider_effect_attempted", False) is True
        driver_status = _string_value(driver_result.get("status", "")).strip()
        if driver_status == "fail" and provider_effect_attempted:
            raise ProviderMutationUnknownError(
                _string_value(driver_result.get("error_message", "")).strip()
                or "Odoo preview provider outcome requires reconciliation."
            )
        response_status_code = 202
        records: dict[str, object] = {}
        lifecycle_finalized = driver_status == "pass"
        if lifecycle_finalized:
            lease.assert_current()
            driver_result, records, response_status_code = self._finalize_successful_result(
                driver_result
            )
            lease.assert_current()
            driver_status = _string_value(driver_result.get("status", "")).strip()
        return ProviderMutationOutcome(
            response_status_code=response_status_code,
            response_payload=provider_operation_response_payload(
                trace_id=self._trace_id,
                records=records,
                result=driver_result,
            ),
            durable=lifecycle_finalized
            or (
                not driver_result_contains_status(driver_result, "blocked")
                and driver_status != "fail"
            ),
            provider_effect_performed=provider_effect_attempted,
        )


@dataclass(frozen=True)
class OdooPreviewApplyOperation:
    """A prepared Odoo preview provider operation, ready to run durably."""

    reservation_scope: str
    idempotency_key: str
    request_fingerprint: str
    trace_id: str
    adapter: OdooPreviewProviderMutationAdapter
    target_supersession: ProviderTargetSupersession
    route_path: str = ODOO_PREVIEW_APPLY_ROUTE

    def run(self, store: DurableProviderOperationStore) -> DurableProviderOperationResult:
        return run_durable_provider_operation(
            store=store,
            scope=self.reservation_scope,
            route_path=self.route_path,
            idempotency_key=self.idempotency_key,
            request_fingerprint=self.request_fingerprint,
            lease_owner=self.trace_id,
            response_trace_id=self.trace_id,
            adapter=self.adapter,
            target_supersession=self.target_supersession,
        )


def prepare_odoo_preview_apply_operation(
    *,
    control_plane_root: Path,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    apply_request: OdooPreviewApplyEnvelope,
    issued_plan: OdooPreviewApplyInputsResult,
    reservation_scope: str,
    idempotency_key: str,
    request_fingerprint: str,
    trace_id: str,
    execute_apply: ExecuteOdooPreviewApply = execute_odoo_preview_apply_result,
    observe_apply: ObserveOdooPreviewApply = observe_odoo_preview_apply_result,
    supersession_is_quiescent: OdooPreviewSupersessionQuiescenceCheck = (
        odoo_preview_supersession_is_quiescent
    ),
) -> OdooPreviewApplyOperation:
    """Build the adapter and supersession policy for a service-issued plan.

    ``apply_request`` must already be the service-validated request returned by
    ``validate_odoo_preview_issued_plan`` for ``issued_plan``.
    """

    database_url = getattr(record_store, "database_url", None)
    adapter = OdooPreviewProviderMutationAdapter(
        control_plane_root=control_plane_root,
        record_store=record_store,
        profile=profile,
        apply_request=apply_request,
        issued_plan=issued_plan,
        database_url=database_url,
        trace_id=trace_id,
        deployment_record_id=build_launchplane_mutation_reservation_id(
            scope=reservation_scope,
            route_path=ODOO_PREVIEW_APPLY_ROUTE,
            idempotency_key=idempotency_key,
        ),
        execute_apply=execute_apply,
        observe_apply=observe_apply,
    )
    target_supersession = ProviderTargetSupersession(
        response_status_code=409,
        response_payload=provider_operation_response_payload(
            trace_id=trace_id,
            records={},
            result={
                "status": "fail",
                "error_message": (
                    "The earlier Odoo preview apply was superseded by an "
                    f"authoritative {issued_plan.operation} after its recovery lease expired."
                ),
            },
        ),
        minimum_expired_seconds=ODOO_PREVIEW_SUPERSESSION_GRACE_SECONDS,
        quiescence_check=lambda _reservation: supersession_is_quiescent(
            control_plane_root_path=control_plane_root,
            request=apply_request,
            database_url=getattr(record_store, "database_url", None),
        ),
    )
    return OdooPreviewApplyOperation(
        reservation_scope=reservation_scope,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        trace_id=trace_id,
        adapter=adapter,
        target_supersession=target_supersession,
    )


def run_odoo_preview_apply_operation(
    *,
    store: DurableProviderOperationStore,
    control_plane_root: Path,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    apply_request: OdooPreviewApplyEnvelope,
    issued_plan: OdooPreviewApplyInputsResult,
    reservation_scope: str,
    idempotency_key: str,
    request_fingerprint: str,
    trace_id: str,
    execute_apply: ExecuteOdooPreviewApply = execute_odoo_preview_apply_result,
    observe_apply: ObserveOdooPreviewApply = observe_odoo_preview_apply_result,
    supersession_is_quiescent: OdooPreviewSupersessionQuiescenceCheck = (
        odoo_preview_supersession_is_quiescent
    ),
) -> DurableProviderOperationResult:
    """Run a service-issued Odoo preview apply/destroy durably, in-process.

    Blocking: callers on an event loop should run it in a worker thread. The
    ``store`` is normally ``record_store`` itself (a ``PostgresRecordStore``).
    """

    return prepare_odoo_preview_apply_operation(
        control_plane_root=control_plane_root,
        record_store=record_store,
        profile=profile,
        apply_request=apply_request,
        issued_plan=issued_plan,
        reservation_scope=reservation_scope,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        trace_id=trace_id,
        execute_apply=execute_apply,
        observe_apply=observe_apply,
        supersession_is_quiescent=supersession_is_quiescent,
    ).run(store)
