"""Settle safe testing attempts and recognize their deterministic successors."""

from collections.abc import Callable
from pathlib import Path
from contextlib import nullcontext
from typing import Protocol, cast

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.generic_web_testing_inputs import (
    TestingInputStore,
    TESTING_DISPATCH_REFUSED_PHASE,
    testing_input_authority,
    require_testing_application_target_authority,
)
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderMutationUnknownError,
    ProviderOperationLease,
    build_provider_operation_key,
    provider_operation_response_payload,
    provider_operation_title,
)
from control_plane.workflows.generic_web_application_lifecycle import (
    application_lifecycle_for_lane,
    DokployApplicationLifecycle,
)
from control_plane.workflows.generic_web_execution import (
    ExecutionRecorder,
    ExecutionReceipt,
    ExecutionRecordStore,
    execution_receipt,
    require_recorded_execution,
)
from control_plane.workflows.generic_web_application_snapshot import (
    ApplicationLifecycleSnapshot,
    generic_web_provider_deployment_record_id,
    recorded_application_lifecycle,
)
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebDeployProvider,
    GenericWebResolvedDeployTarget,
)
from control_plane.workflows.generic_web_preview import serialize_generic_web_product_operation
from control_plane.workflows.ship import build_deployment_record, utc_now_timestamp


TESTING_PRE_IMAGE_SETTLEMENT_PHASE = "testing_pre_image_settlement"
TESTING_POST_IMAGE_SETTLEMENT_PHASE = "testing_failure_settlement"
TESTING_PRE_IMAGE_SETTLED = "testing_deploy_pre_image_settled"
TESTING_DISPATCH_REFUSED_SETTLED = "testing_deploy_dispatch_refused_settled"


class _SettlementStore(TestingInputStore, ExecutionRecordStore, Protocol):
    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...

    def write_deployment_record(self, record: DeploymentRecord) -> object: ...


def testing_attempt_was_settled(record: LaunchplaneIdempotencyRecord) -> bool:
    result = record.response_payload.get("result")
    return record.state == "completed" and (
        record.provider_effect_phase == TESTING_POST_IMAGE_SETTLEMENT_PHASE
        or (
            isinstance(result, dict)
            and result.get("error_code")
            in {TESTING_PRE_IMAGE_SETTLED, TESTING_DISPATCH_REFUSED_SETTLED}
        )
    )


def _require_target(
    store: _SettlementStore,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    target: GenericWebResolvedDeployTarget,
) -> None:
    require_testing_application_target_authority(
        store,
        profile,
        lane,
        target_id=target.resolved_target.target_id,
        provider_id=target.ship_request.provider_id,
    )


def _record_and_receipt(
    store: _SettlementStore,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    target: GenericWebResolvedDeployTarget,
    operation_key: str,
) -> tuple[DeploymentRecord | None, ApplicationLifecycleSnapshot | None]:
    try:
        record = store.read_deployment_record(
            generic_web_provider_deployment_record_id(
                operation_key, context=lane.context, instance=lane.instance
            )
        )
    except FileNotFoundError:
        return None, None
    if (
        record.context != lane.context
        or record.instance != lane.instance
        or record.artifact_identity is None
        or record.artifact_identity.artifact_id != target.ship_request.artifact_id
        or record.source_git_ref != target.ship_request.source_git_ref
        or record.deploy.status == "pass"
        or record.resolved_target is None
        or record.resolved_target.target_id != target.resolved_target.target_id
        or record.resolved_target.target_type != "application"
    ):
        raise ValueError("Original testing settlement record changed.")
    receipt = recorded_application_lifecycle(record)
    if receipt is not None and (
        (
            receipt.product,
            receipt.provider_id,
            receipt.target_id,
            receipt.mode,
        )
        != (
            profile.product,
            target.ship_request.provider_id,
            target.resolved_target.target_id,
            "deploy",
        )
        or receipt.state == "qualified"
        or not set(receipt.managed_names) <= receipt.originals.keys()
    ):
        raise ValueError("Original testing lifecycle baseline is not exact.")
    return record, receipt


