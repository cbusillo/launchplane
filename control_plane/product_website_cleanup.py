"""Reviewed withdrawal of stale public declarations on privately monitored products."""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord


class ProductWebsiteCleanupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    mode: Literal["dry-run", "apply"] = "dry-run"
    reason: str
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def validate_review(self) -> "ProductWebsiteCleanupRequest":
        self.reason = self.reason.strip()
        if not self.reason:
            raise ValueError("A reason is required for public website withdrawal.")
        if self.mode == "apply":
            if not re.fullmatch(r"[0-9a-f]{64}", self.reviewed_plan_sha256):
                raise ValueError("Apply requires the reviewed plan SHA-256.")
        elif self.reviewed_plan_sha256:
            raise ValueError("Dry run cannot supply a reviewed plan.")
        return self


class WebsiteCleanupChange(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    before: str | bool
    after: str | bool


class ProductWebsiteCleanupPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    changed: bool
    applied: bool = False
    public_website_after: Literal["none"] = "none"
    changes: tuple[WebsiteCleanupChange, ...]
    profile_sha256_before: str
    plan_sha256: str
    reason: str


def _digest(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def build_website_cleanup(
    profile: LaunchplaneProductProfileRecord,
    request: ProductWebsiteCleanupRequest,
) -> tuple[ProductWebsiteCleanupPlan, LaunchplaneProductProfileRecord]:
    # Public or unknown intent cannot be silently interpreted as a private product.
    # This control only withdraws ineffective public configuration on private lanes.
    if not profile.lanes or any(
        lane.health_monitoring.monitoring_intent != "private" for lane in profile.lanes
    ):
        raise ValueError("Every recorded lane must declare private monitoring before cleanup.")
    payload = profile.model_dump(mode="json")
    changes: list[WebsiteCleanupChange] = []

    def change(target: dict[str, object], field: str, after: str | bool, path: str) -> None:
        before = target.get(field, "required" if field == "public_website" else "")
        if before != after:
            if not isinstance(before, (str, bool)):
                raise ValueError("Unsupported public declaration value.")
            changes.append(WebsiteCleanupChange(path=path, before=before, after=after))
            target[field] = after

    change(payload, "public_website", "none", "public_website")
    for index, lane in enumerate(payload["lanes"]):
        prefix = f"lanes[{index}] ({lane['context']}/{lane['instance']})"
        change(lane, "base_url", "", f"{prefix}.base_url")
        change(lane, "health_url", "", f"{prefix}.health_url")
        for check_index, check in enumerate(lane["health_monitoring"]["checks"]):
            if check["kind"] == "public_http":
                check_prefix = f"{prefix}.checks[{check_index}] ({check['name']})"
                change(check, "enabled", False, f"{check_prefix}.enabled")
                change(check, "url", "", f"{check_prefix}.url")
    replacement = LaunchplaneProductProfileRecord.model_validate(payload).validate_write_contract()
    profile_sha256 = _digest(profile.model_dump(mode="json"))
    plan = ProductWebsiteCleanupPlan(
        product=profile.product,
        changed=bool(changes),
        changes=tuple(changes),
        profile_sha256_before=profile_sha256,
        plan_sha256=_digest(
            {
                "profile_sha256": profile_sha256,
                "changes": [change.model_dump(mode="json") for change in changes],
                "reason": request.reason,
            }
        ),
        reason=request.reason,
    )
    return plan, replacement
