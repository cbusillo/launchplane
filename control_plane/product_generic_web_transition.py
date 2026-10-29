"""Review a profile's shared-web driver declarations without replacing runtime state."""

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.generic_web_extensions import GenericWebLaneExtensions

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    PreviewSecretEncoding,
    CURRENT_PRODUCT_PROFILE_SCHEMA_VERSION,
    validate_product_profile_history_transition,
)
from control_plane.workflows.generic_web_deploy import product_profile_uses_generic_web_base
from control_plane.workflows.generic_web_post_deploy import (
    generic_web_post_deploy_executor_for_profile,
)
from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256


class GenericWebLaneDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str = Field(min_length=1)
    instance: str = Field(min_length=1)
    extensions: GenericWebLaneExtensions


class GenericWebPreviewDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_transport_mode: Literal["none", "driver"]
    transport_extension: Literal["", "postgres"] = ""
    transport_database_prefix: str = ""
    generated_secret_env: dict[str, PreviewSecretEncoding] = Field(default_factory=dict)
    preview_url_env_keys: tuple[str, ...] = ()
    preview_domain_env_keys: tuple[str, ...] = ()
    app_name_suffix: str = ""
    generic_web_extensions: GenericWebLaneExtensions | None = None


class ProductGenericWebTransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    mode: Literal["dry-run", "apply"] = "dry-run"
    lanes: tuple[GenericWebLaneDeclaration, ...]
    preview: GenericWebPreviewDeclaration | None = None
    reason: str
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def validate_review(self) -> "ProductGenericWebTransitionRequest":
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("A reason is required for a shared-web transition.")
        if self.mode == "apply":
            if not re.fullmatch(r"[0-9a-f]{64}", self.reviewed_plan_sha256):
                raise ValueError("Apply requires the reviewed plan SHA-256.")
        elif self.reviewed_plan_sha256:
            raise ValueError("Dry run cannot supply a reviewed plan.")
        return self


class GenericWebPreviewEndpoint(BaseModel):
    model_config = ConfigDict(extra="forbid")
    runtime_port: int
    health_path: str
    certificate_type: Literal["none", "letsencrypt"]
    https_policy: Literal["preview-url-scheme", "certificate"]


def _preview_endpoint(profile: LaunchplaneProductProfileRecord) -> GenericWebPreviewEndpoint | None:
    if profile.preview.transport_extension not in {"", "postgres"}:
        return None
    return GenericWebPreviewEndpoint(
        runtime_port=profile.runtime_port,
        health_path=profile.health_path,
        certificate_type=profile.preview.domain_certificate_type,
        https_policy="preview-url-scheme"
        if profile.preview.transport_extension == "postgres"
        else "certificate",
    )


class ProductGenericWebTransitionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    driver_before: str
    preview_endpoint_before: GenericWebPreviewEndpoint | None
    preview_endpoint_after: GenericWebPreviewEndpoint | None
    driver_after: Literal["generic-web"] = "generic-web"
    lanes: tuple[GenericWebLaneDeclaration, ...]
    preview: GenericWebPreviewDeclaration
    changed: bool
    applied: bool = False
    reason: str
    plan_sha256: str


def build_generic_web_transition_plan(
    profile: LaunchplaneProductProfileRecord, request: ProductGenericWebTransitionRequest
) -> tuple[ProductGenericWebTransitionPlan, LaunchplaneProductProfileRecord]:
    legacy_profile = profile.driver_id == "verireel"
    if not profile.is_active or not (
        legacy_profile or product_profile_uses_generic_web_base(profile)
    ):
        raise ValueError("Shared-web transition requires an active generic-web based product.")
    declarations = {(item.context, item.instance): item.extensions for item in request.lanes}
    existing = {(lane.context, lane.instance) for lane in profile.lanes}
    if len(declarations) != len(request.lanes) or set(declarations) != existing:
        raise ValueError("Declare each recorded lane exactly once, without adding or moving lanes.")
    lanes = tuple(
        lane.model_copy(
            update={"generic_web_extensions": declarations[lane.context, lane.instance]}
        )
        for lane in profile.lanes
    )
    if any(
        lane.generic_web_extensions.verification_extension and lane.instance != "testing"
        for lane in lanes
    ):
        raise ValueError("The testing verification extension requires the recorded testing lane.")
    if profile.preview.transport_extension not in {"", "postgres"} and (
        request.preview is None
        or request.preview.transport_extension != "postgres"
        or not {"generated_secret_env", "preview_url_env_keys", "preview_domain_env_keys"}.issubset(
            request.preview.model_fields_set
        )
    ):
        raise ValueError(
            "Declare postgres transport, generated_secret_env, preview_url_env_keys and preview_domain_env_keys from the product runtime contract before transitioning a legacy preview profile. Use empty collections only when the product requires none."
        )
    preview = profile.preview
    if request.preview is not None:
        updates = request.preview.model_dump(exclude_unset=True, exclude={"generic_web_extensions"})
        if request.preview.generic_web_extensions is not None:
            updates["generic_web_extensions"] = request.preview.generic_web_extensions
        preview = preview.model_copy(update=updates)
    if preview.data_transport_mode == "driver" and not preview.transport_extension:
        raise ValueError("Driver preview transport requires an explicit shared-web extension.")
    replacement = LaunchplaneProductProfileRecord.model_validate(
        profile.model_copy(
            update={
                "schema_version": CURRENT_PRODUCT_PROFILE_SCHEMA_VERSION,
                "driver_id": "generic-web",
                "lanes": lanes,
                "preview": preview,
            }
        ).model_dump(mode="json")
    ).validate_write_contract()
    validate_product_profile_history_transition(
        existing_profile=profile, replacement_profile=replacement
    )
    for lane in profile.lanes:
        previous_executor = generic_web_post_deploy_executor_for_profile(
            profile, instance=lane.instance
        )
        if previous_executor is not None and previous_executor is not (
            generic_web_post_deploy_executor_for_profile(replacement, instance=lane.instance)
        ):
            raise ValueError(
                "The existing lane qualification must have a supported shared-web declaration."
            )
    preview_declaration = GenericWebPreviewDeclaration.model_validate(
        {
            field: getattr(replacement.preview, field)
            for field in GenericWebPreviewDeclaration.model_fields
        }
    )
    lane_declarations = tuple(
        GenericWebLaneDeclaration(
            context=lane.context, instance=lane.instance, extensions=lane.generic_web_extensions
        )
        for lane in replacement.lanes
    )
    fingerprint = {
        "profile": profile.model_dump(mode="json"),
        "lanes": [item.model_dump(mode="json") for item in lane_declarations],
        "preview": preview_declaration.model_dump(mode="json"),
        "reason": request.reason,
    }
    digest = nonvalidating_compact_sorted_json_sha256(fingerprint)
    return (
        ProductGenericWebTransitionPlan(
            product=profile.product,
            driver_before=profile.driver_id,
            preview_endpoint_before=_preview_endpoint(profile),
            preview_endpoint_after=_preview_endpoint(replacement),
            lanes=lane_declarations,
            preview=preview_declaration,
            changed=profile != replacement,
            reason=request.reason,
            plan_sha256=digest,
        ),
        replacement,
    )
