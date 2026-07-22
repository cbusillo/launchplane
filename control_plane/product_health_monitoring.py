from __future__ import annotations

import hashlib
import json
import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.product_health_monitoring_migration import (
    canonical_health_check_record_token,
    health_check_record_token,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneHealthCheck,
    ProductLaneProfile,
)


ProductHealthMonitoringMode = Literal["dry-run", "apply"]
ProductHealthMonitoringOperation = Literal["create", "update", "unchanged"]
PRODUCT_HEALTH_MONITORING_SOURCE: Literal["service:product-health-monitoring"] = (
    "service:product-health-monitoring"
)


class ProductHealthMonitoringTargetError(ValueError):
    pass


class ProductHealthMonitoringCheckKindError(ValueError):
    pass


class ProductHealthMonitoringApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    product: str
    context: str
    instance: str
    check_name: str
    enabled: bool = True
    require_runtime_identity: bool = False
    mode: ProductHealthMonitoringMode = "dry-run"
    reason: str
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> "ProductHealthMonitoringApplyRequest":
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.check_name = self.check_name.strip()
        self.reason = self.reason.strip()
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        if not self.product:
            raise ValueError("Product health monitoring request requires product.")
        if not self.context:
            raise ValueError("Product health monitoring request requires context.")
        if not self.instance:
            raise ValueError("Product health monitoring request requires instance.")
        if not self.check_name or not health_check_record_token(self.check_name):
            raise ValueError("Product health monitoring request requires a valid check_name.")
        if not self.reason:
            raise ValueError("Product health monitoring request requires reason.")
        if not self.enabled and self.require_runtime_identity:
            raise ValueError(
                "Disabled product health monitoring checks cannot require runtime identity."
            )
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Product health monitoring dry-run rejects reviewed_plan_sha256.")
        if self.mode == "apply" and not re.fullmatch(r"[0-9a-f]{64}", self.reviewed_plan_sha256):
            raise ValueError(
                "Product health monitoring apply requires a reviewed 64-character plan SHA-256."
            )
        return self


class ProductHealthMonitoringPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    mode: ProductHealthMonitoringMode
    product: str
    context: str
    instance: str
    check_name: str
    operation: ProductHealthMonitoringOperation
    current_enabled: bool | None = None
    requested_enabled: bool
    current_require_runtime_identity: bool | None = None
    requested_require_runtime_identity: bool
    resolved_url: str
    changed: bool
    applied: bool = False
    reason: str
    source_label: Literal["service:product-health-monitoring"] = PRODUCT_HEALTH_MONITORING_SOURCE
    profile_updated_at_before: str
    profile_updated_at_after: str = ""
    profile_sha256_before: str
    plan_sha256: str


def build_product_health_monitoring_plan(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ProductHealthMonitoringApplyRequest,
) -> ProductHealthMonitoringPlan:
    lane = _target_lane(profile=profile, request=request)
    existing_check = _matching_check(lane=lane, check_name=request.check_name)
    if existing_check is not None and existing_check.kind != "public_http":
        raise ProductHealthMonitoringCheckKindError(
            "Product health monitoring can mutate only public HTTP checks."
        )
    candidate_check = _candidate_check(existing_check=existing_check, request=request)
    resolved_url = candidate_check.url or lane.health_url or lane.base_url
    _validate_resolved_url(
        lane=lane,
        check=candidate_check,
        resolved_url=resolved_url,
    )
    changed = existing_check is None or (
        existing_check.enabled != request.enabled
        or existing_check.require_runtime_identity != request.require_runtime_identity
    )
    operation: ProductHealthMonitoringOperation
    if existing_check is None:
        operation = "create"
    elif changed:
        operation = "update"
    else:
        operation = "unchanged"
    profile_sha256 = _canonical_sha256(profile.model_dump(mode="json"))
    plan_evidence = {
        "schema_version": 1,
        "product": profile.product,
        "context": lane.context,
        "instance": lane.instance,
        "check_name": existing_check.name if existing_check is not None else request.check_name,
        "operation": operation,
        "current_check": (
            existing_check.model_dump(mode="json") if existing_check is not None else None
        ),
        "requested_enabled": request.enabled,
        "requested_require_runtime_identity": request.require_runtime_identity,
        "resolved_url": resolved_url,
        "profile_sha256": profile_sha256,
        "source_label": PRODUCT_HEALTH_MONITORING_SOURCE,
        "reason": request.reason,
    }
    return ProductHealthMonitoringPlan(
        mode=request.mode,
        product=profile.product,
        context=lane.context,
        instance=lane.instance,
        check_name=existing_check.name if existing_check is not None else request.check_name,
        operation=operation,
        current_enabled=existing_check.enabled if existing_check is not None else None,
        requested_enabled=request.enabled,
        current_require_runtime_identity=(
            existing_check.require_runtime_identity if existing_check is not None else None
        ),
        requested_require_runtime_identity=request.require_runtime_identity,
        resolved_url=resolved_url,
        changed=changed,
        reason=request.reason,
        profile_updated_at_before=profile.updated_at,
        profile_sha256_before=profile_sha256,
        plan_sha256=_canonical_sha256(plan_evidence),
    )


