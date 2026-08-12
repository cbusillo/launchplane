from __future__ import annotations

import hashlib
import json
import re
from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.deploy_target import ProviderTargetRecord
from control_plane.contracts.product_profile_record import (
    LaunchplaneProductProfileRecord,
    product_profile_record_sha256,
    validate_product_profile_history_transition,
)


ProductLaneContextRepairMode = Literal["dry-run", "apply"]
PRODUCT_LANE_CONTEXT_REPAIR_SOURCE: Literal["service:product-lane-context-repair"] = (
    "service:product-lane-context-repair"
)


class ProductLaneContextRepairBoundaryError(ValueError):
    pass


class ProductLaneContextRepairReadStore(Protocol):
    def read_product_profile_record(self, product: str) -> LaunchplaneProductProfileRecord: ...

    def list_physical_provider_target_records(self) -> tuple[ProviderTargetRecord, ...]: ...


class ProductLaneContextRepairRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    product: str
    instance: str
    expected_current_context: str
    requested_context: str
    expected_profile_sha256: str = ""
    mode: ProductLaneContextRepairMode = "dry-run"
    reason: str
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> ProductLaneContextRepairRequest:
        self.product = self.product.strip()
        self.instance = self.instance.strip()
        self.expected_current_context = self.expected_current_context.strip()
        self.requested_context = self.requested_context.strip()
        self.expected_profile_sha256 = self.expected_profile_sha256.strip().lower()
        self.reason = self.reason.strip()
        self.reviewed_plan_sha256 = self.reviewed_plan_sha256.strip().lower()
        for field_name in (
            "product",
            "instance",
            "expected_current_context",
            "requested_context",
            "reason",
        ):
            if not getattr(self, field_name):
                raise ValueError(f"Product lane context repair requires {field_name}.")
        if self.expected_current_context == self.requested_context:
            raise ValueError("Product lane context repair requires distinct contexts.")
        if self.expected_profile_sha256 and not re.fullmatch(
            r"[0-9a-f]{64}", self.expected_profile_sha256
        ):
            raise ValueError(
                "Product lane context repair expected_profile_sha256 must be a 64-character "
                "SHA-256."
            )
        if self.mode == "dry-run" and self.reviewed_plan_sha256:
            raise ValueError("Product lane context repair dry-run rejects reviewed_plan_sha256.")
        if self.mode == "apply":
            if not self.expected_profile_sha256:
                raise ValueError(
                    "Product lane context repair apply requires expected_profile_sha256."
                )
            if not re.fullmatch(r"[0-9a-f]{64}", self.reviewed_plan_sha256):
                raise ValueError(
                    "Product lane context repair apply requires a reviewed 64-character plan "
                    "SHA-256."
                )
        return self


class ProductLaneContextRepairTargetEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid")

    context: str
    instance: str
    provider_id: str
    target_category: str
    provider_target_type: str
    target_id: str
    record_sha256: str


class ProductLaneContextRepairPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    mode: ProductLaneContextRepairMode
    product: str
    instance: str
    current_context: str
    requested_context: str
    changed: bool
    applied: bool = False
    same_physical_target: Literal[True] = True
    source_target: ProductLaneContextRepairTargetEvidence
    target_target: ProductLaneContextRepairTargetEvidence
    preserved_historical_contexts: tuple[str, ...]
    preserved_preview_context: str
    reason: str
    source_label: Literal["service:product-lane-context-repair"] = (
        PRODUCT_LANE_CONTEXT_REPAIR_SOURCE
    )
    profile_sha256_before: str
    profile_sha256_after: str = ""
    profile_updated_at_before: str
    profile_updated_at_after: str = ""
    plan_sha256: str


