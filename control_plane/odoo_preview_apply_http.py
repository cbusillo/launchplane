from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any, cast
import re

import click
from pydantic import Field, model_validator

from control_plane import odoo_instance_overrides as control_plane_odoo_instance_overrides
from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.artifact_dependency_provenance import (
    normalize_artifact_git_commit,
    normalize_artifact_sha256_digest,
)
from control_plane.contracts.idempotency_record import (
    format_launchplane_mutation_timestamp,
    parse_launchplane_mutation_timestamp,
)
from control_plane.contracts.preview_generation_record import PreviewGenerationRecord
from control_plane.contracts.preview_mutation_request import (
    PreviewGenerationMutationRequest,
    PreviewMutationRequest,
)
from control_plane.contracts.preview_record import PreviewRecord
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
    build_odoo_preview_apply_inputs,
    execute_odoo_preview_dokploy_apply,
    observe_odoo_preview_dokploy_apply,
    odoo_preview_destroy_target_is_quiescent,
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
_ODOO_PREVIEW_DESTROY_REASON_PREFIX = "odoo_preview_destroy"


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

    @model_validator(mode="after")
    def _validate_alignment(self) -> "OdooPreviewApplyEnvelope":
        _validate_driver_envelope_product(self.product, label="Odoo preview apply")
        if self.product.strip() != self.apply.dry_run_plan.product.strip():
            raise ValueError("Odoo preview apply requires matching product values.")
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
    driver_result = build_odoo_preview_apply_inputs(
        control_plane_root=control_plane_root,
        record_store=cast(Any, record_store),
        profile=profile,
        request=request,
        database_url=database_url,
    )
    return driver_result.model_dump(mode="json")


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
    if profile.preview.template_instance != issued_plan.template_instance:
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
    if resolved_observed_at >= provenance.expires_at:
        raise OdooPreviewPlanProvenanceError(
            code="odoo_preview_plan_expired",
            message="Odoo preview plan has expired; request fresh apply inputs.",
        )
    try:
        current_plan = build_odoo_preview_apply_inputs(
            control_plane_root=control_plane_root,
            record_store=cast(Any, record_store),
            profile=profile,
            request=issued_plan.plan_request,
            database_url=database_url,
        )
    except (FileNotFoundError, click.ClickException) as error:
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
) -> dict[str, object]:
    current_request = refresh_odoo_preview_issued_plan(
        control_plane_root=control_plane_root_path,
        record_store=record_store,
        profile=profile,
        request=request,
        issued_plan=issued_plan,
        database_url=database_url,
    )
    if current_request.apply.dry_run_plan.repository.strip() != profile.repository.strip():
        raise ValueError("Odoo preview apply repository does not match product profile.")
    resolved_environment_values = _odoo_preview_service_environment_values(
        control_plane_root_path=control_plane_root_path,
        record_store=record_store,
        profile=profile,
        apply_request=current_request.apply,
        database_url=database_url,
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
    driver_result = execute_odoo_preview_dokploy_apply(
        control_plane_root=control_plane_root_path,
        request=service_apply_request,
        database_url=database_url,
        provider_operation_title=provider_operation_title,
        provider_effect_checkpoint=provider_effect_checkpoint,
        provider_lease_check=provider_lease_check,
        expected_runtime_identity=resolved_runtime_identity,
    )
    return driver_result.model_dump(mode="json")


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
    before_destroy: Callable[[], dict[str, object]] | None = None,
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
        if existing_preview is not None and existing_preview.destroy_reason == destroy_reason:
            return _odoo_preview_lifecycle_records(
                preview=existing_preview,
                status="replayed",
                transition="destroyed",
            )
        if existing_preview is not None and _odoo_preview_operation_is_stale(
            record_store=typed_record_store,
            preview=existing_preview,
            issued_at=requested_at,
        ):
            return _odoo_preview_lifecycle_records(
                preview=existing_preview,
                status="stale",
                transition=existing_preview.state,
            )
        destroy_records = before_destroy() if before_destroy is not None else {}
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
        )
        preview_path = typed_record_store.write_preview_record(transitioned_preview)
        return {
            **destroy_records,
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
        if preview.state == "destroyed" and preview.destroy_reason == expected_destroy_reason:
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
) -> bool:
    existing_issued_at = _odoo_preview_existing_operation_issued_at(
        record_store=record_store,
        preview=preview,
    )
    if not existing_issued_at:
        return True
    current_issued_at = parse_launchplane_mutation_timestamp(
        issued_at,
        field_name="Odoo preview lifecycle issued_at",
    )
    parsed_existing_issued_at = parse_launchplane_mutation_timestamp(
        existing_issued_at,
        field_name="existing preview lifecycle issued_at",
    )
    return current_issued_at <= parsed_existing_issued_at


