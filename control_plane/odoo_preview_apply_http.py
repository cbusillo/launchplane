from contextlib import AbstractContextManager, contextmanager, nullcontext
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import base64
import binascii
import hashlib
from pathlib import Path
from typing import Any, cast

import click
from pydantic import Field, model_validator

from control_plane import odoo_instance_overrides as control_plane_odoo_instance_overrides
from control_plane import live_target_runtime as control_plane_live_target_runtime
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane import runtime_platform_credentials
from control_plane.preview_source import PreviewSourceDecision

from control_plane.clean_testing_snapshot import preview_snapshot
from control_plane.odoo_testing_restore import preflight_fresh_testing_intent
from control_plane.integration_readback import INTEGRATION_FAMILIES
from control_plane.contracts.odoo_import_parameters import config_parameter_runtime_key
from control_plane.product_secret_copy import ProductSecretCopyFrom
from control_plane.contracts.dokploy_target_record import DokployTargetIntegrationAllowance
from control_plane.contracts.odoo_instance_override_record import (
    OdooInstanceOverrideRecord,
    OdooWebsiteBootstrapPayload,
)
from control_plane.preview_settings import validate_preview_scope
from control_plane.contracts.odoo_post_deploy_payload import OdooPostDeployPayload
from control_plane.odoo_addon_settings_override import (
    OdooAddonSettingsStore,
    _protected_store_keys,
    _validate_store_key,
)
from control_plane.odoo_preview_import_source import (
    PreviewImportSourceStore,
    PreviewImportSourceError,
    resolve_fishbowl_preview_source,
    PREVIEW_FISHBOWL_SOURCE_ENV_KEY,
)
from control_plane.runtime_key_safety import (
    RuntimeKeySafetyPolicyReadStore,
    evaluate_preview_copied_runtime_key_safety,
    latest_active_runtime_key_safety_policy,
    preview_copied_integration_credential_keys,
    preview_template_runtime_bindings,
)
from control_plane.contracts.artifact_dependency_provenance import (
    normalize_artifact_git_commit,
    normalize_artifact_sha256_digest,
)
from control_plane.contracts.artifact_identity import ArtifactIdentityManifest
from control_plane.contracts.idempotency_record import (
    format_launchplane_mutation_timestamp,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_mutation_request import (
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.odoo_stable_target_replacement import (
    apply_artifact_odoo_version,
    LAUNCHPLANE_REQUIRED_ODOO_MODULES,
    merge_odoo_install_modules,
)
from control_plane.contracts.preview_record import PreviewRecord
from control_plane.contracts.preview_execution import (
    ODOO_PREVIEW_DESTROY_REASON_PREFIX as _ODOO_PREVIEW_DESTROY_REASON_PREFIX,
    odoo_preview_destroy_issued_at,
    matches_odoo_preview_service_destroy,
)
from control_plane.legacy_preview_completion import (
    ReconciliationReceiptStore,
    reconciliation_completion_time,
)
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.runtime_identity import RuntimeIdentity, runtime_identity_env
from control_plane.drivers.dispatch import (
    _ProductRouteEnvelope,
    _validate_driver_envelope_product,
)
from control_plane.launchplane_mutations import (
    LaunchplaneMutationStore,
    apply_launchplane_generation_evidence,
    build_launchplane_preview_from_request,
)
from control_plane.odoo_product_driver_http import (
    OdooProductMismatchError,
    OdooRouteDependencyError,
    resolve_odoo_product_route,
)
from control_plane.workflows.odoo_preview_runtime import (
    ODOO_PREVIEW_REQUIRED_ENV_KEYS,
    OdooPreviewApplyInputsResult,
    OdooPreviewApplyPlanProvenance,
    OdooPreviewApplyInputsRequest,
    OdooPreviewDokployApplyRequest,
    OdooPreviewDokployApplyResult,
    OdooPreviewDokployDryRunPlan,
    build_odoo_preview_apply_inputs,
    execute_odoo_preview_dokploy_apply,
    observe_odoo_preview_dokploy_apply,
    odoo_preview_target_is_quiescent,
    odoo_preview_apply_plan_sha256,
)
from control_plane.workflows.launchplane import (
    apply_preview_destroyed_transition,
    find_preview_record,
    generate_preview_id,
)


ODOO_PREVIEW_APPLY_ROUTE = "/v1/drivers/odoo/preview-apply"
ODOO_PREVIEW_APPLY_INPUTS_ROUTE = "/v1/drivers/odoo/preview-apply-inputs"
ODOO_PREVIEW_PLAN_TTL_SECONDS = 30 * 60


class OdooPreviewApplyProductMismatchError(ValueError):
    pass


class OdooPreviewApplyRouteDependencyError(ValueError):
    pass


class OdooPreviewPlanProvenanceError(ValueError):
    def __init__(self, *, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class OdooPreviewApplyConfigError(click.ClickException):
    def __init__(self, *, context: str, instance: str, missing_keys: tuple[str, ...]) -> None:
        super().__init__("Odoo preview apply runtime environment is incomplete.")
        self.context = context
        self.instance = instance
        self.missing_keys = tuple(sorted(missing_keys))


class OdooPreviewApplyEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    apply: OdooPreviewDokployApplyRequest
    preserve_original_reservation_id: str = Field(
        default="",
        max_length=128,
        pattern=r"^(?:mutation-reservation-[a-f0-9]{64})?$",
        exclude_if=lambda value: not value,
    )
    unrecorded_original_reservation_id: str = Field(
        default="",
        max_length=128,
        pattern=r"^(?:mutation-reservation-[a-f0-9]{64})?$",
        exclude_if=lambda value: not value,
    )

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooPreviewApplyEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo preview apply")
        if self.product.strip() != self.apply.dry_run_plan.product.strip():
            raise ValueError("Odoo preview apply requires matching product values.")
        if self.preserve_original_reservation_id and self.unrecorded_original_reservation_id:
            raise ValueError("Preview preservation and unrecorded recovery cannot be combined.")
        if (
            self.unrecorded_original_reservation_id
            and self.apply.dry_run_plan.operation != "refresh"
        ):
            raise ValueError("Unrecorded preview recovery requires a refresh operation.")
        return self


class OdooPreviewApplyInputsEnvelope(_ProductRouteEnvelope):
    schema_version: int = Field(default=1, ge=1)
    inputs: OdooPreviewApplyInputsRequest

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooPreviewApplyInputsEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo preview apply inputs")
        if self.product.strip() != self.inputs.product.strip():
            raise ValueError("Odoo preview apply inputs require matching product values.")
        return self


def resolve_odoo_preview_apply_profile(
    *,
    record_store: object,
    product: str,
) -> LaunchplaneProductProfileRecord:
    try:
        profile = resolve_odoo_product_route(record_store=record_store, product=product)
    except OdooRouteDependencyError as error:
        raise OdooPreviewApplyRouteDependencyError from error
    except OdooProductMismatchError as error:
        raise OdooPreviewApplyProductMismatchError from error
    preview_profile = profile.preview
    if not preview_profile.enabled or not preview_profile.context.strip():
        raise OdooPreviewApplyProductMismatchError(
            "Odoo preview apply requires an enabled product preview profile."
        )
    return profile


def build_odoo_preview_apply_inputs_result(
    *,
    control_plane_root: Path,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    request: OdooPreviewApplyInputsRequest,
    database_url: str | None,
) -> dict[str, object]:
    return _build_odoo_preview_apply_inputs(
        control_plane_root=control_plane_root,
        record_store=record_store,
        profile=profile,
        request=request,
        database_url=database_url,
    ).model_dump(mode="json")


def _build_odoo_preview_apply_inputs(
    *,
    control_plane_root: Path,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    request: OdooPreviewApplyInputsRequest,
    database_url: str | None,
) -> OdooPreviewApplyInputsResult:
    result = build_odoo_preview_apply_inputs(
        control_plane_root=control_plane_root,
        record_store=cast(Any, record_store),
        profile=profile,
        request=request,
        database_url=database_url,
    )
    if result.status != "ready" or result.operation != "refresh":
        return result
    _, bootstrap_sha256 = control_plane_odoo_instance_overrides.resolve_preview_website_bootstrap(
        cast(Any, record_store),
        context=profile.preview.context,
        template_instance=profile.preview.template_instance,
        preview_slug=result.preview_slug,
    )
    result = result.model_copy(update={"preview_website_bootstrap_sha256": bootstrap_sha256})
    if profile.preview.clean_testing_snapshot:
        if request.fishbowl_source is not None:
            raise ValueError("Snapshot previews cannot connect to an import source.")
        retained = preview_has_completed_restore(
            record_store=record_store,
            profile=profile,
            plan=result.dry_run_plan,
            pr_number=request.pr_number,
        )
        if not retained:
            snapshot = preview_snapshot(cast(Any, record_store), profile)
            result = result.model_copy(
                update={
                    "dry_run_plan": result.dry_run_plan.model_copy(
                        update={"clean_snapshot": snapshot}
                    )
                }
            )
    if request.fishbowl_source is not None:
        source = resolve_fishbowl_preview_source(
            store=cast(PreviewImportSourceStore, record_store),
            profile=profile,
            preview_slug=result.preview_slug,
            copy_from=request.fishbowl_source,
            control_plane_root=control_plane_root,
            database_url=database_url,
        )
        return result.model_copy(update={"fishbowl_source_sha256": source.authority_sha256})
    try:
        environment = _odoo_preview_service_environment(
            control_plane_root_path=control_plane_root,
            record_store=record_store,
            profile=profile,
            plan=result.dry_run_plan,
            manifest=result.plan_request.manifest,
            database_url=database_url,
            fishbowl_source=request.fishbowl_source,
        )
    except (FileNotFoundError, ValueError, click.ClickException):
        if request.fishbowl_source is not None:
            raise
        # Apply resolves the same environment before any provider change and refuses
        # there with the details, so the plan only lists what it can resolve now.
        return result
    omitted_keys = environment.omitted_integration_credential_keys
    return result.model_copy(
        update={
            "omitted_integration_credential_keys": omitted_keys,
            "fishbowl_source_sha256": environment.fishbowl_source_sha256,
        }
    )


def build_odoo_preview_plan_id(*, scope: str, idempotency_key: str) -> str:
    normalized_scope = scope.strip()
    normalized_idempotency_key = idempotency_key.strip()
    if not normalized_scope or not normalized_idempotency_key:
        raise ValueError("Odoo preview plan ids require scope and idempotency key.")
    digest = hashlib.sha256(
        "\x1f".join(("odoo-preview-plan-v1", normalized_scope, normalized_idempotency_key)).encode(
            "utf-8"
        )
    ).hexdigest()
    return f"odoo-preview-plan-{digest}"


def issue_odoo_preview_apply_plan(
    *,
    result: dict[str, object] | OdooPreviewApplyInputsResult,
    plan_id: str,
    issued_at: datetime | None = None,
) -> OdooPreviewApplyInputsResult:
    planned_result = (
        result
        if isinstance(result, OdooPreviewApplyInputsResult)
        else OdooPreviewApplyInputsResult.model_validate(result)
    )
    if planned_result.status != "ready":
        return planned_result
    resolved_issued_at = issued_at or datetime.now(timezone.utc)
    if resolved_issued_at.tzinfo is None:
        raise ValueError("Odoo preview plan issuance requires a timezone-aware timestamp.")
    resolved_issued_at = resolved_issued_at.astimezone(timezone.utc)
    provenance = OdooPreviewApplyPlanProvenance(
        plan_id=plan_id,
        plan_sha256=odoo_preview_apply_plan_sha256(planned_result),
        issued_at=resolved_issued_at,
        expires_at=resolved_issued_at + timedelta(seconds=ODOO_PREVIEW_PLAN_TTL_SECONDS),
    )
    return planned_result.model_copy(update={"plan_provenance": provenance})


def validate_odoo_preview_issued_plan(
    *,
    plan_id: str,
    issued_plan: OdooPreviewApplyInputsResult,
    request: OdooPreviewApplyEnvelope,
) -> OdooPreviewApplyEnvelope:
    provenance = issued_plan.plan_provenance
    if issued_plan.status != "ready" or provenance is None:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_not_issued",
            message="Odoo preview apply requires a service-issued ready plan.",
        )
    if provenance.plan_id != plan_id:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_mismatch",
            message="Odoo preview apply plan identity does not match issued provenance.",
        )
    if odoo_preview_apply_plan_sha256(issued_plan) != provenance.plan_sha256:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_mismatch",
            message="Stored Odoo preview plan evidence failed its provenance fingerprint.",
        )
    if request.product != issued_plan.product:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_mismatch",
            message="Odoo preview apply product does not match the issued plan.",
        )
    if request.apply.dry_run_plan != issued_plan.dry_run_plan:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_mismatch",
            message="Odoo preview apply routing plan does not match the service-issued plan.",
        )
    if (
        request.apply.manifest != issued_plan.plan_request.manifest
        or request.apply.image_reference != issued_plan.plan_request.image_reference
    ):
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_mismatch",
            message="Odoo preview apply artifact does not match the service-issued plan.",
        )
    service_apply_request = request.apply.model_copy(
        update={
            "dry_run_plan": issued_plan.dry_run_plan,
            "manifest": issued_plan.plan_request.manifest,
            "image_reference": issued_plan.plan_request.image_reference,
        }
    )
    return request.model_copy(update={"apply": service_apply_request})


