"""Move Launchplane-owned credentials out of the app runtime environment store.

Backup and rollback workers need SSH material for one lane, and Launchplane itself
uses a GitHub token and an advisory GitHub App key. Stored with the runtime
environment, app delivery could reach them, so they move to stores no app reads.
Record and binding ids stay the same, so later rotations find the moved record by
its new integration.
"""

from __future__ import annotations

import sqlalchemy as sa

RUNTIME_ENVIRONMENT_INTEGRATION = "runtime_environment"
LAUNCHPLANE_WORKER_INTEGRATION = "launchplane_worker"
LAUNCHPLANE_SERVICE_INTEGRATION = "launchplane_service"
MISSPELLED_RUNTIME_ENVIRONMENT_INTEGRATION = "runtime-environment"
SERVICE_BINDING_KEYS = ("GITHUB_TOKEN", "LAUNCHPLANE_ADVISORY_GITHUB_APP_PRIVATE_KEY")
WORKER_BINDING_KEYS = (
    "PRODUCTION_BACKUP_SSH_PRIVATE_KEY",
    "PRODUCTION_BACKUP_SSH_KNOWN_HOSTS",
    "VERIREEL_PROD_PROXMOX_SSH_PRIVATE_KEY",
    "VERIREEL_PROD_PROXMOX_SSH_KNOWN_HOSTS",
)

_SECRETS = sa.table(
    "launchplane_secrets",
    sa.column("secret_id", sa.String),
    sa.column("scope", sa.String),
    sa.column("integration", sa.String),
    sa.column("name", sa.String),
    sa.column("context", sa.String),
    sa.column("instance", sa.String),
    sa.column("status", sa.String),
    sa.column("payload", sa.JSON),
)
_BINDINGS = sa.table(
    "launchplane_secret_bindings",
    sa.column("binding_id", sa.String),
    sa.column("secret_id", sa.String),
    sa.column("integration", sa.String),
    sa.column("binding_key", sa.String),
    sa.column("instance", sa.String),
    sa.column("status", sa.String),
    sa.column("payload", sa.JSON),
)


def move_worker_secrets(connection: sa.Connection, *, source: str, destination: str) -> int:
    """Move lane-scoped worker credentials between stores; returns records moved."""
    return move_secrets(
        connection,
        source=source,
        destination=destination,
        binding_keys=WORKER_BINDING_KEYS,
        scopes=("context_instance",),
    )


def move_service_secrets(connection: sa.Connection, *, source: str, destination: str) -> int:
    """Move Launchplane's global or context-shared service credentials between stores."""
    return move_secrets(
        connection,
        source=source,
        destination=destination,
        binding_keys=SERVICE_BINDING_KEYS,
        scopes=("global", "context"),
    )


def set_integration_status(connection: sa.Connection, *, integration: str, status: str) -> int:
    """Set every record and binding of one integration to a status; returns records changed."""
    secrets = connection.execute(
        sa.select(_SECRETS.c.secret_id, _SECRETS.c.payload).where(
            _SECRETS.c.integration == integration
        )
    ).all()
    for secret in secrets:
        connection.execute(
            _SECRETS.update()
            .where(_SECRETS.c.secret_id == secret.secret_id)
            .values(status=status, payload={**secret.payload, "status": status})
        )
    for binding in connection.execute(
        sa.select(_BINDINGS.c.binding_id, _BINDINGS.c.payload).where(
            _BINDINGS.c.integration == integration
        )
    ).all():
        connection.execute(
            _BINDINGS.update()
            .where(_BINDINGS.c.binding_id == binding.binding_id)
            .values(status=status, payload={**binding.payload, "status": status})
        )
    return len(secrets)


def move_secrets(
    connection: sa.Connection,
    *,
    source: str,
    destination: str,
    binding_keys: tuple[str, ...],
    scopes: tuple[str, ...],
) -> int:
    """Move secrets with these binding keys and scopes between stores; returns records moved.

    A record moves only when every configured binding on it is one of ``binding_keys``,
    so a secret also bound under another key stays resolvable where it is. A record whose
    identity already exists in the destination stays too, rather than failing the move.
    """
    candidate_ids = {
        row.secret_id
        for row in connection.execute(
            sa.select(_BINDINGS.c.secret_id).where(
                _BINDINGS.c.integration == source,
                _BINDINGS.c.binding_key.in_(binding_keys),
                _BINDINGS.c.status == "configured",
            )
        )
    }
    if not candidate_ids:
        return 0
    all_bindings = connection.execute(
        sa.select(
            _BINDINGS.c.binding_id,
            _BINDINGS.c.secret_id,
            _BINDINGS.c.binding_key,
            _BINDINGS.c.status,
            _BINDINGS.c.payload,
        ).where(_BINDINGS.c.secret_id.in_(candidate_ids))
    ).all()
    bound_elsewhere = {
        binding.secret_id
        for binding in all_bindings
        if binding.status == "configured" and binding.binding_key not in binding_keys
    }
    destination_identities = {
        (row.scope, row.name, row.context, row.instance)
        for row in connection.execute(
            sa.select(
                _SECRETS.c.scope, _SECRETS.c.name, _SECRETS.c.context, _SECRETS.c.instance
            ).where(_SECRETS.c.integration == destination)
        )
    }
    secrets = [
        secret
        for secret in connection.execute(
            sa.select(
                _SECRETS.c.secret_id,
                _SECRETS.c.scope,
                _SECRETS.c.name,
                _SECRETS.c.context,
                _SECRETS.c.instance,
                _SECRETS.c.payload,
            ).where(
                _SECRETS.c.secret_id.in_(candidate_ids - bound_elsewhere),
                _SECRETS.c.integration == source,
                _SECRETS.c.scope.in_(scopes),
            )
        ).all()
        if (secret.scope, secret.name, secret.context, secret.instance)
        not in destination_identities
    ]
    moved_secret_ids = {secret.secret_id for secret in secrets}
    for binding in all_bindings:
        if binding.secret_id not in moved_secret_ids:
            continue
        connection.execute(
            _BINDINGS.update()
            .where(_BINDINGS.c.binding_id == binding.binding_id)
            .values(
                integration=destination,
                payload={**binding.payload, "integration": destination},
            )
        )
    for secret in secrets:
        connection.execute(
            _SECRETS.update()
            .where(_SECRETS.c.secret_id == secret.secret_id)
            .values(
                integration=destination,
                payload={**secret.payload, "integration": destination},
            )
        )
    return len(moved_secret_ids)
