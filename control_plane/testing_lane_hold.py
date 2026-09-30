"""Supported, audited reads and writes of a testing lane's staff-testing hold.

While site staff test on a product's testing lane, the site operator holds it:
the event-driven reconcile records its testing deploy as held and does not
deploy, and the worker cancels a reconcile deploy it queued just before the
hold. The hold lives on the lane's tracked Dokploy target record. Writes follow
the integration-allowances shape: dry-run returns the change and a digest, and
apply requires that digest, compare-and-writes, then reads the record back.
Lifting the hold requests a reconcile of the product's testing target, so the
newest verified build deploys without waiting for the sweep.
"""

from __future__ import annotations

from typing import Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, model_validator

from control_plane.contracts.durable_operation_authorization import (
    LAUNCHPLANE_RECONCILE_SUBJECT,
    DurableOperationCallerIdentity,
    DurableOperationCancellation,
)
from control_plane.contracts.dokploy_target_record import (
    DokployTargetRecord,
    DokployTargetRecordChanged,
    DokployTargetStaffTestingHold,
)
from control_plane.contracts.odoo_stable_target_replacement_operation import (
    OdooStableTargetReplacementOperationRecord,
)
from control_plane.contracts.product_reconcile import (
    ProductReconcileRequestRecord,
    ProductReconcileTarget,
)
from control_plane.integration_allowances import (
    canonical_sha256,
    normalize_reviewed_lane_request,
    target_record_sha256,
    utc_now_timestamp,
)

TESTING_HOLD_ROUTE = "/v1/product-config/testing-hold"
TESTING_HOLD_APPLY_ROUTE = "/v1/product-config/testing-hold/apply"
TESTING_HOLD_SOURCE_LABEL = "service:testing-hold"
TESTING_HOLD_INSTANCE = "testing"
STAFF_TESTING_HOLD_REASON = "staff_testing"
# The cancellation reason the worker records on a reconcile deploy it did not run.
STAFF_TESTING_HOLD_CANCELLATION_REASON = (
    "staff_testing_hold: the testing lane is held for staff testing; "
    "the next reconcile after the hold is lifted deploys the newest verified build."
)

TestingHoldMode = Literal["dry-run", "apply"]
TestingHoldRefusalCode = Literal["target_record_missing", "not_testing_lane"]
TestingHoldAction = Literal["set", "update", "clear", "unchanged"]


class TestingHoldRefusal(ValueError):
    def __init__(self, code: TestingHoldRefusalCode, message: str) -> None:
        super().__init__(message)
        self.code = code


class TestingHoldStale(ValueError):
    pass


class TestingHoldReader(Protocol):
    def read_dokploy_target_record(
        self, *, context_name: str, instance_name: str
    ) -> DokployTargetRecord: ...


class TestingHoldStore(TestingHoldReader, Protocol):
    def compare_and_write_dokploy_target_record(
        self,
        *,
        expected_record: DokployTargetRecord,
        replacement_record: DokployTargetRecord,
    ) -> DokployTargetRecord: ...


@runtime_checkable
class ProductReconcileRequester(Protocol):
    """A store with the reconcile queue; the file-backed store has none, and no reconciler."""

    def request_product_reconcile(
        self, target: ProductReconcileTarget, requested_at: str
    ) -> ProductReconcileRequestRecord: ...


def read_staff_testing_hold(
    *, record_store: TestingHoldReader, context: str
) -> DokployTargetStaffTestingHold | None:
    """The testing lane's hold; a lane without a tracked target record has none."""
    try:
        target = record_store.read_dokploy_target_record(
            context_name=context.strip().lower(), instance_name=TESTING_HOLD_INSTANCE
        )
    except FileNotFoundError:
        return None
    return target.policies.staff_testing_hold


