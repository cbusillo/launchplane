"""Resolve only the credential source declared by a merge-train policy."""

import logging
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from pathlib import Path

import click
from sqlalchemy.exc import SQLAlchemyError

from control_plane import secrets
from control_plane.contracts.merge_train_policy import MergeTrainGitHubTokenSource
from control_plane.github_app_identity import (
    GitHubAppIdentity,
    GitHubAppPermissionError,
    mint_merge_train_installation_token,
)
from collections import OrderedDict
from threading import Lock
from control_plane.github_app_identity import GitHubAppInstallationToken

MERGE_TRAIN_GITHUB_APP_SECRET_INTEGRATION = "merge_train_github_app"
_LOGGER = logging.getLogger(__name__)
# Existing issued credentials stay private and process-local. Scope and key
# changes start cold; refresh before expiry rather than extending a token.
_TOKEN_CACHE: OrderedDict[tuple[int, int, str, str], GitHubAppInstallationToken] = OrderedDict()
_TOKEN_CACHE_LOCK = Lock()
_TOKEN_CACHE_CAPACITY = 256
_TOKEN_REFRESH_MARGIN = timedelta(minutes=5)


def invalidate_cached_merge_train_github_token(token: str) -> None:
    """Discard an issued credential after a conclusive provider auth failure."""
    with _TOKEN_CACHE_LOCK:
        for key, issued in tuple(_TOKEN_CACHE.items()):
            if issued.token == token:
                del _TOKEN_CACHE[key]


def resolve_merge_train_github_token(
    *,
    source: MergeTrainGitHubTokenSource,
    control_plane_root: Path,
    repository: str = "",
    raise_resolution_errors: bool = False,
) -> str:
    if source.github_app is not None:
        app = source.github_app
        try:
            private_key = secrets.resolve_context_secret_value(
                integration=MERGE_TRAIN_GITHUB_APP_SECRET_INTEGRATION,
                context_name=app.private_key_context,
                binding_key="private_key",
            )
            if not private_key or not repository:
                _LOGGER.warning(
                    "Merge train App credentials unavailable: exact_key_binding_missing"
                )
                return ""
            cache_key = (
                app.app_id,
                app.repository_id,
                repository.casefold(),
                sha256(private_key.encode()).hexdigest(),
            )
            now = datetime.now(timezone.utc)
            with _TOKEN_CACHE_LOCK:
                cached = _TOKEN_CACHE.get(cache_key)
                if cached is not None:
                    expires_at = datetime.fromisoformat(cached.expires_at.replace("Z", "+00:00"))
                    if expires_at > now + _TOKEN_REFRESH_MARGIN:
                        _TOKEN_CACHE.move_to_end(cache_key)
                        return cached.token
                    del _TOKEN_CACHE[cache_key]
            issued = mint_merge_train_installation_token(
                identity=GitHubAppIdentity(app_id=app.app_id, private_key=private_key),
                repository=repository,
                repository_id=str(app.repository_id),
            )
            with _TOKEN_CACHE_LOCK:
                _TOKEN_CACHE[cache_key] = issued
                _TOKEN_CACHE.move_to_end(cache_key)
                while len(_TOKEN_CACHE) > _TOKEN_CACHE_CAPACITY:
                    _TOKEN_CACHE.popitem(last=False)
            return issued.token
        except GitHubAppPermissionError as error:
            # Only schema-owned permission names/levels; never provider errors or secret values.
            _LOGGER.warning(
                "Merge train App credentials unavailable: missing_grants (%s)",
                error.required_grants,
            )
            return ""
        except (click.ClickException, SQLAlchemyError, OSError, TypeError, ValueError) as error:
            _LOGGER.warning(
                "Merge train App credentials unavailable: resolution_failed (%s)",
                type(error).__name__,
            )
            if raise_resolution_errors:
                # Scoped feedback distinguishes a real absent binding/grant
                # from a failed mint. Keep the transport cause for its existing
                # retry/denial classifier without persisting credential details.
                raise click.ClickException(
                    "Configured merge train GitHub credential resolution failed."
                ) from error
            return ""
    return ""
