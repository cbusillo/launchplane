"""In-process execution of a service-issued Odoo preview apply or destroy.

The HTTP route validates the request, resolves the product profile, checks
authorization, and verifies the service-issued plan. Everything after that
point -- the durable provider operation, its reservation, provider
observation/reconciliation, and lifecycle evidence -- lives here so that a
worker can run the same operation without a FastAPI request or a
``LaunchplaneIdentity``. Callers pass the reservation scope explicitly.
"""

from __future__ import annotations

from contextlib import nullcontext
from collections.abc import Callable
from dataclasses import dataclass
import hashlib
from pathlib import Path
from typing import Any, cast

import click
from control_plane.lane_movement import LaneMovementRefused

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
# Runs after the reservation is held and before the provider apply; raising
# releases the reservation with no provider effect and re-raises to the caller.
OdooPreviewPreMutationGuard = Callable[[], None]


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
        pre_mutation_guard: OdooPreviewPreMutationGuard | None = None,
        qualify_existing_refresh: bool = False,
    ) -> None:
        self._control_plane_root = control_plane_root
        self._pre_mutation_guard = pre_mutation_guard
        self._qualify_existing_refresh = qualify_existing_refresh
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
        self.execution_check: Callable[[], None] = lambda: None
        self.schedule_binding: dict[str, object] | None = None

    def capture_schedule_binding(self) -> None:
        from control_plane.dokploy import post_deploy, source

        plan = self._apply_request.apply.dry_run_plan
        target_id = plan.compose_ref
        if target_id.startswith("${created."):
            target_id = plan.template_compose_id
        if not target_id:
            return
        try:
            host, token = source.read_dokploy_config(
                control_plane_root=self._control_plane_root, database_url=self._database_url
            )
            binding = post_deploy.compose_data_workflow_binding(
                host=host,
                token=token,
                target_definition=source.DokployTargetDefinition(
                    context=plan.product,
                    instance=plan.preview_slug,
                    target_id=target_id,
                    target_name=plan.compose_name,
                ),
            )
        except (FileNotFoundError, ValueError, click.ClickException):
            # Missing read evidence is retained as missing, never synthesized.
            return
        self.schedule_binding = binding.model_dump(mode="json")

    def original_intent(self) -> dict[str, object]:
        # The HTTP service resolves environment values from managed records;
        # caller values are ignored and must never become plaintext recovery data.
        retained_request = self._apply_request.model_copy(
            update={
                "apply": self._apply_request.apply.model_copy(update={"environment_values": {}})
            }
        )
        return {
            "product": self._profile.product,
            "issued_plan": self._issued_plan.model_dump(mode="json"),
            "apply_request": retained_request.model_dump(mode="json"),
            **({"schedule_binding": self.schedule_binding} if self.schedule_binding else {}),
        }

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
        self.execution_check()
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
        self.execution_check()
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
        self.execution_check()
        if self._pre_mutation_guard is not None:
            try:
                self._pre_mutation_guard()
            except Exception as error:
                raise ProviderMutationRejectedError(error) from error

        effect_started = False

        def checkpoint_effect(phase: str) -> None:
            nonlocal effect_started
            self.execution_check()
            if (
                not effect_started or self._qualify_existing_refresh
            ) and self._pre_mutation_guard is not None:
                try:
                    self._pre_mutation_guard()
                except Exception as error:
                    if not effect_started:
                        raise ProviderMutationRejectedError(error) from error
                    raise ProviderMutationUnknownError(str(error)) from error
            lease.checkpoint_effect(phase)
            effect_started = True

        try:
            from control_plane.storage.postgres import PostgresRecordStore

            authority = (
                cast(PostgresRecordStore, self._record_store).serialize_existing_preview_refresh()
                if self._qualify_existing_refresh
                else nullcontext()
            )
            with authority:
                if self._pre_mutation_guard is not None:
                    self._pre_mutation_guard()
                driver_result = self._execute_apply(
                    control_plane_root_path=self._control_plane_root,
                    record_store=self._record_store,
                    profile=self._profile,
                    request=self._apply_request,
                    issued_plan=self._issued_plan,
                    database_url=self._database_url,
                    provider_operation_title=provider_operation_title(provider_operation_key),
                    provider_effect_checkpoint=checkpoint_effect,
                    provider_lease_check=lease.assert_current,
                    deployment_record_id=self._deployment_record_id,
                    runtime_identity=self._runtime_identity,
                    **(
                        {"qualify_existing_refresh": True} if self._qualify_existing_refresh else {}
                    ),
                )
        except LaneMovementRefused as error:
            from control_plane.lane_movement import LaneBuild, record_preview_refusal

            manifest = self._apply_request.apply.manifest
            record_preview_refusal(
                record_store=self._record_store,
                profile=self._profile,
                preview_slug=self._issued_plan.preview_slug,
                desired=LaneBuild(
                    manifest.artifact_id if manifest else self._apply_request.apply.image_reference,
                    manifest.source_commit
                    if manifest
                    else self._issued_plan.plan_request.source_git_ref,
                    self._apply_request.apply.image_reference,
                ),
                error=error,
                target_type="compose",
            )
            return ProviderMutationOutcome(
                response_status_code=409,
                response_payload=provider_operation_response_payload(
                    trace_id=self._trace_id,
                    records={},
                    result={
                        "status": "blocked",
                        "error_code": f"lane_movement.{error.code}",
                        "error_message": error.record_failure().description,
                    },
                ),
                durable=error.code != "source_order_unavailable",
                provider_effect_performed=False,
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

    def run(
        self, store: DurableProviderOperationStore, *, allow_mutation: bool = True
    ) -> DurableProviderOperationResult:
        from control_plane.storage.postgres import PostgresRecordStore

        if not isinstance(store, PostgresRecordStore):
            raise ValueError("Odoo preview execution requires database storage.")
        existing = store.read_idempotency_record(
            scope=self.reservation_scope,
            route_path=self.route_path,
            idempotency_key=self.idempotency_key,
        )
        retained_intent = (
            store.read_original_mutation_intent(existing) if existing is not None else {}
        )
        if existing is not None and existing.state != "completed" and not retained_intent:
            return DurableProviderOperationResult("reconcile_required", existing, 409, {})
        with store.testing_replacement_execution_lock(
            preview_execution_lock_name(self.adapter.target_key())
        ) as check:
            if check is None:
                return DurableProviderOperationResult("in_progress", None, 409, {})
            check()
            if existing is None:
                self.adapter.capture_schedule_binding()
            elif isinstance(retained_intent.get("schedule_binding"), dict):
                self.adapter.schedule_binding = retained_intent["schedule_binding"]
            return self.run_excluded(store, allow_mutation=allow_mutation, check=check)

    def run_excluded(
        self,
        store: DurableProviderOperationStore,
        *,
        allow_mutation: bool,
        check: Callable[[], None],
    ) -> DurableProviderOperationResult:
        check()
        self.adapter.execution_check = check
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
            allow_mutation=allow_mutation,
            original_intent=self.adapter.original_intent(),
        )


