"""Verified repository-bound issued credential and its provider-confirmed scopes."""

from dataclasses import dataclass, field


@dataclass(frozen=True, slots=True)
class GitHubAppInstallationToken:
    token: str = field(repr=False)
    app_id: int
    installation_id: int
    repository_id: int
    repository: str
    expires_at: str
    permissions: tuple[str, ...] = ()
    app_slug: str = ""
    repository_private: bool | None = None
