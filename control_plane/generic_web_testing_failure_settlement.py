"""Hold-only recovery of a proved failed application testing deployment."""

from collections.abc import Callable
from pathlib import Path
from typing import cast

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.generic_web_testing_inputs import require_testing_application_target_authority
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
    has_recorded_application_lifecycle,
    recorded_application_lifecycle,
)
from control_plane.workflows.generic_web_deploy import (
    GenericWebDeployStore,
    record_observed_generic_web_deploy,
)
from control_plane.workflows.generic_web_deploy_provider import (
    GenericWebDeployProvider,
    GenericWebResolvedDeployTarget,
    generic_web_provider_deployment_succeeded,
    qualify_testing_application_observation,
)
from control_plane.workflows.generic_web_preview import serialize_generic_web_product_operation


TESTING_FAILURE_SETTLEMENT_PHASE = "testing_failure_settlement"
TESTING_FAILURE_SETTLEMENT_PHASES = frozenset(
    {
        "deploy_trigger",
        "application_schedule_hold",
        "records_commit",
        TESTING_FAILURE_SETTLEMENT_PHASE,
    }
)


def testing_failure_lifecycle_receipt(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    target: GenericWebResolvedDeployTarget,
    operation_key: str,
    phase: str,
) -> tuple[DeploymentRecord | None, ApplicationLifecycleSnapshot | None]:
    """Validate existing original proof, without reconstructing a missing baseline."""
    if (
        lane.instance != "testing"
        or target.resolved_target.target_type != "application"
        or phase not in TESTING_FAILURE_SETTLEMENT_PHASES
    ):
        raise ValueError("Original testing failure phase is not recoverable.")
    store = cast(GenericWebDeployStore, record_store)
    try:
        record: DeploymentRecord | None = store.read_deployment_record(
            generic_web_provider_deployment_record_id(
                operation_key, context=lane.context, instance=lane.instance
            )
        )
    except FileNotFoundError:
        record = None
    if record is not None and (
        record.deploy.status == "pass"
        or record.artifact_identity is None
        or record.resolved_target is None
        or (
            record.context,
            record.instance,
            record.artifact_identity.artifact_id,
            record.source_git_ref,
            record.resolved_target.target_type,
            record.resolved_target.target_id,
        )
        != (
            lane.context,
            lane.instance,
            target.ship_request.artifact_id,
            target.ship_request.source_git_ref,
            "application",
            target.resolved_target.target_id,
        )
    ):
        raise ValueError("Original testing failure record changed.")
    receipt = recorded_application_lifecycle(record) if record is not None else None
    if receipt is None:
        if (
            phase in {"application_schedule_hold", TESTING_FAILURE_SETTLEMENT_PHASE}
            or lane.generic_web_extensions.configured
            or has_recorded_application_lifecycle(
                store, context=lane.context, instance=lane.instance
            )
        ):
            raise ValueError("Original testing lifecycle baseline is missing.")
        return record, None
    if (
        receipt.state not in {"prepared", "held"}
        or (
            receipt.product,
            receipt.context,
            receipt.instance,
            receipt.provider_id,
            receipt.target_id,
            receipt.artifact_id,
            receipt.source_git_ref,
        )
        != (
            profile.product,
            lane.context,
            lane.instance,
            target.ship_request.provider_id,
            target.resolved_target.target_id,
            target.ship_request.artifact_id,
            target.ship_request.source_git_ref,
        )
        or not set(receipt.managed_names) <= receipt.originals.keys()
        or not set(receipt.previous_owned_names) <= set(receipt.managed_names)
        or not {item.name for item in receipt.extensions.schedules} <= set(receipt.managed_names)
    ):
        raise ValueError("Original testing lifecycle baseline is not exact.")
    return record, receipt


