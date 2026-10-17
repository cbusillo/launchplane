"""Prepare beside the serving copy, then move exactly one stable testing route."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Protocol

from control_plane.contracts.testing_rebuild import (
    TestingCopy,
    TestingCopyReadiness,
    TestingCopyReceipt,
    TestingRebuildPlan,
    TestingRebuildRecord,
    TestingRouteObservation,
)
from control_plane.traffic_switch import (
    require_fresh_readiness,
    require_route_authority,
    require_serving_route,
)


class TestingRebuildStore(Protocol):
    def load(self, operation_id: str) -> TestingRebuildRecord: ...
    def save(self, record: TestingRebuildRecord) -> None: ...


class TestingRebuildProvider(Protocol):
    """Existing service authority and provider fencing belong to the adapter.

    All effects reconcile their original operation key before a retry. An
    unknown dispatch never becomes retryable merely because a route is old.
    No method receives credentials or caller-selected production coordinates.
    """

    def exclusive(self, plan: TestingRebuildPlan) -> AbstractContextManager[None]: ...
    def guard(self, plan: TestingRebuildPlan) -> None:
        """Recheck authorization, lease, holds/use, custody and production isolation."""
        ...

    def current_route(self, plan: TestingRebuildPlan) -> TestingRouteObservation: ...
    def prepare(self, plan: TestingRebuildPlan) -> TestingCopyReceipt:
        """Allocate/restore/sanitize only the independent copy, preserving serving authority."""
        ...

    def readiness(self, plan: TestingRebuildPlan) -> TestingCopyReadiness: ...
    def switch(self, plan: TestingRebuildPlan, previous: TestingCopy, desired: TestingCopy) -> None:
        """Compare-and-switch the existing route; reconcile unknown effects in the runner."""
        ...

    def publish(self, plan: TestingRebuildPlan, desired: TestingCopy) -> None:
        """DB-only atomic CAS/adoption with readback, preserving the lane address.

        Only the original or candidate snapshot from this operation may be
        selected, under its current lease and the authority-writer lock. Read
        the prior transaction outcome before an inverse; an unavailable outcome
        refuses. This method sends no provider command and rotates no secret.
        """
        ...

    def retire_old(self, plan: TestingRebuildPlan) -> None:
        """Drain/stop the old copy idempotently; retain its storage and custody."""
        ...


def _route(
    observation: TestingRouteObservation, plan: TestingRebuildPlan, copy: TestingCopy
) -> None:
    require_route_authority(
        observation,
        route_id=plan.route_id,
        host=plan.host,
        target_url=copy.private_url,
        slot_id=copy.copy_id,
    )


def _serving(
    observation: TestingRouteObservation,
    plan: TestingRebuildPlan,
    copy: TestingCopy,
    now: datetime,
) -> None:
    require_serving_route(
        observation,
        route_id=plan.route_id,
        host=plan.host,
        target_url=copy.private_url,
        slot_id=copy.copy_id,
        identity=copy.runtime_identity,
    )
    require_fresh_readiness(
        observed_at=observation.observed_at,
        now=now,
        actual=copy.runtime_identity,
        expected=copy.runtime_identity,
    )


def execute_testing_rebuild(
    plan: TestingRebuildPlan,
    store: TestingRebuildStore,
    provider: TestingRebuildProvider,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> TestingRebuildRecord:
    """The service caller binds admitted records; this function grants no authority.

    Failed preparation/readiness keeps the old route and authority. Persist
    intent before every effect. Transport or authorization failures propagate
    with that intent intact, with no fallback or automatic retry.
    """
    plan = TestingRebuildPlan.model_validate(plan.model_dump())
    with provider.exclusive(plan):
        provider.guard(plan)
        try:
            record = store.load(plan.operation_id)
        except FileNotFoundError:
            _route(provider.current_route(plan), plan, plan.old)
            record = TestingRebuildRecord(plan=plan, began_at=clock(), updated_at=clock())
            store.save(record)
        record = TestingRebuildRecord.model_validate(record.model_dump())
        if record.plan != plan:
            raise ValueError("Use the original rebuild plan and operation key.")

        def save(**changes: object) -> None:
            nonlocal record
            record = TestingRebuildRecord.model_validate(
                {**record.model_dump(), **changes, "updated_at": clock()}
            )
            store.save(record)

        if record.step in {"complete", "failed"}:
            copy = plan.new if record.step == "complete" else plan.old
            observation = provider.current_route(plan)
            if record.step == "complete":
                _serving(observation, plan, copy, clock())
            else:
                _route(observation, plan, copy)
            return record

        if record.step == "preparing":
            _route(provider.current_route(plan), plan, plan.old)
            provider.guard(plan)
            receipt = provider.prepare(plan)
            if (receipt.plan_digest, receipt.copy_id) != (plan.digest, plan.new.copy_id):
                raise ValueError("Prepared copy differs from the admitted rebuild.")
            _route(provider.current_route(plan), plan, plan.old)
            save(step="prepared", preparation=receipt)

        if record.preparation is None or (
            record.preparation.plan_digest,
            record.preparation.copy_id,
        ) != (plan.digest, plan.new.copy_id):
            raise ValueError("The rebuild has no matching independent-copy receipt.")

        if record.step in {"prepared", "switching"}:
            provider.guard(plan)
            observation = provider.current_route(plan)
            if observation.slot_id == plan.old.copy_id:
                _route(observation, plan, plan.old)
                readiness = provider.readiness(plan)
                if (readiness.copy_id, readiness.host) != (plan.new.copy_id, plan.host):
                    raise ValueError("Readiness belongs to another copy or address.")
                require_fresh_readiness(
                    observed_at=readiness.observed_at,
                    now=clock(),
                    actual=readiness.runtime_identity,
                    expected=plan.new.runtime_identity,
                )
                save(step="switching", readiness=readiness)
                provider.guard(plan)
                provider.switch(plan, plan.old, plan.new)
                observation = provider.current_route(plan)
            elif record.step != "switching":
                raise ValueError("Traffic moved before the persisted switch intent.")
            _route(observation, plan, plan.new)
            try:
                _serving(observation, plan, plan.new, clock())
            except ValueError:
                save(step="rolling_back", error="candidate_public_health_failed", route=observation)
            else:
                save(step="publishing", route=observation)

        if record.step == "publishing":
            provider.guard(plan)
            observation = provider.current_route(plan)
            _route(observation, plan, plan.new)
            try:
                _serving(observation, plan, plan.new, clock())
            except ValueError:
                save(step="rolling_back", error="candidate_public_health_failed", route=observation)
            else:
                provider.publish(plan, plan.new)
                save(step="retiring", route=observation)

        if record.step == "retiring":
            provider.guard(plan)
            observation = provider.current_route(plan)
            _route(observation, plan, plan.new)
            try:
                _serving(observation, plan, plan.new, clock())
            except ValueError:
                if record.retirement_started:
                    # An interrupted stop may already have drained the old copy.
                    # It cannot be offered as a healthy rollback destination.
                    raise
                save(step="rolling_back", error="candidate_public_health_failed", route=observation)

        if record.step == "rolling_back":
            provider.guard(plan)
            observation = provider.current_route(plan)
            if observation.slot_id == plan.new.copy_id:
                _route(observation, plan, plan.new)
                provider.switch(plan, plan.new, plan.old)
                observation = provider.current_route(plan)
            _serving(observation, plan, plan.old, clock())
            provider.guard(plan)
            provider.publish(plan, plan.old)
            save(step="failed", route=observation, ended_at=clock())
            return record

        if record.step == "retiring":
            provider.guard(plan)
            save(retirement_started=True)
            provider.retire_old(plan)
            save(step="complete", route=observation, ended_at=clock())
        return record
