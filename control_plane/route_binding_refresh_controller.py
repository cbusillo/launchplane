from __future__ import annotations

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from control_plane.contracts.product_profile_record import LaunchplaneProductProfileRecord
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    route_binding_record_sha256,
)
from control_plane.route_binding_reconcile import (
    RouteBindingExpectedCurrent,
    RouteBindingReconcileFinding,
    RouteBindingReconcilePlan,
    RouteBindingReconcileRequest,
    RouteBindingReconcileStore,
    plan_route_binding_reconcile,
)


class RouteBindingRefreshControllerStore(RouteBindingReconcileStore, Protocol):
    def list_product_profile_records(
        self, *, driver_id: str = ""
    ) -> tuple[LaunchplaneProductProfileRecord, ...]: ...


class RouteBindingRefreshTargetLimitExceeded(ValueError):
    pass


class RouteBindingRefreshTargetInvariantError(ValueError):
    pass


class RouteBindingRefreshTarget(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: Literal["testing"] = "testing"
    current_record_sha256: str
    record: EnvironmentRouteBindingRecord = Field(exclude=True)


RouteBindingRefreshOutcomeStatus = Literal[
    "unchanged",
    "planned_refresh",
    "blocked",
    "conflict",
]


class RouteBindingRefreshOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    product: str
    context: str
    instance: Literal["testing"] = "testing"
    status: RouteBindingRefreshOutcomeStatus
    operation: Literal["refresh", "none"] = "none"
    findings: tuple[RouteBindingReconcileFinding, ...] = ()
    current_record_sha256: str
    candidate_record_sha256: str
    stale_after: str = ""
    reconcile_plan: RouteBindingReconcilePlan | None = Field(default=None, exclude=True)


class RouteBindingRefreshControllerPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Literal["ready", "attention", "empty"]
    evaluated_at: str
    target_count: int = Field(ge=0)
    outcomes: tuple[RouteBindingRefreshOutcome, ...] = ()


def discover_active_odoo_testing_route_bindings(
    record_store: RouteBindingRefreshControllerStore,
    *,
    target_limit: int,
) -> tuple[RouteBindingRefreshTarget, ...]:
    if target_limit < 1:
        raise ValueError("Route binding refresh target_limit must be positive.")
    targets: dict[tuple[str, str, str], RouteBindingRefreshTarget] = {}
    for profile in record_store.list_product_profile_records(driver_id="odoo"):
        if not profile.is_active:
            continue
        for lane in profile.lanes:
            if lane.instance.strip().lower() != "testing":
                continue
            try:
                record = record_store.read_route_binding_record(
                    product=profile.product,
                    context_name=lane.context,
                    instance_name=lane.instance,
                )
            except FileNotFoundError:
                continue
            expected_identity = (profile.product, lane.context, "testing")
            observed_identity = (record.product, record.context, record.instance)
            if observed_identity != expected_identity:
                raise RouteBindingRefreshTargetInvariantError(
                    "Route binding refresh lookup returned a record outside the requested "
                    "product/context/testing identity."
                )
            if record.status != "active" or record.source.source_kind != "service":
                continue
            key = (record.product, record.context, record.instance)
            targets[key] = RouteBindingRefreshTarget(
                product=record.product,
                context=record.context,
                instance="testing",
                current_record_sha256=route_binding_record_sha256(record),
                record=record,
            )
            if len(targets) > target_limit:
                raise RouteBindingRefreshTargetLimitExceeded(
                    "Odoo testing route binding refresh discovered more targets than the "
                    "bounded controller limit."
                )
    return tuple(targets[key] for key in sorted(targets))


def plan_odoo_testing_route_binding_refresh(
    *,
    record_store: RouteBindingRefreshControllerStore,
    evaluated_at: str,
    target_limit: int,
) -> RouteBindingRefreshControllerPlan:
    targets = discover_active_odoo_testing_route_bindings(
        record_store,
        target_limit=target_limit,
    )
    if not targets:
        return RouteBindingRefreshControllerPlan(
            status="empty",
            evaluated_at=evaluated_at,
            target_count=0,
        )

    outcomes: list[RouteBindingRefreshOutcome] = []
    for target in targets:
        reconcile_plan = plan_route_binding_reconcile(
            record_store=record_store,
            request=RouteBindingReconcileRequest(
                product=target.product,
                context=target.context,
                instance=target.instance,
                expected_current=RouteBindingExpectedCurrent(
                    state="present",
                    record_sha256=target.current_record_sha256,
                ),
                source_label=target.record.source.source_label,
                evaluated_at=evaluated_at,
            ),
        )
        status: RouteBindingRefreshOutcomeStatus
        operation: Literal["refresh", "none"] = "none"
        if reconcile_plan.status == "unchanged":
            status = "unchanged"
        elif reconcile_plan.status == "ready" and reconcile_plan.operation == "refresh":
            status = "planned_refresh"
            operation = "refresh"
        elif reconcile_plan.status == "blocked":
            status = "blocked"
        else:
            status = "conflict"
        outcomes.append(
            RouteBindingRefreshOutcome(
                product=target.product,
                context=target.context,
                instance=target.instance,
                status=status,
                operation=operation,
                findings=reconcile_plan.findings,
                current_record_sha256=reconcile_plan.current_record_sha256,
                candidate_record_sha256=reconcile_plan.candidate_record_sha256,
                stale_after=(
                    reconcile_plan.record.source.stale_after
                    if reconcile_plan.record is not None
                    else target.record.source.stale_after
                ),
                reconcile_plan=reconcile_plan,
            )
        )
    return RouteBindingRefreshControllerPlan(
        status=(
            "attention"
            if any(outcome.status in {"blocked", "conflict"} for outcome in outcomes)
            else "ready"
        ),
        evaluated_at=evaluated_at,
        target_count=len(outcomes),
        outcomes=tuple(outcomes),
    )
