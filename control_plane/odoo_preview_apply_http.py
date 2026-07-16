from collections.abc import Callable
from datetime import datetime, timedelta, timezone
import hashlib
from pathlib import Path
from typing import Any, cast
import re

import click
from pydantic import Field, model_validator

from control_plane import runtime_environments as control_plane_runtime_environments
from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.drivers.dispatch import (
    _ProductRouteEnvelope,
    _validate_driver_envelope_product,
)
from control_plane.drivers.registry import read_driver_descriptor
from control_plane.workflows.odoo_preview_runtime import (
    ODOO_PREVIEW_REQUIRED_ENV_KEYS,
    OdooPreviewApplyInputsResult,
    OdooPreviewApplyPlanProvenance,
    OdooPreviewApplyInputsRequest,
    OdooPreviewDokployApplyRequest,
    build_odoo_preview_apply_inputs,
    execute_odoo_preview_dokploy_apply,
    observe_odoo_preview_dokploy_apply,
    odoo_preview_apply_plan_sha256,
)


ODOO_PREVIEW_APPLY_ROUTE = "/v1/drivers/odoo/preview-apply"
ODOO_PREVIEW_APPLY_INPUTS_ROUTE = "/v1/drivers/odoo/preview-apply-inputs"
ODOO_DRIVER_ID = "odoo"
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


def _product_profile_uses_odoo_driver(profile: LaunchplaneProductProfileRecord) -> bool:
    profile_driver_id = profile.driver_id.strip()
    if profile_driver_id == ODOO_DRIVER_ID:
        return True
    try:
        descriptor = read_driver_descriptor(profile_driver_id)
    except FileNotFoundError:
        return False
    return descriptor.base_driver_id == ODOO_DRIVER_ID


def resolve_odoo_preview_apply_profile(
    *,
    record_store: object,
    product: str,
) -> LaunchplaneProductProfileRecord:
    read_profile = getattr(record_store, "read_product_profile_record", None)
    if not callable(read_profile):
        raise ValueError("Product driver validation requires product profile storage.")
    try:
        profile = read_profile(product.strip())
    except FileNotFoundError as error:
        raise OdooPreviewApplyRouteDependencyError from error
    if not isinstance(profile, LaunchplaneProductProfileRecord):
        profile = LaunchplaneProductProfileRecord.model_validate(profile)
    if not _product_profile_uses_odoo_driver(profile):
        raise OdooPreviewApplyProductMismatchError(
            "Product is not configured for the requested Odoo driver route."
        )
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
        profile=profile,
        apply_request=current_request.apply,
        database_url=database_url,
    )
    service_dry_run_plan = current_request.apply.dry_run_plan.model_copy(
        update={
            "domain_certificate_type": profile.preview.domain_certificate_type,
        }
    )
    service_apply_request = current_request.apply.model_copy(
        update={
            "dry_run_plan": service_dry_run_plan,
            "environment_values": resolved_environment_values,
        }
    )
    driver_result = execute_odoo_preview_dokploy_apply(
        control_plane_root=control_plane_root_path,
        request=service_apply_request,
        database_url=database_url,
        provider_operation_title=provider_operation_title,
        provider_effect_checkpoint=provider_effect_checkpoint,
        provider_lease_check=provider_lease_check,
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
