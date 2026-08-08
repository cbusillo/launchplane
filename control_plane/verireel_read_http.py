from __future__ import annotations

from contextlib import nullcontext
from pathlib import Path
from typing import ContextManager, Protocol, cast

import click
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.preview_mutation_request import (
    PreviewDestroyMutationRequest,
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
)
from control_plane.contracts.promotion_record import (
    HealthcheckEvidence,
    PostDeployUpdateEvidence,
    ReleaseStatus,
)
from control_plane.drivers.dispatch import (
    _DriverRouteExecutionMetadata,
    _ProductRouteEnvelope,
    _repo_token,
    _validate_driver_envelope_product,
    _normalize_release_status,
)
from control_plane.drivers.generic_web_preview_dispatch import (
    GenericWebPreviewVerificationRequest,
    _apply_generic_web_preview_verification_records,
    _write_preview_inventory_scan_if_supported,
)
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.launchplane_mutations import (
    LaunchplanePreviewGenerationIdentity,
    LaunchplaneMutationStore,
    apply_launchplane_destroy_preview_if_present,
    apply_launchplane_generation_evidence,
    resolve_launchplane_preview_id,
    resolve_next_launchplane_preview_generation_identity,
)
from control_plane.runtime_key_safety import RuntimeKeySafetyPolicyReadStore
from control_plane.workflows.evidence_ingestion import (
    EvidenceIngestionStore,
    apply_deployment_evidence,
)
from control_plane.workflows.ship import utc_now_timestamp
from control_plane.workflows.verireel_environment import (
    VeriReelStableEnvironmentRequest,
    resolve_verireel_stable_environment,
)
from control_plane.workflows.verireel_preview_driver import (
    VeriReelPreviewDestroyRequest,
    VeriReelPreviewDestroyResult,
    VeriReelPreviewInventoryRequest,
    VeriReelPreviewRefreshConfigError,
    VeriReelPreviewRefreshRequest,
    VeriReelPreviewRefreshResult,
    VeriReelPreviewRefreshTransportError as VeriReelPreviewRefreshTransportError,
    execute_verireel_preview_destroy,
    execute_verireel_preview_refresh,
    execute_verireel_preview_inventory,
)
from control_plane.workflows.verireel_rollout import (
    VeriReelRolloutVerificationRequest,
    execute_verireel_rollout_verification,
)


VERIREEL_TESTING_VERIFICATION_ROUTE = "/v1/drivers/verireel/testing-verification"
VERIREEL_STABLE_ENVIRONMENT_ROUTE = "/v1/drivers/verireel/stable-environment"
VERIREEL_RUNTIME_VERIFICATION_ROUTE = "/v1/drivers/verireel/runtime-verification"
VERIREEL_PREVIEW_REFRESH_ROUTE = "/v1/drivers/verireel/preview-refresh"
VERIREEL_PREVIEW_INVENTORY_ROUTE = "/v1/drivers/verireel/preview-inventory"
VERIREEL_PREVIEW_DESTROY_ROUTE = "/v1/drivers/verireel/preview-destroy"
VERIREEL_PREVIEW_VERIFICATION_ROUTE = "/v1/drivers/verireel/preview-verification"


class _PreviewRefreshSerializationStore(Protocol):
    def serialize_preview_refresh(self, *, preview_id: str) -> ContextManager[None]: ...


def _preview_refresh_serialization(
    *, record_store: object, preview_id: str
) -> ContextManager[None]:
    serialize = getattr(record_store, "serialize_preview_refresh", None)
    if not callable(serialize):
        return nullcontext()
    return cast(_PreviewRefreshSerializationStore, record_store).serialize_preview_refresh(
        preview_id=preview_id
    )


class VeriReelRouteDependencyError(ValueError):
    pass


class VeriReelProductMismatchError(ValueError):
    pass


