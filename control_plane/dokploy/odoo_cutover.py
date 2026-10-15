"""Compose-slot and atomic Traefik adapter for the release orchestrator.

The existing Odoo driver supplies private runtime/backup operations. This
adapter never calls compose.deploy on the serving project, never installs a
second public router, and never uses publication acknowledgement as readiness.
"""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from control_plane.contracts.odoo_cutover_lane import OdooCutoverLane
from control_plane.contracts.promotion_record import HealthcheckEvidence

from control_plane.contracts.odoo_cutover import (
    OdooCutoverPlan,
    OdooMaintenanceReceipt,
    OdooRecoveryTuple,
    OdooRouteObservation,
    OdooSlot,
    OdooSlotReadiness,
    OdooWebsiteSettingsReceipt,
)
from control_plane.contracts.prepared_public_site import (
    PreparedPublicSite,
    PublicRecoveryObservation,
    PublicSiteBinding,
    WriterObservation,
)
from control_plane.dokploy.traefik_cutover import TraefikFileRoute
from control_plane.prepared_public_site import verify_snapshot
from control_plane.odoo_cutover_plan import rollback_pause_identity
from control_plane.contracts.odoo_website_settings import OdooWebsiteSettingsIntent


@dataclass(frozen=True)
class OdooSlotDriver:
    """Driver capabilities bound to an admitted lane by the service worker.

    Every effect is exclusive/idempotent at the provider. Maintenance must
    invoke devkit's full --post-deploy-maintenance with the complete plan and
    read a persisted run receipt; a started run without a receipt is uncertain.
    Neither a generic provider footer nor installed-module states prove updates.
    """

    exclusive: Callable[[OdooCutoverPlan], AbstractContextManager[None]]
    prepare_candidate: Callable[[OdooCutoverPlan], None]
    prepare_public: Callable[[OdooCutoverPlan], PreparedPublicSite]
    backup: Callable[[OdooCutoverPlan], OdooRecoveryTuple]
    maintenance: Callable[[OdooCutoverPlan], OdooMaintenanceReceipt]
    readiness: Callable[[OdooCutoverPlan, OdooSlot], OdooSlotReadiness]
    observe_public: Callable[
        [OdooCutoverPlan, str], tuple[str, PublicSiteBinding, HealthcheckEvidence | None]
    ]
    stage_public: Callable[[PreparedPublicSite, str], str]
    observe_prepared: Callable[[str], str]
    fence_and_drain: Callable[[PreparedPublicSite, str], tuple[WriterObservation, ...]]
    handoff_writers: Callable[[OdooCutoverPlan, OdooSlot], tuple[tuple[str, int], ...]]
    drain_old: Callable[[OdooCutoverPlan], bool]
    load_prepared: Callable[[str], PreparedPublicSite]
    lane: OdooCutoverLane
    verify_endpoint: Callable[[OdooCutoverPlan, OdooSlot], HealthcheckEvidence]

    website_settings_preflight: Callable[[OdooCutoverPlan], None] | None = None
    website_settings: (
        Callable[[OdooCutoverPlan, OdooSlot, OdooWebsiteSettingsIntent], OdooWebsiteSettingsReceipt]
        | None
    ) = None


