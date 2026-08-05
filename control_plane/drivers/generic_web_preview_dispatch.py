from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal, Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.preview_desired_state_record import PreviewDesiredStateRecord
from control_plane.contracts.preview_generation_record import (
    PreviewGenerationRecord,
    PreviewGenerationState,
)
from control_plane.contracts.preview_inventory_scan_record import (
    PreviewInventoryScanRecord,
    build_preview_inventory_scan_id,
)
from control_plane.contracts.preview_mutation_request import (
    PreviewDestroyMutationRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.preview_record import PreviewState
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.promotion_record import ReleaseStatus
from control_plane.drivers.dispatch import (
    _DriverRouteExecutionMetadata,
    _ProductRouteEnvelope,
    _ResolvedProductDriverContext,
    ProductDriverMismatchError,
    _image_reference_tail,
    _normalize_preview_verification_checked_urls,
    _normalize_release_status,
    _repo_token,
    _validate_driver_envelope_product,
)
from control_plane.launchplane_mutations import (
    LaunchplaneDestroyPreviewStore,
    LaunchplaneMutationStore,
    apply_launchplane_destroy_preview_if_present,
    apply_launchplane_generation_evidence,
)
from control_plane.workflows.generic_web_deploy import product_profile_uses_generic_web_base
from control_plane.workflows.generic_web_preview import (
    GenericWebPreviewDesiredStateRequest,
    GenericWebPreviewDestroyRequest,
    GenericWebPreviewDestroyResult,
    GenericWebPreviewInventoryRequest,
    GenericWebPreviewReadinessRequest,
    GenericWebPreviewRefreshRequest,
    GenericWebPreviewRefreshResult,
    preview_pr_number_from_slug,
)
from control_plane.workflows.launchplane import find_preview_record
from control_plane.workflows.ship import utc_now_timestamp


class GenericWebPreviewDesiredStateEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    desired_state: GenericWebPreviewDesiredStateRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebPreviewDesiredStateEnvelope":
        if not self.product.strip():
            raise ValueError("generic web preview desired state requires product")
        if self.product.strip() != self.desired_state.product.strip():
            raise ValueError("generic web preview desired state requires matching product values")
        return self


_GENERIC_WEB_PREVIEW_DESIRED_STATE_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/preview-desired-state",
    envelope_model=GenericWebPreviewDesiredStateEnvelope,
    denial_message=(
        "Workflow cannot discover generic web preview desired state"
        " for the requested product/context."
    ),
)


class GenericWebPreviewRefreshEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    refresh: GenericWebPreviewRefreshRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebPreviewRefreshEnvelope":
        if not self.product.strip():
            raise ValueError("generic web preview refresh requires product")
        if self.product.strip() != self.refresh.product.strip():
            raise ValueError("generic web preview refresh requires matching product values")
        return self


class GenericWebPreviewInventoryEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    inventory: GenericWebPreviewInventoryRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebPreviewInventoryEnvelope":
        if not self.product.strip():
            raise ValueError("generic web preview inventory requires product")
        if self.product.strip() != self.inventory.product.strip():
            raise ValueError("generic web preview inventory requires matching product values")
        return self


_GENERIC_WEB_PREVIEW_REFRESH_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/preview-refresh",
    envelope_model=GenericWebPreviewRefreshEnvelope,
    denial_message=(
        "Workflow cannot refresh generic web preview state for the requested product/context."
    ),
)


_GENERIC_WEB_PREVIEW_INVENTORY_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/preview-inventory",
    envelope_model=GenericWebPreviewInventoryEnvelope,
    denial_message=(
        "Workflow cannot read generic web preview inventory for the requested product/context."
    ),
)


class GenericWebPreviewReadinessEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    readiness: GenericWebPreviewReadinessRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebPreviewReadinessEnvelope":
        if not self.product.strip():
            raise ValueError("generic web preview readiness requires product")
        if self.product.strip() != self.readiness.product.strip():
            raise ValueError("generic web preview readiness requires matching product values")
        return self


