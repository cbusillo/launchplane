from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import jwt
import click

from control_plane import runtime_environments
from control_plane.github_payload import json_object, required_positive_int, required_string_text
from control_plane.workflows.launchplane import github_api_request


ADVISORY_GITHUB_APP_ID_ENV_KEY = "LAUNCHPLANE_ADVISORY_GITHUB_APP_ID"
ADVISORY_GITHUB_APP_PRIVATE_KEY_ENV_KEY = "LAUNCHPLANE_ADVISORY_GITHUB_APP_PRIVATE_KEY"
_LAUNCHPLANE_SERVICE_CONTEXT = "launchplane"
_ALLOWED_INSTALLATION_PERMISSIONS = {"checks": "write", "metadata": "read"}

GitHubApiRequest = Callable[..., object]


class GitHubAppIdentityError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class GitHubAppIdentity:
    app_id: int
    private_key: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class GitHubAppInstallationToken:
    token: str = field(repr=False)
    app_id: int
    installation_id: int
    repository_id: int
    repository: str
    expires_at: str


def resolve_advisory_github_app_identity(*, control_plane_root: Path) -> GitHubAppIdentity:
    try:
        values = runtime_environments.resolve_runtime_context_values(
            control_plane_root=control_plane_root,
            context_name=_LAUNCHPLANE_SERVICE_CONTEXT,
        )
    except click.ClickException as error:
        raise GitHubAppIdentityError(
            "Launchplane advisory GitHub App identity is unavailable."
        ) from error
    raw_app_id = values.get(ADVISORY_GITHUB_APP_ID_ENV_KEY, "").strip()
    private_key = (
        values.get(ADVISORY_GITHUB_APP_PRIVATE_KEY_ENV_KEY, "").strip().replace("\\n", "\n")
    )
    if not raw_app_id.isdecimal() or int(raw_app_id) < 1 or not private_key:
        raise GitHubAppIdentityError("Launchplane advisory GitHub App identity is unavailable.")
    return GitHubAppIdentity(app_id=int(raw_app_id), private_key=private_key)