class VeriReelResolvedDriverContext(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    profile: LaunchplaneProductProfileRecord | None = None
    lane: ProductLaneProfile | None = None


class VeriReelTestingVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str = "verireel"
    instance: str = "testing"
    deployment_record_id: str
    migration_status: ReleaseStatus
    verification_status: ReleaseStatus
    owner_routes_status: ReleaseStatus

    @field_validator(
        "migration_status", "verification_status", "owner_routes_status", mode="before"
    )
    @classmethod
    def _normalize_status(cls, value: object) -> ReleaseStatus:
        return _normalize_release_status(value, label="Testing verification status")

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelTestingVerificationRequest":
        if not self.context.strip():
            raise ValueError("VeriReel testing verification requires context.")
        if self.instance != "testing":
            raise ValueError("VeriReel testing verification requires instance 'testing'.")
        if not self.deployment_record_id.strip():
            raise ValueError("VeriReel testing verification requires deployment_record_id.")
        return self


class VeriReelTestingVerificationEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    verification: VeriReelTestingVerificationRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelTestingVerificationEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel testing verification")
        return self


class VeriReelStableEnvironmentEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    environment: VeriReelStableEnvironmentRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelStableEnvironmentEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel stable environment")
        return self


class VeriReelRuntimeVerificationEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    verification: VeriReelRolloutVerificationRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelRuntimeVerificationEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel runtime verification")
        return self


class VeriReelPreviewVerificationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: int = Field(default=1, ge=1)
    context: str = "verireel-testing"
    anchor_repo: str = "verireel"
    anchor_pr_number: int = Field(ge=1)
    verification_status: str
    verified_at: str
    failure_summary: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "VeriReelPreviewVerificationRequest":
        if not self.context.strip():
            raise ValueError("VeriReel preview verification requires context.")
        if not self.anchor_repo.strip():
            raise ValueError("VeriReel preview verification requires anchor_repo.")
        if self.verification_status.strip() not in {"pass", "fail"}:
            raise ValueError("VeriReel preview verification status must be 'pass' or 'fail'.")
        if not self.verified_at.strip():
            raise ValueError("VeriReel preview verification requires verified_at.")
        return self


class VeriReelPreviewVerificationEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    verification: VeriReelPreviewVerificationRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelPreviewVerificationEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel preview verification")
        return self


class VeriReelPreviewInventoryEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    inventory: VeriReelPreviewInventoryRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelPreviewInventoryEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel preview inventory")
        return self


class VeriReelPreviewRefreshEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    refresh: VeriReelPreviewRefreshRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelPreviewRefreshEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel preview refresh")
        return self


class VeriReelPreviewDestroyEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    destroy: VeriReelPreviewDestroyRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "VeriReelPreviewDestroyEnvelope":
        _validate_driver_envelope_product(self.product, label="VeriReel preview destroy")
        return self


_VERIREEL_TESTING_VERIFICATION_ROUTE = _DriverRouteExecutionMetadata(
    route_path=VERIREEL_TESTING_VERIFICATION_ROUTE,
    envelope_model=VeriReelTestingVerificationEnvelope,
    denial_message=(
        "Workflow cannot write VeriReel testing verification for the requested product/context."
    ),
)
_VERIREEL_STABLE_ENVIRONMENT_ROUTE = _DriverRouteExecutionMetadata(
    route_path=VERIREEL_STABLE_ENVIRONMENT_ROUTE,
    envelope_model=VeriReelStableEnvironmentEnvelope,
    denial_message=(
        "Workflow cannot read the VeriReel stable environment for the requested product/context."
    ),
)
_VERIREEL_RUNTIME_VERIFICATION_ROUTE = _DriverRouteExecutionMetadata(
    route_path=VERIREEL_RUNTIME_VERIFICATION_ROUTE,
    envelope_model=VeriReelRuntimeVerificationEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel runtime verification driver"
        " for the requested product/context."
    ),
)
_VERIREEL_PREVIEW_INVENTORY_ROUTE = _DriverRouteExecutionMetadata(
    route_path=VERIREEL_PREVIEW_INVENTORY_ROUTE,
    envelope_model=VeriReelPreviewInventoryEnvelope,
    denial_message=(
        "Workflow cannot read the VeriReel preview inventory for the requested product/context."
    ),
)
_VERIREEL_PREVIEW_REFRESH_ROUTE = _DriverRouteExecutionMetadata(
    route_path=VERIREEL_PREVIEW_REFRESH_ROUTE,
    envelope_model=VeriReelPreviewRefreshEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel preview refresh driver"
        " for the requested product/context."
    ),
)
_VERIREEL_PREVIEW_DESTROY_ROUTE = _DriverRouteExecutionMetadata(
    route_path=VERIREEL_PREVIEW_DESTROY_ROUTE,
    envelope_model=VeriReelPreviewDestroyEnvelope,
    denial_message=(
        "Workflow cannot execute the VeriReel preview destroy driver"
        " for the requested product/context."
    ),
)
_VERIREEL_PREVIEW_VERIFICATION_ROUTE = _DriverRouteExecutionMetadata(
    route_path=VERIREEL_PREVIEW_VERIFICATION_ROUTE,
    envelope_model=VeriReelPreviewVerificationEnvelope,
    denial_message=(
        "Workflow cannot write VeriReel preview verification for the requested product/context."
    ),
)


