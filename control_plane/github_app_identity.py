from __future__ import annotations

from control_plane.contracts.ordinary_agent_provider import (
    ordinary_agent_enrollment_effect_profiles as ordinary_agent_enrollment_effect_profiles,
    ordinary_agent_enrollment_permissions as ordinary_agent_enrollment_permissions,
)

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
_ORDINARY_AGENT_EFFECT_PERMISSION_CEILINGS: dict[str, dict[str, str]] = {
    "guarded_merge": {
        "contents": "write",
        "metadata": "read",
        "pull_requests": "read",
    },
    "head_refresh": {
        "contents": "write",
        "metadata": "read",
        "pull_requests": "write",
    },
    "merge_train_snapshot": {
        "administration": "read",
        "checks": "read",
        "contents": "read",
        "metadata": "read",
        "pull_requests": "read",
        "statuses": "read",
    },
    "close_pull_request": {"metadata": "read", "pull_requests": "write"},
    "comment_pull_request": {"metadata": "read", "pull_requests": "write"},
    "label_pull_request": {"metadata": "read", "pull_requests": "write"},
}
_ORDINARY_AGENT_INSTALLATION_PERMISSION_CEILING = dict(
    permission.split(":", 1) for permission in ordinary_agent_enrollment_permissions()
)

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
    permissions: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GitHubAppInstallationInspection:
    """App installation evidence bound to a DB-inventory repository identity.

    ``repository_id`` and ``repository`` are validated caller inputs, not fields
    returned by the App-JWT installation endpoint. App, installation, account,
    and permission values are provider-observed.
    """

    app_id: int
    installation_id: int
    repository_id: int
    repository_owner_id: int
    repository: str
    permissions: tuple[str, ...]


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
    return _mint_repository_installation_token(
        identity=identity,
        repository=repository,
        repository_id=repository_id,
        requested_permissions={"checks": "write"},
        required_installation_permissions={"checks": "write"},
        allowed_installation_permissions=_ALLOWED_INSTALLATION_PERMISSIONS,
        allowed_token_permissions=_ALLOWED_INSTALLATION_PERMISSIONS,
        identity_label="Launchplane advisory GitHub App",
        permission_boundary_label="advisory check projection",
        api_request=api_request,
        now=now,
    )


def mint_ordinary_agent_installation_token(
    *,
    identity: GitHubAppIdentity,
    repository: str,
    repository_id: str,
    effect_profile: str,
    api_request: GitHubApiRequest = github_api_request,
    now: datetime | None = None,
    before_token_mint: Callable[[int, int], None] | None = None,
) -> GitHubAppInstallationToken:
    ceiling = _ORDINARY_AGENT_EFFECT_PERMISSION_CEILINGS.get(effect_profile)
    if ceiling is None:
        raise GitHubAppIdentityError("Ordinary-agent GitHub effect profile is unsupported.")
    requested = {key: value for key, value in ceiling.items() if key != "metadata"}
    return _mint_repository_installation_token(
        identity=identity,
        repository=repository,
        repository_id=repository_id,
        requested_permissions=requested,
        required_installation_permissions=_ORDINARY_AGENT_INSTALLATION_PERMISSION_CEILING,
        allowed_installation_permissions=_ORDINARY_AGENT_INSTALLATION_PERMISSION_CEILING,
        allowed_token_permissions=ceiling,
        identity_label="Ordinary-agent GitHub App",
        permission_boundary_label="selected profile",
        api_request=api_request,
        now=now,
        before_token_mint=before_token_mint,
    )


def ordinary_agent_effect_permissions(effect_profile: str) -> tuple[str, ...]:
    ceiling = _ORDINARY_AGENT_EFFECT_PERMISSION_CEILINGS.get(effect_profile)
    if ceiling is None:
        raise GitHubAppIdentityError("Ordinary-agent GitHub effect profile is unsupported.")
    return tuple(
        f"{permission}:{access}"
        for permission, access in sorted(ceiling.items())
        if permission != "metadata"
    )


