"""Settle obsolete testing inputs only while their original image is untouched."""

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, cast

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.generic_web_testing_inputs import TestingInputStore, testing_input_authority
from control_plane.provider_operations import (
    ProviderEvidenceLease,
    ProviderMutationOutcome,
    ProviderMutationUnknownError,
    ProviderOperationLease,
    build_provider_operation_key,
    provider_operation_response_payload,
    provider_operation_title,
)
from control_plane.workflows.generic_web_application_lifecycle import application_lifecycle_for_lane
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
TESTING_PRE_IMAGE_SETTLED = "testing_deploy_pre_image_settled"


class _SettlementStore(TestingInputStore, Protocol):
    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord: ...

    def read_deployment_record(self, record_id: str) -> DeploymentRecord: ...

    def write_deployment_record(self, record: DeploymentRecord) -> object: ...


def testing_attempt_was_settled(record: LaunchplaneIdempotencyRecord) -> bool:
    result = record.response_payload.get("result")
    return (
        record.state == "completed"
        and isinstance(result, dict)
        and result.get("error_code") == TESTING_PRE_IMAGE_SETTLED
    )


def _require_target(
    store: _SettlementStore,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    target: GenericWebResolvedDeployTarget,
) -> None:
    current = store.read_product_profile_record(profile.product)
    definition = store.read_dokploy_target_record(
        context_name=lane.context, instance_name=lane.instance
    )
    binding = store.read_dokploy_target_id_record(
        context_name=lane.context, instance_name=lane.instance
    )
    provider = store.read_provider_target_record(
        context_name=lane.context, instance_name=lane.instance
    )
    if (
        not current.is_active
        or current.driver_id != "generic-web"
        or not any(
            item.context == lane.context
            and item.instance == "testing"
            and item.generic_web_cutover is None
            for item in current.lanes
        )
        or lane.instance != "testing"
        or definition.target_type != "application"
        or binding.target_id != target.resolved_target.target_id
        or provider.target_id != binding.target_id
        or provider.provider_id != target.ship_request.provider_id
        or provider.target_category != "application"
        or provider.provider_target_type != "application"
    ):
        raise ValueError("Original testing settlement target authority changed.")


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
        settlement_without_effects = no_effect_checkpoint

        def checkpoint(_phase: str = "") -> None:
            check_execution()
            lease.assert_current()
            _require_target(store, profile, lane, target)
            # This durable controller stage never invokes image save or deploy.
            # Retain it through restoration so a crash cannot lose pre-image proof.
            if settlement_without_effects:
                # Nothing was captured or changed. A failed DB write leaves
                # authoritative no-effect evidence intact for the next retry.
                require_no_effect_evidence()
            else:
                lease.checkpoint_effect(TESTING_PRE_IMAGE_SETTLEMENT_PHASE)

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
                "detail": "Obsolete testing attempt settled before image mutation.",
                "evidence": {
                    **record.post_deploy_update.evidence,
                    **(receipt.evidence() if receipt is not None else {}),
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
        store.write_deployment_record(record)
        return ProviderMutationOutcome(
            response_status_code=502,
            response_payload=provider_operation_response_payload(
                trace_id=trace_id,
                records={"deployment_record_id": record.record_id},
                result={"deploy_status": "fail", "error_code": TESTING_PRE_IMAGE_SETTLED},
            ),
            provider_effect_performed=not settlement_without_effects,
        )