def resolve_verireel_driver_context(
    *,
    record_store: object,
    product: str,
    context: str = "",
    instance: str = "",
    require_profile: bool = False,
) -> VeriReelResolvedDriverContext:
    normalized_product = product.strip()
    if normalized_product == "verireel" and not require_profile:
        return VeriReelResolvedDriverContext()
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        raise VeriReelRouteDependencyError(
            "Product driver validation requires product profile storage."
        )
    try:
        profile = read_profile(normalized_product)
    except FileNotFoundError as error:
        raise VeriReelRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    if profile.driver_id.strip() != "verireel":
        try:
            descriptor = read_driver_descriptor(profile.driver_id.strip())
        except FileNotFoundError as error:
            raise VeriReelProductMismatchError(
                "Product profile is not compatible with the requested driver route."
            ) from error
        if descriptor.driver_id != "verireel":
            raise VeriReelProductMismatchError(
                "Product profile is not compatible with the requested driver route."
            )
    if context.strip() or instance.strip():
        lane = _find_product_profile_lane(profile=profile, context=context, instance=instance)
        if lane is None:
            raise VeriReelProductMismatchError(
                "Product profile does not own the requested driver lane."
            )
        return VeriReelResolvedDriverContext(profile=profile, lane=lane)
    return VeriReelResolvedDriverContext(profile=profile)


def apply_verireel_preview_verification_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: VeriReelPreviewVerificationEnvelope,
) -> dict[str, object]:
    generic_request = GenericWebPreviewVerificationRequest(
        context=request.verification.context,
        anchor_repo=request.verification.anchor_repo,
        anchor_pr_number=request.verification.anchor_pr_number,
        verification_status=cast(ReleaseStatus, request.verification.verification_status),
        verified_at=request.verification.verified_at,
        failure_summary=request.verification.failure_summary,
    )
    return _apply_generic_web_preview_verification_records(
        control_plane_root_path=control_plane_root,
        record_store=record_store,
        request=generic_request,
        result_key="verireel_preview_verification",
        default_failure_summary="Preview E2E verification failed.",
    )


def apply_verireel_preview_inventory_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: VeriReelPreviewInventoryEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_verireel_preview_inventory(
        control_plane_root=control_plane_root,
        request=request.inventory,
    )
    preview_inventory_scan_id = _write_preview_inventory_scan_if_supported(
        record_store=record_store,
        context=driver_result.context,
        source="verireel-preview-inventory",
        preview_slugs=tuple(item.previewSlug for item in driver_result.previews),
    )
    records: dict[str, object] = {}
    if preview_inventory_scan_id:
        records["preview_inventory_scan_id"] = preview_inventory_scan_id
    return records, driver_result.model_dump(mode="json")