def _record_sha256(record: ProviderTargetRecord) -> str:
    payload = json.dumps(
        record.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _target_evidence(record: ProviderTargetRecord) -> ProductLaneContextRepairTargetEvidence:
    return ProductLaneContextRepairTargetEvidence(
        context=record.context,
        instance=record.instance,
        provider_id=record.provider_id,
        target_category=record.target_category,
        provider_target_type=record.provider_target_type,
        target_id=record.target_id,
        record_sha256=_record_sha256(record),
    )


def _route_target(
    records: tuple[ProviderTargetRecord, ...], *, context: str, instance: str
) -> ProviderTargetRecord:
    matches = tuple(
        record for record in records if record.context == context and record.instance == instance
    )
    if len(matches) != 1:
        raise ProductLaneContextRepairBoundaryError(
            "Product lane context repair requires exactly one provider target for each route."
        )
    return matches[0]


def build_product_lane_context_repair_plan(
    *,
    record_store: ProductLaneContextRepairReadStore,
    request: ProductLaneContextRepairRequest,
) -> tuple[
    ProductLaneContextRepairPlan,
    LaunchplaneProductProfileRecord,
    LaunchplaneProductProfileRecord,
    ProviderTargetRecord,
    ProviderTargetRecord,
]:
    profile = record_store.read_product_profile_record(request.product)
    profile_sha256_before = product_profile_record_sha256(profile)
    if request.expected_profile_sha256 and request.expected_profile_sha256 != profile_sha256_before:
        raise ProductLaneContextRepairBoundaryError(
            "Product lane context repair expected profile digest does not match stored authority."
        )
    if request.requested_context != profile.product:
        raise ProductLaneContextRepairBoundaryError(
            "Product lane context repair requested_context must be the canonical product context."
        )
    if request.expected_current_context not in profile.historical_contexts:
        raise ProductLaneContextRepairBoundaryError(
            "Product lane context repair source context must already be preserved as history."
        )
    if request.requested_context in profile.historical_contexts:
        raise ProductLaneContextRepairBoundaryError(
            "Product lane context repair cannot restore a historical context as current authority."
        )
    matching_lane_indexes = tuple(
        index
        for index, lane in enumerate(profile.lanes)
        if lane.instance == request.instance and lane.context == request.expected_current_context
    )
    if len(matching_lane_indexes) != 1:
        raise ProductLaneContextRepairBoundaryError(
            "Product lane context repair requires exactly one matching current lane."
        )
    provider_targets = record_store.list_physical_provider_target_records()
    source_target = _route_target(
        provider_targets,
        context=request.expected_current_context,
        instance=request.instance,
    )
    target_target = _route_target(
        provider_targets,
        context=request.requested_context,
        instance=request.instance,
    )
    if (
        source_target.provider_id,
        source_target.target_category,
        source_target.target_id,
    ) != (
        target_target.provider_id,
        target_target.target_category,
        target_target.target_id,
    ):
        raise ProductLaneContextRepairBoundaryError(
            "Product lane context repair routes do not resolve to the same physical target."
        )
    allowed_routes = {
        (request.expected_current_context, request.instance),
        (request.requested_context, request.instance),
    }
    conflicting_routes = sorted(
        (record.context, record.instance)
        for record in provider_targets
        if (record.provider_id, record.target_category, record.target_id)
        == (
            target_target.provider_id,
            target_target.target_category,
            target_target.target_id,
        )
        and (record.context, record.instance) not in allowed_routes
    )
    if conflicting_routes:
        raise ProductLaneContextRepairBoundaryError(
            "Product lane context repair physical target is claimed by an unexpected route."
        )
    lane_index = matching_lane_indexes[0]
    updated_lanes = list(profile.lanes)
    updated_lanes[lane_index] = updated_lanes[lane_index].model_copy(
        update={"context": request.requested_context}
    )
    replacement_profile = LaunchplaneProductProfileRecord.model_validate(
        profile.model_copy(
            update={
                "lanes": tuple(updated_lanes),
                "source": PRODUCT_LANE_CONTEXT_REPAIR_SOURCE,
            }
        ).model_dump(mode="json")
    )
    validate_product_profile_history_transition(
        existing_profile=profile,
        replacement_profile=replacement_profile,
    )
    source_evidence = _target_evidence(source_target)
    target_evidence = _target_evidence(target_target)
    plan_evidence = {
        "schema_version": 1,
        "product": request.product,
        "instance": request.instance,
        "current_context": request.expected_current_context,
        "requested_context": request.requested_context,
        "profile_sha256_before": profile_sha256_before,
        "source_target": source_evidence.model_dump(mode="json"),
        "target_target": target_evidence.model_dump(mode="json"),
        "preserved_historical_contexts": list(profile.historical_contexts),
        "preserved_preview_context": profile.preview.context,
        "source_label": PRODUCT_LANE_CONTEXT_REPAIR_SOURCE,
        "reason": request.reason,
    }
    plan_sha256 = hashlib.sha256(
        json.dumps(plan_evidence, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    return (
        ProductLaneContextRepairPlan(
            mode=request.mode,
            product=request.product,
            instance=request.instance,
            current_context=request.expected_current_context,
            requested_context=request.requested_context,
            changed=True,
            source_target=source_evidence,
            target_target=target_evidence,
            preserved_historical_contexts=profile.historical_contexts,
            preserved_preview_context=profile.preview.context,
            reason=request.reason,
            profile_sha256_before=profile_sha256_before,
            profile_updated_at_before=profile.updated_at,
            plan_sha256=plan_sha256,
        ),
        profile,
        replacement_profile,
        source_target,
        target_target,
    )


def updated_product_lane_context_repair_profile(
    *,
    replacement_profile: LaunchplaneProductProfileRecord,
    updated_at: str,
) -> LaunchplaneProductProfileRecord:
    return LaunchplaneProductProfileRecord.model_validate(
        replacement_profile.model_copy(update={"updated_at": updated_at}).model_dump(mode="json")
    )
