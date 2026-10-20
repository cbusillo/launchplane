"""Private source digests held stable through a native route attestation write."""

from datetime import datetime

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
