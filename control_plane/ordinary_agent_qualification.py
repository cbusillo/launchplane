"""One-page, read-only administrator qualification observer."""

from __future__ import annotations

from typing import Protocol

from control_plane.contracts.canonical_json import canonical_json_sha256
from control_plane.contracts.ordinary_agent_qualification import (
    OrdinaryAgentQualificationSetup,
    OrdinaryRepositoryAdminObservation,
    qualification_identity,
)
from control_plane.contracts.ordinary_agent_snapshot import OrdinaryAgentProviderRequestCounts
from control_plane.ordinary_agent_github_transport import OrdinaryAgentProviderEvidenceError


class QualificationProviderTransport(Protocol):
    rest_core_requests: int
    graphql_requests: int
    graphql_points: int

    def request(
        self, *, method: str, path: str, body: dict[str, object] | None = None
    ) -> object: ...


def observe_repository_administrator(
    *,
    transport: QualificationProviderTransport,
    setup: OrdinaryAgentQualificationSetup,
    observed_at: int,
) -> OrdinaryRepositoryAdminObservation:
    """Read exactly the first admin collaborator page and retain only relevant IDs."""
    payload = transport.request(
        method="GET",
        path=f"/repos/{setup.target.repository}/collaborators?permission=admin&per_page=100&page=1",
    )
    if not isinstance(payload, list) or len(payload) > 100:
        raise OrdinaryAgentProviderEvidenceError("provider_incomplete")
    expected = qualification_identity(
        github_id=setup.administrator_github_id, login=setup.administrator_login
    )
    id_login_changed = None
    login_id_mismatch = None
    qualified = None
    for item in payload:
        if not isinstance(item, dict):
            raise OrdinaryAgentProviderEvidenceError("provider_incomplete")
        github_id = item.get("id")
        login = item.get("login")
        permissions = item.get("permissions")
        if (
            isinstance(github_id, bool)
            or not isinstance(github_id, int)
            or github_id <= 0
            or not isinstance(login, str)
            or not login.strip()
            or not isinstance(permissions, dict)
            or type(permissions.get("admin")) is not bool
        ):
            raise OrdinaryAgentProviderEvidenceError("provider_incomplete")
        if permissions["admin"] is not True:
            continue
        identity = qualification_identity(github_id=github_id, login=login)
        if github_id == expected.github_id:
            if identity.login_normalized == expected.login_normalized:
                qualified = identity
            else:
                id_login_changed = identity
        elif identity.login_normalized == expected.login_normalized:
            login_id_mismatch = identity
    # Numeric identity is the durable activation anchor and wins over a second,
    # conflicting name match in the same page.
    if id_login_changed is not None:
        status, observed = "administrator_login_changed", id_login_changed
    elif qualified is not None:
        status, observed = "qualified", qualified
    elif len(payload) == 100:
        status, observed = "inconclusive_truncated_page", None
    elif login_id_mismatch is not None:
        status, observed = "administrator_identity_mismatch", login_id_mismatch
    else:
        status, observed = "administrator_not_admin", None
    counts = OrdinaryAgentProviderRequestCounts(
        rest_core_requests=transport.rest_core_requests,
        graphql_requests=transport.graphql_requests,
        graphql_points=transport.graphql_points,
    )
    body: dict[str, object] = {
        "schema_version": 1,
        "status": status,
        "expected": expected.model_dump(mode="json"),
        "observed": None if observed is None else observed.model_dump(mode="json"),
        "entry_count": len(payload),
        "counts": counts.model_dump(mode="json"),
        "observed_at": observed_at,
    }
    return OrdinaryRepositoryAdminObservation.model_validate(
        {**body, "observation_sha256": canonical_json_sha256(body)}
    )
