"""Populate recorded facts in isolated test stores; no live evidence is inferred."""

from datetime import datetime, timedelta, timezone
from typing import Any

from control_plane.contracts.disposable_lane import DisposableLaneSafety
from control_plane.disposable_lanes import (
    disposable_lane_binding_sha256,
    disposable_lane_configuration,
)


def prepare_held_rebuild(store: Any, product: str, context: str) -> Any:
    from control_plane.contracts.odoo_stable_target_replacement import (
        OdooStableTargetReplacementApplyRequest,
    )
    from control_plane.contracts.odoo_stable_target_replacement_operation import (
        OdooStableTargetReplacementOperationRecord,
    )
    from control_plane.launchplane_reconcile_authorization import (
        build_launchplane_reconcile_authorization,
    )

    stamp = datetime.now(timezone.utc).isoformat()
    operation = OdooStableTargetReplacementOperationRecord(
        schema_version=2,
        operation_id=f"held-{product}",
        product=product,
        context=context,
        instance="testing",
        idempotency_key="fixture-held",
        request_fingerprint="fixture",
        created_at=stamp,
        updated_at=stamp,
        status="reconciliation_required",
        phase="apply",
        error_code="recovery_journal_absent",
        error_message="Unknown historical effects.",
        request=OdooStableTargetReplacementApplyRequest(
            product=product,
            instance="testing",
            strategy="recreate-in-place",
            data_source_mode="existing",
            allow_empty_data=True,
        ),
        authorization=build_launchplane_reconcile_authorization(
            product=product,
            context=context,
            authorized_at=stamp,
        ),
    )
    store.write_odoo_stable_target_replacement_operation_record(operation)
    if not store.prepare_disposable_testing_replacement(operation, "fixture-director"):
        raise AssertionError("Fixture failed to capture the retained target.")
    return store.read_odoo_stable_target_replacement_operation_record(operation.operation_id)


def record_safety(
    store: Any,
    product: str,
    context: str,
    instance: str,
    *,
    preview: Any = None,
    **changes: Any,
) -> None:
    profile = store.read_product_profile_record(product)
    targets = []
    for method in (store.read_dokploy_target_record, store.read_dokploy_target_id_record):
        try:
            targets.append(method(context_name=context, instance_name=instance))
        except FileNotFoundError:
            targets.append(None)
    now = datetime.now(timezone.utc)
    evidence = DisposableLaneSafety.model_validate(
        {
            "context": context,
            "instance": instance,
            "observed_by": "director-fixture",
            "observer_kind": "director_agent",
            "binding_sha256": disposable_lane_binding_sha256(
                profile,
                targets[0],
                targets[1],
                preview,
                disposable_lane_configuration(store, context, instance),
            ),
            "usage": "unused",
            "data": "reproducible",
            "production_resources": "isolated",
            "usage_evidence": "fixture:usage-inventory",
            "data_evidence": "fixture:reproducible-data",
            "isolation_evidence": "fixture:new-resources-and-retained-target-isolation",
            "observed_at": now,
            "expires_at": now + timedelta(minutes=10),
            **changes,
        }
    )
    store.write_product_profile_record(
        profile.model_copy(
            update={
                "disposable_lane_safety": tuple(
                    item
                    for item in profile.disposable_lane_safety
                    if (item.context, item.instance) != (context, instance)
                )
                + (evidence,),
            }
        )
    )