def staff_testing_hold_cancellation(cancelled_at: str) -> DurableOperationCancellation:
    """How the worker records a reconcile deploy it did not run because of a hold."""
    return DurableOperationCancellation(
        reason=STAFF_TESTING_HOLD_CANCELLATION_REASON,
        cancelled_at=cancelled_at,
        caller=DurableOperationCallerIdentity(
            identity_type="launchplane_reconcile", subject=LAUNCHPLANE_RECONCILE_SUBJECT
        ),
    )


def is_staff_testing_hold_cancellation(
    operation: OdooStableTargetReplacementOperationRecord,
) -> bool:
    cancellation = operation.cancellation
    return (
        operation.status == "cancelled"
        and cancellation is not None
        and cancellation.caller.identity_type == "launchplane_reconcile"
        and cancellation.reason == STAFF_TESTING_HOLD_CANCELLATION_REASON
    )


class TestingHoldApplyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal[1] = 1
    product: str
    context: str
    instance: str
    mode: TestingHoldMode = "dry-run"
    hold: bool
    # The hold's reason when setting it; the audit reason when lifting it.
    reason: str
    reviewed_plan_sha256: str = ""

    @model_validator(mode="after")
    def _validate_request(self) -> TestingHoldApplyRequest:
        normalize_reviewed_lane_request(self, label="Testing hold")
        return self


class TestingHoldPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    mode: TestingHoldMode
    product: str
    context: str
    instance: str
    action: TestingHoldAction
    changed: bool
    applied: bool = False
    before: DokployTargetStaffTestingHold | None = None
    after: DokployTargetStaffTestingHold | None = None
    read_back: DokployTargetStaffTestingHold | None = None
    read_back_matches: bool | None = None
    reconcile_requested: bool = False
    reason: str
    source_label: str = TESTING_HOLD_SOURCE_LABEL
    record_sha256_before: str
    record_sha256_after: str = ""
    plan_sha256: str


class TestingHoldReadResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ok"] = "ok"
    product: str
    context: str
    instance: str
    hold: DokployTargetStaffTestingHold | None
    record_sha256: str


def _require_testing_lane(instance: str) -> None:
    if instance != TESTING_HOLD_INSTANCE:
        raise TestingHoldRefusal(
            "not_testing_lane", "A staff-testing hold is only for a product's testing lane."
        )


def _read_target_record(
    *, record_store: TestingHoldStore, context: str, instance: str
) -> DokployTargetRecord:
    try:
        return record_store.read_dokploy_target_record(context_name=context, instance_name=instance)
    except FileNotFoundError as error:
        raise TestingHoldRefusal(
            "target_record_missing", "A testing hold requires the lane's tracked target record."
        ) from error


def _hold_terms(hold: DokployTargetStaffTestingHold | None) -> str | None:
    return None if hold is None else hold.reason


def _desired_hold(
    *,
    request: TestingHoldApplyRequest,
    existing: DokployTargetStaffTestingHold | None,
    actor: str,
) -> DokployTargetStaffTestingHold | None:
    if not request.hold:
        return None
    candidate = DokployTargetStaffTestingHold(
        reason=request.reason, recorded_by=actor, recorded_at=utc_now_timestamp()
    )
    # An unchanged hold keeps who recorded it and when.
    if existing is not None and _hold_terms(existing) == _hold_terms(candidate):
        return existing
    return candidate


def _action(
    before: DokployTargetStaffTestingHold | None, after: DokployTargetStaffTestingHold | None
) -> TestingHoldAction:
    if _hold_terms(before) == _hold_terms(after):
        return "unchanged"
    if before is None:
        return "set"
    if after is None:
        return "clear"
    return "update"


def read_testing_hold(
    *, record_store: TestingHoldStore, product: str, context: str, instance: str
) -> TestingHoldReadResult:
    context = context.strip().lower()
    instance = instance.strip().lower()
    _require_testing_lane(instance)
    target = _read_target_record(record_store=record_store, context=context, instance=instance)
    return TestingHoldReadResult(
        product=product.strip(),
        context=context,
        instance=instance,
        hold=target.policies.staff_testing_hold,
        record_sha256=target_record_sha256(target),
    )


