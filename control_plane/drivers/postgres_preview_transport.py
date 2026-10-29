from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import ContextManager, Protocol, cast

from pydantic import Field, model_validator

from control_plane.contracts.preview_mutation_request import (
    PreviewDestroyMutationRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
)
from control_plane.drivers.dispatch import (
    _ProductRouteEnvelope,
    _repo_token,
    _validate_driver_envelope_product,
)
from control_plane.launchplane_mutations import (
    LaunchplanePreviewGenerationIdentity,
    LaunchplaneMutationStore,
    apply_launchplane_destroy_preview_if_present,
    apply_launchplane_generation_evidence,
    resolve_launchplane_preview_id,
    resolve_next_launchplane_preview_generation_identity,
)
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.lane_movement import (
    LaneBuild,
    LaneMovementRefused,
    record_preview_refusal,
    require_forward_preview_build,
)
from control_plane.workflows.launchplane import PreviewMutationRecordStore, find_preview_record
from control_plane.workflows.postgres_preview import (
    PostgresPreviewDestroyRequest,
    PostgresPreviewDestroyResult,
    PostgresPreviewRefreshConfigError,
    PostgresPreviewRefreshRequest,
    PostgresPreviewRefreshResult,
    execute_postgres_preview_destroy,
    execute_postgres_preview_refresh,
)


class _ProductProfileStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...


class _PreviewRefreshSerializationStore(Protocol):
    def serialize_preview_refresh(
        self, *, preview_id: str, dedicated: bool = False
    ) -> ContextManager[None]: ...


def _preview_refresh_serialization(
    *, record_store: object, preview_id: str, dedicated: bool = False
) -> ContextManager[None]:
    serialize = getattr(record_store, "serialize_preview_refresh", None)
    if not callable(serialize):
        return nullcontext()
    store = cast(_PreviewRefreshSerializationStore, record_store)
    if dedicated:
        return store.serialize_preview_refresh(preview_id=preview_id, dedicated=True)
    return store.serialize_preview_refresh(preview_id=preview_id)


class PostgresPreviewRefreshEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    refresh: PostgresPreviewRefreshRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "PostgresPreviewRefreshEnvelope":
        _validate_driver_envelope_product(self.product, label="PostgreSQL preview refresh")
        return self


class PostgresPreviewDestroyEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    destroy: PostgresPreviewDestroyRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "PostgresPreviewDestroyEnvelope":
        _validate_driver_envelope_product(self.product, label="PostgreSQL preview destroy")
        return self


def apply_postgres_preview_refresh_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: PostgresPreviewRefreshEnvelope,
    execution_profile: LaunchplaneProductProfileRecord,
) -> tuple[dict[str, object], dict[str, object]]:
    preview_id = resolve_launchplane_preview_id(
        context=request.refresh.context,
        anchor_repo=request.refresh.anchor_repo,
        anchor_pr_number=request.refresh.anchor_pr_number,
    )
    with _preview_refresh_serialization(
        record_store=record_store, preview_id=preview_id, dedicated=True
    ):
        existing = find_preview_record(
            record_store=cast(PreviewMutationRecordStore, record_store),
            context_name=request.refresh.context,
            anchor_repo=request.refresh.anchor_repo,
            anchor_pr_number=request.refresh.anchor_pr_number,
        )
        if existing is not None and existing.state != "destroyed":
            profile = None
            desired = LaneBuild(
                request.refresh.image_reference,
                request.refresh.anchor_head_sha,
                request.refresh.image_reference,
            )
            try:
                read_profile = getattr(record_store, "read_product_profile_record", None)
                if not callable(read_profile):
                    raise LaneMovementRefused("source_authority_unavailable")
                try:
                    profile = LaunchplaneProductProfileRecord.model_validate(
                        cast(_ProductProfileStore, record_store).read_product_profile_record(
                            request.product
                        )
                    )
                except (FileNotFoundError, ValueError) as error:
                    raise LaneMovementRefused("source_authority_unavailable") from error
                if (
                    not profile.is_active
                    or profile.driver_id != "generic-web"
                    or profile.preview.transport_extension != "postgres"
                    or profile.preview.context != request.refresh.context
                    or profile.repository.partition("/")[2] != request.refresh.anchor_repo
                ):
                    raise LaneMovementRefused("source_authority_unavailable")
                require_forward_preview_build(
                    record_store=record_store,
                    profile=profile,
                    preview_slug=request.refresh.preview_slug,
                    desired=desired,
                    pull_request_number=request.refresh.anchor_pr_number,
                )
            except LaneMovementRefused as error:
                record_preview_refusal(
                    record_store=record_store,
                    profile=profile,
                    preview_context=request.refresh.context,
                    preview_slug=request.refresh.preview_slug,
                    desired=desired,
                    error=error,
                    target_type="application",
                )
                now = utc_now_timestamp()
                return {}, {
                    "refresh_status": "blocked",
                    "refresh_started_at": now,
                    "refresh_finished_at": now,
                    "application_name": "",
                    "application_id": "",
                    "preview_url": request.refresh.preview_url,
                    "error_message": f"lane_movement.{error.code}: {error.record_failure().description}",
                }
        generation_identity = resolve_next_launchplane_preview_generation_identity(
            record_store=cast(LaunchplaneMutationStore, record_store),
            context=request.refresh.context,
            anchor_repo=request.refresh.anchor_repo,
            anchor_pr_number=request.refresh.anchor_pr_number,
        )
        try:
            driver_result = execute_postgres_preview_refresh(
                record_store=record_store,
                control_plane_root=control_plane_root,
                request=request.refresh,
                preview_id=generation_identity.preview_id,
                preview_generation_id=generation_identity.generation_id,
                profile=execution_profile,
            )
        except PostgresPreviewRefreshConfigError as error:
            now = utc_now_timestamp()
            error_message = (
                str(error).strip() or "PostgreSQL preview refresh configuration is incomplete."
            )
            driver_result = PostgresPreviewRefreshResult(
                refresh_status="fail",
                refresh_started_at=now,
                refresh_finished_at=now,
                application_name="",
                application_id="",
                preview_url=_postgres_preview_url_for_failed_records(request=request.refresh),
                error_message=error_message,
            )
        records = apply_postgres_preview_refresh_records(
            control_plane_root=control_plane_root,
            record_store=record_store,
            request=request.refresh,
            driver_result=driver_result,
            generation_identity=generation_identity,
        )
    return records, driver_result.model_dump(mode="json")