def testing_pre_image_settlement_required(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    target: GenericWebResolvedDeployTarget,
    operation_key: str,
    phase: str,
    superseded: bool = False,
    no_effect_checkpoint: bool = False,
) -> bool:
    """Pure local classification; the caller separately requires provider absence."""
    if not target.testing_runtime_digest or target.resolved_target.target_type != "application":
        return False
    store = cast(_SettlementStore, record_store)
    _require_target(store, profile, lane, target)
    record, receipt = _record_and_receipt(store, profile, lane, target, operation_key)
    if (
        lane.generic_web_extensions.configured
        and receipt is None
        and not (no_effect_checkpoint and not phase)
    ):
        return False
    if phase == TESTING_DISPATCH_REFUSED_PHASE:
        # Only the running provider can write this after its image-save reply
        # and before any trigger checkpoint. No current input grant is needed
        # to fail this original attempt without dispatching its saved image.
        return True
    pre_image = phase in {
        "",
        "application_lifecycle_prepared",
        "application_schedule_quiesce",
        "runtime_registry_prepare",
        "runtime_environment_update",
        "runtime_inputs_verified",
        TESTING_PRE_IMAGE_SETTLEMENT_PHASE,
    }
    if receipt is not None and receipt.state == "held" and not phase:
        pre_image = False
    if (
        receipt is not None
        and receipt.state == "restored"
        and record is not None
        and record.deploy.status == "fail"
    ):
        # The failure handler restores only when no image mutation was attempted.
        pre_image = True
    if phase.startswith("application_") and receipt is None:
        return False
    if not pre_image:
        return False
    return (
        phase == TESTING_PRE_IMAGE_SETTLEMENT_PHASE
        or superseded
        or testing_input_authority(store, profile, lane) != target.testing_runtime_digest
    )


