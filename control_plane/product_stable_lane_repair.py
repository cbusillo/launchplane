from __future__ import annotations

import hashlib
import json
import re
from typing import Literal, Protocol
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.dokploy_target_id_record import DokployTargetIdRecord
from control_plane.contracts.dokploy_target_record import DokployTargetRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    ProductLaneProfile,
    product_profile_record_sha256,
    validate_product_profile_history_transition,
)
from control_plane.workflows.generic_web_deploy import product_profile_uses_generic_web_base


ProductStableLaneRepairMode = Literal["dry-run", "apply"]
PRODUCT_STABLE_LANE_REPAIR_SOURCE: Literal["service:product-stable-lane-repair"] = (
    "service:product-stable-lane-repair"
)


class ProductStableLaneRepairBoundaryError(ValueError):
    pass


class ProductStableLaneRepairReadStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def read_provider_target_record(
        self, *, context_name: str, instance_name: str
    ) -> ProviderTargetRecord: ...

    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...

    def read_dokploy_target_id_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetIdRecord: ...


class ProductStableLaneRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    product: str
    context: str
    instance: str
    base_url: str
    mode: ProductStableLaneRepairMode = "dry-run"
    reason: str
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> ProductStableLaneRepairRequest:
        self.product = self.product.strip()
        self.context = self.context.strip()
        self.instance = self.instance.strip()
        self.base_url = self.base_url.strip().rstrip("/")
        self.reason = self.reason.strip()
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        for field_name in ("product", "context", "instance", "base_url", "reason"):
            if not getattr(self, field_name):
                raise ValueError(f"Product stable lane repair requires {field_name}.")
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Product stable lane repair dry-run rejects reviewed_plan_sha256.")
        if self.mode == "apply" and not re.fullmatch(r"[0-9a-f]{64}", self.reviewed_plan_sha256):
            raise ValueError(
                "Product stable lane repair apply requires a reviewed 64-character plan SHA-256."
            )
        return self


class ProductStableLaneRepairTargetEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    provider_id: str
    target_category: str
    provider_target_type: str
    target_id: str
    display_name: str
    domains: tuple[str, ...]
    healthcheck_path: str
    provider_target_sha256: str
    dokploy_target_sha256: str
    dokploy_target_id_sha256: str


class ProductStableLaneRepairPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    mode: ProductStableLaneRepairMode
    product: str
    context: str
    instance: str
    base_url: str
    health_url: str
    changed: Literal[True] = True
    applied: bool = False
    target: ProductStableLaneRepairTargetEvidence
    preserved_lane_instances: tuple[str, ...]
    reason: str
    source_label: Literal["service:product-stable-lane-repair"] = PRODUCT_STABLE_LANE_REPAIR_SOURCE
    profile_sha256_before: str
    profile_sha256_after: str = ""
    profile_updated_at_before: str
    profile_updated_at_after: str = ""
    plan_sha256: str


def canonical_record_sha256(record: BaseModel) -> str:
    payload = json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _normalized_target_domain(raw_domain: str) -> str:
    value = raw_domain.strip()
    parsed = urlsplit(value if "://" in value else f"//{value}")
    hostname = (parsed.hostname or "").strip().rstrip(".").lower()
    if (
        not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or any(character.isspace() for character in hostname)
        or "." not in hostname
    ):
        raise ProductStableLaneRepairBoundaryError(
            "Tracked target domains must resolve to bare DNS hostnames."
        )
    return hostname


def _normalized_domains(raw_domains: tuple[str, ...]) -> tuple[str, ...]:
    domains: list[str] = []
    for raw_domain in raw_domains:
        domain = _normalized_target_domain(raw_domain)
        if domain not in domains:
            domains.append(domain)
    if not domains:
        raise ProductStableLaneRepairBoundaryError(
            "Product stable lane repair requires tracked target domains."
        )
    return tuple(domains)


def _validated_base_url(base_url: str, *, domains: tuple[str, ...]) -> str:
    parsed = urlsplit(base_url)
    hostname = (parsed.hostname or "").strip().rstrip(".").lower()
    if (
        parsed.scheme != "https"
        or not hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port is not None
        or parsed.path not in {"", "/"}
        or parsed.query
        or parsed.fragment
    ):
        raise ProductStableLaneRepairBoundaryError(
            "Product stable lane repair base_url must be an HTTPS origin without a path, "
            "port, query, fragment, or credentials."
        )
    if hostname not in domains:
        raise ProductStableLaneRepairBoundaryError(
            "Product stable lane repair base_url host is not owned by the tracked target."
        )
    return f"https://{hostname}"


def _health_url(
    *, base_url: str, target: DokployTargetRecord, profile: LaunchplaneProductProfileRecord
) -> str:
    health_path = target.healthcheck_path.strip() or profile.health_path.strip()
    if not health_path:
        return ""
    if not health_path.startswith("/"):
        raise ProductStableLaneRepairBoundaryError(
            "Tracked target healthcheck_path must start with /."
        )
    return f"{base_url}{health_path}"