def validate_odoo_preview_profile_authority(
    *,
    profile: LaunchplaneProductProfileRecord,
    issued_plan: OdooPreviewApplyInputsResult,
    observation_only: bool = False,
) -> None:
    if profile.product != issued_plan.product:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_stale",
            message="Odoo preview plan product no longer matches current service authority.",
        )
    if profile.repository != issued_plan.repository:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_stale",
            message="Odoo preview plan repository no longer matches current service authority.",
        )
    if profile.preview.context != issued_plan.context:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_stale",
            message="Odoo preview plan context no longer matches current service authority.",
        )
    if not observation_only and profile.preview.template_instance != issued_plan.template_instance:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_stale",
            message="Odoo preview plan template no longer matches current service authority.",
        )


def refresh_odoo_preview_issued_plan(
    *,
    control_plane_root: Path,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    request: OdooPreviewApplyEnvelope,
    issued_plan: OdooPreviewApplyInputsResult,
    database_url: str | None,
    observed_at: datetime | None = None,
    retained_preservation_retry: bool = False,
) -> OdooPreviewApplyEnvelope:
    provenance = issued_plan.plan_provenance
    if provenance is None:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_not_issued",
            message="Odoo preview apply requires service-issued plan provenance.",
        )
    resolved_observed_at = observed_at or datetime.now(timezone.utc)
    if resolved_observed_at.tzinfo is None:
        raise ValueError("Odoo preview plan validation requires a timezone-aware timestamp.")
    resolved_observed_at = resolved_observed_at.astimezone(timezone.utc)
    if resolved_observed_at < provenance.issued_at:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_mismatch",
            message="Odoo preview plan issuance timestamp is in the future.",
        )
    if resolved_observed_at >= provenance.expires_at and not retained_preservation_retry:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_expired",
            message="Odoo preview plan has expired; request fresh apply inputs.",
        )
    try:
        current_plan = _build_odoo_preview_apply_inputs(
            control_plane_root=control_plane_root,
            record_store=record_store,
            profile=profile,
            request=issued_plan.plan_request,
            database_url=database_url,
        )
    except (FileNotFoundError, PreviewImportSourceError, click.ClickException) as error:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_stale",
            message="Odoo preview plan authority could not be revalidated.",
        ) from error
    if (
        current_plan.status != "ready"
        or odoo_preview_apply_plan_sha256(current_plan) != provenance.plan_sha256
    ):
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_stale",
            message="Odoo preview plan no longer matches current service authority.",
        )
    current_apply_request = request.apply.model_copy(
        update={
            "dry_run_plan": current_plan.dry_run_plan,
            "manifest": current_plan.plan_request.manifest,
            "image_reference": current_plan.plan_request.image_reference,
        }
    )
    return request.model_copy(update={"apply": current_apply_request})


