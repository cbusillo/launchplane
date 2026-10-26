"""Restartable two-slot release; providers own exclusive idempotent effects."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from datetime import UTC, datetime
from typing import Protocol

from control_plane.contracts.odoo_cutover import (
    ODOO_CUTOVER_TERMINAL_STEPS,
    CutoverStep,
    OdooCutoverPlan,
    OdooCutoverRecord,
    OdooMaintenanceReceipt,
    OdooRecoveryTuple,
    OdooRouteObservation,
    OdooSlot,
    OdooSlotReadiness,
    OdooWebsiteSettingsReceipt,
)
from control_plane.contracts.prepared_public_site import (
    PreparedPublicSite,
    PublicPauseRecord,
)
from control_plane.contracts.odoo_website_settings import OdooWebsiteSettingsIntent
from control_plane.prepared_public_pause import PreparedPublicPause, PublicPauseProvider
from control_plane.prepared_public_site import verify_snapshot
from control_plane.odoo_cutover_plan import rollback_pause_identity
from control_plane.odoo_cutover_recovery import BackupVerificationRejected, backup_recovery_eligible
from control_plane.traffic_switch import (
    require_serving_route,
    require_fresh_readiness,
    require_route_authority,
)


class OdooCutoverStore(Protocol):
    def load(self, record_id: str) -> OdooCutoverRecord: ...
    def save(self, record: OdooCutoverRecord) -> None: ...


class OdooCutoverProvider(PublicPauseProvider, Protocol):
    def preflight(self, plan: OdooCutoverPlan) -> None:
        """Read-only capability validation before persisting a new cutover."""
        ...

    def exclusive(self, plan: OdooCutoverPlan) -> AbstractContextManager[None]: ...
    def current_route(self, plan: OdooCutoverPlan) -> OdooRouteObservation: ...
    def prepare_candidate(self, plan: OdooCutoverPlan) -> None:
        """Pull/prepare the separate project without starting any registry/hooks."""
        ...

    def prepare_public(self, plan: OdooCutoverPlan) -> PreparedPublicSite:
        """Capture exact old runtime and prove passive navigation/assets in a browser."""
        ...

    def owns_prepared_pause(self, pause_id: str) -> bool: ...
    def cancel_backup_restore(self, plan: OdooCutoverPlan) -> None: ...

    def backup(self, plan: OdooCutoverPlan) -> OdooRecoveryTuple: ...
    def maintenance(self, plan: OdooCutoverPlan) -> OdooMaintenanceReceipt:
        """Complete planned devkit run; read an uncertain receipt before repeating."""
        ...

    def website_settings(
        self, plan: OdooCutoverPlan, slot: OdooSlot, intent: OdooWebsiteSettingsIntent
    ) -> OdooWebsiteSettingsReceipt: ...

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
    def __init__(
        self,
        store: OdooCutoverStore,
        record_id: str,
        *,
        rollback: bool = False,
        checkpoint: Callable[[str], None] = lambda _step: None,
    ) -> None:
        self.store, self.record_id = store, record_id
        self.field = "rollback_pause" if rollback else "pause"
        self.checkpoint = checkpoint

    def load(self, pause_id: str) -> PublicPauseRecord:
        record = self.store.load(self.record_id)
        pause = record.rollback_pause if self.field == "rollback_pause" else record.pause
        if pause is None or pause.pause_id != pause_id:
            raise ValueError("cutover has no matching writer pause")
        return pause

    def save(self, pause: PublicPauseRecord) -> None:
        self.checkpoint("pause_checkpoint")
        record = self.store.load(self.record_id)
        self.store.save(record.model_copy(update={self.field: pause}))


def _require_route(route: OdooRouteObservation, plan: OdooCutoverPlan, slot: OdooSlot) -> None:
    require_serving_route(
        route,
        route_id=plan.route_id,
        host=plan.host,
        target_url=slot.private_url,
        slot_id=slot.slot_id,
        identity=slot.runtime_identity,
    )
    if route.binding != slot.binding:
        raise ValueError("applied route/public identity differs from expected slot")


def _require_ready(
    readiness: OdooSlotReadiness, plan: OdooCutoverPlan, slot: OdooSlot, now: datetime
) -> None:
    require_fresh_readiness(
        observed_at=readiness.observed_at,
        now=now,
        actual=readiness.runtime_identity,
        expected=slot.runtime_identity,
    )
    if (
        readiness.binding != slot.binding
        or readiness.slot_id != slot.slot_id
        or readiness.image != slot.image
        or readiness.host != plan.host
        or readiness.runtime_identity != slot.runtime_identity
        or not 0 <= (now - readiness.observed_at).total_seconds() <= 60
    ):
        raise ValueError("private readiness identity is wrong, stale or unavailable")


def _require_recovery_route(
    route: OdooRouteObservation, plan: OdooCutoverPlan, slot: OdooSlot
) -> None:
    require_route_authority(
        route,
        route_id=plan.route_id,
        host=plan.host,
        target_url=slot.private_url,
        slot_id=slot.slot_id,
    )
    if route.binding != slot.binding or route.runtime_identity not in (None, slot.runtime_identity):
        raise ValueError("rollback traffic authority belongs to another runtime")


def require_website_receipt(
    receipt: OdooWebsiteSettingsReceipt | None,
    slot: OdooSlot,
    intent: OdooWebsiteSettingsIntent | None,
) -> None:
    if intent is None:
        if receipt is not None:
            raise ValueError("website receipt has no decision-bound snapshot")
        return
    if (
        receipt is None
        or receipt.binding != slot.binding
        or receipt.image != slot.image
        or receipt.settings_sha256 != intent.settings.digest
        or receipt.startup_settings_sha256 != intent.settings.digest
        or receipt.retained_startup_settings_sha256 != intent.settings.digest
    ):
        raise ValueError("website settings receipt is missing or differs from the slot snapshot")


def execute_odoo_cutover(
    plan: OdooCutoverPlan,
    store: OdooCutoverStore,
    provider: OdooCutoverProvider,
    *,
    record_id: str,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    checkpoint: Callable[[str], None] = lambda _step: None,
    settings_checkpoint: Callable[[], None] = lambda: None,
    abort_only: bool = False,
) -> OdooCutoverRecord:
    """The caller supplies existing accepted-release/worker and backup authority.

    No fallback to in-place deployment, guessed maintenance, or image-only
    recovery is permitted. A provider failure leaves the recorded intent open.
    """
    try:
        admitted = store.load(record_id)
    except FileNotFoundError:
        plan = OdooCutoverPlan.model_validate(plan.model_dump())
    else:
        if admitted.plan != plan:
            raise ValueError("cannot change the stored cutover plan")
        plan = OdooCutoverRecord.model_validate(admitted.model_dump()).plan
    with provider.exclusive(plan):
        try:
            record = store.load(record_id)
        except FileNotFoundError:
            if abort_only:
                raise ValueError("abort-only continuation cannot create a cutover")
            provider.preflight(plan)
            _require_route(provider.current_route(plan), plan, plan.old)
            record = OdooCutoverRecord(
                record_id=record_id, plan=plan, began_at=clock(), updated_at=clock()
            )
            store.save(record)
        record = OdooCutoverRecord.model_validate(record.model_dump())
        if record.plan != plan:
            raise ValueError("cannot change an uncertain cutover's immutable release plan")
        if abort_only and record.step != "aborted" and not backup_recovery_eligible(record):
            raise ValueError("abort-only continuation requires its existing pre-maintenance record")
        if record.step in ODOO_CUTOVER_TERMINAL_STEPS:
            slot = plan.new if record.step == "complete" else plan.old
            if slot == plan.new:
                if record.maintenance is None:
                    raise ValueError("completed cutover lacks maintenance receipt")
                require_website_receipt(
                    record.maintenance.website_settings, slot, plan.website_settings
                )
            else:
                require_website_receipt(
                    record.rollback_website_settings,
                    slot,
                    plan.rollback_website_settings if record.website_settings_attempted else None,
                )
            route = provider.current_route(plan)
            _require_route(route, plan, slot)
            record = record.model_copy(update={"route": route, "updated_at": clock()})
            checkpoint("terminal_audit")
            store.save(record)
            return record
        if record.step == "rolling_back":
            raise ValueError("recorded rollback must resume through the rollback executor")

        def advance(step: CutoverStep, **values: object) -> None:
            nonlocal record
            checkpoint(step)
            current = store.load(record_id)
            if current.step != record.step:
                raise RuntimeError("the original cutover advanced under another continuation")
            record = current.model_copy(
                update={"step": step, "updated_at": clock(), "error": "", **values}
            )
            store.save(record)

        try:
            if record.step == "aborting":
                return abort_before_maintenance(
                    store, provider, record_id, clock=clock, checkpoint=checkpoint
                )
            if record.step == "preparing":
                checkpoint("prepare_candidate")
                provider.prepare_candidate(plan)
                site = None
                if (
                    plan.compatibility.classification == "database_changing"
                    or plan.website_settings is not None
                ):
                    site = provider.prepare_public(plan)
                    verify_snapshot(site)
                    if site.plan.binding != plan.old.binding or site.plan.writers != plan.writers:
                        raise ValueError("prepared serving is not bound to current runtime/writers")
                advance("prepared", public_site=site)
            if record.step == "prepared":
                advance("pausing" if record.public_site else "backing_up")
            if record.step == "pausing":
                assert record.public_site is not None
                pause_store = _PauseStore(store, record_id, checkpoint=checkpoint)
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
                try:
                    if abort_only:
                        provider.cancel_backup_restore(plan)
                        advance("aborting")
                        return abort_before_maintenance(
                            store, provider, record_id, clock=clock, checkpoint=checkpoint
                        )
                    backup = provider.backup(plan)
                except BackupVerificationRejected:
                    if not backup_recovery_eligible(store.load(record_id)):
                        raise ValueError("backup failure cannot abort an unverified pause")
                    advance("aborting")
                    return abort_before_maintenance(
                        store, provider, record_id, clock=clock, checkpoint=checkpoint
                    )
                if backup.binding != plan.old.binding or backup.image != plan.old.image:
                    raise ValueError(
                        "backup is not the coherent current image/database/filestore tuple"
                    )
                earliest = record.pause.drained_at if record.pause else record.began_at
                if earliest is None or not earliest <= backup.recovery_point_at <= clock():
                    raise ValueError(
                        "backup recovery point must follow verified drain/release start"
                    )
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
                if plan.website_settings is not None:
                    record = store.load(record_id).model_copy(
                        update={"website_settings_attempted": True}
                    )
                    store.save(record)
                    settings_checkpoint()
                receipt = provider.maintenance(plan)
                if (
                    receipt.release_id != plan.release_id
                    or receipt.binding != plan.new.binding
                    or receipt.image != plan.new.image
                    or receipt.candidate_manifest_sha256
                    != plan.compatibility.candidate_manifest_sha256
                    or not set(receipt.install_modules) <= set(plan.compatibility.install_modules)
                    or not set(receipt.update_modules) <= set(plan.compatibility.update_modules)
                    or (
                        plan.compatibility.classification == "compatible"
                        and (receipt.install_modules or receipt.update_modules)
                    )
                ):
                    raise ValueError("maintenance receipt does not match complete candidate plan")
                require_website_receipt(receipt.website_settings, plan.new, plan.website_settings)
                advance("ready", maintenance=receipt)
            if record.step in {"ready", "switching", "resuming", "draining"}:
                if record.maintenance is None:
                    raise ValueError("cutover progress lacks its persisted maintenance receipt")
                require_website_receipt(
                    record.maintenance.website_settings, plan.new, plan.website_settings
                )
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
                        record.public_site,
                        _PauseStore(store, record_id, checkpoint=checkpoint),
                        provider,
                        clock=clock,
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
            failed_record = store.load(record_id)
            if (
                failed_record.step == record.step
                and failed_record.step not in ODOO_CUTOVER_TERMINAL_STEPS
            ):
                checkpoint("record_failure")
                record = failed_record.model_copy(update={"error": type(error).__name__})
                store.save(record)
            raise
        return store.load(record_id)


def abort_before_maintenance(
    store: OdooCutoverStore,
    provider: OdooCutoverProvider,
    record_id: str,
    *,
    clock: Callable[[], datetime],
    checkpoint: Callable[[str], None] = lambda _step: None,
) -> OdooCutoverRecord:
    record = store.load(record_id)
    if record.step != "aborting" or not backup_recovery_eligible(record):
        raise ValueError("abort requires rejected backup verification before maintenance")
    plan = record.plan
    provider.preflight(plan)
    route = (
        provider.current_route(plan)
        if record.pause is None or record.pause.state != "drained"
        else None
    )
    if route is not None:
        _require_route(route, plan, plan.old)
    elif record.pause is not None:
        assert record.public_site is not None
        try:
            current = provider.current_route(plan)
        except (ValueError, OSError):
            current = None
        if current is not None and current.slot_id == plan.old.slot_id:
            _require_recovery_route(current, plan, plan.old)
            route = current
        else:
            if not provider.owns_prepared_pause(record.pause.pause_id):
                raise ValueError("abort lacks its original prepared-route authority")
            _require_ready(provider.readiness(plan, plan.old), plan, plan.old, clock())
            route = provider.switch(plan, plan.old)
            _require_route(route, plan, plan.old)
    if route is None:
        raise ValueError("abort has no verified old serving route")
    if record.pause is not None:
        assert record.public_site is not None
        PreparedPublicPause(
            record.public_site,
            _PauseStore(store, record_id, checkpoint=checkpoint),
            provider,
            clock=clock,
        ).finish(record_id, expected=plan.old.binding)
    provider.handoff_writers(plan, plan.old)
    checkpoint("aborted")
    ended = clock()
    record = store.load(record_id).model_copy(
        update={"step": "aborted", "route": route, "updated_at": ended, "ended_at": ended}
    )
    store.save(record)
    return record


def rollback_compatible_cutover(
    store: OdooCutoverStore,
    provider: OdooCutoverProvider,
    record_id: str,
    *,
    clock: Callable[[], datetime] = lambda: datetime.now(UTC),
    settings_checkpoint: Callable[[], None] = lambda: None,
    checkpoint: Callable[[str], None] = lambda _step: None,
) -> OdooCutoverRecord:
    record = OdooCutoverRecord.model_validate(store.load(record_id).model_dump())
    plan = record.plan
    rollback_pause_id = rollback_pause_identity(record_id)
    if plan.compatibility.classification != "compatible":
        raise ValueError("database-changing rollback requires an authorized coherent tuple restore")
    provider.preflight(plan)
    with provider.exclusive(plan):
        if record.step == "rolled_back":
            require_website_receipt(
                record.rollback_website_settings,
                plan.old,
                plan.rollback_website_settings if record.website_settings_attempted else None,
            )
            _require_route(provider.current_route(plan), plan, plan.old)
            return record
        if record.step != "rolling_back":
            route = provider.current_route(plan)
            if (
                record.public_site is not None
                and provider.observed_public_copy(record_id) == record.public_site.content_digest
            ):
                pass
            else:
                slot = plan.old if route.slot_id == plan.old.slot_id else plan.new
                _require_recovery_route(route, plan, slot)
            started_at = clock()
            record = record.model_copy(
                update={
                    "step": "rolling_back",
                    "updated_at": started_at,
                    "rollback_started_at": started_at,
                }
            )
            checkpoint("rollback_started")
            store.save(record)
        intent = plan.rollback_website_settings if record.website_settings_attempted else None
        if intent is not None:
            if record.public_site is None:
                raise ValueError("website rollback lacks its prepared public site")
            pause_store = _PauseStore(store, record_id, rollback=True, checkpoint=checkpoint)
            pause = record.rollback_pause
            if pause is None:
                pause = PublicPauseRecord(
                    pause_id=rollback_pause_id,
                    binding=plan.old.binding,
                    content_digest=record.public_site.content_digest,
                    began_at=clock(),
                )
                pause_store.save(pause)
            if pause.state == "fencing":
                if provider.publish(record.public_site, rollback_pause_id) != pause.content_digest:
                    raise ValueError("rollback prepared serving publication was not read back")
                PreparedPublicPause(record.public_site, pause_store, provider, clock=clock).drain(
                    rollback_pause_id
                )
            if record.rollback_website_settings is None:
                # A crash before the durable receipt reuses the idempotent native
                # operation under prepared serving; a saved receipt avoids
                # changing the DB again after an uncertain successful switch.
                if (
                    provider.observed_public_copy(rollback_pause_id)
                    != record.public_site.content_digest
                ):
                    raise ValueError("rollback settings require prepared serving authority")
                settings_checkpoint()
                receipt = provider.website_settings(plan, plan.old, intent)
                require_website_receipt(receipt, plan.old, intent)
                record = store.load(record_id).model_copy(
                    update={"rollback_website_settings": receipt}
                )
                checkpoint("rollback_settings_receipt")
                store.save(record)
            require_website_receipt(record.rollback_website_settings, plan.old, intent)
        route = provider.current_route(plan)
        if route.slot_id != plan.old.slot_id:
            if intent is not None:
                assert record.public_site is not None
                if (
                    provider.observed_public_copy(rollback_pause_id)
                    != record.public_site.content_digest
                ):
                    raise ValueError("rollback lost prepared serving authority")
            elif (
                record.public_site is None
                or provider.observed_public_copy(record_id) != record.public_site.content_digest
            ):
                _require_recovery_route(route, plan, plan.new)
            _require_ready(provider.readiness(plan, plan.old), plan, plan.old, clock())
            route = provider.switch(plan, plan.old)
        _require_route(route, plan, plan.old)
        if intent is not None:
            assert record.public_site is not None
            recovered = PreparedPublicPause(
                record.public_site,
                _PauseStore(store, record_id, rollback=True, checkpoint=checkpoint),
                provider,
                clock=clock,
            ).finish(rollback_pause_id, expected=plan.old.binding)
            if record.pause is not None and record.pause.state != "complete":
                assert recovered.ended_at is not None
                pause = record.pause.model_copy(
                    update={
                        "state": "complete",
                        "ended_at": recovered.ended_at,
                        "duration_seconds": (
                            recovered.ended_at - record.pause.began_at
                        ).total_seconds(),
                        "recovery_binding": plan.old.binding,
                    }
                )
                checkpoint("rollback_pause_complete")
                store.save(store.load(record_id).model_copy(update={"pause": pause}))
        elif (
            record.public_site is not None
            and record.pause is not None
            and record.pause.state in {"drained", "resuming"}
        ):
            PreparedPublicPause(
                record.public_site,
                _PauseStore(store, record_id, checkpoint=checkpoint),
                provider,
                clock=clock,
            ).finish(record_id, expected=plan.old.binding)
        else:
            provider.handoff_writers(plan, plan.old)
        checkpoint("rolled_back")
        record = store.load(record_id).model_copy(
            update={"step": "rolled_back", "route": route, "updated_at": clock()}
        )
        store.save(record)
        return record
