from __future__ import annotations

import hashlib
import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    OdooDataAuthority,
    OdooRebuildSourceMode,
    ProductLaneMonitoringIntent,
    ProductLaneProfile,
    ProductOdooPrelaunchRebuildPolicy,
)


ProductPrelaunchRebuildPolicyMode = Literal["dry-run", "apply"]
ProductPrelaunchRebuildPolicyOperation = Literal["update", "unchanged"]
PRODUCT_PRELAUNCH_REBUILD_POLICY_SOURCE: Literal["service:product-prelaunch-rebuild-policy"] = (
    "service:product-prelaunch-rebuild-policy"
)


class ProductPrelaunchRebuildPolicyTargetError(ValueError):
    pass


class ProductPrelaunchRebuildPolicyDriverError(ValueError):
    pass


class ProductPrelaunchRebuildPolicyStateError(ValueError):
    pass


class ProductPrelaunchRebuildPolicyApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    product: str
    context: str
    instance: str
    enabled: bool = False
    approval_issue_url: str = ""
    data_source_mode: OdooRebuildSourceMode = "empty"
    confirmation: str = ""
    expected_target_name: str = ""
    expected_domains: tuple[str, ...] = ()
    mode: ProductPrelaunchRebuildPolicyMode = "dry-run"
    reason: str
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "ProductPrelaunchRebuildPolicyApplyRequest":
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.reason = self.reason.strip()
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        if not self.product:
            raise ValueError("Product prelaunch rebuild policy request requires product.")
        if not self.context:
            raise ValueError("Product prelaunch rebuild policy request requires context.")
        if not self.instance:
            raise ValueError("Product prelaunch rebuild policy request requires instance.")
        if not self.reason:
            raise ValueError("Product prelaunch rebuild policy request requires reason.")
        candidate_policy = ProductOdooPrelaunchRebuildPolicy(
            enabled=self.enabled,
            approval_issue_url=self.approval_issue_url,
            data_source_mode=self.data_source_mode,
            confirmation=self.confirmation,
            expected_target_name=self.expected_target_name,
            expected_domains=self.expected_domains,
        )
        if not self.enabled:
            candidate_policy = ProductOdooPrelaunchRebuildPolicy()
        self.approval_issue_url = candidate_policy.approval_issue_url
        self.data_source_mode = candidate_policy.data_source_mode
        self.confirmation = candidate_policy.confirmation
        self.expected_target_name = candidate_policy.expected_target_name
        self.expected_domains = candidate_policy.expected_domains
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError(
                "Product prelaunch rebuild policy dry-run rejects reviewed_plan_sha256."
            )
        if self.mode == "apply" and not re.fullmatch(r"[0-9a-f]{64}", self.reviewed_plan_sha256):
            raise ValueError(
                "Product prelaunch rebuild policy apply requires a reviewed "
                "64-character plan SHA-256."
            )
        return self

    def requested_policy(self) -> ProductOdooPrelaunchRebuildPolicy:
        return ProductOdooPrelaunchRebuildPolicy(
            enabled=self.enabled,
            approval_issue_url=self.approval_issue_url,
            data_source_mode=self.data_source_mode,
            confirmation=self.confirmation,
            expected_target_name=self.expected_target_name,
            expected_domains=self.expected_domains,
        )


class ProductPrelaunchRebuildPolicyPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    mode: ProductPrelaunchRebuildPolicyMode
    product: str
    context: str
    instance: str
    current_policy: ProductOdooPrelaunchRebuildPolicy
    requested_policy: ProductOdooPrelaunchRebuildPolicy
    data_authority: OdooDataAuthority
    allowed_rebuild_sources: tuple[OdooRebuildSourceMode, ...] = ()
    monitoring_intent: ProductLaneMonitoringIntent
    operation: ProductPrelaunchRebuildPolicyOperation
    changed: bool
    applied: bool = False
    reason: str
    source_label: Literal["service:product-prelaunch-rebuild-policy"] = (
        PRODUCT_PRELAUNCH_REBUILD_POLICY_SOURCE
    )
    profile_updated_at_before: str
    profile_updated_at_after: str = ""
    profile_sha256_before: str
    plan_sha256: str


def product_prelaunch_rebuild_policy_authority(
    profile: LaunchplaneProductProfileRecord,
) -> dict[tuple[str, str], dict[str, object]]:
    return {
        (lane.context, lane.instance): lane.odoo_prelaunch_rebuild.model_dump(mode="json")
        for lane in profile.lanes
    }