def apply_verireel_preview_refresh_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: VeriReelPreviewRefreshEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    preview_id = resolve_launchplane_preview_id(
        context=request.refresh.context,
        anchor_repo=request.refresh.anchor_repo,
        anchor_pr_number=request.refresh.anchor_pr_number,
    )
    with _preview_refresh_serialization(record_store=record_store, preview_id=preview_id):
        generation_identity = resolve_next_launchplane_preview_generation_identity(
            record_store=cast(LaunchplaneMutationStore, record_store),
            context=request.refresh.context,
            anchor_repo=request.refresh.anchor_repo,
            anchor_pr_number=request.refresh.anchor_pr_number,
        )
        try:
            driver_result = execute_verireel_preview_refresh(
                control_plane_root=control_plane_root,
                record_store=_runtime_key_safety_store_or_none(record_store),
                request=request.refresh,
                preview_id=generation_identity.preview_id,
                preview_generation_id=generation_identity.generation_id,
            )
        except VeriReelPreviewRefreshConfigError as error:
            now = utc_now_timestamp()
            error_message = (
                str(error).strip() or "VeriReel preview refresh configuration is incomplete."
            )
            driver_result = VeriReelPreviewRefreshResult(
                refresh_status="fail",
                refresh_started_at=now,
                refresh_finished_at=now,
                application_name="",
                application_id="",
                preview_url=_verireel_preview_url_for_failed_records(request=request.refresh),
                error_message=error_message,
            )
        records = apply_verireel_preview_refresh_records(
            control_plane_root=control_plane_root,
            record_store=record_store,
            request=request.refresh,
            driver_result=driver_result,
            generation_identity=generation_identity,
        )
    return records, driver_result.model_dump(mode="json")


def apply_verireel_preview_destroy_result(
    *,
    control_plane_root: Path,
    record_store: object,
    request: VeriReelPreviewDestroyEnvelope,
) -> tuple[dict[str, object], dict[str, object]]:
    driver_result = execute_verireel_preview_destroy(
        control_plane_root=control_plane_root,
        request=request.destroy,
    )
    records = apply_verireel_preview_destroy_records(
        record_store=record_store,
        request=request.destroy,
        driver_result=driver_result,
    )
    return records, driver_result.model_dump(mode="json")


def apply_verireel_testing_verification_result(
    *, record_store: object, request: VeriReelTestingVerificationEnvelope
) -> dict[str, object]:
    return dict[str, object](
        apply_verireel_testing_verification_records(
            record_store=record_store,
            request=request.verification,
        )
    )


def read_verireel_stable_environment_result(
    *, control_plane_root: Path, request: VeriReelStableEnvironmentEnvelope
) -> dict[str, object]:
    driver_result = resolve_verireel_stable_environment(
        control_plane_root=control_plane_root,
        request=request.environment,
    )
    return driver_result.model_dump(mode="json")


def run_verireel_runtime_verification_result(
    *, control_plane_root: Path, request: VeriReelRuntimeVerificationEnvelope
) -> dict[str, object]:
    driver_result = execute_verireel_rollout_verification(
        control_plane_root=control_plane_root,
        request=request.verification,
    )
    return driver_result.model_dump(mode="json")


def verireel_testing_verification_response_records(
    result: dict[str, object],
) -> dict[str, object]:
    return {
        key: str(value)
        for key, value in result.items()
        if key in {"deployment_record_id", "inventory_record_id"}
    }


def verireel_preview_verification_response_records(
    result: dict[str, object],
) -> dict[str, object]:
    records: dict[str, object] = {}
    for key, value in result.items():
        if key.endswith("_preview_verification") and isinstance(value, dict):
            records[key] = value
            continue
        if key in {
            "generation_id",
            "preview_generation_id",
            "preview_id",
            "preview_state",
            "transition",
            "verification_status",
            "verified_at",
        }:
            records[key] = str(value)
    return records


def should_store_verireel_result_idempotency(result: dict[str, object]) -> bool:
    return not _result_contains_status(result, "blocked") and not _result_contains_status(
        result, "fail"
    )