def settle_testing_failure(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    lane: ProductLaneProfile,
    target: GenericWebResolvedDeployTarget,
    operation_key: str,
    phase: str,
    expected_receipt: str,
    provider: GenericWebDeployProvider,
    control_plane_root: Path,
    lease: ProviderOperationLease,
    check_execution: Callable[[], None],
    trace_id: str,
) -> ProviderMutationOutcome:
    """Repeat only the captured hold under the original lease, then record failure."""
    store = cast(GenericWebDeployStore, record_store)
    with serialize_generic_web_product_operation(
        record_store=store, product=profile.product, exclusive=True
    ):

        def checkpoint(_phase: str = "") -> None:
            check_execution()
            lease.assert_current()
            require_testing_application_target_authority(
                store,
                profile,
                lane,
                target_id=target.resolved_target.target_id,
                provider_id=target.ship_request.provider_id,
            )
            if not isinstance(lease, ProviderEvidenceLease):
                raise ProviderMutationUnknownError(
                    "Original recovery lease evidence is unavailable."
                )
            reservation = lease.evidence_reservation()
            if (
                build_provider_operation_key(
                    scope=reservation.scope,
                    route_path=reservation.route_path,
                    idempotency_key=reservation.idempotency_key,
                    request_fingerprint=reservation.request_fingerprint,
                    reconciliation_key=reservation.reconciliation_key,
                )
                != operation_key
            ):
                raise ProviderMutationUnknownError("Original testing failure operation changed.")
            _record, current = testing_failure_lifecycle_receipt(
                record_store=store,
                profile=profile,
                lane=lane,
                target=target,
                operation_key=operation_key,
                phase=phase,
            )
            if current is None or current.model_dump_json() != expected_receipt:
                raise ProviderMutationUnknownError("Original testing lifecycle receipt changed.")
            # Never replace this checkpoint with an image or maintenance phase.
            lease.checkpoint_effect(TESTING_FAILURE_SETTLEMENT_PHASE)

        checkpoint()
        observed = provider.observe_artifact_deploy(
            control_plane_root=control_plane_root,
            resolved_deploy_target=target,
            deployment_title=provider_operation_title(operation_key),
        )
        original_record = store.read_deployment_record(
            generic_web_provider_deployment_record_id(
                operation_key, context=lane.context, instance=lane.instance
            )
        )
        if original_record.deploy.status == "fail" and generic_web_provider_deployment_succeeded(
            observed.deployment_status
        ):
            observed = observed.model_copy(update={"deployment_status": "unhealthy"})
        qualified = qualify_testing_application_observation(
            provider=provider,
            control_plane_root=control_plane_root,
            target=target,
            observation=observed,
            deployment_record_id=generic_web_provider_deployment_record_id(
                operation_key, context=lane.context, instance=lane.instance
            ),
            original_deadline_expired=True,
        )
        if qualified.outcome != "present" or generic_web_provider_deployment_succeeded(
            qualified.deployment_status
        ):
            raise ProviderMutationUnknownError("Original testing failure is no longer proved.")
        receipt = ApplicationLifecycleSnapshot.model_validate_json(expected_receipt)
        lifecycle = application_lifecycle_for_lane(
            control_plane_root=control_plane_root,
            snapshot=receipt,
            provider=provider,
            target_type="application",
            target_id=target.resolved_target.target_id,
            checkpoint=checkpoint,
            effect_started=lambda: None,
        )
        assert lifecycle is not None
        lifecycle.hold()
        receipt.state = "held"
        checkpoint()
        record_id = generic_web_provider_deployment_record_id(
            operation_key, context=lane.context, instance=lane.instance
        )
        record = store.read_deployment_record(record_id)
        store.write_deployment_record(
            record.model_copy(
                update={
                    "post_deploy_update": record.post_deploy_update.model_copy(
                        update={
                            "attempted": True,
                            "status": "fail",
                            "detail": "Original failed testing deployment settled with schedules held.",
                            "evidence": {
                                **record.post_deploy_update.evidence,
                                **receipt.evidence(),
                            },
                        }
                    ),
                }
            )
        )
        records, result = record_observed_generic_web_deploy(
            record_store=store,
            profile=profile,
            lane=lane,
            resolved_deploy_target=target,
            observation=qualified,
            deployment_record_id=record_id,
            post_deploy_unobserved=True,
        )
        lease.assert_current()
        return ProviderMutationOutcome(
            response_status_code=502,
            response_payload=provider_operation_response_payload(
                trace_id=trace_id,
                records=records,
                result=result,
            ),
            provider_effect_performed=True,
        )