def execute_odoo_preview_apply_result(
    *,
    control_plane_root_path: Path,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    request: OdooPreviewApplyEnvelope,
    issued_plan: OdooPreviewApplyInputsResult,
    database_url: str | None,
    provider_operation_title: str = "",
    provider_effect_checkpoint: Callable[[str], None] | None = None,
    provider_lease_check: Callable[[], None] | None = None,
    deployment_record_id: str,
    runtime_identity: RuntimeIdentity | None = None,
    propagate_domain_lookup_error: bool = False,
    qualify_existing_preview: bool = False,
    provider_mutation_guard: Callable[[], AbstractContextManager[None]] | None = None,
    recovery_reservation: object = None,
    expected_source_decision: PreviewSourceDecision | None = None,
) -> dict[str, object]:
    from control_plane.preview_preservation import (
        PREVIEW_PRESERVATION_PHASES,
        is_retained_preservation_retry,
    )

    retained_preservation_retry = is_retained_preservation_retry(
        store=record_store,
        profile=profile,
        issued_plan=issued_plan,
        request=request,
        operation_record_id=deployment_record_id,
        reservation=recovery_reservation,
    )

    current_request = refresh_odoo_preview_issued_plan(
        control_plane_root=control_plane_root_path,
        record_store=record_store,
        profile=profile,
        request=request,
        issued_plan=issued_plan,
        database_url=database_url,
        retained_preservation_retry=retained_preservation_retry,
    )
    if current_request.apply.dry_run_plan.repository.strip() != profile.repository.strip():
        raise ValueError("Odoo preview apply repository does not match product profile.")
    resolved_environment = _odoo_preview_service_environment(
        control_plane_root_path=control_plane_root_path,
        record_store=record_store,
        profile=profile,
        plan=current_request.apply.dry_run_plan,
        manifest=current_request.apply.manifest,
        database_url=database_url,
        fishbowl_source=issued_plan.plan_request.fishbowl_source,
    )
    if resolved_environment.fishbowl_source_sha256 != issued_plan.fishbowl_source_sha256:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_stale",
            message="Preview import source changed; request a fresh plan.",
        )
    resolved_environment_values = resolved_environment.values
    if current_request.apply.dry_run_plan.operation == "refresh":
        from control_plane.lane_movement import LaneBuild, require_forward_preview_build

        manifest = current_request.apply.manifest
        if manifest is None:
            raise ValueError("Preview refresh requires its artifact manifest.")
        require_forward_preview_build(
            record_store=record_store,
            profile=profile,
            preview_slug=current_request.apply.dry_run_plan.preview_slug,
            pull_request_number=issued_plan.plan_request.pr_number,
            desired=LaneBuild(
                manifest.artifact_id,
                manifest.source_commit,
                current_request.apply.image_reference,
                manifest.source_build,
            ),
        )
    resolved_runtime_identity = runtime_identity
    if current_request.apply.dry_run_plan.operation == "refresh":
        expected_runtime_identity = build_odoo_preview_runtime_identity(
            profile=profile,
            issued_plan=issued_plan,
            deployment_record_id=deployment_record_id,
        )
        if resolved_runtime_identity is None:
            resolved_runtime_identity = expected_runtime_identity
        elif resolved_runtime_identity != expected_runtime_identity:
            raise ValueError("Odoo preview runtime identity does not match issued authority.")
        resolved_environment_values.update(runtime_identity_env(resolved_runtime_identity))
    elif resolved_runtime_identity is not None:
        raise ValueError("Odoo preview destroy cannot include runtime identity.")
    service_dry_run_plan = current_request.apply.dry_run_plan.model_copy(
        update={
            "domain_certificate_type": profile.preview.domain_certificate_type,
        }
    )
    service_apply_request = current_request.apply.model_copy(
        update={
            "dry_run_plan": service_dry_run_plan,
            "environment_values": resolved_environment_values,
            "smoke_check": service_dry_run_plan.operation == "refresh",
        }
    )
    decision = resolve_preview_source_decision(
        record_store=record_store,
        profile=profile,
        issued_plan=issued_plan,
        environment=resolved_environment_values,
        preserve_original_reservation_id=current_request.preserve_original_reservation_id,
    )
    if expected_source_decision is not None and decision != expected_source_decision:
        raise ValueError("Preview effective source changed before execution; plan again.")
    upstream_source = decision.source == "upstream"
    snapshot_source = decision.source == "clean_snapshot"
    retained_copy = decision.source == "retained"
    if snapshot_source and not retained_copy and service_dry_run_plan.clean_snapshot is None:
        raise ValueError("A new snapshot preview requires its issued clean source.")
    effective_mutation_guard = provider_mutation_guard
    if current_request.preserve_original_reservation_id:
        from control_plane.preview_preservation import (
            require_preview_preservation_admission,
            run_preview_preservation,
        )
        from control_plane.storage.postgres import PostgresRecordStore

        if (
            not isinstance(record_store, PostgresRecordStore)
            or provider_effect_checkpoint is None
            or provider_lease_check is None
        ):
            raise ValueError("Original preview preservation requires durable provider custody.")

        def verify_preservation() -> None:
            run_preview_preservation(
                store=record_store,
                profile=profile,
                issued_plan=issued_plan,
                original_reservation_id=current_request.preserve_original_reservation_id,
                operation_record_id=deployment_record_id,
                control_plane_root=control_plane_root_path,
                mode="verify",
                timeout_seconds=current_request.apply.timeout_seconds,
                check=provider_lease_check,
                effect_checkpoint=provider_effect_checkpoint,
                mutation_guard=provider_mutation_guard,
            )

        @contextmanager
        def preserved_mutation_guard() -> Iterator[None]:
            verify_preservation()
            with provider_mutation_guard() if provider_mutation_guard else nullcontext():
                yield

        if (
            retained_preservation_retry
            and getattr(recovery_reservation, "provider_effect_phase", "")
            not in PREVIEW_PRESERVATION_PHASES
        ):
            require_preview_preservation_admission(
                store=record_store,
                profile=profile,
                issued_plan=issued_plan,
                original_reservation_id=current_request.preserve_original_reservation_id,
                operation_record_id=deployment_record_id,
            )
            verify_preservation()
        else:
            run_preview_preservation(
                store=record_store,
                profile=profile,
                issued_plan=issued_plan,
                original_reservation_id=current_request.preserve_original_reservation_id,
                operation_record_id=deployment_record_id,
                control_plane_root=control_plane_root_path,
                mode="preserve",
                timeout_seconds=current_request.apply.timeout_seconds,
                check=provider_lease_check,
                effect_checkpoint=provider_effect_checkpoint,
                mutation_guard=provider_mutation_guard,
            )
        effective_mutation_guard = preserved_mutation_guard
    driver_result = execute_odoo_preview_dokploy_apply(
        record_store=record_store,
        preview_context=profile.preview.context,
        control_plane_root=control_plane_root_path,
        request=service_apply_request,
        database_url=database_url,
        provider_operation_title=provider_operation_title,
        provider_effect_checkpoint=provider_effect_checkpoint,
        provider_lease_check=provider_lease_check,
        expected_runtime_identity=resolved_runtime_identity,
        propagate_domain_lookup_error=propagate_domain_lookup_error,
        integration_allowances=resolved_environment.integration_allowances,
        qualify_existing_preview=qualify_existing_preview,
        restore_upstream=upstream_source and not retained_copy,
        restore_clean_snapshot=snapshot_source and not retained_copy,
        preserve_existing_data=retained_copy,
        provider_mutation_guard=effective_mutation_guard,
    )
    return driver_result.model_dump(mode="json")


