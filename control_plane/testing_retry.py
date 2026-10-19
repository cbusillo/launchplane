"""Reviewed recovery of an exhausted Odoo testing artifact through its reconciler."""

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from control_plane.contracts.product_reconcile import ProductReconcileRequestRecord
from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.storage.postgres import PostgresRecordStore
from control_plane.contracts.testing_retry import TESTING_RETRY_ROUTE as TESTING_RETRY_ROUTE


class TestingRetryRefused(ValueError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class TestingRetryRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    mode: Literal["dry-run", "apply"] = "dry-run"
    instance: Literal["testing"] = "testing"
    artifact_id: str = Field(min_length=1)
    source_git_ref: str = Field(pattern=r"^[a-f0-9]{40}$")
    reason: str = Field(min_length=1, max_length=1000)
    reviewed_plan_sha256: str = Field(default="", pattern=r"^(?:[a-f0-9]{64})?$")

    @model_validator(mode="after")
    def validate_review(self) -> "TestingRetryRequest":
        self.artifact_id = self.artifact_id.strip()
        self.reason = self.reason.strip()
        if not self.artifact_id or not self.reason:
            raise ValueError("An exact artifact and concrete reason are required.")
        if (self.mode == "apply") != bool(self.reviewed_plan_sha256):
            raise ValueError("Apply requires the reviewed plan digest; dry run supplies none.")
        return self


@dataclass(frozen=True)
class TestingRetryPlan:
    request: ProductReconcileRequestRecord
    context: str
    after_operation_id: str
    lane_binding_sha256: str
    plan_sha256: str


def testing_retry_binding(store: PostgresRecordStore, product: str, context: str) -> str:
    """Bind both independent artifact authorities, including absence, and lane ownership."""
    return store.read_testing_retry_binding(product=product, context=context)


def build_testing_retry_plan(
    store: PostgresRecordStore, product: str, request: TestingRetryRequest
) -> TestingRetryPlan:
    from control_plane.odoo_target_replacement_apply_http import (
        resolve_odoo_target_replacement_apply_lane,
    )
    from control_plane.product_reconcile import (
        TESTING_DEPLOY_MAX_FAILED_ATTEMPTS,
        latest_verified_testing_build,
        testing_artifact_attempt,
    )
    from control_plane.odoo_testing_restore import read_current_testing_generation
    from control_plane.testing_lane_hold import read_staff_testing_hold
    from control_plane.testing_restore_prerequisite import incomplete_testing_restore

    profile = store.read_product_profile_record(product)
    lane = resolve_odoo_target_replacement_apply_lane(
        record_store=store, product=product, instance="testing"
    )
    if read_staff_testing_hold(record_store=store, context=lane.context) is not None:
        raise TestingRetryRefused("testing_retry_held")
    generation = read_current_testing_generation(
        store, product=product, context=lane.context, instance="testing", bootstrap_preparation=True
    )
    if generation is not None and not generation.completed_at:
        raise TestingRetryRefused("testing_retry_restore_incomplete")
    if (
        incomplete_testing_restore(
            store, context=lane.context, instance="testing", generation=generation
        )
        is not None
    ):
        raise TestingRetryRefused("testing_retry_restore_incomplete")
    binding = testing_retry_binding(store, product, lane.context)
    recorded = store.read_product_reconcile_request(f"{product}:testing")
    if recorded.state == "running":
        raise TestingRetryRefused("testing_retry_busy")
    verified = latest_verified_testing_build(store, profile, lane)
    if (verified.manifest.artifact_id, verified.manifest.source_commit) != (
        request.artifact_id,
        request.source_git_ref,
    ) or (
        recorded.last_plan.get("desired_artifact_id"),
        recorded.last_plan.get("desired_commit"),
    ) != (request.artifact_id, request.source_git_ref):
        raise TestingRetryRefused("testing_retry_desired_artifact_changed")
    attempt = testing_artifact_attempt(
        record_store=store,
        profile=profile,
        lane=lane,
        artifact_id=request.artifact_id,
        generation_prepared_at=generation.prepared_at if generation else "",
    )
    if attempt.active is not None or attempt.failed_attempts < TESTING_DEPLOY_MAX_FAILED_ATTEMPTS:
        raise TestingRetryRefused("testing_retry_not_exhausted")
    if binding != testing_retry_binding(store, product, lane.context):
        raise TestingRetryRefused("testing_retry_stale")
    return TestingRetryPlan(
        request=recorded,
        context=lane.context,
        after_operation_id=attempt.last_failed_operation_id,
        lane_binding_sha256=binding,
        plan_sha256=canonical_json_sha256(
            {
                "product": product,
                "artifact_id": request.artifact_id,
                "source_git_ref": request.source_git_ref,
                "reason": request.reason,
                "after_operation_id": attempt.last_failed_operation_id,
                "lane_binding_sha256": binding,
            }
        ),
    )
