from pathlib import Path
from typing import Protocol, cast

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
    build_preview_generation_record,
    build_preview_generation_record_from_request,
    build_preview_record_from_request,
    find_preview_record,
)


def _record_locator(value: object, fallback: str) -> str:
    if value is None:
        return fallback
    normalized_value = str(value).strip()
    return normalized_value or fallback


class LaunchplaneMutationStore(PreviewMutationRecordStore, Protocol):
    def write_preview_record(self, record: PreviewRecord) -> object: ...

    def write_preview_generation_record(self, record: PreviewGenerationRecord) -> object: ...


class LaunchplaneDestroyPreviewStore(PreviewMutationRecordStore, Protocol):
    def write_preview_record(self, record: PreviewRecord) -> object: ...


class _PreviewGenerationEvidenceWriteStore(Protocol):
    def write_preview_generation_evidence_records(
        self,
        *,
        preview_record: PreviewRecord,
        generation_record: PreviewGenerationRecord,
    ) -> tuple[object, object]: ...


def control_plane_root() -> Path:
    return Path(__file__).resolve().parent.parent


def upsert_launchplane_preview_from_request(
    *,
    control_plane_root_path: Path,
    record_store: LaunchplaneMutationStore,
    request: PreviewMutationRequest,
) -> PreviewRecord:
    preview_record = build_launchplane_preview_from_request(
        control_plane_root_path=control_plane_root_path,
        record_store=record_store,
        request=request,
    )
    record_store.write_preview_record(preview_record)
    return preview_record


def build_launchplane_preview_from_request(
    *,
    control_plane_root_path: Path,
    record_store: PreviewMutationRecordStore,
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
    return preview_record


def build_launchplane_preview_generation_from_request(
    *,
    record_store: PreviewMutationRecordStore,
    preview_record: PreviewRecord,
    request: PreviewGenerationMutationRequest,
) -> PreviewGenerationRecord:
    existing_generations = record_store.list_preview_generation_records(
        preview_id=preview_record.preview_id
    )
    existing_generation = next(
        (
            record
            for record in existing_generations
            if request.generation_id.strip() and record.generation_id == request.generation_id
        ),
        None,
    )
    if existing_generation is None and not request.generation_id.strip():
        existing_generation = next(
            (
                record
                for record in existing_generations
                if _preview_generation_matches_request(record=record, request=request)
            ),
            None,
        )
    if existing_generation is not None:
        resolved_sequence = existing_generation.sequence
        resolved_generation_id = existing_generation.generation_id
    else:
        resolved_sequence = request.sequence or (
            max((record.sequence for record in existing_generations), default=0) + 1
        )
        resolved_generation_id = request.generation_id.strip()

    return build_preview_generation_record(
        preview_id=preview_record.preview_id,
        sequence=resolved_sequence,
        state=request.state,
        requested_reason=request.requested_reason,
        requested_at=request.requested_at,
        resolved_manifest_fingerprint=request.resolved_manifest_fingerprint,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
        anchor_pr_url=request.anchor_pr_url,
        anchor_head_sha=request.anchor_head_sha,
        generation_id=resolved_generation_id,
        started_at=request.started_at,
        ready_at=request.ready_at,
        finished_at=request.finished_at,
        superseded_at=request.superseded_at,
        failed_at=request.failed_at,
        expires_at=request.expires_at,
        artifact_id=request.artifact_id,
        baseline_release_tuple_id=request.baseline_release_tuple_id,
        source_map=request.source_map,
        companion_summaries=request.companion_summaries,
        deploy_status=request.deploy_status,
        verify_status=request.verify_status,
        overall_health_status=request.overall_health_status,
        failure_stage=request.failure_stage,
        failure_summary=request.failure_summary,
    )


def _preview_generation_matches_request(
    *,
    record: PreviewGenerationRecord,
    request: PreviewGenerationMutationRequest,
) -> bool:
    if request.sequence is not None and record.sequence != request.sequence:
        return False
    return (
        record.state == request.state
        and record.requested_reason == request.requested_reason
        and record.requested_at == request.requested_at
        and record.started_at == request.started_at
        and record.ready_at == request.ready_at
        and record.finished_at == request.finished_at
        and record.superseded_at == request.superseded_at
        and record.failed_at == request.failed_at
        and record.expires_at == request.expires_at
        and record.resolved_manifest_fingerprint == request.resolved_manifest_fingerprint
        and record.artifact_id == request.artifact_id
        and record.baseline_release_tuple_id == request.baseline_release_tuple_id
        and record.source_map == request.source_map
        and record.companion_summaries == request.companion_summaries
        and record.deploy_status == request.deploy_status
        and record.verify_status == request.verify_status
        and record.overall_health_status == request.overall_health_status
        and record.failure_stage == request.failure_stage
        and record.failure_summary == request.failure_summary
        and record.anchor_summary.repo == request.anchor_repo
        and record.anchor_summary.pr_number == request.anchor_pr_number
        and record.anchor_summary.pr_url == request.anchor_pr_url
        and record.anchor_summary.head_sha == request.anchor_head_sha
    )


def _validate_preview_generation_request_alignment(
    *,
    preview_request: PreviewMutationRequest,
    generation_request: PreviewGenerationMutationRequest,
) -> None:
    if preview_request.context != generation_request.context:
        raise click.ClickException("Preview generation evidence requires matching contexts.")
    if preview_request.anchor_repo != generation_request.anchor_repo:
        raise click.ClickException("Preview generation evidence requires matching anchor_repo.")
    if preview_request.anchor_pr_number != generation_request.anchor_pr_number:
        raise click.ClickException(
            "Preview generation evidence requires matching anchor_pr_number."
        )
    if preview_request.anchor_pr_url != generation_request.anchor_pr_url:
        raise click.ClickException("Preview generation evidence requires matching anchor_pr_url.")


def apply_launchplane_generation_evidence(
    *,
    control_plane_root_path: Path,
    record_store: LaunchplaneMutationStore,
    preview_request: PreviewMutationRequest,
    generation_request: PreviewGenerationMutationRequest,
) -> dict[str, object]:
    _validate_preview_generation_request_alignment(
        preview_request=preview_request,
        generation_request=generation_request,
    )
    bundled_write = getattr(record_store, "write_preview_generation_evidence_records", None)
    if callable(bundled_write):
        preview_record = build_launchplane_preview_from_request(
            control_plane_root_path=control_plane_root_path,
            record_store=record_store,
            request=preview_request,
        )
        generation_record = build_launchplane_preview_generation_from_request(
            record_store=record_store,
            preview_record=preview_record,
            request=generation_request,
        )
    else:
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
    if callable(bundled_write):
        generation_path, preview_path = cast(
            _PreviewGenerationEvidenceWriteStore,
            record_store,
        ).write_preview_generation_evidence_records(
            preview_record=transitioned_preview,
            generation_record=generation_record,
        )
    else:
        generation_path = record_store.write_preview_generation_record(generation_record)
        preview_path = record_store.write_preview_record(transitioned_preview)
    return {
        "generation_id": generation_record.generation_id,
        "generation_path": _record_locator(generation_path, generation_record.generation_id),
        "preview_id": transitioned_preview.preview_id,
        "preview_path": _record_locator(preview_path, transitioned_preview.preview_id),
        "transition": generation_record.state,
    }


def apply_launchplane_destroy_preview(
    *,
    record_store: LaunchplaneDestroyPreviewStore,
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
        "preview_path": _record_locator(preview_path, transitioned_preview.preview_id),
        "transition": "destroyed",
    }
