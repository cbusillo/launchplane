from typing import Any

import click

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.generic_web_deploy_provider_adapter import (
    GenericWebDeployProviderMutationAdapter,
)
from control_plane.generic_web_testing_exclusion import bind_testing_recipient
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.workflows.generic_web_application_snapshot import recorded_application_lifecycle
from control_plane.workflows.generic_web_deploy_provider import (
    generic_web_provider_deployment_succeeded,
    decode_generic_web_provider_reconciliation_target,
)
from control_plane.workflows.generic_web_execution import execution_receipt


def testing_abandonment_authority(
    store: PostgresRecordStore,
    adapter: GenericWebDeployProviderMutationAdapter,
    original: LaunchplaneIdempotencyRecord,
) -> dict[str, Any] | None:
    from control_plane.product_reconcile import RECONCILE_SOURCE, reconcile_reservation_scope

    profile, lane = adapter.profile, adapter.lane
    if (
        profile.driver_id != "generic-web"
        or lane.instance != "testing"
        or lane.generic_web_cutover is not None
        or decode_generic_web_provider_reconciliation_target(
            original.reconciliation_key
        ).original_event_deploy
        is None
        or original.state != "reconcile_required"
        or original.scope != reconcile_reservation_scope(profile.product)
        or not original.idempotency_key.startswith(
            f"{RECONCILE_SOURCE}:{profile.product}:{lane.context}:testing:"
        )
        or not original.provider_effect_phase.startswith("post_deploy_")
    ):
        return None
    try:
        bindings = bind_testing_recipient(store, profile.product, lane.context, "testing")
        current_profile, target, target_id, _provider = bindings
        if (
            current_profile != profile
            or target.target_type != "application"
            or target.policies.staff_testing_hold is not None
            or original.provider_target_key != adapter.target_key()
        ):
            return None
        if any(
            held.record_id != original.record_id
            and (
                held.provider_target_key == original.provider_target_key
                or held.scope == original.scope
                and held.idempotency_key.startswith(
                    f"{RECONCILE_SOURCE}:{profile.product}:{lane.context}:testing:"
                )
            )
            for held in store.list_held_provider_target_reservations()
        ):
            return None
        protection = store.read_disposable_lane_safety(profile.product, lane.context, "testing")
        if protection is not None and (
            protection.usage == "in_use"
            or protection.data == "only_copy"
            or protection.production_resources in {"shared", "separation_ready"}
        ):
            return None
        return {
            "bindings": [item.model_dump(mode="json") for item in bindings],
            "protection": protection.model_dump(mode="json") if protection is not None else None,
        }
    except (FileNotFoundError, ValueError, click.ClickException):
        return None


def inspect_testing_abandonment(
    store: PostgresRecordStore,
    adapter: GenericWebDeployProviderMutationAdapter,
    original: LaunchplaneIdempotencyRecord,
    operation_key: str,
) -> dict[str, Any] | None:
    authority = testing_abandonment_authority(store, adapter, original)
    if authority is None:
        return None
    target_id = authority["bindings"][2]["target_id"]
    try:
        inspected = adapter.inspect(
            provider_operation_key=operation_key,
            provider_effect_phase=original.provider_effect_phase,
            reconciliation_key=original.reconciliation_key,
            expected_provider_target_key=original.provider_target_key,
        )
        record = inspected.execution_record
        if not inspected.identity_matches or record is None:
            return None
        receipt = execution_receipt(record)
        if receipt is None or not receipt.entries:
            return None
        if inspected.execution is None or inspected.execution.digest_evidence.get(
            "receipt"
        ) != receipt.model_dump(mode="json"):
            return None
        last = receipt.entries[-1]
        if (
            last.kind != "product_executor"
            or last.target_id != target_id
            or last.acknowledgement != "none"
            or last.terminal
            or any(not entry.terminal for entry in receipt.entries[:-1])
            or any(entry.kind == "product_executor" for entry in receipt.entries[:-1])
            or any(
                entry.kind in {"schedule_write", "schedule_delete"}
                and entry.terminal != "write_completed"
                or entry.kind == "schedule_run"
                and entry.terminal not in {"done", "not_started"}
                for entry in receipt.entries[:-1]
            )
        ):
            return None
        lifecycle = recorded_application_lifecycle(record)
        if (
            lifecycle is None
            or lifecycle.mode != "deploy"
            or lifecycle.state not in {"prepared", "held"}
        ):
            return None
        image = adapter.inspect_evidence(
            provider_operation_key=operation_key,
            provider_effect_phase=original.provider_effect_phase,
            reconciliation_key=original.reconciliation_key,
            expected_provider_target_key=original.provider_target_key,
        )
        if (
            not image.identity_matches
            or image.observation.outcome != "present"
            or not generic_web_provider_deployment_succeeded(image.observation.deployment_status)
            or image.observation.deployment_id != record.deploy.deployment_id
        ):
            return None
        return {
            **authority,
            "deployment": record.model_dump(mode="json"),
            "image": image.observation.model_dump(mode="json"),
            "original_outcome": "unknown",
            "possible_fixture_residue": True,
        }
    except (FileNotFoundError, ValueError, click.ClickException):
        return None