def settle_testing_pre_image_attempt(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    target: GenericWebResolvedDeployTarget,
    operation_key: str,
    phase: str,
    no_effect_checkpoint: bool,
    superseded: bool,
    provider: GenericWebDeployProvider,
    control_plane_root: Path,
    lease: ProviderOperationLease,
    check_execution: Callable[[], None],
    trace_id: str,
) -> ProviderMutationOutcome:
    root = control_plane_root
    store = cast(_SettlementStore, record_store)
    with serialize_generic_web_product_operation(
        record_store=store, product=profile.product, exclusive=True
    ):
        check_execution()
        lease.assert_current()
        refused_dispatch = phase == TESTING_DISPATCH_REFUSED_PHASE

        def require_refusal_evidence() -> None:
            if not refused_dispatch:
                return
            if not isinstance(lease, ProviderEvidenceLease):
                raise ProviderMutationUnknownError(
                    "Original dispatch-refusal lease is unavailable."
                )
            reservation = lease.evidence_reservation()
            if reservation.provider_effect_phase != TESTING_DISPATCH_REFUSED_PHASE or (
                build_provider_operation_key(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                    request_fingerprint=reservation.request_fingerprint,
                    reconciliation_key=reservation.reconciliation_key,
                )
                != operation_key
            ):
                raise ProviderMutationUnknownError("Original dispatch-refusal proof changed.")

        require_refusal_evidence()

        def require_no_effect_evidence() -> None:
            if not isinstance(lease, ProviderEvidenceLease):
                raise ProviderMutationUnknownError(
                    "Original no-effect lease evidence is unavailable."
                )
            reservation = lease.evidence_reservation()
            if (
                reservation.provider_effect_phase
                or reservation.provider_effect_started_at
                or build_provider_operation_key(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                    request_fingerprint=reservation.request_fingerprint,
                    reconciliation_key=reservation.reconciliation_key,
                )
                != operation_key
            ):
                raise ProviderMutationUnknownError(
                    "Original no-effect reservation evidence changed."
                )

        if no_effect_checkpoint:
            require_no_effect_evidence()
        if not testing_pre_image_settlement_required(
            record_store=store,
            profile=profile,
            lane=lane,
            target=target,
            operation_key=operation_key,
            phase=phase,
            superseded=superseded,
            no_effect_checkpoint=no_effect_checkpoint,
        ):
            raise ProviderMutationUnknownError("Original pre-image settlement is no longer proved.")
        observed = provider.observe_artifact_deploy(
            control_plane_root=root,
            resolved_deploy_target=target,
            deployment_title=provider_operation_title(operation_key),
        )
        if observed.outcome != "absent":
            raise ProviderMutationUnknownError(
                "Original testing image outcome is no longer absent."
            )
        record, receipt = _record_and_receipt(store, profile, lane, target, operation_key)
        recorder = (
            ExecutionRecorder(
                store,
                record.record_id,
                execution_receipt(record) or ExecutionReceipt(),
                expected_record=record,
            )
            if record is not None
            else None
        )
        if recorder is not None:
            require_recorded_execution(recorder.receipt)
        settlement_without_effects = no_effect_checkpoint

        def checkpoint(_phase: str = "") -> None:
            check_execution()
            lease.assert_current()
            _require_target(store, profile, lane, target)
            require_refusal_evidence()
            # This durable controller stage never invokes image save or deploy.
            # Retain it through restoration so a crash cannot lose pre-image proof.
            if settlement_without_effects:
                # Nothing was captured or changed. A failed DB write leaves
                # authoritative no-effect evidence intact for the next retry.
                require_no_effect_evidence()
            else:
                lease.checkpoint_effect(
                    TESTING_DISPATCH_REFUSED_PHASE
                    if refused_dispatch
                    else TESTING_PRE_IMAGE_SETTLEMENT_PHASE
                )

        checkpoint()
        if receipt is not None and settlement_without_effects:
            if receipt.state == "prepared" and not receipt.resume_from_hold:
                # Capture only read the unchanged provider baseline. Preserve
                # inherited holds instead of claiming to have restored them.
                receipt.state = "restored"
        elif receipt is not None:
            lifecycle = application_lifecycle_for_lane(
                control_plane_root=root,
                snapshot=receipt,
                provider=provider,
                target_type="application",
                target_id=target.resolved_target.target_id,
                checkpoint=checkpoint,
                effect_started=lambda: None,
            )
            assert lifecycle is not None
            if isinstance(lifecycle, DokployApplicationLifecycle):
                lifecycle.execution_recorder = recorder
            if refused_dispatch or receipt.resume_from_hold or receipt.state == "held":
                lifecycle.hold()
                receipt.state = "held"
            else:
                lifecycle.restore()
                receipt.state = "restored"
        finished = utc_now_timestamp()
        if record is None:
            record = build_deployment_record(
                request=target.ship_request,
                record_id=generic_web_provider_deployment_record_id(
                    operation_key, context=lane.context, instance=lane.instance
                ),
                deployment_id="control-plane-dokploy",
                deployment_status="fail",
                started_at=finished,
                finished_at=finished,
                resolved_target=target.resolved_target,
                deployed_target=target.deployed_target,
                delegated_executor=provider.delegated_executor,
            )
        update = record.post_deploy_update.model_copy(
            update={
                "attempted": (receipt is not None and not settlement_without_effects)
                or record.post_deploy_update.attempted,
                "status": "fail"
                if (receipt is not None and not settlement_without_effects)
                or record.post_deploy_update.attempted
                else "skipped",
                "detail": (
                    "Testing dispatch refused after image save; schedules remain held."
                    if refused_dispatch
                    else "Obsolete testing attempt settled before image mutation."
                ),
                "evidence": {
                    **record.post_deploy_update.evidence,
                    **(receipt.evidence() if receipt is not None else {}),
                    **(recorder.evidence() if recorder is not None else {}),
                },
            }
        )
        record = record.model_copy(
            update={
                "deploy": record.deploy.model_copy(
                    update={"status": "fail", "finished_at": finished}
                ),
                "post_deploy_update": update,
            }
        )
        checkpoint()
        if recorder is not None:
            require_recorded_execution(recorder.receipt)
        publisher = getattr(store, "publish_generic_web_testing_result", None)
        if publisher is not None:
            try:
                inventory = getattr(store, "read_environment_inventory")(
                    context_name=lane.context, instance_name=lane.instance
                )
            except FileNotFoundError:
                inventory = None
            guard = getattr(store, "provider_evidence_guard", None)

            def authority() -> None:
                check_execution()
                _require_target(store, profile, lane, target)

            with (
                guard(lease.evidence_reservation, authority_checkpoint=authority)
                if guard is not None and isinstance(lease, ProviderEvidenceLease)
                else nullcontext()
            ):
                publisher(
                    expected_deployment=recorder.expected_record if recorder is not None else None,
                    expected_inventory=inventory,
                    deployment=record,
                    inventory=None,
                )
        else:
            store.write_deployment_record(record)
        return ProviderMutationOutcome(
            response_status_code=502,
            response_payload=provider_operation_response_payload(
                trace_id=trace_id,
                records={"deployment_record_id": record.record_id},
                result={
                    "deploy_status": "fail",
                    "error_code": TESTING_DISPATCH_REFUSED_SETTLED
                    if refused_dispatch
                    else TESTING_PRE_IMAGE_SETTLED,
                },
            ),
            provider_effect_performed=not settlement_without_effects,
        )
