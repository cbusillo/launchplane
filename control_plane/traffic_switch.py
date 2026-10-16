"""Shared private-readiness and applied/public authority checks for app slots."""

from datetime import datetime
from typing import Protocol

from control_plane.contracts.promotion_record import HealthcheckEvidence
from control_plane.contracts.runtime_identity import RuntimeIdentity


class ServingRoute(Protocol):
    @property
    def route_id(self) -> str: ...
    @property
    def host(self) -> str: ...
    @property
    def target_url(self) -> str: ...
    @property
    def slot_id(self) -> str: ...
    @property
    def public_health(self) -> HealthcheckEvidence | None: ...


def require_serving_route(
    observation: ServingRoute,
    *,
    route_id: str,
    host: str,
    target_url: str,
    slot_id: str,
    identity: RuntimeIdentity,
) -> None:
    require_route_authority(
        observation, route_id=route_id, host=host, target_url=target_url, slot_id=slot_id
    )
    health = observation.public_health
    if (
        health is None
        or not health.verified
        or health.status != "pass"
        or health.observed_runtime_identity != identity
    ):
        raise ValueError("applied route/public identity differs from expected slot")


def require_route_authority(
    observation: ServingRoute, *, route_id: str, host: str, target_url: str, slot_id: str
) -> None:
    if (
        observation.route_id != route_id
        or observation.host != host
        or observation.target_url != target_url
        or observation.slot_id != slot_id
    ):
        raise ValueError("applied traffic authority differs from the exact slot")


def require_fresh_readiness(
    *, observed_at: datetime, now: datetime, actual: RuntimeIdentity, expected: RuntimeIdentity
) -> None:
    if actual != expected or not 0 <= (now - observed_at).total_seconds() <= 60:
        raise ValueError("private readiness identity is wrong, stale or unavailable")
