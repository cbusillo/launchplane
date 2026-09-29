"""Copy global plain runtime settings into chosen sites' own settings.

A site's environment (``resolve_site_runtime_environment``) leaves global settings
out, so a site that relies on one needs its own copy before its paths move onto
that resolver.
"""

from __future__ import annotations

from typing import Any

import sqlalchemy as sa

_RUNTIME_ENVIRONMENTS = sa.table(
    "launchplane_runtime_environments",
    sa.column("scope", sa.String),
    sa.column("context", sa.String),
    sa.column("instance", sa.String),
    sa.column("updated_at", sa.String),
    sa.column("payload", sa.JSON),
)


def copy_global_settings_to_contexts(
    connection: sa.Connection,
    *,
    keys: tuple[str, ...],
    contexts: tuple[str, ...],
    recorded_at: str,
    source_label: str,
) -> dict[str, tuple[str, ...]]:
    """Copy each global setting in ``keys`` into each context's own settings.

    A context that already has its own value for a key keeps it. A key with no
    global value is skipped. The context row is locked while it is merged, so a
    settings save during the migration is not overwritten. Returns the keys copied,
    by context.
    """
    global_payload: dict[str, Any] | None = connection.execute(
        sa.select(_RUNTIME_ENVIRONMENTS.c.payload).where(
            _RUNTIME_ENVIRONMENTS.c.scope == "global",
            _RUNTIME_ENVIRONMENTS.c.context == "",
            _RUNTIME_ENVIRONMENTS.c.instance == "",
        )
    ).scalar_one_or_none()
    global_env: dict[str, object] = (global_payload or {}).get("env") or {}
    copyable = {key: global_env[key] for key in keys if key in global_env}
    copied: dict[str, tuple[str, ...]] = {}
    for context in contexts:
        where = (
            _RUNTIME_ENVIRONMENTS.c.scope == "context",
            _RUNTIME_ENVIRONMENTS.c.context == context,
            _RUNTIME_ENVIRONMENTS.c.instance == "",
        )
        payload: dict[str, Any] | None = connection.execute(
            sa.select(_RUNTIME_ENVIRONMENTS.c.payload).where(*where).with_for_update()
        ).scalar_one_or_none()
        env: dict[str, object] = dict((payload or {}).get("env") or {})
        added = tuple(key for key in copyable if key not in env)
        if not added:
            continue
        env.update({key: copyable[key] for key in added})
        if payload is None:
            connection.execute(
                sa.insert(_RUNTIME_ENVIRONMENTS).values(
                    scope="context",
                    context=context,
                    instance="",
                    updated_at=recorded_at,
                    payload={
                        "schema_version": 1,
                        "scope": "context",
                        "context": context,
                        "instance": "",
                        "env": env,
                        "updated_at": recorded_at,
                        "source_label": source_label,
                    },
                )
            )
        else:
            connection.execute(
                sa.update(_RUNTIME_ENVIRONMENTS)
                .where(*where)
                .values(
                    updated_at=recorded_at,
                    payload={**payload, "env": env, "updated_at": recorded_at},
                )
            )
        copied[context] = added
    return copied
