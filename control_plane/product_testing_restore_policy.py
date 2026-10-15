"""Prepare a same-product production source for a disposable Odoo testing restore."""

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductOdooLaneDataPolicy,
)


class TestingRestorePolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    mode: Literal["dry-run", "apply"] = "dry-run"
    reason: str = Field(min_length=1)
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def validate_review(self) -> "TestingRestorePolicyRequest":
        self.context = self.context.strip()
        self.reason = self.reason.strip()
        if not self.context or not self.reason:
            raise ValueError("A testing context and reason are required.")
        if self.mode == "apply":
            if not re.fullmatch(r"[a-f0-9]{64}", self.reviewed_plan_sha256):
                raise ValueError("Apply requires the reviewed plan SHA-256.")
        elif self.reviewed_plan_sha256:
            raise ValueError("Dry run cannot supply a reviewed plan.")
        return self


class TestingRestorePolicyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: Literal["testing"] = "testing"
    source_context: str
    source_instance: Literal["prod"] = "prod"
    policy_before: ProductOdooLaneDataPolicy
    policy_after: ProductOdooLaneDataPolicy
    changed: bool
    applied: bool = False
    plan_sha256: str


def build_testing_restore_policy_plan(
    profile: LaunchplaneProductProfileRecord, request: TestingRestorePolicyRequest
) -> tuple[TestingRestorePolicyPlan, LaunchplaneProductProfileRecord]:
    if profile.driver_id != "odoo" or profile.lifecycle_state != "active":
        raise ValueError("Restore policy preparation requires an active Odoo product.")
    testing = [
        lane
        for lane in profile.lanes
        if (lane.context, lane.instance) == (request.context, request.instance)
    ]
    production = [lane for lane in profile.lanes if lane.instance == "prod"]
    if len(testing) != 1 or len(production) != 1:
        raise ValueError("An exact owned testing lane and one production source are required.")
    lane, upstream = testing[0], production[0]
    # This is a source reference, not an SSH host, credential, or a restore request.
    # Operational source settings remain in the lane's managed runtime records.
    policy = ProductOdooLaneDataPolicy(
        data_authority=(
            "restorable"
            if lane.odoo_data_policy.data_authority == "unknown"
            else lane.odoo_data_policy.data_authority
        ),
        allowed_rebuild_sources=tuple(
            dict.fromkeys((*lane.odoo_data_policy.allowed_rebuild_sources, "upstream_restore"))
        ),
        upstream_source=f"{profile.product}:{upstream.context}:{upstream.instance}",
        requires_runtime_identity=True,
    )
    digest = hashlib.sha256(
        json.dumps(
            {
                "profile": profile.model_dump(mode="json"),
                "context": request.context,
                "policy": policy.model_dump(mode="json"),
                "reason": request.reason,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()
    replacement = profile.model_copy(
        update={
            "lanes": tuple(
                candidate.model_copy(update={"odoo_data_policy": policy})
                if candidate == lane
                else candidate
                for candidate in profile.lanes
            )
        }
    )
    replacement = LaunchplaneProductProfileRecord.model_validate(
        replacement.model_dump(mode="json")
    ).validate_write_contract()
    return (
        TestingRestorePolicyPlan(
            product=profile.product,
            context=lane.context,
            source_context=upstream.context,
            policy_before=lane.odoo_data_policy,
            policy_after=policy,
            changed=lane.odoo_data_policy != policy,
            plan_sha256=digest,
        ),
        replacement,
    )
