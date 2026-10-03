"""Bounded, reviewed changes to a product's production-use classification."""

from __future__ import annotations

import hashlib
import json
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord

ProductionUse = Literal["unknown", "prelaunch", "live"]
ProductionUseMode = Literal["dry-run", "apply"]
PRODUCT_PRODUCTION_USE_SOURCE = "service:product-production-use"


class ProductProductionUseChangedError(ValueError):
    """The reviewed profile, classification, or reason no longer matches."""


class ProductProductionUseApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    mode: ProductionUseMode = "dry-run"
    production_use: ProductionUse
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
    changed: bool
    applied: bool = False
    reason: str
    source_label: str = PRODUCT_PRODUCTION_USE_SOURCE
    profile_updated_at_before: str
    profile_updated_at_after: str = ""
    plan_sha256: str


def build_product_production_use_plan(
    *, profile: LaunchplaneProductProfileRecord, request: ProductProductionUseApplyRequest
) -> ProductProductionUsePlan:
    evidence = {
        "profile": profile.model_dump(mode="json"),
        "production_use": request.production_use,
        "reason": request.reason,
        "source": PRODUCT_PRODUCTION_USE_SOURCE,
    }
    digest = hashlib.sha256(
        json.dumps(evidence, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    if request.mode == "apply" and request.reviewed_plan_sha256 != digest:
        raise ProductProductionUseChangedError("The plan changed; review a new dry run.")
    return ProductProductionUsePlan(
        mode=request.mode,
        product=profile.product,
        production_use_before=profile.production_use,
        production_use_after=request.production_use,
        changed=profile.production_use != request.production_use,
        reason=request.reason,
        profile_updated_at_before=profile.updated_at,
        plan_sha256=digest,
    )


def updated_product_production_use_profile(
    *, profile: LaunchplaneProductProfileRecord, production_use: ProductionUse, updated_at: str
) -> LaunchplaneProductProfileRecord:
    updated = profile.model_copy(
        update={
            "production_use": production_use,
            "updated_at": updated_at,
            "source": PRODUCT_PRODUCTION_USE_SOURCE,
        }
    )
    return LaunchplaneProductProfileRecord.model_validate(updated.model_dump(mode="json"))
