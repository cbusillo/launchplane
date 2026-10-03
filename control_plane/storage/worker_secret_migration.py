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
COPIED_VERSION_SUFFIX = "-version-copied-from-global"
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
    sa.column("current_version_id", sa.String),
    sa.column("updated_at", sa.String),
    sa.column("payload", sa.JSON),
)
_BINDINGS = sa.table(
    "launchplane_secret_bindings",
    sa.column("binding_id", sa.String),
    sa.column("secret_id", sa.String),
    sa.column("integration", sa.String),
    sa.column("binding_key", sa.String),
    sa.column("context", sa.String),
    sa.column("instance", sa.String),
    sa.column("status", sa.String),
    sa.column("updated_at", sa.String),
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


_VERSIONS = sa.table(
    "launchplane_secret_versions",
    sa.column("version_id", sa.String),
    sa.column("secret_id", sa.String),
    sa.column("created_at", sa.String),
    sa.column("payload", sa.JSON),
)
_AUDIT_EVENTS = sa.table(
    "launchplane_secret_audit_events",
    sa.column("event_id", sa.String),
    sa.column("secret_id", sa.String),
    sa.column("event_type", sa.String),
    sa.column("recorded_at", sa.String),
    sa.column("payload", sa.JSON),
)


def _slug(value: str) -> str:
    compact = "".join(
        character.lower() if character.isalnum() else "-" for character in value.strip()
    )
    return "-".join(part for part in compact.split("-") if part) or "secret"


def copy_global_secret_to_contexts(
    connection: sa.Connection,
    *,
    integration: str,
    binding_key: str,
    contexts: tuple[str, ...],
    recorded_at: str,
) -> tuple[str, ...]:
    """Copy one configured global secret into a context-scoped record for each context.

    The copy reuses the current ciphertext, so no value is decrypted. A context that
    already has its own configured copy is left alone. Returns the contexts copied to.
    """
    bound_ids = connection.execute(
        sa.select(_BINDINGS.c.secret_id).where(
            _BINDINGS.c.integration == integration,
            _BINDINGS.c.binding_key == binding_key,
            _BINDINGS.c.status == "configured",
        )
    ).all()
    global_rows = connection.execute(
        sa.select(_SECRETS.c.secret_id, _SECRETS.c.payload).where(
            _SECRETS.c.secret_id.in_({row.secret_id for row in bound_ids}),
            _SECRETS.c.integration == integration,
            _SECRETS.c.scope == "global",
            _SECRETS.c.status == "configured",
        )
    ).all()
    if len(global_rows) != 1:
        return ()
    source = global_rows[0].payload
    version: dict[str, object] = connection.execute(
        sa.select(_VERSIONS.c.payload).where(_VERSIONS.c.version_id == source["current_version_id"])
    ).scalar_one()
    has_own_copy = {
        row.context
        for row in connection.execute(
            sa.select(_BINDINGS.c.context).where(
                _BINDINGS.c.integration == integration,
                _BINDINGS.c.binding_key == binding_key,
                _BINDINGS.c.status == "configured",
                _BINDINGS.c.context.in_(contexts),
                _BINDINGS.c.instance == "",
            )
        )
    }
    copied: list[str] = []
    for context in contexts:
        if context in has_own_copy:
            continue
        secret_id = "-".join(
            _slug(part) for part in ("secret", integration, source["name"], context)
        )
        if connection.execute(
            sa.select(_SECRETS.c.secret_id).where(_SECRETS.c.secret_id == secret_id)
        ).first():
            continue
        version_id = f"{secret_id}{COPIED_VERSION_SUFFIX}"
        binding_id = f"{secret_id}-binding-{_slug(binding_key)}"
        secret_payload = {
            **source,
            "secret_id": secret_id,
            "scope": "context",
            "context": context,
            "instance": "",
            "current_version_id": version_id,
            "created_at": recorded_at,
            "updated_at": recorded_at,
            "updated_by": "migration",
        }
        version_payload = {
            **version,
            "version_id": version_id,
            "secret_id": secret_id,
            "created_at": recorded_at,
            "created_by": "migration",
        }
        binding_payload = {
            "schema_version": 1,
            "binding_id": binding_id,
            "secret_id": secret_id,
            "integration": integration,
            "binding_type": "env",
            "binding_key": binding_key,
            "context": context,
            "instance": "",
            "status": "configured",
            "created_at": recorded_at,
            "updated_at": recorded_at,
        }
        connection.execute(
            _SECRETS.insert().values(
                secret_id=secret_id,
                scope="context",
                integration=integration,
                name=source["name"],
                context=context,
                instance="",
                status="configured",
                current_version_id=version_id,
                updated_at=recorded_at,
                payload=secret_payload,
            )
        )
        connection.execute(
            _VERSIONS.insert().values(
                version_id=version_id,
                secret_id=secret_id,
                created_at=recorded_at,
                payload=version_payload,
            )
        )
        connection.execute(
            _BINDINGS.insert().values(
                binding_id=binding_id,
                secret_id=secret_id,
                integration=integration,
                binding_key=binding_key,
                context=context,
                instance="",
                status="configured",
                updated_at=recorded_at,
                payload=binding_payload,
            )
        )
        event_id = f"{secret_id}-event-imported-copied-from-global"
        connection.execute(
            _AUDIT_EVENTS.insert().values(
                event_id=event_id,
                secret_id=secret_id,
                event_type="imported",
                recorded_at=recorded_at,
                payload={
                    "schema_version": 1,
                    "event_id": event_id,
                    "secret_id": secret_id,
                    "event_type": "imported",
                    "recorded_at": recorded_at,
                    "actor": "migration",
                    "detail": "Copied from the global secret so the site stores its own.",
                    "metadata": {"source_secret_id": source["secret_id"]},
                },
            )
        )
        copied.append(context)
    return tuple(copied)


def remove_copied_secrets(
    connection: sa.Connection, *, integration: str, contexts: tuple[str, ...]
) -> int:
    """Remove only the copies ``copy_global_secret_to_contexts`` created; returns count.

    A copy is recognised by its migration-made current version, so one an admin has
    rotated since is kept. Each record is locked while it is checked and deleted.
    """
    removed = 0
    candidates = connection.execute(
        sa.select(_SECRETS.c.secret_id).where(
            _SECRETS.c.integration == integration,
            _SECRETS.c.scope == "context",
            _SECRETS.c.context.in_(contexts),
        )
    ).all()
    for candidate in candidates:
        secret_id = candidate.secret_id
        locked = connection.execute(
            sa.select(_SECRETS.c.current_version_id)
            .where(_SECRETS.c.secret_id == secret_id)
            .with_for_update()
        ).first()
        if locked is None or locked.current_version_id != f"{secret_id}{COPIED_VERSION_SUFFIX}":
            continue
        for table in (_AUDIT_EVENTS, _BINDINGS, _VERSIONS, _SECRETS):
            connection.execute(table.delete().where(table.c.secret_id == secret_id))
        removed += 1
    return removed