def inspect_ordinary_agent_github_app_installation(
    *,
    identity: GitHubAppIdentity,
    repository: str,
    repository_id: str,
    repository_owner_id: str,
    api_request: GitHubApiRequest = github_api_request,
    now: datetime | None = None,
) -> GitHubAppInstallationInspection:
    """Inspect an App installation without minting a repository token."""
    normalized_repository = repository.strip()
    if normalized_repository.count("/") != 1:
        raise GitHubAppIdentityError("GitHub App repository must use owner/name.")
    owner, repo = normalized_repository.split("/", 1)
    if (
        not owner
        or not repo
        or not repository_id.isdecimal()
        or int(repository_id) < 1
        or not repository_owner_id.isdecimal()
        or int(repository_owner_id) < 1
    ):
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
        raise GitHubAppIdentityError("Ordinary-agent GitHub App private key is invalid.") from error
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
    numeric_repository_id = int(repository_id)
    numeric_repository_owner_id = int(repository_owner_id)
    installation_payload = json_object(
        _github_api_request(
            api_request,
            path=f"/repos/{quote(owner, safe='')}/{quote(repo, safe='')}/installation",
            token=app_jwt,
        ),
        "GitHub App installation response",
        error_type=GitHubAppIdentityError,
    )
    account_payload = json_object(
        installation_payload.get("account"),
        "GitHub App installation account",
        error_type=GitHubAppIdentityError,
    )
    if (
        required_positive_int(
            account_payload.get("id"),
            "GitHub App installation account requires id.",
            error_type=GitHubAppIdentityError,
        )
        != numeric_repository_owner_id
        or required_string_text(
            account_payload.get("login"),
            "GitHub App installation account requires login.",
            error_type=GitHubAppIdentityError,
        ).casefold()
        != owner.casefold()
    ):
        raise GitHubAppIdentityError(
            "GitHub App installation account does not match repository inventory owner."
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
    expected_permissions = {
        permission: access
        for permission, access in (
            item.split(":", 1) for item in ordinary_agent_enrollment_permissions()
        )
    }
    observed_permissions = _validate_permissions(
        installation_payload.get("permissions"),
        label="installation",
        required_permissions=expected_permissions,
        allowed_permissions=expected_permissions,
        permission_boundary_label="ordinary-agent enrollment",
    )
    return GitHubAppInstallationInspection(
        app_id=identity.app_id,
        installation_id=installation_id,
        repository_id=numeric_repository_id,
        repository_owner_id=numeric_repository_owner_id,
        repository=normalized_repository,
        permissions=tuple(
            f"{permission}:{access}" for permission, access in sorted(observed_permissions.items())
        ),
    )


def _mint_repository_installation_token(
    *,
    identity: GitHubAppIdentity,
    repository: str,
    repository_id: str,
    requested_permissions: Mapping[str, str],
    required_installation_permissions: Mapping[str, str],
    allowed_installation_permissions: Mapping[str, str],
    allowed_token_permissions: Mapping[str, str],
    identity_label: str,
    permission_boundary_label: str,
    api_request: GitHubApiRequest,
    now: datetime | None,
    before_token_mint: Callable[[int, int], None] | None = None,
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
        raise GitHubAppIdentityError(f"{identity_label} private key is invalid.") from error
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
    _validate_permissions(
        installation_payload.get("permissions"),
        label="installation",
        required_permissions=required_installation_permissions,
        allowed_permissions=allowed_installation_permissions,
        permission_boundary_label=permission_boundary_label,
    )
    if before_token_mint is not None:
        before_token_mint(identity.app_id, installation_id)
    token_payload = json_object(
        _github_api_request(
            api_request,
            path=f"/app/installations/{installation_id}/access_tokens",
            token=app_jwt,
            method="POST",
            body={
                "repository_ids": [int(repository_id)],
                "permissions": dict(requested_permissions),
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
        observed_permissions = _validate_permissions(
            token_payload.get("permissions"),
            label="installation token",
            required_permissions=requested_permissions,
            allowed_permissions=allowed_token_permissions,
            permission_boundary_label=permission_boundary_label,
        )
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
            permissions=tuple(
                f"{permission}:{access}"
                for permission, access in sorted(observed_permissions.items())
                if permission != "metadata"
            ),
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


def _validate_permissions(
    value: object,
    *,
    label: str,
    required_permissions: Mapping[str, str],
    allowed_permissions: Mapping[str, str],
    permission_boundary_label: str,
) -> dict[str, str]:
    if not isinstance(value, Mapping):
        raise GitHubAppIdentityError(f"GitHub App {label} permissions are malformed.")
    observed = {str(key): str(permission) for key, permission in value.items()}
    missing = {
        key: permission
        for key, permission in required_permissions.items()
        if observed.get(key) != permission
    }
    if missing:
        raise GitHubAppIdentityError(f"GitHub App {label} lacks required permission.")
    unexpected = {
        key: permission
        for key, permission in observed.items()
        if key not in allowed_permissions or allowed_permissions[key] != permission
    }
    if unexpected:
        raise GitHubAppIdentityError(
            f"GitHub App {label} grants permissions beyond {permission_boundary_label}."
        )
    return observed


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
