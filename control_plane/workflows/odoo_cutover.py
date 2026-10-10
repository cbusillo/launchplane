"""Restartable two-slot release; providers own exclusive idempotent effects."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Protocol

from control_plane.contracts.odoo_cutover import (
    CutoverStep,
    OdooCutoverPlan,
    OdooCutoverRecord,
    OdooMaintenanceReceipt,
    OdooRecoveryTuple,
    OdooRouteObservation,
    OdooSlot,
    OdooSlotReadiness,
)
from control_plane.contracts.prepared_public_site import (
    PreparedPublicSite,
    PublicPauseRecord,
)
from control_plane.prepared_public_pause import PreparedPublicPause, PublicPauseProvider
from control_plane.prepared_public_site import verify_snapshot


class OdooCutoverStore(Protocol):
    def load(self, record_id: str) -> OdooCutoverRecord: ...
    def save(self, record: OdooCutoverRecord) -> None: ...


class OdooCutoverProvider(PublicPauseProvider, Protocol):
    def exclusive(self, plan: OdooCutoverPlan) -> AbstractContextManager[None]: ...
    def current_route(self, plan: OdooCutoverPlan) -> OdooRouteObservation: ...
    def prepare_candidate(self, plan: OdooCutoverPlan) -> None:
        """Pull/prepare the separate project without starting any registry/hooks."""
        ...

    def prepare_public(self, plan: OdooCutoverPlan) -> PreparedPublicSite:
        """Capture exact old runtime and prove passive navigation/assets in a browser."""
        ...

    def backup(self, plan: OdooCutoverPlan) -> OdooRecoveryTuple: ...
    def maintenance(self, plan: OdooCutoverPlan) -> OdooMaintenanceReceipt:
        """Complete planned devkit run; read an uncertain receipt before repeating."""
        ...

    def readiness(self, plan: OdooCutoverPlan, slot: OdooSlot) -> OdooSlotReadiness: ...
    def switch(self, plan: OdooCutoverPlan, slot: OdooSlot) -> OdooRouteObservation:
        """CAS one file-provider route, read applied authority and public identity."""
        ...

    def handoff_writers(self, plan: OdooCutoverPlan, slot: OdooSlot) -> None:
        """Fence/drain predecessor first; read exactly one idempotent owner per target."""
        ...

    def drain_old(self, plan: OdooCutoverPlan) -> bool:
        """Observe zero old connections; retain old slot and referenced assets."""
        ...


class _PauseStore:
    def __init__(self, store: OdooCutoverStore, record_id: str) -> None:
        self.store, self.record_id = store, record_id

    def load(self, pause_id: str) -> PublicPauseRecord:
        pause = self.store.load(self.record_id).pause
        if pause is None or pause.pause_id != pause_id:
            raise ValueError("cutover has no matching writer pause")
        return pause

    def save(self, pause: PublicPauseRecord) -> None:
        record = self.store.load(self.record_id)
        self.store.save(record.model_copy(update={"pause": pause}))


def _require_route(route: OdooRouteObservation, plan: OdooCutoverPlan, slot: OdooSlot) -> None:
    if (
        route.route_id != plan.route_id
        or route.host != plan.host
        or route.target_url != slot.private_url
        or route.slot_id != slot.slot_id
        or route.binding != slot.binding
        or route.runtime_identity != slot.runtime_identity
    ):
        raise ValueError("applied route/public identity differs from expected slot")


def _require_ready(
    readiness: OdooSlotReadiness, plan: OdooCutoverPlan, slot: OdooSlot, now: datetime
) -> None:
    if (
        readiness.binding != slot.binding
        or readiness.slot_id != slot.slot_id
        or readiness.image != slot.image
        or readiness.host != plan.host
        or readiness.runtime_identity != slot.runtime_identity
        or not 0 <= (now - readiness.observed_at).total_seconds() <= 60
    ):
        raise ValueError("private readiness identity is wrong, stale or unavailable")


def execute_odoo_cutover(
    plan: OdooCutoverPlan,
    store: OdooCutoverStore,
    provider: OdooCutoverProvider,
    *,
    record_id: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    checkpoint: Callable[[str], None] = lambda _step: None,
) -> OdooCutoverRecord:
    """The caller supplies existing accepted-release/worker and backup authority.

    No fallback to in-place deployment, guessed maintenance, or image-only
    recovery is permitted. A provider failure leaves the recorded intent open.
    """
    plan = OdooCutoverPlan.model_validate(plan.model_dump())
    with provider.exclusive(plan):
        try:
            record = store.load(record_id)
        except FileNotFoundError:
            _require_route(provider.current_route(plan), plan, plan.old)
            record = OdooCutoverRecord(
                record_id=record_id, plan=plan, began_at=clock(), updated_at=clock()
            )
            store.save(record)
        record = OdooCutoverRecord.model_validate(record.model_dump())
        if record.plan != plan:
            raise ValueError("cannot change an uncertain cutover's immutable release plan")
        if record.step in {"complete", "rolled_back"}:
            return record
        if record.step == "rolling_back":
            raise ValueError("recorded rollback must resume through the rollback executor")

        def advance(step: CutoverStep, **values: object) -> None:
            nonlocal record
            record = store.load(record_id).model_copy(
                update={"step": step, "updated_at": clock(), "error": "", **values}
            )
            store.save(record)
            checkpoint(step)

        try:
            if record.step == "preparing":
                checkpoint("prepare_candidate")
                provider.prepare_candidate(plan)
                site = None
                if plan.compatibility.classification == "database_changing":
                    site = provider.prepare_public(plan)
                    verify_snapshot(site)
                    if site.plan.binding != plan.old.binding or site.plan.writers != plan.writers:
                        raise ValueError("prepared serving is not bound to current runtime/writers")
                advance("prepared", public_site=site)
            if record.step == "prepared":
                advance("pausing" if record.public_site else "backing_up")
            if record.step == "pausing":
                assert record.public_site is not None
                pause_store = _PauseStore(store, record_id)
                pause = record.pause
                if pause is None:
                    # The identity and beginning survive a crash during publication.
                    pause = PublicPauseRecord(
                        pause_id=record_id,
                        binding=plan.old.binding,
                        content_digest=record.public_site.content_digest,
                        began_at=clock(),
                    )
                    pause_store.save(pause)
                controller = PreparedPublicPause(
                    record.public_site, pause_store, provider, clock=clock
                )
                if pause.state == "fencing":
                    checkpoint("publish_prepared")
                    if provider.publish(record.public_site, pause.pause_id) != pause.content_digest:
                        raise ValueError("prepared route publication/readback failed")
                    checkpoint("drain_writers")
                    controller.drain(pause.pause_id)
                advance("backing_up")
            if record.step == "backing_up":
                checkpoint("capture_backup")
                backup = provider.backup(plan)
                if backup.binding != plan.old.binding or backup.image != plan.old.image:
                    raise ValueError(
                        "backup is not the coherent current image/database/filestore tuple"
                    )
                if record.pause and backup.recovery_point_at < record.pause.began_at:
                    raise ValueError("database-changing backup predates writer pause")
                advance("maintaining", backup=backup)
            if record.step in {"maintaining", "ready", "switching", "resuming", "draining"}:
                if record.backup is None or record.backup.binding != plan.old.binding:
                    raise ValueError("cutover progress requires the bound coherent backup")
                if plan.compatibility.classification == "database_changing" and (
                    record.public_site is None
                    or record.pause is None
                    or record.pause.state not in {"drained", "resuming", "complete"}
                ):
                    raise ValueError("database-changing progress lacks its persisted writer pause")
            if record.step == "maintaining":
                checkpoint("planned_maintenance")
                receipt = provider.maintenance(plan)
                if (
                    receipt.release_id != plan.release_id
                    or receipt.binding != plan.new.binding
                    or receipt.image != plan.new.image
                    or receipt.candidate_manifest_sha256
                    != plan.compatibility.candidate_manifest_sha256
                    or not set(receipt.install_modules) <= set(plan.compatibility.install_modules)
                    or not set(plan.compatibility.update_modules)
                    - set(plan.compatibility.install_modules)
                    <= set(receipt.update_modules)
                    or (
                        plan.compatibility.classification == "compatible"
                        and (receipt.install_modules or receipt.update_modules)
                    )
                ):
                    raise ValueError("maintenance receipt does not match complete candidate plan")
                advance("ready", maintenance=receipt)
            if record.step == "ready":
                readiness = provider.readiness(plan, plan.new)
                _require_ready(readiness, plan, plan.new, clock())
                advance("switching", readiness=readiness)
            if record.step == "switching":
                # No acknowledged publication is assumed after an uncertain switch.
                current = provider.current_route(plan)
                if current.slot_id != plan.new.slot_id:
                    if record.public_site:
                        if (
                            provider.observed_public_copy(record_id)
                            != record.public_site.content_digest
                        ):
                            raise ValueError("prepared authority lost before candidate switch")
                    else:
                        _require_route(current, plan, plan.old)
                    _require_ready(provider.readiness(plan, plan.new), plan, plan.new, clock())
                    checkpoint("atomic_switch")
                    current = provider.switch(plan, plan.new)
                _require_route(current, plan, plan.new)
                advance("resuming", route=current)
            if record.step == "resuming":
                _require_route(provider.current_route(plan), plan, plan.new)
                checkpoint("resume_writers")
                if record.public_site:
                    PreparedPublicPause(
                        record.public_site, _PauseStore(store, record_id), provider, clock=clock
                    ).finish(record_id, expected=plan.new.binding)
                else:
                    provider.handoff_writers(plan, plan.new)
                advance("draining")
            if record.step == "draining":
                _require_route(provider.current_route(plan), plan, plan.new)
                if not provider.drain_old(plan):
                    raise ValueError("old connections have not drained; retain both slots")
                advance("complete", ended_at=clock(), old_connections_drained=True)
        except Exception as error:
            # Never persist exception text, which may contain provider secrets/logs.
            record = store.load(record_id).model_copy(update={"error": type(error).__name__})
            store.save(record)
            raise
        return store.load(record_id)


def rollback_compatible_cutover(
    store: OdooCutoverStore,
    provider: OdooCutoverProvider,
    record_id: str,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
) -> OdooCutoverRecord:
    record = store.load(record_id)
    plan = record.plan
    if plan.compatibility.classification != "compatible":
        raise ValueError("database-changing rollback requires an authorized coherent tuple restore")
    with provider.exclusive(plan):
        if record.step == "rolled_back":
            _require_route(provider.current_route(plan), plan, plan.old)
            return record
        if record.step not in {"complete", "rolling_back"}:
            raise ValueError("unfinished cutover requires recorded recovery, not a new rollback")
        if record.step == "complete":
            _require_route(provider.current_route(plan), plan, plan.new)
            record = record.model_copy(update={"step": "rolling_back", "updated_at": clock()})
            store.save(record)
        route = provider.current_route(plan)
        if route.slot_id != plan.old.slot_id:
            _require_route(route, plan, plan.new)
            _require_ready(provider.readiness(plan, plan.old), plan, plan.old, clock())
            route = provider.switch(plan, plan.old)
        _require_route(route, plan, plan.old)
        provider.handoff_writers(plan, plan.old)
        record = record.model_copy(
            update={"step": "rolled_back", "route": route, "updated_at": clock()}
        )
        store.save(record)
        return record