def resolve_preview_source_decision(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    issued_plan: OdooPreviewApplyInputsResult,
    environment: dict[str, str],
    preserve_original_reservation_id: str = "",
) -> PreviewSourceDecision:
    if issued_plan.operation == "refresh" and (
        preserve_original_reservation_id
        or preview_has_completed_preservation(
            record_store=record_store, profile=profile, issued_plan=issued_plan
        )
    ):
        return PreviewSourceDecision(source="retained", fishbowl_import=False)
    upstream = preview_uses_upstream_restore(profile, environment)
    snapshot = profile.preview.clean_testing_snapshot and issued_plan.operation == "refresh"
    retained = (upstream or snapshot) and preview_has_completed_restore(
        record_store=record_store,
        profile=profile,
        plan=issued_plan.dry_run_plan,
        pr_number=issued_plan.plan_request.pr_number,
    )
    return PreviewSourceDecision(
        source="retained"
        if retained
        else "clean_snapshot"
        if snapshot
        else "upstream"
        if upstream
        else "none",
        fishbowl_import=issued_plan.plan_request.fishbowl_source is not None,
    )


def preview_uses_upstream_restore(
    profile: LaunchplaneProductProfileRecord, environment: dict[str, str]
) -> bool:
    return (
        not profile.preview.clean_testing_snapshot
        and profile.preview.data_transport_mode == "driver"
        and any(
            key.startswith("ODOO_UPSTREAM_") and value.strip() for key, value in environment.items()
        )
    )


def preview_has_completed_preservation(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    issued_plan: OdooPreviewApplyInputsResult,
) -> bool:
    from control_plane.preview_reservation_recovery import original_preview_intent
    from control_plane.storage.postgres import PostgresRecordStore

    if not isinstance(record_store, PostgresRecordStore):
        return False
    preview = find_preview_record(
        record_store=record_store,
        context_name=profile.preview.context,
        anchor_repo=_odoo_preview_anchor_repo(profile.repository),
        anchor_pr_number=issued_plan.plan_request.pr_number,
    )
    if preview is None or not preview.serving_generation_id or preview.state == "destroyed":
        return False
    generation = record_store.read_preview_generation_record(preview.serving_generation_id)
    identity = generation.runtime_identity
    original = (
        record_store.read_idempotency_record_by_id(identity.deployment_record_id)
        if identity is not None
        else None
    )
    if original is None or original.state != "completed":
        return False
    retained = original_preview_intent(original, profile=profile, store=record_store)
    if not retained.apply_request.preserve_original_reservation_id:
        return False
    from control_plane.odoo_preview_qualification import read_preview_inspection_authority

    authority = read_preview_inspection_authority(record_store, preview)
    return (
        authority.target_id.target_id == issued_plan.dry_run_plan.compose_ref
        and record_store.read_original_mutation_intent(original).get("effective_source")
        == PreviewSourceDecision(source="retained", fishbowl_import=False).model_dump(mode="json")
    )


def preview_has_completed_restore(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    plan: OdooPreviewDokployDryRunPlan,
    pr_number: int,
) -> bool:
    store = cast(Any, record_store)
    preview = find_preview_record(
        record_store=store,
        context_name=profile.preview.context,
        anchor_repo=_odoo_preview_anchor_repo(profile.repository),
        anchor_pr_number=pr_number,
    )
    if preview is None or preview.state == "destroyed":
        return False
    read_operation = getattr(store, "read_idempotency_record_by_id", None)
    generations = store.list_preview_generation_records(preview_id=preview.preview_id)
    if callable(read_operation):
        for generation in generations:
            identity = generation.runtime_identity
            if (
                generation.state not in {"ready", "superseded"}
                or generation.preview_id != preview.preview_id
                or identity is None
                or identity.product != profile.product
                or identity.environment_kind != "preview"
                or identity.preview_id != preview.preview_id
                or identity.preview_generation_id != generation.generation_id
                or identity.context != profile.preview.context
                or identity.instance != plan.compose_name
            ):
                continue
            operation = read_operation(identity.deployment_record_id)
            if (
                operation is None
                or operation.state != "completed"
                or operation.route_path != ODOO_PREVIEW_APPLY_ROUTE
                or operation.response_status_code not in {200, 202}
            ):
                continue
            payload = operation.response_payload
            records = payload.get("records", {})
            if (
                not isinstance(records, dict)
                or records.get("preview_id") != preview.preview_id
                or records.get("generation_id") != generation.generation_id
            ):
                continue
            try:
                result = OdooPreviewDokployApplyResult.model_validate(payload.get("result"))
            except ValueError:
                continue
            if (
                result.status == "pass"
                and result.operation == "refresh"
                and result.product == profile.product
                and result.repository == profile.repository
                and result.preview_slug == plan.preview_slug
                and result.compose_id == plan.compose_ref
                and result.compose_name == plan.compose_name
                and result.module_install_update_status == "pass"
                and (
                    profile.preview.clean_testing_snapshot
                    or (
                        result.module_install_update_evidence.get("odoo_restore_completed")
                        == "true"
                        and any(
                            result.module_install_update_evidence.get(key) == "true"
                            for key in (
                                "upstream_restore_requested",
                                "clean_snapshot_restore_requested",
                            )
                        )
                    )
                )
            ):
                return True
    if profile.preview.clean_testing_snapshot and not any(
        item.state in {"ready", "superseded"} for item in generations
    ):
        return False
    raise click.ClickException(
        "Existing preview has no matching completed upstream restore; preserve its data and inspect its original operation."
    )


def observe_odoo_preview_apply_result(
    *,
    control_plane_root_path: Path,
    profile: LaunchplaneProductProfileRecord,
    request: OdooPreviewApplyEnvelope,
    database_url: str | None,
    provider_operation_title: str,
    provider_effect_phase: str = "",
) -> tuple[str, dict[str, object] | None, bool]:
    observation = observe_odoo_preview_dokploy_apply(
        control_plane_root=control_plane_root_path,
        context_name=profile.preview.context,
        request=request.apply,
        database_url=database_url,
        provider_operation_title=provider_operation_title,
        provider_effect_phase=provider_effect_phase,
    )
    if observation.outcome != "present" or observation.result is None:
        return observation.outcome, None, observation.retry_safe
    return observation.outcome, observation.result.model_dump(mode="json"), False


