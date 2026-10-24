"""Reviewed cleanup of a retained testing generation's stored override intent."""

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.odoo_instance_override_record import (
    OdooInstanceOverrideRecord,
    odoo_override_intent_payload,
)
from control_plane.disposable_lanes import disposable_lane_configuration
from control_plane.odoo_testing_restore import (
    FreshOdooTestingGeneration,
    generation_binding,
    read_current_testing_generation,
)
from control_plane.testing_candidate_runtime import neutralized_candidate_overrides

if TYPE_CHECKING:
    from control_plane.storage.postgres import PostgresRecordStore

ROUTE = "/v1/products/{product}/testing-override-sanitization"
AUDIT_SCOPE = "launchplane:testing-override-sanitization:v1"
AUDIT_ROUTE = "service:testing-override-sanitization"


class TestingOverrideSanitizationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    context: str = Field(min_length=1)
    instance: Literal["testing"] = "testing"
    operation_id: str = Field(min_length=1)
    reason: str = Field(min_length=1)
    mode: Literal["dry-run", "apply", "readback"] = "dry-run"
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")

    def intent(self) -> dict[str, object]:
        return self.model_dump(mode="json", exclude={"mode", "reviewed_plan_sha256"})


class TestingOverrideSanitizationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: Literal["testing"] = "testing"
    operation_id: str
    generation_binding_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    removed_config_parameters: tuple[str, ...]
    removed_addon_settings: tuple[str, ...]
    generation_preserved: Literal[True] = True
    copy_preserved: Literal[True] = True
    applied: bool = False


class TestingOverrideSanitizationAudit(BaseModel):
    """Service-only rollback preimage. Never returned by the HTTP route."""

    model_config = ConfigDict(extra="forbid")

    plan: TestingOverrideSanitizationPlan
    reason: str
    before: OdooInstanceOverrideRecord = Field(repr=False)
    after: OdooInstanceOverrideRecord = Field(repr=False)
    applied_by: str
    applied_at: str


@dataclass(frozen=True)
class TestingOverrideSanitizationSnapshot:
    plan: TestingOverrideSanitizationPlan
    generation: FreshOdooTestingGeneration
    before: OdooInstanceOverrideRecord = field(repr=False)
    after: OdooInstanceOverrideRecord = field(repr=False)


def audit_key(product: str, request: TestingOverrideSanitizationRequest) -> str:
    return canonical_json_sha256(
        {"product": product, "intent": request.intent(), "plan": request.reviewed_plan_sha256}
    )


def read_sanitization_audit(
    store: "PostgresRecordStore", product: str, request: TestingOverrideSanitizationRequest
) -> TestingOverrideSanitizationAudit | None:
    row = store.read_idempotency_record(
        scope=AUDIT_SCOPE, route_path=AUDIT_ROUTE, idempotency_key=audit_key(product, request)
    )
    if row is None:
        return None
    audit = TestingOverrideSanitizationAudit.model_validate(row.response_payload)
    if (
        row.state != "completed"
        or row.request_fingerprint != request.reviewed_plan_sha256
        or audit.plan.plan_sha256 != request.reviewed_plan_sha256
        or (audit.plan.product, audit.plan.context, audit.plan.operation_id, audit.reason)
        != (product, request.context, request.operation_id, request.reason)
    ):
        raise ValueError("The sanitization receipt belongs to another review.")
    return audit


def sanitization_snapshot(
    store: "PostgresRecordStore", product: str, request: TestingOverrideSanitizationRequest
) -> TestingOverrideSanitizationSnapshot:
    generation = read_current_testing_generation(
        store, product=product, context=request.context, instance="testing"
    )
    if (
        generation is None
        or generation.operation_id != request.operation_id
        or generation.completed_at
        or generation.non_use_invalidated
    ):
        raise ValueError("Sanitization requires the current unfinished, unused testing generation.")
    profile = store.read_product_profile_record(product)
    lane = next(
        (
            lane
            for lane in profile.lanes
            if (lane.context, lane.instance) == (request.context, "testing")
        ),
        None,
    )
    if lane is None:
        raise ValueError("The current product no longer declares this testing lane.")
    if urlsplit(lane.base_url).hostname != generation.domain_host:
        raise ValueError("Testing public origin differs from its retained generation.")
    target = store.read_dokploy_target_record(context_name=request.context, instance_name="testing")
    target_id = store.read_dokploy_target_id_record(
        context_name=request.context, instance_name="testing"
    )
    if target.target_type != "compose" or target.domains != (generation.domain_host,):
        raise ValueError("Testing target differs from its retained generation.")
    before = store.read_odoo_instance_override_record(
        context_name=request.context, instance_name="testing"
    )
    after = neutralized_candidate_overrides(
        before,
        context=request.context,
        base_url=lane.base_url.rstrip("/"),
        updated_at=before.updated_at,
    ).model_copy(update={"source_label": "service:testing-override-sanitization"})
    digest = canonical_json_sha256(
        {
            "product": product,
            "request": request.intent(),
            "generation": generation.model_dump(mode="json"),
            "profile": profile.model_dump(mode="json"),
            "target": target.model_dump(mode="json"),
            "target_id": target_id.model_dump(mode="json"),
            "configuration": [
                item.model_dump(mode="json")
                for item in disposable_lane_configuration(store, request.context, "testing")
            ],
            "before": before.model_dump(mode="json"),
            "after": after.model_dump(mode="json"),
        }
    )
    kept_parameters = {item.key for item in after.config_parameters}
    kept_addons = {(item.addon, item.setting) for item in after.addon_settings}
    plan = TestingOverrideSanitizationPlan(
        product=product,
        context=request.context,
        operation_id=request.operation_id,
        generation_binding_sha256=generation_binding(generation),
        plan_sha256=digest,
        removed_config_parameters=tuple(
            sorted(item.key for item in before.config_parameters if item.key not in kept_parameters)
        ),
        removed_addon_settings=tuple(
            sorted(
                f"{item.addon}.{item.setting}"
                for item in before.addon_settings
                if (item.addon, item.setting) not in kept_addons
            )
        ),
    )
    return TestingOverrideSanitizationSnapshot(plan, generation, before, after)


def sanitization_readback(
    store: "PostgresRecordStore", product: str, request: TestingOverrideSanitizationRequest
) -> TestingOverrideSanitizationPlan:
    audit = read_sanitization_audit(store, product, request)
    if audit is None:
        raise ValueError("No sanitization receipt exists for this review.")
    generation = store.read_fresh_odoo_testing_generation(product, request.context)
    current = store.read_odoo_instance_override_record(
        context_name=request.context, instance_name="testing"
    )
    if (
        generation is None
        or generation_binding(generation) != audit.plan.generation_binding_sha256
        or canonical_json_sha256(odoo_override_intent_payload(current))
        != canonical_json_sha256(odoo_override_intent_payload(audit.after))
    ):
        raise ValueError("The current generation or override intent differs from the receipt.")
    return audit.plan.model_copy(update={"applied": True})
