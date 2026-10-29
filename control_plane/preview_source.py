from typing import Literal

from pydantic import BaseModel, ConfigDict

from control_plane.contracts.idempotency_record import LaunchplaneIdempotencyRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.workflows.odoo_preview_runtime import OdooPreviewApplyInputsResult


class PreviewSourceDecision(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    source: Literal["none", "upstream", "clean_snapshot", "retained"]
    fishbowl_import: bool


def require_source_free_preview_original(
    original: LaunchplaneIdempotencyRecord, intent: dict[str, object]
) -> None:
    issued = OdooPreviewApplyInputsResult.model_validate(intent.get("issued_plan"))
    if (
        issued.plan_request.fishbowl_source is not None
        or issued.dry_run_plan.clean_snapshot is not None
    ):
        raise ValueError("Preview original requested a data source; preserve its data.")
    result = original.response_payload.get("result")
    evidence = result.get("module_install_update_evidence") if isinstance(result, dict) else None
    fields = (
        "upstream_restore_requested",
        "clean_snapshot_restore_requested",
        "odoo_restore_completed",
    )
    if isinstance(evidence, dict) and any(
        key in evidence and evidence[key] != "false" for key in fields
    ):
        raise ValueError("Preview original restored data; preserve its data.")
    decision = intent.get("effective_source")
    if decision is not None:
        source = PreviewSourceDecision.model_validate(decision)
        if source.source == "none" and not source.fishbowl_import:
            return
    elif isinstance(evidence, dict) and all(evidence.get(key) == "false" for key in fields):
        return
    raise ValueError("Preview original source history is unknown; preserve its data.")


def require_source_free_preview_history(
    store: object,
    *,
    profile: LaunchplaneProductProfileRecord,
    issued_plan: OdooPreviewApplyInputsResult,
    operation_record_id: str,
    effective_source: PreviewSourceDecision | None = None,
) -> None:
    from control_plane.preview_reservation_recovery import original_preview_intent
    from control_plane.storage.postgres import PostgresRecordStore

    plan = issued_plan.dry_run_plan
    if (
        issued_plan.operation != "refresh"
        or profile.preview.clean_testing_snapshot
        or (
            profile.preview.data_transport_mode != "none"
            and (effective_source is None or effective_source.source != "none")
        )
        or plan.compose_ref.startswith("${created.")
    ):
        return
    if not isinstance(store, PostgresRecordStore):
        raise ValueError("Preview source history requires database storage.")
    matched = False
    for original in store.list_odoo_preview_originals(
        product=profile.product, number=issued_plan.plan_request.pr_number
    ):
        if original.record_id == operation_record_id:
            continue
        retained = original_preview_intent(original, profile=profile, store=store)
        previous = retained.issued_plan.dry_run_plan
        if retained.issued_plan.operation != "refresh":
            continue
        result = original.response_payload.get("result")
        receipt_target = result.get("compose_id") if isinstance(result, dict) else None
        if (
            previous.compose_ref != plan.compose_ref
            and receipt_target != plan.compose_ref
            and not (previous.compose_ref.startswith("${created.") and not receipt_target)
        ):
            continue
        if (
            receipt_target
            and not previous.compose_ref.startswith("${created.")
            and receipt_target != previous.compose_ref
        ):
            raise ValueError("Preview original target receipt conflicts; preserve its data.")
        matched = True
        require_source_free_preview_original(
            original, store.read_original_mutation_intent(original)
        )
    if not matched:
        raise ValueError("Existing preview has no original source history; preserve its data.")