def apply_odoo_preview_lifecycle_evidence(
    *,
    control_plane_root_path: Path,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    issued_plan: OdooPreviewApplyInputsResult,
    driver_result: dict[str, object],
    runtime_identity: RuntimeIdentity | None = None,
) -> dict[str, object]:
    result = OdooPreviewDokployApplyResult.model_validate(driver_result)
    if result.status != "pass":
        return {}
    if result.operation != issued_plan.operation:
        raise ValueError("Odoo preview result operation does not match the issued plan.")
    if result.repository != profile.repository:
        raise ValueError("Odoo preview result repository does not match the product profile.")
    if result.compose_name != issued_plan.dry_run_plan.compose_name:
        raise ValueError("Odoo preview result compose does not match the issued plan.")
    if result.preview_url != issued_plan.preview_url:
        raise ValueError("Odoo preview result URL does not match the issued plan.")

    provenance = issued_plan.plan_provenance
    if provenance is None:
        raise ValueError("Ready Odoo preview apply requires issued plan provenance.")
    anchor_repo = _odoo_preview_anchor_repo(profile.repository)
    pr_number = issued_plan.plan_request.pr_number
    preview_id = generate_preview_id(
        context_name=profile.preview.context,
        anchor_repo=anchor_repo,
        anchor_pr_number=pr_number,
    )
    requested_at = format_launchplane_mutation_timestamp(provenance.issued_at)
    completed_at = requested_at
    typed_record_store = cast(LaunchplaneMutationStore, record_store)
    existing_preview = find_preview_record(
        record_store=typed_record_store,
        context_name=profile.preview.context,
        anchor_repo=anchor_repo,
        anchor_pr_number=pr_number,
    )
    if result.operation == "destroy":
        destroy_reason = _odoo_preview_destroy_reason(
            plan_id=provenance.plan_id,
            issued_at=requested_at,
        )
        if existing_preview is not None and matches_odoo_preview_service_destroy(
            existing_preview, destroy_reason
        ):
            return _odoo_preview_lifecycle_records(
                preview=existing_preview,
                status="replayed",
                transition="destroyed",
            )
        if existing_preview is not None and _odoo_preview_operation_is_stale(
            record_store=typed_record_store,
            preview=existing_preview,
            issued_at=requested_at,
            product=profile.product,
        ):
            return _odoo_preview_lifecycle_records(
                preview=existing_preview,
                status="stale",
                transition=existing_preview.state,
            )
        preview_request = PreviewMutationRequest(
            context=profile.preview.context,
            anchor_repo=anchor_repo,
            anchor_pr_number=pr_number,
            anchor_pr_url=f"https://github.com/{profile.repository}/pull/{pr_number}",
            canonical_url=issued_plan.preview_url,
            state="active",
            created_at=requested_at,
            updated_at=completed_at,
            eligible_at=requested_at,
        )
        preview_record = build_launchplane_preview_from_request(
            control_plane_root_path=control_plane_root_path,
            record_store=typed_record_store,
            request=preview_request,
        )
        transitioned_preview = apply_preview_destroyed_transition(
            preview=preview_record,
            destroyed_at=completed_at,
            destroy_reason=destroy_reason,
        ).model_copy(update={"reconciliation_receipt_id": ""})
        preview_path = typed_record_store.write_preview_record(transitioned_preview)
        return {
            **_odoo_preview_lifecycle_records(
                preview=transitioned_preview,
                status="applied",
                transition="destroyed",
            ),
            "preview_path": (
                transitioned_preview.preview_id
                if preview_path is None
                else str(preview_path).strip() or transitioned_preview.preview_id
            ),
        }

    if runtime_identity is None:
        raise ValueError("Successful Odoo preview refresh requires verified runtime identity.")
    expected_runtime_identity = build_odoo_preview_runtime_identity(
        profile=profile,
        issued_plan=issued_plan,
        deployment_record_id=runtime_identity.deployment_record_id,
    )
    if runtime_identity != expected_runtime_identity:
        raise ValueError("Verified Odoo preview runtime identity does not match issued authority.")
    generation_id = runtime_identity.preview_generation_id
    existing_generation = next(
        (
            generation
            for generation in typed_record_store.list_preview_generation_records(
                preview_id=preview_id
            )
            if generation.generation_id == generation_id
        ),
        None,
    )
    if (
        existing_generation is not None
        and existing_preview is not None
        and existing_preview.state == "active"
        and existing_preview.serving_generation_id == generation_id
    ):
        return _odoo_preview_lifecycle_records(
            preview=existing_preview,
            generation=existing_generation,
            status="replayed",
            transition=existing_generation.state,
        )
    if existing_preview is not None and _odoo_preview_operation_is_stale(
        record_store=typed_record_store,
        preview=existing_preview,
        issued_at=requested_at,
        product=profile.product,
    ):
        return _odoo_preview_lifecycle_records(
            preview=existing_preview,
            status="stale",
            transition=existing_preview.state,
        )
    if existing_generation is not None and existing_preview is not None:
        return _odoo_preview_lifecycle_records(
            preview=existing_preview,
            generation=existing_generation,
            status="replayed",
            transition=existing_generation.state,
        )

    anchor_pr_url = f"https://github.com/{profile.repository}/pull/{pr_number}"
    preview_request = PreviewMutationRequest(
        context=profile.preview.context,
        anchor_repo=anchor_repo,
        anchor_pr_number=pr_number,
        anchor_pr_url=anchor_pr_url,
        canonical_url=issued_plan.preview_url,
        state="active",
        created_at=requested_at,
        updated_at=completed_at,
        eligible_at=requested_at,
    )
    generation_request = PreviewGenerationMutationRequest(
        context=profile.preview.context,
        anchor_repo=anchor_repo,
        anchor_pr_number=pr_number,
        anchor_pr_url=anchor_pr_url,
        anchor_head_sha=runtime_identity.source_git_ref,
        generation_id=generation_id,
        state="ready",
        requested_reason="odoo_preview_refresh",
        requested_at=requested_at,
        started_at=requested_at,
        ready_at=completed_at,
        finished_at=completed_at,
        resolved_manifest_fingerprint=f"odoo-preview-plan-{provenance.plan_sha256}",
        artifact_id=runtime_identity.artifact_id,
        deploy_status="pass",
        verify_status="pass",
        overall_health_status="pass",
        runtime_identity=runtime_identity,
    )
    records = apply_launchplane_generation_evidence(
        control_plane_root_path=control_plane_root_path,
        record_store=typed_record_store,
        preview_request=preview_request,
        generation_request=generation_request,
    )
    return {**records, "lifecycle_evidence_status": "applied"}


