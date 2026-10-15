"""Bounded, reviewed changes to a product's production-use classification, and to
whether its Client's release acceptance starts the release."""

from __future__ import annotations

import hashlib
import json
from typing import Literal
from collections.abc import Iterable

from control_plane.contracts.deployment_record import DeploymentRecord
from control_plane.contracts.promotion_record import PromotionRecord
from control_plane.contracts.preview_record import PreviewRecord

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ReleaseOnAcceptance,
    ProductionUseAuthority as ProductionUseAuthority,
)

ProductionUse = Literal["unknown", "prelaunch", "live"]
ProductionUseMode = Literal["dry-run", "apply"]
PRODUCT_PRODUCTION_USE_SOURCE = "service:product-production-use"
DIRECTOR_NEVER_LIVE_SOURCE = "service:director-never-live-classification"


class ProductProductionUseChangedError(ValueError):
    """The reviewed profile, classification, or reason no longer matches."""


class ProductProductionUseApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    mode: ProductionUseMode = "dry-run"
    production_use: ProductionUse
    # Omitted: unchanged. Standing acceptance is an explicit admin attestation
    # that the recorded Client is the Director; it is never inferred from admin access.
    release_on_acceptance: ReleaseOnAcceptance | None = None
    reviewed_plan_sha256: str = ""
    reason: str

    @model_validator(mode="after")
    def _validate_request(self) -> "ProductProductionUseApplyRequest":
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("Production use change requires a reason.")
        if self.mode == "apply" and (
            len(self.reviewed_plan_sha256) != 64
            or any(character not in "0123456789abcdef" for character in self.reviewed_plan_sha256)
        ):
            raise ValueError("Apply requires the reviewed dry-run plan digest.")
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Dry run cannot carry a reviewed plan digest.")
        return self


class ProductProductionUsePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    mode: ProductionUseMode
    product: str
    production_use_before: ProductionUse
    production_use_after: ProductionUse
    release_on_acceptance_before: ReleaseOnAcceptance = "held"
    release_on_acceptance_after: ReleaseOnAcceptance = "held"
    changed: bool
    applied: bool = False
    reason: str
    source_label: str = PRODUCT_PRODUCTION_USE_SOURCE
    profile_updated_at_before: str
    profile_updated_at_after: str = ""
    plan_sha256: str
    prelaunch_bootstrap_authorized_before: bool | None = Field(
        default=None, exclude_if=lambda value: value is None
    )
    prelaunch_bootstrap_authorized_after: bool | None = Field(
        default=None, exclude_if=lambda value: value is None
    )


def build_product_production_use_plan(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ProductProductionUseApplyRequest,
    authority: ProductionUseAuthority = "explicit",
) -> ProductProductionUsePlan:
    source = (
        DIRECTOR_NEVER_LIVE_SOURCE
        if authority == "director_agent"
        else PRODUCT_PRODUCTION_USE_SOURCE
    )
    evidence = {
        "profile": profile.model_dump(mode="json"),
        "production_use": request.production_use,
        "reason": request.reason,
        "source": source,
    }
    if authority != "explicit" or profile.production_use_authority != "explicit":
        evidence["production_use_authority"] = authority
    if request.release_on_acceptance is not None:
        evidence["release_on_acceptance"] = request.release_on_acceptance
    release_on_acceptance_after = request.release_on_acceptance or profile.release_on_acceptance
    if release_on_acceptance_after == "director_standing":
        if profile.driver_id != "generic-web" or not profile.owner.is_set:
            raise ValueError(
                "Director standing acceptance requires a generic-web product with a recorded Client."
            )
    digest = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if request.mode == "apply" and request.reviewed_plan_sha256 != digest:
        raise ProductProductionUseChangedError("The plan changed; review a new dry run.")
    bootstrap_reconfirmation = (
        profile.production_use_authority == "director_agent"
        and authority == "explicit"
        and request.production_use == "prelaunch"
    )
    return ProductProductionUsePlan(
        mode=request.mode,
        product=profile.product,
        production_use_before=profile.production_use,
        production_use_after=request.production_use,
        release_on_acceptance_before=profile.release_on_acceptance,
        release_on_acceptance_after=release_on_acceptance_after,
        changed=profile.production_use != request.production_use
        or profile.release_on_acceptance != release_on_acceptance_after
        or profile.production_use_authority != authority,
        reason=request.reason,
        source_label=source,
        profile_updated_at_before=profile.updated_at,
        plan_sha256=digest,
        prelaunch_bootstrap_authorized_before=False if bootstrap_reconfirmation else None,
        prelaunch_bootstrap_authorized_after=True if bootstrap_reconfirmation else None,
    )


def updated_product_production_use_profile(
    *,
    profile: LaunchplaneProductProfileRecord,
    production_use: ProductionUse,
    release_on_acceptance: ReleaseOnAcceptance,
    updated_at: str,
    authority: ProductionUseAuthority = "explicit",
) -> LaunchplaneProductProfileRecord:
    updated = profile.model_copy(
        update={
            "production_use": production_use,
            "production_use_authority": authority,
            "release_on_acceptance": release_on_acceptance,
            "updated_at": updated_at,
            "source": DIRECTOR_NEVER_LIVE_SOURCE
            if authority == "director_agent"
            else PRODUCT_PRODUCTION_USE_SOURCE,
        }
    )
    return LaunchplaneProductProfileRecord.model_validate(updated.model_dump(mode="json"))


def require_never_live_classification(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ProductProductionUseApplyRequest,
    deployments: Iterable[DeploymentRecord],
    promotions: Iterable[PromotionRecord],
    previews: Iterable[PreviewRecord],
) -> None:
    """Validate an attested never-live fact against known contradictions.

    The request records the Director-confirmed fact; missing history is not
    proof of it. Bounded-agent provenance never supplies a production exemption.

    A product with production or historical lane ownership needs the existing
    administrator path. The recorded fact is a retirement prerequisite;
    retirement still checks actual resources and recorded protected use.
    """
    if (
        request.production_use != "prelaunch"
        or request.release_on_acceptance is not None
        or profile.production_use not in {"unknown", "prelaunch"}
        or profile.lifecycle_state != "active"
        or not profile.lanes
        or any(lane.instance != "testing" for lane in profile.lanes)
        or profile.historical_contexts
    ):
        raise ValueError(
            "Agent classification requires an active testing-only product without live or "
            "historical lane ownership and cannot change release acceptance."
        )
    preview_lanes = {
        (preview.context, profile.preview.slug_template.format(number=preview.anchor_pr_number))
        for preview in previews
        if preview.context == profile.preview.context
        and preview.anchor_repo in {profile.repository, profile.repository.partition("/")[2]}
    }
    testing_lanes = {(lane.context, lane.instance) for lane in profile.lanes}
    contexts = {profile.preview.context, *(lane.context for lane in profile.lanes)} - {""}
    if any(
        (record.context, record.instance) not in testing_lanes | preview_lanes
        for record in deployments
        if record.context in contexts
        or (
            record.runtime_identity is not None
            and record.runtime_identity.product == profile.product
        )
    ) or any(
        record.context in contexts
        or (
            record.destination_health.observed_runtime_identity is not None
            and record.destination_health.observed_runtime_identity.product == profile.product
        )
        for record in promotions
    ):
        raise ValueError("Recorded production or promotion history prevents agent classification.")
