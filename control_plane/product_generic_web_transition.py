"""Review a profile's shared-web driver declarations without replacing runtime state."""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.generic_web_extensions import GenericWebLaneExtensions
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    validate_product_profile_history_transition,
)
from control_plane.workflows.generic_web_deploy import product_profile_uses_generic_web_base
from control_plane.workflows.generic_web_post_deploy import (
    generic_web_post_deploy_executor_for_profile,
)


class GenericWebLaneDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str = Field(min_length=1)
    instance: str = Field(min_length=1)
    extensions: GenericWebLaneExtensions


class GenericWebPreviewDeclaration(BaseModel):
    model_config = ConfigDict(extra="forbid")

    data_transport_mode: Literal["none", "driver"]
    transport_extension: Literal["", "verireel-postgres"] = ""
    transport_database_prefix: str = ""
    app_name_suffix: str = ""


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


class ProductGenericWebTransitionPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    driver_before: str
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
    if not profile.is_active or not product_profile_uses_generic_web_base(profile):
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
    preview = profile.preview
    if request.preview is not None:
        preview = preview.model_copy(update=request.preview.model_dump())
    if preview.data_transport_mode == "driver" and not preview.transport_extension:
        raise ValueError("Driver preview transport requires an explicit shared-web extension.")
    replacement = LaunchplaneProductProfileRecord.model_validate(
        profile.model_copy(
            update={"driver_id": "generic-web", "lanes": lanes, "preview": preview}
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
    fingerprint = {
        "profile": profile.model_dump(mode="json"),
        "lanes": [item.model_dump(mode="json") for item in request.lanes],
        "preview": request.preview.model_dump(mode="json") if request.preview is not None else None,
        "reason": request.reason,
    }
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return (
        ProductGenericWebTransitionPlan(
            product=profile.product,
            driver_before=profile.driver_id,
            lanes=request.lanes,
            preview=GenericWebPreviewDeclaration.model_validate(
                {
                    field: getattr(preview, field)
                    for field in GenericWebPreviewDeclaration.model_fields
                }
            ),
            changed=profile != replacement,
            reason=request.reason,
            plan_sha256=digest,
        ),
        replacement,
    )