def validate_odoo_preview_lifecycle_response_current(
    *,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    issued_plan: OdooPreviewApplyInputsResult,
    records: dict[str, object],
) -> None:
    lifecycle_status = str(records.get("lifecycle_evidence_status") or "").strip()
    if lifecycle_status not in {"applied", "replayed"}:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_lifecycle_evidence_missing",
            message="Odoo preview apply did not produce current lifecycle evidence.",
        )
    provenance = issued_plan.plan_provenance
    if provenance is None:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_not_issued",
            message="Odoo preview lifecycle validation requires issued plan provenance.",
        )
    anchor_repo = _odoo_preview_anchor_repo(profile.repository)
    pr_number = issued_plan.plan_request.pr_number
    preview_id = generate_preview_id(
        context_name=profile.preview.context,
        anchor_repo=anchor_repo,
        anchor_pr_number=pr_number,
    )
    if str(records.get("preview_id") or "").strip() != preview_id:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_lifecycle_evidence_missing",
            message="Odoo preview lifecycle evidence does not match the issued preview.",
        )
    typed_record_store = cast(LaunchplaneMutationStore, record_store)
    preview = find_preview_record(
        record_store=typed_record_store,
        context_name=profile.preview.context,
        anchor_repo=anchor_repo,
        anchor_pr_number=pr_number,
    )
    if preview is None:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_operation_superseded",
            message="Odoo preview lifecycle ownership is no longer current.",
        )
    if issued_plan.operation == "destroy":
        expected_destroy_reason = _odoo_preview_destroy_reason(
            plan_id=provenance.plan_id,
            issued_at=format_launchplane_mutation_timestamp(provenance.issued_at),
        )
        if matches_odoo_preview_service_destroy(preview, expected_destroy_reason):
            return
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_operation_superseded",
            message="Odoo preview destroy is no longer the current lifecycle owner.",
        )
    generation_id = _odoo_preview_generation_id(
        preview_id=preview_id,
        plan_id=provenance.plan_id,
    )
    if str(records.get("generation_id") or "").strip() != generation_id:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_lifecycle_evidence_missing",
            message="Odoo preview lifecycle evidence does not match the issued generation.",
        )
    generation = next(
        (
            candidate
            for candidate in typed_record_store.list_preview_generation_records(
                preview_id=preview_id
            )
            if candidate.generation_id == generation_id
        ),
        None,
    )
    if (
        preview.state == "active"
        and preview.serving_generation_id == generation_id
        and generation is not None
        and generation.state == "ready"
    ):
        return
    raise OdooPreviewPlanProvenanceError(
        code="odoo_preview_operation_superseded",
        message="Odoo preview refresh is no longer the current serving generation.",
    )


def build_odoo_preview_runtime_identity(
    *,
    profile: LaunchplaneProductProfileRecord,
    issued_plan: OdooPreviewApplyInputsResult,
    deployment_record_id: str,
) -> RuntimeIdentity:
    if issued_plan.operation != "refresh":
        raise ValueError("Odoo preview runtime identity requires a refresh plan.")
    provenance = issued_plan.plan_provenance
    if provenance is None:
        raise ValueError("Ready Odoo preview apply requires issued plan provenance.")
    normalized_deployment_record_id = deployment_record_id.strip()
    if not normalized_deployment_record_id:
        raise ValueError("Odoo preview runtime identity requires deployment_record_id.")
    anchor_repo = _odoo_preview_anchor_repo(profile.repository)
    pr_number = issued_plan.plan_request.pr_number
    preview_id = generate_preview_id(
        context_name=profile.preview.context,
        anchor_repo=anchor_repo,
        anchor_pr_number=pr_number,
    )
    generation_id = _odoo_preview_generation_id(
        preview_id=preview_id,
        plan_id=provenance.plan_id,
    )
    manifest = issued_plan.plan_request.manifest
    image_reference = (
        f"{manifest.image.repository}@{manifest.image.digest}"
        if manifest is not None
        else issued_plan.plan_request.image_reference
    )
    image_digest = image_reference.rpartition("@")[2]
    normalize_artifact_sha256_digest(
        image_digest,
        label="Odoo preview runtime image digest",
    )
    source_git_ref = normalize_artifact_git_commit(
        manifest.source_commit if manifest is not None else issued_plan.plan_request.source_git_ref,
        label="Odoo preview runtime source_git_ref",
    )
    artifact_id = manifest.artifact_id if manifest is not None else image_reference
    return RuntimeIdentity(
        product=profile.product,
        context=profile.preview.context,
        instance=issued_plan.dry_run_plan.compose_name,
        environment_kind="preview",
        deployment_record_id=normalized_deployment_record_id,
        artifact_id=artifact_id,
        source_git_ref=source_git_ref,
        image_reference=image_reference,
        preview_id=preview_id,
        preview_generation_id=generation_id,
        deployed_at=format_launchplane_mutation_timestamp(provenance.issued_at),
    )


def _odoo_preview_generation_id(*, preview_id: str, plan_id: str) -> str:
    return f"{preview_id}-odoo-{_odoo_preview_operation_token(plan_id)}"


def _odoo_preview_operation_token(plan_id: str) -> str:
    normalized_plan_id = plan_id.strip()
    if not normalized_plan_id:
        raise ValueError("Odoo preview lifecycle evidence requires plan_id.")
    return hashlib.sha256(normalized_plan_id.encode("utf-8")).hexdigest()[:20]


def _odoo_preview_destroy_reason(*, plan_id: str, issued_at: str) -> str:
    return (
        f"{_ODOO_PREVIEW_DESTROY_REASON_PREFIX}:"
        f"{_odoo_preview_operation_token(plan_id)}:{issued_at}"
    )


def _odoo_preview_lifecycle_records(
    *,
    preview: PreviewRecord,
    status: str,
    transition: str,
    generation: PreviewGenerationRecord | None = None,
) -> dict[str, object]:
    records: dict[str, object] = {
        "preview_id": preview.preview_id,
        "preview_path": preview.preview_id,
        "transition": transition,
        "lifecycle_evidence_status": status,
    }
    if generation is not None:
        records.update(
            {
                "generation_id": generation.generation_id,
                "generation_path": generation.generation_id,
            }
        )
    return records


def _odoo_preview_operation_is_stale(
    *,
    record_store: LaunchplaneMutationStore,
    preview: PreviewRecord,
    issued_at: str,
    product: str = "",
) -> bool:
    existing_issued_at = _odoo_preview_existing_operation_issued_at(
        record_store=record_store,
        preview=preview,
        product=product,
    )
    if not existing_issued_at:
        return True
    current_issued_at = parse_launchplane_mutation_timestamp(
        issued_at,
        field_name="Odoo preview lifecycle issued_at",
    )
    try:
        parsed_existing_issued_at = parse_launchplane_mutation_timestamp(
            existing_issued_at,
            field_name="existing preview lifecycle issued_at",
        )
    except ValueError:
        return True
    return current_issued_at <= parsed_existing_issued_at


def _odoo_preview_existing_operation_issued_at(
    *,
    record_store: LaunchplaneMutationStore,
    preview: PreviewRecord,
    product: str = "",
) -> str:
    if preview.state == "destroyed":
        if preview.reconciliation_receipt_id:
            return reconciliation_completion_time(
                cast(ReconciliationReceiptStore, record_store), preview, product=product
            )
        return odoo_preview_destroy_issued_at(preview.destroy_reason)
    generation_id = preview.serving_generation_id or preview.latest_generation_id
    if not generation_id:
        return ""
    generation = next(
        (
            record
            for record in record_store.list_preview_generation_records(
                preview_id=preview.preview_id
            )
            if record.generation_id == generation_id
        ),
        None,
    )
    return generation.requested_at if generation is not None else ""