def preview_execution_lock_name(target_key: str) -> str:
    return f"preview:{target_key}"


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
    pre_mutation_guard: OdooPreviewPreMutationGuard | None = None,
    qualify_existing_refresh: bool = False,
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
        pre_mutation_guard=pre_mutation_guard,
        qualify_existing_refresh=qualify_existing_refresh,
    )

    def qualified_supersession(reservation: object) -> bool:
        if pre_mutation_guard is not None:
            pre_mutation_guard()
        return _original_supersession_is_quiescent(
            reservation=reservation,
            profile=profile,
            request=apply_request,
            root=control_plane_root,
            database_url=database_url,
            check=supersession_is_quiescent,
            store=record_store,
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
        quiescence_check=qualified_supersession,
    )

    return OdooPreviewApplyOperation(
        reservation_scope=reservation_scope,
        idempotency_key=idempotency_key,
        request_fingerprint=request_fingerprint,
        trace_id=trace_id,
        adapter=adapter,
        target_supersession=target_supersession,
    )


def _original_supersession_is_quiescent(
    *,
    reservation: object,
    profile: LaunchplaneProductProfileRecord,
    request: OdooPreviewApplyEnvelope,
    root: Path,
    database_url: str | None,
    check: OdooPreviewSupersessionQuiescenceCheck,
    store: object,
) -> bool:
    from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
    from control_plane.preview_reservation_recovery import original_preview_intent

    from control_plane.storage.postgres import PostgresRecordStore

    if not isinstance(reservation, LaunchplaneIdempotencyRecord) or not isinstance(
        store, PostgresRecordStore
    ):
        return False
    try:
        original = original_preview_intent(reservation, profile=profile, store=store)
    except ValueError:
        return False
    if (
        original.apply_request.apply.dry_run_plan.compose_ref
        != request.apply.dry_run_plan.compose_ref
    ):
        old_plan = original.apply_request.apply.dry_run_plan
        new_plan = request.apply.dry_run_plan
        if (
            not old_plan.compose_ref.startswith("${created.")
            or old_plan.environment_id != new_plan.environment_id
        ):
            return False
        from control_plane.workflows.odoo_preview_runtime import resolve_original_preview_compose_id

        try:
            resolved_id = resolve_original_preview_compose_id(
                control_plane_root=root,
                request=original.apply_request.apply,
                database_url=database_url,
            )
        except (ValueError, click.ClickException):
            return False
        if not resolved_id or resolved_id != new_plan.compose_ref:
            return False
    return check(
        control_plane_root_path=root,
        request=original.apply_request,
        database_url=database_url,
        schedule_binding=store.read_original_mutation_intent(reservation).get("schedule_binding"),
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
    pre_mutation_guard: OdooPreviewPreMutationGuard | None = None,
    allow_mutation: bool = True,
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
        pre_mutation_guard=pre_mutation_guard,
    ).run(store, allow_mutation=allow_mutation)