_GENERIC_WEB_PREVIEW_READINESS_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/preview-readiness",
    envelope_model=GenericWebPreviewReadinessEnvelope,
    denial_message=(
        "Workflow cannot evaluate generic web preview readiness for the requested product/context."
    ),
)


class GenericWebPreviewDestroyEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    destroy: GenericWebPreviewDestroyRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "GenericWebPreviewDestroyEnvelope":
        if not self.product.strip():
            raise ValueError("generic web preview destroy requires product")
        if self.product.strip() != self.destroy.product.strip():
            raise ValueError("generic web preview destroy requires matching product values")
        return self


_GENERIC_WEB_PREVIEW_DESTROY_ROUTE = _DriverRouteExecutionMetadata(
    route_path="/v1/drivers/generic-web/preview-destroy",
    envelope_model=GenericWebPreviewDestroyEnvelope,
    denial_message=(
        "Workflow cannot destroy generic web preview state for the requested product/context."
    ),
)


class GenericWebPreviewVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str = ""
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


def _validate_generic_web_preview_profile(
    request: _ProductRouteEnvelope,
    resolved_context: _ResolvedProductDriverContext,
    record_store: object,
    control_plane_root_path: Path,
) -> None:
    del request, record_store, control_plane_root_path
    profile = resolved_context.profile
    if profile is None:
        raise ProductDriverMismatchError("Generic web preview routes require a product profile.")
    if not product_profile_uses_generic_web_base(profile):
        raise click.ClickException(
            f"Product {profile.product!r} is configured for driver {profile.driver_id!r}, "
            "not generic-web or a generic-web based driver."
        )
    if not profile.preview.enabled:
        raise click.ClickException(
            f"Product {profile.product!r} does not have generic-web previews enabled."
        )
    if not profile.preview.context.strip():
        raise click.ClickException(
            f"Product {profile.product!r} does not define a preview context."
        )


def _write_preview_inventory_scan_if_supported(
    *,
    record_store: object,
    context: str,
    source: str,
    preview_slugs: tuple[str, ...],
) -> str:
    if not hasattr(record_store, "write_preview_inventory_scan_record"):
        return ""
    scanned_at = utc_now_timestamp()
    scan_id = build_preview_inventory_scan_id(
        context_name=context,
        scanned_at=scanned_at,
    )
    getattr(record_store, "write_preview_inventory_scan_record")(
        PreviewInventoryScanRecord(
            scan_id=scan_id,
            context=context,
            scanned_at=scanned_at,
            source=source,
            status="pass",
            preview_count=len(preview_slugs),
            preview_slugs=preview_slugs,
        )
    )
    return scan_id


def _write_preview_desired_state_if_supported(
    *, record_store: object, record: PreviewDesiredStateRecord
) -> str:
    if not hasattr(record_store, "write_preview_desired_state_record"):
        return ""
    getattr(record_store, "write_preview_desired_state_record")(record)
    return record.desired_state_id


def _generic_web_preview_manifest_fingerprint(
    request: GenericWebPreviewRefreshRequest,
    *,
    preview_slug: str = "",
) -> str:
    resolved_preview_slug = preview_slug.strip() or request.preview_slug
    artifact_token = _repo_token(_image_reference_tail(request.image_reference) or "image")
    return f"{_repo_token(resolved_preview_slug)}-{artifact_token}"


def _generic_web_preview_anchor_pr_number(
    *,
    request: GenericWebPreviewRefreshRequest,
    profile: LaunchplaneProductProfileRecord,
) -> int:
    if request.anchor_pr_number is not None:
        return request.anchor_pr_number
    pr_number = preview_pr_number_from_slug(
        preview_slug=request.preview_slug,
        slug_template=profile.preview.slug_template,
    )
    if pr_number is None:
        raise click.ClickException(
            "Generic web preview refresh requires anchor_pr_number when preview_slug does not match the profile slug_template."
        )
    return pr_number


def _generic_web_preview_destroy_anchor_pr_number(
    *,
    request: GenericWebPreviewDestroyRequest,
    profile: LaunchplaneProductProfileRecord,
) -> int:
    if request.anchor_pr_number is not None:
        return request.anchor_pr_number
    pr_number = preview_pr_number_from_slug(
        preview_slug=request.preview_slug,
        slug_template=profile.preview.slug_template,
    )
    if pr_number is None:
        raise click.ClickException(
            "Generic web preview destroy requires anchor_pr_number when preview_slug does not match the profile slug_template."
        )
    return pr_number


