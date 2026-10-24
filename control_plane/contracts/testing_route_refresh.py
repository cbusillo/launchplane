"""Private source digests held stable through a native route attestation write."""

from datetime import datetime
from control_plane.contracts.route_binding_record import EnvironmentRouteBindingRecord

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


def is_native_testing_binding(record: EnvironmentRouteBindingRecord) -> bool:
    return record.instance == "testing" and (
        record.source.source_label == "service:testing-rebuild"
        or bool({"testing_publication", "testing_route"} & record.source.source_versions.keys())
    )
