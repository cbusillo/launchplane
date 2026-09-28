"""Move worker-only lane credentials out of the runtime environment store.

Backup and rollback workers need SSH material for one lane. Storing it with the
lane's runtime environment lets app delivery reach it, so it moves to the
Launchplane worker store. Record and binding ids stay the same, so later
rotations find the moved record by its new integration.
"""

from __future__ import annotations

import sqlalchemy as sa

RUNTIME_ENVIRONMENT_INTEGRATION = "runtime_environment"
LAUNCHPLANE_WORKER_INTEGRATION = "launchplane_worker"
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
    sa.column("payload", sa.JSON),
)
_BINDINGS = sa.table(
    "launchplane_secret_bindings",
    sa.column("binding_id", sa.String),
    sa.column("secret_id", sa.String),
    sa.column("integration", sa.String),
    sa.column("binding_key", sa.String),
    sa.column("instance", sa.String),
    sa.column("payload", sa.JSON),
)


def move_worker_secrets(connection: sa.Connection, *, source: str, destination: str) -> int:
    """Move lane-scoped worker credentials between stores; returns records moved."""
    bindings = connection.execute(
        sa.select(_BINDINGS.c.binding_id, _BINDINGS.c.secret_id, _BINDINGS.c.payload).where(
            _BINDINGS.c.integration == source,
            _BINDINGS.c.binding_key.in_(WORKER_BINDING_KEYS),
            _BINDINGS.c.instance != "",
        )
    ).all()
    lane_secret_ids = {
        row.secret_id
        for row in connection.execute(
            sa.select(_SECRETS.c.secret_id).where(
                _SECRETS.c.secret_id.in_({binding.secret_id for binding in bindings}),
                _SECRETS.c.integration == source,
                _SECRETS.c.scope == "context_instance",
            )
        )
    }
    for binding in bindings:
        if binding.secret_id not in lane_secret_ids:
            continue
        connection.execute(
            _BINDINGS.update()
            .where(_BINDINGS.c.binding_id == binding.binding_id)
            .values(
                integration=destination,
                payload={**binding.payload, "integration": destination},
            )
        )
    for secret in connection.execute(
        sa.select(_SECRETS.c.secret_id, _SECRETS.c.payload).where(
            _SECRETS.c.secret_id.in_(lane_secret_ids)
        )
    ).all():
        connection.execute(
            _SECRETS.update()
            .where(_SECRETS.c.secret_id == secret.secret_id)
            .values(
                integration=destination,
                payload={**secret.payload, "integration": destination},
            )
        )
    return len(lane_secret_ids)
