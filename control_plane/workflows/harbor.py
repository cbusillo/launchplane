from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.storage.filesystem import FilesystemRecordStore

RECENT_GENERATION_LIMIT = 3


def find_preview_record(
    *,
    record_store: FilesystemRecordStore,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> PreviewRecord | None:
    records = record_store.list_preview_records(
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
        limit=2,
    )
    if not records:
        return None
    return records[0]


def build_preview_status_payload(
    *,
    record_store: FilesystemRecordStore,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> dict[str, object] | None:
    preview = find_preview_record(
        record_store=record_store,
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    if preview is None:
        return None

    generations = record_store.list_preview_generation_records(preview_id=preview.preview_id)
    generations_by_id = {record.generation_id: record for record in generations}
    serving_generation = generations_by_id.get(preview.serving_generation_id)
    latest_generation = generations_by_id.get(preview.latest_generation_id)
    input_generation = serving_generation or latest_generation
    evidence_generation = serving_generation or latest_generation
    recent_generations = generations[:RECENT_GENERATION_LIMIT]
    serving_matches_latest = (
        serving_generation is not None
        and latest_generation is not None
        and serving_generation.generation_id == latest_generation.generation_id
    )

    return {
        "preview": {
            "preview_id": preview.preview_id,
            "context": preview.context,
            "anchor_repo": preview.anchor_repo,
            "anchor_pr_number": preview.anchor_pr_number,
            "anchor_pr_url": preview.anchor_pr_url,
            "preview_label": preview.preview_label,
            "canonical_url": preview.canonical_url,
            "state": preview.state,
            "created_at": preview.created_at,
            "updated_at": preview.updated_at,
            "eligible_at": preview.eligible_at,
            "paused_at": preview.paused_at,
            "destroy_after": preview.destroy_after,
            "destroyed_at": preview.destroyed_at,
            "destroy_reason": preview.destroy_reason,
        },
        "serving_generation": _generation_payload(serving_generation),
        "latest_generation": _generation_payload(latest_generation),
        "trust_summary": {
            "active_generation_id": preview.active_generation_id,
            "serving_generation_id": preview.serving_generation_id,
            "latest_generation_id": preview.latest_generation_id,
            "artifact_id": evidence_generation.artifact_id if evidence_generation is not None else "",
            "manifest_fingerprint": (
                input_generation.resolved_manifest_fingerprint if input_generation is not None else ""
            ),
            "expires_at": evidence_generation.expires_at if evidence_generation is not None else "",
            "destroy_after": preview.destroy_after,
        },
        "health_summary": {
            "overall_health_status": (
                evidence_generation.overall_health_status if evidence_generation is not None else "pending"
            ),
            "deploy_status": evidence_generation.deploy_status if evidence_generation is not None else "pending",
            "verify_status": evidence_generation.verify_status if evidence_generation is not None else "pending",
            "serving_matches_latest": serving_matches_latest,
            "status_summary": _status_summary(
                preview=preview,
                serving_generation=serving_generation,
                latest_generation=latest_generation,
            ),
        },
        "input_summary": {
            "anchor": (
                input_generation.anchor_summary.model_dump(mode="json")
                if input_generation is not None
                else {
                    "repo": preview.anchor_repo,
                    "pr_number": preview.anchor_pr_number,
                    "pr_url": preview.anchor_pr_url,
                }
            ),
            "companions": (
                [item.model_dump(mode="json") for item in input_generation.companion_summaries]
                if input_generation is not None
                else []
            ),
            "baseline_release_tuple_id": (
                input_generation.baseline_release_tuple_id if input_generation is not None else ""
            ),
            "resolved_manifest_fingerprint": (
                input_generation.resolved_manifest_fingerprint if input_generation is not None else ""
            ),
            "source_map": (
                [item.model_dump(mode="json") for item in input_generation.source_map]
                if input_generation is not None
                else []
            ),
        },
        "lifecycle_summary": {
            "state": preview.state,
            "destroy_after": preview.destroy_after,
            "destroyed_at": preview.destroyed_at,
            "destroy_reason": preview.destroy_reason,
            "next_action": _next_action(
                preview=preview,
                serving_generation=serving_generation,
                latest_generation=latest_generation,
            ),
        },
        "recent_generations": [_generation_brief(item) for item in recent_generations],
        "links": {
            "canonical_url": preview.canonical_url,
            "anchor_pr_url": preview.anchor_pr_url,
        },
    }


def build_preview_inventory_payload(
    *,
    record_store: FilesystemRecordStore,
    context_name: str = "",
) -> dict[str, object]:
    previews = record_store.list_preview_records(context_name=context_name)
    preview_rows = []
    for preview in previews:
        generations = record_store.list_preview_generation_records(preview_id=preview.preview_id)
        generations_by_id = {record.generation_id: record for record in generations}
        serving_generation = generations_by_id.get(preview.serving_generation_id)
        latest_generation = generations_by_id.get(preview.latest_generation_id)
        input_generation = serving_generation or latest_generation
        evidence_generation = serving_generation or latest_generation
        preview_rows.append(
            {
                "preview_id": preview.preview_id,
                "context": preview.context,
                "anchor_repo": preview.anchor_repo,
                "anchor_pr_number": preview.anchor_pr_number,
                "preview_label": preview.preview_label,
                "canonical_url": preview.canonical_url,
                "state": preview.state,
                "updated_at": preview.updated_at,
                "destroy_after": preview.destroy_after,
                "destroyed_at": preview.destroyed_at,
                "destroy_reason": preview.destroy_reason,
                "serving_generation_id": preview.serving_generation_id,
                "latest_generation_id": preview.latest_generation_id,
                "artifact_id": evidence_generation.artifact_id if evidence_generation is not None else "",
                "manifest_fingerprint": (
                    input_generation.resolved_manifest_fingerprint if input_generation is not None else ""
                ),
                "overall_health_status": (
                    evidence_generation.overall_health_status if evidence_generation is not None else "pending"
                ),
                "status_summary": _status_summary(
                    preview=preview,
                    serving_generation=serving_generation,
                    latest_generation=latest_generation,
                ),
                "next_action": _next_action(
                    preview=preview,
                    serving_generation=serving_generation,
                    latest_generation=latest_generation,
                ),
            }
        )
    return {
        "context": context_name,
        "count": len(preview_rows),
        "previews": preview_rows,
    }


def build_preview_history_payload(
    *,
    record_store: FilesystemRecordStore,
    context_name: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> dict[str, object] | None:
    preview = find_preview_record(
        record_store=record_store,
        context_name=context_name,
        anchor_repo=anchor_repo,
        anchor_pr_number=anchor_pr_number,
    )
    if preview is None:
        return None

    generations = record_store.list_preview_generation_records(preview_id=preview.preview_id)
    return {
        "preview": {
            "preview_id": preview.preview_id,
            "context": preview.context,
            "anchor_repo": preview.anchor_repo,
            "anchor_pr_number": preview.anchor_pr_number,
            "preview_label": preview.preview_label,
            "canonical_url": preview.canonical_url,
            "state": preview.state,
            "updated_at": preview.updated_at,
            "serving_generation_id": preview.serving_generation_id,
            "latest_generation_id": preview.latest_generation_id,
            "active_generation_id": preview.active_generation_id,
        },
        "generation_count": len(generations),
        "generations": [
            {
                **_generation_payload_required(record),
                "is_active": record.generation_id == preview.active_generation_id,
                "is_serving": record.generation_id == preview.serving_generation_id,
                "is_latest": record.generation_id == preview.latest_generation_id,
            }
            for record in generations
        ],
    }


def _generation_payload(record: PreviewGenerationRecord | None) -> dict[str, object] | None:
    if record is None:
        return None
    return {
        "generation_id": record.generation_id,
        "sequence": record.sequence,
        "state": record.state,
        "requested_reason": record.requested_reason,
        "requested_at": record.requested_at,
        "started_at": record.started_at,
        "ready_at": record.ready_at,
        "finished_at": record.finished_at,
        "failed_at": record.failed_at,
        "superseded_at": record.superseded_at,
        "expires_at": record.expires_at,
        "artifact_id": record.artifact_id,
        "resolved_manifest_fingerprint": record.resolved_manifest_fingerprint,
        "baseline_release_tuple_id": record.baseline_release_tuple_id,
        "deploy_status": record.deploy_status,
        "verify_status": record.verify_status,
        "overall_health_status": record.overall_health_status,
        "failure_stage": record.failure_stage,
        "failure_summary": record.failure_summary,
    }


def _generation_brief(record: PreviewGenerationRecord) -> dict[str, object]:
    return {
        "generation_id": record.generation_id,
        "sequence": record.sequence,
        "state": record.state,
        "artifact_id": record.artifact_id,
        "resolved_manifest_fingerprint": record.resolved_manifest_fingerprint,
        "requested_reason": record.requested_reason,
        "requested_at": record.requested_at,
        "ready_at": record.ready_at,
        "failed_at": record.failed_at,
        "failure_stage": record.failure_stage,
    }


def _generation_payload_required(record: PreviewGenerationRecord) -> dict[str, object]:
    return _generation_payload(record) or {}


def _status_summary(
    *,
    preview: PreviewRecord,
    serving_generation: PreviewGenerationRecord | None,
    latest_generation: PreviewGenerationRecord | None,
) -> str:
    if preview.state == "destroyed":
        return "Preview destroyed; evidence retained."
    if preview.state == "paused":
        return "Preview is paused; no new generations will start until resumed."
    if latest_generation is None:
        return "Waiting for the first generation."
    if serving_generation is None:
        return "No serving preview is available yet."
    if serving_generation.generation_id == latest_generation.generation_id:
        return "Serving the latest requested generation."
    if latest_generation.state == "failed":
        return "Serving the last healthy generation while the latest replacement failed."
    return "Serving a prior generation while Harbor prepares a replacement."


def _next_action(
    *,
    preview: PreviewRecord,
    serving_generation: PreviewGenerationRecord | None,
    latest_generation: PreviewGenerationRecord | None,
) -> str:
    if preview.state == "destroyed":
        return "No runtime action remains; Harbor is retaining historical evidence only."
    if preview.state == "teardown_pending":
        return "Harbor will destroy runtime resources after the current teardown window."
    if preview.state == "paused":
        return "Harbor will keep current evidence but will not start new generations until resumed."
    if latest_generation is None:
        return "Harbor is waiting to create the first generation for this preview."
    if latest_generation.state in {"resolving", "building", "deploying", "verifying"}:
        return f"Harbor is progressing generation {latest_generation.generation_id} toward readiness."
    if latest_generation.state == "failed" and serving_generation is not None:
        return "Harbor is retaining the prior serving generation because the latest replacement failed."
    if preview.destroy_after:
        return "Harbor will keep this preview until the current destroy-after deadline or a lifecycle event replaces it."
    return "Harbor is serving the current preview state."