def build_product_stable_lane_repair_plan(
    *,
    record_store: ProductStableLaneRepairReadStore,
    request: ProductStableLaneRepairRequest,
) -> tuple[
    ProductStableLaneRepairPlan,
    LaunchplaneProductProfileRecord,
    LaunchplaneProductProfileRecord,
    ProviderTargetRecord,
    DokployTargetRecord,
    DokployTargetIdRecord,
]:
    profile = record_store.read_product_profile_record(request.product)
    if profile.lifecycle_state != "active":
        raise ProductStableLaneRepairBoundaryError(
            "Product stable lane repair requires an active product profile."
        )
    if not product_profile_uses_generic_web_base(profile):
        raise ProductStableLaneRepairBoundaryError(
            "Product stable lane repair requires a generic-web based product profile."
        )
    if any(lane.instance == request.instance for lane in profile.lanes):
        raise ProductStableLaneRepairBoundaryError(
            "Product stable lane repair requires the requested lane instance to be absent."
        )
    if request.context in profile.historical_contexts:
        raise ProductStableLaneRepairBoundaryError(
            "Product stable lane repair cannot reactivate a historical context."
        )
    if not any(lane.context == request.context for lane in profile.lanes):
        raise ProductStableLaneRepairBoundaryError(
            "Product stable lane repair requires the requested context to be owned by an "
            "existing stable lane."
        )

    provider_target = record_store.read_provider_target_record(
        context_name=request.context,
        instance_name=request.instance,
    )
    dokploy_target = record_store.read_dokploy_target_record(
        context_name=request.context,
        instance_name=request.instance,
    )
    dokploy_target_id = record_store.read_dokploy_target_id_record(
        context_name=request.context,
        instance_name=request.instance,
    )
    expected_provider_target = ProviderTargetRecord.from_dokploy_records(
        target_record=dokploy_target,
        target_id_record=dokploy_target_id,
    )
    if provider_target != expected_provider_target:
        raise ProductStableLaneRepairBoundaryError(
            "Tracked provider-target authority does not match the Dokploy target records."
        )
    if provider_target.provider_id != "dokploy":
        raise ProductStableLaneRepairBoundaryError(
            "Product stable lane repair currently requires a tracked Dokploy target."
        )

    domains = _normalized_domains(dokploy_target.domains)
    base_url = _validated_base_url(request.base_url, domains=domains)
    health_url = _health_url(base_url=base_url, target=dokploy_target, profile=profile)
    replacement_profile = LaunchplaneProductProfileRecord.model_validate(
        profile.model_copy(
            update={
                "lanes": (
                    *profile.lanes,
                    ProductLaneProfile(
                        instance=request.instance,
                        context=request.context,
                        base_url=base_url,
                        health_url=health_url,
                    ),
                ),
                "source": PRODUCT_STABLE_LANE_REPAIR_SOURCE,
            }
        ).model_dump(mode="json")
    )
    validate_product_profile_history_transition(
        existing_profile=profile,
        replacement_profile=replacement_profile,
    )
    target_evidence = ProductStableLaneRepairTargetEvidence(
        context=provider_target.context,
        instance=provider_target.instance,
        provider_id=provider_target.provider_id,
        target_category=provider_target.target_category,
        provider_target_type=provider_target.provider_target_type,
        target_id=provider_target.target_id,
        display_name=provider_target.display_name,
        domains=domains,
        healthcheck_path=dokploy_target.healthcheck_path,
        provider_target_sha256=canonical_record_sha256(provider_target),
        dokploy_target_sha256=canonical_record_sha256(dokploy_target),
        dokploy_target_id_sha256=canonical_record_sha256(dokploy_target_id),
    )
    profile_sha256 = product_profile_record_sha256(profile)
    plan_evidence = {
        "schema_version": 1,
        "product": request.product,
        "context": request.context,
        "instance": request.instance,
        "base_url": base_url,
        "health_url": health_url,
        "target": target_evidence.model_dump(mode="json"),
        "profile_sha256_before": profile_sha256,
        "preserved_lane_instances": [lane.instance for lane in profile.lanes],
        "reason": request.reason,
        "source_label": PRODUCT_STABLE_LANE_REPAIR_SOURCE,
    }
    plan_sha256 = hashlib.sha256(
        json.dumps(plan_evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return (
        ProductStableLaneRepairPlan(
            mode=request.mode,
            product=request.product,
            context=request.context,
            instance=request.instance,
            base_url=base_url,
            health_url=health_url,
            target=target_evidence,
            preserved_lane_instances=tuple(lane.instance for lane in profile.lanes),
            reason=request.reason,
            profile_sha256_before=profile_sha256,
            profile_updated_at_before=profile.updated_at,
            plan_sha256=plan_sha256,
        ),
        profile,
        replacement_profile,
        provider_target,
        dokploy_target,
        dokploy_target_id,
    )


def updated_product_stable_lane_repair_profile(
    *,
    replacement_profile: LaunchplaneProductProfileRecord,
    updated_at: str,
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord.model_validate(
        replacement_profile.model_copy(update={"updated_at": updated_at}).model_dump(mode="json")
    ).validate_write_contract()
