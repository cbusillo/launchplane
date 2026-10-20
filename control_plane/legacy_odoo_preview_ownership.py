"""Physical sibling ownership from the Odoo producer's retained operation evidence."""

from urllib.parse import urlsplit

from control_plane.contracts.idempotency_record import build_launchplane_mutation_reservation_id
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.odoo_preview_apply_http import (
    build_odoo_preview_runtime_identity,
)
from control_plane.preview_reservation_recovery import original_preview_intent
from control_plane.storage.postgres import ORIGINAL_MUTATION_INTENT_ROUTE, PostgresRecordStore
from control_plane.workflows.launchplane import generate_preview_id
from control_plane.workflows.odoo_preview_runtime import OdooPreviewDokployApplyResult
from control_plane.legacy_preview_sibling_ownership import PreviewSiblingOwnership


def current_odoo_sibling_ownership(
    store: PostgresRecordStore,
    profile: LaunchplaneProductProfileRecord,
    preview: PreviewRecord,
) -> PreviewSiblingOwnership | None:
    """Never substitute a runtime instance name for a physical provider receipt."""
    if not preview.serving_generation_id:
        return None
    generation = store.read_preview_generation_record(preview.serving_generation_id)
    runtime = generation.runtime_identity
    if runtime is None:
        return None
    expected_preview_id = generate_preview_id(
        context_name=profile.preview.context,
        anchor_repo=profile.repository.partition("/")[2],
        anchor_pr_number=preview.anchor_pr_number,
    )
    if (
        preview.preview_id != expected_preview_id
        or preview.context != profile.preview.context
        or preview.anchor_repo != profile.repository.partition("/")[2]
        or generation.preview_id != preview.preview_id
        or generation.generation_id != preview.serving_generation_id
    ):
        raise ValueError("Sibling preview identity is uncertain.")
    operation = store.read_idempotency_record_by_id(runtime.deployment_record_id)
    if operation is None:
        return None
    if (
        operation.record_id
        != build_launchplane_mutation_reservation_id(
            scope=operation.scope,
            route_path=operation.route_path,
            idempotency_key=operation.idempotency_key,
        )
        or operation.state != "completed"
        or operation.response_status_code != 202
        or operation.response_payload.get("status") != "accepted"
        or operation.response_payload.get("trace_id") != operation.response_trace_id
    ):
        raise ValueError("Sibling preview lacks a successful original operation.")
    intent = original_preview_intent(operation, profile=profile, store=store)
    original_receipt = store.read_idempotency_record(
        scope=operation.scope,
        route_path=ORIGINAL_MUTATION_INTENT_ROUTE,
        idempotency_key=operation.record_id,
    )
    if original_receipt is None:
        raise ValueError("Sibling original operation evidence is missing.")
    issued = intent.issued_plan
    if (
        issued.operation != "refresh"
        or issued.product != profile.product
        or issued.context != preview.context
        or issued.repository != profile.repository
        or issued.plan_request.pr_number != preview.anchor_pr_number
        or issued.preview_url != preview.canonical_url
        or runtime
        != build_odoo_preview_runtime_identity(
            profile=profile, issued_plan=issued, deployment_record_id=operation.record_id
        )
        or runtime.preview_id != preview.preview_id
        or runtime.preview_generation_id != generation.generation_id
        or generation.artifact_id != runtime.artifact_id
        or generation.state != "ready"
        or generation.requested_at != runtime.deployed_at
        or generation.anchor_summary.repo != preview.anchor_repo
        or generation.anchor_summary.pr_number != preview.anchor_pr_number
        or generation.anchor_summary.head_sha != runtime.source_git_ref
        or generation.deploy_status != "pass"
        or generation.verify_status != "pass"
    ):
        raise ValueError("Sibling runtime disagrees with original preview authority.")
    records = operation.response_payload.get("records")
    if not isinstance(records, dict) or (
        records.get("lifecycle_evidence_status") not in {"applied", "replayed"}
        or records.get("preview_id") != preview.preview_id
        or records.get("generation_id") != generation.generation_id
        or records.get("transition") != "ready"
    ):
        raise ValueError("Sibling preview has no applied lifecycle evidence.")
    result = OdooPreviewDokployApplyResult.model_validate(operation.response_payload.get("result"))
    if (
        result.status != "pass"
        or result.operation != "refresh"
        or result.product != profile.product
        or result.repository != profile.repository
        or result.preview_slug != issued.preview_slug
        or result.preview_url != preview.canonical_url
        or result.domain_host != urlsplit(preview.canonical_url).hostname
        or result.compose_name != runtime.instance
        or not result.compose_id.strip()
        or result.compose_id.startswith("${")
        or (
            not issued.dry_run_plan.compose_ref.startswith("${created.")
            and result.compose_id != issued.dry_run_plan.compose_ref
        )
    ):
        raise ValueError("Sibling provider result does not prove its physical compose.")
    return PreviewSiblingOwnership(
        target_id=result.compose_id,
        target_name=result.compose_name,
        evidence={
            "preview": preview.model_dump(mode="json"),
            "generation": generation.model_dump(mode="json"),
            "operation": operation.model_dump(mode="json"),
            "original_intent": store.read_original_mutation_intent(operation),
            "original_intent_receipt": original_receipt.model_dump(mode="json"),
        },
        receipt_ids=(operation.record_id, original_receipt.record_id),
    )
