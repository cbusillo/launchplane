"""Resolve only the credential source declared by a merge-train policy."""

import logging
import os
from pathlib import Path

import click
from sqlalchemy.exc import SQLAlchemyError

from control_plane import secrets
from control_plane.contracts.merge_train_policy import MergeTrainGitHubTokenSource
from control_plane.github_app_identity import GitHubAppIdentity, mint_merge_train_installation_token
from control_plane.workflows.launchplane import resolve_launchplane_github_token

MERGE_TRAIN_GITHUB_APP_SECRET_INTEGRATION = "merge_train_github_app"
_LOGGER = logging.getLogger(__name__)


def resolve_merge_train_github_token(
    *, source: MergeTrainGitHubTokenSource, control_plane_root: Path, repository: str = ""
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
            return mint_merge_train_installation_token(
                identity=GitHubAppIdentity(app_id=app.app_id, private_key=private_key),
                repository=repository,
                repository_id=str(app.repository_id),
            ).token
        except (click.ClickException, SQLAlchemyError, OSError, TypeError, ValueError) as error:
            _LOGGER.warning(
                "Merge train App credentials unavailable: resolution_failed (%s)",
                type(error).__name__,
            )
            return ""
    if source.runtime_context:
        try:
            return resolve_launchplane_github_token(
                control_plane_root=control_plane_root,
                context_name=source.runtime_context,
            )
        except (click.ClickException, SQLAlchemyError, OSError, ValueError):
            return ""
    if source.env_var:
        return os.environ.get(source.env_var, "").strip()
    return ""