def updated_product_health_monitoring_profile(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ProductHealthMonitoringApplyRequest,
    updated_at: str,
) -> LaunchplaneProductProfileRecord:
    lane = _target_lane(profile=profile, request=request)
    existing_check = _matching_check(lane=lane, check_name=request.check_name)
    if existing_check is not None and existing_check.kind != "public_http":
        raise ProductHealthMonitoringCheckKindError(
            "Product health monitoring can mutate only public HTTP checks."
        )
    candidate_check = _candidate_check(existing_check=existing_check, request=request)
    _validate_resolved_url(
        lane=lane,
        check=candidate_check,
        resolved_url=candidate_check.url or lane.health_url or lane.base_url,
    )
    target_token = canonical_health_check_record_token(request.check_name)
    updated_checks: list[ProductLaneHealthCheck] = []
    replaced = False
    for check in lane.health_monitoring.checks:
        if canonical_health_check_record_token(check.name) == target_token:
            updated_checks.append(candidate_check)
            replaced = True
        else:
            updated_checks.append(check)
    if not replaced:
        updated_checks.append(candidate_check)
    updated_lane = lane.model_copy(
        update={
            "health_monitoring": lane.health_monitoring.model_copy(
                update={"checks": tuple(updated_checks)}
            )
        }
    )
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
            "source": PRODUCT_HEALTH_MONITORING_SOURCE,
        }
    )
    return LaunchplaneProductProfileRecord.model_validate(
        updated_profile.model_dump(mode="json")
    ).validate_write_contract()


def _target_lane(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ProductHealthMonitoringApplyRequest,
) -> ProductLaneProfile:
    if profile.product != request.product:
        raise ProductHealthMonitoringTargetError(
            "Product health monitoring target does not match the loaded profile."
        )
    matches = tuple(
        lane
        for lane in profile.lanes
        if lane.context.strip() == request.context and lane.instance.strip() == request.instance
    )
    if not matches:
        raise ProductHealthMonitoringTargetError(
            "Product health monitoring target lane does not exist."
        )
    if len(matches) != 1:
        raise ProductHealthMonitoringTargetError(
            "Product health monitoring target lane is ambiguous."
        )
    return matches[0]


def _matching_check(
    *,
    lane: ProductLaneProfile,
    check_name: str,
) -> ProductLaneHealthCheck | None:
    target_token = canonical_health_check_record_token(check_name)
    matches = tuple(
        check
        for check in lane.health_monitoring.checks
        if canonical_health_check_record_token(check.name) == target_token
    )
    if len(matches) > 1:
        raise ProductHealthMonitoringTargetError(
            "Product health monitoring target check is ambiguous."
        )
    return matches[0] if matches else None


def _candidate_check(
    *,
    existing_check: ProductLaneHealthCheck | None,
    request: ProductHealthMonitoringApplyRequest,
) -> ProductLaneHealthCheck:
    if existing_check is None:
        return ProductLaneHealthCheck(
            name=request.check_name,
            kind="public_http",
            enabled=request.enabled,
            require_runtime_identity=request.require_runtime_identity,
        )
    return existing_check.model_copy(
        update={
            "enabled": request.enabled,
            "require_runtime_identity": request.require_runtime_identity,
        }
    )


def _validate_resolved_url(
    *,
    lane: ProductLaneProfile,
    check: ProductLaneHealthCheck,
    resolved_url: str,
) -> None:
    if not check.enabled:
        return
    parsed_url = urlsplit(resolved_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ProductHealthMonitoringTargetError(
            "Enabled public HTTP health monitoring requires an existing lane-owned URL."
        )
    if not check.require_runtime_identity:
        return
    if parsed_url.scheme != "https":
        raise ProductHealthMonitoringTargetError(
            "Strict public runtime-identity monitoring requires HTTPS."
        )
    lane_hosts = {host for host in (_url_host(lane.base_url), _url_host(lane.health_url)) if host}
    if parsed_url.hostname.lower() not in lane_hosts:
        raise ProductHealthMonitoringTargetError(
            "Strict public runtime-identity monitoring must use a lane-owned host."
        )


def _url_host(value: str) -> str:
    parsed_url = urlsplit(value.strip())
    return parsed_url.hostname.lower() if parsed_url.hostname else ""


def _canonical_sha256(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
