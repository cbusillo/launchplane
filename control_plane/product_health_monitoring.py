from __future__ import annotations

import re
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.canonical_json import nonvalidating_compact_sorted_json_sha256
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.private_health_endpoint_record import PrivateHealthEndpointRecord
from control_plane.contracts.product_health_monitoring_migration import (
    canonical_health_check_record_token,
    health_check_record_token,
)
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneHealthCheck,
    ProductLaneHealthCheckKind,
    ProductLaneHealthMonitoringPolicy,
    ProductLaneMonitoringIntent,
    ProductLaneProfile,
)
from control_plane.contracts.public_dns import PublicDnsExpectedTarget
from control_plane.contracts.runtime_dependency_health import RuntimeDependencyHealthPolicy
from control_plane.outbound_http import public_url_error
from control_plane.contracts.public_hosts import normalize_public_hosts

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
    check_kind: Literal["public_http", "private_http"] = "public_http"
    monitoring_intent: ProductLaneMonitoringIntent
    enabled: bool = True
    require_runtime_identity: bool = False
    private_endpoint_key: str = ""
    public_url: str | None = None
    expected_dns_targets: tuple[PublicDnsExpectedTarget, ...] | None = Field(
        default=None, max_length=16
    )
    mode: ProductHealthMonitoringMode = "dry-run"
    reason: str
    reviewed_plan_sha256: str = ""
    dependency_health: RuntimeDependencyHealthPolicy | None = None

    @model_validator(mode="after")
    def _validate_request(self) -> "ProductHealthMonitoringApplyRequest":
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.check_name = self.check_name.strip()
        self.private_endpoint_key = self.private_endpoint_key.strip()
        self.reason = self.reason.strip()
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        if self.public_url is not None:
            public_url = self.public_url.strip()
            self.public_url = public_url
            parsed = urlsplit(public_url)
            if (
                self.check_kind != "public_http"
                or public_url_error(public_url)
                or parsed.scheme != "https"
                or parsed.port not in {None, 443}
                or parsed.fragment
            ):
                raise ValueError(
                    "Public endpoint selection requires a public HTTPS URL on port 443."
                )
            normalize_public_hosts([parsed.hostname or ""])
        if self.expected_dns_targets is not None and self.check_kind != "public_http":
            raise ValueError("DNS expectations are only supported on public HTTP checks")
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
        if self.check_kind == "public_http" and self.private_endpoint_key:
            raise ValueError(
                "Public HTTP health monitoring requests cannot set private_endpoint_key."
            )
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
    current_check_kind: ProductLaneHealthCheckKind | None = None
    requested_check_kind: Literal["public_http", "private_http"]
    current_monitoring_intent: ProductLaneMonitoringIntent
    requested_monitoring_intent: ProductLaneMonitoringIntent
    current_dependency_health: RuntimeDependencyHealthPolicy = Field(
        default_factory=RuntimeDependencyHealthPolicy,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    requested_dependency_health: RuntimeDependencyHealthPolicy = Field(
        default_factory=RuntimeDependencyHealthPolicy,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    operation: ProductHealthMonitoringOperation
    current_enabled: bool | None = None
    requested_enabled: bool
    current_require_runtime_identity: bool | None = None
    requested_require_runtime_identity: bool
    current_private_endpoint_key: str | None = None
    private_endpoint_key: str = ""
    resolved_url: str
    current_resolved_url: str | None = Field(
        default=None,
        exclude_if=lambda value: value is None,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    current_expected_dns_targets: tuple[PublicDnsExpectedTarget, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    requested_expected_dns_targets: tuple[PublicDnsExpectedTarget, ...] = Field(
        default=(),
        exclude_if=lambda value: not value,
        json_schema_extra={"x-launchplane-optional-response": True},
    )
    changed: bool
    applied: bool = False
    reason: str
    source_label: Literal["service:product-health-monitoring"] = PRODUCT_HEALTH_MONITORING_SOURCE
    profile_updated_at_before: str
    profile_updated_at_after: str = ""
    profile_sha256_before: str
    plan_sha256: str


class ProductHealthMonitoringResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["accepted"] = "accepted"
    trace_id: str
    records: dict[str, object]
    result: ProductHealthMonitoringPlan | None = None
    replayed: bool | None = None
    original_trace_id: str | None = None


def product_health_monitoring_authority(
    profile: LaunchplaneProductProfileRecord,
) -> dict[tuple[str, str], dict[str, object]]:
    return {
        (lane.context, lane.instance): lane.health_monitoring.model_dump(mode="json")
        for lane in profile.lanes
    }


def validate_product_health_monitoring_private_endpoint(
    *,
    request: ProductHealthMonitoringApplyRequest,
    endpoint: PrivateHealthEndpointRecord,
) -> None:
    if request.check_kind != "private_http" or not request.enabled:
        return
    if endpoint.endpoint_key != request.private_endpoint_key:
        raise ProductHealthMonitoringTargetError(
            "Private health endpoint does not match the requested endpoint key."
        )
    if endpoint.status != "active":
        raise ProductHealthMonitoringTargetError("Private health endpoint is not active.")
    if endpoint.product != request.product:
        raise ProductHealthMonitoringTargetError(
            "Private health endpoint belongs to another product."
        )
    if endpoint.context != request.context or endpoint.instance != request.instance:
        raise ProductHealthMonitoringTargetError("Private health endpoint belongs to another lane.")


def build_product_health_monitoring_plan(
    *,
    profile: LaunchplaneProductProfileRecord,
    request: ProductHealthMonitoringApplyRequest,
    public_target: DokployTargetRecord | None = None,
) -> ProductHealthMonitoringPlan:
    lane = _target_lane(profile=profile, request=request)
    existing_check = _matching_check(lane=lane, check_name=request.check_name)
    if existing_check is not None and existing_check.kind != request.check_kind:
        raise ProductHealthMonitoringCheckKindError(
            "Product health monitoring cannot change the kind of an existing check name."
        )
    candidate_check = _candidate_check(existing_check=existing_check, request=request)
    candidate_policy = _candidate_policy(
        lane=lane,
        candidate_check=candidate_check,
        monitoring_intent=request.monitoring_intent,
        dependency_health=request.dependency_health,
    )
    resolved_url = (
        candidate_check.url or lane.health_url or lane.base_url
        if candidate_check.kind == "public_http"
        else ""
    )
    _validate_resolved_target(
        lane=lane,
        check=candidate_check,
        resolved_url=resolved_url,
        validate_dns_scope=request.expected_dns_targets is not None,
        validate_public_scope=request.public_url is not None,
        public_target=public_target,
    )
    # Dry-run must reject the same full-profile conflicts as apply.
    updated_product_health_monitoring_profile(
        profile=profile,
        request=request,
        updated_at=profile.updated_at,
        public_target=public_target,
    )
    changed = (
        existing_check is None
        or existing_check.model_dump(mode="json") != candidate_check.model_dump(mode="json")
        or lane.health_monitoring.monitoring_intent != candidate_policy.monitoring_intent
        or lane.health_monitoring.dependency_health != candidate_policy.dependency_health
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
        "current_monitoring_intent": lane.health_monitoring.monitoring_intent,
        "requested_monitoring_intent": candidate_policy.monitoring_intent,
        "requested_dependency_health": candidate_policy.dependency_health.model_dump(mode="json"),
        "current_check": (
            existing_check.model_dump(mode="json") if existing_check is not None else None
        ),
        "requested_check": candidate_check.model_dump(mode="json"),
        "resolved_url": resolved_url,
        "profile_sha256": profile_sha256,
        "source_label": PRODUCT_HEALTH_MONITORING_SOURCE,
        "reason": request.reason,
    }
    if public_target is not None:
        plan_evidence["public_target_sha256"] = _canonical_sha256(
            public_target.model_dump(mode="json")
        )
    return ProductHealthMonitoringPlan(
        mode=request.mode,
        product=profile.product,
        context=lane.context,
        instance=lane.instance,
        check_name=existing_check.name if existing_check is not None else request.check_name,
        current_check_kind=existing_check.kind if existing_check is not None else None,
        requested_check_kind=request.check_kind,
        current_monitoring_intent=lane.health_monitoring.monitoring_intent,
        requested_monitoring_intent=candidate_policy.monitoring_intent,
        current_dependency_health=lane.health_monitoring.dependency_health,
        requested_dependency_health=candidate_policy.dependency_health,
        operation=operation,
        current_enabled=existing_check.enabled if existing_check is not None else None,
        requested_enabled=request.enabled,
        current_require_runtime_identity=(
            existing_check.require_runtime_identity if existing_check is not None else None
        ),
        requested_require_runtime_identity=request.require_runtime_identity,
        current_private_endpoint_key=(
            existing_check.private_endpoint_key if existing_check else ""
        ),
        private_endpoint_key=candidate_check.private_endpoint_key,
        resolved_url=resolved_url,
        current_resolved_url=(
            (existing_check.url if existing_check else "") or lane.health_url or lane.base_url
        )
        if request.public_url is not None
        else None,
        current_expected_dns_targets=(
            existing_check.expected_dns_targets if existing_check else ()
        ),
        requested_expected_dns_targets=candidate_check.expected_dns_targets,
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
    public_target: DokployTargetRecord | None = None,
) -> LaunchplaneProductProfileRecord:
    lane = _target_lane(profile=profile, request=request)
    existing_check = _matching_check(lane=lane, check_name=request.check_name)
    if existing_check is not None and existing_check.kind != request.check_kind:
        raise ProductHealthMonitoringCheckKindError(
            "Product health monitoring cannot change the kind of an existing check name."
        )
    candidate_check = _candidate_check(existing_check=existing_check, request=request)
    candidate_policy = _candidate_policy(
        lane=lane,
        candidate_check=candidate_check,
        monitoring_intent=request.monitoring_intent,
        dependency_health=request.dependency_health,
    )
    resolved_url = (
        candidate_check.url or lane.health_url or lane.base_url
        if candidate_check.kind == "public_http"
        else ""
    )
    _validate_resolved_target(
        lane=lane,
        check=candidate_check,
        resolved_url=resolved_url,
        validate_dns_scope=request.expected_dns_targets is not None,
        validate_public_scope=request.public_url is not None,
        public_target=public_target,
    )
    updated_lane = lane.model_copy(update={"health_monitoring": candidate_policy})
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
        candidate = ProductLaneHealthCheck(
            name=request.check_name,
            kind=request.check_kind,
            enabled=request.enabled,
            private_endpoint_key=request.private_endpoint_key,
            require_runtime_identity=request.require_runtime_identity,
            url=request.public_url or "",
            expected_dns_targets=request.expected_dns_targets or (),
        )
    else:
        candidate = existing_check.model_copy(
            update={
                "enabled": request.enabled,
                "url": request.public_url if request.public_url is not None else existing_check.url,
                "private_endpoint_key": (
                    request.private_endpoint_key or existing_check.private_endpoint_key
                    if request.check_kind == "private_http"
                    else ""
                ),
                "require_runtime_identity": request.require_runtime_identity,
                "expected_dns_targets": (
                    request.expected_dns_targets
                    if request.expected_dns_targets is not None
                    else existing_check.expected_dns_targets
                ),
            }
        )
    if (
        candidate.enabled
        and candidate.kind == "private_http"
        and not candidate.private_endpoint_key
    ):
        raise ProductHealthMonitoringTargetError(
            "Enabled private HTTP health monitoring requires private_endpoint_key."
        )
    return ProductLaneHealthCheck.model_validate(candidate.model_dump(mode="json"))


def _candidate_policy(
    *,
    lane: ProductLaneProfile,
    candidate_check: ProductLaneHealthCheck,
    monitoring_intent: ProductLaneMonitoringIntent,
    dependency_health: RuntimeDependencyHealthPolicy | None = None,
) -> ProductLaneHealthMonitoringPolicy:
    target_token = canonical_health_check_record_token(candidate_check.name)
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
    return ProductLaneHealthMonitoringPolicy(
        monitoring_intent=monitoring_intent,
        checks=tuple(updated_checks),
        dependency_health=dependency_health or lane.health_monitoring.dependency_health,
    )


def _validate_resolved_target(
    *,
    lane: ProductLaneProfile,
    check: ProductLaneHealthCheck,
    resolved_url: str,
    validate_dns_scope: bool,
    validate_public_scope: bool,
    public_target: DokployTargetRecord | None,
) -> None:
    if validate_public_scope:
        _validate_public_host_authority(lane, resolved_url, public_target)
    if not check.enabled:
        return
    if check.kind == "private_http":
        if not check.private_endpoint_key:
            raise ProductHealthMonitoringTargetError(
                "Enabled private HTTP health monitoring requires private_endpoint_key."
            )
        return
    parsed_url = urlsplit(resolved_url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.hostname:
        raise ProductHealthMonitoringTargetError(
            "Enabled public HTTP health monitoring requires an existing lane-owned URL."
        )
    target_hosts = {_url_host(lane.base_url), _url_host(resolved_url)}
    if validate_dns_scope and any(
        target.public_name not in target_hosts for target in check.expected_dns_targets
    ):
        raise ProductHealthMonitoringTargetError(
            "DNS expectations must name a host probed by this public HTTP check."
        )
    if not check.require_runtime_identity:
        return
    if parsed_url.scheme != "https":
        raise ProductHealthMonitoringTargetError(
            "Strict public runtime-identity monitoring requires HTTPS."
        )
    _validate_public_host_authority(lane, resolved_url, public_target)


def requires_product_health_monitoring_public_target(
    profile: LaunchplaneProductProfileRecord, request: ProductHealthMonitoringApplyRequest
) -> bool:
    if request.check_kind != "public_http":
        return False
    lane = _target_lane(profile=profile, request=request)
    existing = _matching_check(lane=lane, check_name=request.check_name)
    url = (
        request.public_url if request.public_url is not None else (existing.url if existing else "")
    )
    return bool(
        url
        and (request.public_url is not None or request.require_runtime_identity)
        and _url_host(url) not in {_url_host(lane.base_url), _url_host(lane.health_url)}
    )


def _validate_public_host_authority(
    lane: ProductLaneProfile, url: str, public_target: DokployTargetRecord | None
) -> None:
    host = _url_host(url)
    if host in {value for value in (_url_host(lane.base_url), _url_host(lane.health_url)) if value}:
        return
    if (
        public_target is None
        or (public_target.context, public_target.instance) != (lane.context, lane.instance)
        or lane.instance != "prod"
        or host not in public_target.public_hosts
        or public_target.pending_public_host_effects
    ):
        raise ProductHealthMonitoringTargetError(
            "Public HTTP monitoring must use a lane-owned host."
        )


def _url_host(value: str) -> str:
    parsed_url = urlsplit(value.strip())
    return parsed_url.hostname.lower() if parsed_url.hostname else ""


def _canonical_sha256(value: object) -> str:
    return nonvalidating_compact_sorted_json_sha256(value)
