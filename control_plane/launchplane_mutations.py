from pathlib import Path
from typing import Protocol

import click

from control_plane.contracts.preview_mutation_request import (
    PreviewDestroyMutationRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.workflows.launchplane import (
    PreviewMutationRecordStore,
    apply_generation_failed_transition,
    apply_generation_ready_transition,
    apply_generation_requested_transition,
    apply_preview_destroyed_transition,
    build_preview_generation_record_from_request,
    build_preview_record_from_request,
    find_preview_record,
)


class LaunchplaneMutationStore(PreviewMutationRecordStore, Protocol):
    def write_preview_record(self, record: PreviewRecord) -> object: ...

    def write_preview_generation_record(self, record: PreviewGenerationRecord) -> object: ...


def control_plane_root() -> Path:
    return Path(__file__).resolve().parent.parent


def upsert_launchplane_preview_from_request(
    *,
    control_plane_root_path: Path,
    record_store: LaunchplaneMutationStore,
    request: PreviewMutationRequest,
) -> PreviewRecord:
    existing_preview = find_preview_record(
        record_store=record_store,
        context_name=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
    )
    normalized_request = request
    if existing_preview is None and not request.created_at.strip():
        derived_created_at = request.updated_at.strip() or request.eligible_at.strip()
        if derived_created_at:
            normalized_request = request.model_copy(update={"created_at": derived_created_at})
    preview_record = (
        existing_preview.model_copy(
            update={
                "anchor_pr_url": normalized_request.anchor_pr_url,
                "canonical_url": normalized_request.canonical_url.strip()
                or existing_preview.canonical_url,
                "updated_at": normalized_request.updated_at.strip() or existing_preview.updated_at,
                "eligible_at": normalized_request.eligible_at.strip()
                or existing_preview.eligible_at,
                "paused_at": normalized_request.paused_at or existing_preview.paused_at,
                "destroy_after": normalized_request.destroy_after or existing_preview.destroy_after,
            }
        )
        if existing_preview is not None
        else build_preview_record_from_request(
            control_plane_root=control_plane_root_path,
            record_store=record_store,
            request=normalized_request,
        )
    )
    record_store.write_preview_record(preview_record)
    return preview_record


def apply_launchplane_generation_evidence(
    *,
    control_plane_root_path: Path,
    record_store: LaunchplaneMutationStore,
    preview_request: PreviewMutationRequest,
    generation_request: PreviewGenerationMutationRequest,
) -> dict[str, object]:
    preview_record = upsert_launchplane_preview_from_request(
        control_plane_root_path=control_plane_root_path,
        record_store=record_store,
        request=preview_request,
    )
    generation_record = build_preview_generation_record_from_request(
        record_store=record_store,
        request=generation_request,
    )
    if generation_record.state == "ready":
        transitioned_preview = apply_generation_ready_transition(
            preview=preview_record,
            generation=generation_record,
        )
    elif generation_record.state == "failed":
        transitioned_preview = apply_generation_failed_transition(
            preview=preview_record,
            generation=generation_record,
        )
    else:
        transitioned_preview = apply_generation_requested_transition(
            preview=preview_record,
            generation=generation_record,
        )
    generation_path = record_store.write_preview_generation_record(generation_record)
    preview_path = record_store.write_preview_record(transitioned_preview)
    return {
        "generation_id": generation_record.generation_id,
        "generation_path": str(generation_path),
        "preview_id": transitioned_preview.preview_id,
        "preview_path": str(preview_path),
        "transition": generation_record.state,
    }


def apply_launchplane_destroy_preview(
    *,
    record_store: LaunchplaneMutationStore,
    request: PreviewDestroyMutationRequest,
) -> dict[str, object]:
    preview_record = find_preview_record(
        record_store=record_store,
        context_name=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
    )
    if preview_record is None:
        raise click.ClickException(
            f"No Launchplane preview found for {request.context}/{request.anchor_repo}/pr-{request.anchor_pr_number}."
        )
    transitioned_preview = apply_preview_destroyed_transition(
        preview=preview_record,
        destroyed_at=request.destroyed_at,
        destroy_reason=request.destroy_reason,
    )
    preview_path = record_store.write_preview_record(transitioned_preview)
    return {
        "preview_id": transitioned_preview.preview_id,
        "preview_path": str(preview_path),
        "transition": "destroyed",
    }
