from typing import Any

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.disposable_lane import DisposableTestingResourcePreparation
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.testing_address import retained_address_custody_sha256
from control_plane.storage import postgres as db
from control_plane.testing_generation_retirement import (
    RetainedTestingGeneration,
    RetainedTestingHostnameReservation,
)


def address_custody(
    session: Any,
    *,
    product: str,
    context: str,
    retirement_operation_id: str,
    reservations: tuple[RetainedTestingHostnameReservation, ...],
    for_update: bool = True,
) -> tuple[dict[str, str], str]:
    operations: dict[str, str] = {}
    retirement_digest = ""
    if retirement_operation_id:
        row = session.get(
            db.LaunchplaneRetainedTestingGenerationRow,
            retirement_operation_id,
            with_for_update=for_update,
        )
        if row is None:
            raise ValueError("The original retirement inspection is unavailable.")
        retained = RetainedTestingGeneration.model_validate(row.payload)
        if (retained.plan.product, retained.plan.context) != (product, context):
            raise ValueError("The retirement inspection belongs to another lane.")
        retirement_digest = retained_address_custody_sha256(retained)
    keys = {item.operation_id for item in reservations}
    for key in keys | ({retirement_operation_id} if retirement_operation_id else set()):
        operation = session.get(
            db.LaunchplaneOdooStableTargetReplacementOperationRow, key, with_for_update=for_update
        )
        if operation is not None:
            record = OdooStableTargetReplacementOperationRecord.model_validate(operation.payload)
            if not retirement_operation_id or key in keys and record.status != "superseded":
                raise ValueError("Replacement custody requires retirement and supersession.")
            operations[key] = retained_address_custody_sha256(record)
            continue
        preparation = session.get(
            db.LaunchplaneDisposableReplacementPreparationRow, key, with_for_update=for_update
        )
        allocation = session.get(
            db.LaunchplaneDisposableTestingResourceAllocationRow, key, with_for_update=for_update
        )
        if preparation is None or allocation is None or key not in keys:
            raise ValueError("An original retained operation is unavailable.")
        initial = DisposableTestingResourcePreparation.model_validate(
            {**preparation.payload, **allocation.payload}
        )
        recipients = {item.target_id for item in reservations if item.operation_id == key}
        if (
            retirement_operation_id
            or (initial.product, initial.context, initial.operation_id) != (product, context, key)
            or not initial.initial_request_sha256
            or not initial.initial_apply_started_at
            or not initial.initial_reconcile_requested_at
            or initial.initial_released_at
            or recipients != {initial.retained_target_id}
            or initial.retained_target_record is None
        ):
            raise ValueError("Initial hostname allocation custody is unverified.")
        operations[key] = canonical_json_sha256({**preparation.payload, **allocation.payload})
    return operations, retirement_digest