def build_product_prelaunch_rebuild_policy_plan(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ProductPrelaunchRebuildPolicyApplyRequest,
) -> ProductPrelaunchRebuildPolicyPlan:
    lane = _target_lane(profile=profile, request=request)
    requested_policy = request.requested_policy()
    _validate_requested_policy(lane=lane, policy=requested_policy)
    current_policy = lane.odoo_prelaunch_rebuild
    changed = current_policy.model_dump(mode="json") != requested_policy.model_dump(mode="json")
    operation: ProductPrelaunchRebuildPolicyOperation = "update" if changed else "unchanged"
    profile_sha256 = _canonical_sha256(profile.model_dump(mode="json"))
    plan_evidence = {
        "schema_version": 1,
        "product": profile.product,
        "context": lane.context,
        "instance": lane.instance,
        "current_policy": current_policy.model_dump(mode="json"),
        "requested_policy": requested_policy.model_dump(mode="json"),
        "data_authority": lane.odoo_data_policy.data_authority,
        "allowed_rebuild_sources": lane.odoo_data_policy.allowed_rebuild_sources,
        "monitoring_intent": lane.health_monitoring.monitoring_intent,
        "operation": operation,
        "profile_sha256": profile_sha256,
        "source_label": PRODUCT_PRELAUNCH_REBUILD_POLICY_SOURCE,
        "reason": request.reason,
    }
    return ProductPrelaunchRebuildPolicyPlan(
        mode=request.mode,
        product=profile.product,
        context=lane.context,
        instance=lane.instance,
        current_policy=current_policy,
        requested_policy=requested_policy,
        data_authority=lane.odoo_data_policy.data_authority,
        allowed_rebuild_sources=lane.odoo_data_policy.allowed_rebuild_sources,
        monitoring_intent=lane.health_monitoring.monitoring_intent,
        operation=operation,
        changed=changed,
        reason=request.reason,
        profile_updated_at_before=profile.updated_at,
        profile_sha256_before=profile_sha256,
        plan_sha256=_canonical_sha256(plan_evidence),
    )


def updated_product_prelaunch_rebuild_policy_profile(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ProductPrelaunchRebuildPolicyApplyRequest,
    updated_at: str,
) -> LaunchplaneProductProfileRecord:
    lane = _target_lane(profile=profile, request=request)
    requested_policy = request.requested_policy()
    _validate_requested_policy(lane=lane, policy=requested_policy)
    updated_lane = lane.model_copy(update={"odoo_prelaunch_rebuild": requested_policy})
    updated_lanes = tuple(
        updated_lane
        if candidate.context.strip() == request.context
        and candidate.instance.strip() == request.instance
        else candidate
        for candidate in profile.lanes
    )
    updated_profile = profile.model_copy(
        update={
            "lanes": updated_lanes,
            "updated_at": updated_at,
            "source": PRODUCT_PRELAUNCH_REBUILD_POLICY_SOURCE,
        }
    )
    return LaunchplaneProductProfileRecord.model_validate(
        updated_profile.model_dump(mode="json")
    ).validate_write_contract()


def _target_lane(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ProductPrelaunchRebuildPolicyApplyRequest,
) -> ProductLaneProfile:
    if profile.product != request.product:
        raise ProductPrelaunchRebuildPolicyTargetError(
            "Product prelaunch rebuild policy target does not match the loaded profile."
        )
    if profile.driver_id != "odoo":
        raise ProductPrelaunchRebuildPolicyDriverError(
            "Product prelaunch rebuild policy requires the Odoo driver."
        )
    matches = tuple(
        lane
        for lane in profile.lanes
        if lane.context.strip() == request.context and lane.instance.strip() == request.instance
    )
    if not matches:
        raise ProductPrelaunchRebuildPolicyTargetError(
            "Product prelaunch rebuild policy target lane does not exist."
        )
    if len(matches) != 1:
        raise ProductPrelaunchRebuildPolicyTargetError(
            "Product prelaunch rebuild policy target lane is ambiguous."
        )
    return matches[0]


def _validate_requested_policy(
    *,
    lane: ProductLaneProfile,
    policy: ProductOdooPrelaunchRebuildPolicy,
) -> None:
    if not policy.enabled:
        return
    if lane.health_monitoring.monitoring_intent != "prelaunch":
        raise ProductPrelaunchRebuildPolicyStateError(
            "Enabled product prelaunch rebuild policy requires prelaunch monitoring intent."
        )
    if not lane.odoo_data_policy.allows_rebuild_source(policy.data_source_mode):
        raise ProductPrelaunchRebuildPolicyStateError(
            "Product prelaunch rebuild data source is not allowed by the lane data policy."
        )
    if policy.data_source_mode == "empty" and lane.odoo_data_policy.data_authority != "resettable":
        raise ProductPrelaunchRebuildPolicyStateError(
            "Empty product prelaunch rebuild requires resettable lane data authority."
        )


def _canonical_sha256(payload: object) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
