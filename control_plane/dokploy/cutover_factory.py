"""Bind service-installed host capabilities to current DB-backed lane records."""

from collections.abc import Callable
from control_plane.contracts.odoo_cutover import OdooCutoverPlan
from control_plane.contracts.odoo_cutover_lane import OdooCutoverLane
from control_plane.dokploy.odoo_cutover import DokployOdooCutoverProvider, OdooSlotDriver
from control_plane.dokploy.traefik_cutover import TraefikFileRoute
from control_plane.odoo_cutover_plan import (
    CutoverAuthorityStore,
    cutover_record_id,
    resolve_profile_cutover_plan,
)


class DokployOdooCutoverProviderFactory:
    """Host adapters are capabilities, not data/commands accepted over HTTP.

    The service's deployed provider host supplies its transport, managed-env,
    browser, backup, writers, exclusive lease and watched-file mount. Both
    builders only bind/read; all effects run under the accepted worker's guard.
    No capability default manufactures access or activates a site.
    """

    def __init__(
        self,
        store: CutoverAuthorityStore,
        *,
        driver: Callable[[OdooCutoverPlan, OdooCutoverLane], OdooSlotDriver],
        route: Callable[[OdooCutoverLane], TraefikFileRoute],
    ):
        self.store, self.driver, self.route = store, driver, route

    def __call__(self, plan: OdooCutoverPlan) -> DokployOdooCutoverProvider:
        expected = resolve_profile_cutover_plan(
            self.store,
            product=plan.new.binding.product_id,
            context=plan.new.runtime_identity.context,
            instance=plan.new.binding.environment_id,
            artifact_id=plan.candidate.artifact_id,
            release_id=plan.release_id,
        )
        if expected is not None:
            expected = expected.model_copy(
                update={
                    "website_settings": plan.website_settings,
                    "production_website_settings": plan.production_website_settings,
                }
            )
        if expected != plan:
            raise ValueError("cutover differs from current persisted lane/artifact authority")
        profile = self.store.read_product_profile_record(plan.new.binding.product_id)
        current_lane = next(
            (
                lane
                for lane in profile.lanes
                if lane.context == plan.new.runtime_identity.context
                and lane.instance == plan.new.binding.environment_id
            ),
            None,
        )
        lane = current_lane.odoo_cutover if current_lane is not None else None
        if lane is None:
            raise ValueError("cutover differs from current persisted lane/artifact authority")
        driver = self.driver(plan, lane)
        if driver.lane != lane:
            raise ValueError("host capability differs from its admitted lane")
        return DokployOdooCutoverProvider(
            plan, driver, self.route(lane), pause_id=cutover_record_id(plan)
        )
