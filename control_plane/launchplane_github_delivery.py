"""Launchplane-owned GitHub delivery identity, independent of train credentials."""

import logging
from pathlib import Path

import click
from sqlalchemy.exc import SQLAlchemyError

from control_plane import runtime_environments, secrets
from control_plane.github_app_identity import GitHubAppIdentity, mint_delivery_installation_token
from control_plane.contracts.repository_inventory import RepositoryInventoryRecord
from control_plane.product_repository_identity import current_tracked_inventory_record
from control_plane.storage.factory import resolve_database_url
from control_plane.storage.filesystem import FilesystemRecordStore
from control_plane.storage.postgres import PostgresRecordStore

DELIVERY_GITHUB_APP_ID_KEY = "LAUNCHPLANE_DELIVERY_GITHUB_APP_ID"
DELIVERY_GITHUB_APP_INTEGRATION_KEY = "LAUNCHPLANE_DELIVERY_GITHUB_APP_INTEGRATION"
_SERVICE_CONTEXT = "launchplane"
_PERMISSIONS = {
    "repository_read": {"contents": "read", "pull_requests": "read"},
    "pull_request_feedback": {"contents": "read", "pull_requests": "write"},
    "source_issue_feedback": {
        "contents": "read",
        "pull_requests": "write",
        "issues": "write",
    },
    "release_record": {"issues": "write"},
    "workflow_dispatch": {"actions": "write"},
    "release_publish": {"contents": "write"},
    "admission_status": {
        "checks": "read",
        "contents": "read",
        "pull_requests": "read",
        "statuses": "write",
    },
    "admission_merge": {
        "administration": "read",
        "checks": "read",
        "contents": "write",
        "pull_requests": "read",
        "statuses": "read",
    },
    "admission_read": {
        "administration": "read",
        "checks": "read",
        "contents": "read",
        "pull_requests": "read",
        "statuses": "read",
    },
}
_LOGGER = logging.getLogger(__name__)


def resolve_delivery_github_app_identity(*, control_plane_root: Path) -> GitHubAppIdentity:
    values = runtime_environments.resolve_runtime_context_values(
        control_plane_root=control_plane_root, context_name=_SERVICE_CONTEXT
    )
    app_id = values.get(DELIVERY_GITHUB_APP_ID_KEY, "").strip()
    integration = values.get(DELIVERY_GITHUB_APP_INTEGRATION_KEY, "").strip()
    if not app_id.isdecimal() or int(app_id) < 1 or not integration:
        raise ValueError("Launchplane delivery GitHub App identity is not configured.")
    private_key = secrets.resolve_context_secret_value(
        integration=integration, context_name=_SERVICE_CONTEXT, binding_key="private_key"
    )
    if not private_key:
        raise ValueError("Launchplane delivery GitHub App key binding is unavailable.")
    return GitHubAppIdentity(app_id=int(app_id), private_key=private_key.replace("\\n", "\n"))


def resolve_delivery_github_app_id(*, control_plane_root: Path) -> int:
    return resolve_delivery_github_app_identity(control_plane_root=control_plane_root).app_id


def _tracked_repository(control_plane_root: Path, repository: str) -> RepositoryInventoryRecord:
    database_url = resolve_database_url()
    store: PostgresRecordStore | FilesystemRecordStore = (
        PostgresRecordStore(database_url=database_url)
        if database_url
        else FilesystemRecordStore(control_plane_root / "state")
    )
    try:
        return current_tracked_inventory_record(
            repository=repository.strip().lower(),
            inventory_records=store.list_repository_inventory_records(),
        )
    finally:
        if isinstance(store, PostgresRecordStore):
            store.close()


def delivery_github_credentials_ready(*, control_plane_root: Path, repository: str) -> bool:
    """Check configured credentials and inventory without creating a provider token.

    Accepted installation grants are verified by the actual operation's mint.
    """
    try:
        resolve_delivery_github_app_identity(control_plane_root=control_plane_root)
        _tracked_repository(control_plane_root, repository)
        return True
    except (click.ClickException, SQLAlchemyError, OSError, TypeError, ValueError, KeyError):
        return False


def resolve_delivery_github_token(
    *, control_plane_root: Path, context_name: str, repository: str, purpose: str
) -> str:
    if not repository.strip():
        return ""
    try:
        permissions = _PERMISSIONS[purpose]
        identity = resolve_delivery_github_app_identity(control_plane_root=control_plane_root)
        repository = repository.strip().lower()
        inventory = _tracked_repository(control_plane_root, repository)
        return mint_delivery_installation_token(
            identity=identity,
            repository=repository,
            repository_id=inventory.repository_id,
            permissions=permissions,
        ).token
    except (
        click.ClickException,
        SQLAlchemyError,
        OSError,
        TypeError,
        ValueError,
        KeyError,
    ) as error:
        _LOGGER.warning("Delivery App credentials unavailable (%s).", type(error).__name__)
        return ""
