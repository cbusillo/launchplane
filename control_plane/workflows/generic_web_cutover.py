"""Compatible generic-web release/recovery using the common traffic contract."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Protocol

from control_plane.contracts.generic_web_cutover import (
    GenericWebCutoverPlan,
    GenericWebCutoverRecord,
    WebSlot,
    WebSlotReadiness,
    WebRouteObservation,
    WebCutoverStep,
)
from control_plane.traffic_switch import (
    require_serving_route,
    require_fresh_readiness,
    require_route_authority,
)


class WebCutoverStore(Protocol):
    def load(self, record_id: str) -> GenericWebCutoverRecord: ...
    def save(self, record: GenericWebCutoverRecord) -> None: ...


class WebCutoverProvider(Protocol):
    def exclusive(self, plan: GenericWebCutoverPlan) -> AbstractContextManager[None]: ...
    def preflight(self, plan: GenericWebCutoverPlan) -> None: ...
    def prepare_candidate(self, plan: GenericWebCutoverPlan) -> None:
        """Idempotent private target preparation; all singleton workers stay fenced."""
        ...

    def readiness(self, plan: GenericWebCutoverPlan, slot: WebSlot) -> WebSlotReadiness: ...
    def current_route(self, plan: GenericWebCutoverPlan) -> WebRouteObservation: ...
    def switch(self, plan: GenericWebCutoverPlan, slot: WebSlot) -> WebRouteObservation: ...
    def handoff_writers(self, plan: GenericWebCutoverPlan, slot: WebSlot) -> None:
        """Drain predecessor workers before resuming the one idempotent owner."""
        ...

    def drain(self, plan: GenericWebCutoverPlan, slot: WebSlot) -> bool:
        """Observe zero old connections without destroying the retained slot/assets."""
        ...


def require_route(route: WebRouteObservation, plan: GenericWebCutoverPlan, slot: WebSlot) -> None:
    require_serving_route(
        route,
        route_id=plan.lane.route_id,
        host=plan.lane.host,
        target_url=slot.private_url,
        slot_id=slot.slot_id,
        identity=slot.runtime_identity,
    )


def require_ready(
    ready: WebSlotReadiness,
    plan: GenericWebCutoverPlan,
    slot: WebSlot,
    now: datetime,
    *,
    require_fenced: bool = True,
) -> None:
    require_fresh_readiness(
        observed_at=ready.observed_at,
        now=now,
        actual=ready.runtime_identity,
        expected=slot.runtime_identity,
    )
    if (
        ready.slot_id != slot.slot_id
        or ready.image != slot.image
        or ready.host != plan.lane.host
        or ready.runtime_settings_sha256 != plan.lane.runtime_settings_sha256
        or (require_fenced and not ready.background_workers_fenced)
    ):
        raise ValueError("private readiness differs from the exact slot/settings")


def execute_web_cutover(
    plan: GenericWebCutoverPlan,
    store: WebCutoverStore,
    provider: WebCutoverProvider,
    *,
    record_id: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    checkpoint: Callable[[str], None] = lambda _step: None,
) -> GenericWebCutoverRecord:
    plan = GenericWebCutoverPlan.model_validate(plan.model_dump())
    with provider.exclusive(plan):
        try:
            record = store.load(record_id)
        except FileNotFoundError:
            record = GenericWebCutoverRecord(
                record_id=record_id, plan=plan, began_at=clock(), updated_at=clock()
            )
            if plan.compatibility.classification == "compatible":
                provider.preflight(plan)
                require_route(provider.current_route(plan), plan, plan.old)
            store.save(record)
        record = GenericWebCutoverRecord.model_validate(record.model_dump())
        if record.plan != plan:
            raise ValueError("cannot change a cutover's immutable release plan")
        if plan.compatibility.classification != "compatible":
            store.save(record.model_copy(update={"step": "blocked", "error": "database_changing"}))
            raise ValueError(
                "generic-web release lacks proved read/write compatibility; no fallback"
            )
        provider.preflight(plan)
        if record.step in {"rolling_back", "rolled_back", "blocked"}:
            raise ValueError("release requires its recorded recovery path")

        def advance(step: WebCutoverStep, **values: object) -> None:
            nonlocal record
            record = store.load(record_id).model_copy(
                update={"step": step, "updated_at": clock(), "error": "", **values}
            )
            store.save(record)

        try:
            if record.step == "complete":
                require_route(provider.current_route(plan), plan, plan.new)
                return record
            if record.step == "preparing":
                require_route(provider.current_route(plan), plan, plan.old)
                checkpoint("prepare_candidate")
                provider.prepare_candidate(plan)
                require_route(provider.current_route(plan), plan, plan.old)
                advance("ready")
            if record.step == "ready":
                ready = provider.readiness(plan, plan.new)
                require_ready(ready, plan, plan.new, clock())
                advance("switching", readiness=ready)
            if record.step == "switching":
                route = provider.current_route(plan)
                if route.slot_id != plan.new.slot_id:
                    require_route(route, plan, plan.old)
                    require_ready(provider.readiness(plan, plan.new), plan, plan.new, clock())
                    checkpoint("atomic_switch")
                    route = provider.switch(plan, plan.new)
                require_route(route, plan, plan.new)
                advance("resuming", route=route)
            if record.step == "resuming":
                require_route(provider.current_route(plan), plan, plan.new)
                checkpoint("resume_writers")
                provider.handoff_writers(plan, plan.new)
                advance("draining")
            if record.step == "draining":
                require_route(provider.current_route(plan), plan, plan.new)
                checkpoint("drain_old")
                if not provider.drain(plan, plan.old):
                    raise TimeoutError("old connections have not drained; both slots retained")
                advance("complete", ended_at=clock(), old_connections_drained=True)
        except Exception as error:
            store.save(store.load(record_id).model_copy(update={"error": type(error).__name__}))
            raise
        return store.load(record_id)


def rollback_web_cutover(
    store: WebCutoverStore,
    provider: WebCutoverProvider,
    record_id: str,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    checkpoint: Callable[[str], None] = lambda _step: None,
) -> GenericWebCutoverRecord:
    initial = store.load(record_id)
    plan = GenericWebCutoverPlan.model_validate(initial.plan.model_dump())
    if plan.compatibility.classification != "compatible":
        raise ValueError("incompatible rollback requires authorized data recovery")
    with provider.exclusive(plan):
        record = store.load(record_id)
        provider.preflight(plan)
        if record.step == "rolled_back":
            require_route(provider.current_route(plan), plan, plan.old)
            return record
        route = provider.current_route(plan)
        current = plan.old if route.slot_id == plan.old.slot_id else plan.new
        require_route_authority(
            route,
            route_id=plan.lane.route_id,
            host=plan.lane.host,
            target_url=current.private_url,
            slot_id=current.slot_id,
        )
        observed_identity = route.public_health.observed_runtime_identity
        if observed_identity is not None and observed_identity != current.runtime_identity:
            raise ValueError("public runtime differs from the admitted rollback authority")
        if record.step != "rolling_back":
            record = record.model_copy(
                update={"step": "rolling_back", "rollback_started_at": clock()}
            )
            store.save(record)
        try:
            require_ready(
                provider.readiness(plan, plan.old),
                plan,
                plan.old,
                clock(),
                require_fenced=current != plan.old,
            )
            if current != plan.old:
                checkpoint("rollback_atomic_switch")
                route = provider.switch(plan, plan.old)
            require_route(route, plan, plan.old)
            checkpoint("rollback_resume_writers")
            provider.handoff_writers(plan, plan.old)
            checkpoint("rollback_drain")
            if not provider.drain(plan, plan.new):
                raise TimeoutError("candidate connections have not drained")
            store.save(
                store.load(record_id).model_copy(
                    update={
                        "step": "rolled_back",
                        "route": route,
                        "updated_at": clock(),
                        "error": "",
                    }
                )
            )
        except Exception as error:
            store.save(store.load(record_id).model_copy(update={"error": type(error).__name__}))
            raise
        return store.load(record_id)
