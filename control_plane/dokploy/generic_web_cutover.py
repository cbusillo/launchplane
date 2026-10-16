"""Dokploy app/Compose preparation with the shared atomic Traefik publisher."""

from collections.abc import Callable
from contextlib import AbstractContextManager
from dataclasses import dataclass

from control_plane.contracts.generic_web_cutover import (
    GenericWebCutoverPlan,
    WebSlot,
    WebSlotReadiness,
    WebRouteObservation,
)
from control_plane.contracts.deployment_record import ResolvedTargetEvidence
from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.ship_request import ShipRequest
from control_plane.dokploy.traefik_cutover import TraefikFileRoute
from control_plane.workflows.dokploy_deploy import execute_dokploy_artifact_deploy
from control_plane.workflows.generic_web_cutover import require_route, require_ready
from datetime import UTC, datetime


@dataclass(frozen=True)
class WebSlotDriver:
    """Installed capabilities, never provider coordinates supplied by a release.

    preflight resolves any immutable provider tag to the plan image digest
    before startup, checks both targets have no public routers/ports and the
    candidate startup fences background jobs. stage_runtime writes/read-backs the
    exact managed settings into only the inactive target *before* startup, and
    returns their fingerprint. It must be idempotent for this release identity.
    readiness independently inspects running image digest, settings, warming and
    fenced background jobs. handoff drains the predecessor before one successor.
    """

    exclusive: Callable[[GenericWebCutoverPlan], AbstractContextManager[None]]
    preflight: Callable[[GenericWebCutoverPlan], None]
    stage_runtime: Callable[[GenericWebCutoverPlan], str]
    readiness: Callable[[GenericWebCutoverPlan, WebSlot], WebSlotReadiness]
    private_health: Callable[[GenericWebCutoverPlan, WebSlot], HealthcheckEvidence]
    public_health: Callable[[GenericWebCutoverPlan], HealthcheckEvidence]
    handoff: Callable[[GenericWebCutoverPlan, WebSlot], dict[str, str]]
    drain: Callable[[GenericWebCutoverPlan, WebSlot], bool]


class DokployWebCutoverProvider:
    def __init__(
        self,
        plan: GenericWebCutoverPlan,
        *,
        host: str,
        token: str,
        route: TraefikFileRoute,
        driver: WebSlotDriver,
        timeout_seconds: int = 300,
    ):
        if route.target.route_id != plan.lane.route_id or route.target.host != plan.lane.host:
            raise ValueError("provider route differs from the admitted lane")
        self.plan, self.host, self.token = plan, host, token
        self.route, self.driver, self.timeout_seconds = route, driver, timeout_seconds

    def exclusive(self, plan: GenericWebCutoverPlan) -> AbstractContextManager[None]:
        if plan != self.plan:
            raise ValueError("provider is bound to another release")
        return self.driver.exclusive(plan)

    def preflight(self, plan: GenericWebCutoverPlan) -> None:
        if plan != self.plan:
            raise ValueError("provider is bound to another release")
        self.driver.preflight(plan)

    def current_route(self, plan: GenericWebCutoverPlan) -> WebRouteObservation:
        target = self.route.observed_target()
        slot = next((slot for slot in (plan.old, plan.new) if slot.private_url == target), None)
        if slot is None:
            raise ValueError("unknown applied route; no traffic authority inferred")
        try:
            public_health = self.driver.public_health(plan)
        except Exception:
            # Applied traffic authority remains independently observable when
            # a broken candidate times out. Forward progress still needs health;
            # rollback can privately verify old before restoring its exact route.
            public_health = HealthcheckEvidence(status="fail")
        return WebRouteObservation(
            route_id=plan.lane.route_id,
            host=plan.lane.host,
            target_url=target,
            slot_id=slot.slot_id,
            evidence_id=self.route.evidence_id(),
            applied=True,
            authoritative_router_count=1,
            public_health=public_health,
        )

    def prepare_candidate(self, plan: GenericWebCutoverPlan) -> None:
        require_route(self.current_route(plan), plan, plan.old)
        self.preflight(plan)
        if self.driver.stage_runtime(plan) != plan.lane.runtime_settings_sha256:
            raise ValueError("candidate managed settings differ from immutable release settings")
        slot = plan.new
        request = ShipRequest(
            artifact_id=slot.image,
            deploy_reference=slot.deploy_reference,
            context=slot.runtime_identity.context,
            instance=slot.runtime_identity.instance,
            source_git_ref=slot.runtime_identity.source_git_ref,
            target_type=slot.target_type,
            target_name=slot.slot_id,
            deploy_mode="dokploy-" + slot.target_type + "-api",
            provider_id="dokploy",
            target_category=slot.target_type,
            provider_target_type=slot.target_type,
            wait=True,
            verify_health=False,
        )
        execute_dokploy_artifact_deploy(
            host=self.host,
            token=self.token,
            ship_request=request,
            resolved_target=ResolvedTargetEvidence(
                target_type=slot.target_type, target_id=slot.target_id, target_name=slot.slot_id
            ),
            deploy_timeout_seconds=self.timeout_seconds,
            runtime_identity=slot.runtime_identity,
            deployment_title=plan.release_id,
        )
        require_route(self.current_route(plan), plan, plan.old)

    def readiness(self, plan: GenericWebCutoverPlan, slot: WebSlot) -> WebSlotReadiness:
        ready = self.driver.readiness(plan, slot)
        require_ready(
            ready,
            plan,
            slot,
            datetime.now(UTC),
            require_fenced=slot != plan.old or self.route.observed_target() != slot.private_url,
        )
        health = self.driver.private_health(plan, slot)
        if (
            not health.verified
            or health.status != "pass"
            or health.observed_runtime_identity != slot.runtime_identity
            or not health.urls
            or not all(url.startswith(slot.private_url.rstrip("/") + "/") for url in health.urls)
        ):
            raise ValueError("private endpoint does not serve the inspected immutable runtime")
        return ready

    def switch(self, plan: GenericWebCutoverPlan, slot: WebSlot) -> WebRouteObservation:
        self.route.publish(slot.private_url, expected=(plan.old.private_url, plan.new.private_url))
        observed = self.current_route(plan)
        require_route(observed, plan, slot)
        return observed

    def handoff_writers(self, plan: GenericWebCutoverPlan, slot: WebSlot) -> None:
        if self.driver.handoff(plan, slot) != {
            writer: slot.target_id for writer in plan.lane.writers
        }:
            raise ValueError("writer handoff lacks exactly one observed owner per target")

    def drain(self, plan: GenericWebCutoverPlan, slot: WebSlot) -> bool:
        return self.driver.drain(plan, slot)