def odoo_preview_supersession_is_quiescent(
    *,
    control_plane_root_path: Path,
    request: OdooPreviewApplyEnvelope,
    database_url: str | None,
    schedule_binding: object = None,
    require_snapshot_custody: bool = False,
) -> bool:
    from control_plane.dokploy.post_deploy import ComposeDataWorkflowBinding

    binding = (
        ComposeDataWorkflowBinding.model_validate(schedule_binding)
        if schedule_binding is not None
        else None
    )
    return odoo_preview_target_is_quiescent(
        control_plane_root=control_plane_root_path,
        request=request.apply,
        database_url=database_url,
        schedule_binding=binding,
        require_snapshot_custody=require_snapshot_custody,
    )


def _odoo_preview_anchor_repo(repository: str) -> str:
    _owner, separator, repo = repository.strip().partition("/")
    if not separator or not repo.strip():
        raise ValueError("Odoo preview repository must use owner/repo format.")
    return repo.strip()


def driver_result_contains_status(
    driver_result: dict[str, object] | object, expected_status: str
) -> bool:
    if isinstance(driver_result, dict):
        items = driver_result.items()
    elif hasattr(driver_result, "model_dump"):
        model_dump = getattr(driver_result, "model_dump")
        if callable(model_dump):
            dumped_result = model_dump(mode="json")
            if isinstance(dumped_result, dict):
                items = dumped_result.items()
            else:
                return False
        else:
            return False
    elif hasattr(driver_result, "__dict__"):
        items = vars(driver_result).items()
    else:
        return False
    return any(
        str(value).strip() == expected_status
        for key, value in items
        if key.endswith("_status") or key == "status"
    )


@dataclass(frozen=True)
class _OdooPreviewServiceEnvironment:
    values: dict[str, str]
    omitted_integration_credential_keys: tuple[str, ...] = ()
    integration_allowances: tuple[DokployTargetIntegrationAllowance, ...] = ()
    fishbowl_source_sha256: str = ""


@dataclass(frozen=True)
class _OdooPreviewBaseEnvironment:
    values: dict[str, str]
    omitted_keys: tuple[str, ...]
    encoded_preview: str
    website_bootstrap: OdooWebsiteBootstrapPayload | None


def _odoo_preview_base_environment(
    *,
    control_plane_root_path: Path,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    plan: OdooPreviewDokployDryRunPlan,
    manifest: ArtifactIdentityManifest | None,
    database_url: str | None,
) -> _OdooPreviewBaseEnvironment:
    preview_profile = profile.preview
    template_instance = preview_profile.template_instance.strip()
    # The preview runs unmerged code, so it gets only the template lane's site
    # environment: no global values, and platform credentials refused.
    template_values = dict(
        control_plane_runtime_environments.resolve_site_runtime_environment(
            control_plane_root=control_plane_root_path,
            context_name=preview_profile.context,
            instance_name=template_instance,
            database_url=database_url,
        ).values
    )
    preview_environment = (
        control_plane_runtime_environments.resolve_preview_runtime_environment_from_store(
            record_store=cast(
                control_plane_runtime_environments.SiteRuntimeEnvironmentStore, record_store
            ),
            context_name=preview_profile.context,
            template_values=template_values,
        )
    )
    environment_values = dict(preview_environment.values)
    payload_key = control_plane_odoo_instance_overrides.ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
    encoded_preview = (
        environment_values.get(payload_key, "")
        if payload_key in preview_environment.setting_keys
        else ""
    )
    bootstrap_record, bootstrap_sha256 = (
        control_plane_odoo_instance_overrides.resolve_preview_website_bootstrap(
            cast(Any, record_store),
            context=preview_profile.context,
            template_instance=template_instance,
            preview_slug=plan.preview_slug,
        )
    )
    if (
        bootstrap_sha256
        and bootstrap_record is not None
        and bootstrap_record.website_bootstrap is None
    ):
        required = (
            environment_values.get(
                control_plane_odoo_instance_overrides.LAUNCHPLANE_WEBSITE_BOOTSTRAP_REQUIRED_ENV_KEY,
                "",
            )
            .strip()
            .lower()
        )
        carried = environment_values.get(payload_key, "")
        if required not in {"", "0", "false", "no", "off"}:
            raise ValueError("Carried Website intent conflicts with exact preview withdrawal.")
        if carried:
            try:
                payload = OdooPostDeployPayload.model_validate_json(
                    base64.b64decode(carried, validate=True)
                )
            except ValueError:
                raise ValueError("Carried preview settings could not be validated.") from None
            if payload.website_bootstrap is not None or payload.website_settings is not None:
                raise ValueError("Carried Website intent conflicts with exact preview withdrawal.")
    preview_bootstrap_environment = (
        control_plane_odoo_instance_overrides.build_preview_website_bootstrap_environment(
            bootstrap_record,
            preview_url=plan.preview_url,
        )
    )
    if preview_bootstrap_environment is not None:
        environment_values.update(preview_bootstrap_environment.inline_environment)
    environment_values["ODOO_PROJECT_NAME"] = plan.compose_name
    environment_values["ODOO_STACK_NAME"] = plan.compose_name
    from control_plane.odoo_preview_resources import preview_resource_environment

    environment_values.update(preview_resource_environment(plan.compose_name))
    for key in preview_profile.preview_url_env_keys:
        environment_values[key] = plan.preview_url
    for key in preview_profile.preview_domain_env_keys:
        environment_values[key] = plan.domain_host
    connection_keys = {
        config_parameter_runtime_key(key)
        for family in INTEGRATION_FAMILIES
        for key in family.config_parameters
    }
    if preview_profile.clean_testing_snapshot:
        for key in list(environment_values):
            if (
                key.startswith(("ODOO_UPSTREAM_", "DATA_WORKFLOW_SSH_", "OPENUPGRADE_"))
                or key in connection_keys
            ):
                environment_values.pop(key)
    # A declared key the site environment no longer supplies (for example one that
    # only exists as a global value) fails closed instead of silently dropping out.
    declared_keys = control_plane_live_target_runtime.declared_runtime_keys(
        profile=profile,
        context_name=preview_profile.context,
        instance_name=template_instance,
    )
    if preview_profile.clean_testing_snapshot:
        declared_keys = {
            key
            for key in declared_keys
            if key not in connection_keys
            and not key.startswith(("ODOO_UPSTREAM_", "DATA_WORKFLOW_SSH_", "OPENUPGRADE_"))
        }
    # The preview runs the artifact being applied, so its recorded Odoo version wins.
    apply_artifact_odoo_version(
        environment_values, artifact_manifest=manifest, declared_keys=declared_keys
    )
    missing_env_keys = tuple(
        sorted(
            {
                key
                for key in ODOO_PREVIEW_REQUIRED_ENV_KEYS
                if not environment_values.get(key, "").strip()
            }
            | (
                declared_keys
                - environment_values.keys()
                - runtime_platform_credentials.PLATFORM_CREDENTIAL_KEYS
            )
        )
    )
    if missing_env_keys:
        raise OdooPreviewApplyConfigError(
            context=preview_profile.context,
            instance=template_instance,
            missing_keys=missing_env_keys,
        )
    omitted_keys = _copied_integration_credentials_to_omit(
        record_store=record_store,
        preview_context=preview_profile.context,
        preview_slug=plan.preview_slug,
        template_instance=template_instance,
        copied_values={
            key: value
            for key, value in template_values.items()
            if environment_values.get(key) == value
            and key not in preview_environment.setting_keys | preview_environment.secret_keys
        },
    )
    for key in omitted_keys:
        environment_values[key] = ""
    return _OdooPreviewBaseEnvironment(
        values=environment_values,
        omitted_keys=omitted_keys,
        encoded_preview=encoded_preview,
        website_bootstrap=preview_bootstrap_environment.payload.website_bootstrap
        if preview_bootstrap_environment is not None
        else None,
    )