def _generic_web_preview_anchor_pr_url(
    *,
    request: GenericWebPreviewRefreshRequest,
    profile: LaunchplaneProductProfileRecord,
    anchor_pr_number: int,
) -> str:
    if request.anchor_pr_url.strip():
        return request.anchor_pr_url.strip()
    return f"https://github.com/{profile.repository.strip()}/pull/{anchor_pr_number}"


def _generic_web_preview_anchor_repo(profile: LaunchplaneProductProfileRecord) -> str:
    _owner, separator, repo = profile.repository.strip().partition("/")
    if not separator or not repo.strip():
        raise click.ClickException(
            "Generic web preview profile repository must use owner/repo format."
        )
    return repo.strip()


def _generic_web_preview_anchor_head_sha(request: GenericWebPreviewRefreshRequest) -> str:
    if request.anchor_head_sha.strip():
        return request.anchor_head_sha.strip()
    return _image_reference_tail(request.image_reference) or request.image_reference.strip()


def _generic_web_preview_refresh_timing(
    driver_result: GenericWebPreviewRefreshResult,
) -> tuple[str, str]:
    requested_at = (
        driver_result.refresh_started_at.strip() or driver_result.refresh_finished_at.strip()
    )
    finished_at = driver_result.refresh_finished_at.strip() or requested_at
    return requested_at, finished_at


def _generic_web_preview_refresh_failure_summary(
    driver_result: GenericWebPreviewRefreshResult,
) -> str:
    smoke = driver_result.smoke
    if smoke is not None and smoke.smoke_status == "fail" and smoke.failure_summary.strip():
        return smoke.failure_summary.strip()
    return driver_result.error_message.strip() or "Preview provisioning failed."


@dataclass(frozen=True)
class _GenericWebPreviewRefreshStates:
    preview_state: Literal["pending", "active", "failed", "paused", "teardown_pending", "destroyed"]
    generation_state: Literal[
        "resolving", "building", "deploying", "verifying", "ready", "failed", "superseded"
    ]
    deploy_status: Literal["pending", "pass", "fail", "skipped"]
    verify_status: Literal["pending", "pass", "fail", "skipped"]
    overall_health_status: Literal["pending", "pass", "fail", "skipped"]
    failure_stage: str


def _generic_web_preview_refresh_states(
    driver_result: GenericWebPreviewRefreshResult,
) -> _GenericWebPreviewRefreshStates:
    refresh_passed = driver_result.refresh_status == "pass"
    smoke = driver_result.smoke
    smoke_passed = smoke is not None and smoke.smoke_status == "pass"
    smoke_failed = smoke is not None and smoke.smoke_status == "fail"
    verify_status: Literal["pending", "pass", "fail", "skipped"] = "skipped"
    if smoke_passed:
        verify_status = "pass"
    elif smoke_failed:
        verify_status = "fail"
    elif refresh_passed:
        verify_status = "pending"
    overall_health_status: Literal["pending", "pass", "fail", "skipped"] = "fail"
    if smoke_passed:
        overall_health_status = "pass"
    elif smoke_failed:
        overall_health_status = "fail"
    elif refresh_passed:
        overall_health_status = "pending"
    return _GenericWebPreviewRefreshStates(
        preview_state="active" if smoke_passed else "pending" if refresh_passed else "failed",
        generation_state="ready" if smoke_passed else "verifying" if refresh_passed else "failed",
        deploy_status="pass" if refresh_passed or smoke_failed else "fail",
        verify_status=verify_status,
        overall_health_status=overall_health_status,
        failure_stage="" if refresh_passed else "verify" if smoke_failed else "provision",
    )


