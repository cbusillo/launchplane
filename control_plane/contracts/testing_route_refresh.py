"""Private source digests held stable through a native route attestation write."""

from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING
from control_plane.contracts.route_binding_record import (
    EnvironmentRouteBindingRecord,
    RouteBindingDomain,
)

if TYPE_CHECKING:
    from control_plane.dokploy.testing_route import TestingLaneRoute

from control_plane.contracts.prepared_public_site import FrozenRecord


class TestingRouteRefreshSources(FrozenRecord):
    product: str
    context: str
    namespace: str
    target_id: str
    publication_key: str
    publication_sha256: str
    candidate_sha256: str
    snapshot_sha256: str
    profile_sha256: str
    observed_at: datetime


def testing_route_domains(route: TestingLaneRoute) -> tuple[RouteBindingDomain, ...]:
    return (
        RouteBindingDomain(domain_name=route.host),
        *(RouteBindingDomain(domain_name=alias, role="alias") for alias in route.aliases),
    )


def is_native_testing_binding(record: EnvironmentRouteBindingRecord) -> bool:
    return record.instance == "testing" and (
        record.source.source_label == "service:testing-rebuild"
        or bool({"testing_publication", "testing_route"} & record.source.source_versions.keys())
    )