def apply_verireel_preview_refresh_records(
    *,
    control_plane_root: Path,
    record_store: object,
    request: VeriReelPreviewRefreshRequest,
    driver_result: VeriReelPreviewRefreshResult,
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
        preview_url = _verireel_preview_url_for_failed_records(request=request)
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
        resolved_manifest_fingerprint=_verireel_preview_manifest_fingerprint(request),
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


def apply_verireel_preview_destroy_records(
    *,
    record_store: object,
    request: VeriReelPreviewDestroyRequest,
    driver_result: VeriReelPreviewDestroyResult,
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


def apply_verireel_testing_verification_records(
    *,
    record_store: object,
    request: VeriReelTestingVerificationRequest,
) -> dict[str, str]:
    evidence_store = cast(EvidenceIngestionStore, record_store)
    try:
        deployment_record = evidence_store.read_deployment_record(request.deployment_record_id)
    except FileNotFoundError as exc:
        raise click.ClickException(
            f"No Launchplane deployment record found for {request.deployment_record_id}."
        ) from exc
    if deployment_record.context != request.context:
        raise click.ClickException(
            "Testing verification context does not match deployment record context."
        )
    if deployment_record.instance != request.instance:
        raise click.ClickException(
            "Testing verification instance does not match deployment record instance."
        )

    destination_health_status = _testing_destination_health_status(
        deployment_record=deployment_record,
        request=request,
    )
    updated_record = deployment_record.model_copy(
        update={
            "post_deploy_update": PostDeployUpdateEvidence(
                attempted=request.migration_status != "skipped",
                status=request.migration_status,
                detail=_testing_post_deploy_detail(request.migration_status),
            ),
            "destination_health": _updated_testing_destination_health(
                deployment_record=deployment_record,
                status=destination_health_status,
            ),
        }
    )
    result = apply_deployment_evidence(
        record_store=evidence_store,
        deployment_record=updated_record,
    )
    result["deployment_health_status"] = destination_health_status
    result["post_deploy_status"] = request.migration_status
    return result


def _runtime_key_safety_store_or_none(
    record_store: object,
) -> RuntimeKeySafetyPolicyReadStore | None:
    if hasattr(record_store, "list_runtime_key_safety_policy_records"):
        return cast(RuntimeKeySafetyPolicyReadStore, record_store)
    return None


def _verireel_preview_manifest_fingerprint(request: VeriReelPreviewRefreshRequest) -> str:
    normalized_sha = request.anchor_head_sha.strip().lower()
    short_sha = normalized_sha[:7]
    return (
        f"{_repo_token(request.anchor_repo)}-preview-manifest-"
        f"{request.preview_slug.strip()}-{short_sha}"
    )


def _verireel_preview_url_for_failed_records(*, request: VeriReelPreviewRefreshRequest) -> str:
    return f"https://{request.preview_slug}.preview-config-missing.launchplane.invalid"


def _find_product_profile_lane(
    *, profile: LaunchplaneProductProfileRecord, context: str, instance: str
) -> ProductLaneProfile | None:
    normalized_context = context.strip()
    normalized_instance = instance.strip()
    for lane in profile.lanes:
        lane_context = lane.context.strip()
        lane_instance = lane.instance.strip()
        if (not normalized_context or lane_context == normalized_context) and (
            not normalized_instance or lane_instance == normalized_instance
        ):
            return lane
    return None


def _testing_post_deploy_detail(status: ReleaseStatus) -> str:
    if status == "pass":
        return "Prisma migrations completed on testing."
    if status == "fail":
        return "Prisma migrations failed on testing."
    return ""


def _testing_destination_health_status(
    *,
    deployment_record: DeploymentRecord,
    request: VeriReelTestingVerificationRequest,
) -> ReleaseStatus:
    statuses = (
        deployment_record.destination_health.status,
        request.verification_status,
        request.owner_routes_status,
    )
    if any(status == "fail" for status in statuses):
        return "fail"
    if all(status == "pass" for status in statuses):
        return "pass"
    if any(status == "pending" for status in statuses):
        return "pending"
    return "skipped"


def _updated_testing_destination_health(
    *,
    deployment_record: DeploymentRecord,
    status: ReleaseStatus,
) -> HealthcheckEvidence:
    if status in {"pass", "fail"} and deployment_record.destination_health.urls:
        return deployment_record.destination_health.model_copy(update={"status": status})
    return HealthcheckEvidence(status=status)


def _result_contains_status(value: object, status: str) -> bool:
    if isinstance(value, str):
        return value == status
    if isinstance(value, dict):
        return any(_result_contains_status(nested_value, status) for nested_value in value.values())
    if isinstance(value, (list, tuple)):
        return any(_result_contains_status(nested_value, status) for nested_value in value)
    return False
