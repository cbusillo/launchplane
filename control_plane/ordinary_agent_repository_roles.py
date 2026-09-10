"""Positive repository-admin evidence shared only within one provider observation."""

from collections.abc import Callable

from control_plane.ordinary_agent_github_transport import OrdinaryAgentProviderEvidenceError


class OrdinaryRepositoryAdminObservation:
    """Lazy, bounded read; missing evidence never grants a repository role.

    Each snapshot or landing creates a new instance. No author role survives
    across observations, even when the PR head commit is unchanged.
    """

    def __init__(self, *, request: Callable[[str], object], repository_path: str) -> None:
        self._request = request
        self._path = f"/repos/{repository_path}/collaborators?permission=admin&per_page=100&page=1"
        self._admins: frozenset[tuple[int, str]] | None = None
        self._failure: Exception | None = None

    def is_admin(self, actor_id: int, login: str) -> bool:
        if self._failure is not None:
            raise self._failure
        if self._admins is None:
            try:
                self._admins = self._read_admins()
            except Exception as error:
                # One failed observation stays failed; callers cannot turn it
                # into repeated requests by asking about another author.
                self._failure = error
                raise
        return (actor_id, login.casefold()) in self._admins

    def _read_admins(self) -> frozenset[tuple[int, str]]:
        payload = self._request(self._path)
        if not isinstance(payload, list) or len(payload) > 100:
            raise OrdinaryAgentProviderEvidenceError("repository_admin_observation_malformed")
        admins = set()
        for item in payload:
            if not isinstance(item, dict):
                continue
            identity, username, permissions = (
                item.get("id"),
                item.get("login"),
                item.get("permissions"),
            )
            if (
                isinstance(identity, int)
                and not isinstance(identity, bool)
                and identity > 0
                and isinstance(username, str)
                and username.strip()
                and isinstance(permissions, dict)
                and permissions.get("admin") is True
            ):
                admins.add((identity, username.casefold()))
        return frozenset(admins)