def apply_postgres_preview_destroy_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: PostgresPreviewDestroyEnvelope,
    execution_profile: LaunchplaneProductProfileRecord,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_postgres_preview_destroy(
        record_store=record_store,
        control_plane_root=control_plane_root,
        request=request.destroy,
        profile=execution_profile,
    )
    records = apply_postgres_preview_destroy_records(
        record_store=record_store,
        request=request.destroy,
        driver_result=driver_result,
    )
    return records, driver_result.model_dump(mode="json")


def apply_postgres_preview_refresh_records(
    *,
    control_plane_root: Path,
    record_store: object,
    request: PostgresPreviewRefreshRequest,
    driver_result: PostgresPreviewRefreshResult,
    generation_identity: LaunchplanePreviewGenerationIdentity,
) -> dict[str, object]:
    requested_at = (
        driver_result.refresh_started_at.strip() or driver_result.refresh_finished_at.strip()
    )
    finished_at = driver_result.refresh_finished_at.strip() or requested_at
    refresh_passed = driver_result.refresh_status == "pass"
    failure_summary = driver_result.error_message.strip() or "Preview provisioning failed."
    preview_url = driver_result.preview_url.strip() or request.preview_url.strip()
    if not preview_url and not refresh_passed:
        preview_url = _postgres_preview_url_for_failed_records(request=request)
    preview_request = PreviewMutationRequest(
        context=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
        anchor_pr_url=request.anchor_pr_url,
        canonical_url=preview_url,
        state="pending" if refresh_passed else "failed",
        created_at=requested_at,
        updated_at=finished_at,
        eligible_at=requested_at,
    )
    generation_request = PreviewGenerationMutationRequest(
        context=request.context,
        anchor_repo=request.anchor_repo,
        anchor_pr_number=request.anchor_pr_number,
        anchor_pr_url=request.anchor_pr_url,
        anchor_head_sha=request.anchor_head_sha,
        sequence=generation_identity.sequence,
        generation_id=generation_identity.generation_id,
        state="verifying" if refresh_passed else "failed",
        requested_reason="external_preview_refresh",
        requested_at=requested_at,
        started_at=requested_at,
        finished_at="" if refresh_passed else finished_at,
        failed_at="" if refresh_passed else finished_at,
        resolved_manifest_fingerprint=_postgres_preview_manifest_fingerprint(request),
        artifact_id=request.image_reference,
        deploy_status="pass" if refresh_passed else "fail",
        verify_status="pending" if refresh_passed else "skipped",
        overall_health_status="pending" if refresh_passed else "fail",
        failure_stage="" if refresh_passed else "provision",
        failure_summary="" if refresh_passed else failure_summary,
        runtime_identity=driver_result.runtime_identity,
    )
    return apply_launchplane_generation_evidence(
        control_plane_root_path=control_plane_root,
        record_store=cast(LaunchplaneMutationStore, record_store),
        preview_request=preview_request,
        generation_request=generation_request,
    )


def apply_postgres_preview_destroy_records(
    *,
    record_store: object,
    request: PostgresPreviewDestroyRequest,
    driver_result: PostgresPreviewDestroyResult,
) -> dict[str, object]:
    if driver_result.destroy_status != "pass":
        return {"transition": "destroy_failed"}
    return apply_launchplane_destroy_preview_if_present(
        record_store=cast(LaunchplaneMutationStore, record_store),
        request=PreviewDestroyMutationRequest(
            context=request.context,
            anchor_repo=request.anchor_repo,
            anchor_pr_number=request.anchor_pr_number,
            destroyed_at=(
                driver_result.destroy_finished_at.strip()
                or driver_result.destroy_started_at.strip()
                or utc_now_timestamp()
            ),
            destroy_reason=request.destroy_reason,
        ),
    )


def _postgres_preview_manifest_fingerprint(request: PostgresPreviewRefreshRequest) -> str:
    normalized_sha = request.anchor_head_sha.strip().lower()
    short_sha = normalized_sha[:7]
    return (
        f"{_repo_token(request.anchor_repo)}-preview-manifest-"
        f"{request.preview_slug.strip()}-{short_sha}"
    )


def _postgres_preview_url_for_failed_records(*, request: PostgresPreviewRefreshRequest) -> str:
    return f"https://{request.preview_slug}.preview-config-missing.launchplane.invalid"