def _generic_web_preview_refresh_mutation_requests(
    *,
    request: GenericWebPreviewRefreshRequest,
    driver_result: GenericWebPreviewRefreshResult,
    profile: LaunchplaneProductProfileRecord,
) -> tuple[PreviewMutationRequest, PreviewGenerationMutationRequest]:
    anchor_pr_number = _generic_web_preview_anchor_pr_number(
        request=request,
        profile=profile,
    )
    requested_at, finished_at = _generic_web_preview_refresh_timing(driver_result)
    states = _generic_web_preview_refresh_states(driver_result)
    refresh_passed = driver_result.refresh_status == "pass"
    failure_summary = _generic_web_preview_refresh_failure_summary(driver_result)
    preview_request = PreviewMutationRequest(
        context=profile.preview.context,
        anchor_repo=_generic_web_preview_anchor_repo(profile),
        anchor_pr_number=anchor_pr_number,
        anchor_pr_url=_generic_web_preview_anchor_pr_url(
            request=request,
            profile=profile,
            anchor_pr_number=anchor_pr_number,
        ),
        canonical_url=driver_result.preview_url.strip() or request.preview_url.strip(),
        state=states.preview_state,
        created_at=requested_at,
        updated_at=finished_at,
        eligible_at=requested_at,
    )
    generation_request = PreviewGenerationMutationRequest(
        context=profile.preview.context,
        anchor_repo=preview_request.anchor_repo,
        anchor_pr_number=anchor_pr_number,
        anchor_pr_url=preview_request.anchor_pr_url,
        anchor_head_sha=_generic_web_preview_anchor_head_sha(request),
        state=states.generation_state,
        requested_reason="external_preview_refresh",
        requested_at=requested_at,
        started_at=requested_at,
        ready_at=finished_at if states.generation_state == "ready" else "",
        finished_at=finished_at if states.generation_state == "ready" or not refresh_passed else "",
        failed_at=finished_at if not refresh_passed else "",
        resolved_manifest_fingerprint=_generic_web_preview_manifest_fingerprint(
            request,
            preview_slug=driver_result.preview_slug,
        ),
        artifact_id=request.image_reference,
        deploy_status=states.deploy_status,
        verify_status=states.verify_status,
        overall_health_status=states.overall_health_status,
        failure_stage=states.failure_stage,
        failure_summary="" if refresh_passed else failure_summary,
        runtime_identity=driver_result.runtime_identity,
    )
    return preview_request, generation_request


def _apply_generic_web_preview_refresh_records(
    *,
    control_plane_root_path: Path,
    record_store: object,
    request: GenericWebPreviewRefreshRequest,
    driver_result: GenericWebPreviewRefreshResult,
    profile: LaunchplaneProductProfileRecord,
) -> dict[str, object]:
    if driver_result.refresh_status == "blocked":
        return {}
    preview_request, generation_request = _generic_web_preview_refresh_mutation_requests(
        request=request,
        driver_result=driver_result,
        profile=profile,
    )
    return apply_launchplane_generation_evidence(
        control_plane_root_path=control_plane_root_path,
        record_store=cast(LaunchplaneMutationStore, record_store),
        preview_request=preview_request,
        generation_request=generation_request,
    )


def _apply_generic_web_preview_destroy_records(
    *,
    record_store: object,
    request: GenericWebPreviewDestroyRequest,
    driver_result: GenericWebPreviewDestroyResult,
    context: str,
    anchor_repo: str,
    anchor_pr_number: int,
) -> dict[str, object]:
    locator: dict[str, object] = {
        "context": context,
        "anchor_repo": anchor_repo,
        "anchor_pr_number": anchor_pr_number,
    }
    if driver_result.destroy_status != "pass":
        return {**locator, "transition": "destroy_failed"}
    transition = apply_launchplane_destroy_preview_if_present(
        record_store=cast(LaunchplaneDestroyPreviewStore, record_store),
        request=PreviewDestroyMutationRequest(
            context=context,
            anchor_repo=anchor_repo,
            anchor_pr_number=anchor_pr_number,
            destroyed_at=(
                driver_result.destroy_finished_at.strip()
                or driver_result.destroy_started_at.strip()
                or utc_now_timestamp()
            ),
            destroy_reason=request.destroy_reason,
        ),
    )
    return {**locator, **transition}


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
            runtime_identity=generation.runtime_identity,
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