def _odoo_preview_service_environment(
    *,
    control_plane_root_path: Path,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    plan: OdooPreviewDokployDryRunPlan,
    manifest: ArtifactIdentityManifest | None,
    database_url: str | None,
    fishbowl_source: ProductSecretCopyFrom | None = None,
) -> _OdooPreviewServiceEnvironment:
    if plan.operation == "destroy":
        return _OdooPreviewServiceEnvironment(values={})
    if profile.preview.clean_testing_snapshot and fishbowl_source is not None:
        raise ValueError("Snapshot previews cannot receive source credentials.")
    prepared = _odoo_preview_base_environment(
        control_plane_root_path=control_plane_root_path,
        record_store=record_store,
        profile=profile,
        plan=plan,
        manifest=manifest,
        database_url=database_url,
    )
    environment_values = prepared.values
    omitted_keys = prepared.omitted_keys
    preview_profile = profile.preview
    import_source = None
    if fishbowl_source is not None:
        import_source = resolve_fishbowl_preview_source(
            store=cast(PreviewImportSourceStore, record_store),
            profile=profile,
            preview_slug=plan.preview_slug,
            copy_from=fishbowl_source,
            control_plane_root=control_plane_root_path,
            database_url=database_url,
            include_secret_value=True,
        )
    if import_source is not None:
        bootstrap_record = OdooInstanceOverrideRecord(
            context=preview_profile.context,
            instance=plan.preview_slug,
            config_parameters=import_source.parameters,
            website_bootstrap=prepared.website_bootstrap,
            updated_at=profile.updated_at,
        )
        environment_values.update(
            control_plane_odoo_instance_overrides.build_post_deploy_environment(
                bootstrap_record,
                clear_undeclared_shopify=False,
                preview_import_source=import_source,
            ).inline_environment
        )
        environment_values.update(import_source.environment)
        environment_values[PREVIEW_FISHBOWL_SOURCE_ENV_KEY] = "true"
        omitted_keys = tuple(key for key in omitted_keys if key not in import_source.environment)
    _merge_preview_addon_settings(
        environment_values,
        profile=profile,
        record_store=record_store,
        encoded_preview=prepared.encoded_preview,
    )
    if preview_profile.clean_testing_snapshot:
        environment_values.update(
            {
                "BOOTSTRAP": "false",
                "OPENUPGRADE_ENABLED": "false",
                "NO_SANITIZE": "false",
                "ODOO_MAX_CRON_THREADS": "0",
                "ENV_OVERRIDE_DISABLE_CRON": "true",
            }
        )
        preflight_fresh_testing_intent(
            modules=merge_odoo_install_modules(
                LAUNCHPLANE_REQUIRED_ODOO_MODULES,
                manifest.odoo_install_modules if manifest else (),
                environment_values.get("ODOO_INSTALL_MODULES", ""),
            ),
            encoded_payload=environment_values.get("ODOO_INSTANCE_OVERRIDES_PAYLOAD_B64", ""),
        )
    return _OdooPreviewServiceEnvironment(
        values=environment_values,
        omitted_integration_credential_keys=omitted_keys,
        integration_allowances=(import_source.allowance,) if import_source else (),
        fishbowl_source_sha256=import_source.authority_sha256 if import_source else "",
    )


def _merge_preview_addon_settings(
    environment: dict[str, str],
    *,
    profile: LaunchplaneProductProfileRecord,
    record_store: object,
    encoded_preview: str,
) -> None:
    key = control_plane_odoo_instance_overrides.ODOO_INSTANCE_OVERRIDES_PAYLOAD_ENV_KEY
    if not encoded_preview.strip():
        return
    try:
        preview = validate_preview_scope({key: encoded_preview}, context=profile.preview.context)
        if preview is None:
            return
        if not preview.addon_settings:
            return
        if any(item.addon == "shopify" for item in preview.addon_settings):
            if control_plane_odoo_instance_overrides.odoo_instance_is_production(
                profile.preview.template_instance
            ):
                raise ValueError("Preview Shopify settings require a non-production template")
            protected = _protected_store_keys(
                record_store=cast(OdooAddonSettingsStore, record_store),
                product=profile.product,
                context=profile.preview.context,
                instance=profile.preview.template_instance,
            )
            for item in preview.addon_settings:
                if (
                    item.addon == "shopify"
                    and item.setting in control_plane_odoo_instance_overrides.SHOPIFY_STORE_SETTINGS
                ):
                    if item.value.source != "literal" or not isinstance(item.value.value, str):
                        raise ValueError("Shopify store selection requires a literal")
                    _validate_store_key(
                        shop_url_key=item.value.value.strip().lower(),
                        protected_store_keys=protected,
                    )
        if any(
            not environment.get(name, "").strip()
            for name in preview.required_container_environment_keys
        ):
            raise ValueError("Preview addon secret is not delivered")
        if environment.get(key) == encoded_preview:
            return
        generated = OdooPostDeployPayload.model_validate_json(
            base64.b64decode(environment[key], validate=True)
        )
    except (ValueError, binascii.Error):
        raise click.ClickException("Invalid preview addon settings payload.") from None
    merged = generated.model_copy(update={"addon_settings": preview.addon_settings})
    environment[key] = base64.b64encode(merged.to_wire_json_bytes()).decode("ascii")
    environment[
        control_plane_odoo_instance_overrides.LAUNCHPLANE_INSTANCE_OVERRIDES_REQUIRED_ENV_KEY
    ] = "true"


# The preview runs unmerged code against whatever its copied environment reaches, so a
# template-lane integration credential (including one inside a URL such as SMTP_URL) is
# left out unless a key-safety rule allows it on previews. The template lane's own
# classification and integration allowances never carry over. The preview stack's own
# database and admin credentials reach nothing outside it and are not checked here.
def _copied_integration_credentials_to_omit(
    *,
    record_store: object,
    preview_context: str,
    preview_slug: str,
    template_instance: str,
    copied_values: dict[str, str],
) -> tuple[str, ...]:
    policy_store = cast(RuntimeKeySafetyPolicyReadStore, record_store)
    try:
        template_bindings = preview_template_runtime_bindings(
            record_store=policy_store,
            template_context=preview_context,
            template_instance=template_instance,
        )
    except AttributeError:
        template_bindings = ()
    candidate_keys = preview_copied_integration_credential_keys(
        copied_values, template_bindings=template_bindings
    )
    try:
        policy_record = latest_active_runtime_key_safety_policy(policy_store)
    except (AttributeError, ValueError):
        return candidate_keys
    copied_keys = preview_copied_integration_credential_keys(
        copied_values,
        template_bindings=template_bindings,
        extra_integration_key_markers=policy_record.integration_key_markers,
    )
    if not copied_keys:
        return ()
    evaluation = evaluate_preview_copied_runtime_key_safety(
        template_bindings=template_bindings,
        policy_record=policy_record,
        preview_context=preview_context,
        preview_slug=preview_slug,
        copied_keys=copied_keys,
    )
    if evaluation.status == "pass":
        return ()
    refused_keys = {finding.binding_key for finding in evaluation.findings if finding.binding_key}
    # A failure not tied to a key leaves every copied credential out.
    return tuple(sorted(refused_keys)) if refused_keys else copied_keys