def mint_repository_installation_token(
    *,
    identity: GitHubAppIdentity,
    repository: str,
    repository_id: str,
    api_request: GitHubApiRequest = github_api_request,
    now: datetime | None = None,
) -> GitHubAppInstallationToken:
    normalized_repository = repository.strip()
    if normalized_repository.count("/") != 1:
        raise GitHubAppIdentityError("GitHub App repository must use owner/name.")
    owner, repo = normalized_repository.split("/", 1)
    if not owner or not repo or not repository_id.isdecimal() or int(repository_id) < 1:
        raise GitHubAppIdentityError("GitHub App repository identity is invalid.")
    issued_at = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    try:
        app_jwt = jwt.encode(
            {
                "iat": int((issued_at - timedelta(seconds=60)).timestamp()),
                "exp": int((issued_at + timedelta(minutes=8)).timestamp()),
                "iss": str(identity.app_id),
            },
            identity.private_key,
            algorithm="RS256",
        )
    except jwt.PyJWTError as error:
        raise GitHubAppIdentityError(
            "Launchplane advisory GitHub App private key is invalid."
        ) from error
    app_payload = json_object(
        _github_api_request(api_request, path="/app", token=app_jwt),
        "GitHub App identity response",
        error_type=GitHubAppIdentityError,
    )
    if (
        required_positive_int(
            app_payload.get("id"),
            "GitHub App identity response requires id.",
            error_type=GitHubAppIdentityError,
        )
        != identity.app_id
    ):
        raise GitHubAppIdentityError("GitHub App identity does not match configured app id.")
    installation_payload = json_object(
        _github_api_request(
            api_request,
            path=f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/installation",
            token=app_jwt,
        ),
        "GitHub App installation response",
        error_type=GitHubAppIdentityError,
    )
    installation_id = required_positive_int(
        installation_payload.get("id"),
        "GitHub App installation response requires id.",
        error_type=GitHubAppIdentityError,
    )
    if (
        required_positive_int(
            installation_payload.get("app_id"),
            "GitHub App installation response requires app_id.",
            error_type=GitHubAppIdentityError,
        )
        != identity.app_id
    ):
        raise GitHubAppIdentityError("GitHub App installation belongs to another app.")
    _validate_permissions(installation_payload.get("permissions"), label="installation")
    token_payload = json_object(
        _github_api_request(
            api_request,
            path=f"/app/installations/{installation_id}/access_tokens",
            token=app_jwt,
            method="POST",
            body={
                "repository_ids": [int(repository_id)],
                "permissions": {"checks": "write"},
            },
        ),
        "GitHub App installation token response",
        error_type=GitHubAppIdentityError,
    )
    token = required_string_text(
        token_payload.get("token"),
        "GitHub App installation token response requires token.",
        error_type=GitHubAppIdentityError,
    )
    try:
        expires_at = required_string_text(
            token_payload.get("expires_at"),
            "GitHub App installation token response requires expires_at.",
            error_type=GitHubAppIdentityError,
        )
        if _parse_github_timestamp(expires_at) <= issued_at + timedelta(minutes=1):
            raise GitHubAppIdentityError(
                "GitHub App installation token expiry is not safely in the future."
            )
        _validate_permissions(token_payload.get("permissions"), label="installation token")
        repositories = token_payload.get("repositories")
        if not isinstance(repositories, list) or len(repositories) != 1:
            raise GitHubAppIdentityError(
                "GitHub App installation token must be scoped to exactly one repository."
            )
        repository_payload = json_object(
            repositories[0],
            "GitHub App installation token repository",
            error_type=GitHubAppIdentityError,
        )
        observed_repository_id = required_positive_int(
            repository_payload.get("id"),
            "GitHub App installation token repository requires id.",
            error_type=GitHubAppIdentityError,
        )
        if observed_repository_id != int(repository_id):
            raise GitHubAppIdentityError(
                "GitHub App installation token repository does not match exact repository id."
            )
        if (
            required_string_text(
                repository_payload.get("full_name"),
                "GitHub App installation token repository requires full_name.",
                error_type=GitHubAppIdentityError,
            ).casefold()
            != normalized_repository.casefold()
        ):
            raise GitHubAppIdentityError(
                "GitHub App installation token repository does not match exact repository name."
            )
        return GitHubAppInstallationToken(
            token=token,
            app_id=identity.app_id,
            installation_id=installation_id,
            repository_id=observed_repository_id,
            repository=normalized_repository,
            expires_at=expires_at,
        )
    except Exception as validation_error:
        try:
            _revoke_installation_token_value(token=token, api_request=api_request)
        except Exception as revocation_error:
            validation_error.add_note(
                f"GitHub App installation token revocation also failed: {revocation_error}"
            )
        raise


def revoke_installation_token(
    *,
    installation_token: GitHubAppInstallationToken,
    api_request: GitHubApiRequest = github_api_request,
) -> None:
    _revoke_installation_token_value(
        token=installation_token.token,
        api_request=api_request,
    )


def _revoke_installation_token_value(
    *,
    token: str,
    api_request: GitHubApiRequest,
) -> None:
    response = _github_api_request(
        api_request,
        path="/installation/token",
        token=token,
        method="DELETE",
    )
    if response is not None:
        raise GitHubAppIdentityError(
            "GitHub App installation token revocation response must be empty."
        )


def _validate_permissions(value: object, *, label: str) -> None:
    if not isinstance(value, Mapping):
        raise GitHubAppIdentityError(f"GitHub App {label} permissions are malformed.")
    observed = {str(key): str(permission) for key, permission in value.items()}
    if observed.get("checks") != "write":
        raise GitHubAppIdentityError(f"GitHub App {label} lacks checks write permission.")
    unexpected = {
        key: permission
        for key, permission in observed.items()
        if key not in _ALLOWED_INSTALLATION_PERMISSIONS
        or _ALLOWED_INSTALLATION_PERMISSIONS[key] != permission
    }
    if unexpected:
        raise GitHubAppIdentityError(
            f"GitHub App {label} grants permissions beyond advisory check projection."
        )


def _parse_github_timestamp(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise GitHubAppIdentityError(
            "GitHub App installation token expiry is malformed."
        ) from error
    if parsed.tzinfo is None:
        raise GitHubAppIdentityError(
            "GitHub App installation token expiry must include a timezone."
        )
    return parsed.astimezone(timezone.utc)


def _github_api_request(
    api_request: GitHubApiRequest,
    **kwargs: object,
) -> object:
    try:
        return api_request(**kwargs)
    except click.ClickException as error:
        raise GitHubAppIdentityError(str(error)) from error