class DokployOdooCutoverProvider:
    def __init__(
        self,
        plan: OdooCutoverPlan,
        driver: OdooSlotDriver,
        route: TraefikFileRoute,
        *,
        pause_id: str,
    ) -> None:
        if route.target.route_id != plan.route_id or route.target.host != plan.host:
            raise ValueError("provider route does not belong to the release lane")
        lane = driver.lane
        if (
            lane.route_id != plan.route_id
            or lane.host != plan.host
            or lane.database != plan.old.binding.database
            or lane.database_uuid != plan.old.runtime_identity.database_uuid
            or lane.writers != plan.writers
            or {(slot.slot_id, slot.compose_id, slot.private_url) for slot in lane.slots}
            != {(slot.slot_id, slot.compose_id, slot.private_url) for slot in (plan.old, plan.new)}
        ):
            raise ValueError("release request coordinates differ from the admitted lane record")
        self.plan, self.driver, self.route = plan, driver, route
        self.pause_id = pause_id
        self.prepared_url: str | None = None

    def exclusive(self, plan: OdooCutoverPlan) -> AbstractContextManager[None]:
        if plan != self.plan:
            raise ValueError("cannot cross provider/release binding")
        return self.driver.exclusive(plan)

    def prepare_candidate(self, plan: OdooCutoverPlan) -> None:
        if plan.rollback_website_settings is not None and (
            self.driver.website_settings is None or self.driver.website_settings_preflight is None
        ):
            raise ValueError("bound website settings/startup capability is unavailable")
        if plan.rollback_website_settings is not None:
            assert self.driver.website_settings_preflight is not None
            self.driver.website_settings_preflight(plan)
        self.driver.prepare_candidate(plan)
        current = self.current_route(plan)
        if current.slot_id != plan.old.slot_id or current.binding != plan.old.binding:
            raise ValueError("candidate preparation changed the serving authority")

    def prepare_public(self, plan: OdooCutoverPlan) -> PreparedPublicSite:
        site = self.driver.prepare_public(plan)
        verify_snapshot(site)
        return site

    def current_route(self, plan: OdooCutoverPlan) -> OdooRouteObservation:
        target = self.route.observed_target()
        slot_id, binding, health = self.driver.observe_public(plan, target)
        return OdooRouteObservation(
            route_id=plan.route_id,
            host=plan.host,
            target_url=target,
            slot_id=slot_id,
            binding=binding,
            evidence_id=self.route.evidence_id(),
            applied=True,
            authoritative_router_count=1,
            public_health=health,
        )

    def publish(self, site: PreparedPublicSite, pause_id: str) -> str:
        verify_snapshot(site)
        if (
            pause_id not in (self.pause_id, rollback_pause_identity(self.pause_id))
            or site.plan.binding != self.plan.old.binding
        ):
            raise ValueError("prepared publication differs from its persisted pause binding")
        self.prepared_url = self.driver.stage_public(site, pause_id)
        if (
            pause_id not in (self.pause_id, rollback_pause_identity(self.pause_id))
            or self.prepared_url != self.driver.lane.prepared_origin
        ):
            raise ValueError("prepared endpoint/pause differs from its authoritative binding")
        expected: tuple[str, ...] = (self.plan.old.private_url, self.prepared_url)
        if pause_id == rollback_pause_identity(self.pause_id):
            expected += (self.plan.new.private_url,)
        self.route.publish(self.prepared_url, expected=expected)
        return self.observed_public_copy(pause_id)

    def observed_public_copy(self, pause_id: str) -> str:
        # Read the prepared server itself and the applied route, not memory.
        target = self.route.observed_target()
        if target != self.driver.lane.prepared_origin or pause_id not in (
            self.pause_id,
            rollback_pause_identity(self.pause_id),
        ):
            return ""
        try:
            site = self.driver.load_prepared(pause_id)
        except FileNotFoundError:
            return ""
        verify_snapshot(site)
        if site.plan.binding != self.plan.old.binding:
            raise ValueError("prepared record crosses the release binding")
        digest = self.driver.observe_prepared(pause_id)
        return digest if digest == site.content_digest else ""

    def fence_and_drain(
        self, site: PreparedPublicSite, pause_id: str
    ) -> tuple[WriterObservation, ...]:
        return self.driver.fence_and_drain(site, pause_id)

    def backup(self, plan: OdooCutoverPlan) -> OdooRecoveryTuple:
        return self.driver.backup(plan)

    def maintenance(self, plan: OdooCutoverPlan) -> OdooMaintenanceReceipt:
        return self.driver.maintenance(plan)

    def website_settings(
        self, plan: OdooCutoverPlan, slot: OdooSlot, intent: OdooWebsiteSettingsIntent
    ) -> OdooWebsiteSettingsReceipt:
        if self.driver.website_settings is None:
            raise ValueError("bound website settings/startup capability is unavailable")
        return self.driver.website_settings(plan, slot, intent)

    def readiness(self, plan: OdooCutoverPlan, slot: OdooSlot) -> OdooSlotReadiness:
        ready = self.driver.readiness(plan, slot)
        health = self.driver.verify_endpoint(plan, slot)
        if (
            not health.verified
            or health.status != "pass"
            or health.observed_runtime_identity != slot.runtime_identity
            or not health.urls
            or not all(url.startswith(slot.private_url.rstrip("/") + "/") for url in health.urls)
        ):
            raise ValueError("private route endpoint differs from the ready immutable container")
        return ready

    def switch(self, plan: OdooCutoverPlan, slot: OdooSlot) -> OdooRouteObservation:
        expected: tuple[str, ...] = (plan.old.private_url, plan.new.private_url)
        if self.prepared_url:
            expected += (self.prepared_url,)
        # Restart has no local prepared_url: recover it from applied authority
        # only after the driver verifies the exact prepared pause bundle.
        observed = self.route.observed_target()
        if observed == self.driver.lane.prepared_origin or observed not in expected:
            if observed != self.driver.lane.prepared_origin or not any(
                self.observed_public_copy(pause_id)
                for pause_id in (self.pause_id, rollback_pause_identity(self.pause_id))
            ):
                raise ValueError("unknown route is not a verified prepared public copy")
            expected += (observed,)
        self.route.publish(slot.private_url, expected=expected)
        result = self.current_route(plan)
        if result.slot_id != slot.slot_id or result.binding != slot.binding:
            raise ValueError("public runtime identity differs after atomic switch")
        return result

    def handoff_writers(self, plan: OdooCutoverPlan, slot: OdooSlot) -> None:
        owners = self.driver.handoff_writers(plan, slot)
        if len(owners) != len(plan.writers) or dict(owners) != {
            writer.writer_id: 1 for writer in plan.writers
        }:
            raise ValueError("writer handoff did not read exactly one owner per target")

    def recover_and_resume(
        self, site: PreparedPublicSite, pause_id: str, expected: PublicSiteBinding
    ) -> PublicRecoveryObservation:
        slot = self.plan.new if expected == self.plan.new.binding else self.plan.old
        if expected != slot.binding:
            raise ValueError("recovery target does not match either recorded slot")
        observation = self.current_route(self.plan)
        if observation.slot_id != slot.slot_id or observation.binding != expected:
            raise ValueError("cannot resume writers before verified serving switch")
        self.handoff_writers(self.plan, slot)
        return PublicRecoveryObservation(
            binding=expected,
            pause_id=pause_id,
            serving_evidence_id=observation.evidence_id,
            writer_owners=tuple((writer.writer_id, 1) for writer in self.plan.writers),
        )

    def drain_old(self, plan: OdooCutoverPlan) -> bool:
        return self.driver.drain_old(plan)
