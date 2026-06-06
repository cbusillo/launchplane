from __future__ import annotations

from pathlib import Path
from typing import Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewGenerationState,
)
from control_plane.contracts.preview_mutation_request import (
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.preview_record import PreviewState
from control_plane.contracts.promotion_record import ReleaseStatus
from control_plane.drivers.dispatch import (
    _DescriptorDriverDispatchResult,
    _DriverRouteExecutionMetadata,
    _ProductRouteEnvelope,
    _ResolvedProductDriverContext,
    _normalize_preview_verification_checked_urls,
    _normalize_release_status,
    _validate_driver_envelope_product,
)
from control_plane.launchplane_mutations import (
    LaunchplaneMutationStore,
    apply_launchplane_generation_evidence,
)
from control_plane.workflows.launchplane import find_preview_record


class GenericWebPreviewVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str
    anchor_repo: str
    anchor_pr_number: int = Field(ge=1)
    verification_status: ReleaseStatus
    verified_at: str
    checked_urls: tuple[str, ...] = ()
    timeout_seconds: int | None = Field(default=None, ge=1)
    failure_summary: str = ""

    @field_validator("verification_status", mode="before")
    @classmethod
    def _normalize_status(cls, value: object) -> ReleaseStatus:
        return _normalize_release_status(value, label="Generic web preview verification status")

    @field_validator("checked_urls", mode="before")
    @classmethod
    def _normalize_checked_urls(cls, value: object) -> tuple[str, ...]:
        return _normalize_preview_verification_checked_urls(
            value, label="Generic web preview verification"
        )

    @model_validator(mode="after")
    def _validate_request(self) -> "GenericWebPreviewVerificationRequest":
        if not self.context.strip():
            raise ValueError("Generic web preview verification requires context.")
        if not self.anchor_repo.strip():
            raise ValueError("Generic web preview verification requires anchor_repo.")
        if self.verification_status not in {"pass", "fail"}:
            raise ValueError("Generic web preview verification status must be pass or fail.")
        if not self.verified_at.strip():
            raise ValueError("Generic web preview verification requires verified_at.")
        if self.checked_urls and self.timeout_seconds is None:
            raise ValueError(
                "Generic web preview verification checked_urls require timeout_seconds."
            )
        return self


class GenericWebPreviewVerificationResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    preview_id: str
    preview_generation_id: str
    preview_state: str
    generation_state: str
    verification_status: ReleaseStatus
    verified_at: str
    checked_urls: tuple[str, ...] = ()
    timeout_seconds: int | None = None
    failure_summary: str = ""


class GenericWebPreviewVerificationEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    verification: GenericWebPreviewVerificationRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebPreviewVerificationEnvelope":
        _validate_driver_envelope_product(self.product, label="Generic web preview verification")
        return self


_GENERIC_WEB_PREVIEW_VERIFICATION_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/preview-verification",
    envelope_model=GenericWebPreviewVerificationEnvelope,
    denial_message=(
        "Workflow cannot write generic web preview verification for the requested product/context."
    ),
)


class _PreviewGenerationMutationStore(LaunchplaneMutationStore, Protocol):
    def read_preview_generation_record(self, generation_id: str) -> PreviewGenerationRecord: ...


def _preview_generation_mutation_store(
    record_store: object,
) -> _PreviewGenerationMutationStore:
    required_methods = (
        "list_preview_records",
        "list_preview_generation_records",
        "read_preview_generation_record",
        "write_preview_record",
        "write_preview_generation_record",
    )
    if all(hasattr(record_store, method_name) for method_name in required_methods):
        return cast(_PreviewGenerationMutationStore, record_store)
    raise TypeError("record store does not support Launchplane preview generation mutations")


def _handle_generic_web_preview_verification(
    request: GenericWebPreviewVerificationEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> _DescriptorDriverDispatchResult:
    del resolved_context
    return _DescriptorDriverDispatchResult(
        result=_apply_generic_web_preview_verification_records(
            control_plane_root_path=control_plane_root_path,
            record_store=record_store,
            request=request.verification,
        )
    )


def _apply_generic_web_preview_verification_records(
    *,
    control_plane_root_path: Path,
    record_store: object,
    request: GenericWebPreviewVerificationRequest,
    result_key: str = "generic_web_preview_verification",
    default_failure_summary: str = "Preview verification failed.",
) -> dict[str, object]:
    typed_record_store = _preview_generation_mutation_store(record_store)
    preview = find_preview_record(
        record_store=typed_record_store,
        context_name=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
    )
    if preview is None:
        raise click.ClickException(
            f"No Launchplane preview found for {request.context}/{request.anchor_repo}/pr-{request.anchor_pr_number}."
        )
    generation_id = preview.latest_generation_id or preview.active_generation_id
    if not generation_id:
        raise click.ClickException(
            f"No Launchplane preview generation found for {preview.preview_id}."
        )
    generation = typed_record_store.read_preview_generation_record(generation_id)
    verified_at = request.verified_at.strip()
    verification_passed = request.verification_status == "pass"
    failure_summary = request.failure_summary.strip() or default_failure_summary
    preview_state: PreviewState = "active" if verification_passed else "failed"
    generation_state: PreviewGenerationState = "ready" if verification_passed else "failed"
    result = apply_launchplane_generation_evidence(
        control_plane_root_path=control_plane_root_path,
        record_store=typed_record_store,
        preview_request=PreviewMutationRequest(
            context=preview.context,
            anchor_repo=preview.anchor_repo,
            anchor_pr_number=preview.anchor_pr_number,
            anchor_pr_url=preview.anchor_pr_url,
            canonical_url=preview.canonical_url,
            state=preview_state,
            created_at=preview.created_at,
            updated_at=verified_at,
            eligible_at=preview.eligible_at,
        ),
        generation_request=PreviewGenerationMutationRequest(
            context=preview.context,
            anchor_repo=preview.anchor_repo,
            anchor_pr_number=preview.anchor_pr_number,
            anchor_pr_url=preview.anchor_pr_url,
            anchor_head_sha=generation.anchor_summary.head_sha,
            sequence=generation.sequence,
            generation_id=generation.generation_id,
            state=generation_state,
            requested_reason=generation.requested_reason,
            requested_at=generation.requested_at,
            started_at=generation.started_at,
            ready_at=verified_at if verification_passed else "",
            finished_at=verified_at,
            failed_at="" if verification_passed else verified_at,
            resolved_manifest_fingerprint=generation.resolved_manifest_fingerprint,
            artifact_id=generation.artifact_id,
            baseline_release_tuple_id=generation.baseline_release_tuple_id,
            source_map=generation.source_map,
            companion_summaries=generation.companion_summaries,
            deploy_status=generation.deploy_status,
            verify_status="pass" if verification_passed else "fail",
            overall_health_status="pass" if verification_passed else "fail",
            failure_stage="" if verification_passed else "verify",
            failure_summary="" if verification_passed else failure_summary,
        ),
    )
    verification_result = GenericWebPreviewVerificationResult(
        preview_id=preview.preview_id,
        preview_generation_id=generation.generation_id,
        preview_state=preview_state,
        generation_state=generation_state,
        verification_status=request.verification_status,
        verified_at=verified_at,
        checked_urls=request.checked_urls,
        timeout_seconds=request.timeout_seconds if request.checked_urls else None,
        failure_summary="" if verification_passed else failure_summary,
    )
    result["preview_state"] = preview_state
    result["preview_generation_id"] = generation.generation_id
    result["verification_status"] = request.verification_status
    result["verified_at"] = verified_at
    result[result_key] = verification_result.model_dump(mode="json")
    return result