def build_testing_hold_plan(
    *, record_store: TestingHoldStore, request: TestingHoldApplyRequest, actor: str
) -> tuple[TestingHoldPlan, DokployTargetRecord]:
    """Validate the request against the current record and return a digest-bound plan."""

    _require_testing_lane(request.instance)
    target = _read_target_record(
        record_store=record_store, context=request.context, instance=request.instance
    )
    existing = target.policies.staff_testing_hold
    desired = _desired_hold(request=request, existing=existing, actor=actor)
    action = _action(existing, desired)
    record_sha256_before = target_record_sha256(target)
    plan = TestingHoldPlan(
        mode=request.mode,
        product=request.product,
        context=request.context,
        instance=request.instance,
        action=action,
        changed=action != "unchanged",
        before=existing,
        after=desired,
        reason=request.reason,
        record_sha256_before=record_sha256_before,
        plan_sha256=canonical_sha256(
            {
                "product": request.product,
                "context": request.context,
                "instance": request.instance,
                "record_sha256_before": record_sha256_before,
                "desired": _hold_terms(desired),
            }
        ),
    )
    replacement = target.model_copy(
        update={
            "policies": target.policies.model_copy(update={"staff_testing_hold": desired}),
        }
    )
    return plan, replacement


def apply_testing_hold_plan(
    *, record_store: TestingHoldStore, request: TestingHoldApplyRequest, actor: str
) -> TestingHoldPlan:
    """Re-plan against the current record, require the reviewed digest, write, read back.

    An apply that leaves the lane unheld requests a reconcile of the product's
    testing target, so a deploy that waited on the hold runs now.
    """

    plan, replacement = build_testing_hold_plan(
        record_store=record_store, request=request, actor=actor
    )
    requested_terms = _hold_terms(replacement.policies.staff_testing_hold)
    applied = False
    if request.reviewed_plan_sha256 != plan.plan_sha256:
        current = _read_target_record(
            record_store=record_store, context=request.context, instance=request.instance
        )
        # A retried apply whose first attempt wrote but lost its receipt finds the lane
        # already in the requested state: report it, don't refuse it.
        if _hold_terms(current.policies.staff_testing_hold) != requested_terms:
            raise TestingHoldStale(
                "Reviewed testing hold plan no longer matches the lane's record."
            )
        plan = plan.model_copy(update={"changed": False, "action": "unchanged"})
    elif plan.changed:
        expected = _read_target_record(
            record_store=record_store, context=request.context, instance=request.instance
        )
        if target_record_sha256(expected) != plan.record_sha256_before:
            raise TestingHoldStale(
                "Reviewed testing hold plan no longer matches the lane's record."
            )
        try:
            record_store.compare_and_write_dokploy_target_record(
                expected_record=expected,
                replacement_record=replacement.model_copy(
                    update={
                        "updated_at": utc_now_timestamp(),
                        "source_label": TESTING_HOLD_SOURCE_LABEL,
                    }
                ),
            )
        except DokployTargetRecordChanged as error:
            raise TestingHoldStale(
                "The lane's target record changed while the testing hold was applied."
            ) from error
        applied = True
    stored = _read_target_record(
        record_store=record_store, context=request.context, instance=request.instance
    )
    stored_hold = stored.policies.staff_testing_hold
    read_back_matches = _hold_terms(stored_hold) == requested_terms
    reconcile_requested = False
    if (
        stored_hold is None
        and read_back_matches
        and isinstance(record_store, ProductReconcileRequester)
    ):
        # Also on a retried or no-op lift: a folded reconcile request costs nothing.
        record_store.request_product_reconcile(
            ProductReconcileTarget(product=request.product, target_kind="testing"),
            utc_now_timestamp(),
        )
        reconcile_requested = True
    return plan.model_copy(
        update={
            "applied": applied,
            "read_back": stored_hold,
            "read_back_matches": read_back_matches,
            "reconcile_requested": reconcile_requested,
            "record_sha256_after": target_record_sha256(stored),
        }
    )
