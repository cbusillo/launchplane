"""Assemble native cutover consumers around installed provider-host capabilities.

Host code is installed with the service, never named by an HTTP request or a
product record. Records still own every lane coordinate. Discovery and builders
must only bind/read; effects remain inside the accepted release's exclusive guard.
"""

from collections.abc import Callable
from dataclasses import dataclass, replace
from importlib.metadata import entry_points
from pathlib import Path
from typing import Protocol, cast

from control_plane.contracts.generic_web_cutover import GenericWebCutoverPlan
from control_plane.contracts.odoo_cutover import OdooCutoverPlan, OdooSlot
from control_plane.contracts.odoo_cutover_lane import OdooCutoverLane
from control_plane.contracts.generic_web_cutover import GenericWebCutoverLane
from control_plane.dokploy.cutover_factory import DokployOdooCutoverProviderFactory
from control_plane.dokploy.generic_web_cutover import DokployWebCutoverProvider, WebSlotDriver
from control_plane.dokploy.odoo_cutover import OdooSlotDriver
from control_plane.dokploy.slot_maintenance import ContainerCommand, run_planned_slot_maintenance
from control_plane.dokploy.slot_website_settings import StartupSettings, run_slot_website_settings
from control_plane.dokploy.slot_writers import ContainerSlotWriters
from control_plane.dokploy.traefik_cutover import TraefikFileRoute
from control_plane.dokploy.source import read_dokploy_config
from control_plane.generic_web_cutover import WebCutoverAuthorityStore, compile_web_cutover
from control_plane.odoo_cutover_plan import CutoverAuthorityStore
from control_plane.workflows.odoo_cutover import OdooCutoverProvider
from control_plane.workflows.generic_web_cutover import WebCutoverProvider


@dataclass(frozen=True)
class OdooHostCapabilities:
    driver: OdooSlotDriver
    writers: ContainerSlotWriters
    execute: ContainerCommand
    runner: Callable[[OdooSlot], str]
    startup: StartupSettings | None = None
    startup_preflight: Callable[[OdooCutoverPlan], None] | None = None


class InstalledCutoverHost(Protocol):
    def odoo(
        self, plan: OdooCutoverPlan, lane: OdooCutoverLane, store: object, root: Path
    ) -> OdooHostCapabilities: ...

    def web(self, plan: GenericWebCutoverPlan, store: object, root: Path) -> WebSlotDriver: ...

    def route(self, lane: OdooCutoverLane | GenericWebCutoverLane) -> TraefikFileRoute: ...


def installed_cutover_host() -> InstalledCutoverHost:
    """Use Python's installed-package discovery; no module/command input channel."""
    candidates = tuple(entry_points(group="launchplane.cutover_hosts", name="dokploy"))
    if len(candidates) != 1:
        raise ValueError(
            "cutover_host_capability_unavailable: install exactly one Dokploy host "
            "capability in the API and worker packages before lane activation"
        )
    return cast(InstalledCutoverHost, candidates[0].load()())


def bind_odoo_driver(plan: OdooCutoverPlan, capabilities: OdooHostCapabilities) -> OdooSlotDriver:
    writers = capabilities.writers
    if writers.plan != plan:
        raise ValueError("writer capability differs from the admitted plan")

    def preflight(current: OdooCutoverPlan) -> None:
        if current != plan:
            raise ValueError("startup capability crossed its release binding")
        if capabilities.startup is None or capabilities.startup_preflight is None:
            raise ValueError("bound website settings/startup capability is unavailable")
        capabilities.startup_preflight(current)

    return replace(
        capabilities.driver,
        fence_and_drain=writers.fence,
        handoff_writers=lambda current, slot: writers.handoff(slot),
        maintenance=lambda current: run_planned_slot_maintenance(
            current,
            runner_container=capabilities.runner(current.new),
            execute=capabilities.execute,
            configure_startup=capabilities.startup,
            writers=writers,
        ),
        website_settings_preflight=preflight,
        website_settings=lambda current, slot, intent: run_slot_website_settings(
            current,
            slot,
            intent,
            runner_container=capabilities.runner(slot),
            execute=capabilities.execute,
            configure_startup=capabilities.startup,
            writers=writers,
        ),
    )


def service_odoo_cutover_factory(
    store: object, root: Path
) -> Callable[[OdooCutoverPlan], OdooCutoverProvider]:
    def provider(plan: OdooCutoverPlan) -> OdooCutoverProvider:
        host: InstalledCutoverHost | None = None

        def driver(current: OdooCutoverPlan, lane: OdooCutoverLane) -> OdooSlotDriver:
            nonlocal host
            host = installed_cutover_host()
            return bind_odoo_driver(current, host.odoo(current, lane, store, root))

        def route(lane: OdooCutoverLane) -> TraefikFileRoute:
            assert host is not None
            return host.route(lane)

        return DokployOdooCutoverProviderFactory(
            cast(CutoverAuthorityStore, store), driver=driver, route=route
        )(plan)

    return provider


def service_web_cutover_factory(
    store: object, root: Path
) -> Callable[[GenericWebCutoverPlan], WebCutoverProvider]:
    def provider(plan: GenericWebCutoverPlan) -> DokployWebCutoverProvider:
        records = cast(WebCutoverAuthorityStore, store)
        identity = plan.new.runtime_identity
        profile = records.read_product_profile_record(identity.product)
        lane = next(
            (
                lane
                for lane in profile.lanes
                if (lane.context, lane.instance) == (identity.context, identity.instance)
            ),
            None,
        )
        if lane is None:
            raise ValueError("cutover differs from current persisted lane/artifact authority")
        expected = compile_web_cutover(
            records,
            product=profile.product,
            lane=lane,
            artifact_id=plan.candidate.artifact_id,
            source_git_ref=plan.candidate.source_commit,
            deploy_reference=plan.new.deploy_reference,
            record_id=plan.release_id,
        )
        if expected != plan:
            raise ValueError("cutover differs from current persisted lane/artifact authority")
        host = installed_cutover_host()
        provider_host, token = read_dokploy_config(control_plane_root=root)
        return DokployWebCutoverProvider(
            plan,
            host=provider_host,
            token=token,
            driver=host.web(plan, store, root),
            route=host.route(plan.lane),
        )

    return provider
