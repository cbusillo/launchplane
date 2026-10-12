"""Review a display-name change without replacing product authority."""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord


class ProductDisplayNameRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    mode: Literal["dry-run", "apply"] = "dry-run"
    display_name: str = Field(min_length=1)
    reason: str
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def validate_review(self) -> "ProductDisplayNameRequest":
        self.display_name = self.display_name.strip()
        self.reason = self.reason.strip()
        if not self.display_name or any(ord(c) < 32 for c in self.display_name):
            raise ValueError("Supply a nonempty, single-line display name.")
        if not self.reason:
            raise ValueError("A reason is required for a display-name change.")
        if self.mode == "apply":
            if not re.fullmatch(r"[0-9a-f]{64}", self.reviewed_plan_sha256):
                raise ValueError("Apply requires the reviewed plan SHA-256.")
        elif self.reviewed_plan_sha256:
            raise ValueError("Dry run cannot supply a reviewed plan.")
        return self


class ProductDisplayNamePlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    display_name_before: str
    display_name_after: str
    changed: bool
    applied: bool = False
    plan_sha256: str


def build_display_name_plan(
    profile: LaunchplaneProductProfileRecord, request: ProductDisplayNameRequest
) -> tuple[ProductDisplayNamePlan, LaunchplaneProductProfileRecord]:
    fingerprint = {
        "profile": profile.model_dump(mode="json"),
        "display_name": request.display_name,
        "reason": request.reason,
    }
    digest = hashlib.sha256(
        json.dumps(fingerprint, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    return (
        ProductDisplayNamePlan(
            product=profile.product,
            display_name_before=profile.display_name,
            display_name_after=request.display_name,
            changed=profile.display_name != request.display_name,
            plan_sha256=digest,
        ),
        profile.model_copy(update={"display_name": request.display_name}),
    )