def _odoo_preview_existing_operation_issued_at(
    *,
    record_store: LaunchplaneMutationStore,
    preview: PreviewRecord,
) -> str:
    if preview.state == "destroyed":
        prefix = f"{_ODOO_PREVIEW_DESTROY_REASON_PREFIX}:"
        if preview.destroy_reason.startswith(prefix):
            _token, separator, issued_at = preview.destroy_reason[len(prefix) :].partition(":")
            if separator:
                return issued_at
        return ""
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


def odoo_preview_destroy_supersession_is_quiescent(
    *,
    control_plane_root_path: Path,
    request: OdooPreviewApplyEnvelope,
    database_url: str | None,
) -> bool:
    return odoo_preview_destroy_target_is_quiescent(
        control_plane_root=control_plane_root_path,
        request=request.apply,
        database_url=database_url,
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


def _odoo_preview_service_environment_values(
    *,
    control_plane_root_path: Path,
    record_store: object,
    profile: LaunchplaneProductProfileRecord,
    apply_request: OdooPreviewDokployApplyRequest,
    database_url: str | None,
) -> dict[str, str]:
    plan = apply_request.dry_run_plan
    if plan.operation == "destroy":
        return {}
    preview_profile = profile.preview
    template_instance = preview_profile.template_instance.strip()
    environment_values = control_plane_runtime_environments.resolve_runtime_environment_values(
        control_plane_root=control_plane_root_path,
        context_name=preview_profile.context,
        instance_name=template_instance,
        database_url=database_url,
    )
    environment_values.update(preview_profile.override_env)
    try:
        template_override_record = cast(Any, record_store).read_odoo_instance_override_record(
            context_name=preview_profile.context,
            instance_name=template_instance,
        )
    except FileNotFoundError:
        template_override_record = None
    preview_bootstrap_environment = (
        control_plane_odoo_instance_overrides.build_preview_website_bootstrap_environment(
            template_override_record,
            preview_url=plan.preview_url,
        )
    )
    if preview_bootstrap_environment is not None:
        environment_values.update(preview_bootstrap_environment.inline_environment)
    environment_values["ODOO_PROJECT_NAME"] = plan.compose_name
    environment_values["ODOO_STACK_NAME"] = plan.compose_name
    environment_values["ODOO_DB_NAME"] = _odoo_preview_identifier(plan.compose_name, suffix="db")
    environment_values["ODOO_DATA_VOLUME"] = _odoo_preview_identifier(
        plan.compose_name, suffix="data"
    )
    environment_values["ODOO_LOG_VOLUME"] = _odoo_preview_identifier(
        plan.compose_name, suffix="logs"
    )
    environment_values["ODOO_DB_VOLUME"] = _odoo_preview_identifier(
        plan.compose_name, suffix="db-volume"
    )
    for key in preview_profile.preview_url_env_keys:
        environment_values[key] = plan.preview_url
    for key in preview_profile.preview_domain_env_keys:
        environment_values[key] = plan.domain_host
    missing_env_keys = tuple(
        key for key in ODOO_PREVIEW_REQUIRED_ENV_KEYS if not environment_values.get(key, "").strip()
    )
    if missing_env_keys:
        raise OdooPreviewApplyConfigError(
            context=preview_profile.context,
            instance=template_instance,
            missing_keys=missing_env_keys,
        )
    return environment_values


def _odoo_preview_identifier(value: str, *, suffix: str) -> str:
    normalized = re.sub(r"[^a-zA-Z0-9]+", "_", value.strip()).strip("_").lower()
    if not normalized:
        normalized = "odoo_preview"
    suffix_identifier = re.sub(r"[^a-zA-Z0-9]+", "_", suffix.strip()).strip("_").lower()
    return f"{normalized}_{suffix_identifier}" if suffix_identifier else normalized
